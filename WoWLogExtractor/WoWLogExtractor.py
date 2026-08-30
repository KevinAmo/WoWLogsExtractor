#!/usr/bin/env python3
"""WoWLogExtractor - extract Mythic+ runs and raid boss pulls from WoW Retail combat logs.

Single file, stdlib only, Python 3.10+. Streams combat logs in binary, writes one .txt
per Mythic+ run / raid pull (original bytes preserved) plus a .json metadata sidecar.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import traceback
from collections import deque
from datetime import datetime, timedelta

APP_NAME = "WoWLogExtractor"

# --- tuning constants (see plan) --------------------------------------------------
CONTEXT_SECONDS = 10            # pre-context and trailing context around a segment
MAX_BUFFER_LINES = 5000         # hard cap for the pre-context ring buffer
BACKWARDS_JUMP_SECONDS = 30     # backwards timestamp jump that means "new session"
WARMUP_BYTES = 512 * 1024       # rewind budget used only to refill the ring buffer
READ_BLOCK = 1024 * 1024
HASH_BYTES = 256                # head/tail hash window for state validation
STALE_SECONDS = 15 * 60         # a log untouched for this long is considered finished
WATCH_INTERVAL = 2.0
MAX_COMPONENT_LEN = 60          # cap for dungeon/boss name inside a filename

LOG_GLOB_PREFIX = "WoWCombatLog"
OUTPUT_ROOT_NAME = "WoWCombatLog Extracted"
MPLUS_DIR_NAME = "MPlus"
RAID_DIR_NAME = "Raids"
STATE_FILENAME = "state.json"
CONFIG_FILENAME = "config.json"

KIND_MPLUS = "mythic_plus"
KIND_RAID = "raid"

DIFFICULTIES = {
    1: "Normal-Dungeon",
    2: "Heroic-Dungeon",
    8: "MythicKeystone",
    14: "Normal",
    15: "Heroic",
    16: "Mythic",
    17: "LFR",
    23: "Mythic-Dungeon",
    24: "Timewalking",
    33: "Timewalking",
}


# --- small helpers ----------------------------------------------------------------

def safe_print(message: str = "") -> None:
    """print() that never dies on a cp1252 console."""
    try:
        print(message)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(message.encode(encoding, "replace").decode(encoding, "replace"))


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


_TS_RE = re.compile(
    r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?[ T]+(\d{1,2}):(\d{1,2}):(\d{1,2})(?:[.,](\d{1,6}))?\s*$"
)


def parse_timestamp(text: str, default_year: int) -> datetime | None:
    """Parse 'M/D/YYYY HH:MM:SS.ffff'. Year optional (old logs) -> default_year."""
    match = _TS_RE.match(text)
    if match is None:
        return None
    month, day, year, hour, minute, second, frac = match.groups()
    try:
        if year is None:
            year_value = default_year
        else:
            year_value = int(year)
            if year_value < 100:
                year_value += 2000
        micro = int((frac or "").ljust(6, "0")) if frac else 0
        return datetime(year_value, int(month), int(day), int(hour), int(minute),
                        int(second), micro)
    except ValueError:
        return None


def split_args(text: str) -> list[str]:
    """CSV-aware split on top-level commas.

    Combat log lines are not strict CSV: quoted strings may hold commas and arguments
    may be bracketed lists such as [158,9,10] (which must stay a single argument).
    Quotes win over brackets; brackets do not nest inside quotes.
    """
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    in_quotes = False
    for char in text:
        if in_quotes:
            buf.append(char)
            if char == '"':
                in_quotes = False
            continue
        if char == '"':
            in_quotes = True
            buf.append(char)
        elif char in "[(":
            depth += 1
            buf.append(char)
        elif char in "])":
            if depth > 0:
                depth -= 1
            buf.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(char)
    parts.append("".join(buf))
    return parts


def parse_line(text: str, default_year: int) -> tuple[datetime | None, str | None, list[str]]:
    """Split a decoded log line into (timestamp, event name, args)."""
    head, sep, rest = text.partition("  ")
    if not sep:
        return None, None, []
    timestamp = parse_timestamp(head.strip(), default_year)
    if timestamp is None:
        return None, None, []
    parts = split_args(rest.strip())
    event = parts[0].strip()
    return timestamp, event, [part.strip() for part in parts[1:]]


def unquote(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


def arg_at(args: list[str], index: int) -> str | None:
    if 0 <= index < len(args):
        return args[index]
    return None


def to_int(value: str | None) -> int | None:
    if value is None:
        return None
    value = value.strip().strip('"').strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        try:
            return int(float(value))
        except ValueError:
            return None


def to_bool(value: str | None) -> bool | None:
    number = to_int(value)
    if number is None:
        return None
    return number != 0


def parse_affixes(value: str | None) -> list[int]:
    if not value:
        return []
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    result = []
    for chunk in value.split(","):
        number = to_int(chunk)
        if number is not None:
            result.append(number)
    return result


def difficulty_name(difficulty_id: int | None) -> str:
    if difficulty_id is None:
        return "Unknown"
    return DIFFICULTIES.get(difficulty_id, "Difficulty%d" % difficulty_id)


_INVALID_CHARS = set('\\/:*?"<>|')


def sanitize_filename(name: str | None, max_len: int = MAX_COMPONENT_LEN,
                      fallback: str = "Unknown") -> str:
    """Windows-safe filename component; keeps unicode, spaces become '-'."""
    if not name:
        return fallback
    chars: list[str] = []
    for char in name:
        if char in _INVALID_CHARS or ord(char) < 32 or ord(char) == 127:
            continue
        chars.append("-" if char.isspace() else char)
    cleaned = "".join(chars)
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    cleaned = cleaned.strip(" .-")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].strip(" .-")
    return cleaned or fallback


def format_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.strftime("%Y-%m-%d %H:%M:%S.") + "%03d" % (value.microsecond // 1000)


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _atomic_write_bytes(path: str, data: bytes) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    temp_path = path + ".tmp"
    with open(temp_path, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


# --- segments ---------------------------------------------------------------------

class Segment:
    """One in-progress extraction (a M+ run or a raid pull)."""

    def __init__(self, kind: str, start_ts: datetime, source_file: str, segment_id: str):
        self.kind = kind
        self.start_ts = start_ts
        self.source_file = source_file
        self.segment_id = segment_id
        self.partial_path: str | None = None
        self.start_offset = 0
        self.lines = 0
        self.end_ts: datetime | None = None
        self.duration_ms: int | None = None
        # mythic+
        self.dungeon: str | None = None
        self.map_id: int | None = None
        self.challenge_mode_id: int | None = None
        self.key_level: int | None = None
        self.affixes: list[int] = []
        self.completed: bool | None = None
        self.bosses: list[dict] = []
        # raid
        self.encounter_id: int | None = None
        self.boss: str | None = None
        self.difficulty_id: int | None = None
        self.raid_size: int | None = None
        self.success: bool | None = None
        self._handle = None

    @property
    def complete(self) -> bool:
        return self.end_ts is not None

    def begin_body(self, partial_path: str) -> None:
        """Open the .partial body file. Called once the START args are parsed."""
        self.partial_path = partial_path
        self._handle = open(partial_path, "wb")

    def write(self, raw: bytes) -> None:
        self._handle.write(raw)
        self.lines += 1

    def close(self) -> None:
        if self._handle is not None and not self._handle.closed:
            self._handle.flush()
            self._handle.close()

    def abandon(self) -> None:
        self.close()
        if self.partial_path:
            try:
                os.remove(self.partial_path)
            except OSError:
                pass

    def display_name(self) -> str:
        if self.kind == KIND_MPLUS:
            return self.dungeon or ("Map%d" % self.map_id if self.map_id is not None
                                    else "UnknownDungeon")
        return self.boss or "UnknownBoss"

    def name_core(self, with_seconds: bool) -> str:
        """Base filename without the outcome suffix."""
        date_part = self.start_ts.strftime("%Y-%m-%d")
        time_part = self.start_ts.strftime("%H-%M-%S" if with_seconds else "%H-%M")
        name = sanitize_filename(self.display_name())
        if self.kind == KIND_MPLUS:
            level = self.key_level if self.key_level is not None else 0
            return "%s_%s_MPlus_%s_+%d" % (date_part, time_part, name, level)
        difficulty = sanitize_filename(difficulty_name(self.difficulty_id), 30, "Unknown")
        return "%s_%s_Raid_%s_%s" % (date_part, time_part, name, difficulty)

    def base_name(self, with_seconds: bool) -> str:
        core = self.name_core(with_seconds)
        if not self.complete:
            return core + "_INCOMPLETE"
        if self.kind == KIND_MPLUS:
            return core
        if self.success is None:
            return core + "_INCOMPLETE"
        return core + ("_Kill" if self.success else "_Wipe")

    def metadata(self) -> dict:
        data = {
            "segment_id": self.segment_id,
            "type": self.kind,
            "date": self.start_ts.strftime("%Y-%m-%d"),
            "start_time": format_timestamp(self.start_ts),
            "end_time": format_timestamp(self.end_ts),
            "complete": self.complete,
            "source_file": self.source_file,
            "context_seconds": CONTEXT_SECONDS,
            "lines": self.lines,
        }
        if self.kind == KIND_MPLUS:
            data.update({
                "dungeon": self.dungeon,
                "map_id": self.map_id,
                "challenge_mode_id": self.challenge_mode_id,
                "key_level": self.key_level,
                "affixes": list(self.affixes),
                "completed": self.completed,
                "duration_ms": self.duration_ms,
                "bosses": list(self.bosses),
            })
        else:
            data.update({
                "encounter_id": self.encounter_id,
                "boss": self.boss,
                "difficulty_id": self.difficulty_id,
                "difficulty": difficulty_name(self.difficulty_id),
                "raid_size": self.raid_size,
                "success": self.success,
                "duration_ms": self.duration_ms,
            })
        return data


class SegmentPublisher:
    """Owns the output tree and the recoverable publication protocol."""

    def __init__(self, output_dir: str, verbose: bool = True):
        self.output_dir = os.path.abspath(output_dir)
        self.mplus_dir = os.path.join(self.output_dir, MPLUS_DIR_NAME)
        self.raids_dir = os.path.join(self.output_dir, RAID_DIR_NAME)
        self.verbose = verbose

    def ensure_dirs(self) -> None:
        for path in (self.output_dir, self.mplus_dir, self.raids_dir):
            os.makedirs(path, exist_ok=True)

    def directory_for(self, kind: str) -> str:
        return self.mplus_dir if kind == KIND_MPLUS else self.raids_dir

    def cleanup_partials(self) -> int:
        """Stray *.partial files are crash leftovers; they are never authoritative."""
        removed = 0
        for directory in (self.mplus_dir, self.raids_dir):
            try:
                entries = os.listdir(directory)
            except OSError:
                continue
            for entry in entries:
                if entry.endswith(".partial") or entry.endswith(".tmp"):
                    try:
                        os.remove(os.path.join(directory, entry))
                        removed += 1
                    except OSError:
                        pass
        return removed

    def partial_path(self, kind: str, core_name: str, segment_id: str) -> str:
        # The hash keeps two simultaneously open segments (different log files, watch
        # mode) from sharing a partial file.
        suffix = _sha1(segment_id.encode("utf-8"))[:8]
        return os.path.join(self.directory_for(kind),
                            "%s.%s.txt.partial" % (core_name, suffix))

    def _existing_segment_id(self, json_path: str) -> str | None:
        try:
            with open(json_path, "r", encoding="utf-8") as handle:
                return json.load(handle).get("segment_id")
        except Exception:
            return None

    def _candidate_names(self, segment: Segment):
        yield segment.base_name(False)
        with_seconds = segment.base_name(True)
        yield with_seconds
        for index in range(2, 1000):
            yield "%s-%d" % (with_seconds, index)

    def resolve_name(self, segment: Segment) -> str:
        directory = self.directory_for(segment.kind)
        for candidate in self._candidate_names(segment):
            txt_path = os.path.join(directory, candidate + ".txt")
            json_path = os.path.join(directory, candidate + ".json")
            has_txt = os.path.exists(txt_path)
            has_json = os.path.exists(json_path)
            if not has_txt and not has_json:
                return candidate
            if has_json:
                existing = self._existing_segment_id(json_path)
                if existing == segment.segment_id:
                    return candidate  # same entity, idempotent rewrite
                if not has_txt:
                    return candidate  # json without txt = reclaimable crash orphan
            # otherwise occupied by a different segment: try the next candidate
        raise RuntimeError("could not find a free output name for " + segment.segment_id)

    def _purge_stale(self, directory: str, segment_id: str, keep_name: str) -> None:
        """Drop an older pair for the same segment published under another name.

        Happens when a segment first seen as _INCOMPLETE is later reprocessed with
        its END (e.g. after --reset-state): the outcome suffix changes, so without
        this the stale _INCOMPLETE pair would linger as a duplicate.
        """
        try:
            entries = os.listdir(directory)
        except OSError:
            return
        for entry in entries:
            if not entry.endswith(".json"):
                continue
            name = entry[:-5]
            if name == keep_name:
                continue
            if self._existing_segment_id(os.path.join(directory, entry)) != segment_id:
                continue
            for stale in (name + ".txt", entry):
                try:
                    os.remove(os.path.join(directory, stale))
                except FileNotFoundError:
                    pass

    def publish(self, segment: Segment) -> tuple[str, str]:
        """Publish order: json first, then rename the body. Returns (kind, txt path)."""
        segment.close()
        self.ensure_dirs()
        directory = self.directory_for(segment.kind)
        name = self.resolve_name(segment)
        json_path = os.path.join(directory, name + ".json")
        txt_path = os.path.join(directory, name + ".txt")
        payload = json.dumps(segment.metadata(), ensure_ascii=False, indent=2)
        _atomic_write_bytes(json_path, payload.encode("utf-8"))
        os.replace(segment.partial_path, txt_path)
        self._purge_stale(directory, segment.segment_id, name)
        if self.verbose:
            safe_print("  + %s" % os.path.join(os.path.basename(directory), name + ".txt"))
        return segment.kind, txt_path


# --- state machine ----------------------------------------------------------------

class SegmentTracker:
    """Per-log-file state machine driving segment open/close decisions."""

    def __init__(self, source_file: str, publisher: SegmentPublisher, default_year: int):
        self.source_file = source_file
        self.publisher = publisher
        self.default_year = default_year
        self.buffer: deque[tuple[datetime | None, int, bytes]] = deque()
        self.segment: Segment | None = None
        self.last_ts: datetime | None = None
        self.warmup = False
        self.published: list[tuple[str, str]] = []

    # -- public ------------------------------------------------------------------
    def feed(self, offset: int, raw: bytes, text: str) -> None:
        timestamp, event, args = parse_line(text, self.default_year)
        if self.warmup:
            self._feed_warmup(timestamp, event, offset, raw)
            return
        if timestamp is None:
            # Unparseable line: still part of the segment body, never fatal.
            self._buffer_line(self.last_ts, offset, raw)
            if self.segment is not None:
                self.segment.write(raw)
            return

        if self.last_ts is not None and \
                (self.last_ts - timestamp).total_seconds() > BACKWARDS_JUMP_SECONDS:
            self._close_segment()
            self.buffer.clear()
        self.last_ts = timestamp

        if event == "COMBAT_LOG_VERSION":
            self._close_segment()
            self.buffer.clear()

        segment = self.segment
        if segment is not None and segment.end_ts is not None and \
                (timestamp - segment.end_ts).total_seconds() > CONTEXT_SECONDS:
            # Trailing window elapsed: this line is not written, but stays buffered.
            self._close_segment()

        if event == "CHALLENGE_MODE_START":
            self._close_segment()
            self._buffer_line(timestamp, offset, raw)
            self._open_mplus(timestamp, args)
            return

        if event == "ENCOUNTER_START":
            segment = self.segment
            if segment is not None and segment.kind == KIND_MPLUS and segment.end_ts is None:
                # Encounters inside an open M+ never get their own file.
                self._buffer_line(timestamp, offset, raw)
                segment.write(raw)
                self._record_boss(args)
                return
            self._close_segment()
            self._buffer_line(timestamp, offset, raw)
            self._open_raid(timestamp, args)
            return

        self._buffer_line(timestamp, offset, raw)
        if self.segment is not None:
            self.segment.write(raw)

        if event == "CHALLENGE_MODE_END":
            self._handle_challenge_end(timestamp, args)
        elif event == "ENCOUNTER_END":
            self._handle_encounter_end(timestamp, args)

    def finalize_at_eof(self, is_latest: bool, mtime: float, now: float) -> bool:
        """Apply EOF rules. Returns True if nothing is left pending."""
        segment = self.segment
        if segment is None:
            return True
        if segment.end_ts is not None:
            self._close_segment()  # trailing context is best effort
            return True
        if (not is_latest) or (now - mtime) > STALE_SECONDS:
            self._close_segment()  # finalized _INCOMPLETE
            return True
        return False  # still being written: leave pending

    def shutdown(self) -> None:
        """Ctrl+C: finalize segments that already saw their END, leave the rest pending."""
        if self.segment is not None and self.segment.end_ts is not None:
            self._close_segment()

    def drop_open_segment(self) -> None:
        if self.segment is not None:
            self.segment.abandon()
            self.segment = None

    def pending_offset(self) -> int | None:
        return self.segment.start_offset if self.segment is not None else None

    def counts(self) -> tuple[int, int]:
        mplus = sum(1 for kind, _ in self.published if kind == KIND_MPLUS)
        return mplus, len(self.published) - mplus

    # -- internals ---------------------------------------------------------------
    def _feed_warmup(self, timestamp, event, offset: int, raw: bytes) -> None:
        """Refill the ring buffer only; no segment may be opened during warm-up."""
        if timestamp is not None:
            if self.last_ts is not None and \
                    (self.last_ts - timestamp).total_seconds() > BACKWARDS_JUMP_SECONDS:
                self.buffer.clear()
            self.last_ts = timestamp
            if event == "COMBAT_LOG_VERSION":
                self.buffer.clear()
            self._buffer_line(timestamp, offset, raw)
        else:
            self._buffer_line(self.last_ts, offset, raw)

    def _buffer_line(self, timestamp: datetime | None, offset: int, raw: bytes) -> None:
        if timestamp is not None:
            limit = timestamp - timedelta(seconds=CONTEXT_SECONDS)
            while self.buffer and (self.buffer[0][0] is None or self.buffer[0][0] < limit):
                self.buffer.popleft()
        self.buffer.append((timestamp, offset, raw))
        while len(self.buffer) > MAX_BUFFER_LINES:
            self.buffer.popleft()

    def _segment_id(self, kind: str, start_ts: datetime, main_id: int | None) -> str:
        return "%s|%s|%s|%s" % (kind, self.source_file, format_timestamp(start_ts),
                                "?" if main_id is None else main_id)

    def _start_segment(self, segment: Segment, fallback_offset: int) -> None:
        self.publisher.ensure_dirs()
        segment.begin_body(self.publisher.partial_path(
            segment.kind, segment.name_core(False), segment.segment_id))
        segment.start_offset = self.buffer[0][1] if self.buffer else fallback_offset
        for _, _, raw in self.buffer:
            segment.write(raw)
        self.segment = segment

    def _open_mplus(self, timestamp: datetime, args: list[str]) -> None:
        map_id = to_int(arg_at(args, 1))
        segment = Segment(KIND_MPLUS, timestamp, self.source_file,
                          self._segment_id(KIND_MPLUS, timestamp, map_id))
        segment.dungeon = unquote(arg_at(args, 0)) or None
        segment.map_id = map_id
        segment.challenge_mode_id = to_int(arg_at(args, 2))
        segment.key_level = to_int(arg_at(args, 3))
        segment.affixes = parse_affixes(arg_at(args, 4))
        self._start_segment(segment, self.buffer[-1][1] if self.buffer else 0)

    def _open_raid(self, timestamp: datetime, args: list[str]) -> None:
        encounter_id = to_int(arg_at(args, 0))
        segment = Segment(KIND_RAID, timestamp, self.source_file,
                          self._segment_id(KIND_RAID, timestamp, encounter_id))
        segment.encounter_id = encounter_id
        segment.boss = unquote(arg_at(args, 1)) or None
        segment.difficulty_id = to_int(arg_at(args, 2))
        segment.raid_size = to_int(arg_at(args, 3))
        self._start_segment(segment, self.buffer[-1][1] if self.buffer else 0)

    def _record_boss(self, args: list[str]) -> None:
        if self.segment is None:
            return
        self.segment.bosses.append({
            "encounter_id": to_int(arg_at(args, 0)),
            "boss": unquote(arg_at(args, 1)) or None,
            "success": None,
        })

    def _handle_challenge_end(self, timestamp: datetime, args: list[str]) -> None:
        segment = self.segment
        # An END with no open M+ is the spurious one WoW emits on zone-in.
        if segment is None or segment.kind != KIND_MPLUS or segment.end_ts is not None:
            return
        segment.end_ts = timestamp
        segment.completed = to_bool(arg_at(args, 1))
        segment.duration_ms = to_int(arg_at(args, 3))

    def _handle_encounter_end(self, timestamp: datetime, args: list[str]) -> None:
        segment = self.segment
        if segment is None:
            return
        encounter_id = to_int(arg_at(args, 0))
        if segment.kind == KIND_MPLUS:
            self._close_boss(encounter_id, to_bool(arg_at(args, 4)))
            return
        if segment.end_ts is not None or encounter_id is None:
            return
        if encounter_id != segment.encounter_id:
            self._close_segment()  # mismatched END: previous pull ends _INCOMPLETE
            return
        segment.end_ts = timestamp
        segment.success = to_bool(arg_at(args, 4))
        segment.duration_ms = to_int(arg_at(args, 5))

    def _close_boss(self, encounter_id: int | None, success: bool | None) -> None:
        if self.segment is None:
            return
        for entry in reversed(self.segment.bosses):
            if entry.get("success") is None and entry.get("encounter_id") == encounter_id:
                entry["success"] = success
                return

    def _close_segment(self) -> None:
        segment = self.segment
        if segment is None:
            return
        self.segment = None
        self.published.append(self.publisher.publish(segment))


# --- per-file streaming -----------------------------------------------------------

class FileProcessor:
    """Streams one log file from a committed offset, feeding the tracker."""

    def __init__(self, path: str, publisher: SegmentPublisher, offset: int = 0):
        self.path = os.path.abspath(path)
        self.name = os.path.basename(self.path)
        self.publisher = publisher
        self.offset = max(0, int(offset))
        self._signature: tuple | None = None   # fingerprint of the consumed prefix
        self._needs_warmup = self.offset > 0
        self.tracker = self._new_tracker()

    def _new_tracker(self) -> SegmentTracker:
        try:
            year = datetime.fromtimestamp(os.path.getmtime(self.path)).year
        except OSError:
            year = datetime.now().year
        return SegmentTracker(self.name, self.publisher, year)

    def process_new_data(self) -> None:
        size = os.path.getsize(self.path)
        if size < self.offset:
            self._handle_truncation()
            size = os.path.getsize(self.path)
        if self._signature is None:
            # Resumed from persisted state: fingerprint the prefix now, so a later
            # replacement is still detected even if this poll reads nothing.
            self._signature = self._compute_signature()
        if size <= self.offset:
            return
        with open(self.path, "rb") as handle:
            if self._needs_warmup:
                self._warm_up(handle)
                self._needs_warmup = False
            handle.seek(self.offset)
            position = self.offset
            pending = b""
            while True:
                block = handle.read(READ_BLOCK)
                if not block:
                    break
                data = pending + block
                start = 0
                while True:
                    index = data.find(b"\n", start)
                    if index == -1:
                        break
                    raw = data[start:index + 1]
                    self.tracker.feed(position, raw, _decode(raw))
                    position += len(raw)
                    start = index + 1
                pending = data[start:]
            # A trailing chunk without '\n' is an unfinished line: not processed.
            self.offset = position
        self._signature = self._compute_signature()

    def _compute_signature(self) -> tuple | None:
        """Hashes of the first and last bytes of the prefix already consumed."""
        if self.offset <= 0:
            return None
        window = min(HASH_BYTES, self.offset)
        try:
            with open(self.path, "rb") as handle:
                head = handle.read(window)
                handle.seek(self.offset - window)
                tail = handle.read(window)
        except OSError:
            return None
        return (_sha1(head), _sha1(tail), self.offset)

    def identity_changed(self) -> bool:
        """True when the file no longer contains the prefix this processor consumed.

        Used by --watch: between two polls the log can be replaced by a different one
        that is already larger than our position, which a size check cannot catch.
        """
        if self._signature is None:
            return False
        try:
            if os.path.getsize(self.path) < self.offset:
                return True
        except OSError:
            return False
        return self._compute_signature() != self._signature

    def _warm_up(self, handle) -> None:
        start = max(0, self.offset - WARMUP_BYTES)
        handle.seek(start)
        if start > 0:
            handle.readline()  # discard the partial line we landed in
        position = handle.tell()
        self.tracker.warmup = True
        try:
            while position < self.offset:
                line = handle.readline()
                if not line or not line.endswith(b"\n"):
                    break
                self.tracker.feed(position, line, _decode(line))
                position += len(line)
        finally:
            self.tracker.warmup = False

    def _handle_truncation(self) -> None:
        self.tracker.finalize_at_eof(is_latest=False, mtime=0.0, now=0.0)
        self.tracker = self._new_tracker()
        self.offset = 0
        self._signature = None
        self._needs_warmup = False

    def finish(self, is_latest: bool, now: float | None = None) -> None:
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            mtime = 0.0
        self.tracker.finalize_at_eof(is_latest, mtime, time.time() if now is None else now)

    def shutdown(self) -> None:
        self.tracker.shutdown()

    def commit_offset(self) -> int:
        pending = self.tracker.pending_offset()
        if pending is None:
            return self.offset
        # Never commit past the pending segment's pre-context start.
        return min(pending, self.offset)

    def counts(self) -> tuple[int, int]:
        return self.tracker.counts()


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace").rstrip("\r\n")


# --- state store ------------------------------------------------------------------

class StateStore:
    """state.json: committed offset per log file plus replacement detection."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.data: dict = {"version": 1, "files": {}}

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and isinstance(data.get("files"), dict):
                self.data = {"version": data.get("version", 1), "files": data["files"]}
        except Exception:
            self.data = {"version": 1, "files": {}}

    def save(self) -> None:
        payload = json.dumps(self.data, ensure_ascii=False, indent=2)
        _atomic_write_bytes(self.path, payload.encode("utf-8"))

    def reset(self) -> None:
        self.data = {"version": 1, "files": {}}

    @staticmethod
    def _hashes(path: str, offset: int) -> tuple[str, str]:
        # The head window never extends past the committed offset: hashing bytes that
        # did not exist at commit time would mistake ordinary growth for replacement.
        with open(path, "rb") as handle:
            head = handle.read(min(HASH_BYTES, offset))
            tail_start = max(0, offset - HASH_BYTES)
            handle.seek(tail_start)
            tail = handle.read(max(0, offset - tail_start))
        return _sha1(head), _sha1(tail)

    def get_offset(self, path: str) -> int:
        entry = self.data["files"].get(os.path.basename(path))
        if not isinstance(entry, dict):
            return 0
        offset = to_int(str(entry.get("offset", 0))) or 0
        if offset <= 0:
            return 0
        try:
            if os.path.getsize(path) < offset:
                return 0
            head_hash, tail_hash = self._hashes(path, offset)
        except OSError:
            return 0
        if head_hash != entry.get("head_hash") or tail_hash != entry.get("tail_hash"):
            return 0  # replaced/rewritten log: reprocess from the beginning
        return offset

    def update(self, path: str, offset: int) -> None:
        offset = max(0, int(offset))
        try:
            head_hash, tail_hash = self._hashes(path, offset)
            size = os.path.getsize(path)
            mtime = os.path.getmtime(path)
        except OSError:
            return
        self.data["files"][os.path.basename(path)] = {
            "offset": offset,
            "size": size,
            "mtime": mtime,
            "head_hash": head_hash,
            "tail_hash": tail_hash,
        }


