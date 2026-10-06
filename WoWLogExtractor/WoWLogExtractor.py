#!/usr/bin/env python3
"""WoWLogExtractor - extract Mythic+ runs and raid boss pulls from WoW Retail combat logs.

Single file, stdlib only, Python 3.10+. Streams combat logs in binary, writes one .txt
per Mythic+ run / raid pull (original bytes preserved) plus a .json metadata sidecar.
"""

from __future__ import annotations

import argparse
import gzip as gzip_module
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import traceback
import zipfile
from collections import OrderedDict, deque
from dataclasses import dataclass
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
LOCK_FILENAME = ".output.lock"
ANALYSIS_SCHEMA_VERSION = 2

APP_VERSION = "1.3.0"
PERFORMANCE_SCHEMA_VERSION = 1
PACKET_SCHEMA_VERSION = 1
PERFORMANCE_RULES_VERSION = 1
DIAGNOSTICS_DIR_NAME = "Diagnostics"
DEFAULT_PACKET_MAX_BYTES = 200000
DEFAULT_SESSION_GAP_MINUTES = 120
HEADER_PROBE_BYTES = 4096       # bounded read of a log's first line (game header)
# spec id -> rule set ({"id", "version", ...}); every entry is part of the profile.
SPEC_RULES: dict[int, dict] = {}

MAX_PLAYER_IDENTITIES = 256
MAX_PLAYER_AGGREGATES = 80
MAX_ACTOR_NAMES = 8192
MAX_PET_OWNERS = 2048
MAX_RELEVANT_HOSTILES = 4096
MAX_ACTIVE_AURAS = 8192
MAX_SPELL_AGGREGATES = 8192
MAX_INTERRUPT_DETAILS = 10000
MAX_DISPEL_DETAILS = 10000
MAX_CAUSAL_LINES = 50000
MAX_CAUSAL_BYTES = 64 * 1024 * 1024
CAUSAL_SECONDS = 20
DEATH_WINDOW_SECONDS = 12
ACTOR_NAME_TTL_SECONDS = 300
HOSTILE_TTL_SECONDS = 60
GZIP_LEVEL = 9

# Per-pull caps of the performance accumulator (reject-new; totals stay exact).
MAX_PERF_SPELLS = 256
MAX_PERF_TARGETS = 256
MAX_PERF_AURAS = 512
MAX_PERF_AURA_INTERVALS = 4000   # total across every aura key
MAX_PERF_TIMELINE = 6000
MAX_PERF_WINDOWS = 64
MAX_PERF_WINDOW_SPELLS = 32      # distinct cast spell ids per spec window; the rest: other
MAX_PERF_CONFIGS = 128
MAX_PERF_RESOURCE_POINTS = 240
# Internal bounds of the same accumulator (detail retained for derived metrics).
MAX_PERF_CHECKED_GUIDS = 4096    # name-match cache; when full, names are compared directly
MAX_PERF_INSTANCES = 64          # distinct GUIDs kept per damage target / aura holder set
MAX_PERF_RESOURCE_SAMPLES = 30000
MAX_PERF_ENERGIZE_EVENTS = 20000
MAX_PERF_LONGEST_GAPS = 5
MAX_PERF_SIGNATURE = 12
OPENER_SECONDS = 20
ACTION_GAP_SECONDS = 2.5
COMMON_WINDOWS = (30, 60, 120)

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


def format_megabytes(size: int | None) -> str:
    if size is None:
        return "not written"
    return "%.1f MB" % (size / (1024 * 1024))


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
    descriptor, temp_path = tempfile.mkstemp(prefix=".%s." % os.path.basename(path),
                                             suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.remove(temp_path)
        except FileNotFoundError:
            pass


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def _copy_atomic(source: str, destination: str) -> None:
    directory = os.path.dirname(destination) or "."
    os.makedirs(directory, exist_ok=True)
    descriptor, temp_path = tempfile.mkstemp(prefix=".%s." % os.path.basename(destination),
                                             suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as target, open(source, "rb") as origin:
            descriptor = -1
            shutil.copyfileobj(origin, target, READ_BLOCK)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp_path, destination)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.remove(temp_path)
        except FileNotFoundError:
            pass


def _deterministic_gzip(source: str, destination: str) -> None:
    with open(source, "rb") as origin, open(destination, "wb") as raw_target:
        with gzip_module.GzipFile(filename="", mode="wb", fileobj=raw_target,
                                  compresslevel=GZIP_LEVEL, mtime=0) as target:
            shutil.copyfileobj(origin, target, READ_BLOCK)
        raw_target.flush()
        os.fsync(raw_target.fileno())


@dataclass(frozen=True)
class OutputOptions:
    """Explicit output contract derived from the four analysis CLI switches."""

    analysis: bool = False
    analysis_only: bool = False
    gzip: bool = False
    bundle: bool = False
    keep_player_damage: bool = False
    performance_player: str | None = None

    def __post_init__(self) -> None:
        if self.analysis and self.analysis_only:
            raise ValueError("--analysis and --analysis-only are mutually exclusive")
        if self.bundle and not self.wants_analysis:
            raise ValueError("--bundle requires --analysis or --analysis-only")
        if self.keep_player_damage and not self.wants_analysis:
            raise ValueError("--keep-player-damage requires --analysis or --analysis-only")
        if self.performance_player is not None:
            if not self.wants_analysis:
                raise ValueError("--performance-player requires --analysis or --analysis-only")
            if not self.performance_player.strip():
                raise ValueError("--performance-player requires a non-empty player selector")

    @property
    def wants_analysis(self) -> bool:
        return self.analysis or self.analysis_only

    @property
    def wants_full(self) -> bool:
        return not self.analysis_only

    @property
    def is_legacy_default(self) -> bool:
        return not (self.analysis or self.analysis_only or self.gzip or self.bundle)

    @property
    def profile(self) -> str:
        if self.is_legacy_default:
            return "full"
        parts = ["analysis-only" if self.analysis_only else
                 ("full+analysis" if self.analysis else "full")]
        if self.gzip:
            parts.append("gzip")
        if self.bundle:
            parts.append("bundle")
        if self.keep_player_damage:
            parts.append("keep-player-damage")
        fingerprint = self.performance_fingerprint
        if fingerprint is not None:
            parts.append("perf-" + fingerprint)
        return "+".join(parts)

    @property
    def performance_fingerprint(self) -> str | None:
        """Identity of the performance output: player, schema and every rule version.

        Part of the profile, so changing any of them reprocesses each log once. The
        packet budget and session gap are deliberately not part of it.
        """
        if self.performance_player is None:
            return None
        rules = ",".join("%s=%s:%s" % (spec_id, SPEC_RULES[spec_id].get("id"),
                                       SPEC_RULES[spec_id].get("version"))
                         for spec_id in sorted(SPEC_RULES))
        text = "perf|%s|%s|%s|%s" % (self.performance_player.strip().casefold(),
                                     PERFORMANCE_SCHEMA_VERSION,
                                     PERFORMANCE_RULES_VERSION, rules)
        return _sha1(text.encode("utf-8"))[:12]

    def as_dict(self) -> dict:
        data = {"full": self.wants_full, "analysis": self.wants_analysis,
                "gzip": self.gzip, "bundle": self.bundle,
                "keep_player_damage": self.keep_player_damage,
                "profile": self.profile}
        if self.performance_player is not None:
            data["performance"] = {"player": self.performance_player,
                                   "fingerprint": self.performance_fingerprint}
        return data


class OutputLock:
    """Cross-process exclusive lock for one complete output tree."""

    def __init__(self, output_dir: str):
        self.path = os.path.join(os.path.abspath(output_dir), LOCK_FILENAME)
        self._handle = None
        self._depth = 0

    def acquire(self) -> None:
        if self._handle is not None:
            self._depth += 1
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                try:
                    import fcntl
                except ImportError as exc:
                    raise RuntimeError("no safe output locking primitive available") from exc
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, RuntimeError) as exc:
            handle.close()
            raise RuntimeError("output folder is already in use: %s" %
                               os.path.dirname(self.path)) from exc
        self._handle = handle
        self._depth = 1

    def release(self) -> None:
        if self._depth > 1:
            self._depth -= 1
            return
        handle, self._handle = self._handle, None
        self._depth = 0
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.release()


# --- tolerant combat parsing and bounded analysis --------------------------------

STRUCTURAL_EVENTS = {
    "COMBAT_LOG_VERSION", "ZONE_CHANGE", "MAP_CHANGE", "CHALLENGE_MODE_START",
    "CHALLENGE_MODE_END", "ENCOUNTER_START", "ENCOUNTER_END", "COMBATANT_INFO",
}
ACTOR_EVENT_PREFIXES = (
    "SPELL_", "RANGE_", "SWING_", "ENVIRONMENTAL_", "DAMAGE_", "UNIT_",
    "PARTY_", "SPELL_EMPOWER_",
)
CAST_EVENTS = {"SPELL_CAST_START", "SPELL_CAST_SUCCESS", "SPELL_CAST_FAILED",
               "SPELL_EMPOWER_START", "SPELL_EMPOWER_END", "SPELL_EMPOWER_INTERRUPT"}
DAMAGE_EVENTS = {"SWING_DAMAGE", "RANGE_DAMAGE", "SPELL_DAMAGE",
                 "SPELL_PERIODIC_DAMAGE", "ENVIRONMENTAL_DAMAGE",
                 "DAMAGE_SHIELD", "DAMAGE_SPLIT"}
# SWING_DAMAGE_LANDED repeats an already-counted swing to report the victim's state.
# It is parsed like damage but never aggregated: only DAMAGE_EVENTS carry an amount.
DAMAGE_RESULT_EVENTS = DAMAGE_EVENTS | {"SWING_DAMAGE_LANDED"}
# Pure resource bookkeeping: high volume, no evidence about any interaction.
RESOURCE_EVENTS = {"SPELL_ENERGIZE", "SPELL_PERIODIC_ENERGIZE", "SPELL_DRAIN",
                   "SPELL_LEECH"}
HEAL_EVENTS = {"SPELL_HEAL", "SPELL_PERIODIC_HEAL"}
AURA_APPLY_EVENTS = {"SPELL_AURA_APPLIED", "SPELL_AURA_REFRESH",
                     "SPELL_AURA_APPLIED_DOSE"}
AURA_REMOVE_EVENTS = {"SPELL_AURA_REMOVED", "SPELL_AURA_REMOVED_DOSE",
                      "SPELL_AURA_BROKEN", "SPELL_AURA_BROKEN_SPELL"}
SUMMON_EVENTS = {"SPELL_SUMMON", "SPELL_CREATE"}
DISPEL_EVENTS = {"SPELL_DISPEL", "SPELL_STOLEN"}
ALWAYS_KEEP_ACTOR_EVENTS = {"UNIT_DIED", "UNIT_DESTROYED", "PARTY_KILL"}
DETAIL_EVENTS = CAST_EVENTS | DAMAGE_RESULT_EVENTS | HEAL_EVENTS | AURA_APPLY_EVENTS | \
    AURA_REMOVE_EVENTS | SUMMON_EVENTS | DISPEL_EVENTS | RESOURCE_EVENTS | {
        "SWING_MISSED", "RANGE_MISSED", "SPELL_MISSED", "SPELL_ABSORBED",
        "SPELL_HEAL_ABSORBED", "SPELL_DISPEL_FAILED", "SPELL_INTERRUPT",
        "UNIT_DIED", "UNIT_DESTROYED", "PARTY_KILL", "SPELL_RESURRECT",
    }

TYPE_PLAYER = 0x00000400
TYPE_PET = 0x00001000
TYPE_GUARDIAN = 0x00002000
REACTION_HOSTILE = 0x00000040


def _flags(value: str | None) -> int:
    if value is None:
        return 0
    try:
        return int(value.strip().strip('"'), 0)
    except ValueError:
        return 0


def _is_player(guid: str | None, flags: int = 0) -> bool:
    if not guid or guid in {"0000000000000000", "nil"}:
        return False
    return bool(guid.startswith("Player-") or flags & TYPE_PLAYER)


def _is_pet(flags: int) -> bool:
    return bool(flags & (TYPE_PET | TYPE_GUARDIAN))


# The secondary spell of an event means something different per family; naming it
# after that meaning is what makes the JSON readable without the WoW docs at hand.
EXTRA_SPELL_KEYS = {
    "SPELL_INTERRUPT": ("interrupted_spell_id", "interrupted_spell"),
    "SPELL_DISPEL": ("dispelled_spell_id", "dispelled_spell"),
    "SPELL_DISPEL_FAILED": ("dispelled_spell_id", "dispelled_spell"),
    "SPELL_STOLEN": ("dispelled_spell_id", "dispelled_spell"),
    "SPELL_ABSORBED": ("shield_spell_id", "shield_spell"),
    "SPELL_HEAL_ABSORBED": ("shield_spell_id", "shield_spell"),
}


@dataclass
class ParsedCombatEvent:
    event: str
    source_guid: str | None = None
    source_name: str | None = None
    source_flags: int = 0
    destination_guid: str | None = None
    destination_name: str | None = None
    destination_flags: int = 0
    spell_id: int | None = None
    spell_name: str | None = None
    amount: int | None = None
    overheal: int | None = None
    absorbed: int | None = None
    extra_spell_id: int | None = None
    extra_spell_name: str | None = None
    aura_type: str | None = None
    miss_type: str | None = None
    target_hp: int | None = None
    target_max_hp: int | None = None
    target_owner_guid: str | None = None
    source_owner_guid: str | None = None
    spec_id: int | None = None
    item_level: int | None = None
    x: float | None = None
    y: float | None = None
    parse_fallback: bool = False

    @property
    def source_is_player(self) -> bool:
        return _is_player(self.source_guid, self.source_flags)

    @property
    def destination_is_player(self) -> bool:
        return _is_player(self.destination_guid, self.destination_flags)

    def as_dict(self, timestamp: datetime, raw: bytes,
                death_timestamp: datetime | None = None) -> dict:
        data = {"timestamp": format_timestamp(timestamp), "event": self.event,
                "raw": raw.decode("utf-8", errors="replace").rstrip("\r\n")}
        supplemental = self.event == "SWING_DAMAGE_LANDED"
        for key in ("source_guid", "source_name", "destination_guid",
                    "destination_name", "spell_id", "spell_name", "amount",
                    "overheal", "absorbed",
                    "aura_type", "miss_type", "target_hp", "target_max_hp",
                    "target_owner_guid", "spec_id", "item_level", "x", "y"):
            # The swing amount is already reported by SWING_DAMAGE; LANDED only adds
            # the victim's state, so it must not look like a second hit.
            if supplemental and key in {"amount", "absorbed"}:
                continue
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        extra_keys = EXTRA_SPELL_KEYS.get(self.event)
        if extra_keys is not None:
            for key, value in zip(extra_keys, (self.extra_spell_id,
                                               self.extra_spell_name)):
                if value is not None:
                    data[key] = value
        if supplemental:
            data["supplemental_state"] = True
        if death_timestamp is not None:
            data["seconds_before_death"] = round(
                (death_timestamp - timestamp).total_seconds(), 3)
        return data


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.strip().strip('"'))
    except ValueError:
        return None


def _looks_like_guid(value: str | None) -> bool:
    value = unquote(value)
    return bool(value and ("-" in value or value in {"0000000000000000", "nil"}))


def _advanced_state(payload: list[str], base: int) -> tuple[int, int, int, str | None,
                                                            float | None,
                                                            float | None] | None:
    """Detect Retail's 19-field advanced target-state prefix."""
    if len(payload) < base + 19 or not _looks_like_guid(arg_at(payload, base)):
        return None
    hp = to_int(arg_at(payload, base + 2))
    maximum = to_int(arg_at(payload, base + 3))
    if hp is None or maximum is None or maximum <= 0 or hp < 0:
        return None
    x = _to_float(arg_at(payload, base + 14))
    y = _to_float(arg_at(payload, base + 15))
    owner = unquote(arg_at(payload, base + 1)) or None
    return base + 19, hp, maximum, owner, x, y


_PLAIN_INT_RE = re.compile(r"-?\d+")


def _plain_int(value: str | None) -> int | None:
    """Integer only when the field is literally one ('0|3' or '4.5' are not)."""
    value = unquote(value)
    if value is None or not _PLAIN_INT_RE.fullmatch(value):
        return None
    return int(value)


def _number(value: str | None) -> int | float | None:
    """'4.0000' -> 4, '0.5' -> 0.5; anything else -> None."""
    number = _to_float(value)
    if number is None or number != number or number in (float("inf"), float("-inf")):
        return None
    return int(number) if number.is_integer() else number


def parse_log_header(args: list[str]) -> dict:
    """COMBAT_LOG_VERSION args: the version, then KEY,value pairs."""
    header = {"combat_log_version": _plain_int(arg_at(args, 0)),
              "advanced_logging": None, "build_version": None, "project_id": None}
    for index in range(1, len(args) - 1, 2):
        key = (unquote(args[index]) or "").upper()
        value = args[index + 1]
        if key == "ADVANCED_LOG_ENABLED":
            flag = _plain_int(value)
            header["advanced_logging"] = None if flag is None else flag != 0
        elif key == "BUILD_VERSION":
            header["build_version"] = unquote(value) or None
        elif key == "PROJECT_ID":
            header["project_id"] = _plain_int(value)
    return header


def _power_state(payload: list[str], base: int) -> tuple[str | None, int, int | None,
                                                         int | None, int | None] | None:
    """(info_guid, power_type, current, maximum, cost) from the advanced block.

    Which unit the block describes depends on the event (target for damage/heal,
    caster for casts and energizes); callers compare info_guid themselves. Units
    with several power types log pipe-separated values, which are not read.
    """
    if _advanced_state(payload, base) is None:
        return None
    power_type = _plain_int(arg_at(payload, base + 10))
    if power_type is None:
        return None
    return (unquote(arg_at(payload, base)) or None, power_type,
            _plain_int(arg_at(payload, base + 11)), _plain_int(arg_at(payload, base + 12)),
            _plain_int(arg_at(payload, base + 13)))


def _modern_damage_tail(event: str, payload: list[str], value_index: int) -> bool:
    """True for Retail's damage suffix (amount, baseAmount, overkill, school, ...)."""
    tail = payload[value_index:]
    expected_school = 1 if event in {"SWING_DAMAGE", "SWING_DAMAGE_LANDED"} \
        else _flags(arg_at(payload, 2))
    modern_damage = (event == "ENVIRONMENTAL_DAMAGE" and len(tail) >= 10) or \
        bool(tail and unquote(tail[-1]) in {"ST", "AOE"})
    # Modern swing tails have no ST/AOE marker and are one field shorter than
    # spell tails (no isOffHand): amount, base, overkill, school, resisted,
    # blocked, absorbed, critical, glancing, crushing. The school position
    # (index 3 modern vs index 2 legacy) is the discriminator.
    minimum_tail = 10 if event in {"SWING_DAMAGE", "SWING_DAMAGE_LANDED"} else 11
    if not modern_damage and len(tail) >= minimum_tail and expected_school:
        modern_damage = (_flags(arg_at(payload, value_index + 3)) == expected_school and
                         _flags(arg_at(payload, value_index + 2)) != expected_school)
    return modern_damage


def _damage_suffix(event: str, payload: list[str], value_index: int) -> dict:
    """amount/overkill/absorbed/critical of a damage suffix starting at value_index.

    `amount` includes the overkill; `overkill` is -1 when there is none. A field
    that is missing or not in its expected form is None, never guessed.
    """
    modern = _modern_damage_tail(event, payload, value_index)
    # Legacy suffix: amount, overkill, school, resisted, blocked, absorbed, critical.
    overkill_at, absorbed_at, critical_at = (2, 6, 7) if modern else (1, 5, 6)
    critical_text = unquote(arg_at(payload, value_index + critical_at))
    critical = {"1": True, "nil": False}.get(critical_text or "")
    return {"amount": _plain_int(arg_at(payload, value_index)),
            "overkill": _plain_int(arg_at(payload, value_index + overkill_at)),
            "absorbed": _plain_int(arg_at(payload, value_index + absorbed_at)),
            "critical": critical}


def _energize_suffix(payload: list[str]) -> dict | None:
    """SPELL_ENERGIZE suffix: amount, overEnergize, powerType, maxPower."""
    advanced = _advanced_state(payload, 3)
    index = advanced[0] if advanced is not None else 3
    if len(payload) < index + 4:
        return None
    power_type = _plain_int(arg_at(payload, index + 2))
    if power_type is None:
        return None
    return {"amount": _number(arg_at(payload, index)),
            "over_energize": _number(arg_at(payload, index + 1)),
            "power_type": power_type,
            "max_power": _plain_int(arg_at(payload, index + 3))}


def _aura_stacks(payload: list[str]) -> int | None:
    """Stack count of *_AURA_APPLIED_DOSE (new) / *_AURA_REMOVED_DOSE (remaining)."""
    stacks = _plain_int(arg_at(payload, 4))
    return stacks if stacks is not None and stacks >= 0 else None


def _bracket_items(text: str) -> list[str] | None:
    """Top-level entries of a '[...]' argument, or None when it is not one."""
    text = text.strip()
    if not (text.startswith("[") and text.endswith("]")):
        return None
    inner = text[1:-1].strip()
    return [entry.strip() for entry in split_args(inner)] if inner else []


def _tuple_fields(entry: str) -> list[str] | None:
    if not (entry.startswith("(") and entry.endswith(")")):
        return None
    return [field.strip() for field in split_args(entry[1:-1])]


def _combatant_fingerprint(text: str) -> str:
    return _sha1(text.strip().encode("utf-8"))[:12]


def _parse_talents(text: str | None) -> dict:
    if text is None:
        return {"status": "absent", "fingerprint": None, "count": None}
    entries = _bracket_items(text)
    unsupported = {"status": "unsupported_layout", "fingerprint": None, "count": None}
    if entries is None:
        return unsupported
    for entry in entries:
        fields = _tuple_fields(entry)
        if fields is None or len(fields) != 3 or \
                any(_plain_int(field) is None for field in fields):
            return unsupported
    return {"status": "ok", "fingerprint": _combatant_fingerprint(text),
            "count": len(entries)}


def _parse_equipment(text: str | None) -> dict:
    if text is None:
        return {"status": "absent", "fingerprint": None, "items": None}
    entries = _bracket_items(text)
    unsupported = {"status": "unsupported_layout", "fingerprint": None, "items": None}
    if entries is None:
        return unsupported
    items = []
    for entry in entries:
        fields = _tuple_fields(entry)
        if fields is None or len(fields) < 2:
            return unsupported
        item_id, level = _plain_int(fields[0]), _plain_int(fields[1])
        if item_id is None or level is None:
            return unsupported
        # Empty slots stay as (0, 0): the list keeps the log's slot order.
        items.append([item_id, level])
    return {"status": "ok", "fingerprint": _combatant_fingerprint(text), "items": items}


def _parse_initial_auras(text: str | None) -> dict:
    if text is None:
        return {"status": "absent", "auras": None}
    entries = _bracket_items(text)
    unsupported = {"status": "unsupported_layout", "auras": None}
    if entries is None or len(entries) % 3:
        return unsupported
    auras = []
    for index in range(0, len(entries), 3):
        caster = unquote(entries[index]) or None
        spell_id, stacks = _plain_int(entries[index + 1]), _plain_int(entries[index + 2])
        if not _looks_like_guid(caster) or spell_id is None or stacks is None:
            return unsupported
        auras.append((caster, spell_id, stacks))
    return {"status": "ok", "auras": auras}


def parse_combatant_details(args: list[str]) -> dict:
    """Talents, equipment and pre-pull auras of a COMBATANT_INFO line.

    Arrays are located as parse_combat_event does (spec = the integer just before
    the first '[' argument; equipment = the first '[(' argument after the talent
    array; pre-pull auras = the argument right after the equipment). Each block is
    judged on its own: a malformed one is 'unsupported_layout' without values, a
    missing one 'absent'. The aura list is only what the client chose to log.
    """
    details = {"guid": unquote(arg_at(args, 0)) or None, "spec_id": None}
    talent_text = equipment_text = aura_text = None
    for index in range(20, len(args)):
        if not args[index].lstrip().startswith("["):
            continue
        details["spec_id"] = _plain_int(arg_at(args, index - 1))
        talent_text = args[index]
        for equipment_index in range(index + 1, len(args)):
            if args[equipment_index].strip().startswith("[("):
                equipment_text = args[equipment_index]
                aura_text = arg_at(args, equipment_index + 1)
                break
        break
    details["talents"] = _parse_talents(talent_text)
    details["equipment"] = _parse_equipment(equipment_text)
    details["initial_auras"] = _parse_initial_auras(aura_text)
    return details


def _equipment_item_level(args: list[str], start: int) -> int | None:
    """Average the positive item levels of COMBATANT_INFO's equipment array.

    The array is the first `[(...)]` argument after the talent array (the pvp-talent
    tuple sits between them). Each entry is (itemID, ilvl, (enchants), (bonusIDs),
    (gems)); empty slots come as (0,0,(),(),()). Every tuple is judged on its own:
    malformed ones and non-positive levels are ignored, so a partially broken line
    still yields a usable average and only a fully unusable one yields None.
    """
    for index in range(start, len(args)):
        text = args[index].strip()
        if not text.startswith("[("):
            continue
        if not text.endswith("]"):
            return None
        levels = []
        for entry in split_args(text[1:-1]):
            entry = entry.strip()
            if not (entry.startswith("(") and entry.endswith(")")):
                continue
            fields = split_args(entry[1:-1])
            if len(fields) < 2:
                continue
            level = to_int(fields[1])
            if level is not None and level > 0:
                levels.append(level)
        # Half-up, so an exact .5 average never depends on banker's rounding.
        return (2 * sum(levels) + len(levels)) // (2 * len(levels)) if levels else None
    return None


def parse_combat_event(event: str, args: list[str]) -> ParsedCombatEvent:
    parsed = ParsedCombatEvent(event=event)
    if event == "COMBATANT_INFO":
        parsed.source_guid = unquote(arg_at(args, 0))
        # The spec is the integer immediately before the first talent-tree array.
        # That survived the extra 12.0 stat field (older logs used one less column).
        for index in range(20, len(args)):
            if args[index].lstrip().startswith("["):
                parsed.spec_id = to_int(arg_at(args, index - 1))
                parsed.item_level = _equipment_item_level(args, index + 1)
                break
        parsed.parse_fallback = parsed.source_guid is None
        return parsed
    if event not in DETAIL_EVENTS and event not in STRUCTURAL_EVENTS:
        if not event.startswith(ACTOR_EVENT_PREFIXES):
            return parsed
    if event in STRUCTURAL_EVENTS:
        return parsed
    if len(args) < 8:
        parsed.parse_fallback = True
        return parsed
    parsed.source_guid = unquote(args[0]) or None
    parsed.source_name = unquote(args[1]) or None
    parsed.source_flags = _flags(args[2])
    parsed.destination_guid = unquote(args[4]) or None
    parsed.destination_name = unquote(args[5]) or None
    parsed.destination_flags = _flags(args[6])
    payload = args[8:]
    has_spell = event.startswith("SPELL_") or event.startswith("RANGE_") or \
        event.startswith("DAMAGE_")
    if has_spell and event not in {"SPELL_ABSORBED", "SPELL_HEAL_ABSORBED"}:
        parsed.spell_id = to_int(arg_at(payload, 0))
        parsed.spell_name = unquote(arg_at(payload, 1))
    value_index = 3 if has_spell else 0
    if event in DAMAGE_RESULT_EVENTS | HEAL_EVENTS:
        state_base = value_index
        advanced = _advanced_state(payload, value_index)
        if advanced is not None:
            value_index, hp, maximum, owner, x, y = advanced
            # SWING_DAMAGE's block describes the attacker, not the target. Avoid
            # labelling source health/position as the victim's state.
            info_guid = unquote(arg_at(payload, state_base))
            if info_guid == parsed.destination_guid:
                parsed.target_hp, parsed.target_max_hp, parsed.x, parsed.y = \
                    hp, maximum, x, y
                parsed.target_owner_guid = owner
            if info_guid == parsed.source_guid:
                parsed.source_owner_guid = owner
    elif event in CAST_EVENTS:
        # Casts carry a source-side block. It is read only for the owner GUID: it is
        # the only way to attribute a pet summoned before the segment started.
        advanced = _advanced_state(payload, 3)
        if advanced is not None and unquote(arg_at(payload, 3)) == parsed.source_guid:
            parsed.source_owner_guid = advanced[3]
    if event == "ENVIRONMENTAL_DAMAGE":
        value_index += 1
    if event in DAMAGE_RESULT_EVENTS:
        parsed.amount = to_int(arg_at(payload, value_index))
        modern_damage = _modern_damage_tail(event, payload, value_index)
        parsed.absorbed = to_int(arg_at(payload, value_index +
                                       (6 if modern_damage else 5)))
    elif event in HEAL_EVENTS:
        # Modern Retail inserts healedToHP before amount even when advanced logging
        # is disabled. Detect the five-field suffix rather than using the unrelated
        # presence of the advanced state block as a version signal.
        if len(payload) - value_index >= 5:
            parsed.amount = to_int(arg_at(payload, value_index + 1))
            parsed.overheal = to_int(arg_at(payload, value_index + 2))
            parsed.absorbed = to_int(arg_at(payload, value_index + 3))
        else:
            parsed.amount = to_int(arg_at(payload, value_index))
            parsed.overheal = to_int(arg_at(payload, value_index + 1))
            parsed.absorbed = to_int(arg_at(payload, value_index + 2))
    elif event.endswith("_MISSED"):
        parsed.miss_type = unquote(arg_at(payload, value_index))
    elif event in AURA_APPLY_EVENTS | AURA_REMOVE_EVENTS:
        parsed.aura_type = unquote(arg_at(payload, 3))
    elif event in {"SPELL_INTERRUPT", "SPELL_DISPEL", "SPELL_DISPEL_FAILED",
                   "SPELL_STOLEN"}:
        parsed.extra_spell_id = to_int(arg_at(payload, 3))
        parsed.extra_spell_name = unquote(arg_at(payload, 4))
    elif event == "SPELL_ABSORBED":
        # Swing form starts with the absorber header; spell form prepends the
        # attacking spell triplet. Both then carry shield triplet + amount.
        shield_index = 4 if _looks_like_guid(arg_at(payload, 0)) else 7
        if shield_index == 7:
            parsed.spell_id = to_int(arg_at(payload, 0))
            parsed.spell_name = unquote(arg_at(payload, 1))
        parsed.extra_spell_id = to_int(arg_at(payload, shield_index))
        parsed.extra_spell_name = unquote(arg_at(payload, shield_index + 1))
        parsed.amount = to_int(arg_at(payload, shield_index + 3))
        parsed.absorbed = parsed.amount
    elif event == "SPELL_HEAL_ABSORBED":
        parsed.spell_id = to_int(arg_at(payload, 0))
        parsed.spell_name = unquote(arg_at(payload, 1))
        parsed.extra_spell_id = to_int(arg_at(payload, 7))
        parsed.extra_spell_name = unquote(arg_at(payload, 8))
        parsed.amount = to_int(arg_at(payload, 10))
        parsed.absorbed = parsed.amount
    return parsed


@dataclass
class _AnalysisRecord:
    timestamp: datetime
    raw: bytes
    parsed: ParsedCombatEvent
    selected: bool = False
    aggregated: bool = False


