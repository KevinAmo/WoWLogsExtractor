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

    def __post_init__(self) -> None:
        if self.analysis and self.analysis_only:
            raise ValueError("--analysis and --analysis-only are mutually exclusive")
        if self.bundle and not self.wants_analysis:
            raise ValueError("--bundle requires --analysis or --analysis-only")
        if self.keep_player_damage and not self.wants_analysis:
            raise ValueError("--keep-player-damage requires --analysis or --analysis-only")

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
        return "+".join(parts)

    def as_dict(self) -> dict:
        return {"full": self.wants_full, "analysis": self.wants_analysis,
                "gzip": self.gzip, "bundle": self.bundle,
                "keep_player_damage": self.keep_player_damage,
                "profile": self.profile}


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

    def __init__(self, stage_dir: str, kind: str, keep_player_damage: bool = False):
        self.stage_dir = stage_dir
        self.kind = kind
        self.keep_player_damage = keep_player_damage
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
                event: str | None, args: list[str]) -> None:
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
            self.analysis_session = AnalysisSession(
                stage_dir, self.kind,
                keep_player_damage=self.output_options.keep_player_damage)

    def write(self, raw: bytes, timestamp: datetime | None = None,
              event: str | None = None, args: list[str] | None = None) -> None:
        if self._handle is not None:
            self._handle.write(raw)
        if self.analysis_session is not None:
            self.analysis_session.consume(raw, timestamp, event, args or [])
        self.lines += 1
        self.raw_bytes += len(raw)

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
                # Every profile publishes into the same analysis folder, replacing
                # the payload one file at a time. Retire the previous marker (and the
                # other container's combat body) first: a crash mid-replacement must
                # leave an obviously incomplete package, never a marker still
                # advertising the profile being overwritten. State has not advanced,
                # so the next run of this profile republishes over the same names.
                for obsolete in ("metadata.json",
                                 "combat.txt" if self.options.gzip else "combat.txt.gz"):
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
        os.makedirs(self.publisher.directory_for(segment.kind), exist_ok=True)
        stage_dir = None
        if not self.publisher.options.is_legacy_default:
            stage_dir = self.publisher.stage_dir(segment.kind, segment.segment_id)
        partial_path = self.publisher.partial_path(
            segment.kind, segment.name_core(False), segment.segment_id) or None
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

    def get_offset(self, path: str) -> int:
        entry = self._profile_entry(path)
        if entry is None:
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

    def update(self, path: str, offset: int) -> None:
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


# --- orchestration ----------------------------------------------------------------

class Extractor:
    """Ties config paths, state and publication together."""

    def __init__(self, log_dir: str, output_dir: str, state_path: str | None = None,
                 verbose: bool = True,
                 output_options: OutputOptions | None = None):
        self.log_dir = os.path.abspath(log_dir)
        self.output_options = output_options or OutputOptions()
        self.publisher = SegmentPublisher(output_dir, verbose=verbose,
                                          output_options=self.output_options)
        self.state = StateStore(state_path or
                                os.path.join(self.publisher.output_dir, STATE_FILENAME),
                                profile=self.output_options.profile)
        self.output_lock = OutputLock(self.publisher.output_dir)
        self.verbose = verbose

    def prepare(self, reset_state: bool = False) -> None:
        with self.output_lock:
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
        with self.output_lock:
            return self._run_once()

    def _run_once(self) -> tuple[int, int, int]:
        paths = self.list_logs()
        latest = self._latest(paths)
        mplus_total = raid_total = errors = 0
        for path in paths:
            try:
                processor = FileProcessor(path, self.publisher,
                                          self.state.get_offset(path))
                if self.state.claim(path):
                    self.state.save()
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
        with self.output_lock:
            return self._watch(interval, max_polls)

    def _watch(self, interval: float, max_polls: int | None) -> tuple[int, int, int]:
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
                            if self.state.claim(path):
                                self.state.save()
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
                                keep_player_damage=args.keep_player_damage)
    except ValueError as exc:
        parser.error(str(exc))
    log_dir, output_dir = resolve_paths(args.log_dir, args.output, args.config,
                                        args.reconfigure)
    extractor = Extractor(log_dir, output_dir, output_options=options)
    with extractor.output_lock:
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