# --- configuration ----------------------------------------------------------------

def script_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def default_output_dir() -> str:
    return os.path.join(script_dir(), OUTPUT_ROOT_NAME)


class Config:
    """config.json next to the script: log_dir and output_dir."""

    def __init__(self, path: str | None = None):
        self.path = os.path.abspath(path or os.path.join(script_dir(), CONFIG_FILENAME))
        self.data: dict = {}

    def load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                self.data = data
        except Exception:
            self.data = {}
        return self.data

    def save(self) -> None:
        payload = json.dumps(self.data, ensure_ascii=False, indent=2)
        _atomic_write_bytes(self.path, payload.encode("utf-8"))

    def get(self, key: str) -> str | None:
        value = self.data.get(key)
        return value if isinstance(value, str) and value else None


def _logs_candidates_from_install(install_path: str) -> list[str]:
    install_path = install_path.rstrip("\\/")
    tail = os.path.basename(install_path).lower()
    candidates = []
    if tail == "_retail_":
        candidates.append(os.path.join(install_path, "Logs"))
    else:
        candidates.append(os.path.join(install_path, "_retail_", "Logs"))
        candidates.append(os.path.join(install_path, "Logs"))
    return candidates


def detect_from_registry() -> list[str]:
    candidates: list[str] = []
    try:
        import winreg  # noqa: WPS433 (Windows only)
    except ImportError:
        return candidates
    keys = [
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Blizzard Entertainment\World of Warcraft"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Blizzard Entertainment\World of Warcraft"),
        (winreg.HKEY_CURRENT_USER,
         r"SOFTWARE\Blizzard Entertainment\World of Warcraft"),
    ]
    for hive, subkey in keys:
        try:
            with winreg.OpenKey(hive, subkey) as handle:
                install_path, _ = winreg.QueryValueEx(handle, "InstallPath")
            if install_path:
                candidates.extend(_logs_candidates_from_install(str(install_path)))
        except Exception:
            continue
    return candidates