def _new_player(guid: str, name: str | None) -> dict:
    return {"guid": guid, "name": name, "spec_id": None, "role": None,
            "class_id": None,
            "item_level": None, "deaths": 0, "interrupts": 0, "dispels": 0,
            "damage_done": 0, "damage_taken": 0, "healing_done": 0,
            "healing_received": 0, "self_healing": 0, "absorbs_received": 0,
            "pets": []}


SPEC_ROLES = {
    62: "DAMAGER", 63: "DAMAGER", 64: "DAMAGER",
    65: "HEALER", 66: "TANK", 70: "DAMAGER",
    71: "DAMAGER", 72: "DAMAGER", 73: "TANK",
    102: "DAMAGER", 103: "DAMAGER", 104: "TANK", 105: "HEALER",
    250: "TANK", 251: "DAMAGER", 252: "DAMAGER",
    253: "DAMAGER", 254: "DAMAGER", 255: "DAMAGER",
    256: "HEALER", 257: "HEALER", 258: "DAMAGER",
    259: "DAMAGER", 260: "DAMAGER", 261: "DAMAGER",
    262: "DAMAGER", 263: "DAMAGER", 264: "HEALER",
    265: "DAMAGER", 266: "DAMAGER", 267: "DAMAGER",
    268: "TANK", 269: "DAMAGER", 270: "HEALER",
    577: "DAMAGER", 581: "TANK", 1467: "DAMAGER", 1468: "HEALER",
    1473: "DAMAGER", 1480: "DAMAGER",
}

# Spec id -> WoW class id (1..13). Derived from the same Retail spec ids as SPEC_ROLES,
# so a COMBATANT_INFO line identifies the class without any external table.
SPEC_CLASSES = {
    71: 1, 72: 1, 73: 1,
    65: 2, 66: 2, 70: 2,
    253: 3, 254: 3, 255: 3,
    259: 4, 260: 4, 261: 4,
    256: 5, 257: 5, 258: 5,
    250: 6, 251: 6, 252: 6,
    262: 7, 263: 7, 264: 7,
    62: 8, 63: 8, 64: 8,
    265: 9, 266: 9, 267: 9,
    268: 10, 269: 10, 270: 10,
    102: 11, 103: 11, 104: 11, 105: 11,
    577: 12, 581: 12,
    1467: 13, 1468: 13, 1473: 13,
}


class AnalysisSession:
    """One bounded, streaming analysis pipeline for a single extracted segment."""

    def __init__(self, stage_dir: str, kind: str, keep_player_damage: bool = False,
                 performance: "PerformanceAccumulator | None" = None):
        self.stage_dir = stage_dir
        self.kind = kind
        self.keep_player_damage = keep_player_damage
        # Optional observer only: it never changes the policy or the aggregates.
        self.performance = performance
        os.makedirs(stage_dir, exist_ok=True)
        self.combat_raw_path = os.path.join(stage_dir, "combat.raw")
        self.deaths_spool_path = os.path.join(stage_dir, "deaths.jsonl")
        self._combat = open(self.combat_raw_path, "wb")
        self._deaths = open(self.deaths_spool_path, "wb")
        self.history: deque[_AnalysisRecord] = deque()
        self.history_bytes = 0
        self.dropped_intervals: deque[tuple[datetime, datetime]] = deque()
        self.actor_names: OrderedDict[str, tuple[str | None, datetime]] = OrderedDict()
        self.pet_owners: OrderedDict[str, str] = OrderedDict()
        self.hostiles: OrderedDict[str, datetime] = OrderedDict()
        self.active_auras: dict[tuple[str, int | None, str | None], dict] = {}
        self.players: OrderedDict[str, dict] = OrderedDict()
        self.player_identities: set[str] = set()
        self.player_identity_truncated = False
        self.spell_keys: set[tuple[str, int | None]] = set()
        self.interrupts: list[dict] = []
        self.dispels: list[dict] = []
        self.enemy_cast_successes: dict[tuple[int | None, str | None], int] = {}
        self.parse_fallbacks: dict[str, int] = {}
        self.event_counts: dict[str, int] = {}
        self.total_player_deaths = 0
        self.total_interrupts = 0
        self.total_dispels = 0
        self.warnings: OrderedDict[str, dict] = OrderedDict()
        self.persistent_incomplete: set[str] = set()
        self.combat_lines = 0
        self.combat_bytes = 0
        self.current_encounter: dict | None = None

    def _warn(self, code: str, cap: int, timestamp: datetime, incomplete: bool = False) -> None:
        warning = self.warnings.get(code)
        stamp = format_timestamp(timestamp)
        if warning is None:
            warning = {"code": code, "cap": cap, "dropped": 0,
                       "first_timestamp": stamp, "last_timestamp": stamp}
            self.warnings[code] = warning
        warning["dropped"] += 1
        warning["last_timestamp"] = stamp
        if incomplete:
            self.persistent_incomplete.add(code)

    def _remember_name(self, guid: str | None, name: str | None, timestamp: datetime) -> None:
        if not guid:
            return
        if guid in self.actor_names:
            self.actor_names.pop(guid)
        elif len(self.actor_names) >= MAX_ACTOR_NAMES:
            self.actor_names.popitem(last=False)
            self._warn("actor_names_evicted", MAX_ACTOR_NAMES, timestamp)
        self.actor_names[guid] = (name, timestamp)

    def _expire(self, timestamp: datetime) -> None:
        name_limit = timestamp - timedelta(seconds=ACTOR_NAME_TTL_SECONDS)
        while self.actor_names:
            _, (_, seen) = next(iter(self.actor_names.items()))
            if seen >= name_limit:
                break
            self.actor_names.popitem(last=False)
        hostile_limit = timestamp - timedelta(seconds=HOSTILE_TTL_SECONDS)
        while self.hostiles:
            guid, seen = next(iter(self.hostiles.items()))
            if seen >= hostile_limit:
                break
            self.hostiles.popitem(last=False)
            self._retire_actor(guid)

    def _retire_actor(self, guid: str) -> None:
        """Release state owned by a retired destination without losing its DoTs."""
        for key in [item for item in self.active_auras if item[0] == guid]:
            self.active_auras.pop(key, None)

    def _identity(self, guid: str | None, timestamp: datetime) -> None:
        if not guid or guid in self.player_identities:
            return
        if len(self.player_identities) >= MAX_PLAYER_IDENTITIES:
            self.player_identity_truncated = True
            self._warn("player_identities_truncated", MAX_PLAYER_IDENTITIES, timestamp)
            return
        self.player_identities.add(guid)

    def _player(self, guid: str | None, name: str | None,
                timestamp: datetime) -> dict | None:
        if not guid:
            return None
        self._identity(guid, timestamp)
        player = self.players.get(guid)
        if player is not None:
            if name and not player.get("name"):
                player["name"] = name
            return player
        if len(self.players) >= MAX_PLAYER_AGGREGATES:
            self._warn("player_aggregates_truncated", MAX_PLAYER_AGGREGATES, timestamp)
            return None
        player = _new_player(guid, name)
        self.players[guid] = player
        return player

    def _mark_hostile(self, guid: str | None, timestamp: datetime) -> None:
        if not guid or _is_player(guid):
            return
        if guid in self.hostiles:
            self.hostiles.pop(guid)
            self.hostiles[guid] = timestamp
            return
        elif len(self.hostiles) >= MAX_RELEVANT_HOSTILES:
            evicted, _ = self.hostiles.popitem(last=False)
            self._retire_actor(evicted)
            self._warn("hostiles_capacity_evicted", MAX_RELEVANT_HOSTILES,
                       timestamp, incomplete=True)
        self.hostiles[guid] = timestamp
        for record in self.history:
            # Causal look-back promotes this actor's own prior casts/auras. Merely
            # targeting a now-known hostile does not make an unrelated NPC relevant.
            # The very same policy decides: a line the policy rejects is never
            # resurrected here, and the record flags keep the promotion idempotent.
            if record.parsed.source_guid == guid:
                self._apply_policy(record)

    def _remember_pet(self, pet: str | None, owner: str | None,
                      timestamp: datetime) -> None:
        if not pet or not owner:
            return
        if pet in self.pet_owners:
            self.pet_owners.pop(pet)
        elif len(self.pet_owners) >= MAX_PET_OWNERS:
            evicted, _ = self.pet_owners.popitem(last=False)
            self._retire_actor(evicted)
            self._warn("pet_owners_capacity_evicted", MAX_PET_OWNERS,
                       timestamp, incomplete=True)
        self.pet_owners[pet] = owner
        player = self.players.get(owner)
        if player is not None and pet not in player["pets"]:
            player["pets"].append(pet)

    @staticmethod
    def _owned_pet_claim(pet: str | None, pet_flags: int, owner: str | None) -> bool:
        """Only a friendly pet/guardian with a Player owner may be claimed as ours.

        An enemy player's pet also carries a Player owner GUID in its advanced block;
        it must stay a plain hostile so its damage is attributed to nobody.
        """
        return bool(pet and owner and _is_player(owner) and _is_pet(pet_flags) and
                    not pet_flags & REACTION_HOSTILE)

    def _relevant(self, guid: str | None, flags: int = 0) -> bool:
        return bool(_is_player(guid, flags) or guid in self.pet_owners or guid in self.hostiles)

    def _own_pet(self, guid: str | None) -> bool:
        """A pet whose owner is a known player. Boss summons also live in pet_owners
        (with a Creature owner) and must not count as friendly units."""
        return _is_player(self.pet_owners.get(guid or ""))

    def _friendly(self, guid: str | None, flags: int = 0) -> bool:
        return _is_player(guid, flags) or self._own_pet(guid)

    def _keep_policy(self, parsed: ParsedCombatEvent) -> tuple[bool, bool]:
        """Decide, once per record, whether it feeds the aggregates and whether its
        raw line belongs in combat.txt. See plans/analysis-gap-fixes.md for the table.
        """
        event = parsed.event
        if event in STRUCTURAL_EVENTS or event in ALWAYS_KEEP_ACTOR_EVENTS or \
                parsed.parse_fallback:
            return True, True
        if event in RESOURCE_EVENTS:
            return False, False
        friendly_source = self._friendly(parsed.source_guid, parsed.source_flags)
        friendly_target = self._friendly(parsed.destination_guid,
                                         parsed.destination_flags)
        if event == "SWING_DAMAGE_LANDED":
            # Never counted: the paired SWING_DAMAGE already carries the amount.
            if friendly_target:
                return False, True
            return False, bool(self.keep_player_damage and friendly_source)
        if friendly_source and not friendly_target and \
                (event in DAMAGE_EVENTS or event == "SPELL_ABSORBED"):
            # Outgoing results answer none of the analysis questions, but their totals
            # do: aggregate them always, keep the raw line only when asked to.
            return True, self.keep_player_damage
        direct_player = parsed.source_is_player or parsed.destination_is_player
        # Only heals between units nobody owns are noise: a heal landing on a
        # player or on an owned pet is evidence, whoever cast it.
        pet_only_heal = event in HEAL_EVENTS and not direct_player and \
            not friendly_target and \
            (_is_pet(parsed.source_flags) or parsed.source_guid in self.pet_owners) and \
            (_is_pet(parsed.destination_flags) or
             parsed.destination_guid in self.pet_owners)
        keep = not pet_only_heal and (
            direct_player or friendly_target or
            self._relevant(parsed.source_guid, parsed.source_flags))
        return keep, keep

    def _apply_policy(self, record: _AnalysisRecord) -> None:
        count, write = self._keep_policy(record.parsed)
        if count:
            self._count_record(record)
        if write:
            self._select_record(record)

    def _add_spell_key(self, kind: str, spell_id: int | None,
                       timestamp: datetime) -> bool:
        key = (kind, spell_id)
        if key in self.spell_keys:
            return True
        if len(self.spell_keys) >= MAX_SPELL_AGGREGATES:
            self._warn("spell_aggregates_truncated", MAX_SPELL_AGGREGATES, timestamp)
            return False
        self.spell_keys.add(key)
        return True

    def _aggregate(self, parsed: ParsedCombatEvent, timestamp: datetime) -> None:
        source_guid = self.pet_owners.get(parsed.source_guid or "", parsed.source_guid)
        destination_guid = self.pet_owners.get(parsed.destination_guid or "",
                                               parsed.destination_guid)
        source = self.players.get(source_guid or "")
        destination = self.players.get(destination_guid or "")
        amount = max(0, parsed.amount or 0)
        spell_admitted = parsed.spell_id is None or \
            self._add_spell_key(parsed.event, parsed.spell_id, timestamp)
        if parsed.event in DAMAGE_EVENTS:
            if source is not None:
                source["damage_done"] += amount
            if destination is not None:
                destination["damage_taken"] += amount
        elif parsed.event in HEAL_EVENTS:
            effective = max(0, amount - max(0, parsed.overheal or 0))
            if source is not None:
                source["healing_done"] += effective
            if destination is not None:
                destination["healing_received"] += effective
            if source is not None and source_guid == destination_guid:
                source["self_healing"] += effective
        elif parsed.event == "SPELL_ABSORBED":
            if destination is not None:
                destination["absorbs_received"] += amount
        elif parsed.event == "SPELL_INTERRUPT":
            self.total_interrupts += 1
            if source is not None:
                source["interrupts"] += 1
            detail = parsed.as_dict(timestamp, b"")
            detail.pop("raw", None)
            if len(self.interrupts) < MAX_INTERRUPT_DETAILS:
                self.interrupts.append(detail)
            else:
                self._warn("interrupt_details_truncated", MAX_INTERRUPT_DETAILS, timestamp)
        elif parsed.event in DISPEL_EVENTS:
            self.total_dispels += 1
            if source is not None:
                source["dispels"] += 1
            detail = parsed.as_dict(timestamp, b"")
            detail.pop("raw", None)
            if len(self.dispels) < MAX_DISPEL_DETAILS:
                self.dispels.append(detail)
            else:
                self._warn("dispel_details_truncated", MAX_DISPEL_DETAILS, timestamp)
        if parsed.event == "SPELL_CAST_SUCCESS" and parsed.source_guid in self.hostiles and \
                spell_admitted:
            key = (parsed.spell_id, parsed.spell_name)
            self.enemy_cast_successes[key] = self.enemy_cast_successes.get(key, 0) + 1

    def _count_record(self, record: _AnalysisRecord) -> None:
        """Count a record once, whether or not its raw line is kept."""
        if record.aggregated:
            return
        record.aggregated = True
        event = record.parsed.event
        self.event_counts[event] = self.event_counts.get(event, 0) + 1
        self._aggregate(record.parsed, record.timestamp)

    def _select_record(self, record: _AnalysisRecord) -> None:
        """Mark a record's raw line for combat.txt."""
        record.selected = True

    def _auras(self, parsed: ParsedCombatEvent, timestamp: datetime) -> None:
        key = (parsed.destination_guid or "", parsed.spell_id, parsed.source_guid)
        if parsed.event in AURA_APPLY_EVENTS:
            if not self._relevant(parsed.destination_guid, parsed.destination_flags):
                return
            if key not in self.active_auras and len(self.active_auras) >= MAX_ACTIVE_AURAS:
                self._warn("active_auras_truncated", MAX_ACTIVE_AURAS,
                           timestamp, incomplete=True)
                return
            self.active_auras[key] = {
                "destination_guid": parsed.destination_guid,
                "source_guid": parsed.source_guid, "spell_id": parsed.spell_id,
                "spell_name": parsed.spell_name, "aura_type": parsed.aura_type,
                "applied_at": format_timestamp(timestamp),
            }
        elif parsed.event in AURA_REMOVE_EVENTS:
            for aura_key in [item for item in self.active_auras
                             if item[0] == parsed.destination_guid and
                             item[1] == parsed.spell_id]:
                self.active_auras.pop(aura_key, None)
        elif parsed.event in {"UNIT_DIED", "UNIT_DESTROYED"}:
            destination = parsed.destination_guid
            for aura_key in [item for item in self.active_auras if item[0] == destination]:
                self.active_auras.pop(aura_key, None)

    def _write_death(self, record: _AnalysisRecord) -> None:
        parsed, timestamp = record.parsed, record.timestamp
        player = self.players.get(parsed.destination_guid or "")
        if player is not None:
            player["deaths"] += 1
        self.total_player_deaths += 1
        aura_cutoff = timestamp - timedelta(seconds=CAUSAL_SECONDS)
        base_cutoff = timestamp - timedelta(seconds=DEATH_WINDOW_SECONDS)
        active_keys = {(key[1], key[2]) for key in self.active_auras
                       if key[0] == parsed.destination_guid}
        has_early_aura = any(
            aura_cutoff <= item.timestamp < base_cutoff and
            item.parsed.event in AURA_APPLY_EVENTS and
            item.parsed.destination_guid == parsed.destination_guid and
            (item.parsed.spell_id, item.parsed.source_guid) in active_keys
            for item in self.history)
        window_seconds = CAUSAL_SECONDS if has_early_aura else DEATH_WINDOW_SECONDS
        cutoff = timestamp - timedelta(seconds=window_seconds)
        player_guid = parsed.destination_guid

        def death_relevant(item: _AnalysisRecord) -> bool:
            candidate = item.parsed
            if candidate.event in RESOURCE_EVENTS:
                return False
            if candidate.destination_guid == player_guid:
                return True
            if candidate.event in STRUCTURAL_EVENTS | ALWAYS_KEEP_ACTOR_EVENTS:
                return True
            if candidate.source_guid == player_guid and candidate.event in \
                    (CAST_EVENTS | {"SPELL_INTERRUPT", "SPELL_DISPEL",
                                    "SPELL_DISPEL_FAILED", "SPELL_STOLEN"}):
                return True
            if candidate.event in CAST_EVENTS | {"SPELL_INTERRUPT", "SPELL_DISPEL",
                                                  "SPELL_DISPEL_FAILED", "SPELL_STOLEN"}:
                return item.selected
            if candidate.source_guid in self.hostiles and candidate.event in \
                    (AURA_APPLY_EVENTS | AURA_REMOVE_EVENTS | SUMMON_EVENTS):
                return item.selected
            return False

        events = [item.parsed.as_dict(item.timestamp, item.raw, timestamp)
                  for item in self.history if item.timestamp >= cutoff and
                  death_relevant(item)]
        involved = []
        for item in self.history:
            if item.timestamp >= cutoff:
                for guid in (item.parsed.source_guid, item.parsed.destination_guid):
                    if guid in self.hostiles and guid not in involved:
                        involved.append(guid)
        active = [dict(value) for key, value in self.active_auras.items()
                  if key[0] == parsed.destination_guid]
        reasons = set(self.persistent_incomplete)
        for first, last in self.dropped_intervals:
            if first <= timestamp and last >= cutoff:
                reasons.add("causal_history_dropped")
        death = {"timestamp": format_timestamp(timestamp),
                 "player_guid": parsed.destination_guid,
                 "player": parsed.destination_name,
                 "window_seconds": window_seconds,
                 "hostiles": involved, "active_auras": active, "events": events,
                 "raw": record.raw.decode("utf-8", errors="replace").rstrip("\r\n")}
        if self.current_encounter is not None:
            death["encounter"] = dict(self.current_encounter)
        if reasons:
            death["analysis_incomplete"] = True
            death["incomplete_reasons"] = sorted(reasons)
        self._deaths.write(json.dumps(death, ensure_ascii=False,
                                      separators=(",", ":")).encode("utf-8") + b"\n")

    def _flush_record(self, record: _AnalysisRecord) -> None:
        if record.selected:
            self._combat.write(record.raw)
            self.combat_lines += 1
            self.combat_bytes += len(record.raw)

    def _trim_history(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=CAUSAL_SECONDS)
        while self.dropped_intervals and self.dropped_intervals[0][1] < cutoff:
            self.dropped_intervals.popleft()
        while self.history and self.history[0].timestamp < cutoff:
            record = self.history.popleft()
            self.history_bytes -= len(record.raw)
            self._flush_record(record)
        while self.history and (len(self.history) > MAX_CAUSAL_LINES or
                                self.history_bytes > MAX_CAUSAL_BYTES):
            first = self.history.popleft()
            self.history_bytes -= len(first.raw)
            self._flush_record(first)
            self._warn("causal_history_dropped",
                       MAX_CAUSAL_LINES if len(self.history) >= MAX_CAUSAL_LINES
                       else MAX_CAUSAL_BYTES, first.timestamp)
            if self.dropped_intervals and self.dropped_intervals[-1][1] >= \
                    first.timestamp - timedelta(microseconds=1):
                self.dropped_intervals[-1] = (self.dropped_intervals[-1][0], first.timestamp)
            elif self.dropped_intervals and len(self.dropped_intervals) >= \
                    max(1, MAX_CAUSAL_LINES):
                previous = self.dropped_intervals.pop()
                self.dropped_intervals.append((previous[0], first.timestamp))
            else:
                self.dropped_intervals.append((first.timestamp, first.timestamp))

    def consume(self, raw: bytes, timestamp: datetime | None,
                event: str | None, args: list[str],
                line_offset: int | None = None) -> None:
        if timestamp is None:
            return
        if event is None:
            parsed = ParsedCombatEvent(event="UNPARSEABLE", parse_fallback=True)
            self.parse_fallbacks["UNPARSEABLE"] = \
                self.parse_fallbacks.get("UNPARSEABLE", 0) + 1
            self._warn("parse_fallback", 0, timestamp)
            record = _AnalysisRecord(timestamp, raw, parsed)
            self.history.append(record)
            self.history_bytes += len(raw)
            # Same route as every other record: the parse-fallback row of the
            # policy table counts it too, so event_counts reports what
            # combat.txt actually contains.
            self._apply_policy(record)
            self._trim_history(timestamp)
            return
        self._expire(timestamp)
        if event == "ENCOUNTER_START":
            self.current_encounter = {
                "type": self.kind,
                "encounter_id": to_int(arg_at(args, 0)),
                "boss": unquote(arg_at(args, 1)) or None,
            }
        parsed = parse_combat_event(event, args)
        if parsed.parse_fallback:
            self.parse_fallbacks[event] = self.parse_fallbacks.get(event, 0) + 1
            self._warn("parse_fallback", 0, timestamp)
        self._remember_name(parsed.source_guid, parsed.source_name, timestamp)
        self._remember_name(parsed.destination_guid, parsed.destination_name, timestamp)
        if parsed.source_is_player:
            self._player(parsed.source_guid, parsed.source_name, timestamp)
        if parsed.destination_is_player:
            self._player(parsed.destination_guid, parsed.destination_name, timestamp)
        # An advanced block names the owner of the unit it describes. That is the only
        # evidence available for a pet summoned before the segment started.
        for pet_guid, pet_flags, owner in (
                (parsed.destination_guid, parsed.destination_flags,
                 parsed.target_owner_guid),
                (parsed.source_guid, parsed.source_flags, parsed.source_owner_guid)):
            if self._owned_pet_claim(pet_guid, pet_flags, owner):
                self._player(owner, None, timestamp)
                self._remember_pet(pet_guid, owner, timestamp)
        if event == "COMBATANT_INFO" and parsed.source_guid:
            player = self._player(parsed.source_guid, None, timestamp)
            if player is not None:
                player["spec_id"] = parsed.spec_id
                player["role"] = SPEC_ROLES.get(parsed.spec_id)
                player["class_id"] = SPEC_CLASSES.get(parsed.spec_id)
                if parsed.item_level is not None:
                    player["item_level"] = parsed.item_level
        direct_player = parsed.source_is_player or parsed.destination_is_player
        if direct_player and event not in RESOURCE_EVENTS:
            other_guid = (parsed.destination_guid if parsed.source_is_player
                          else parsed.source_guid)
            other_flags = (parsed.destination_flags if parsed.source_is_player
                           else parsed.source_flags)
            if other_guid and not _is_player(other_guid, other_flags) and (other_flags & REACTION_HOSTILE):
                # An interaction proves relevance, but not ownership: hostile pets
                # frequently attack players. Only summon/create establishes owner.
                self._mark_hostile(other_guid, timestamp)
        if event in SUMMON_EVENTS and self._relevant(parsed.source_guid, parsed.source_flags):
            owner = self.pet_owners.get(parsed.source_guid or "", parsed.source_guid)
            self._remember_pet(parsed.destination_guid, owner, timestamp)
        if self.performance is not None and self.performance.failure is None:
            # After pet learning (a summon's own pet is already attributable) and
            # before the raw-line policy, which drops resource events. A failure of
            # this optional analysis stops it for the segment; the line is still
            # handled below exactly as without it.
            try:
                self.performance.observe(timestamp, event, args, parsed, self.pet_owners,
                                         line_offset)
            except Exception as exc:
                self.performance.fail(exc, "observe")
        if event not in RESOURCE_EVENTS:
            for guid in (parsed.source_guid, parsed.destination_guid):
                if guid in self.hostiles:
                    self.hostiles.pop(guid)
                    self.hostiles[guid] = timestamp
        record = _AnalysisRecord(timestamp, raw, parsed)
        self.history.append(record)
        self.history_bytes += len(raw)
        self._apply_policy(record)
        self._trim_history(timestamp)
        if event == "UNIT_DIED" and parsed.destination_is_player:
            self._write_death(record)
        self._auras(parsed, timestamp)
        if event == "ENCOUNTER_END" and self.current_encounter is not None:
            encounter_id = to_int(arg_at(args, 0))
            if encounter_id is None or encounter_id == self.current_encounter.get("encounter_id"):
                self.current_encounter = None

    def close_streams(self) -> None:
        while self.history:
            self._flush_record(self.history.popleft())
        self.history_bytes = 0
        for handle in (self._combat, self._deaths):
            if not handle.closed:
                handle.flush()
                os.fsync(handle.fileno())
                handle.close()

    def deaths(self) -> list[dict]:
        result = []
        try:
            with open(self.deaths_spool_path, "rb") as handle:
                for raw in handle:
                    if raw.strip():
                        result.append(json.loads(raw))
        except FileNotFoundError:
            pass
        return result

    @staticmethod
    def _encounter_for(segment_metadata: dict) -> dict | None:
        if segment_metadata.get("type") == KIND_RAID:
            return {"type": KIND_RAID,
                    "encounter_id": segment_metadata.get("encounter_id"),
                    "boss": segment_metadata.get("boss"),
                    "difficulty_id": segment_metadata.get("difficulty_id")}
        if segment_metadata.get("type") == KIND_MPLUS:
            return {"type": KIND_MPLUS,
                    "dungeon": segment_metadata.get("dungeon"),
                    "map_id": segment_metadata.get("map_id"),
                    "key_level": segment_metadata.get("key_level")}
        return None

    def write_deaths_json(self, path: str, segment_metadata: dict) -> None:
        """Assemble the JSON array from the bounded-memory JSONL spool."""
        encounter = self._encounter_for(segment_metadata)
        with open(path, "wb") as target:
            target.write(b"[\n")
            first = True
            try:
                with open(self.deaths_spool_path, "rb") as source:
                    for raw in source:
                        if not raw.strip():
                            continue
                        death = json.loads(raw)
                        if encounter is not None:
                            merged_encounter = dict(encounter)
                            merged_encounter.update(death.get("encounter") or {})
                            death["encounter"] = merged_encounter
                        if not first:
                            target.write(b",\n")
                        target.write(json.dumps(death, ensure_ascii=False,
                                                separators=(",", ":")).encode("utf-8"))
                        first = False
            except FileNotFoundError:
                pass
            target.write(b"\n]\n")
            target.flush()
            os.fsync(target.fileno())

    def _enemy_cast_rows(self) -> list[dict]:
        """Readable, deterministic list: an id alone tells a reader nothing."""
        rows = [{"spell_id": spell_id, "spell_name": spell_name, "count": count}
                for (spell_id, spell_name), count in self.enemy_cast_successes.items()]
        rows.sort(key=lambda row: (-row["count"], row["spell_id"] is None,
                                   row["spell_id"] or 0, row["spell_name"] or ""))
        return rows

    def summary_and_players(self, segment_metadata: dict) -> tuple[dict, dict]:
        summary = dict(segment_metadata)
        summary.update({
            "analysis_schema_version": ANALYSIS_SCHEMA_VERSION,
            "player_count": len(self.player_identities),
            "player_count_truncated": self.player_identity_truncated,
            "player_deaths": self.total_player_deaths,
            "combat_lines": self.combat_lines,
            "event_counts": dict(sorted(self.event_counts.items())),
            "enemy_cast_successes": self._enemy_cast_rows(),
            "interrupts": self.interrupts,
            "interrupt_count": self.total_interrupts,
            "dispels": self.dispels,
            "dispel_count": self.total_dispels,
            "parse_fallbacks": dict(sorted(self.parse_fallbacks.items())),
            "warnings": list(self.warnings.values()),
        })
        players = sorted((dict(value) for value in self.players.values()),
                         key=lambda value: value["guid"])
        return summary, {"players": players}

    def result(self, segment_metadata: dict) -> tuple[dict, dict, list[dict]]:
        summary, players = self.summary_and_players(segment_metadata)
        deaths = self.deaths()
        encounter = self._encounter_for(segment_metadata)
        if encounter is not None:
            for death in deaths:
                merged_encounter = dict(encounter)
                merged_encounter.update(death.get("encounter") or {})
                death["encounter"] = merged_encounter
        return summary, players, deaths


# --- raid performance accumulator -------------------------------------------------

# Outgoing damage only; SWING_DAMAGE_LANDED repeats a swing and ENVIRONMENTAL has no
# attacker.
PERF_DAMAGE_EVENTS = DAMAGE_EVENTS - {"ENVIRONMENTAL_DAMAGE"}
PERF_CAST_KINDS = {"SPELL_CAST_START": "start", "SPELL_CAST_SUCCESS": "success",
                   "SPELL_CAST_FAILED": "failed"}
PERF_AURA_EVENTS = AURA_APPLY_EVENTS | AURA_REMOVE_EVENTS
PERF_ENERGIZE_EVENTS = {"SPELL_ENERGIZE", "SPELL_PERIODIC_ENERGIZE"}
# Events that may carry an advanced block; it is sampled when it describes the player.
# Swing and environmental payloads have no spell prefix, so their block starts at 0.
PERF_SAMPLED_EVENTS = DAMAGE_RESULT_EVENTS | HEAL_EVENTS | CAST_EVENTS | RESOURCE_EVENTS
PERF_UNPREFIXED_EVENTS = ("SWING_", "ENVIRONMENTAL_")
# Swings carry no spell; they are keyed under WoW's Auto Attack id.
MELEE_SPELL_ID = 6603
MELEE_SPELL_NAME = "Melee"
PERF_OTHER = "other"
_PERF_UNIT_KINDS = {"Creature": "creature", "Vehicle": "vehicle", "Pet": "pet"}


def _ms_between(start: datetime, timestamp: datetime) -> int:
    delta = timestamp - start
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def _perf_s(milliseconds: int | None) -> float | None:
    return None if milliseconds is None else round(milliseconds / 1000, 3)


def _perf_rate(numerator, denominator_ms: int | None, reason: str | None = None,
               per_seconds: int = 1) -> dict:
    """{"value", "numerator", "denominator_s"}, or {"value": None, "reason"}."""
    if reason is None and not denominator_ms:
        reason = "zero duration"
    if reason is not None:
        return {"value": None, "reason": reason}
    return {"value": round(numerator * per_seconds * 1000 / denominator_ms, 3),
            "numerator": numerator, "denominator_s": _perf_s(denominator_ms)}