_SCAN_FOLDER_NAMES = {
    "games", "juegos", "battlenet", "battle.net", "blizzard",
    "program files", "program files (x86)",
}


def scan_for_log_dirs() -> list[str]:
    """Bounded, non-recursive scan of the usual install locations on fixed drives."""
    candidates: list[str] = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = "%s:\\" % letter
        try:
            if not os.path.isdir(root):
                continue
        except Exception:
            continue
        bases = [root]
        try:
            for entry in os.listdir(root):
                lowered = entry.lower()
                if lowered in _SCAN_FOLDER_NAMES or lowered.startswith("program files"):
                    full = os.path.join(root, entry)
                    try:
                        if os.path.isdir(full):
                            bases.append(full)
                    except Exception:
                        continue
        except Exception:
            pass
        for base in bases:
            try:
                candidates.append(os.path.join(base, "World of Warcraft", "_retail_", "Logs"))
            except Exception:
                continue
    return candidates


def autodetect_log_dir() -> str | None:
    for candidate in detect_from_registry() + scan_for_log_dirs():
        try:
            if candidate and os.path.isdir(candidate):
                return os.path.abspath(candidate)
        except Exception:
            continue
    return None


def prompt_for_log_dir(reason: str = "Could not find the WoW Logs folder automatically.") -> str | None:
    # Never block a non-interactive run (tests, CI, scheduled task).
    if not sys.stdin.isatty():
        return None
    safe_print(reason)
    safe_print(r"It usually looks like: C:\...\World of Warcraft\_retail_\Logs")
    for _ in range(3):
        try:
            answer = input("Path to the Logs folder (blank to abort): ").strip().strip('"')
        except EOFError:
            return None
        if not answer:
            return None
        if os.path.isdir(answer):
            return os.path.abspath(answer)
        safe_print("Not a folder: %s" % answer)
    return None


def resolve_paths(cli_log_dir: str | None, cli_output: str | None,
                  config_path: str | None, reconfigure: bool) -> tuple[str, str]:
    """Return (log_dir, output_dir); persists autodetected values into config.json."""
    config = Config(config_path)
    config.load()
    dirty = False

    log_dir = cli_log_dir
    if log_dir is None and not reconfigure:
        stored = config.get("log_dir")
        if stored and os.path.isdir(stored):
            log_dir = stored
    if log_dir is None:
        if reconfigure:
            log_dir = prompt_for_log_dir("Enter the WoW Logs folder (blank to autodetect).")
        log_dir = log_dir or autodetect_log_dir() or prompt_for_log_dir()
        if log_dir:
            config.data["log_dir"] = log_dir
            dirty = True
    if not log_dir:
        raise SystemExit("ERROR: no WoW Logs folder found. Re-run with --log-dir <path>.")
    if not os.path.isdir(log_dir):
        raise SystemExit("ERROR: log folder does not exist: %s" % log_dir)

    # --reconfigure only re-detects the log folder; a customized output_dir survives.
    output_dir = cli_output
    if output_dir is None:
        output_dir = config.get("output_dir")
    if output_dir is None:
        output_dir = default_output_dir()
        config.data["output_dir"] = output_dir
        dirty = True
    if dirty:
        try:
            config.save()
        except OSError:
            pass
    return os.path.abspath(log_dir), os.path.abspath(output_dir)