def _perf_unit(guid: str) -> tuple[str, int | None, str]:
    """(key, npc_id, kind): NPCs by their id (5th field after the type), players by GUID."""
    parts = guid.split("-")
    kind = _PERF_UNIT_KINDS.get(parts[0])
    if kind is not None and len(parts) > 5:
        npc_id = _plain_int(parts[5])
        if npc_id is not None:
            return str(npc_id), npc_id, kind
    return guid, None, "player" if parts[0] == "Player" else "other"


def _perf_sort_id(value) -> tuple:
    return (value is None, value if isinstance(value, int) else 0, str(value))


def performance_json_bytes(data: dict) -> bytes:
    """performance.json bytes; NaN/Infinity raise instead of being written."""
    return json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")


def _damage_row(name: str | None = None) -> dict:
    return {"name": name, "hits": 0, "ticks": 0, "crits": 0, "amount": 0, "overkill": 0,
            "effective": 0, "absorbed_by_target": 0}


class _PerfTable:
    """Rows keyed by id with a reject-new cap; overflow is summed into one `other` row
    so the totals still add up. The warning counts the events sent to `other`."""

    def __init__(self, cap: int, code: str, factory, warn):
        self.cap, self.code, self.factory, self.warn = cap, code, factory, warn
        self.rows: dict = {}
        self.other: dict | None = None

    def row(self, key, name: str | None) -> dict:
        row = self.rows.get(key)
        if row is not None:
            if row.get("name") is None and name:
                row["name"] = name
            return row
        if len(self.rows) < self.cap:
            row = self.factory(name)
            self.rows[key] = row
            return row
        self.warn(self.code, self.cap)
        if self.other is None:
            self.other = self.factory(PERF_OTHER)
        return self.other


class _PerfAura:
    """One aura key: streaming uptime plus a capped interval history.

    `holders` are the casters (auras on the player) or the targets (auras the player
    applies): the interval is open while any holder has the aura. A key without
    interval history still follows this live state (nothing is recorded), so spec
    rules activated later can upgrade it with its open interval.
    """

    __slots__ = ("spell_id", "name", "aura_type", "source", "holders", "start_ms",
                 "start_basis", "stacks", "interval_max_stacks", "last_inactive_ms",
                 "applications", "refreshes", "removals", "max_stacks", "observed_ms",
                 "upper_ms", "unknown_start_intervals", "intervals", "partial",
                 "covered_until_ms", "targets", "with_intervals", "reserved",
                 "holders_truncated")

    def __init__(self, spell_id, name, aura_type, source, with_intervals: bool,
                 reserved: bool = False):
        self.spell_id, self.name, self.aura_type, self.source = \
            spell_id, name, aura_type, source
        self.with_intervals = with_intervals
        self.reserved = reserved
        # A holder beyond MAX_PERF_INSTANCES was not followed: its removal cannot close
        # the interval, so uptime may be overstated from then on (partial row).
        self.holders_truncated = False
        self.holders: set[str] = set()
        self.start_ms: int | None = None
        self.start_basis: str | None = None     # None while no interval is open
        self.stacks = self.interval_max_stacks = self.max_stacks = 0
        self.last_inactive_ms = 0                # origin of an unknown-start upper bound
        self.applications = self.refreshes = self.removals = 0
        self.observed_ms = self.upper_ms = self.unknown_start_intervals = 0
        self.intervals: list[list] = []          # [start_ms|None, end_ms, sb, eb, stacks]
        self.partial = False
        self.covered_until_ms: int | None = None
        self.targets: set[str] = set()

    @property
    def active(self) -> bool:
        return self.start_basis is not None


class PerformanceAccumulator:
    """Per-pull, per-player raid performance (performance.json v1), in streaming.

    One instance per raid segment when --performance-player is set. AnalysisSession
    hands it every timestamped line once, in order, before the raw-line policy.

    Phase is decided by event order: `pre` until the segment's own ENCOUNTER_START
    (same encounter id AND the segment's start timestamp), `encounter` until its
    ENCOUNTER_END, `post` afterwards. `pre` only initialises state (auras, life, a
    cast in progress); `post` only yields labelled evidence. Times are milliseconds
    relative to the own ENCOUNTER_START.

    Spec-rules hook. `SPEC_RULES[spec_id]` (or the `spec_rules` mapping given to the
    constructor) is a dict; it is looked up when the player's spec becomes known,
    i.e. when their COMBATANT_INFO is adopted, right after ENCOUNTER_START. The aura
    keys every registered rule set tracks are reserved from the first event, before
    the spec is known (pre-context evidence comes first):
      "id", "version"          identity of the rule set (part of the profile);
      "validated_builds"       builds the rules were checked against;
      "tracked_self_auras"     spell ids of auras on the player whose keys are
                               reserved (never rejected by MAX_PERF_AURAS);
      "tracked_target_auras"   spell ids of auras the player applies on other units:
                               reserved, and their interval history is retained;
      "channel_tick_spells"    damage spell ids whose hits count as player actions
                               for continuity (a channel has one cast, many ticks);
      "begin"    (optional)    callable(acc, rules, t_ms) called once, before the
                               first player event of the encounter phase is applied
                               (with the rules known), to seed `acc.spec_state` from
                               the state established before it;
      "observe"  (optional)    callable(acc, rules, record) called for every
                               interpreted player event of the encounter phase, so a
                               rule set can keep streaming aggregates in
                               `acc.spec_state`. record = {"kind": "cast" | "damage" |
                               "absorbed" | "aura" | "energize" | "death" |
                               "resurrection", "t_ms": int, ...event fields
                               (event-specific; see each _spec_event call)};
      "build"                  callable(acc, rules, build_version) -> dict, the
                               published `spec` section. It runs inside result(),
                               after open intervals were closed, and may read
                               acc.aura_intervals(), acc.timeline,
                               acc.resource_samples, acc.energize_events,
                               acc.deaths_ms, acc.resurrections_ms, acc.end_ms,
                               acc.spec_state and report caps with acc.warn().
    """

    def __init__(self, selector: str, encounter_id: int | None, start_ts: datetime,
                 spec_rules: dict | None = None):
        self.selector = selector.strip()
        folded = self.selector.casefold()
        if folded.startswith("player-"):
            self._mode = "guid"
        elif "-" in folded:
            self._mode = "full"
        else:
            self._mode = "short"
        self._selector_folded = folded
        self.encounter_id = encounter_id
        self.start_ts = start_ts
        self._rules_source = spec_rules if spec_rules is not None else SPEC_RULES
        self.phase = "pre"
        self.encounter_name: str | None = None
        self.encounter_start_offset: int | None = None
        self.encounter_end_offset: int | None = None
        self.last_offset: int | None = None
        self.last_ms = 0
        self.end_ms: int | None = None            # own ENCOUNTER_END, or observation end
        self._ended = False
        self._finished = False
        self.warnings: OrderedDict[str, dict] = OrderedDict()
        # identity
        self.player_guid: str | None = None
        self.player_name: str | None = None
        self.matches: OrderedDict[str, str | None] = OrderedDict()
        self._checked: dict[str, bool] = {}
        self._configs: dict[str, dict] = {}
        self._configs_dropped = 0
        self.config: dict | None = None
        self.spec_id: int | None = None
        self.spec_rules: dict | None = None
        self.spec_state: dict = {}
        self._spec_begun = False
        # Until the spec is known, the tracked auras of every registered rule set (a
        # small fixed set) are reserved and keep their interval history, so evidence
        # seen before the rules (pre-context) is never rejected by MAX_PERF_AURAS.
        self._tracked_target_auras: set[int] = set()
        self._reserved_auras: set[int] = set()
        for rules in self._rules_source.values():
            if rules:
                self._tracked_target_auras.update(rules.get("tracked_target_auras", ()))
                self._reserved_auras.update(rules.get("tracked_self_auras", ()))
        self._reserved_auras |= self._tracked_target_auras
        self._channel_ticks: set[int] = set()
        # damage
        self.damage_player = _damage_row()
        self.damage_pets = _damage_row()
        self.by_spell = _PerfTable(MAX_PERF_SPELLS, "perf_spells_truncated",
                                   _damage_row, self.warn)
        self.by_target = _PerfTable(MAX_PERF_TARGETS, "perf_targets_truncated",
                                    self._target_row, self.warn)
        self.by_pet = _PerfTable(MAX_PERF_TARGETS, "perf_pets_truncated",
                                 self._pet_row, self.warn)
        self.excluded = {"self_damage": 0, "non_hostile_target": 0, "pre_context": 0,
                         "post_context": 0}
        self.after_death_effective = 0
        self._unknown_life_effective = 0
        # COMMON_WINDOWS: effective damage and completed casts in [0, N] s, streaming
        self._window_effective = [0] * len(COMMON_WINDOWS)
        self._window_casts = [0] * len(COMMON_WINDOWS)
        # casts
        self.casts = _PerfTable(MAX_PERF_SPELLS, "perf_cast_spells_truncated",
                                self._cast_row, self.warn)
        self._pending_casts: dict = {}
        self._cast_seen: set = set()
        self.total_success = 0
        self.timeline: list[list] = []
        self.timeline_covered_until_ms: int | None = None
        # life
        self._pre_alive: bool | None = None
        self.alive_at_start: bool | None = None
        self.alive_basis: str | None = None
        self.alive: bool | None = None
        self.life_changes: list[tuple[int, bool]] = []
        self.deaths_ms: list[int] = []
        self.resurrections_ms: list[int] = []
        self.post_deaths_ms: list[int] = []
        # continuity
        self._last_action_ms: int | None = None
        self.gaps_count = self.gaps_total_ms = self.gap_max_ms = 0
        self.longest_gaps: list[tuple[int, int]] = []
        # auras
        self.auras: dict[tuple, _PerfAura] = {}
        self._unreserved_aura_keys = 0
        self._rejected_aura_keys: set[tuple] = set()
        self.rejected_aura_keys = 0
        self.rejected_keys_is_lower_bound = False
        self.first_rejected_ms: int | None = None
        self._interval_count = 0
        # resources
        self.power: dict[int, dict] = {}
        self.resource_samples: list[tuple[int, int, int, int | None]] = []
        self.resource_samples_partial = False
        self.energize = _PerfTable(MAX_PERF_SPELLS, "perf_energize_truncated",
                                   self._energize_row, self.warn)
        self.energize_events: list[tuple] = []
        self.energize_events_partial = False
        # (exception, "observe" | "result") once a call into this accumulator raised:
        # it is not fed again and result() is replaced by error_result().
        self.failure: tuple[BaseException, str] | None = None

    # -- small helpers ------------------------------------------------------------
    def warn(self, code: str, cap: int) -> None:
        warning = self.warnings.get(code)
        if warning is None:
            warning = self.warnings[code] = {"code": code, "cap": cap, "dropped": 0}
        warning["dropped"] += 1

    @staticmethod
    def _target_row(name):
        return {"name": name, "npc_id": None, "kind": None, "instances": set(), "hits": 0,
                "amount": 0, "overkill": 0, "effective": 0, "max_hp": None,
                "raid_marker": None}

    def _pet_row(self, name):
        return {"name": name, "npc_id": None, "effective": 0,
                "spells": _PerfTable(MAX_PERF_SPELLS, "perf_pet_spells_truncated",
                                     _damage_row, self.warn)}

    @staticmethod
    def _cast_row(name):
        return {"name": name, "start": 0, "success": 0, "failed": {},
                "start_without_outcome": 0, "started_before_pull": 0}

    @staticmethod
    def _energize_row(name):
        return {"name": name, "events": 0, "amount": 0, "over_energize": 0}

    @staticmethod
    def _add_to_windows(totals: list[int], t_ms: int, value: int) -> None:
        for index, seconds in enumerate(COMMON_WINDOWS):
            if t_ms <= seconds * 1000:
                totals[index] += value

    def _window_rows(self) -> list[dict]:
        """damage.windows: a row is covered only when the observation and the player's
        alive time from t=0 both reach N (a player dead earlier is not comparable)."""
        alive_until = None
        if self.alive_at_start is True:
            alive_until = min([self.end_ms or 0] + self.deaths_ms[:1])
        return [{"seconds": seconds, "effective": self._window_effective[index],
                 "casts_success": self._window_casts[index],
                 "covered": alive_until is not None and alive_until >= seconds * 1000}
                for index, seconds in enumerate(COMMON_WINDOWS)]

    def _spec_event(self, kind: str, t_ms: int, **fields) -> None:
        observer = self.spec_rules.get("observe") if self.spec_rules else None
        if observer is not None and self.phase == "encounter":
            record = {"kind": kind, "t_ms": t_ms}
            record.update(fields)
            observer(self, self.spec_rules, record)

    @property
    def status(self) -> str:
        if len(self.matches) > 1:
            return "ambiguous"
        return "resolved" if self.player_guid is not None else "absent"

    # -- identity -----------------------------------------------------------------
    def _name_matches(self, name: str | None) -> bool:
        folded = name.casefold()
        if self._mode == "full":
            return folded == self._selector_folded
        return folded.split("-", 1)[0] == self._selector_folded

    def _check_unit(self, guid: str | None, name: str | None, t_ms: int) -> None:
        if not guid or not guid.startswith("Player-"):
            return
        if guid == self.player_guid:
            if self.player_name is None and name and name != "nil":
                self.player_name = self.matches[guid] = name
            return
        if self._mode == "guid":
            if guid.casefold() == self._selector_folded:
                self._add_match(guid, name, t_ms)
            return
        matched = self._checked.get(guid)
        if matched is None:
            if not name or name == "nil":
                return
            matched = self._name_matches(name)
            # A full cache only stops caching: the name is then compared directly,
            # so ambiguity detection never depends on this cap.
            if len(self._checked) < MAX_PERF_CHECKED_GUIDS:
                self._checked[guid] = matched
        if matched and guid not in self.matches:
            self._add_match(guid, name, t_ms)

    def _add_match(self, guid: str, name: str | None, t_ms: int) -> None:
        self.matches[guid] = name if name and name != "nil" else None
        if len(self.matches) > 1:
            self._configs.clear()
            return
        self.player_guid = guid
        self.player_name = self.matches[guid]
        config = self._configs.pop(guid, None)
        self._configs.clear()
        if config is not None:
            self._adopt_config(config)

    def _combatant_info(self, args: list[str], parsed: ParsedCombatEvent,
                        t_ms: int) -> None:
        guid = parsed.source_guid
        if not guid or self.status == "ambiguous":
            return
        if self.player_guid is not None:
            if guid != self.player_guid:
                return
        elif self._mode == "guid":
            if guid.casefold() != self._selector_folded:
                return
            # The exact GUID is the player: their COMBATANT_INFO alone resolves them
            # (the name is filled by the first event that carries it).
            self._add_match(guid, None, t_ms)
        elif self._checked.get(guid) is False:
            return
        details = parse_combatant_details(args)
        config = {"spec_id": details["spec_id"], "item_level": parsed.item_level,
                  "talents": details["talents"], "equipment": details["equipment"],
                  "initial_auras": details["initial_auras"], "t_ms": t_ms,
                  "phase": self.phase}
        if guid == self.player_guid:
            self._adopt_config(config)
        elif guid in self._configs or len(self._configs) < MAX_PERF_CONFIGS:
            self._configs[guid] = config
        else:
            self._configs_dropped += 1
            self.warn("perf_configs_truncated", MAX_PERF_CONFIGS)

    def _adopt_config(self, config: dict) -> None:
        self.config = config
        if config["spec_id"] is not None and config["spec_id"] != self.spec_id:
            self.spec_id = config["spec_id"]
            self._activate_spec_rules()
        # Only the snapshot of this pull (logged after its own START) is the state
        # at t=0; a short earlier pull's COMBATANT_INFO in the pre-context is not.
        auras = config["initial_auras"].get("auras")
        if config["phase"] != "encounter" or self.phase != "encounter" or not auras:
            return
        start_ms = max(0, config["t_ms"])
        for caster, spell_id, stacks in auras:
            source = "self" if caster == self.player_guid else "other"
            aura = self._aura("on_player", spell_id, source, None, None, start_ms)
            if aura is None:
                continue
            if not aura.active:
                self._open(aura, start_ms, "combatant_info")
            self._hold(aura, caster, start_ms)
            self._set_stacks(aura, stacks)

    def _activate_spec_rules(self) -> None:
        rules = self._rules_source.get(self.spec_id)
        self.spec_rules = rules
        # Only the matching rules keep target-aura interval history from here on.
        self._settle_tracked_targets(
            set(rules.get("tracked_target_auras", ())) if rules else set())
        if not rules:
            return
        self._reserved_auras |= set(rules.get("tracked_self_auras", ())) | \
            self._tracked_target_auras
        self._channel_ticks = set(rules.get("channel_tick_spells", ()))
        # Keys of every registered rule set are reserved (and target auras followed)
        # from their creation; a key created otherwise (another spec's rules before a
        # spec change) is upgraded here: it stops counting against MAX_PERF_AURAS,
        # and a tracked target aura gains its interval history, starting from the
        # live state (its open interval keeps its real start and basis).
        for (scope, spell_id, _), aura in self.auras.items():
            if spell_id not in self._reserved_auras:
                continue
            if not aura.reserved:
                aura.reserved = True
                self._unreserved_aura_keys -= 1
            if scope == "from_player" and spell_id in self._tracked_target_auras and \
                    not aura.with_intervals:
                aura.with_intervals = True
                if aura.holders_truncated:
                    self.warn("perf_aura_holders_truncated", MAX_PERF_INSTANCES)

    def _settle_tracked_targets(self, tracked: set) -> None:
        """Keep target-aura interval history only for `tracked` (the matching rules):
        a key followed only because another rule set tracks it publishes no list."""
        self._tracked_target_auras = tracked
        for (scope, spell_id, _), aura in self.auras.items():
            if scope == "from_player" and aura.with_intervals and spell_id not in tracked:
                aura.with_intervals = False
                self._interval_count -= len(aura.intervals)
                aura.intervals = []

    # -- streaming entry point ------------------------------------------------------
    def observe(self, timestamp: datetime, event: str, args: list[str],
                parsed: ParsedCombatEvent, pet_owners, line_offset: int | None) -> None:
        t_ms = _ms_between(self.start_ts, timestamp)
        self.last_ms = t_ms
        self.last_offset = line_offset
        if event == "ENCOUNTER_START":
            if self.phase == "pre" and timestamp == self.start_ts and \
                    to_int(arg_at(args, 0)) == self.encounter_id:
                self._begin_encounter(args, line_offset)
            return
        if event == "ENCOUNTER_END":
            encounter_id = to_int(arg_at(args, 0))
            if self.phase == "encounter" and encounter_id is not None and \
                    encounter_id == self.encounter_id:
                self._end_encounter(t_ms, line_offset)
            return
        if event == "COMBATANT_INFO":
            self._combatant_info(args, parsed, t_ms)
            return
        if self.phase == "encounter" and t_ms < 0:
            t_ms = 0
        source, destination = parsed.source_guid, parsed.destination_guid
        if len(self.matches) > 1:
            return
        if self._mode != "guid" or self.player_guid is None:
            self._check_unit(source, parsed.source_name, t_ms)
            self._check_unit(destination, parsed.destination_name, t_ms)
            if len(self.matches) > 1:
                return
        elif self.player_name is None:
            self._check_unit(source, parsed.source_name, t_ms)
            self._check_unit(destination, parsed.destination_name, t_ms)
        player = self.player_guid
        if player is None:
            return
        from_player = source == player
        to_player = destination == player
        pet = None
        if not from_player and source and pet_owners.get(source) == player:
            pet = source
        if not (from_player or to_player or pet):
            return
        if self.phase == "encounter" and self.spec_rules and not self._spec_begun:
            # Before this event is applied: the rules see the state established
            # before it, whichever event happens to come first.
            self._spec_begun = True
            begin = self.spec_rules.get("begin")
            if begin is not None:
                begin(self, self.spec_rules, t_ms)
        if event in PERF_SAMPLED_EVENTS:
            # Any block describing the player carries their power: casts and swings
            # by them, damage and heals they receive, energizes they cast.
            self._sample(args[8:], t_ms,
                         0 if event.startswith(PERF_UNPREFIXED_EVENTS) else 3)
        if event in PERF_DAMAGE_EVENTS:
            if from_player or pet:
                self._damage(event, args, parsed, t_ms, pet)
        elif event in PERF_CAST_KINDS:
            if from_player:
                self._cast(event, args, parsed, t_ms)
        elif event in PERF_AURA_EVENTS:
            if to_player:
                self._aura_event(event, args, parsed, t_ms, "on_player",
                                 "self" if from_player else "other", source or "")
            elif from_player:
                self._aura_event(event, args, parsed, t_ms, "from_player", "self",
                                 destination or "")
        elif event == "SPELL_ABSORBED":
            if (from_player or pet) and not to_player:
                self._absorbed(parsed, t_ms, pet)
        elif event in PERF_ENERGIZE_EVENTS:
            self._energize(args, parsed, t_ms, to_player)
        elif event == "UNIT_DIED":
            if to_player:
                self._death(t_ms)
        elif event == "SPELL_RESURRECT":
            if to_player:
                self._resurrection(t_ms)

    # -- phase --------------------------------------------------------------------
    def _begin_encounter(self, args: list[str], line_offset: int | None) -> None:
        self.phase = "encounter"
        self.encounter_name = unquote(arg_at(args, 1)) or None
        self.encounter_start_offset = line_offset
        if self._pre_alive is not None:
            self.alive_at_start, self.alive_basis = self._pre_alive, "pre_context"
            self.alive = self._pre_alive

    def _end_encounter(self, t_ms: int, line_offset: int | None) -> None:
        self.phase = "post"
        self._ended = True
        self.end_ms = max(0, t_ms)
        self.encounter_end_offset = line_offset
        self._close_pending_casts()

    def _close_pending_casts(self) -> None:
        # A START never followed by its SUCCESS/FAILED: no outcome was logged, which
        # is not evidence of a cancel. (A list is a pre-context START: not counted.)
        for row in self._pending_casts.values():
            if isinstance(row, dict):
                row["start_without_outcome"] += 1
        self._pending_casts.clear()

    # -- life and continuity -------------------------------------------------------
    def _life_evidence(self, basis: str, alive_before: bool) -> None:
        """First in-encounter evidence fixes an unknown state at t=0 retroactively."""
        if self.alive_at_start is not None:
            return
        self.alive_at_start, self.alive_basis = alive_before, basis
        self.alive = alive_before
        if not alive_before:
            self.after_death_effective += self._unknown_life_effective
        self._unknown_life_effective = 0

    def _action(self, t_ms: int) -> None:
        if self.phase != "encounter":
            if self.phase == "pre":
                self._pre_alive = True
            return
        self._life_evidence("first_action", True)
        if self.alive is False:
            return
        if self._last_action_ms is not None:
            gap = t_ms - self._last_action_ms
            if gap > ACTION_GAP_SECONDS * 1000:
                self.gaps_count += 1
                self.gaps_total_ms += gap
                self.gap_max_ms = max(self.gap_max_ms, gap)
                self.longest_gaps.append((self._last_action_ms, t_ms))
                self.longest_gaps.sort(key=lambda item: (item[0] - item[1], item[0]))
                del self.longest_gaps[MAX_PERF_LONGEST_GAPS:]
        self._last_action_ms = t_ms

    def _death(self, t_ms: int) -> None:
        if self.phase == "pre":
            self._pre_alive = False
            return
        if self.phase == "post":
            self.post_deaths_ms.append(t_ms - (self.end_ms or 0))
            return
        self._life_evidence("death", True)
        self.deaths_ms.append(t_ms)
        self.life_changes.append((t_ms, False))
        self.alive = False
        # A gap is only measured between actions of one alive period.
        self._last_action_ms = None
        self._spec_event("death", t_ms)

    def _resurrection(self, t_ms: int) -> None:
        if self.phase == "pre":
            self._pre_alive = True
            return
        if self.phase == "post":
            return
        self._life_evidence("resurrection", False)
        self.resurrections_ms.append(t_ms)
        self.life_changes.append((t_ms, True))
        self.alive = True
        self._spec_event("resurrection", t_ms)

    # -- damage ---------------------------------------------------------------------
    def _damage(self, event: str, args: list[str], parsed: ParsedCombatEvent,
                t_ms: int, pet: str | None) -> None:
        payload = args[8:]
        value_index = 0 if event == "SWING_DAMAGE" else 3
        advanced = _advanced_state(payload, value_index)
        if advanced is not None:
            value_index = advanced[0]
        suffix = _damage_suffix(event, payload, value_index)
        amount = suffix["amount"]
        if amount is None:
            return
        overkill = max(suffix["overkill"] or 0, 0)
        effective = amount - overkill
        if self.phase != "encounter":
            self.excluded[self.phase + "_context"] += effective
            return
        if parsed.destination_guid == self.player_guid:
            self.excluded["self_damage"] += effective
            return
        if not parsed.destination_flags & REACTION_HOSTILE:
            self.excluded["non_hostile_target"] += effective
            return
        if pet is None:
            self._life_evidence("first_action", True)
        if self.alive is False:
            self.after_death_effective += effective
        elif self.alive is None:
            self._unknown_life_effective += effective
        self._add_to_windows(self._window_effective, t_ms, effective)
        spell_id = parsed.spell_id if event != "SWING_DAMAGE" else MELEE_SPELL_ID
        name = parsed.spell_name if event != "SWING_DAMAGE" else MELEE_SPELL_NAME
        tick = event == "SPELL_PERIODIC_DAMAGE"
        rows = [self.damage_pets if pet else self.damage_player]
        if pet:
            pet_row = self._pet(pet, parsed.source_name)
            pet_row["effective"] += effective
            rows.append(pet_row["spells"].row(spell_id, name))
        else:
            rows.append(self.by_spell.row(spell_id, name))
        for row in rows:
            row["ticks" if tick else "hits"] += 1
            row["crits"] += 1 if suffix["critical"] else 0
            row["amount"] += amount
            row["overkill"] += overkill
            row["effective"] += effective
        target_key = self._target(parsed, args, amount, overkill, effective)
        if pet is None and spell_id in self._channel_ticks:
            self._action(t_ms)
        self._spec_event("damage", t_ms, spell_id=spell_id, pet=pet is not None,
                         tick=tick, amount=amount, effective=effective,
                         critical=bool(suffix["critical"]), target_key=target_key)

    def _pet(self, pet: str, name: str | None) -> dict:
        key, npc_id, _ = _perf_unit(pet)
        row = self.by_pet.row(key, name)
        if row is not self.by_pet.other:
            row["npc_id"] = npc_id
        return row

    def _target(self, parsed: ParsedCombatEvent, args: list[str], amount: int,
                overkill: int, effective: int) -> str:
        guid = parsed.destination_guid or ""
        key, npc_id, kind = _perf_unit(guid)
        row = self.by_target.row(key, parsed.destination_name)
        if row is not self.by_target.other:
            row["npc_id"], row["kind"] = npc_id, kind
            if guid not in row["instances"]:
                if len(row["instances"]) < MAX_PERF_INSTANCES:
                    row["instances"].add(guid)
                else:
                    self.warn("perf_target_instances_truncated", MAX_PERF_INSTANCES)
            if parsed.target_max_hp is not None:
                row["max_hp"] = max(row["max_hp"] or 0, parsed.target_max_hp)
            marker = _flags(arg_at(args, 7)) & 0xFF
            if marker:
                row["raid_marker"] = marker
        row["hits"] += 1
        row["amount"] += amount
        row["overkill"] += overkill
        row["effective"] += effective
        return key

    def _absorbed(self, parsed: ParsedCombatEvent, t_ms: int, pet: str | None) -> None:
        """Damage a hostile target's shield absorbed: SPELL_ABSORBED is the only source
        (a fully absorbed hit has no damage line; a damage line's own `absorbed`
        field is never added on top)."""
        if self.phase != "encounter" or not parsed.destination_flags & REACTION_HOSTILE:
            return
        amount = parsed.amount
        if amount is None or amount <= 0:
            return
        spell_id = parsed.spell_id if parsed.spell_id is not None else MELEE_SPELL_ID
        name = parsed.spell_name if parsed.spell_id is not None else MELEE_SPELL_NAME
        if pet:
            self.damage_pets["absorbed_by_target"] += amount
            self._pet(pet, parsed.source_name)["spells"].row(spell_id, name)["absorbed_by_target"] += amount
        else:
            self.damage_player["absorbed_by_target"] += amount
            self.by_spell.row(spell_id, name)["absorbed_by_target"] += amount
        self._spec_event("absorbed", t_ms, spell_id=spell_id, pet=pet is not None,
                         amount=amount)

    # -- casts and timeline -----------------------------------------------------------
    def _cast(self, event: str, args: list[str], parsed: ParsedCombatEvent,
              t_ms: int) -> None:
        if self.phase == "post":
            return
        kind = PERF_CAST_KINDS[event]
        spell_id = parsed.spell_id
        if len(self._cast_seen) < 4 * MAX_PERF_SPELLS:
            self._cast_seen.add(spell_id)
        destination = parsed.destination_guid
        target_key = _perf_unit(destination)[0] \
            if destination and destination != "0000000000000000" else None
        entry = [t_ms, kind, spell_id, target_key]
        if kind != "failed":
            self._action(t_ms)
        # Pending START per spell: a list = started in the pre-context (its timeline
        # entry), a dict = started in the encounter (its row).
        pending = self._pending_casts
        room = spell_id in pending or len(pending) < 2 * MAX_PERF_SPELLS
        if self.phase == "pre":
            # Nothing of the pre-context enters the timeline, except the START of a
            # cast whose outcome lands in the encounter (added with that outcome).
            if kind == "start":
                if room:
                    pending[spell_id] = entry
            else:
                pending.pop(spell_id, None)
            return
        row = self.casts.row(spell_id, parsed.spell_name)
        if kind == "start":
            previous = pending.get(spell_id)
            if isinstance(previous, dict):
                previous["start_without_outcome"] += 1
            row["start"] += 1
            if room:
                pending[spell_id] = row
        else:
            started = pending.pop(spell_id, None)
            if isinstance(started, list):
                row["started_before_pull"] += 1
                self._timeline_add(started, precast=True)
            if kind == "success":
                row["success"] += 1
                self.total_success += 1
                self._add_to_windows(self._window_casts, t_ms, 1)
            else:
                reason = unquote(arg_at(args, 11)) or "unknown"
                row["failed"][reason] = row["failed"].get(reason, 0) + 1
        self._timeline_add(entry)
        self._spec_event("cast", t_ms, cast=kind, spell_id=spell_id, target_key=target_key)

    def _timeline_add(self, entry: list, precast: bool = False) -> None:
        timeline = self.timeline
        if len(timeline) >= MAX_PERF_TIMELINE:
            if self.timeline_covered_until_ms is None and not precast:
                self.timeline_covered_until_ms = entry[0]
            self.warn("perf_timeline_truncated", MAX_PERF_TIMELINE)
            return
        if not precast:
            timeline.append(entry)
            return
        # A precast START (negative t_ms) goes after the earlier precast STARTs and
        # before every encounter entry (t_ms >= 0), so the list stays in time order.
        index = 0
        while index < len(timeline) and timeline[index][0] < 0 and \
                timeline[index][0] <= entry[0]:
            index += 1
        timeline.insert(index, entry)

    # -- auras --------------------------------------------------------------------------
    def _aura(self, scope: str, spell_id, source: str, name, aura_type,
              t_ms: int) -> _PerfAura | None:
        key = (scope, spell_id, source)
        aura = self.auras.get(key)
        if aura is not None:
            if aura.name is None and name:
                aura.name = name
            if aura.aura_type is None and aura_type:
                aura.aura_type = aura_type
            return aura
        reserved = spell_id in self._reserved_auras
        if not reserved and self._unreserved_aura_keys >= MAX_PERF_AURAS:
            # Saturation (b): the new key is not followed at all.
            if key not in self._rejected_aura_keys:
                if len(self._rejected_aura_keys) >= MAX_PERF_AURAS:
                    # The set of rejected keys is full: a key outside it may already
                    # have been counted, so counting stops and the count is a minimum.
                    self.rejected_keys_is_lower_bound = True
                    return None
                self._rejected_aura_keys.add(key)
                self.rejected_aura_keys += 1
                if self.first_rejected_ms is None:
                    self.first_rejected_ms = max(0, t_ms)
                self.warn("perf_aura_keys_truncated", MAX_PERF_AURAS)
            return None
        if not reserved:
            self._unreserved_aura_keys += 1
        with_intervals = scope == "on_player" or spell_id in self._tracked_target_auras
        aura = _PerfAura(spell_id, name, aura_type, source, with_intervals, reserved)
        self.auras[key] = aura
        return aura

    def _open(self, aura: _PerfAura, start_ms: int | None, basis: str) -> None:
        aura.start_ms, aura.start_basis = start_ms, basis
        aura.interval_max_stacks = aura.stacks

    def _close(self, aura: _PerfAura, end_ms: int, end_basis: str) -> None:
        if self.phase == "pre":
            aura.start_ms = aura.start_basis = None
            aura.holders.clear()
            aura.stacks = 0
            return
        origin = aura.start_ms if aura.start_ms is not None else aura.last_inactive_ms
        end_ms = max(end_ms, origin)
        aura.upper_ms += end_ms - origin
        if aura.start_basis == "unknown":
            aura.unknown_start_intervals += 1
        else:
            aura.observed_ms += end_ms - aura.start_ms
        if aura.with_intervals:
            if aura.partial or self._interval_count >= MAX_PERF_AURA_INTERVALS:
                # Saturation (a): sums above stay exact, only the list is partial.
                # (Also after a holder overflow, which made the row partial first.)
                if not aura.partial:
                    aura.partial, aura.covered_until_ms = True, origin
                if self._interval_count >= MAX_PERF_AURA_INTERVALS:
                    self.warn("perf_aura_intervals_truncated", MAX_PERF_AURA_INTERVALS)
            else:
                self._interval_count += 1
                aura.intervals.append([aura.start_ms, end_ms, aura.start_basis, end_basis,
                                       aura.interval_max_stacks])
        aura.last_inactive_ms = end_ms
        aura.start_ms = aura.start_basis = None
        aura.holders.clear()
        aura.stacks = 0

    def _hold(self, aura: _PerfAura, holder: str, t_ms: int) -> None:
        if holder in aura.holders:
            return
        if len(aura.holders) < MAX_PERF_INSTANCES:
            aura.holders.add(holder)
            return
        # The untracked holder's removal will not be seen: from here the interval may
        # stay open too long, so the row is partial from this time on.
        if not aura.holders_truncated:
            aura.holders_truncated = True
            if not aura.partial:
                aura.partial, aura.covered_until_ms = True, max(0, t_ms)
        if aura.with_intervals:
            self.warn("perf_aura_holders_truncated", MAX_PERF_INSTANCES)

    @staticmethod
    def _set_stacks(aura: _PerfAura, stacks: int | None) -> None:
        if stacks is None:
            return
        aura.stacks = stacks
        aura.interval_max_stacks = max(aura.interval_max_stacks, stacks)
        aura.max_stacks = max(aura.max_stacks, stacks)

    def _aura_event(self, event: str, args: list[str], parsed: ParsedCombatEvent,
                    t_ms: int, scope: str, source: str, holder: str) -> None:
        if self.phase == "post":
            return
        aura = self._aura(scope, parsed.spell_id, source, parsed.spell_name,
                          parsed.aura_type, t_ms)
        if aura is None:
            return
        encounter = self.phase == "encounter"
        removal = event in {"SPELL_AURA_REMOVED", "SPELL_AURA_BROKEN",
                            "SPELL_AURA_BROKEN_SPELL"}
        if encounter:
            if event == "SPELL_AURA_APPLIED":
                aura.applications += 1
            elif event == "SPELL_AURA_REFRESH":
                aura.refreshes += 1
            elif removal:
                aura.removals += 1
            if scope == "from_player" and holder not in aura.targets:
                if len(aura.targets) < MAX_PERF_INSTANCES:
                    aura.targets.add(holder)
                else:
                    self.warn("perf_aura_targets_truncated", MAX_PERF_INSTANCES)
        # Every key follows the live state below; only keys with_intervals record it
        # (see _close), so a key upgraded by spec rules starts from its real state.
        if removal:
            if holder in aura.holders:
                aura.holders.discard(holder)
                if not aura.holders:
                    self._close(aura, t_ms, "observed")
            elif not aura.active and encounter:
                # Removed without a seen application nor initial state: the start
                # is not established (the COMBATANT_INFO list is partial).
                self._open(aura, None, "unknown")
                self._close(aura, t_ms, "observed")
        else:
            if not aura.active:
                if event == "SPELL_AURA_APPLIED":
                    self._open(aura, 0 if not encounter else t_ms,
                               "pre_context" if not encounter else "observed")
                else:
                    # Refresh/dose of an aura never seen applied: it is active, but
                    # since when is only known if the evidence predates the pull.
                    self._open(aura, 0 if not encounter else None,
                               "pre_context" if not encounter else "unknown")
            self._hold(aura, holder, t_ms)
            if event == "SPELL_AURA_APPLIED":
                self._set_stacks(aura, 1)
            elif event in {"SPELL_AURA_APPLIED_DOSE", "SPELL_AURA_REMOVED_DOSE"}:
                self._set_stacks(aura, _aura_stacks(args[8:]))
        if encounter:
            self._spec_event("aura", t_ms, scope=scope, event=event,
                             spell_id=parsed.spell_id, source=source, holder=holder,
                             stacks=aura.stacks, active=aura.active)

    def aura_intervals(self, scope: str, spell_id) -> list[dict]:
        """Retained intervals of one spell (every source), for spec builders."""
        result = []
        for (key_scope, key_spell, _), aura in sorted(
                self.auras.items(), key=lambda item: item[0][2]):
            if key_scope == scope and key_spell == spell_id and aura.with_intervals:
                result.append({"source": aura.source, "intervals": aura.intervals,
                               "partial": aura.partial,
                               "covered_until_ms": aura.covered_until_ms})
        return result

    # -- resources ----------------------------------------------------------------------
    def _sample(self, payload: list[str], t_ms: int, base: int) -> None:
        if self.phase != "encounter":
            return
        state = _power_state(payload, base)
        # Only a block describing the player is a sample; any other unit's is ignored.
        if state is None or state[0] != self.player_guid or state[2] is None:
            return
        _, power_type, current, maximum, _ = state
        stats = self.power.get(power_type)
        if stats is None:
            stats = self.power[power_type] = {
                "samples": 0, "max_gap_ms": 0, "first": (t_ms, current, maximum),
                "last": None, "min": current, "max": current}
        else:
            stats["max_gap_ms"] = max(stats["max_gap_ms"], t_ms - stats["last"][0])
            stats["min"] = min(stats["min"], current)
            stats["max"] = max(stats["max"], current)
        stats["samples"] += 1
        stats["last"] = (t_ms, current, maximum)
        if len(self.resource_samples) < MAX_PERF_RESOURCE_SAMPLES:
            self.resource_samples.append((t_ms, power_type, current, maximum))
        else:
            self.resource_samples_partial = True
            self.warn("perf_resource_samples_truncated", MAX_PERF_RESOURCE_SAMPLES)

    def _energize(self, args: list[str], parsed: ParsedCombatEvent, t_ms: int,
                  to_player: bool) -> None:
        if self.phase != "encounter":
            return
        payload = args[8:]
        if not to_player:
            return
        suffix = _energize_suffix(payload)
        if suffix is None:
            return
        amount = suffix["amount"] or 0
        over = suffix["over_energize"] or 0
        row = self.energize.row((parsed.spell_id, suffix["power_type"]), parsed.spell_name)
        row["events"] += 1
        row["amount"] += amount
        row["over_energize"] += over
        if len(self.energize_events) < MAX_PERF_ENERGIZE_EVENTS:
            self.energize_events.append((t_ms, parsed.spell_id, suffix["power_type"],
                                         amount, over))
        else:
            self.energize_events_partial = True
            self.warn("perf_energize_events_truncated", MAX_PERF_ENERGIZE_EVENTS)
        self._spec_event("energize", t_ms, spell_id=parsed.spell_id,
                         power_type=suffix["power_type"], amount=amount,
                         over_energize=over, max_power=suffix["max_power"])

    # -- result -------------------------------------------------------------------------
    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        if not self.spec_rules:
            # Spec never known (or without rules): no target aura is tracked.
            self._settle_tracked_targets(set())
        if not self._ended:
            # Incomplete pull: the observation ends at the last line seen.
            self.end_ms = max(0, self.last_ms) if self.phase == "encounter" else 0
            self._close_pending_casts()
        if self.phase == "pre":
            return
        end_basis = "encounter_end" if self._ended else "observation_end"
        phase, self.phase = self.phase, "encounter"
        for aura in self.auras.values():
            if aura.active:
                self._close(aura, self.end_ms, end_basis)
        self.phase = phase

    def _life_seconds(self) -> tuple[int | None, int | None]:
        if self.alive_at_start is None:
            return None, None
        alive_ms = dead_ms = 0
        state, since = self.alive_at_start, 0
        for t_ms, alive in self.life_changes + [(self.end_ms, state)]:
            t_ms = min(max(t_ms, since), self.end_ms)
            if state:
                alive_ms += t_ms - since
            else:
                dead_ms += t_ms - since
            state, since = alive, t_ms
        return alive_ms, dead_ms

    def _character(self) -> dict:
        config = self.config
        if config is None:
            status = "not_retained" if self._configs_dropped else "absent"
            return {"combatant_info": status, "spec_id": None, "class_id": None,
                    "item_level": None, "item_level_basis": None,
                    "talents": {"status": "absent", "fingerprint": None, "count": None},
                    "equipment": {"status": "absent", "fingerprint": None, "items": None},
                    "initial_auras": {"status": "absent", "count": None}}
        auras = config["initial_auras"]
        item_level, basis = config["item_level"], "all_positive_slots"
        items = config["equipment"].get("items")
        if items is not None and len(items) == 18:
            # COMBATANT_INFO slot order: index 3 is the shirt, 17 the tabard. Both are
            # cosmetic (a tabard logs ilvl 1-2) and would drag the average down.
            levels = [level for index, (_, level) in enumerate(items)
                      if index not in (3, 17) and level > 0]
            item_level = (2 * sum(levels) + len(levels)) // (2 * len(levels)) \
                if levels else None
            basis = "equipped_slots_excluding_shirt_tabard"
        return {"combatant_info": "ok" if config["spec_id"] is not None
                else "unsupported_layout",
                "spec_id": config["spec_id"],
                "class_id": SPEC_CLASSES.get(config["spec_id"]),
                "item_level": item_level, "item_level_basis": basis,
                "talents": dict(config["talents"]),
                "equipment": dict(config["equipment"]),
                "initial_auras": {"status": auras["status"],
                                  "count": None if auras.get("auras") is None
                                  else len(auras["auras"])}}

    @staticmethod
    def _damage_rows(table: _PerfTable, id_key: str = "spell_id") -> list[dict]:
        rows = [{id_key: key, **row} for key, row in table.rows.items()]
        rows.sort(key=lambda row: (-row["amount"], _perf_sort_id(row[id_key])))
        if table.other is not None:
            rows.append({id_key: None, **table.other})
        return rows

    def _damage_section(self, spell_names: dict, duration_ms: int | None,
                        duration_reason: str | None, alive_ms: int | None) -> dict:
        total = self.damage_player["effective"] + self.damage_pets["effective"]
        by_spell = self._damage_rows(self.by_spell)
        pets = []
        for key, row in self.by_pet.rows.items():
            pets.append({"npc_id": row["npc_id"], "name": row["name"],
                         "effective": row["effective"],
                         "by_spell": self._damage_rows(row["spells"])})
        pets.sort(key=lambda row: (-row["effective"], _perf_sort_id(row["npc_id"])))
        if self.by_pet.other is not None:
            other = self.by_pet.other
            pets.append({"npc_id": None, "name": PERF_OTHER, "effective": other["effective"],
                         "by_spell": self._damage_rows(other["spells"])})
        targets = []
        for key, row in self.by_target.rows.items():
            matches = bool(self.encounter_name) and row["name"] == self.encounter_name
            targets.append({"key": key, "npc_id": row["npc_id"], "name": row["name"],
                            "kind": row["kind"], "instances": len(row["instances"]),
                            "hits": row["hits"], "amount": row["amount"],
                            "overkill": row["overkill"], "effective": row["effective"],
                            "role": "boss" if matches else "unknown",
                            "evidence": {"max_hp": row["max_hp"],
                                         "raid_marker": row["raid_marker"],
                                         "name_matches_encounter": matches}})
        targets.sort(key=lambda row: (-row["amount"], row["key"]))
        if self.by_target.other is not None:
            other = self.by_target.other
            targets.append({"key": PERF_OTHER, "npc_id": None, "name": PERF_OTHER,
                            "kind": None, "instances": None, "hits": other["hits"],
                            "amount": other["amount"], "overkill": other["overkill"],
                            "effective": other["effective"], "role": "unknown",
                            "evidence": {"max_hp": None, "raid_marker": None,
                                         "name_matches_encounter": False}})
        for row in by_spell:
            if row["spell_id"] is not None:
                spell_names.setdefault(row["spell_id"], row["name"])
        for pet in pets:
            for row in pet["by_spell"]:
                if row["spell_id"] is not None:
                    spell_names.setdefault(row["spell_id"], row["name"])
        # Auto-attack is never cast, so it would always be listed.
        without_cast = sorted(key for key in self.by_spell.rows
                              if key not in self._cast_seen and
                              key not in (None, MELEE_SPELL_ID))
        if alive_ms is None:
            while_alive = _perf_rate(0, None, "life state unknown")
        elif not alive_ms:
            while_alive = _perf_rate(0, None, "no alive time")
        else:
            while_alive = _perf_rate(total - self.after_death_effective, alive_ms)
        rates = {"dps_encounter": _perf_rate(total, duration_ms, duration_reason),
                 "dps_while_alive": while_alive}
        if not self._ended:
            rates["dps_observed"] = _perf_rate(total, self.end_ms)
        pets_section = dict(self.damage_pets)
        pets_section.pop("name")
        pets_section["by_pet"] = pets
        player_section = dict(self.damage_player)
        player_section.pop("name")
        return {"player": player_section, "pets": pets_section,
                "total_effective": total,
                "after_death_effective": self.after_death_effective,
                "excluded": dict(self.excluded), "by_spell": by_spell,
                "by_target": targets, "damage_without_cast": without_cast,
                "rates": rates, "windows": self._window_rows()}

    def _casts_section(self, spell_names: dict, rate_ms: int | None) -> dict:
        rows = []
        for key, row in self.casts.rows.items():
            rows.append({"spell_id": key, "name": row["name"], "start": row["start"],
                         "success": row["success"],
                         "failed": dict(sorted(row["failed"].items())),
                         "start_without_outcome": row["start_without_outcome"],
                         "started_before_pull": row["started_before_pull"]})
            if key is not None:
                spell_names.setdefault(key, row["name"])
        rows.sort(key=lambda row: (-row["success"], -row["start"],
                                   _perf_sort_id(row["spell_id"])))
        if self.casts.other is not None:
            other = self.casts.other
            rows.append({"spell_id": None, "name": PERF_OTHER, "start": other["start"],
                         "success": other["success"],
                         "failed": dict(sorted(other["failed"].items())),
                         "start_without_outcome": other["start_without_outcome"],
                         "started_before_pull": other["started_before_pull"]})
        return {"by_spell": rows, "total_success": self.total_success,
                "rates": {"casts_per_minute": _perf_rate(self.total_success, rate_ms,
                                                         per_seconds=60)}}

    def _aura_section(self, spell_names: dict) -> dict:
        on_player, from_player = [], []
        for (scope, spell_id, source), aura in self.auras.items():
            if spell_id is not None and aura.name:
                spell_names.setdefault(spell_id, aura.name)
            intervals = [{"start": _perf_s(start), "end": _perf_s(end),
                          "start_basis": start_basis, "end_basis": end_basis,
                          "max_stacks": stacks}
                         for start, end, start_basis, end_basis, stacks in aura.intervals]
            if scope == "on_player":
                on_player.append({
                    "spell_id": spell_id, "name": aura.name, "aura_type": aura.aura_type,
                    "source": source, "applications": aura.applications,
                    "refreshes": aura.refreshes, "max_stacks": aura.max_stacks,
                    "uptime_observed_s": _perf_s(aura.observed_ms),
                    "uptime_upper_bound_s": _perf_s(aura.upper_ms),
                    "unknown_start_intervals": aura.unknown_start_intervals,
                    "intervals": intervals, "partial": aura.partial,
                    "covered_until_s": _perf_s(aura.covered_until_ms),
                    "holders_truncated": aura.holders_truncated})
            else:
                row = {"spell_id": spell_id, "name": aura.name,
                       "aura_type": aura.aura_type, "applications": aura.applications,
                       "refreshes": aura.refreshes, "removals": aura.removals,
                       "targets": len(aura.targets)}
                if aura.with_intervals:
                    row.update({"intervals": intervals, "partial": aura.partial,
                                "covered_until_s": _perf_s(aura.covered_until_ms),
                                "holders_truncated": aura.holders_truncated})
                from_player.append(row)
        on_player.sort(key=lambda row: (-row["uptime_upper_bound_s"],
                                        _perf_sort_id(row["spell_id"]), row["source"]))
        from_player.sort(key=lambda row: (-row["applications"],
                                          _perf_sort_id(row["spell_id"])))
        return {"coverage": {"complete": self.rejected_aura_keys == 0,
                             "rejected_keys": self.rejected_aura_keys,
                             "rejected_keys_is_lower_bound":
                                 self.rejected_keys_is_lower_bound,
                             "first_rejected_s": _perf_s(self.first_rejected_ms)},
                "on_player": on_player, "from_player": from_player}

    def _resource_section(self, spell_names: dict, rate_ms: int | None) -> dict:
        by_type = {}
        for power_type in sorted(self.power):
            stats = self.power[power_type]
            points = [(t_ms, current) for t_ms, kind, current, _ in self.resource_samples
                      if kind == power_type]
            reduced = len(points) > MAX_PERF_RESOURCE_POINTS
            if reduced:
                last = len(points) - 1
                step = MAX_PERF_RESOURCE_POINTS - 1
                points = [points[(index * last + step // 2) // step]
                          for index in range(MAX_PERF_RESOURCE_POINTS)]
            first, last_sample = stats["first"], stats["last"]
            by_type[str(power_type)] = {
                "samples": stats["samples"], "max_gap_s": _perf_s(stats["max_gap_ms"]),
                "first": {"t_s": _perf_s(first[0]), "current": first[1], "max": first[2]},
                "last": {"t_s": _perf_s(last_sample[0]), "current": last_sample[1],
                         "max": last_sample[2]},
                "min_observed": stats["min"], "max_observed": stats["max"],
                "series": [[_perf_s(t_ms), current] for t_ms, current in points],
                "partial": reduced or self.resource_samples_partial,
                "rates": {"samples_per_minute": _perf_rate(stats["samples"], rate_ms,
                                                           per_seconds=60)}}
        energize = []
        for (spell_id, power_type), row in self.energize.rows.items():
            energize.append({"spell_id": spell_id, "name": row["name"],
                             "power_type": power_type, "events": row["events"],
                             "amount": round(row["amount"], 3),
                             "over_energize": round(row["over_energize"], 3)})
            if spell_id is not None:
                spell_names.setdefault(spell_id, row["name"])
        energize.sort(key=lambda row: (-row["amount"], _perf_sort_id(row["spell_id"]),
                                       row["power_type"]))
        if self.energize.other is not None:
            other = self.energize.other
            energize.append({"spell_id": None, "name": PERF_OTHER, "power_type": None,
                             "events": other["events"], "amount": round(other["amount"], 3),
                             "over_energize": round(other["over_energize"], 3)})
        return {"by_power_type": by_type, "energize": energize}

    def _continuity_section(self) -> dict:
        if self.alive_at_start is None:
            return {"value": None, "reason": "life state unknown: no evidence of the "
                                             "player being alive or dead in the pull"}
        return {"threshold_s": ACTION_GAP_SECONDS, "gaps_count": self.gaps_count,
                "gaps_total_s": _perf_s(self.gaps_total_ms),
                "gap_max_s": _perf_s(self.gap_max_ms),
                "longest": [{"start_s": _perf_s(start), "end_s": _perf_s(end),
                             "duration_s": _perf_s(end - start)}
                            for start, end in self.longest_gaps],
                "basis": "observed gaps between consecutive player actions (cast start "
                         "or success, channel tick) within one alive period; cause not "
                         "determined"}

    def _timeline_sections(self) -> tuple[dict, dict]:
        covered = self.timeline_covered_until_ms
        timeline = {"entries": [list(entry) for entry in self.timeline],
                    "partial": covered is not None, "covered_until_s": _perf_s(covered)}
        limit = OPENER_SECONDS * 1000
        entries = [list(entry) for entry in self.timeline if entry[0] <= limit]
        signature = [entry[2] for entry in entries
                     if entry[1] == "success"][:MAX_PERF_SIGNATURE]
        opener_partial = covered is not None and covered <= limit
        opener = {"seconds": OPENER_SECONDS, "entries": entries, "signature": signature,
                  "partial": opener_partial,
                  "covered_until_s": _perf_s(covered) if opener_partial else None}
        return timeline, opener

    def fail(self, exc: BaseException, phase: str) -> None:
        """Remember the first failure; the accumulator is not used again."""
        if self.failure is None:
            self.failure = (exc, phase)

    def error_result(self, segment_metadata: dict, game_context: dict | None,
                     fingerprint: str | None,
                     segment_start_offset: int | None = None) -> dict:
        """performance.json of a pull whose analysis failed: identity, no metrics."""
        exc, phase = self.failure
        data = self._identity(segment_metadata, game_context, fingerprint,
                              segment_start_offset)
        data["rules"]["spec"] = None
        data["player"] = {"selector": self.selector, "status": "error", "guid": None,
                          "name": None, "candidates": []}
        data["error"] = {"type": type(exc).__name__, "message": str(exc)[:300],
                         "phase": phase}
        return data

    def _identity(self, segment_metadata: dict, game_context: dict | None,
                  fingerprint: str | None, segment_start_offset: int | None) -> dict:
        game = dict(game_context or parse_log_header([]))
        game.setdefault("header_source", "unknown")
        complete = bool(segment_metadata.get("complete")) and self._ended
        success = segment_metadata.get("success")
        duration_ms = segment_metadata.get("duration_ms")
        return {
            "performance_schema_version": PERFORMANCE_SCHEMA_VERSION,
            "extractor_version": APP_VERSION,
            "fingerprint": fingerprint,
            "rules": {"general_version": PERFORMANCE_RULES_VERSION, "spec": None},
            "segment": {
                "segment_id": segment_metadata.get("segment_id"),
                "encounter_id": segment_metadata.get("encounter_id"),
                "boss": segment_metadata.get("boss"),
                "difficulty_id": segment_metadata.get("difficulty_id"),
                "difficulty": segment_metadata.get("difficulty"),
                "raid_size": segment_metadata.get("raid_size"),
                "start_time": segment_metadata.get("start_time"),
                "end_time": segment_metadata.get("end_time"),
                "duration_ms": duration_ms if complete else None,
                "complete": complete,
                "result": ("kill" if success else "wipe")
                if complete and success is not None else "incomplete",
                "observed_seconds": _perf_s(self.end_ms),
                "duration_basis": "encounter_end" if self._ended else "observation_end"},
            "source": {"file": segment_metadata.get("source_file"),
                       "segment_start_offset": segment_start_offset,
                       "encounter_start_offset": self.encounter_start_offset,
                       "encounter_end_offset": self.encounter_end_offset,
                       "observation_end_offset": self.last_offset},
            "game": {key: game.get(key) for key in (
                "combat_log_version", "advanced_logging", "build_version", "project_id",
                "header_source")},
            "player": {"selector": self.selector, "status": self.status,
                       "guid": self.player_guid if self.status == "resolved" else None,
                       "name": self.player_name if self.status == "resolved" else None,
                       "candidates": [{"guid": guid, "name": name}
                                      for guid, name in self.matches.items()]},
        }

    def result(self, segment_metadata: dict, game_context: dict | None,
               fingerprint: str | None, segment_start_offset: int | None = None) -> dict:
        """The performance.json v1 document (the spec section comes from SPEC_RULES)."""
        self._finish()
        data = self._identity(segment_metadata, game_context, fingerprint,
                              segment_start_offset)
        if self.status != "resolved":
            return data
        game = data["game"]
        complete = data["segment"]["complete"]
        duration_ms = segment_metadata.get("duration_ms")
        if not complete:
            duration_reason = "incomplete pull: no ENCOUNTER_END"
            rate_ms = self.end_ms
        elif not duration_ms or duration_ms <= 0:
            duration_reason, rate_ms = "encounter duration missing or zero", None
        else:
            duration_reason, rate_ms = None, duration_ms
        alive_ms, dead_ms = self._life_seconds()
        spell_names: dict = {}
        timeline, opener = self._timeline_sections()
        data.update({
            "character": self._character(),
            "life": {"alive_at_start": "unknown" if self.alive_at_start is None
                     else self.alive_at_start,
                     "basis": self.alive_basis,
                     "deaths": [{"t_s": _perf_s(t)} for t in self.deaths_ms],
                     "resurrections": [{"t_s": _perf_s(t)} for t in self.resurrections_ms],
                     "death_in_post_context": [{"after_end_s": _perf_s(t)}
                                               for t in self.post_deaths_ms],
                     "alive_seconds": _perf_s(alive_ms), "dead_seconds": _perf_s(dead_ms)},
            "damage": self._damage_section(spell_names, duration_ms if complete else None,
                                           duration_reason, alive_ms),
            "casts": self._casts_section(spell_names, rate_ms),
            "auras": self._aura_section(spell_names),
            "resources": self._resource_section(spell_names, rate_ms),
            "continuity": self._continuity_section(),
            "timeline": timeline,
            "opener": opener,
        })
        if self.alive_at_start is None:
            data["life"]["reason"] = "no evidence of the player being alive or dead"
        data["spell_names"] = {str(key): spell_names[key]
                               for key in sorted(spell_names, key=_perf_sort_id)}
        data["spec"] = self._spec_section(game.get("build_version"), data)
        data["warnings"] = [dict(warning) for warning in self.warnings.values()]
        return data

    def _spec_section(self, build_version: str | None, data: dict) -> dict:
        if self.spec_id is None:
            return {"status": "not_applied", "reason": "spec unknown"}
        rules = self.spec_rules
        builder = rules.get("build") if rules else None
        if builder is None:
            return {"status": "not_applied",
                    "reason": "no rules registered for spec %s" % self.spec_id}
        data["rules"]["spec"] = {
            "id": rules.get("id"), "version": rules.get("version"),
            "validated_for_build": build_version in tuple(rules.get("validated_builds", ()))}
        return builder(self, rules, build_version)


# --- spec rules: Arcane Mage (spec 62) --------------------------------------------------
# Spell ids observed on a 12.1.0 log. Rules are selected by spec id and match spell
# ids only, never names (the client is localised; names are observed labels).
ARCANE_SPEC_ID = 62
ARCANE_SURGE_CAST = 365350          # CAST_START then CAST_SUCCESS
ARCANE_SURGE_BUFF = 365362          # self buff: the burst window
ARCANE_TOUCH_CAST = 321507
ARCANE_TOUCH_DEBUFF = 210824        # debuff on the target: the Touch window
ARCANE_MISSILES_CAST = 5143         # one CAST_SUCCESS at channel start
ARCANE_MISSILES_TICK = 7268         # SPELL_DAMAGE ~0.15 s apart while channelling
ARCANE_BARRAGE_CAST = 44425         # Arcane Charge spender
ARCANE_CLEARCASTING = 263725        # self buff with stacks
ARCANE_SELF_AURAS = (ARCANE_SURGE_BUFF, ARCANE_CLEARCASTING, 451038, 1223797, 1295942)
ARCANE_CHARGES_POWER = 16           # only ever seen as SPELL_ENERGIZE gains
ARCANE_MANA_POWER = 0
ARCANE_SAMPLE_MS = 2000             # a mana sample must be this close to a boundary
ARCANE_TOUCH_PAIR_MS = 10000        # Touch application paired with a burst window
_ARCANE_REMOVALS = {"SPELL_AURA_REMOVED", "SPELL_AURA_BROKEN", "SPELL_AURA_BROKEN_SPELL"}

ARCANE_LIMITATIONS = (
    "Theoretical cooldown availability and 'missed' uses of Arcane Surge, Touch of the "
    "Magi or Arcane Orb are not evaluated: that needs talents, charges and cooldown "
    "resets modelled.",
    "Clearcasting expiry cannot be told apart from consumption: a stack decrement is "
    "only paired with Arcane Missiles when a Missiles CAST_SUCCESS has the same "
    "timestamp; the rest are reported as unexplained.",
    "Arcane Charges are never logged as a resource: the count is inferred from "
    "SPELL_ENERGIZE gains and Arcane Barrage casts, and checked against the log.",
    "Boss and add damage are not separated: no log event identifies the boss.",
    "Arcane Missiles channel clipping is not evaluated: ticks are counted as actions, "
    "the intended channel length is not known.",
    "Window membership follows log line order: a cast logged before the buff "
    "application at the same timestamp (e.g. the Arcane Surge cast itself) is outside "
    "its window.",
    "Touch windows match damage by target key (NPC id): several instances of the same "
    "NPC are not told apart.",
)

ARCANE_DEFINITIONS = {
    "casts": "CAST_SUCCESS count by spell id inside the window; past %d distinct spells "
             "the rest are summed under 'other' (casts_partial: true), so the window "
             "total stays exact" % MAX_PERF_WINDOW_SPELLS,
    "damage_effective": "player (not pet) amount - overkill on hostile targets inside "
                        "the window",
    "mana": "nearest player power-type-0 sample within %d s of the boundary (ties: the "
            "earlier one); sample_age_s = boundary - sample time, negative when the "
            "sample follows the boundary" % (ARCANE_SAMPLE_MS // 1000),
    "touch": "nearest Touch of the Magi debuff application by the player within "
             "+-%d s of the window start; offset_from_start_s = applied - start"
             % (ARCANE_TOUCH_PAIR_MS // 1000),
    "partial_windows": "burst buff intervals whose start is not established; no "
                       "entry-state statistics",
}

ARCANE_CHARGE_MODEL = (
    "Counter unknown until the first Arcane Barrage CAST_SUCCESS (resets it to 0) or an "
    "energize with over_energize > 0 (implies the maximum). Each energize adds its "
    "amount, capped at max_power. Every energize while the counter is known is a check: "
    "at max with amount 0 confirms; at max with amount > 0 contradicts; below max with "
    "over_energize > 0 and amount smaller than the room left contradicts; anything else "
    "is consistent. After a contradiction the counter takes the value the event implies: "
    "max_power when over_energize > 0, otherwise unknown. A death or resurrection makes "
    "it unknown.")


def _arcane_list(code: str) -> dict:
    return {"code": code, "count": 0, "items": [], "covered_until_ms": None}


def _arcane_tracker(key: tuple, established: dict, unknown: dict) -> dict:
    return {"key": key, "open": None, "unknown_seen": 0,
            "established": established, "unknown": unknown}


def _arcane_new_state() -> dict:
    touch_list = _arcane_list("perf_touch_windows_truncated")
    return {
        "surge": _arcane_tracker(("on_player", ARCANE_SURGE_BUFF, "self"),
                                 _arcane_list("perf_burst_windows_truncated"),
                                 _arcane_list("perf_partial_windows_truncated")),
        "touch": _arcane_tracker(("from_player", ARCANE_TOUCH_DEBUFF, "self"),
                                 touch_list, touch_list),
        "last_touch_ms": None,
        "opener": {"surge_ms": None, "touch_ms": None, "surge_precast": None,
                   "surge_failed": False},
        "cc": {"stacks": None, "applications": 0, "refreshes": 0, "refresh_stacks": {},
               "increments": 0, "decrements": 0, "max": None, "bucket_t": None,
               "bucket_dec": 0, "bucket_missiles": 0, "matched": 0},
        "charges": {"counter": None, "checks": 0, "confirmed": 0, "contradicted": 0,
                    "barrage": {}},
    }


def _arcane_int(value):
    return int(value) if isinstance(value, float) and value.is_integer() else value


def _arcane_begin(acc, rules: dict, t_ms: int) -> None:
    """SPEC_RULES "begin" hook: adopt the aura state established before the first
    player event of the encounter (acc has not applied that event yet)."""
    state = acc.spec_state
    state.update(_arcane_new_state())
    aura = acc.auras.get(("on_player", ARCANE_CLEARCASTING, "self"))
    if aura is not None:
        # Inactive after pre-context evidence = 0; active with logged stacks = them.
        state["cc"]["stacks"] = (aura.stacks or None) if aura.active else 0
        _arcane_cc_max(state["cc"])
    for name in ("surge", "touch"):
        tracker = state[name]
        aura = acc.auras.get(tracker["key"])
        if aura is not None and aura.active:
            holder = next(iter(sorted(aura.holders)), None)
            _arcane_open(acc, state, tracker, aura.start_ms, aura.start_basis, t_ms,
                         holder)
        tracker["unknown_seen"] = aura.unknown_start_intervals if aura is not None else 0


def _arcane_open(acc, state: dict, tracker: dict, start_ms, start_basis: str,
                 t_ms: int, holder: str | None) -> dict:
    established = start_basis != "unknown"
    window = {"start_ms": start_ms if established else None, "start_basis": start_basis,
              "first_evidence_ms": t_ms, "end_ms": None, "end_basis": None,
              "casts": {}, "casts_partial": False, "damage": 0, "death": False,
              "cc": state["cc"]["stacks"],
              "target_key": _perf_unit(holder)[0] if holder else None,
              "touch_before_ms": None, "touch_after_ms": None, "aggregate": False}
    last_touch = state["last_touch_ms"]
    if established and last_touch is not None and \
            start_ms - last_touch <= ARCANE_TOUCH_PAIR_MS:
        window["touch_before_ms"] = last_touch
    target = tracker["established" if established else "unknown"]
    target["count"] += 1
    if len(target["items"]) < MAX_PERF_WINDOWS:
        target["items"].append(window)
        window["aggregate"] = established
    else:
        if target["covered_until_ms"] is None:
            target["covered_until_ms"] = start_ms if established else t_ms
        acc.warn(target["code"], MAX_PERF_WINDOWS)
    tracker["open"] = window
    return window


def _arcane_aura(acc, state: dict, tracker: dict, record: dict) -> None:
    """Follow the window of one tracked aura key from acc's live aura state."""
    aura = acc.auras.get(tracker["key"])
    if aura is None:
        return
    t_ms = record["t_ms"]
    window = tracker["open"]
    if window is not None:
        if not aura.active:
            window["end_ms"], window["end_basis"] = aura.last_inactive_ms, "observed"
            tracker["open"] = None
    elif aura.active:
        _arcane_open(acc, state, tracker, aura.start_ms, aura.start_basis, t_ms,
                     record["holder"])
    elif record["event"] in _ARCANE_REMOVALS:
        if aura.unknown_start_intervals > tracker["unknown_seen"]:
            # Removed without a seen application: opened and closed by this event.
            window = _arcane_open(acc, state, tracker, None, "unknown", t_ms,
                                  record["holder"])
            window["end_ms"], window["end_basis"] = aura.last_inactive_ms, "observed"
            tracker["open"] = None
    tracker["unknown_seen"] = aura.unknown_start_intervals


def _arcane_window_cast(acc, window: dict, spell_id) -> None:
    """Count a successful cast in a window; past MAX_PERF_WINDOW_SPELLS distinct
    spells it goes to `other`, so the window's total stays exact."""
    casts = window["casts"]
    if spell_id not in casts and len(casts) >= MAX_PERF_WINDOW_SPELLS:
        spell_id = "other"
        window["casts_partial"] = True
        acc.warn("perf_window_spells_truncated", MAX_PERF_WINDOW_SPELLS)
    casts[spell_id] = casts.get(spell_id, 0) + 1


def _arcane_cc_max(cc: dict) -> None:
    if cc["stacks"]:
        cc["max"] = max(cc["max"] or 0, cc["stacks"])


def _arcane_bucket(cc: dict, t_ms: int, decrements: int = 0, missiles: int = 0) -> None:
    """Pair Clearcasting decrements with Missiles casts one-to-one per timestamp."""
    if cc["bucket_t"] != t_ms:
        cc["matched"] += min(cc["bucket_dec"], cc["bucket_missiles"])
        cc["bucket_t"], cc["bucket_dec"], cc["bucket_missiles"] = t_ms, 0, 0
    cc["bucket_dec"] += decrements
    cc["bucket_missiles"] += missiles


def _arcane_clearcasting(cc: dict, record: dict) -> None:
    event, stacks = record["event"], record["stacks"]
    if event == "SPELL_AURA_APPLIED":
        cc["applications"] += 1
        cc["stacks"] = stacks or None
    elif event == "SPELL_AURA_APPLIED_DOSE":
        cc["increments"] += 1
        cc["stacks"] = stacks or None
    elif event == "SPELL_AURA_REMOVED_DOSE":
        cc["decrements"] += 1
        _arcane_bucket(cc, record["t_ms"], decrements=1)
        cc["stacks"] = stacks
    elif event == "SPELL_AURA_REFRESH":
        cc["refreshes"] += 1
        key = cc["stacks"]
        cc["refresh_stacks"][key] = cc["refresh_stacks"].get(key, 0) + 1
    elif event in _ARCANE_REMOVALS:
        # The aura was present, so unless it is known to be at 0 this drops >= 1 stack.
        if cc["stacks"] != 0:
            cc["decrements"] += 1
            _arcane_bucket(cc, record["t_ms"], decrements=1)
        cc["stacks"] = 0
    _arcane_cc_max(cc)


def _arcane_energize(charges: dict, record: dict) -> None:
    amount = _arcane_int(record["amount"] or 0)
    over = _arcane_int(record["over_energize"] or 0)
    maximum = record.get("max_power")
    if not maximum or maximum <= 0:
        charges["counter"] = None
        return
    counter = charges["counter"]
    if counter is None:
        if over > 0:
            counter = maximum
    else:
        charges["checks"] += 1
        if counter >= maximum:
            contradicted = amount > 0
        else:
            contradicted = over > 0 and amount < maximum - counter
        if contradicted:
            charges["contradicted"] += 1
            counter = maximum if over > 0 else None
        else:
            if counter >= maximum:
                charges["confirmed"] += 1
            counter = min(maximum, counter + amount)
    charges["counter"] = _arcane_int(counter)


def _arcane_observe(acc, rules: dict, record: dict) -> None:
    """Streaming aggregates of the Arcane spec section (SPEC_RULES hook)."""
    state = acc.spec_state
    kind, t_ms = record["kind"], record["t_ms"]
    surge, touch, cc = state["surge"], state["touch"], state["cc"]
    if kind == "aura":
        key = (record["scope"], record["spell_id"], record["source"])
        if key == surge["key"]:
            _arcane_aura(acc, state, surge, record)
        elif key == touch["key"]:
            _arcane_aura(acc, state, touch, record)
            if record["event"] == "SPELL_AURA_APPLIED":
                state["last_touch_ms"] = t_ms
                # Pair with the burst windows that started at most 10 s earlier.
                for window in reversed(surge["established"]["items"]):
                    if window["start_ms"] < t_ms - ARCANE_TOUCH_PAIR_MS:
                        break
                    if window["touch_after_ms"] is None and window["start_ms"] <= t_ms:
                        window["touch_after_ms"] = t_ms
        elif key == ("on_player", ARCANE_CLEARCASTING, "self"):
            _arcane_clearcasting(cc, record)
        return
    windows = [tracker["open"] for tracker in (surge, touch)
               if tracker["open"] is not None and tracker["open"]["aggregate"]]
    if kind == "cast":
        spell_id = record["spell_id"]
        opener = state["opener"]
        if record["cast"] != "success":
            if record["cast"] == "failed" and spell_id == ARCANE_SURGE_CAST:
                opener["surge_failed"] = True
            return
        for window in windows:
            _arcane_window_cast(acc, window, spell_id)
        if spell_id == ARCANE_SURGE_CAST and opener["surge_ms"] is None:
            opener["surge_ms"] = t_ms
            row = acc.casts.rows.get(ARCANE_SURGE_CAST)
            # started_before_pull counts outcomes of a pre-pull START; a FAILED
            # before this SUCCESS consumed it.
            opener["surge_precast"] = None if row is None else \
                row["started_before_pull"] > 0 and not opener["surge_failed"]
        elif spell_id == ARCANE_TOUCH_CAST and opener["touch_ms"] is None:
            opener["touch_ms"] = t_ms
        if spell_id == ARCANE_MISSILES_CAST:
            _arcane_bucket(cc, t_ms, missiles=1)
        elif spell_id == ARCANE_BARRAGE_CAST:
            charges = state["charges"]
            counter = charges["counter"]
            key = "unknown" if counter is None else str(counter)
            charges["barrage"][key] = charges["barrage"].get(key, 0) + 1
            charges["counter"] = 0
    elif kind == "damage":
        if record["pet"]:
            return
        for window in windows:
            if window is surge["open"] or window["target_key"] == record["target_key"]:
                window["damage"] += record["effective"]
    elif kind in ("death", "resurrection"):
        if kind == "death" and surge["open"] is not None:
            surge["open"]["death"] = True
        state["charges"]["counter"] = None
    elif kind == "energize" and record["power_type"] == ARCANE_CHARGES_POWER:
        _arcane_energize(state["charges"], record)


def _arcane_mana(acc, t_ms: int) -> dict | None:
    samples = acc.resource_samples
    if acc.resource_samples_partial and \
            (not samples or t_ms + ARCANE_SAMPLE_MS > samples[-1][0]):
        return None    # a nearer sample may be among the dropped ones
    best = None
    for sample_ms, power_type, current, maximum in samples:
        distance = abs(sample_ms - t_ms)
        if power_type == ARCANE_MANA_POWER and distance <= ARCANE_SAMPLE_MS and \
                (best is None or distance < best[0]):
            best = (distance, sample_ms, current, maximum)
    if best is None:
        return None
    return {"current": best[2], "max": best[3], "sample_age_s": _perf_s(t_ms - best[1])}


def _arcane_casts(casts: dict) -> dict:
    return {str(spell_id): count for spell_id, count in
            sorted(casts.items(), key=lambda item: (item[0] == "other", -item[1],
                                                    _perf_sort_id(item[0])))}


def _arcane_section(acc, rules: dict, build_version: str | None) -> dict:
    """The published Arcane `spec` section (SPEC_RULES build hook; no state change)."""
    state = acc.spec_state or _arcane_new_state()
    end_basis = "encounter_end" if acc._ended else "observation_end"

    def bounds(window):
        if window["end_ms"] is None:
            return max(acc.end_ms or 0, window["start_ms"] or 0), end_basis
        return window["end_ms"], window["end_basis"]

    def listing(target, rows):
        covered = target["covered_until_ms"]
        return {"count": target["count"], "partial": covered is not None,
                "covered_until_s": _perf_s(covered), "windows": rows}

    def casts_partial(target):
        # Per-spell cast detail of a retained window was cut (its total is exact).
        return any(window["casts_partial"] for window in target["items"]
                   if window["aggregate"])

    surge, touch = state["surge"], state["touch"]
    burst = []
    for window in surge["established"]["items"]:
        end_ms, basis = bounds(window)
        start_ms = window["start_ms"]
        pair = None
        for applied in (window["touch_before_ms"], window["touch_after_ms"]):
            if applied is not None and \
                    (pair is None or abs(applied - start_ms) < abs(pair - start_ms)):
                pair = applied
        burst.append({
            "start_s": _perf_s(start_ms), "end_s": _perf_s(end_ms),
            "duration_s": _perf_s(end_ms - start_ms),
            "start_basis": window["start_basis"], "end_basis": basis,
            "casts": _arcane_casts(window["casts"]),
            "casts_partial": window["casts_partial"],
            "damage_effective": window["damage"],
            "mana_at_start": _arcane_mana(acc, start_ms),
            "mana_at_end": _arcane_mana(acc, end_ms),
            "clearcasting_stacks_at_start": window["cc"],
            "touch": None if pair is None else
            {"applied_s": _perf_s(pair), "offset_from_start_s": _perf_s(pair - start_ms)},
            "death_inside": window["death"]})
    partial = []
    for window in surge["unknown"]["items"]:
        end_ms, basis = bounds(window)
        partial.append({"start_s": None, "start_basis": "unknown",
                        "first_evidence_s": _perf_s(window["first_evidence_ms"]),
                        "end_s": _perf_s(end_ms), "end_basis": basis})
    touches = []
    for window in touch["established"]["items"]:
        end_ms, basis = bounds(window)
        aggregate = window["aggregate"]
        touches.append({
            "start_s": _perf_s(window["start_ms"]), "start_basis": window["start_basis"],
            "end_s": _perf_s(end_ms), "end_basis": basis,
            "target_key": window["target_key"],
            "casts": _arcane_casts(window["casts"]) if aggregate else None,
            "casts_partial": window["casts_partial"] if aggregate else None,
            "damage_effective_to_target": window["damage"] if aggregate else None})
    opener = state["opener"]
    surge_ms, touch_ms = opener["surge_ms"], opener["touch_ms"]
    if surge_ms is None or touch_ms is None:
        order = None
    else:
        order = "surge_first" if surge_ms < touch_ms else \
            "touch_first" if touch_ms < surge_ms else "same_timestamp"
    cc = state["cc"]
    matched = cc["matched"] + min(cc["bucket_dec"], cc["bucket_missiles"])
    refresh_stacks = cc["refresh_stacks"]
    observed = []
    for (spell_id, power_type), row in acc.energize.rows.items():
        if power_type == ARCANE_CHARGES_POWER:
            observed.append({"spell_id": spell_id, "events": row["events"],
                             "gains": _arcane_int(row["amount"]),
                             "over_energize": _arcane_int(row["over_energize"])})
    observed.sort(key=lambda row: (-row["gains"], _perf_sort_id(row["spell_id"])))
    charges = state["charges"]
    checks = charges["checks"]
    histogram = {str(count): 0 for count in range(5)}
    histogram.update(charges["barrage"])
    histogram["unknown"] = histogram.pop("unknown", 0)
    return {
        "status": "applied", "id": rules.get("id"), "version": rules.get("version"),
        "rules_validated_for_build":
            build_version in tuple(rules.get("validated_builds", ())),
        "definitions": dict(ARCANE_DEFINITIONS),
        "opener": {"surge_first_success_s": _perf_s(surge_ms),
                   "touch_first_success_s": _perf_s(touch_ms), "order": order,
                   "surge_precast": opener["surge_precast"]},
        "burst_windows": {"aura_spell_id": ARCANE_SURGE_BUFF,
                          **listing(surge["established"], burst),
                          "casts_partial": casts_partial(surge["established"])},
        "partial_windows": {"aura_spell_id": ARCANE_SURGE_BUFF,
                            **listing(surge["unknown"], partial)},
        "touch_windows": {"aura_spell_id": ARCANE_TOUCH_DEBUFF,
                          **listing(touch["established"], touches),
                          "casts_partial": casts_partial(touch["established"])},
        "procs": [{
            "spell_id": ARCANE_CLEARCASTING, "applications": cc["applications"],
            "refreshes": cc["refreshes"],
            "refreshes_at_max_stacks": refresh_stacks.get(cc["max"], 0)
            if cc["max"] is not None else 0,
            "refreshes_with_unknown_stacks": refresh_stacks.get(None, 0),
            "stack_increments": cc["increments"], "decrements": cc["decrements"],
            "decrements_with_missiles_cast_same_timestamp": matched,
            "decrements_unexplained": cc["decrements"] - matched,
            "max_stacks_observed": cc["max"]}],
        "charges": {
            "power_type": ARCANE_CHARGES_POWER,
            "observed": {"kind": "observed", "by_spell": observed,
                         "partial": acc.energize.other is not None},
            "inferred": {
                "kind": "inferred", "model": ARCANE_CHARGE_MODEL,
                "checks": checks, "confirmed": charges["confirmed"],
                "contradicted": charges["contradicted"],
                "agreement_rate": {"value": round((checks - charges["contradicted"])
                                                  / checks, 3),
                                   "numerator": checks - charges["contradicted"],
                                   "denominator": checks} if checks else
                {"value": None, "reason": "no energize while the counter was known"},
                "barrage_casts_by_inferred_charges": histogram}},
        "limitations": list(ARCANE_LIMITATIONS),
    }


ARCANE_RULES = {
    "id": "mage-arcane",
    "version": 1,
    "validated_builds": ("12.1.0",),
    "tracked_self_auras": ARCANE_SELF_AURAS,
    "tracked_target_auras": (ARCANE_TOUCH_DEBUFF,),
    "channel_tick_spells": (ARCANE_MISSILES_TICK,),
    "begin": _arcane_begin,
    "observe": _arcane_observe,
    "build": _arcane_section,
}
SPEC_RULES[ARCANE_SPEC_ID] = ARCANE_RULES


# --- segments ---------------------------------------------------------------------

class Segment:
    """One in-progress extraction (a M+ run or a raid pull)."""

    def __init__(self, kind: str, start_ts: datetime, source_file: str, segment_id: str,
                 output_options: OutputOptions | None = None):
        self.kind = kind
        self.start_ts = start_ts
        self.source_file = source_file
        self.segment_id = segment_id
        self.output_options = output_options or OutputOptions()
        self.partial_path: str | None = None
        self.stage_dir: str | None = None
        self.analysis_session: AnalysisSession | None = None
        self.performance: PerformanceAccumulator | None = None
        # Game header in force when the tracker opened the segment, plus its source.
        self.game_context: dict | None = None
        self.start_offset = 0
        self.lines = 0
        self.raw_bytes = 0
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

    def begin_body(self, partial_path: str | None, stage_dir: str | None = None) -> None:
        """Open the .partial body file. Called once the START args are parsed."""
        self.partial_path = partial_path
        self.stage_dir = stage_dir
        if partial_path is not None:
            self._handle = open(partial_path, "wb")
        if self.output_options.wants_analysis:
            if stage_dir is None:
                raise RuntimeError("analysis staging directory was not created")
            if self.output_options.performance_player is not None and \
                    self.kind == KIND_RAID:
                self.performance = PerformanceAccumulator(
                    self.output_options.performance_player, self.encounter_id,
                    self.start_ts)
            self.analysis_session = AnalysisSession(
                stage_dir, self.kind,
                keep_player_damage=self.output_options.keep_player_damage,
                performance=self.performance)

    def write(self, raw: bytes, timestamp: datetime | None = None,
              event: str | None = None, args: list[str] | None = None) -> None:
        if self._handle is not None:
            self._handle.write(raw)
        if self.analysis_session is not None:
            # Lines are contiguous from start_offset: this is the line's own offset.
            self.analysis_session.consume(raw, timestamp, event, args or [],
                                          self.start_offset + self.raw_bytes)
        self.lines += 1
        self.raw_bytes += len(raw)

    def performance_result(self) -> dict | None:
        """performance.json content of a raid pull, or None without the flag."""
        accumulator = self.performance
        if accumulator is None:
            return None
        arguments = (self.metadata(), self.game_context,
                     self.output_options.performance_fingerprint, self.start_offset)
        if accumulator.failure is None:
            # An optional analysis never breaks the extraction: a failing builder
            # gives a performance.json with status "error" instead.
            try:
                return accumulator.result(*arguments)
            except Exception as exc:
                accumulator.fail(exc, "result")
        return accumulator.error_result(*arguments)

    def close(self) -> None:
        if self._handle is not None and not self._handle.closed:
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._handle.close()
        if self.analysis_session is not None:
            self.analysis_session.close_streams()

    def abandon(self) -> None:
        self.close()
        if self.partial_path:
            try:
                os.remove(self.partial_path)
            except OSError:
                pass
        if self.stage_dir:
            shutil.rmtree(self.stage_dir, ignore_errors=True)

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

    def __init__(self, output_dir: str, verbose: bool = True,
                 output_options: OutputOptions | None = None):
        self.output_dir = os.path.abspath(output_dir)
        self.mplus_dir = os.path.join(self.output_dir, MPLUS_DIR_NAME)
        self.raids_dir = os.path.join(self.output_dir, RAID_DIR_NAME)
        self.verbose = verbose
        self.options = output_options or OutputOptions()

    def ensure_dirs(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)

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
                if entry == ".staging":
                    staging = os.path.join(directory, entry)
                    try:
                        for child in os.listdir(staging):
                            shutil.rmtree(os.path.join(staging, child))
                            removed += 1
                    except OSError:
                        pass
                    continue
                candidate_tree = os.path.join(directory, entry)
                analysis_tree = os.path.join(candidate_tree, "analysis")
                if os.path.isdir(analysis_tree):
                    try:
                        for child in os.listdir(analysis_tree):
                            if child.endswith(".tmp"):
                                os.remove(os.path.join(analysis_tree, child))
                                removed += 1
                        if not os.listdir(analysis_tree):
                            os.rmdir(analysis_tree)
                            os.rmdir(candidate_tree)
                            removed += 1
                            continue
                    except OSError:
                        pass
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
        if not self.options.wants_full:
            return ""
        if not self.options.is_legacy_default:
            return os.path.join(self.stage_dir(kind, segment_id), "full.raw")
        suffix = _sha1(segment_id.encode("utf-8"))[:8]
        return os.path.join(self.directory_for(kind),
                            "%s.%s.txt.partial" % (core_name, suffix))

    def stage_dir(self, kind: str, segment_id: str) -> str:
        path = os.path.join(self.directory_for(kind), ".staging",
                            _sha1(segment_id.encode("utf-8")))
        os.makedirs(path, exist_ok=True)
        return path

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

    def _name_segment_id(self, directory: str, name: str) -> str | None:
        root_id = self._existing_segment_id(os.path.join(directory, name + ".json"))
        if root_id:
            return root_id
        marker = os.path.join(directory, name, "analysis", "metadata.json")
        marker_id = self._existing_segment_id(marker)
        if marker_id or os.path.exists(marker):
            return marker_id
        # Before the global marker exists, summary is the recovery identity for a
        # partially published analysis-only attempt. An unreadable marker is never
        # bypassed through this fallback, so unknown existing data is not purged.
        return self._existing_segment_id(
            os.path.join(directory, name, "analysis", "summary.json"))

    def resolve_name(self, segment: Segment) -> str:
        directory = self.directory_for(segment.kind)
        for candidate in self._candidate_names(segment):
            txt_path = os.path.join(directory, candidate + ".txt")
            gzip_path = txt_path + ".gz"
            json_path = os.path.join(directory, candidate + ".json")
            analysis_path = os.path.join(directory, candidate)
            zip_path = os.path.join(directory, candidate + "_analysis.zip")
            has_txt = os.path.exists(txt_path) or os.path.exists(gzip_path)
            has_json = os.path.exists(json_path)
            has_analysis = os.path.exists(analysis_path) or os.path.exists(zip_path)
            if not has_txt and not has_json and not has_analysis:
                return candidate
            existing = self._name_segment_id(directory, candidate)
            if existing == segment.segment_id:
                return candidate  # same entity, idempotent/profile extension
            if has_json and not has_txt and not has_analysis:
                return candidate  # root json without body = reclaimable crash orphan
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
        names = set()
        for entry in entries:
            if entry.endswith(".json"):
                names.add(entry[:-5])
            elif os.path.isdir(os.path.join(directory, entry)) and entry != ".staging":
                names.add(entry)
        for name in sorted(names):
            if name == keep_name:
                continue
            if self._name_segment_id(directory, name) != segment_id:
                continue
            stale_files = []
            if self.options.wants_full:
                stale_files.extend((name + ".txt", name + ".txt.gz", name + ".json"))
            if self.options.wants_analysis:
                stale_files.append(name + "_analysis.zip")
            for stale in stale_files:
                try:
                    os.remove(os.path.join(directory, stale))
                except FileNotFoundError:
                    pass
            analysis_tree = os.path.join(directory, name)
            if self.options.wants_analysis and os.path.isdir(analysis_tree):
                shutil.rmtree(analysis_tree)

    @staticmethod
    def _write_stage(path: str, data: bytes) -> None:
        with open(path, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _deterministic_zip(path: str, files: list[tuple[str, str | bytes]]) -> None:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=9) as archive:
            for arcname, source in sorted(files):
                info = zipfile.ZipInfo(arcname, (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                if isinstance(source, bytes):
                    archive.writestr(info, source, compress_type=zipfile.ZIP_DEFLATED,
                                     compresslevel=9)
                else:
                    with open(source, "rb") as origin, archive.open(
                            info, "w", force_zip64=True) as target:
                        shutil.copyfileobj(origin, target, READ_BLOCK)
        with open(path, "ab") as handle:
            handle.flush()
            os.fsync(handle.fileno())

    def _analysis_payload(self, segment: Segment, name: str,
                          full_stored_bytes: int | None) -> tuple[dict, dict[str, str]]:
        session = segment.analysis_session
        stage_dir = segment.stage_dir
        if session is None or stage_dir is None:
            raise RuntimeError("analysis session missing at publication")
        segment_metadata = segment.metadata()
        summary, players = session.summary_and_players(segment_metadata)
        paths = {
            "summary.json": os.path.join(stage_dir, "summary.json"),
            "players.json": os.path.join(stage_dir, "players.json"),
            "deaths.json": os.path.join(stage_dir, "deaths.json"),
        }
        for filename, value in (("summary.json", summary), ("players.json", players)):
            self._write_stage(paths[filename], _json_bytes(value))
        session.write_deaths_json(paths["deaths.json"], segment_metadata)
        combat_name = "combat.txt.gz" if self.options.gzip else "combat.txt"
        combat_path = os.path.join(stage_dir, combat_name)
        if self.options.gzip:
            _deterministic_gzip(session.combat_raw_path, combat_path)
        else:
            shutil.copyfile(session.combat_raw_path, combat_path)
        paths[combat_name] = combat_path
        # Raid pulls under --performance-player only; otherwise the package is unchanged.
        performance = segment.performance_result()
        if performance is not None:
            try:
                performance_bytes = performance_json_bytes(performance)
            except ValueError as exc:     # e.g. a NaN from a builder
                segment.performance.fail(exc, "result")
                performance = segment.performance_result()
                performance_bytes = performance_json_bytes(performance)
            paths["performance.json"] = os.path.join(stage_dir, "performance.json")
            self._write_stage(paths["performance.json"], performance_bytes)
        combat_stored = os.path.getsize(combat_path)
        bundle_bytes = combat_stored + sum(os.path.getsize(paths[item]) for item in
                                           ("summary.json", "deaths.json", "players.json"))
        reduction = None
        if segment.raw_bytes:
            reduction = round(100.0 * (segment.raw_bytes - session.combat_bytes) /
                              segment.raw_bytes, 2)
        artifacts = ([name + ".json",
                      name + (".txt.gz" if self.options.gzip else ".txt")]
                     if self.options.wants_full else [])
        artifacts.extend([os.path.join(name, "analysis", item).replace("\\", "/")
                          for item in sorted(paths)])
        artifacts.append(os.path.join(name, "analysis", "metadata.json").replace("\\", "/"))
        if self.options.bundle:
            artifacts.append(name + "_analysis.zip")
        metadata = {
            "analysis_schema_version": ANALYSIS_SCHEMA_VERSION,
            "segment_id": segment.segment_id,
            "profile": self.options.profile,
            "options": self.options.as_dict(),
            "artifacts": artifacts,
            "warnings": list(session.warnings.values()),
            "full_uncompressed_bytes": segment.raw_bytes,
            "full_stored_bytes": full_stored_bytes,
            "combat_uncompressed_bytes": session.combat_bytes,
            "combat_stored_bytes": combat_stored,
            "analysis_bundle_bytes": bundle_bytes,
            "analysis_zip_bytes": None,
            "reduction_percent": reduction,
        }
        if performance is not None:
            player = performance["player"]
            metadata["performance"] = {
                "fingerprint": performance["fingerprint"],
                "schema_version": performance["performance_schema_version"],
                "rules_version": performance["rules"]["general_version"],
                "player": player["selector"], "player_status": player["status"],
                "player_guid": player["guid"]}
            metadata["performance_bytes"] = os.path.getsize(paths["performance.json"])
        return metadata, paths

    def publish(self, segment: Segment) -> tuple[str, str]:
        """Publish requested artifacts, with analysis metadata as the final marker."""
        segment.close()
        self.ensure_dirs()
        directory = self.directory_for(segment.kind)
        name = self.resolve_name(segment)
        json_path = os.path.join(directory, name + ".json")
        body_suffix = ".txt.gz" if self.options.gzip else ".txt"
        body_path = os.path.join(directory, name + body_suffix)
        stage_dir = segment.stage_dir
        full_stage = segment.partial_path
        try:
            if self.options.wants_full:
                if full_stage is None:
                    raise RuntimeError("full body staging file missing")
                if self.options.gzip:
                    if stage_dir is None:
                        raise RuntimeError("gzip staging directory missing")
                    compressed = os.path.join(stage_dir, "full.txt.gz")
                    _deterministic_gzip(full_stage, compressed)
                    full_stage = compressed
                full_stored_bytes = os.path.getsize(full_stage)
            else:
                full_stored_bytes = None

            analysis_metadata = None
            analysis_paths: dict[str, str] = {}
            if self.options.wants_analysis:
                analysis_metadata, analysis_paths = self._analysis_payload(
                    segment, name, full_stored_bytes)

            # Legacy invariant: metadata is visible before its requested body.
            if self.options.wants_full:
                _atomic_write_bytes(json_path, _json_bytes(segment.metadata()))
                if self.options.is_legacy_default:
                    os.replace(segment.partial_path, body_path)
                else:
                    _copy_atomic(full_stage, body_path)

            marker_path = body_path
            if analysis_metadata is not None:
                analysis_dir = os.path.join(directory, name, "analysis")
                os.makedirs(analysis_dir, exist_ok=True)
                combat_names = [item for item in analysis_paths if item.startswith("combat.")]
                publish_order = ["summary.json"] + combat_names + \
                    ["deaths.json", "players.json"]
                obsolete_files = ["metadata.json",
                                  "combat.txt" if self.options.gzip else "combat.txt.gz"]
                if "performance.json" in analysis_paths:
                    publish_order.append("performance.json")
                else:
                    obsolete_files.append("performance.json")
                # Every profile publishes into the same analysis folder, replacing
                # the payload one file at a time. Retire the previous marker (and the
                # other container's combat body, and a performance.json this profile
                # does not produce) first: a crash mid-replacement must leave an
                # obviously incomplete package, never a marker still advertising the
                # profile being overwritten. State has not advanced, so the next run
                # of this profile republishes over the same names.
                for obsolete in obsolete_files:
                    try:
                        os.remove(os.path.join(analysis_dir, obsolete))
                    except FileNotFoundError:
                        pass
                for filename in publish_order:
                    _copy_atomic(analysis_paths[filename], os.path.join(analysis_dir, filename))
                embedded = dict(analysis_metadata)
                embedded_bytes = _json_bytes(embedded)
                if self.options.bundle:
                    zip_stage = os.path.join(stage_dir or "", "analysis.zip")
                    zip_files: list[tuple[str, str | bytes]] = [
                        (filename, source) for filename, source in analysis_paths.items()]
                    zip_files.append(("metadata.json", embedded_bytes))
                    self._deterministic_zip(zip_stage, zip_files)
                    zip_path = os.path.join(directory, name + "_analysis.zip")
                    _copy_atomic(zip_stage, zip_path)
                    analysis_metadata["analysis_zip_bytes"] = os.path.getsize(zip_path)
                marker_path = os.path.join(analysis_dir, "metadata.json")
                _atomic_write_bytes(marker_path, _json_bytes(analysis_metadata))
                folder_total = sum(os.path.getsize(os.path.join(analysis_dir, item))
                                   for item in publish_order) + os.path.getsize(marker_path)
                if self.verbose:
                    safe_print("    Full log:              %s" % format_megabytes(
                        analysis_metadata["full_uncompressed_bytes"]))
                    if analysis_metadata["full_stored_bytes"] is not None and \
                            analysis_metadata["full_stored_bytes"] != \
                            analysis_metadata["full_uncompressed_bytes"]:
                        safe_print("    Full log stored:       %s" % format_megabytes(
                            analysis_metadata["full_stored_bytes"]))
                    safe_print("    Analysis log:          %s" % format_megabytes(
                        analysis_metadata["combat_uncompressed_bytes"]))
                    safe_print("    Reduction:             %s%%" %
                               analysis_metadata["reduction_percent"])
                    safe_print("    Analysis bundle total: %s" % format_megabytes(folder_total))
                    if analysis_metadata["analysis_zip_bytes"] is not None:
                        safe_print("    Analysis ZIP:          %s" % format_megabytes(
                            analysis_metadata["analysis_zip_bytes"]))

            self._purge_stale(directory, segment.segment_id, name)
            if self.verbose:
                safe_print("  + %s" % os.path.relpath(marker_path, self.output_dir))
            return segment.kind, marker_path
        finally:
            if stage_dir:
                shutil.rmtree(stage_dir, ignore_errors=True)
                try:
                    os.rmdir(os.path.dirname(stage_dir))
                except OSError:
                    pass


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
        # Last COMBAT_LOG_VERSION seen. It clears the ring buffer, so no pending
        # segment ever starts before it: a resume can rely on the stored copy.
        self.log_header: dict | None = None
        self.log_header_source = "unknown"   # stream | state | file_start | unknown

    # -- public ------------------------------------------------------------------
    def feed(self, offset: int, raw: bytes, text: str) -> None:
        timestamp, event, args = parse_line(text, self.default_year)
        if self.warmup:
            self._feed_warmup(timestamp, event, offset, raw, args)
            return
        if timestamp is None:
            # Unparseable line: still part of the segment body, never fatal.
            self._buffer_line(self.last_ts, offset, raw)
            if self.segment is not None:
                self.segment.write(raw, self.last_ts, None, [])
            return

        if self.last_ts is not None and \
                (self.last_ts - timestamp).total_seconds() > BACKWARDS_JUMP_SECONDS:
            self._close_segment()
            self.buffer.clear()
        self.last_ts = timestamp

        if event == "COMBAT_LOG_VERSION":
            self._close_segment()
            self.buffer.clear()
            self._set_log_header(args)

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
                segment.write(raw, timestamp, event, args)
                self._record_boss(args)
                return
            self._close_segment()
            self._buffer_line(timestamp, offset, raw)
            self._open_raid(timestamp, args)
            return

        self._buffer_line(timestamp, offset, raw)
        if self.segment is not None:
            self.segment.write(raw, timestamp, event, args)

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
    def _feed_warmup(self, timestamp, event, offset: int, raw: bytes,
                     args: list[str] | None = None) -> None:
        """Refill the ring buffer only; no segment may be opened during warm-up."""
        if timestamp is not None:
            if self.last_ts is not None and \
                    (self.last_ts - timestamp).total_seconds() > BACKWARDS_JUMP_SECONDS:
                self.buffer.clear()
            self.last_ts = timestamp
            if event == "COMBAT_LOG_VERSION":
                self.buffer.clear()
                self._set_log_header(args or [])
            self._buffer_line(timestamp, offset, raw)
        else:
            self._buffer_line(self.last_ts, offset, raw)

    def _set_log_header(self, args: list[str]) -> None:
        self.log_header = parse_log_header(args)
        self.log_header_source = "stream"

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
        os.makedirs(self.publisher.directory_for(segment.kind), exist_ok=True)
        stage_dir = None
        if not self.publisher.options.is_legacy_default:
            stage_dir = self.publisher.stage_dir(segment.kind, segment.segment_id)
        partial_path = self.publisher.partial_path(
            segment.kind, segment.name_core(False), segment.segment_id) or None
        segment.game_context = dict(self.log_header or parse_log_header([]))
        segment.game_context["header_source"] = self.log_header_source
        segment.begin_body(partial_path, stage_dir)
        segment.start_offset = self.buffer[0][1] if self.buffer else fallback_offset
        for buffered_ts, _, raw in self.buffer:
            parsed_ts, event, args = parse_line(_decode(raw), self.default_year)
            segment.write(raw, parsed_ts or buffered_ts, event, args)
        self.segment = segment

    def _open_mplus(self, timestamp: datetime, args: list[str]) -> None:
        map_id = to_int(arg_at(args, 1))
        segment = Segment(KIND_MPLUS, timestamp, self.source_file,
                          self._segment_id(KIND_MPLUS, timestamp, map_id),
                          self.publisher.options)
        segment.dungeon = unquote(arg_at(args, 0)) or None
        segment.map_id = map_id
        segment.challenge_mode_id = to_int(arg_at(args, 2))
        segment.key_level = to_int(arg_at(args, 3))
        segment.affixes = parse_affixes(arg_at(args, 4))
        self._start_segment(segment, self.buffer[-1][1] if self.buffer else 0)

    def _open_raid(self, timestamp: datetime, args: list[str]) -> None:
        encounter_id = to_int(arg_at(args, 0))
        segment = Segment(KIND_RAID, timestamp, self.source_file,
                          self._segment_id(KIND_RAID, timestamp, encounter_id),
                          self.publisher.options)
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

    def __init__(self, path: str, publisher: SegmentPublisher, offset: int = 0,
                 log_header: dict | None = None, log_header_source: str = "unknown"):
        self.path = os.path.abspath(path)
        self.name = os.path.basename(self.path)
        self.publisher = publisher
        self.offset = max(0, int(offset))
        self._signature: tuple | None = None   # fingerprint of the consumed prefix
        self._needs_warmup = self.offset > 0
        self.tracker = self._new_tracker()
        # Header known before reading (resume); a COMBAT_LOG_VERSION in the stream,
        # warm-up included, replaces it. A truncation restarts without it.
        if log_header is not None:
            self.tracker.log_header = dict(log_header)
            self.tracker.log_header_source = log_header_source

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

    def take_counts(self) -> tuple[int, int]:
        """Counts of the segments published since the last call (single use).

        The list is detached before it is counted, so an interruption between the two
        can lose a count but never report one publication twice.
        """
        published, self.tracker.published = self.tracker.published, []
        mplus = sum(1 for kind, _ in published if kind == KIND_MPLUS)
        return mplus, len(published) - mplus


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace").rstrip("\r\n")


def read_log_header_at_start(path: str) -> dict | None:
    """Parse the file's first line if it is a COMBAT_LOG_VERSION header (read-only)."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(HEADER_PROBE_BYTES)
    except OSError:
        return None
    end = head.find(b"\n")
    if end == -1:
        return None
    _, event, args = parse_line(_decode(head[:end + 1]), 2000)
    if event != "COMBAT_LOG_VERSION":
        return None
    return parse_log_header(args)


# --- state store ------------------------------------------------------------------

class StateStore:
    """state.json: committed offset per log file plus replacement detection."""

    def __init__(self, path: str, profile: str = "full"):
        self.path = os.path.abspath(path)
        self.profile = profile
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
        self.data = {"version": 2, "files": {}}

    def _migrate_v2(self) -> None:
        if self.data.get("version") == 2:
            return
        migrated = {}
        for name, old_entry in self.data.get("files", {}).items():
            if not isinstance(old_entry, dict):
                continue
            legacy = dict(old_entry)
            legacy.pop("profiles", None)
            entry = dict(legacy)
            entry["profiles"] = {"full": legacy}
            migrated[name] = entry
        self.data = {"version": 2, "files": migrated}

    def _profile_entry(self, path: str) -> dict | None:
        entry = self.data.get("files", {}).get(os.path.basename(path))
        if not isinstance(entry, dict):
            return None
        if self.data.get("version") == 2:
            profiles = entry.get("profiles")
            if not isinstance(profiles, dict):
                return None
            value = profiles.get(self.profile)
            return value if isinstance(value, dict) else None
        if self.profile == "full":
            return entry
        return None

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

    def _validated_entry(self, path: str) -> tuple[dict, int] | None:
        entry = self._profile_entry(path)
        if entry is None:
            return None
        offset = to_int(str(entry.get("offset", 0))) or 0
        if offset <= 0:
            return None
        try:
            if os.path.getsize(path) < offset:
                return None
            head_hash, tail_hash = self._hashes(path, offset)
        except OSError:
            return None
        if head_hash != entry.get("head_hash") or tail_hash != entry.get("tail_hash"):
            return None  # replaced/rewritten log: reprocess from the beginning
        return entry, offset

    def get_offset(self, path: str) -> int:
        validated = self._validated_entry(path)
        return validated[1] if validated is not None else 0

    def get_log_header(self, path: str) -> dict | None:
        """Game header stored with the offset; only trusted when the offset is."""
        validated = self._validated_entry(path)
        if validated is None:
            return None
        header = validated[0].get("log_header")
        return dict(header) if isinstance(header, dict) else None

    def claim(self, path: str) -> bool:
        """Take ownership of a file's state before this profile starts publishing.

        Publication replaces the shared <name>/ destinations and can crash half-way;
        if the previous owner's EOF offset survived until the commit, switching back
        to that profile would never repair the package. Dropping the other profiles'
        entries up front makes any later run of any profile re-scan and converge.
        Returns True when the stored state changed.
        """
        self._migrate_v2()
        name = os.path.basename(path)
        entry = self.data.get("files", {}).get(name)
        if not isinstance(entry, dict):
            return False
        profiles = entry.get("profiles")
        if not isinstance(profiles, dict) or set(profiles) <= {self.profile}:
            return False
        own = profiles.get(self.profile)
        new_entry: dict = {"profiles": {}}
        if isinstance(own, dict):
            new_entry["profiles"][self.profile] = own
            if self.profile == "full" and (to_int(str(own.get("offset", 0))) or 0) > 0:
                new_entry.update(own)
        self.data["files"][name] = new_entry
        return True

    def update(self, path: str, offset: int, log_header: dict | None = None) -> None:
        offset = max(0, int(offset))
        try:
            head_hash, tail_hash = self._hashes(path, offset)
            size = os.path.getsize(path)
            mtime = os.path.getmtime(path)
        except OSError:
            return
        profile_entry = {
            "offset": offset,
            "size": size,
            "mtime": mtime,
            "head_hash": head_hash,
            "tail_hash": tail_hash,
        }
        if log_header is not None:
            # Last header before the committed offset (see SegmentTracker.log_header).
            profile_entry["log_header"] = dict(log_header)
        self._migrate_v2()
        name = os.path.basename(path)
        # A publication owns the shared destinations under <name>/, so the offsets
        # recorded for other profiles no longer describe what is on disk: drop them.
        # Switching flags therefore backfills the file exactly once (the republish
        # reuses the same names) while repeating the same flags still does nothing.
        # Artifacts already written by another profile are never deleted.
        new_entry: dict = {"profiles": {self.profile: profile_entry}}
        if self.profile == "full" and offset > 0:
            # v1 mirror: only the full profile may claim the top-level offset.
            new_entry.update(profile_entry)
        self.data["files"][name] = new_entry


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


# --- diagnostic packet --------------------------------------------------------------
# One diagnostic_packet.json per session and resolved player GUID, rebuilt on every run
# from the published performance.json files (never from deltas or offsets). Pure and
# deterministic: no generation time, every list in a fixed order, compact strict JSON.

PACKET_SUFFIX = "_diagnostic_packet.json"
PACKET_TOP_SPELLS = 10               # pulls[].top_spells before the budget trims it
PACKET_BUDGET_GROUP_SPELLS = 8       # budget step 4
PACKET_BUDGET_GROUP_TARGETS = 5
PACKET_BUDGET_TOP_SPELLS = 5
PACKET_DEATH_CASTS = 5               # completed casts listed before each death
PACKET_CONCENTRATION_SPELLS = 3
MAX_PACKET_SKIPPED = 50              # data_quality.skipped_results rows per packet
# Skip reasons of a package that exists without a valid marker: it may be half
# republished (the marker is retired first), so the packets that list it are held.
INCOMPLETE_PACKAGE_REASONS = ("marker_missing", "marker_unreadable")
# A complete pull shorter than this still counts in attempts and duration_s, but not in
# the per-pull rate statistics (the PACKET_POOLED metrics) nor in the choice of
# representative pulls; the pooled rates keep it, since they weight by duration.
MIN_COMPARABLE_SECONDS = 30
SHORT_PULL = "pull_shorter_than_min_comparable"
PACKET_OPENER_PREFIXES = (2, 3, 4, 6)
PACKET_OPENER_PREFIX = 4             # the opener_consistency numerator/denominator row
# A resolved performance.json without one of these cannot be summarised.
PACKET_RESOLVED_KEYS = ("character", "life", "damage", "casts", "auras", "resources",
                        "continuity", "timeline", "opener", "spec")
PACKET_GENERAL_METRICS = ("dps_encounter", "dps_while_alive", "casts_per_minute",
                          "total_effective", "deaths", "alive_seconds",
                          "action_gaps_total_s")
PACKET_ARCANE_METRICS = ("burst_windows", "burst_buff_uptime_share", "surge_first_use_s")
# metric -> (pooled name, per seconds): pooled = sum of numerators / sum of denominators.
PACKET_POOLED = {"dps_encounter": ("dps_encounter_pooled", 1),
                 "dps_while_alive": ("dps_while_alive_pooled", 1),
                 "casts_per_minute": ("casts_per_minute_pooled", 60)}

PACKET_DEFINITIONS = {name: {"description": text, "unit": unit, "numerator": numerator,
                             "denominator": denominator}
                      for name, (text, unit, numerator, denominator) in {
    "duration_s": ("Encounter duration of a pull (ENCOUNTER_END fight time; observation "
                   "end for incomplete pulls).", "s", None, None),
    "total_effective": ("Effective damage (amount - max(overkill, 0)) of the player and "
                        "attributed pets on hostile targets during the encounter.",
                        "damage", None, None),
    "dps_encounter": ("Effective damage per second of encounter (complete pulls). Group "
                      "statistics leave out pulls shorter than %d s "
                      "(pull_shorter_than_min_comparable)." % MIN_COMPARABLE_SECONDS,
                      "damage/s", "total_effective", "duration_s"),
    "dps_while_alive": ("Effective damage dealt while alive per second alive. Group "
                        "statistics leave out pulls shorter than %d s."
                        % MIN_COMPARABLE_SECONDS, "damage/s",
                        "total_effective - after_death_effective", "alive_seconds"),
    "dps_observed": ("Incomplete pulls only: effective damage per observed second.",
                     "damage/s", "total_effective",
                     "seconds from ENCOUNTER_START to the last line seen"),
    "casts_per_minute": ("Completed casts (SPELL_CAST_SUCCESS) per minute of encounter. "
                         "Group statistics leave out pulls shorter than %d s."
                         % MIN_COMPARABLE_SECONDS, "casts/min", "casts_success",
                         "duration_s / 60"),
    "casts_success": ("Completed casts (SPELL_CAST_SUCCESS) by the player.", "casts",
                      None, None),
    "deaths": ("Player deaths inside the encounter.", "count", None, None),
    "alive_seconds": ("Seconds the player was alive during the encounter.", "s", None,
                      None),
    "action_gaps_total_s": ("Sum of observed gaps longer than the continuity threshold "
                            "between consecutive player actions within one alive "
                            "period; cause not determined.", "s", None, None),
    "dps_encounter_pooled": ("Sum of total_effective over the sum of duration_s of the "
                             "eligible pulls, short pulls included; not the median of "
                             "per-pull rates.",
                             "damage/s", "sum of total_effective", "sum of duration_s"),
    "dps_while_alive_pooled": ("Sum of damage dealt while alive over the sum of "
                               "alive_seconds of the eligible pulls, short pulls "
                               "included.", "damage/s",
                               "sum of total_effective - after_death_effective",
                               "sum of alive_seconds"),
    "casts_per_minute_pooled": ("Sum of completed casts over the sum of encounter "
                                "minutes of the eligible pulls, short pulls included.",
                                "casts/min",
                                "sum of casts_success", "sum of duration_s / 60"),
    "common_windows.effective": ("Effective damage of the player and pets in [0, N] s of "
                                 "a pull that covers N s with the player alive.",
                                 "damage", None, None),
    "common_windows.casts_success": ("Completed casts in [0, N] s of a pull that covers "
                                     "N s with the player alive.", "casts", None, None),
    "opener_signature": ("Spell ids of the first completed casts in the first "
                         "%d s, in order." % OPENER_SECONDS, "spell ids", None, None),
    "opener_signatures.prefixes": ("Per length k in %s: the most common sequence of the "
                                   "first k opener casts (ties: higher count, then the "
                                   "smaller sequence); pulls whose signature has fewer "
                                   "than k casts are left out of that row's denominator."
                                   % ", ".join(str(k) for k in PACKET_OPENER_PREFIXES),
                                   "pulls", "pulls with that first-k sequence",
                                   "eligible pulls with at least k opener casts"),
    "share_of_effective": ("A spell's or target's effective damage over the group's "
                           "total_effective.", "ratio", "row effective",
                           "sum of total_effective"),
    "burst_windows": ("Arcane: established intervals of the burst buff per pull.",
                      "count", None, None),
    "burst_buff_uptime_share": ("Arcane: observed uptime of the burst buff over the "
                                "encounter duration.", "ratio", "uptime_observed_s",
                                "duration_s"),
    "surge_first_use_s": ("Arcane: time of the first Arcane Surge CAST_SUCCESS.", "s",
                          None, None),
    "deaths_before_end": ("Eligible pulls with a player death inside the encounter; each "
                          "first death is listed as a fraction of the pull duration with "
                          "the pull result.", "pulls", "pulls with a death",
                          "eligible pulls"),
    "dead_time_share": ("Seconds dead over observed encounter seconds.", "ratio",
                        "sum of dead seconds", "sum of alive + dead seconds"),
    "action_gap_share": ("Seconds in observed action gaps over seconds alive; cause not "
                         "determined.", "ratio", "sum of action_gaps_total_s",
                         "sum of alive_seconds"),
    "opener_consistency": ("How often the most common first-k opener sequence repeats "
                           "(opener_signatures.prefixes); the numbers are those of the "
                           "first-%d row." % PACKET_OPENER_PREFIX, "pulls",
                           "pulls with the most common first-%d sequence"
                           % PACKET_OPENER_PREFIX,
                           "eligible pulls with at least %d opener casts"
                           % PACKET_OPENER_PREFIX),
    "damage_spell_concentration": ("Effective damage of the top spells over the total.",
                                   "damage", "effective of the top spells",
                                   "sum of total_effective"),
    "surge_first_use": ("Arcane: pulls with an observed Arcane Surge CAST_SUCCESS.",
                        "pulls", "pulls with a use", "eligible pulls"),
    "surge_touch_order": ("Arcane: pulls whose first Arcane Surge preceded the first "
                          "Touch of the Magi.", "pulls", "pulls with Surge first",
                          "eligible pulls"),
    "burst_window_casts": ("Arcane: completed casts inside established burst windows.",
                           "casts", "casts inside windows", "windows"),
    "clearcasting_refresh_at_max": ("Arcane: Clearcasting refreshes while at the maximum "
                                    "observed stacks.", "refreshes",
                                    "refreshes at max stacks", "refreshes"),
    "charge_over_energize": ("Arcane: over-cap charges reported by Arcane Charge "
                             "energize events over the charges they generated (gained "
                             "plus over the cap).", "charges", "sum of over_energize",
                             "sum of gains + sum of over_energize"),
    "barrage_inferred_charges": ("Arcane, inferred: Arcane Barrage casts at 4 inferred "
                                 "charges.", "casts", "casts at 4 inferred charges",
                                 "casts with a known inferred count"),
}.items()}


def _packet_time(text) -> datetime | None:
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S.%f")
    except (TypeError, ValueError):
        return None


_MISSING = object()


def _read_package_json(path: str):
    """Parsed JSON; _MISSING when the file does not exist; None when it does not parse.

    Any other OSError (a denied or failed read) propagates: it says nothing about the
    package, so the rebuild must fail instead of treating the pull as absent.
    """
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        return _MISSING
    try:
        return json.loads(data.decode("utf-8"))
    except ValueError:
        return None


_SKIPPED_NAME_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})(?:-(\d{2}))?(?:[_.-]|$)")


def _skipped_start(name: str, marker) -> datetime | None:
    """Start of a skipped package: its marker's segment id, else its name prefix."""
    if isinstance(marker, dict) and isinstance(marker.get("segment_id"), str):
        parts = marker["segment_id"].split("|")
        start = _packet_time(parts[-2]) if len(parts) >= 4 else None
        if start is not None:
            return start
    match = _SKIPPED_NAME_RE.match(name)
    if match is None:
        return None
    day, hours, minutes, seconds = match.groups()
    try:
        return datetime.strptime("%s %s:%s:%s" % (day, hours, minutes, seconds or "00"),
                                 "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _load_performance(package: str, name: str,
                      fingerprint: str | None) -> tuple[dict | None, str | None, object]:
    """(performance.json, None, marker) of a valid package, or (None, reason, marker).

    `marker` is the parsed metadata.json, or None when it is missing or unreadable.
    """
    analysis = os.path.join(package, "analysis")
    marker = _read_package_json(os.path.join(analysis, "metadata.json"))
    if marker is _MISSING:
        return None, "marker_missing", None
    if not isinstance(marker, dict):
        return None, "marker_unreadable", None
    artifacts = marker.get("artifacts")
    if not isinstance(artifacts, list) or \
            name + "/analysis/performance.json" not in artifacts:
        return None, "performance_not_published", marker
    info = marker.get("performance")
    if not isinstance(info, dict) or info.get("fingerprint") != fingerprint:
        return None, "fingerprint_mismatch", marker
    performance = _read_package_json(os.path.join(analysis, "performance.json"))
    if not isinstance(performance, dict):
        return None, "performance_unreadable", marker
    if performance.get("performance_schema_version") != PERFORMANCE_SCHEMA_VERSION:
        return None, "schema_version_mismatch", marker
    segment, player = performance.get("segment"), performance.get("player")
    if not isinstance(segment, dict) or segment.get("segment_id") != marker.get("segment_id"):
        return None, "segment_id_mismatch", marker
    if _packet_time(segment.get("start_time")) is None or not isinstance(player, dict):
        return None, "performance_unreadable", marker
    if player.get("status") == "resolved" and (
            not player.get("guid") or
            any(not isinstance(performance.get(key), dict) for key in PACKET_RESOLVED_KEYS)):
        return None, "performance_unreadable", marker
    return performance, None, marker


def _stat_mode_or_none(path: str) -> int | None:
    """st_mode, or None for an entry that vanished; any other OSError propagates
    (os.path.isdir/isfile would read a denied or failed stat as absence)."""
    try:
        return os.stat(path).st_mode
    except FileNotFoundError:
        return None


def _is_directory(path: str) -> bool:
    mode = _stat_mode_or_none(path)
    return mode is not None and stat.S_ISDIR(mode)


def collect_performance_results(raids_dir: str,
                                fingerprint: str | None) -> tuple[list[dict], list[dict]]:
    """(results, skipped) over `<output>/Raids/*/analysis/metadata.json`.

    A pull is accepted only when its marker parses, lists performance.json and carries
    the current fingerprint, and performance.json parses with the current schema and
    the marker's segment id. Results are {"name", "performance"}; skipped entries are
    {"name", "reason", "start"} (start: see _skipped_start, or None). Both are sorted
    by package name. Only a missing `raids_dir` means "no results": any other read
    error raises, so a failed listing or read never empties the packets.
    """
    results, skipped = [], []
    try:
        names = sorted(os.listdir(raids_dir))
    except FileNotFoundError:
        return results, skipped
    for name in names:
        package = os.path.join(raids_dir, name)
        if name == ".staging" or not _is_directory(package):
            continue
        performance, reason, marker = _load_performance(package, name, fingerprint)
        if reason is None:
            results.append({"name": name, "performance": performance})
        else:
            skipped.append({"name": name, "reason": reason,
                            "start": _skipped_start(name, marker)})
    return results, skipped


def _observed_end(performance: dict) -> datetime:
    """END timestamp of a complete pull; start + observed seconds otherwise."""
    segment = performance["segment"]
    start = _packet_time(segment["start_time"])
    end = _packet_time(segment.get("end_time")) if segment.get("complete") else None
    if end is None:
        end = start + timedelta(seconds=segment.get("observed_seconds") or 0)
    return end


def _copy_rank(result: dict) -> tuple:
    """Duplicate winner first: complete, then later observed end, then lower log name."""
    performance = result["performance"]
    return (not performance["segment"].get("complete"),
            datetime.max - _observed_end(performance),
            str(performance.get("source", {}).get("file") or ""),
            str(performance["segment"].get("segment_id")), result["name"])


def _pull_totals(performance: dict) -> dict:
    return {"duration_ms": performance["segment"].get("duration_ms"),
            "result": performance["segment"].get("result"),
            "total_effective": performance["damage"].get("total_effective"),
            "casts_success": performance["casts"].get("total_success"),
            "deaths": len(performance["life"].get("deaths") or [])}


def _duplicate_conflicts(copies: list[dict]) -> dict:
    """{field: [value per complete copy]} for the totals the complete copies disagree on."""
    complete = [copy["performance"] for copy in copies
                if copy["performance"]["segment"].get("complete")]
    if len(complete) < 2:
        return {}
    totals = [_pull_totals(performance) for performance in complete]
    return {field: [row[field] for row in totals] for field in totals[0]
            if len({json.dumps(row[field]) for row in totals}) > 1}


def _session_pull(guid: str, copies: list[dict]) -> dict:
    copies = sorted(copies, key=_copy_rank)
    performance = copies[0]["performance"]
    return {"guid": guid, "start": _packet_time(performance["segment"]["start_time"]),
            "end": _observed_end(performance), "name": copies[0]["name"],
            "performance": performance, "copies": copies,
            "conflicts": _duplicate_conflicts(copies)}


def build_sessions(results: list[dict], gap_minutes: int) -> list[dict]:
    """Sessions by resolved GUID (decision 6), independent of the collection order.

    A session is a maximal chain of one GUID's pulls, by start, where the gap from the
    observed end of the chain so far to the next start is at most `gap_minutes`; date
    and log file are irrelevant. Copies of one pull (same GUID, encounter and start to
    the millisecond) collapse to one winner that keeps every copy. Absent/ambiguous
    pulls go to every session whose interval, widened by the gap, contains their start.
    """
    gap = timedelta(minutes=gap_minutes)
    copies_by_key: dict[tuple, list[dict]] = {}
    unresolved = []
    for result in results:
        performance = result["performance"]
        player = performance["player"]
        if player.get("status") != "resolved":
            unresolved.append(result)
            continue
        segment = performance["segment"]
        key = (player["guid"], str(segment.get("encounter_id")), segment["start_time"])
        copies_by_key.setdefault(key, []).append(result)
    pulls_by_guid: dict[str, list[dict]] = {}
    for (guid, _, _), copies in copies_by_key.items():
        pulls_by_guid.setdefault(guid, []).append(_session_pull(guid, copies))
    unresolved.sort(key=lambda result: (
        _packet_time(result["performance"]["segment"]["start_time"]), result["name"]))
    sessions = []
    for guid in sorted(pulls_by_guid):
        pulls = sorted(pulls_by_guid[guid], key=lambda pull: (
            pull["start"], str(pull["performance"]["segment"].get("encounter_id")),
            pull["name"]))
        # A pull can be resolved without a name (an exact GUID in COMBATANT_INFO
        # only): the GUID's first observed name, by pull order, names every session.
        player_name = next((copy["performance"]["player"].get("name")
                            for pull in pulls for copy in pull["copies"]
                            if copy["performance"]["player"].get("name")), None)
        chains: list[list[dict]] = []
        chain_end = None
        for pull in pulls:
            if not chains or pull["start"] - chain_end > gap:
                chains.append([])
                chain_end = pull["end"]
            chains[-1].append(pull)
            chain_end = max(chain_end, pull["end"])
        for chain in chains:
            start, end = chain[0]["start"], max(pull["end"] for pull in chain)
            without = [result for result in unresolved if start - gap <=
                       _packet_time(result["performance"]["segment"]["start_time"])
                       <= end + gap]
            sessions.append({"guid": guid, "start": start, "end": end,
                             "gap_minutes": gap_minutes, "pulls": chain,
                             "without_player": without, "player_name": player_name})
    sessions.sort(key=lambda session: (session["start"], session["guid"]))
    return sessions


# -- packet content ---------------------------------------------------------------------

def _packet_round(value):
    return round(value, 3) if isinstance(value, float) else value


def _quantile(ordered: list, fraction: float):
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    if position == lower:
        return ordered[lower]
    return ordered[lower] + (ordered[lower + 1] - ordered[lower]) * (position - lower)


def packet_stats(values: list) -> dict:
    """STATS: n, min, q1, median, q3, max (linear interpolation); null when n = 0."""
    if not values:
        return {"n": 0, "min": None, "q1": None, "median": None, "q3": None, "max": None}
    ordered = sorted(values)
    return {"n": len(ordered), "min": _packet_round(ordered[0]),
            "q1": _packet_round(_quantile(ordered, 0.25)),
            "median": _packet_round(_quantile(ordered, 0.5)),
            "q3": _packet_round(_quantile(ordered, 0.75)),
            "max": _packet_round(ordered[-1])}


def _ratio(numerator, denominator):
    return round(numerator / denominator, 3) if denominator else None


def _fmt(value) -> str:
    return "n/a" if value is None else str(_packet_round(value))


def _percent(numerator, denominator) -> str:
    return "%.1f%%" % (100.0 * numerator / denominator) if denominator else "n/a"


def _rate_value(rate) -> tuple:
    """(value, None), or (None, reason) for a null performance.json rate."""
    if not isinstance(rate, dict):
        return None, "unavailable"
    if rate.get("value") is None:
        return None, "denominator_null: %s" % rate.get("reason", "unknown")
    return rate["value"], None


def _arcane_applied(performance: dict) -> bool:
    spec = performance["spec"]
    return spec.get("status") == "applied" and spec.get("id") == ARCANE_RULES["id"]


def _aura_keys_truncated(performance: dict) -> bool:
    return not performance["auras"]["coverage"].get("complete", True)


def _aura_holders_truncated(performance: dict, spell_id) -> bool:
    """An aura row of `spell_id` lost holders: its uptime may be overstated."""
    return any(row.get("holders_truncated") for row in performance["auras"]["on_player"]
               if row.get("spell_id") == spell_id)


def _burst_exclusion(performance: dict) -> str | None:
    """Why a pull's burst windows / burst buff uptime are not comparable, if they aren't."""
    if _aura_keys_truncated(performance):
        return "aura_keys_truncated"
    if _aura_holders_truncated(performance,
                               performance["spec"]["burst_windows"].get("aura_spell_id")):
        return "metric_partial: aura_holders"
    return None


def _pull_duration_s(performance: dict):
    segment = performance["segment"]
    if segment.get("complete") and segment.get("duration_ms"):
        return round(segment["duration_ms"] / 1000, 3)
    return segment.get("observed_seconds")


def _metric_values(entry: dict, arcane_metrics: bool) -> dict:
    """{metric: (value, None) | (None, reason)} of one pull of a group."""
    performance = entry["performance"]
    if not entry["complete"]:
        names = PACKET_GENERAL_METRICS + (PACKET_ARCANE_METRICS if arcane_metrics else ())
        return {name: (None, "incomplete") for name in names}
    damage, life, continuity = performance["damage"], performance["life"], \
        performance["continuity"]
    values = {
        "dps_encounter": _rate_value(damage["rates"].get("dps_encounter")),
        "dps_while_alive": _rate_value(damage["rates"].get("dps_while_alive")),
        "casts_per_minute": _rate_value(performance["casts"]["rates"].get(
            "casts_per_minute")),
        "total_effective": (damage["total_effective"], None),
        "deaths": (len(life["deaths"]), None),
        "alive_seconds": (life["alive_seconds"], None)
        if life.get("alive_seconds") is not None else (None, "life_state_unknown"),
        "action_gaps_total_s": (continuity["gaps_total_s"], None)
        if "gaps_total_s" in continuity else (None, "life_state_unknown"),
    }
    if entry["short"]:
        # Only an available rate becomes "short": a null rate keeps its own reason.
        for name in PACKET_POOLED:
            if values[name][1] is None:
                values[name] = (None, SHORT_PULL)
    if not arcane_metrics:
        return values
    if not entry["arcane"]:
        values.update({name: (None, "spec_rules_not_applied")
                       for name in PACKET_ARCANE_METRICS})
        return values
    spec = performance["spec"]
    excluded = _burst_exclusion(performance)
    burst = spec["burst_windows"]
    if excluded is not None:
        values["burst_windows"] = (None, excluded)
        values["burst_buff_uptime_share"] = (None, excluded)
    else:
        values["burst_windows"] = (None, "metric_partial: burst_windows") \
            if burst.get("partial") else (burst["count"], None)
        uptime = sum(row.get("uptime_observed_s") or 0
                     for row in performance["auras"]["on_player"]
                     if row.get("spell_id") == burst.get("aura_spell_id"))
        values["burst_buff_uptime_share"] = (_ratio(uptime, entry["duration_s"]), None) \
            if entry["duration_s"] else (None, "denominator_null: zero duration")
    first = spec["opener"].get("surge_first_success_s")
    values["surge_first_use_s"] = (first, None) if first is not None \
        else (None, "not_observed")
    return values


def _pooled(entries: list[dict], metric: str) -> tuple[str, dict]:
    name, per_seconds = PACKET_POOLED[metric]
    numerator = denominator = 0
    pulls = []
    for entry in entries:
        performance = entry["performance"]
        rates = performance["casts"]["rates"] if metric == "casts_per_minute" \
            else performance["damage"]["rates"]
        rate = rates[metric]
        numerator += rate["numerator"]
        denominator += rate["denominator_s"]
        pulls.append(entry["pull_id"])
    denominator = round(denominator, 3)
    value = round(numerator * per_seconds / denominator, 3) if denominator else None
    row = {"value": value, "numerator": numerator, "denominator": denominator,
           "n": len(pulls), "pulls": pulls}
    if value is None:
        row["reason"] = "no eligible pull"
    return name, row


def _window_exclusion(entry: dict, seconds: int) -> str | None:
    if not entry["complete"]:
        return "incomplete"
    rows = [row for row in entry["performance"]["damage"].get("windows") or []
            if row.get("seconds") == seconds]
    if not rows:
        return "window_unavailable"
    if rows[0].get("covered"):
        return None
    observed = entry["performance"]["segment"].get("observed_seconds") or 0
    return "pull_shorter_than_window" if observed < seconds else \
        "player_not_alive_for_window"


def _common_windows(members: list[dict]) -> list[dict]:
    windows = []
    for seconds in COMMON_WINDOWS:
        included, excluded = [], []
        for entry in members:
            reason = _window_exclusion(entry, seconds)
            if reason is None:
                row = [row for row in entry["performance"]["damage"]["windows"]
                       if row["seconds"] == seconds][0]
                included.append((entry["pull_id"], row))
            else:
                excluded.append({"pull_id": entry["pull_id"], "reason": reason})
        windows.append({"seconds": seconds, "n": len(included),
                        "pulls": [pull_id for pull_id, _ in included],
                        "excluded": excluded,
                        "effective": packet_stats([row["effective"] for _, row in included]),
                        "casts_success": packet_stats([row["casts_success"]
                                                       for _, row in included])})
    return windows


def _opener_exclusion(entry: dict) -> str | None:
    performance = entry["performance"]
    if not entry["complete"]:
        return "incomplete"
    if performance["opener"].get("partial"):
        return "opener_partial"
    if (performance["segment"].get("observed_seconds") or 0) < OPENER_SECONDS:
        return "pull_shorter_than_opener"
    life = performance["life"]
    deaths = [death["t_s"] for death in life.get("deaths") or []]
    if life.get("alive_at_start") is not True or (deaths and deaths[0] < OPENER_SECONDS):
        return "player_not_alive_for_opener"
    return None


def _opener_prefix(eligible: list[tuple], length: int) -> dict:
    """Most common first-`length` sequence among the (pull_id, signature) pairs.

    `denominator` counts the pulls whose signature has at least `length` casts; the
    others are left out of the row. Ties: higher count, then the smaller sequence.
    """
    counts: dict[tuple, list] = {}
    for pull_id, signature in eligible:
        if len(signature) >= length:
            counts.setdefault(tuple(signature[:length]), []).append(pull_id)
    denominator = sum(len(pulls) for pulls in counts.values())
    if not counts:
        return {"length": length, "count": 0, "denominator": 0, "signature": None,
                "pulls": []}
    prefix, pulls = min(counts.items(), key=lambda item: (
        -len(item[1]), [_perf_sort_id(spell) for spell in item[0]]))
    return {"length": length, "count": len(pulls), "denominator": denominator,
            "signature": list(prefix), "pulls": pulls}


def _opener_signatures(members: list[dict]) -> dict:
    counts: dict[str, list] = {}
    excluded, eligible = [], []
    for entry in members:
        reason = _opener_exclusion(entry)
        if reason is not None:
            excluded.append({"pull_id": entry["pull_id"], "reason": reason})
            continue
        signature = entry["performance"]["opener"].get("signature") or []
        eligible.append((entry["pull_id"], signature))
        counts.setdefault(json.dumps(signature), [signature, []])[1].append(
            entry["pull_id"])
    signatures = [{"signature": signature, "count": len(pulls), "pulls": pulls}
                  for signature, pulls in counts.values()]
    signatures.sort(key=lambda row: (-row["count"], json.dumps(row["signature"])))
    return {"denominator": len(eligible), "excluded": excluded,
            "prefixes": [_opener_prefix(eligible, length)
                         for length in PACKET_OPENER_PREFIXES],
            "signatures": signatures}


def _group_spells(eligible: list[dict], total: int) -> list[dict]:
    rows: dict = {}
    for entry in eligible:
        performance = entry["performance"]
        for damage in performance["damage"]["by_spell"]:
            row = rows.setdefault(damage["spell_id"], {
                "spell_id": damage["spell_id"], "name": damage["name"], "effective": 0,
                "casts_success": 0, "pulls_with_casts": 0})
            row["effective"] += damage["effective"]
        for cast in performance["casts"]["by_spell"]:
            row = rows.setdefault(cast["spell_id"], {
                "spell_id": cast["spell_id"], "name": cast["name"], "effective": 0,
                "casts_success": 0, "pulls_with_casts": 0})
            row["casts_success"] += cast["success"]
            row["pulls_with_casts"] += 1 if cast["success"] else 0
    result = []
    for row in rows.values():
        result.append({"spell_id": row["spell_id"], "name": row["name"],
                       "effective": row["effective"],
                       "share_of_effective": _ratio(row["effective"], total),
                       "casts_success": row["casts_success"],
                       "pulls_with_casts": row["pulls_with_casts"]})
    result.sort(key=lambda row: (-row["effective"], -row["casts_success"],
                                 _perf_sort_id(row["spell_id"])))
    return result


def _group_targets(eligible: list[dict], total: int) -> list[dict]:
    rows: dict = {}
    for entry in eligible:
        for target in entry["performance"]["damage"]["by_target"]:
            row = rows.setdefault(target["key"], {
                "key": target["key"], "npc_id": target["npc_id"], "name": target["name"],
                "role": "unknown", "effective": 0, "max_hp": None, "pulls": 0})
            row["effective"] += target["effective"]
            row["pulls"] += 1
            if target.get("role") == "boss":
                row["role"] = "boss"
            max_hp = (target.get("evidence") or {}).get("max_hp")
            if max_hp is not None:
                row["max_hp"] = max(row["max_hp"] or 0, max_hp)
    result = [{"key": row["key"], "npc_id": row["npc_id"], "name": row["name"],
               "role": row["role"], "effective": row["effective"],
               "share_of_effective": _ratio(row["effective"], total),
               "max_hp": row["max_hp"], "pulls": row["pulls"]} for row in rows.values()]
    result.sort(key=lambda row: (-row["effective"], str(row["key"])))
    return result


def _representative_pulls(members: list[dict], dps: list[tuple]) -> list[dict]:
    """Most recent kill, longest wipe, median and lowest dps_encounter; no repeats.

    Pulls shorter than MIN_COMPARABLE_SECONDS are never chosen (`dps` already leaves
    them out).
    """
    chosen: list[dict] = []

    def add(pull_id, reason):
        if all(row["pull_id"] != pull_id for row in chosen):
            chosen.append({"pull_id": pull_id, "reason": reason})

    comparable = [entry for entry in members if not entry["short"]]
    kills = [entry for entry in comparable if entry["result"] == "kill"]
    if kills:
        add(max(kills, key=lambda entry: (entry["start"], entry["pull_id"]))["pull_id"],
            "most_recent_kill")
    wipes = [entry for entry in comparable if entry["result"] == "wipe"]
    if wipes:
        add(min(wipes, key=lambda entry: (-(entry["duration_s"] or 0),
                                          entry["pull_id"]))["pull_id"], "longest_wipe")
    if dps:
        ordered = sorted(dps, key=lambda item: (item[1], item[0]))
        add(ordered[(len(ordered) - 1) // 2][0], "median_dps_encounter")
        add(ordered[0][0], "lowest_dps_encounter")
    return chosen


def _group_metrics(members: list[dict], arcane_metrics: bool) -> tuple[dict, dict, list]:
    values = {entry["pull_id"]: _metric_values(entry, arcane_metrics) for entry in members}
    names = PACKET_GENERAL_METRICS + (PACKET_ARCANE_METRICS if arcane_metrics else ())
    metrics, pooled, dps = {}, {}, []
    for name in names:
        included, excluded, weighted = [], [], []
        for entry in members:
            value, reason = values[entry["pull_id"]][name]
            if reason is None:
                included.append(entry)
                if name == "dps_encounter":
                    dps.append((entry["pull_id"], value))
            else:
                excluded.append({"pull_id": entry["pull_id"], "reason": reason})
            if reason in (None, SHORT_PULL):
                weighted.append(entry)
        stats = packet_stats([values[entry["pull_id"]][name][0] for entry in included])
        stats["excluded"] = excluded
        metrics[name] = stats
        if name in PACKET_POOLED:
            pooled_name, row = _pooled(weighted, name)
            pooled[pooled_name] = row
    return metrics, pooled, dps


def _death_fractions(eligible: list[dict]) -> tuple[list, list]:
    died, evidence = [], []
    for entry in eligible:
        deaths = entry["performance"]["life"]["deaths"]
        if deaths:
            died.append("%s at %s (%s)" % (
                entry["pull_id"], _fmt(_ratio(deaths[0]["t_s"], entry["duration_s"])),
                entry["result"]))
            evidence.append({"pull_id": entry["pull_id"], "t_s": deaths[0]["t_s"],
                             "result": entry["result"], "ref": "life.deaths[0]"})
    return died, evidence


def _observation(obs_id: str, kind: str, statement: str, numerator, denominator,
                 unit: str, pulls: list, excluded: list, evidence: list,
                 applicability: str) -> dict:
    return {"id": obs_id, "kind": kind, "statement": statement,
            "numerator": _packet_round(numerator), "denominator": _packet_round(denominator),
            "unit": unit, "pulls": pulls, "excluded": excluded, "evidence": evidence,
            "applicability": applicability}


def _general_observations(group: dict, members: list[dict], spells: list[dict]) -> list:
    group_id = group["group_id"]
    eligible = [entry for entry in members if entry["complete"]]
    incomplete = [{"pull_id": entry["pull_id"], "reason": "incomplete"}
                  for entry in members if not entry["complete"]]
    pulls = [entry["pull_id"] for entry in eligible]
    scope = "complete pulls of group %s with the player resolved" % group_id
    observations = []
    died, evidence = _death_fractions(eligible)
    observations.append(_observation(
        "%s.deaths_before_end" % group_id, "observed",
        "The player died before the encounter ended in %d of %d eligible pulls; first "
        "death as a fraction of the pull duration, with the pull result: %s." % (
            len(died), len(eligible), ", ".join(died) or "none"),
        len(died), len(eligible), "pulls", pulls, incomplete, evidence, scope))
    known = [entry for entry in eligible
             if entry["performance"]["life"].get("alive_seconds") is not None]
    unknown = incomplete + [{"pull_id": entry["pull_id"], "reason": "life_state_unknown"}
                            for entry in eligible if entry not in known]
    dead = round(sum(entry["performance"]["life"]["dead_seconds"] for entry in known), 3)
    observed = round(dead + sum(entry["performance"]["life"]["alive_seconds"]
                                for entry in known), 3)
    observations.append(_observation(
        "%s.dead_time_share" % group_id, "observed",
        "The player was dead for %s s of %s s of observed encounter time (%s) across %d "
        "pulls." % (_fmt(dead), _fmt(observed), _percent(dead, observed), len(known)),
        dead, observed, "s", [entry["pull_id"] for entry in known], unknown, [], scope))
    gapped = [entry for entry in known
              if "gaps_total_s" in entry["performance"]["continuity"] and
              entry["performance"]["life"]["alive_seconds"]]
    gaps = round(sum(entry["performance"]["continuity"]["gaps_total_s"]
                     for entry in gapped), 3)
    alive = round(sum(entry["performance"]["life"]["alive_seconds"] for entry in gapped), 3)
    gap_evidence = [{"pull_id": entry["pull_id"],
                     "t_s": entry["performance"]["continuity"]["longest"][0]["start_s"],
                     "ref": "continuity.longest[0]"} for entry in gapped
                    if entry["performance"]["continuity"].get("longest")]
    observations.append(_observation(
        "%s.action_gap_share" % group_id, "observed",
        "Observed gaps longer than %s s between consecutive player actions add up to %s s "
        "of %s s alive (%s) across %d pulls; the cause is not determined." % (
            _fmt(ACTION_GAP_SECONDS), _fmt(gaps), _fmt(alive), _percent(gaps, alive),
            len(gapped)),
        gaps, alive, "s", [entry["pull_id"] for entry in gapped],
        unknown + [{"pull_id": entry["pull_id"], "reason": "no_alive_time"}
                   for entry in known if entry not in gapped], gap_evidence, scope))
    openers = group["opener_signatures"]
    rows = {row["length"]: row for row in openers["prefixes"]}
    main = rows[PACKET_OPENER_PREFIX]
    shorter = [{"pull_id": pull_id, "reason": "signature_shorter_than_prefix"}
               for pull_id in sorted(pull_id for row in openers["signatures"]
                                     if len(row["signature"]) < PACKET_OPENER_PREFIX
                                     for pull_id in row["pulls"])]
    first = PACKET_OPENER_PREFIXES[0]
    parts = ["the first %d casts match the most common sequence in %d of %d pulls with at "
             "least %d opener casts" % (first, rows[first]["count"],
                                        rows[first]["denominator"], first)]
    parts += ["the first %d in %d of %d" % (length, rows[length]["count"],
                                             rows[length]["denominator"])
              for length in PACKET_OPENER_PREFIXES[1:]]
    observations.append(_observation(
        "%s.opener_consistency" % group_id, "observed",
        "Among %d eligible pulls, %s; most common first-%d sequence: %s." % (
            openers["denominator"], ", ".join(parts), PACKET_OPENER_PREFIX,
            " > ".join(str(spell) for spell in main["signature"])
            if main["signature"] else "none"),
        main["count"], main["denominator"], "pulls", main["pulls"],
        openers["excluded"] + shorter,
        [{"pull_id": pull_id, "t_s": 0.0, "ref": "opener.signature"}
         for pull_id in main["pulls"]],
        "pulls of group %s eligible for opener comparison" % group_id))
    total = sum(entry["performance"]["damage"]["total_effective"] for entry in eligible)
    top_spells = [row for row in spells if row["spell_id"] is not None and
                  row["effective"] > 0][:PACKET_CONCENTRATION_SPELLS]
    top_effective = sum(row["effective"] for row in top_spells)
    observations.append(_observation(
        "%s.damage_spell_concentration" % group_id, "observed",
        "The %d spells with the most effective damage (%s) account for %s of %s effective "
        "damage (%s) across %d pulls." % (
            len(top_spells), ", ".join(str(row["spell_id"]) for row in top_spells) or
            "none", top_effective, total, _percent(top_effective, total), len(eligible)),
        top_effective, total, "damage", pulls, incomplete, [], scope))
    return observations


def _arcane_observations(group_id: str, members: list[dict]) -> list:
    applied = [entry for entry in members if entry["complete"] and entry["arcane"]]
    if not applied:
        return []
    excluded = [{"pull_id": entry["pull_id"],
                 "reason": "incomplete" if not entry["complete"] else
                 "spec_rules_not_applied"} for entry in members if entry not in applied]
    pulls = [entry["pull_id"] for entry in applied]
    scope = "complete pulls of group %s with the Arcane rules applied" % group_id
    specs = [(entry["pull_id"], entry["performance"]["spec"]) for entry in applied]
    observations = []
    firsts = [(pull_id, spec["opener"]["surge_first_success_s"]) for pull_id, spec in specs
              if spec["opener"].get("surge_first_success_s") is not None]
    stats = packet_stats([value for _, value in firsts])
    observations.append(_observation(
        "%s.surge_first_use" % group_id, "observed",
        "Arcane Surge was first cast successfully in %d of %d pulls, at a median of %s s "
        "after the pull start (range %s to %s s)." % (
            len(firsts), len(applied), _fmt(stats["median"]), _fmt(stats["min"]),
            _fmt(stats["max"])),
        len(firsts), len(applied), "pulls", pulls, excluded,
        [{"pull_id": pull_id, "t_s": value, "ref": "spec.opener.surge_first_success_s"}
         for pull_id, value in firsts], scope))
    orders = [spec["opener"].get("order") for _, spec in specs]
    counts = {order: orders.count(order) for order in
              ("surge_first", "touch_first", "same_timestamp", None)}
    observations.append(_observation(
        "%s.surge_touch_order" % group_id, "observed",
        "Arcane Surge was cast before Touch of the Magi in %d of %d pulls (Touch first: %d, "
        "same timestamp: %d, not both observed: %d)." % (
            counts["surge_first"], len(applied), counts["touch_first"],
            counts["same_timestamp"], counts[None]),
        counts["surge_first"], len(applied), "pulls", pulls, excluded, [], scope))
    burst_rows, burst_excluded = [], list(excluded)
    for entry in applied:
        spec = entry["performance"]["spec"]
        reason = _burst_exclusion(entry["performance"])
        if reason is not None:
            burst_excluded.append({"pull_id": entry["pull_id"], "reason": reason})
        elif spec["burst_windows"].get("partial"):
            burst_excluded.append({"pull_id": entry["pull_id"],
                                   "reason": "metric_partial: burst_windows"})
        else:
            burst_rows.append((entry["pull_id"], spec["burst_windows"]["windows"]))
    windows = [(pull_id, index, window) for pull_id, rows in burst_rows
               for index, window in enumerate(rows)]
    casts = sum(sum(window["casts"].values()) for _, _, window in windows)
    with_death = sum(1 for _, _, window in windows if window.get("death_inside"))
    observations.append(_observation(
        "%s.burst_window_casts" % group_id, "observed",
        "Burst windows contained %d successful casts over %d windows (%s per window); %d "
        "of those windows had a player death inside." % (
            casts, len(windows), _fmt(_ratio(casts, len(windows))), with_death),
        casts, len(windows), "casts", [pull_id for pull_id, _ in burst_rows],
        burst_excluded,
        [{"pull_id": pull_id, "t_s": window["start_s"],
          "ref": "spec.burst_windows.windows[%d]" % index}
         for pull_id, index, window in windows], scope))
    procs = [spec["procs"][0] for _, spec in specs if spec.get("procs")]
    at_max = sum(row["refreshes_at_max_stacks"] for row in procs)
    refreshes = sum(row["refreshes"] for row in procs)
    observations.append(_observation(
        "%s.clearcasting_refresh_at_max" % group_id, "observed",
        "Clearcasting was refreshed while at its maximum observed stacks %d times out of "
        "%d refreshes; the count alone does not establish a consequence." % (
            at_max, refreshes),
        at_max, refreshes, "refreshes", pulls, excluded, [], scope))
    charge_rows, charge_excluded = [], list(excluded)
    for pull_id, spec in specs:
        observed = spec["charges"]["observed"]
        if observed.get("partial"):
            charge_excluded.append({"pull_id": pull_id,
                                    "reason": "metric_partial: charges_observed"})
        else:
            charge_rows.append((pull_id, observed["by_spell"]))
    by_spell: dict = {}
    for _, rows in charge_rows:
        for row in rows:
            # [over the cap, generated = gained + over the cap]
            sums = by_spell.setdefault(row["spell_id"], [0, 0])
            sums[0] += row["over_energize"]
            sums[1] += row["gains"] + row["over_energize"]
    over = sum(sums[0] for sums in by_spell.values())
    generated = sum(sums[1] for sums in by_spell.values())
    spells = sorted(by_spell.items(), key=lambda item: (-item[1][1], _perf_sort_id(item[0])))
    observations.append(_observation(
        "%s.charge_over_energize" % group_id, "observed",
        "%s of %s generated Arcane Charges were over the cap (%s), as reported by Arcane "
        "Charge energize events; by spell (over-cap/generated): %s." % (
            _fmt(over), _fmt(generated), _percent(over, generated),
            ", ".join("%s %s/%s" % (spell, _fmt(sums[0]), _fmt(sums[1]))
                      for spell, sums in spells) or "none"),
        over, generated, "charges", [pull_id for pull_id, _ in charge_rows],
        charge_excluded, [], scope))
    histogram: dict = {}
    checks = agreed = 0
    for _, spec in specs:
        inferred = spec["charges"]["inferred"]
        for key, count in inferred["barrage_casts_by_inferred_charges"].items():
            histogram[key] = histogram.get(key, 0) + count
        rate = inferred.get("agreement_rate") or {}
        checks += rate.get("denominator") or 0
        agreed += rate.get("numerator") or 0
    unknown = histogram.get("unknown", 0)
    known = sum(count for key, count in histogram.items() if key != "unknown")
    at_four = histogram.get("4", 0)
    observations.append(_observation(
        "%s.barrage_inferred_charges" % group_id, "inferred",
        "By the inferred charge counter, %d of %d Arcane Barrage casts with a known count "
        "were at 4 charges; %d had an unknown count." % (at_four, known, unknown),
        at_four, known, "casts", pulls, excluded, [],
        "inferred from SPELL_ENERGIZE gains and Arcane Barrage casts; the counter's "
        "self-check agreed in %d of %d checks (%s)" % (agreed, checks,
                                                         _percent(agreed, checks))))
    return observations


def _pull_partial(performance: dict) -> list[str]:
    partial = []
    if performance["timeline"].get("partial"):
        partial.append("timeline")
    if performance["opener"].get("partial"):
        partial.append("opener")
    auras = performance["auras"]
    if _aura_keys_truncated(performance):
        partial.append("aura_keys")
    if any(row.get("partial") for row in auras["on_player"] + auras["from_player"]):
        partial.append("aura_intervals")
    if any(row.get("holders_truncated")
           for row in auras["on_player"] + auras["from_player"]):
        partial.append("aura_holders")
    if any(stats.get("partial")
           for stats in performance["resources"]["by_power_type"].values()):
        partial.append("resource_series")
    spec = performance["spec"]
    if _arcane_applied(performance):
        for key in ("burst_windows", "partial_windows", "touch_windows"):
            if spec[key].get("partial"):
                partial.append(key)
        # Per-spell window casts cut into `other`: the totals stay comparable.
        for key, name in (("burst_windows", "burst_window_casts"),
                          ("touch_windows", "touch_window_casts")):
            if spec[key].get("casts_partial"):
                partial.append(name)
        if spec["charges"]["observed"].get("partial"):
            partial.append("charges_observed")
    return partial


def _spec_summary(performance: dict) -> dict:
    spec = performance["spec"]
    if spec.get("status") != "applied":
        return {"status": spec.get("status"), "reason": spec.get("reason")}
    if not _arcane_applied(performance):
        return {"status": "applied", "id": spec.get("id"), "version": spec.get("version")}
    opener, procs = spec["opener"], (spec.get("procs") or [{}])[0]
    return {"status": "applied", "id": spec["id"], "version": spec["version"],
            "rules_validated_for_build": spec.get("rules_validated_for_build"),
            "surge_first_success_s": opener.get("surge_first_success_s"),
            "touch_first_success_s": opener.get("touch_first_success_s"),
            "order": opener.get("order"), "surge_precast": opener.get("surge_precast"),
            "burst_windows": spec["burst_windows"]["count"],
            "partial_windows": spec["partial_windows"]["count"],
            "touch_windows": spec["touch_windows"]["count"],
            "clearcasting": {key: procs.get(key) for key in (
                "applications", "refreshes", "refreshes_at_max_stacks", "decrements",
                "decrements_unexplained", "max_stacks_observed")},
            "charges_agreement_rate":
                spec["charges"]["inferred"]["agreement_rate"].get("value")}


def _source_ref(copy: dict) -> dict:
    performance = copy["performance"]
    source = performance.get("source") or {}
    return {"file": source.get("file"),
            "segment_id": performance["segment"].get("segment_id"),
            "segment_start_offset": source.get("segment_start_offset"),
            "encounter_start_offset": source.get("encounter_start_offset"),
            "encounter_end_offset": source.get("encounter_end_offset"),
            "published_name": copy["name"]}


def _pull_summary(entry: dict) -> dict:
    performance = entry["performance"]
    segment, damage, life = performance["segment"], performance["damage"], \
        performance["life"]
    rates, continuity = damage["rates"], performance["continuity"]
    resources = {}
    for power_type, stats in performance["resources"]["by_power_type"].items():
        resources[power_type] = {key: stats.get(key) for key in (
            "samples", "max_gap_s", "min_observed", "first", "last")}
    return {
        "pull_id": entry["pull_id"], "segment_id": segment["segment_id"],
        "group_id": entry["group_id"], "start_time": segment["start_time"],
        "boss": segment.get("boss"), "result": segment.get("result"),
        "complete": entry["complete"], "duration_s": entry["duration_s"],
        "duration_basis": segment.get("duration_basis"),
        "raid_size": segment.get("raid_size"), "effective": damage["total_effective"],
        "pets_effective": damage["pets"]["effective"],
        "absorbed_by_target": damage["player"]["absorbed_by_target"] +
        damage["pets"]["absorbed_by_target"],
        "self_damage_excluded": damage["excluded"]["self_damage"],
        "dps_encounter": (rates.get("dps_encounter") or {}).get("value"),
        "dps_while_alive": (rates.get("dps_while_alive") or {}).get("value"),
        "dps_observed": (rates.get("dps_observed") or {}).get("value"),
        "alive_at_start": life.get("alive_at_start"),
        "deaths_s": [death["t_s"] for death in life["deaths"]],
        "death_in_post_context": [death["after_end_s"]
                                  for death in life.get("death_in_post_context") or []],
        "alive_seconds": life.get("alive_seconds"),
        "casts_success": performance["casts"]["total_success"],
        "casts_per_minute": performance["casts"]["rates"]["casts_per_minute"].get("value"),
        "top_spells": _top_spells(performance, PACKET_TOP_SPELLS),
        "continuity": {key: continuity[key] for key in (
            "gaps_count", "gaps_total_s", "gap_max_s")}
        if "gaps_total_s" in continuity else None,
        "resources": resources,
        "spec": _spec_summary(performance),
        "partial": _pull_partial(performance),
        "warnings": [warning.get("code") for warning in performance.get("warnings") or []],
        "sources": [_source_ref(copy) for copy in entry["copies"]]}


def _top_spells(performance: dict, limit: int) -> list[list]:
    success = {row["spell_id"]: row["success"] for row in performance["casts"]["by_spell"]}
    rows = [[row["spell_id"], row["effective"], success.get(row["spell_id"], 0)]
            for row in performance["damage"]["by_spell"]]
    rows.sort(key=lambda row: (-row[1], _perf_sort_id(row[0])))
    return rows[:limit]


def _unavailable(entries: list[dict]) -> list[dict]:
    """Null per-pull metrics, grouped by (metric, reason)."""
    grouped: dict[tuple, list] = {}
    for entry in entries:
        performance = entry["performance"]
        rates = dict(performance["damage"]["rates"])
        rates["casts_per_minute"] = performance["casts"]["rates"]["casts_per_minute"]
        for metric in ("dps_encounter", "dps_while_alive", "dps_observed",
                       "casts_per_minute"):
            rate = rates.get(metric)
            if isinstance(rate, dict) and rate.get("value") is None:
                grouped.setdefault((metric, rate.get("reason")), []).append(
                    entry["pull_id"])
        continuity = performance["continuity"]
        if "gaps_total_s" not in continuity:
            grouped.setdefault(("continuity", continuity.get("reason")), []).append(
                entry["pull_id"])
    return [{"metric": metric, "pulls": pulls, "reason": reason}
            for (metric, reason), pulls in grouped.items()]


def _death_evidence(performance: dict) -> list[dict]:
    entries = performance["timeline"].get("entries") or []
    deaths = []
    for death in performance["life"]["deaths"]:
        death_ms = int(round(death["t_s"] * 1000))
        casts = [[t_ms, spell_id] for t_ms, kind, spell_id, _ in entries
                 if kind == "success" and t_ms <= death_ms]
        deaths.append({"t_s": death["t_s"], "last_casts": casts[-PACKET_DEATH_CASTS:]})
    return deaths


def _evidence(entries: list[dict], order: list[str]) -> dict:
    """Detail per pull, representative pulls first (`order`)."""
    by_id = {entry["pull_id"]: entry for entry in entries}
    openers, bursts, gaps, deaths = {}, {}, {}, {}
    for pull_id in order:
        performance = by_id[pull_id]["performance"]
        opener = performance["opener"]
        openers[pull_id] = {"signature": opener.get("signature") or [],
                            "entries": opener.get("entries") or []}
        if _arcane_applied(performance) and performance["spec"]["burst_windows"]["windows"]:
            bursts[pull_id] = performance["spec"]["burst_windows"]["windows"]
        if performance["continuity"].get("longest"):
            gaps[pull_id] = performance["continuity"]["longest"]
        if performance["life"]["deaths"]:
            deaths[pull_id] = _death_evidence(performance)
    return {"openers": openers, "burst_windows": bursts, "gaps": gaps, "deaths": deaths}


def _config_key(performance: dict) -> tuple:
    character = performance["character"]
    return (character.get("spec_id"), character.get("item_level"),
            (character.get("talents") or {}).get("fingerprint"),
            (character.get("equipment") or {}).get("fingerprint"),
            character.get("combatant_info"))


def _character_configs(entries: list[dict]) -> list[dict]:
    configs: dict[tuple, dict] = {}
    for entry in entries:
        character = entry["performance"]["character"]
        key = _config_key(entry["performance"])
        config = configs.get(key)
        if config is None:
            config = configs[key] = {
                "config_id": "c%d" % (len(configs) + 1),
                "combatant_info": character.get("combatant_info"),
                "spec_id": character.get("spec_id"), "class_id": character.get("class_id"),
                "item_level": character.get("item_level"),
                "item_level_basis": character.get("item_level_basis"),
                "talents_fingerprint": key[2], "equipment_fingerprint": key[3], "pulls": []}
        config["pulls"].append(entry["pull_id"])
        entry["config_id"] = config["config_id"]
    return list(configs.values())


def _game_contexts(entries: list[dict]) -> list[dict]:
    contexts: dict[tuple, dict] = {}
    for entry in entries:
        performance = entry["performance"]
        game, spec = performance.get("game") or {}, performance["rules"].get("spec")
        validated = spec.get("validated_for_build") if spec else None
        key = (game.get("build_version"), game.get("combat_log_version"),
               game.get("header_source"), validated)
        contexts.setdefault(key, {
            "build_version": key[0], "combat_log_version": key[1], "header_source": key[2],
            "rules_validated_for_build": validated, "pulls": []})["pulls"].append(
            entry["pull_id"])
    return list(contexts.values())


def _groups(entries: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
    """(groups, observations, representative pull ids in group order)."""
    by_key: dict[tuple, list[dict]] = {}
    for entry in entries:
        segment = entry["performance"]["segment"]
        key = (segment.get("encounter_id"), segment.get("difficulty_id"),
               (entry["performance"].get("game") or {}).get("build_version"),
               entry["config_id"])
        by_key.setdefault(key, []).append(entry)
    groups, observations, representatives = [], [], []
    for index, ((encounter_id, difficulty_id, build, config_id), members) in enumerate(
            by_key.items()):
        group_id = "g%d" % (index + 1)
        for entry in members:
            entry["group_id"] = group_id
        eligible = [entry for entry in members if entry["complete"]]
        arcane_metrics = any(entry["arcane"] for entry in eligible)
        metrics, pooled, dps = _group_metrics(members, arcane_metrics)
        total = sum(entry["performance"]["damage"]["total_effective"] for entry in eligible)
        spells = _group_spells(eligible, total)
        first = members[0]["performance"]["segment"]
        group = {
            "group_id": group_id, "encounter_id": encounter_id, "boss": first.get("boss"),
            "difficulty_id": difficulty_id, "difficulty": first.get("difficulty"),
            "build_version": build, "config_id": config_id,
            "pulls": [entry["pull_id"] for entry in members], "attempts": len(members),
            "kills": sum(1 for entry in members if entry["result"] == "kill"),
            "wipes": sum(1 for entry in members if entry["result"] == "wipe"),
            "incomplete": len(members) - len(eligible),
            "duration_s": packet_stats([entry["duration_s"] for entry in eligible
                                        if entry["duration_s"] is not None]),
            "metrics": metrics, "pooled": pooled,
            "common_windows": _common_windows(members),
            "opener_signatures": _opener_signatures(members),
            "spells": spells, "targets": _group_targets(eligible, total),
            "representative_pulls": _representative_pulls(members, dps)}
        groups.append(group)
        observations.extend(_general_observations(group, members, spells))
        observations.extend(_arcane_observations(group_id, members))
        representatives.extend(row["pull_id"] for row in group["representative_pulls"])
    return groups, observations, representatives


def packet_bytes(packet: dict) -> bytes:
    """Compact UTF-8 bytes; NaN/Infinity raise instead of being written."""
    return json.dumps(packet, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _fix_size(packet: dict) -> bytes:
    """Serialise with budget.actual_bytes equal to the serialised size (fixed point)."""
    budget = packet["budget"]
    data = packet_bytes(packet)
    for _ in range(16):
        if budget["actual_bytes"] == len(data):
            return data
        budget["actual_bytes"] = len(data)
        data = packet_bytes(packet)
    raise RuntimeError("packet size did not converge")


def _drop_evidence(packet: dict, sections: tuple, keep: set, reason: str) -> list[dict]:
    """Remove the evidence of every pull not in `keep`; openers keep their signature."""
    omitted = []
    for section in sections:
        evidence = packet["evidence"][section]
        dropped = []
        for pull_id in [pull_id for pull_id in evidence if pull_id not in keep]:
            if section == "openers":
                if "entries" not in evidence[pull_id]:
                    continue
                del evidence[pull_id]["entries"]
            else:
                del evidence[pull_id]
            dropped.append(pull_id)
        if dropped:
            what = "evidence.openers.entries" if section == "openers" else \
                "evidence." + section
            omitted.append({"what": what, "pulls": sorted(dropped), "reason": reason})
    return omitted


def _trim_tables(packet: dict) -> list[dict]:
    omitted = []
    for key, limit in (("spells", PACKET_BUDGET_GROUP_SPELLS),
                       ("targets", PACKET_BUDGET_GROUP_TARGETS)):
        pulls = []
        for group in packet["groups"]:
            if len(group[key]) > limit:
                del group[key][limit:]
                pulls.extend(group["pulls"])
        if pulls:
            omitted.append({"what": "groups.%s beyond the first %d rows" % (key, limit),
                            "pulls": sorted(pulls), "reason": "packet budget"})
    pulls = []
    for pull in packet["pulls"]:
        if len(pull["top_spells"]) > PACKET_BUDGET_TOP_SPELLS:
            del pull["top_spells"][PACKET_BUDGET_TOP_SPELLS:]
            pulls.append(pull["pull_id"])
    if pulls:
        omitted.append({"what": "pulls.top_spells beyond the first %d rows"
                                % PACKET_BUDGET_TOP_SPELLS,
                        "pulls": pulls, "reason": "packet budget"})
    return omitted


def _apply_budget(packet: dict, max_bytes: int, representatives: set) -> bytes:
    """The contract's fixed reduction order, step by step while over `max_bytes`."""
    budget = packet["budget"]
    other, chosen = "packet budget: non-representative pulls", \
        "packet budget: representative pulls"
    steps = (
        lambda: _drop_evidence(packet, ("burst_windows",), representatives, other),
        lambda: _drop_evidence(packet, ("openers",), representatives, other),
        lambda: _drop_evidence(packet, ("gaps", "deaths"), representatives, other),
        lambda: _trim_tables(packet),
        lambda: _drop_evidence(packet, ("burst_windows", "openers", "gaps", "deaths"),
                               set(), chosen),
    )
    data = _fix_size(packet)
    for step in steps:
        if len(data) <= max_bytes:
            break
        budget["omitted"].extend(step())
        packet["complete"] = not budget["omitted"]
        data = _fix_size(packet)
    budget["budget_exceeded"] = len(data) > max_bytes
    return _fix_size(packet)


def _session_skipped(session: dict, skipped) -> tuple[list[dict], int]:
    """(rows, omitted): the skipped packages whose start falls inside the session's
    interval widened by the gap (the pulls_without_player rule), in package-name order,
    capped at MAX_PACKET_SKIPPED. A package with no known start is in no session."""
    gap = timedelta(minutes=session["gap_minutes"])
    low, high = session["start"] - gap, session["end"] + gap
    rows = sorted(({"name": row["name"], "reason": row["reason"]} for row in skipped
                   if row.get("start") is not None and low <= row["start"] <= high),
                  key=lambda row: (row["name"], row["reason"]))
    return rows[:MAX_PACKET_SKIPPED], max(0, len(rows) - MAX_PACKET_SKIPPED)


def build_packet(session: dict, max_bytes: int, skipped=()) -> tuple[dict, bytes]:
    """(packet, bytes) of one session: diagnostic_packet.json v1, pure and deterministic."""
    pulls = session["pulls"]
    width = max(2, len(str(len(pulls))))
    entries = []
    for index, pull in enumerate(pulls):
        performance = pull["performance"]
        complete = bool(performance["segment"].get("complete"))
        duration_s = _pull_duration_s(performance)
        entries.append({
            "pull_id": "p%0*d" % (width, index + 1), "performance": performance,
            "copies": pull["copies"], "conflicts": pull["conflicts"],
            "start": pull["start"], "complete": complete,
            "result": performance["segment"].get("result"), "duration_s": duration_s,
            "short": complete and duration_s is not None and
            duration_s < MIN_COMPARABLE_SECONDS,
            "arcane": _arcane_applied(performance)})
    configs = _character_configs(entries)
    groups, observations, representatives = _groups(entries)
    first = entries[0]["performance"]
    spec_rules = next((entry["performance"]["rules"]["spec"] for entry in entries
                       if entry["performance"]["rules"].get("spec")), None)
    player = first["player"]
    # This session's own name first; else one from another pull of the GUID; else
    # null (the file name then falls back to the selector).
    name = next((entry["performance"]["player"].get("name") for entry in entries
                 if entry["performance"]["player"].get("name")),
                session.get("player_name"))
    files = sorted({str(copy["performance"].get("source", {}).get("file"))
                    for entry in entries for copy in entry["copies"]})
    order = list(dict.fromkeys(representatives)) + \
        [entry["pull_id"] for entry in entries if entry["pull_id"] not in representatives]
    spell_names: dict = {}
    for entry in entries:
        for key, value in (entry["performance"].get("spell_names") or {}).items():
            spell_names.setdefault(key, value)
    gap = session["gap_minutes"]
    skipped_rows, skipped_omitted = _session_skipped(session, skipped)
    packet = {
        "packet_schema_version": PACKET_SCHEMA_VERSION,
        "extractor_version": APP_VERSION,
        "complete": True,
        "budget": {"max_bytes": max_bytes, "actual_bytes": 0, "budget_exceeded": False,
                   "omitted": []},
        "versions": {"performance_schema_version": first["performance_schema_version"],
                     "fingerprint": first.get("fingerprint"),
                     "rules": {"general_version": first["rules"].get("general_version"),
                               "spec": None if spec_rules is None else
                               {"id": spec_rules.get("id"),
                                "version": spec_rules.get("version")}}},
        "player": {"selector": player.get("selector"), "guid": session["guid"],
                   "name": name},
        "session": {"id": session["start"].strftime("%Y-%m-%d_%H-%M-%S"),
                    "start_time": first["segment"]["start_time"],
                    "end_time": format_timestamp(session["end"]),
                    "crosses_midnight": session["start"].date() != session["end"].date(),
                    "gap_minutes": gap,
                    "policy": "Pulls of one resolved player GUID, ordered by start, stay in "
                              "one session while the gap from a pull's observed end to the "
                              "next pull's start is at most %d minutes, whatever the date "
                              "or log file." % gap,
                    "source_files": files, "pull_count": len(entries)},
        "game": _game_contexts(entries),
        "character_configs": configs,
        "definitions": PACKET_DEFINITIONS,
        "data_quality": {
            "skipped_results": skipped_rows,
            "warnings": [{"pull_id": entry["pull_id"], "code": warning.get("code"),
                          "cap": warning.get("cap"), "dropped": warning.get("dropped")}
                         for entry in entries
                         for warning in entry["performance"].get("warnings") or []],
            "unavailable": _unavailable(entries),
            "duplicates": [{"pull_id": entry["pull_id"],
                            "sources": [_source_ref(copy) for copy in entry["copies"]]}
                           for entry in entries if len(entry["copies"]) > 1],
            "duplicate_conflicts": [{"pull_id": entry["pull_id"],
                                     "fields": entry["conflicts"]}
                                    for entry in entries if entry["conflicts"]]},
        "pulls_without_player": [{
            "segment_id": result["performance"]["segment"].get("segment_id"),
            "start_time": result["performance"]["segment"].get("start_time"),
            "boss": result["performance"]["segment"].get("boss"),
            "status": result["performance"]["player"].get("status"),
            "candidates": result["performance"]["player"].get("candidates") or []}
            for result in session["without_player"]],
        "groups": groups,
        "pulls": [_pull_summary(entry) for entry in entries],
        "observations": observations,
        "evidence": _evidence(entries, order),
        "spell_names": {key: spell_names[key]
                        for key in sorted(spell_names, key=lambda key: (
                            _plain_int(key) is None, _plain_int(key) or 0, key))},
    }
    if skipped_omitted:
        packet["data_quality"]["skipped_results_omitted"] = skipped_omitted
    data = _apply_budget(packet, max_bytes, set(representatives))
    return packet, data


def packet_file_name(packet: dict) -> str:
    """<session id>_<player name>_<first 8 hex of sha1(GUID)>_diagnostic_packet.json"""
    player = packet["player"]
    return "%s_%s_%s%s" % (packet["session"]["id"],
                           sanitize_filename(player.get("name") or player.get("selector")),
                           _sha1(player["guid"].encode("utf-8"))[:8], PACKET_SUFFIX)


def _own_packet(path: str) -> dict | None:
    """The parsed packet. Only files with the packet suffix that parse as a packet are
    ours, and only those are ever deleted.

    A file that reads but does not parse as a packet is not ours and is kept; one
    that vanished is not ours. A file that cannot be inspected or read raises: it
    is never deleted, and the failure is reported instead of passing for foreign.
    """
    if not os.path.basename(path).endswith(PACKET_SUFFIX):
        return None
    mode = _stat_mode_or_none(path)
    if mode is None or not stat.S_ISREG(mode):
        return None
    data = _read_package_json(path)
    return data if isinstance(data, dict) and "packet_schema_version" in data else None


def _packet_sources(packet: dict) -> set:
    """Package names an existing packet lists in pulls[].sources[].published_name."""
    names = set()
    pulls = packet.get("pulls")
    for pull in pulls if isinstance(pulls, list) else ():
        sources = pull.get("sources") if isinstance(pull, dict) else None
        for source in sources if isinstance(sources, list) else ():
            name = source.get("published_name") if isinstance(source, dict) else None
            if isinstance(name, str):
                names.add(name)
    return names


def _read_bytes_or_none(path: str) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def rebuild_diagnostics(output_dir: str, options: OutputOptions, packet_max_bytes: int,
                        session_gap_minutes: int) -> dict:
    """Make `<output>/Diagnostics/` a function of the published performance results.

    Every new or changed packet is written first (atomically, only when its bytes
    differ); obsolete own packets are deleted only after every write succeeded. A
    failure propagates with the previous packets kept and nothing deleted.

    An existing own packet that lists a package left without a valid marker (it may
    be half republished) is held: neither overwritten nor deleted until that package
    is repaired or gone, whatever this process remembers about its publications.
    """
    results, skipped = collect_performance_results(
        os.path.join(output_dir, RAID_DIR_NAME), options.performance_fingerprint)
    sessions = build_sessions(results, session_gap_minutes)
    packets = {}
    for session in sessions:
        packet, data = build_packet(session, packet_max_bytes, skipped)
        packets[packet_file_name(packet)] = data
    incomplete = {row["name"] for row in skipped
                  if row["reason"] in INCOMPLETE_PACKAGE_REASONS}
    directory = os.path.join(output_dir, DIAGNOSTICS_DIR_NAME)
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        names = []
    # Every existing packet is inspected before the first write or delete: a failed
    # inspection writes and deletes nothing.
    own, held = [], set()
    for name in names:
        packet = _own_packet(os.path.join(directory, name))
        if packet is None:
            continue
        own.append(name)
        if _packet_sources(packet) & incomplete:
            held.add(name)
    written = unchanged = deleted = 0
    for name in sorted(packets):
        if name in held:
            continue
        path = os.path.join(directory, name)
        if _read_bytes_or_none(path) == packets[name]:
            unchanged += 1
            continue
        _atomic_write_bytes(path, packets[name])
        written += 1
    for name in own:
        if name in packets or name in held:
            continue
        try:
            os.remove(os.path.join(directory, name))
        except FileNotFoundError:
            continue
        deleted += 1
    statuses = [result["performance"]["player"].get("status") for result in results]
    return {"written": written, "unchanged": unchanged, "deleted": deleted,
            "held": len(held), "sessions": len(sessions), "pulls": len(results),
            "resolved": statuses.count("resolved"), "absent": statuses.count("absent"),
            "ambiguous": statuses.count("ambiguous"), "failed": statuses.count("error"),
            "skipped": len(skipped)}


def diagnostics_summary_line(summary: dict) -> str:
    line = "Diagnostics: %d packet(s) written, %d unchanged, %d removed; player resolved " \
           "in %d of %d pulls" % (summary["written"], summary["unchanged"],
                                  summary["deleted"], summary["resolved"], summary["pulls"])
    if summary["resolved"] < summary["pulls"]:
        line += ": %d absent, %d ambiguous, %d failed" % (
            summary["absent"], summary["ambiguous"], summary["failed"])
    if summary["skipped"]:
        line += "; %d published result(s) skipped" % summary["skipped"]
    if summary["held"]:
        line += "; %d packet(s) kept: a pull is being republished" % summary["held"]
    return line


# --- orchestration ----------------------------------------------------------------

class Extractor:
    """Ties config paths, state and publication together."""

    def __init__(self, log_dir: str, output_dir: str, state_path: str | None = None,
                 verbose: bool = True,
                 output_options: OutputOptions | None = None,
                 packet_max_bytes: int = DEFAULT_PACKET_MAX_BYTES,
                 session_gap_minutes: int = DEFAULT_SESSION_GAP_MINUTES):
        if packet_max_bytes <= 0 or session_gap_minutes <= 0:
            raise ValueError("packet_max_bytes and session_gap_minutes must be positive")
        self.log_dir = os.path.abspath(log_dir)
        self.output_options = output_options or OutputOptions()
        # Packet-only settings: not part of the profile (changing them never
        # reprocesses a log).
        self.packet_max_bytes = packet_max_bytes
        self.session_gap_minutes = session_gap_minutes
        self.publisher = SegmentPublisher(output_dir, verbose=verbose,
                                          output_options=self.output_options)
        self.state = StateStore(state_path or
                                os.path.join(self.publisher.output_dir, STATE_FILENAME),
                                profile=self.output_options.profile)
        self.output_lock = OutputLock(self.publisher.output_dir)
        self.verbose = verbose
        # Only a successful rebuild clears it; --watch never rebuilds and leaves it set.
        self.diagnostics_dirty = True
        self.diagnostics_summary: dict | None = None

    @property
    def diagnostics_dir(self) -> str:
        return os.path.join(self.publisher.output_dir, DIAGNOSTICS_DIR_NAME)

    def prepare(self, reset_state: bool = False) -> None:
        with self.output_lock:
            self.publisher.ensure_dirs()
            self.state.load()
            if reset_state:
                self.state.reset()
                self.state.save()
            self.publisher.cleanup_partials()
            if self.output_options.performance_player is not None:
                self._cleanup_diagnostics_temps()

    def _cleanup_diagnostics_temps(self) -> None:
        """Crash leftovers of _atomic_write_bytes for a packet; nothing else is touched."""
        try:
            entries = os.listdir(self.diagnostics_dir)
        except OSError:
            return
        for entry in entries:
            if entry.startswith(".") and entry.endswith(".tmp") and PACKET_SUFFIX in entry:
                try:
                    os.remove(os.path.join(self.diagnostics_dir, entry))
                except OSError:
                    pass

    def _rebuild_diagnostics(self) -> int:
        """Rebuild the packets under --performance-player; returns the error count."""
        if self.output_options.performance_player is None:
            return 0
        try:
            self.diagnostics_summary = rebuild_diagnostics(
                self.publisher.output_dir, self.output_options, self.packet_max_bytes,
                self.session_gap_minutes)
        except Exception as exc:
            self.diagnostics_dirty = True
            safe_print("  ! error rebuilding diagnostics: %s" % exc)
            if os.environ.get("WOWLOGEXTRACTOR_DEBUG"):
                traceback.print_exc()
            return 1
        self.diagnostics_dirty = False
        return 0

    def _rebuild_unless_errors(self, processing_errors: int) -> int:
        """Rebuild only after error-free processing; returns the rebuild's error count.

        A processing error can leave a package half republished (its marker retired
        first). rebuild_diagnostics already holds the packets that list such a package;
        this is a second guard: Diagnostics/ is left exactly as it is and stays dirty
        until an error-free run.
        """
        if self.output_options.performance_player is None:
            return 0
        if processing_errors:
            self.diagnostics_dirty = True
            safe_print("  ! diagnostics not rebuilt because of %d processing error(s)"
                       % processing_errors)
            return 0
        return self._rebuild_diagnostics()

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

    def _new_processor(self, path: str) -> FileProcessor:
        offset = self.state.get_offset(path)
        header, source = None, "unknown"
        if self.output_options.performance_player is not None and offset > 0:
            # At offset 0 the stream itself provides the header.
            header = self.state.get_log_header(path)
            source = "state"
            if header is None:
                header = read_log_header_at_start(path)
                source = "file_start" if header is not None else "unknown"
        return FileProcessor(path, self.publisher, offset, log_header=header,
                             log_header_source=source)

    def _commit_header(self, processor: FileProcessor) -> dict | None:
        # Only performance profiles store the header: other state files stay as-is.
        if self.output_options.performance_player is None:
            return None
        return processor.tracker.log_header

    def run_once(self) -> tuple[int, int, int]:
        with self.output_lock:
            return self._run_once()

    def _run_once(self) -> tuple[int, int, int]:
        paths = self.list_logs()
        latest = self._latest(paths)
        mplus_total = raid_total = errors = 0
        for path in paths:
            try:
                processor = self._new_processor(path)
                if self.state.claim(path):
                    self.state.save()
                processor.process_new_data()
                processor.finish(is_latest=(path == latest))
                # Outputs are published before the offset advances.
                self.state.update(path, processor.commit_offset(),
                                  self._commit_header(processor))
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
        # Always after error-free processing, even when nothing was published: the
        # packets are a function of the published results, so a missing or stale
        # packet is repaired here.
        errors += self._rebuild_unless_errors(errors)
        return mplus_total, raid_total, errors

    def watch(self, interval: float = WATCH_INTERVAL,
              max_polls: int | None = None) -> tuple[int, int, int]:
        """max_polls is a test hook: stop after N polls instead of running forever."""
        with self.output_lock:
            return self._watch(interval, max_polls)

    @staticmethod
    def _release_worker(worker: FileProcessor | None) -> None:
        """Drop a failed worker's open segment (handle, .partial, staging)."""
        if worker is None:
            return
        try:
            worker.tracker.drop_open_segment()
        except Exception:
            pass

    def _watch(self, interval: float, max_polls: int | None) -> tuple[int, int, int]:
        workers: dict[str, FileProcessor] = {}
        mplus_total = raid_total = errors = 0
        polls = 0
        active: str | None = None   # log whose worker was running when Ctrl+C arrived
        # --watch publishes performance.json per pull but never rebuilds Diagnostics/:
        # a run without --watch does (see rebuild_diagnostics).
        self.diagnostics_dirty = True
        self.diagnostics_summary = None
        safe_print("Watching %s (Ctrl+C to stop)..." % self.log_dir)
        try:
            while max_polls is None or polls < max_polls:
                polls += 1
                paths = self.list_logs()
                latest = self._latest(paths)
                for path in paths:
                    name = os.path.basename(path)
                    # Ctrl+C can land between any two statements, so a worker is
                    # never judged healthy after the fact: the log named here is the
                    # one whose worker may be half-updated when the loop is left.
                    active = name
                    try:
                        worker = workers.get(name)
                        if worker is not None and worker.identity_changed():
                            # The bytes we already consumed no longer match the file:
                            # it was replaced (or truncated and regrown) between polls.
                            # Salvage what already saw its END (the source bytes are
                            # gone for good), then restart from the top.
                            worker.shutdown()
                            worker.tracker.drop_open_segment()
                            mplus, raid = worker.take_counts()
                            mplus_total += mplus
                            raid_total += raid
                            workers.pop(name, None)
                            worker = None
                        if worker is None:
                            worker = self._new_processor(path)
                            if self.state.claim(path):
                                self.state.save()
                            workers[name] = worker
                        worker.process_new_data()
                        if path != latest:
                            # Rotated away: drain it and apply its EOF rules.
                            worker.finish(is_latest=False)
                        self.state.update(path, worker.commit_offset(),
                                          self._commit_header(worker))
                        mplus, raid = worker.take_counts()
                        mplus_total += mplus
                        raid_total += raid
                    except Exception as exc:
                        errors += 1
                        safe_print("  ! error processing %s: %s" % (name, exc))
                        # A worker that raised may have consumed data whose segment
                        # was never published (the offset advances before the EOF
                        # close): it is dropped uncommitted and uncounted, and the
                        # next poll replays from the committed offset.
                        self._release_worker(workers.pop(name, None))
                    active = None
                # A worker whose log vanished (deleted/moved) is finalized and dropped
                # instead of erroring on every poll. It stays registered until then:
                # it may hold the only copy of a pull, which the shared shutdown can
                # still publish if Ctrl+C arrives first.
                current = {os.path.basename(p) for p in paths}
                for name in [n for n in workers if n not in current]:
                    active = name
                    worker = workers[name]
                    try:
                        worker.finish(is_latest=False)
                        mplus, raid = worker.take_counts()
                        mplus_total += mplus
                        raid_total += raid
                    except Exception as exc:
                        errors += 1
                        safe_print("  ! error finalizing %s: %s" % (name, exc))
                        self._release_worker(worker)
                    workers.pop(name, None)
                    active = None
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
                # Only after the shutdown published: a failed close leaves the offset
                # at its last commit, so the next run replays that segment.
                # The worker Ctrl+C caught mid-poll publishes (same names on replay)
                # but never checkpoints: its offset, tracker and source file may no
                # longer describe each other. Nor does one whose log was replaced
                # since its last poll: its offset belongs to the previous contents.
                if name != active and not worker.identity_changed():
                    self.state.update(worker.path, worker.commit_offset(),
                                      self._commit_header(worker))
                mplus, raid = worker.take_counts()
                mplus_total += mplus
                raid_total += raid
            except Exception as exc:
                errors += 1
                safe_print("  ! error finalizing %s: %s" % (name, exc))
                self._release_worker(worker)
        self.state.save()
        if self.output_options.performance_player is not None:
            safe_print("Diagnostics: not rebuilt in --watch mode; run once without "
                       "--watch to rebuild the packets")
        return mplus_total, raid_total, errors


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a positive integer, got %r" % text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0, got %d" % value)
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Extract Mythic+ runs and raid boss pulls from WoW Retail combat logs.")
    parser.add_argument("--watch", action="store_true",
                        help="keep running and process new lines as WoW writes them")
    analysis_group = parser.add_mutually_exclusive_group()
    analysis_group.add_argument("--analysis", action="store_true",
                                help="publish the full log and a compact analysis package")
    analysis_group.add_argument("--analysis-only", dest="analysis_only",
                                action="store_true",
                                help="publish analysis without creating a new full log")
    parser.add_argument("--gzip", action="store_true",
                        help="store requested full/combat bodies as deterministic gzip")
    parser.add_argument("--bundle", action="store_true",
                        help="also create an analysis-only ZIP (requires an analysis mode)")
    parser.add_argument("--keep-player-damage", dest="keep_player_damage",
                        action="store_true",
                        help="keep outgoing player/pet damage lines in combat.txt "
                             "(requires an analysis mode)")
    parser.add_argument("--performance-player", dest="performance_player",
                        metavar="SELECTOR", default=None,
                        help="per-pull raid performance and a session diagnostic packet "
                             "for one player: Player-GUID, Name-Realm-Region or Name "
                             "(requires an analysis mode)")
    parser.add_argument("--packet-max-bytes", dest="packet_max_bytes", metavar="N",
                        type=_positive_int, default=DEFAULT_PACKET_MAX_BYTES,
                        help="size budget of each diagnostic packet (default: %(default)s)")
    parser.add_argument("--session-gap-minutes", dest="session_gap_minutes", metavar="N",
                        type=_positive_int, default=DEFAULT_SESSION_GAP_MINUTES,
                        help="largest gap between pulls of one session "
                             "(default: %(default)s)")
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
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        options = OutputOptions(analysis=args.analysis, analysis_only=args.analysis_only,
                                gzip=args.gzip, bundle=args.bundle,
                                keep_player_damage=args.keep_player_damage,
                                performance_player=args.performance_player)
    except ValueError as exc:
        parser.error(str(exc))
    log_dir, output_dir = resolve_paths(args.log_dir, args.output, args.config,
                                        args.reconfigure)
    extractor = Extractor(log_dir, output_dir, output_options=options,
                          packet_max_bytes=args.packet_max_bytes,
                          session_gap_minutes=args.session_gap_minutes)
    with extractor.output_lock:
        extractor.prepare(reset_state=args.reset_state)
        safe_print("Logs:   %s" % log_dir)
        safe_print("Output: %s" % output_dir)
        if args.watch:
            mplus, raid, errors = extractor.watch()
        else:
            mplus, raid, errors = extractor.run_once()
    safe_print("Processed: %d Mythic+ runs, %d raid pulls, %d errors" % (mplus, raid, errors))
    if extractor.diagnostics_summary is not None:
        safe_print(diagnostics_summary_line(extractor.diagnostics_summary))
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