# --- orchestration ----------------------------------------------------------------

class Extractor:
    """Ties config paths, state and publication together."""

    def __init__(self, log_dir: str, output_dir: str, state_path: str | None = None,
                 verbose: bool = True):
        self.log_dir = os.path.abspath(log_dir)
        self.publisher = SegmentPublisher(output_dir, verbose=verbose)
        self.state = StateStore(state_path or
                                os.path.join(self.publisher.output_dir, STATE_FILENAME))
        self.verbose = verbose

    def prepare(self, reset_state: bool = False) -> None:
        self.publisher.ensure_dirs()
        self.state.load()
        if reset_state:
            self.state.reset()
            self.state.save()
        self.publisher.cleanup_partials()

    def list_logs(self) -> list[str]:
        try:
            entries = os.listdir(self.log_dir)
        except OSError:
            return []
        paths = []
        for entry in entries:
            if entry.startswith(LOG_GLOB_PREFIX) and entry.lower().endswith(".txt"):
                full = os.path.join(self.log_dir, entry)
                if os.path.isfile(full):
                    paths.append(full)
        paths.sort(key=lambda p: (os.path.basename(p).lower(), p))
        return paths

    @staticmethod
    def _latest(paths: list[str]) -> str | None:
        best = None
        best_key = None
        for path in paths:
            try:
                key = (os.path.getmtime(path), os.path.basename(path))
            except OSError:
                continue
            if best_key is None or key > best_key:
                best, best_key = path, key
        return best

    def run_once(self) -> tuple[int, int, int]:
        paths = self.list_logs()
        latest = self._latest(paths)
        mplus_total = raid_total = errors = 0
        for path in paths:
            try:
                processor = FileProcessor(path, self.publisher,
                                          self.state.get_offset(path))
                processor.process_new_data()
                processor.finish(is_latest=(path == latest))
                # Outputs are published before the offset advances.
                self.state.update(path, processor.commit_offset())
                self.state.save()
                mplus, raid = processor.counts()
                # A still-pending segment is re-read next run (the offset stayed at its
                # pre-context start); drop it now so no handle or .partial lingers.
                processor.tracker.drop_open_segment()
                mplus_total += mplus
                raid_total += raid
            except Exception as exc:
                errors += 1
                safe_print("  ! error processing %s: %s" % (os.path.basename(path), exc))
                if os.environ.get("WOWLOGEXTRACTOR_DEBUG"):
                    traceback.print_exc()
        self.state.save()
        return mplus_total, raid_total, errors

    def watch(self, interval: float = WATCH_INTERVAL,
              max_polls: int | None = None) -> tuple[int, int, int]:
        """max_polls is a test hook: stop after N polls instead of running forever."""
        workers: dict[str, FileProcessor] = {}
        mplus_total = raid_total = errors = 0
        polls = 0
        safe_print("Watching %s (Ctrl+C to stop)..." % self.log_dir)
        try:
            while max_polls is None or polls < max_polls:
                polls += 1
                paths = self.list_logs()
                latest = self._latest(paths)
                for path in paths:
                    name = os.path.basename(path)
                    try:
                        worker = workers.get(name)
                        if worker is not None and worker.identity_changed():
                            # The bytes we already consumed no longer match the file:
                            # it was replaced (or truncated and regrown) between polls.
                            # Salvage what already saw its END (the source bytes are
                            # gone for good), then restart from the top.
                            worker.shutdown()
                            worker.tracker.drop_open_segment()
                            mplus, raid = worker.counts()
                            mplus_total += mplus
                            raid_total += raid
                            worker = None
                        if worker is None:
                            worker = FileProcessor(path, self.publisher,
                                                   self.state.get_offset(path))
                            workers[name] = worker
                        worker.process_new_data()
                        if path != latest:
                            # Rotated away: drain it and apply its EOF rules.
                            worker.finish(is_latest=False)
                        self.state.update(path, worker.commit_offset())
                        mplus, raid = worker.counts()
                        mplus_total += mplus
                        raid_total += raid
                        worker.tracker.published.clear()
                    except Exception as exc:
                        errors += 1
                        safe_print("  ! error processing %s: %s" % (name, exc))
                # A worker whose log vanished (deleted/moved) is finalized and dropped
                # instead of erroring on every poll.
                current = {os.path.basename(p) for p in paths}
                for name in [n for n in workers if n not in current]:
                    worker = workers.pop(name)
                    try:
                        worker.finish(is_latest=False)
                        mplus, raid = worker.counts()
                        mplus_total += mplus
                        raid_total += raid
                    except Exception as exc:
                        errors += 1
                        safe_print("  ! error finalizing %s: %s" % (name, exc))
                self.state.save()
                time.sleep(interval)
        except KeyboardInterrupt:
            safe_print("")
            safe_print("Stopping...")
        # Shared shutdown (Ctrl+C or the max_polls test hook): finalize segments that
        # already saw their END, persist state, leave the rest pending.
        for name, worker in workers.items():
            try:
                worker.shutdown()
                self.state.update(worker.path, worker.commit_offset())
                mplus, raid = worker.counts()
                mplus_total += mplus
                raid_total += raid
                worker.tracker.published.clear()
            except Exception as exc:
                errors += 1
                safe_print("  ! error finalizing %s: %s" % (name, exc))
        self.state.save()
        return mplus_total, raid_total, errors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Extract Mythic+ runs and raid boss pulls from WoW Retail combat logs.")
    parser.add_argument("--watch", action="store_true",
                        help="keep running and process new lines as WoW writes them")
    parser.add_argument("--log-dir", dest="log_dir", default=None,
                        help=r"WoW Logs folder (...\World of Warcraft\_retail_\Logs)")
    parser.add_argument("--output", dest="output", default=None,
                        help="output folder for the extracted files")
    parser.add_argument("--reset-state", dest="reset_state", action="store_true",
                        help="forget processed offsets and re-scan every log")
    parser.add_argument("--reconfigure", action="store_true",
                        help="re-run folder detection and rewrite config.json")
    parser.add_argument("--config", dest="config", default=None,
                        help="path to config.json (defaults to next to this script)")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log_dir, output_dir = resolve_paths(args.log_dir, args.output, args.config,
                                        args.reconfigure)
    extractor = Extractor(log_dir, output_dir)
    extractor.prepare(reset_state=args.reset_state)
    safe_print("Logs:   %s" % log_dir)
    safe_print("Output: %s" % output_dir)
    if args.watch:
        mplus, raid, errors = extractor.watch()
    else:
        mplus, raid, errors = extractor.run_once()
    safe_print("Processed: %d Mythic+ runs, %d raid pulls, %d errors" % (mplus, raid, errors))
    safe_print("Output: %s" % output_dir)
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    try:
        return run(argv)
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, str):
            safe_print(code)
            code = 2
        if code and sys.stdin.isatty():
            _pause()
        return int(code or 0)
    except KeyboardInterrupt:
        safe_print("")
        safe_print("Interrupted.")
        return 130
    except Exception:
        traceback.print_exc()
        _pause()
        return 1


def _pause() -> None:
    if sys.stdin.isatty():
        try:
            input("Press Enter to exit...")
        except EOFError:
            pass


if __name__ == "__main__":
    sys.exit(main())
