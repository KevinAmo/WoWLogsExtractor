#!/usr/bin/env python3
"""Unit tests for WoWLogExtractor.

Stdlib unittest only. Run from the repo root with:

    python -m unittest discover -s WoWLogExtractor/tests -v

Every test runs inside a tempfile.TemporaryDirectory() sandbox (fake log dir,
output dir and state.json path) -- nothing here touches the real script
directory, config.json, or D:\\BattleNet.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
import gzip
import zipfile
from datetime import datetime, timedelta
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PACKAGE_DIR = os.path.dirname(TESTS_DIR)
if PACKAGE_DIR not in sys.path:
    sys.path.insert(0, PACKAGE_DIR)

import WoWLogExtractor as wle  # noqa: E402


# --- synthetic log construction helpers --------------------------------------------

def ts_str(dt: datetime) -> str:
    """Render a datetime as 'M/D/YYYY HH:MM:SS.ffffff' (6-digit fraction, exact)."""
    return "%d/%d/%d %02d:%02d:%02d.%06d" % (
        dt.month, dt.day, dt.year, dt.hour, dt.minute, dt.second, dt.microsecond)


def q(value: str) -> str:
    return '"%s"' % value


def line_bytes(dt: datetime, event: str, *fields: str) -> bytes:
    payload = ",".join(fields)
    text = "%s  %s,%s" % (ts_str(dt), event, payload) if fields else "%s  %s" % (ts_str(dt), event)
    return text.encode("utf-8") + b"\r\n"


class LogBuilder:
    """Accumulates synthetic combat-log bytes and remembers exact byte offsets."""

    def __init__(self):
        self._chunks: list[bytes] = []
        self.offset = 0
        self.marks: dict[str, tuple[int, int]] = {}

    def add(self, dt: datetime, event: str, *fields: str, mark: str | None = None) -> bytes:
        raw = line_bytes(dt, event, *fields)
        return self.add_raw(raw, mark=mark)

    def add_raw(self, raw: bytes, mark: str | None = None) -> bytes:
        start = self.offset
        self._chunks.append(raw)
        self.offset += len(raw)
        if mark is not None:
            self.marks[mark] = (start, self.offset)
        return raw

    def data(self) -> bytes:
        return b"".join(self._chunks)


LOG_NAME = "WoWCombatLog-083026_100000.txt"


class ExtractorTestCase(unittest.TestCase):
    """Base class: a fresh temp sandbox (log dir + output dir + state path) per test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.log_dir = os.path.join(self.root, "Logs")
        self.output_dir = os.path.join(self.root, "Output")
        os.makedirs(self.log_dir, exist_ok=True)
        self.state_path = os.path.join(self.output_dir, wle.STATE_FILENAME)

    def log_path(self, name: str = LOG_NAME) -> str:
        return os.path.join(self.log_dir, name)

    def write_log(self, data: bytes, name: str = LOG_NAME) -> str:
        path = self.log_path(name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def append_log(self, data: bytes, name: str = LOG_NAME) -> str:
        path = self.log_path(name)
        with open(path, "ab") as handle:
            handle.write(data)
        return path

    def make_extractor(self, output_options=None) -> "wle.Extractor":
        return wle.Extractor(self.log_dir, self.output_dir, state_path=self.state_path,
                              verbose=False, output_options=output_options)

    def mplus_dir(self) -> str:
        return os.path.join(self.output_dir, wle.MPLUS_DIR_NAME)

    def raids_dir(self) -> str:
        return os.path.join(self.output_dir, wle.RAID_DIR_NAME)

    def list_outputs(self, directory: str) -> list[str]:
        try:
            return sorted(os.listdir(directory))
        except OSError:
            return []

    def read_json(self, path: str) -> dict:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def assertNoTxtOrJson(self, directory: str):
        outputs = self.list_outputs(directory)
        self.assertFalse(any(f.endswith(".txt") for f in outputs),
                         "unexpected .txt in %r: %r" % (directory, outputs))
        self.assertFalse(any(f.endswith(".json") for f in outputs),
                         "unexpected .json in %r: %r" % (directory, outputs))


# --- 1: helper-function unit tests ---------------------------------------------------

class HelperFunctionTests(unittest.TestCase):

    def test_split_args_quoted_comma(self):
        parts = wle.split_args('"Council, Ascended",8,5,2859')
        self.assertEqual(parts, ['"Council, Ascended"', "8", "5", "2859"])

    def test_split_args_bracket_list_stays_one_field(self):
        parts = wle.split_args('"Valle Cegador",2859,584,10,[158,9,10]')
        self.assertEqual(parts, ['"Valle Cegador"', "2859", "584", "10", "[158,9,10]"])

    def test_split_args_plain_csv(self):
        self.assertEqual(wle.split_args("a,b,c"), ["a", "b", "c"])

    def test_split_args_empty_field(self):
        self.assertEqual(wle.split_args('"",1,2'), ['""', "1", "2"])

    def test_parse_timestamp_exact_microseconds(self):
        dt = wle.parse_timestamp("8/30/2026 10:23:24.468100", 2026)
        self.assertEqual(dt, datetime(2026, 8, 30, 10, 23, 24, 468100))

    def test_parse_timestamp_four_digit_fraction_like_real_logs(self):
        # Real logs show 4-digit fractions, e.g. ".4681" -> right-padded to
        # microseconds, i.e. 0.4681s, NOT 0.0004681s.
        dt = wle.parse_timestamp("8/30/2026 10:23:24.4681", 2026)
        self.assertEqual(dt, datetime(2026, 8, 30, 10, 23, 24, 468100))

    def test_parse_timestamp_invalid_returns_none(self):
        self.assertIsNone(wle.parse_timestamp("not a timestamp", 2026))

    def test_parse_line_full(self):
        text = ('8/30/2026 10:25:24.475100  CHALLENGE_MODE_START,'
                '"Valle Cegador",2859,584,10,[158,9,10]')
        ts, event, args = wle.parse_line(text, 2026)
        self.assertEqual(ts, datetime(2026, 8, 30, 10, 25, 24, 475100))
        self.assertEqual(event, "CHALLENGE_MODE_START")
        self.assertEqual(args, ['"Valle Cegador"', "2859", "584", "10", "[158,9,10]"])

    def test_parse_line_no_double_space_is_unparseable(self):
        ts, event, args = wle.parse_line("garbage single space,line", 2026)
        self.assertIsNone(ts)
        self.assertIsNone(event)
        self.assertEqual(args, [])

    def test_sanitize_filename_strips_invalid_windows_chars_and_spaces(self):
        result = wle.sanitize_filename('Boss/Test: Name*?"<>|Ok')
        self.assertEqual(result, "BossTest-NameOk")

    def test_sanitize_filename_keeps_unicode(self):
        result = wle.sanitize_filename("Трактирщик josé's")
        self.assertEqual(result, "Трактирщик-josé's")

    def test_sanitize_filename_empty_falls_back(self):
        self.assertEqual(wle.sanitize_filename(""), "Unknown")
        self.assertEqual(wle.sanitize_filename(None), "Unknown")

    def test_difficulty_name_known_ids(self):
        self.assertEqual(wle.difficulty_name(14), "Normal")
        self.assertEqual(wle.difficulty_name(15), "Heroic")
        self.assertEqual(wle.difficulty_name(16), "Mythic")

    def test_difficulty_name_unknown_id_falls_back(self):
        self.assertEqual(wle.difficulty_name(99), "Difficulty99")

    def test_difficulty_name_none(self):
        self.assertEqual(wle.difficulty_name(None), "Unknown")


# --- 2: complete Mythic+ run: full metadata schema + filename ------------------------

class CompleteMPlusRunTests(ExtractorTestCase):

    def test_complete_mplus_run_produces_one_file_matching_schema(self):
        builder = LogBuilder()
        start = datetime(2026, 8, 30, 10, 25, 24)
        builder.add(start - timedelta(seconds=5), "SPELL_CAST_SUCCESS",
                    "Player-1234-ABCD", q("Pull"))
        builder.add(start, "CHALLENGE_MODE_START", q("Valle Cegador"), "2859", "584", "10",
                    "[158,9,10]")
        enc_start = start + timedelta(seconds=64)
        builder.add(enc_start, "ENCOUNTER_START", "3199",
                    q("Trinidad de floración de Luz"), "8", "5", "2859")
        enc_end = enc_start + timedelta(seconds=133)
        builder.add(enc_end, "ENCOUNTER_END", "3199", q("Trinidad de floración de Luz"),
                    "8", "5", "1", "132566")
        end = start + timedelta(seconds=1960)
        builder.add(end, "CHALLENGE_MODE_END", "2859", "1", "10", "1960162",
                    "301.663300", "2205.470703")
        builder.add(end + timedelta(seconds=3), "SPELL_CAST_SUCCESS",
                    "Player-1234-ABCD", q("Loot"))
        self.write_log(builder.data())

        extractor = self.make_extractor()
        extractor.prepare()
        mplus, raid, errors = extractor.run_once()
        self.assertEqual((mplus, raid, errors), (1, 0, 0))

        txts = [f for f in self.list_outputs(self.mplus_dir()) if f.endswith(".txt")]
        jsons = [f for f in self.list_outputs(self.mplus_dir()) if f.endswith(".json")]
        self.assertEqual(len(txts), 1)
        self.assertEqual(len(jsons), 1)
        self.assertEqual(txts[0], "2026-08-30_10-25_MPlus_Valle-Cegador_+10.txt")
        self.assertEqual(jsons[0], "2026-08-30_10-25_MPlus_Valle-Cegador_+10.json")

        meta = self.read_json(os.path.join(self.mplus_dir(), jsons[0]))
        self.assertEqual(meta["type"], "mythic_plus")
        self.assertEqual(meta["dungeon"], "Valle Cegador")
        self.assertEqual(meta["map_id"], 2859)
        self.assertEqual(meta["challenge_mode_id"], 584)
        self.assertEqual(meta["key_level"], 10)
        self.assertEqual(meta["affixes"], [158, 9, 10])
        self.assertTrue(meta["complete"])
        self.assertTrue(meta["completed"])
        self.assertEqual(meta["duration_ms"], 1960162)
        self.assertEqual(meta["date"], "2026-08-30")
        self.assertEqual(meta["context_seconds"], wle.CONTEXT_SECONDS)
        self.assertEqual(meta["source_file"], LOG_NAME)
        self.assertIsInstance(meta["lines"], int)
        self.assertGreater(meta["lines"], 0)
        self.assertEqual(len(meta["bosses"]), 1)
        self.assertEqual(meta["bosses"][0]["encounter_id"], 3199)
        self.assertEqual(meta["bosses"][0]["boss"], "Trinidad de floración de Luz")
        self.assertTrue(meta["bosses"][0]["success"])
        self.assertIn("segment_id", meta)
        self.assertTrue(meta["segment_id"])


# --- 3: two consecutive Mythic+ runs --------------------------------------------------

class TwoConsecutiveMPlusTests(ExtractorTestCase):

    def test_two_consecutive_mplus_runs_produce_two_files(self):
        builder = LogBuilder()
        start1 = datetime(2026, 8, 30, 9, 0, 0)
        builder.add(start1, "CHALLENGE_MODE_START", q("Sala de Ejecución"), "3001", "300",
                    "8", "[9]")
        end1 = start1 + timedelta(minutes=25)
        builder.add(end1, "CHALLENGE_MODE_END", "3001", "1", "8", "1500000", "0.0", "0.0")

        start2 = start1 + timedelta(minutes=30)
        builder.add(start2, "CHALLENGE_MODE_START", q("Los Rescoldos"), "3002", "301", "9",
                    "[9,10]")
        end2 = start2 + timedelta(minutes=20)
        builder.add(end2, "CHALLENGE_MODE_END", "3002", "1", "9", "1200000", "0.0", "0.0")
        self.write_log(builder.data())

        extractor = self.make_extractor()
        extractor.prepare()
        mplus, raid, errors = extractor.run_once()
        self.assertEqual((mplus, raid, errors), (2, 0, 0))

        txts = [f for f in self.list_outputs(self.mplus_dir()) if f.endswith(".txt")]
        self.assertEqual(len(txts), 2)
        self.assertTrue(any("Sala-de-Ejecución" in f for f in txts))
        self.assertTrue(any("Los-Rescoldos" in f for f in txts))


# --- 4: raid wipe / kill + difficulty mapping -----------------------------------------

class RaidWipeKillTests(ExtractorTestCase):

    def test_wipe_and_kill_map_difficulty_and_suffix(self):
        builder = LogBuilder()
        wipe_start = datetime(2026, 8, 30, 20, 0, 0)
        builder.add(wipe_start, "ENCOUNTER_START", "2600", q("Ulgrax the Devourer"), "15",
                    "20", "2657")
        wipe_end = wipe_start + timedelta(seconds=90)
        builder.add(wipe_end, "ENCOUNTER_END", "2600", q("Ulgrax the Devourer"), "15", "20",
                    "0", "90000")

        kill_start = wipe_start + timedelta(minutes=5)
        builder.add(kill_start, "ENCOUNTER_START", "2600", q("Ulgrax the Devourer"), "15",
                    "20", "2657")
        kill_end = kill_start + timedelta(seconds=210)
        builder.add(kill_end, "ENCOUNTER_END", "2600", q("Ulgrax the Devourer"), "15", "20",
                    "1", "210000")
        self.write_log(builder.data())

        extractor = self.make_extractor()
        extractor.prepare()
        mplus, raid, errors = extractor.run_once()
        self.assertEqual((mplus, raid, errors), (0, 2, 0))

        jsons = sorted(f for f in self.list_outputs(self.raids_dir()) if f.endswith(".json"))
        self.assertEqual(len(jsons), 2)
        metas = [self.read_json(os.path.join(self.raids_dir(), f)) for f in jsons]
        wipe_meta = next(m for m in metas if m["success"] is False)
        kill_meta = next(m for m in metas if m["success"] is True)
        self.assertEqual(wipe_meta["difficulty"], "Heroic")
        self.assertEqual(wipe_meta["difficulty_id"], 15)
        self.assertEqual(kill_meta["difficulty"], "Heroic")
        self.assertTrue(wipe_meta["complete"])
        self.assertTrue(kill_meta["complete"])

        txts = sorted(f for f in self.list_outputs(self.raids_dir()) if f.endswith(".txt"))
        self.assertTrue(any(name.endswith("_Wipe.txt") for name in txts))
        self.assertTrue(any(name.endswith("_Kill.txt") for name in txts))


# --- 5: multiple pulls of the same boss, incl. same-minute collision -----------------

class SameMinuteCollisionTests(ExtractorTestCase):

    def _build(self):
        builder = LogBuilder()
        start1 = datetime(2026, 8, 30, 12, 10, 5)
        builder.add(start1, "ENCOUNTER_START", "2700", q("Test Boss"), "14", "20", "999")
        end1 = start1 + timedelta(seconds=10)
        builder.add(end1, "ENCOUNTER_END", "2700", q("Test Boss"), "14", "20", "0", "10000")

        start2 = datetime(2026, 8, 30, 12, 10, 40)
        builder.add(start2, "ENCOUNTER_START", "2700", q("Test Boss"), "14", "20", "999")
        end2 = start2 + timedelta(seconds=10)
        builder.add(end2, "ENCOUNTER_END", "2700", q("Test Boss"), "14", "20", "0", "10000")
        return builder

    def test_two_pulls_same_minute_get_distinct_stable_names(self):
        builder = self._build()
        self.write_log(builder.data())

        extractor = self.make_extractor()
        extractor.prepare()
        mplus, raid, errors = extractor.run_once()
        self.assertEqual((mplus, raid, errors), (0, 2, 0))

        name1 = "2026-08-30_12-10_Raid_Test-Boss_Normal_Wipe"
        name2 = "2026-08-30_12-10-40_Raid_Test-Boss_Normal_Wipe"
        txts = set(f for f in self.list_outputs(self.raids_dir()) if f.endswith(".txt"))
        jsons = set(f for f in self.list_outputs(self.raids_dir()) if f.endswith(".json"))
        self.assertEqual(txts, {name1 + ".txt", name2 + ".txt"})
        self.assertEqual(jsons, {name1 + ".json", name2 + ".json"})

        # snapshot before an immediate no-op re-run
        snapshot = {}
        for name in sorted(txts | jsons):
            full = os.path.join(self.raids_dir(), name)
            with open(full, "rb") as handle:
                snapshot[name] = (os.path.getmtime(full), handle.read())

        result2 = extractor.run_once()
        self.assertEqual(result2, (0, 0, 0))
        for name, (mtime, content) in snapshot.items():
            full = os.path.join(self.raids_dir(), name)
            self.assertEqual(os.path.getmtime(full), mtime, "mtime changed for %s" % name)
            with open(full, "rb") as handle:
                self.assertEqual(handle.read(), content, "content changed for %s" % name)

        # --reset-state: full reprocess, but names stay stable, no duplicates appear
        extractor.prepare(reset_state=True)
        mplus3, raid3, errors3 = extractor.run_once()
        self.assertEqual((mplus3, raid3, errors3), (0, 2, 0))
        txts_after_reset = set(f for f in self.list_outputs(self.raids_dir())
                               if f.endswith(".txt"))
        jsons_after_reset = set(f for f in self.list_outputs(self.raids_dir())
                                if f.endswith(".json"))
        self.assertEqual(txts_after_reset, txts)
        self.assertEqual(jsons_after_reset, jsons)
        for name in txts | jsons:
            full = os.path.join(self.raids_dir(), name)
            with open(full, "rb") as handle:
                self.assertEqual(handle.read(), snapshot[name][1],
                                 "content differs after --reset-state for %s" % name)


# --- 6: ENCOUNTER inside an open Mythic+ -> no separate raid file --------------------

class EncounterInsideMPlusTests(ExtractorTestCase):

    def test_encounter_inside_mplus_has_no_separate_raid_file(self):
        start = datetime(2026, 8, 30, 10, 31, 0)
        builder = LogBuilder()
        builder.add(start, "CHALLENGE_MODE_START", q("Cripta de Ara-Kara"), "2010", "200",
                    "8", "[9]")
        enc_start = start + timedelta(seconds=28)
        builder.add(enc_start, "ENCOUNTER_START", "3199",
                    q("Trinidad de floración de Luz"), "8", "5", "2010")
        enc_end = enc_start + timedelta(seconds=133)
        builder.add(enc_end, "ENCOUNTER_END", "3199", q("Trinidad de floración de Luz"),
                    "8", "5", "1", "132566")
        end = enc_end + timedelta(seconds=1200)
        builder.add(end, "CHALLENGE_MODE_END", "2010", "1", "8", "1500000", "0.0", "0.0")
        self.write_log(builder.data())

        extractor = self.make_extractor()
        extractor.prepare()
        mplus, raid, errors = extractor.run_once()
        self.assertEqual((mplus, raid, errors), (1, 0, 0))
        self.assertEqual(self.list_outputs(self.raids_dir()), [])

        jsons = [f for f in self.list_outputs(self.mplus_dir()) if f.endswith(".json")]
        self.assertEqual(len(jsons), 1)
        meta = self.read_json(os.path.join(self.mplus_dir(), jsons[0]))
        self.assertEqual(len(meta["bosses"]), 1)
        self.assertEqual(meta["bosses"][0]["encounter_id"], 3199)
        self.assertTrue(meta["bosses"][0]["success"])


# --- 7: incomplete raid encounter -----------------------------------------------------

class IncompleteRaidTests(ExtractorTestCase):

    def test_incomplete_raid_stale_mtime_finalizes_incomplete(self):
        builder = LogBuilder()
        start = datetime(2026, 8, 30, 18, 0, 0)
        builder.add(start, "ENCOUNTER_START", "700", q("Lonely Boss"), "16", "20", "999")
        path = self.write_log(builder.data())
        old_time = time.time() - (20 * 60)  # 20 min ago > STALE_SECONDS (15 min)
        os.utime(path, (old_time, old_time))

        extractor = self.make_extractor()
        extractor.prepare()
        mplus, raid, errors = extractor.run_once()
        self.assertEqual((mplus, raid, errors), (0, 1, 0))
        txts = [f for f in self.list_outputs(self.raids_dir()) if f.endswith(".txt")]
        self.assertEqual(len(txts), 1)
        self.assertIn("_INCOMPLETE", txts[0])
        meta = self.read_json(os.path.join(
            self.raids_dir(), txts[0].replace(".txt", ".json")))
        self.assertFalse(meta["complete"])
        self.assertIsNone(meta["success"])
        self.assertIsNone(meta["duration_ms"])

    def test_incomplete_raid_not_latest_finalizes_incomplete(self):
        builder_a = LogBuilder()
        start = datetime(2026, 8, 30, 18, 30, 0)
        builder_a.add(start, "ENCOUNTER_START", "701", q("Abandoned Boss"), "16", "20", "999")
        path_a = self.write_log(builder_a.data(), name="WoWCombatLog-083026_180000.txt")

        builder_b = LogBuilder()
        builder_b.add(start + timedelta(minutes=1), "COMBAT_LOG_VERSION", "22",
                      "ADVANCED_LOG_ENABLED", "1", "BUILD_VERSION", "12.1.0",
                      "PROJECT_ID", "1")
        path_b = self.write_log(builder_b.data(), name="WoWCombatLog-083026_190000.txt")

        now = time.time()
        os.utime(path_a, (now - 5, now - 5))
        os.utime(path_b, (now, now))  # b is the newest/latest file

        extractor = self.make_extractor()
        extractor.prepare()
        mplus, raid, errors = extractor.run_once()
        self.assertEqual(errors, 0)
        txts = [f for f in self.list_outputs(self.raids_dir()) if f.endswith(".txt")]
        self.assertEqual(len(txts), 1)
        self.assertIn("_INCOMPLETE", txts[0])


# --- 8: incomplete challenge (Mythic+) -------------------------------------------------

class IncompleteChallengeTests(ExtractorTestCase):

    def test_incomplete_challenge_finalizes_incomplete(self):
        builder = LogBuilder()
        start = datetime(2026, 8, 30, 19, 0, 0)
        builder.add(start, "CHALLENGE_MODE_START", q("Foso Interminable"), "3010", "310",
                    "12", "[9,10,14]")
        path = self.write_log(builder.data())
        old_time = time.time() - (20 * 60)
        os.utime(path, (old_time, old_time))

        extractor = self.make_extractor()
        extractor.prepare()
        mplus, raid, errors = extractor.run_once()
        self.assertEqual((mplus, raid, errors), (1, 0, 0))
        txts = [f for f in self.list_outputs(self.mplus_dir()) if f.endswith(".txt")]
        self.assertEqual(len(txts), 1)
        self.assertIn("_INCOMPLETE", txts[0])
        meta = self.read_json(os.path.join(self.mplus_dir(), txts[0].replace(".txt", ".json")))
        self.assertFalse(meta["complete"])
        self.assertIsNone(meta["completed"])
        self.assertIsNone(meta["duration_ms"])


# --- 9: spurious CHALLENGE_MODE_END before any START ----------------------------------

class SpuriousChallengeEndTests(ExtractorTestCase):

    def test_spurious_end_before_start_is_ignored_and_next_run_is_extracted(self):
        builder = LogBuilder()
        zonein = datetime(2026, 8, 30, 8, 0, 0)
        builder.add(zonein, "CHALLENGE_MODE_END", "2859", "0", "0", "0", "0.000000",
                    "0.000000")

        start = zonein + timedelta(seconds=30)
        builder.add(start, "CHALLENGE_MODE_START", q("Valle Cegador"), "2859", "584", "10",
                    "[158,9,10]")
        end = start + timedelta(minutes=31)
        builder.add(end, "CHALLENGE_MODE_END", "2859", "1", "10", "1960162", "301.6633",
                    "2205.470703")
        self.write_log(builder.data())

        extractor = self.make_extractor()
        extractor.prepare()
        mplus, raid, errors = extractor.run_once()
        self.assertEqual((mplus, raid, errors), (1, 0, 0))
        txts = [f for f in self.list_outputs(self.mplus_dir()) if f.endswith(".txt")]
        self.assertEqual(len(txts), 1)
        self.assertNotIn("_INCOMPLETE", txts[0])


# --- 10: boss name with an embedded quoted comma --------------------------------------

class QuotedCommaNameTests(ExtractorTestCase):

    def test_boss_name_with_embedded_comma_parses_correctly(self):
        builder = LogBuilder()
        start = datetime(2026, 8, 30, 21, 0, 0)
        builder.add(start, "ENCOUNTER_START", "800", q("Council, Ascended"), "16", "20",
                    "999")
        end = start + timedelta(seconds=200)
        builder.add(end, "ENCOUNTER_END", "800", q("Council, Ascended"), "16", "20", "1",
                    "200000")
        self.write_log(builder.data())

        extractor = self.make_extractor()
        extractor.prepare()
        mplus, raid, errors = extractor.run_once()
        self.assertEqual((mplus, raid, errors), (0, 1, 0))
        jsons = [f for f in self.list_outputs(self.raids_dir()) if f.endswith(".json")]
        meta = self.read_json(os.path.join(self.raids_dir(), jsons[0]))
        self.assertEqual(meta["boss"], "Council, Ascended")
        txts = [f for f in self.list_outputs(self.raids_dir()) if f.endswith(".txt")]
        self.assertIn("Council,-Ascended", txts[0])


# --- 11: unicode names + filename sanitization ----------------------------------------

class UnicodeNameTests(ExtractorTestCase):

    def test_unicode_boss_and_dungeon_names_round_trip(self):
        builder = LogBuilder()
        start = datetime(2026, 8, 30, 22, 0, 0)
        dungeon = "Кладбище Штормграда"  # Russian
        builder.add(start, "CHALLENGE_MODE_START", q(dungeon), "4001", "400", "11", "[9]")
        boss = "L'Écuyer Éperdu"  # apostrophe + accents
        enc_start = start + timedelta(seconds=40)
        builder.add(enc_start, "ENCOUNTER_START", "900", q(boss), "8", "5", "4001")
        enc_end = enc_start + timedelta(seconds=100)
        builder.add(enc_end, "ENCOUNTER_END", "900", q(boss), "8", "5", "1", "100000")
        end = enc_end + timedelta(seconds=300)
        builder.add(end, "CHALLENGE_MODE_END", "4001", "1", "11", "800000", "0.0", "0.0")
        self.write_log(builder.data())

        extractor = self.make_extractor()
        extractor.prepare()
        mplus, raid, errors = extractor.run_once()
        self.assertEqual((mplus, raid, errors), (1, 0, 0))

        jsons = [f for f in self.list_outputs(self.mplus_dir()) if f.endswith(".json")]
        meta = self.read_json(os.path.join(self.mplus_dir(), jsons[0]))
        self.assertEqual(meta["dungeon"], dungeon)
        self.assertEqual(meta["bosses"][0]["boss"], boss)

        txts = [f for f in self.list_outputs(self.mplus_dir()) if f.endswith(".txt")]
        self.assertIn(wle.sanitize_filename(dungeon), txts[0])

        with open(os.path.join(self.mplus_dir(), txts[0]), "rb") as handle:
            body = handle.read()
        self.assertIn(dungeon.encode("utf-8"), body)
        self.assertIn(boss.encode("utf-8"), body)

    def test_invalid_windows_chars_removed_from_filename_but_kept_in_body(self):
        builder = LogBuilder()
        start = datetime(2026, 8, 30, 22, 30, 0)
        boss = "Boss: The Cutter/Slicer <Elite>"
        builder.add(start, "ENCOUNTER_START", "950", q(boss), "16", "20", "999")
        end = start + timedelta(seconds=50)
        builder.add(end, "ENCOUNTER_END", "950", q(boss), "16", "20", "1", "50000")
        self.write_log(builder.data())

        extractor = self.make_extractor()
        extractor.prepare()
        extractor.run_once()

        txts = [f for f in self.list_outputs(self.raids_dir()) if f.endswith(".txt")]
        self.assertEqual(len(txts), 1)
        for bad in '\\/:*?"<>|':
            self.assertNotIn(bad, txts[0])
        with open(os.path.join(self.raids_dir(), txts[0]), "rb") as handle:
            body = handle.read()
        self.assertIn(boss.encode("utf-8"), body)


# --- 12: truncated / replaced log (StateStore-level, precise) ------------------------

class StateStoreReplacementTests(ExtractorTestCase):

    def test_size_shrink_forces_offset_reset(self):
        data = b"B" * 500
        path = self.write_log(data, name="WoWCombatLog-shrink.txt")
        state = wle.StateStore(self.state_path)
        state.load()
        state.update(path, 400)
        state.save()

        with open(path, "wb") as handle:
            handle.write(b"B" * 100)  # shrink below the committed offset of 400

        state2 = wle.StateStore(self.state_path)
        state2.load()
        self.assertEqual(state2.get_offset(path), 0)

    def test_tail_hash_detects_same_prefix_different_tail(self):
        prefix = b"A" * 300  # > HASH_BYTES (256), so head_hash alone can't detect this
        original = prefix + b"ORIGINAL-TAIL-DATA-" + b"x" * 300
        path = self.write_log(original, name="WoWCombatLog-legacy.txt")
        state = wle.StateStore(self.state_path)
        state.load()
        committed_offset = len(original)
        state.update(path, committed_offset)
        state.save()

        replaced = prefix + b"REPLACED-TAIL-COMPLETELY-DIFFERENT-" + b"y" * 400
        # Preconditions this test is actually meant to exercise:
        self.assertEqual(original[:256], replaced[:256], "precondition: same head")
        self.assertGreater(len(replaced), committed_offset, "precondition: size > offset")
        with open(path, "wb") as handle:
            handle.write(replaced)

        state2 = wle.StateStore(self.state_path)
        state2.load()
        self.assertEqual(state2.get_offset(path), 0)


class TruncationEndToEndTests(ExtractorTestCase):

    def test_end_to_end_shrink_then_regrow_reprocesses_from_zero(self):
        builder = LogBuilder()
        start = datetime(2026, 8, 30, 7, 0, 0)
        for i in range(5):
            builder.add(start - timedelta(seconds=5 + i), "SPELL_CAST_SUCCESS",
                        "Player-1-A", q("Padding line %d to guarantee size" % i))
        builder.add(start, "ENCOUNTER_START", "1300", q("Shrink Boss"), "14", "20", "999")
        end = start + timedelta(seconds=40)
        builder.add(end, "ENCOUNTER_END", "1300", q("Shrink Boss"), "14", "20", "1", "40000")
        path = self.write_log(builder.data())

        extractor = self.make_extractor()
        extractor.prepare()
        mplus1, raid1, errors1 = extractor.run_once()
        self.assertEqual((mplus1, raid1, errors1), (0, 1, 0))
        committed_offset = extractor.state.get_offset(path)
        self.assertGreater(committed_offset, 0)

        builder2 = LogBuilder()
        start2 = datetime(2026, 8, 30, 7, 10, 0)
        builder2.add(start2, "ENCOUNTER_START", "1301", q("Regrown Boss"), "14", "20", "999")
        end2 = start2 + timedelta(seconds=30)
        builder2.add(end2, "ENCOUNTER_END", "1301", q("Regrown Boss"), "14", "20", "1",
                     "30000")
        new_data = builder2.data()
        self.assertLess(len(new_data), committed_offset, "precondition: file really shrank")
        with open(path, "wb") as handle:
            handle.write(new_data)

        extractor2 = self.make_extractor()
        extractor2.prepare()
        mplus2, raid2, errors2 = extractor2.run_once()
        self.assertEqual(errors2, 0)
        self.assertEqual(raid2, 1)

        jsons = [f for f in self.list_outputs(self.raids_dir()) if f.endswith(".json")]
        bosses = {self.read_json(os.path.join(self.raids_dir(), f))["boss"] for f in jsons}
        self.assertIn("Regrown Boss", bosses)


# --- short-log growth must not be misdetected as "replacement" -----------------------

class HeadHashGrowthBugTests(ExtractorTestCase):
    """The head-hash window is capped at the committed offset, so a log that was
    shorter than HASH_BYTES at commit time and then grows by ordinary appending
    must keep its committed offset (growth is not replacement)."""

    def test_short_log_growth_is_not_mistaken_for_replacement(self):
        data_v1 = b"X" * 100  # well under HASH_BYTES (256)
        path = self.write_log(data_v1, name="WoWCombatLog-shortgrowth.txt")
        state = wle.StateStore(self.state_path)
        state.load()
        state.update(path, len(data_v1))
        state.save()

        # Pure append -- nothing before the committed offset changes.
        grown = data_v1 + b"Y" * 300
        self.append_log(b"Y" * 300, name="WoWCombatLog-shortgrowth.txt")
        self.assertEqual(grown[:len(data_v1)], data_v1)  # precondition: a true append

        state2 = wle.StateStore(self.state_path)
        state2.load()
        # Expected (per the plan's contract): pure growth is not a replacement,
        # so the previously committed offset should still be honored.
        self.assertEqual(state2.get_offset(path), len(data_v1))


# --- 13: incremental run: append + rerun ----------------------------------------------

class IncrementalRunTests(ExtractorTestCase):

    def test_incremental_append_only_adds_new_pull_leaves_old_untouched(self):
        start1 = datetime(2026, 8, 30, 14, 0, 0)
        builder = LogBuilder()
        # Padding to push the file safely past HASH_BYTES (256): see
        # HeadHashGrowthBugTests below -- a log shorter than 256 bytes at commit
        # time gets spuriously "replacement detected" on the next run simply
        # because it grew, which is not what this test is about.
        for i in range(5):
            builder.add(start1 - timedelta(seconds=10 - i), "SPELL_CAST_SUCCESS",
                        "Player-1-A", q("Padding line %d to clear the hash window" % i))
        builder.add(start1, "ENCOUNTER_START", "500", q("First Boss"), "15", "20", "10")
        builder.add(start1 + timedelta(seconds=20), "ENCOUNTER_END", "500", q("First Boss"),
                    "15", "20", "1", "20000")
        self.write_log(builder.data())

        extractor = self.make_extractor()
        extractor.prepare()
        mplus1, raid1, errors1 = extractor.run_once()
        self.assertEqual((mplus1, raid1, errors1), (0, 1, 0))

        raids_before = self.list_outputs(self.raids_dir())
        snapshot = {}
        for name in raids_before:
            full = os.path.join(self.raids_dir(), name)
            with open(full, "rb") as handle:
                snapshot[name] = (os.path.getmtime(full), handle.read())

        start2 = datetime(2026, 8, 30, 14, 5, 0)
        builder2 = LogBuilder()
        builder2.add(start2, "ENCOUNTER_START", "501", q("Second Boss"), "15", "20", "10")
        builder2.add(start2 + timedelta(seconds=25), "ENCOUNTER_END", "501", q("Second Boss"),
                     "15", "20", "1", "25000")
        self.append_log(builder2.data())

        extractor2 = self.make_extractor()
        extractor2.prepare()
        mplus2, raid2, errors2 = extractor2.run_once()
        self.assertEqual((mplus2, raid2, errors2), (0, 1, 0))

        raids_after = self.list_outputs(self.raids_dir())
        self.assertEqual(len(raids_after), len(raids_before) + 2)  # +1 .txt +1 .json
        for name, (mtime, content) in snapshot.items():
            full = os.path.join(self.raids_dir(), name)
            self.assertEqual(os.path.getmtime(full), mtime, "mtime changed for %s" % name)
            with open(full, "rb") as handle:
                self.assertEqual(handle.read(), content, "content changed for %s" % name)

        extractor3 = self.make_extractor()
        extractor3.prepare()
        self.assertEqual(extractor3.run_once(), (0, 0, 0))


# --- 14: open segment continues across two executions --------------------------------

class OpenSegmentContinuationTests(ExtractorTestCase):

    def test_open_mplus_continues_across_two_executions(self):
        start = datetime(2026, 8, 30, 9, 0, 0)
        builder1 = LogBuilder()
        builder1.add(start - timedelta(seconds=5), "SPELL_CAST_SUCCESS", "Player-1-A",
                     q("Pre"))
        builder1.add(start, "CHALLENGE_MODE_START", q("Sala de Trivialidades"), "2001",
                     "111", "7", "[9]")
        builder1.add(start + timedelta(seconds=30), "SPELL_CAST_SUCCESS", "Player-1-A",
                     q("Mid"))
        self.write_log(builder1.data())

        extractor1 = self.make_extractor()
        extractor1.prepare()
        mplus1, raid1, errors1 = extractor1.run_once()
        self.assertEqual((mplus1, raid1, errors1), (0, 0, 0))
        self.assertNoTxtOrJson(self.mplus_dir())  # still open, pending -- no output yet

        end = start + timedelta(seconds=60)
        builder2 = LogBuilder()
        builder2.add(end, "CHALLENGE_MODE_END", "2001", "1", "7", "500000", "0.0", "0.0")
        builder2.add(end + timedelta(seconds=2), "SPELL_CAST_SUCCESS", "Player-1-A",
                     q("Post"))
        self.append_log(builder2.data())

        extractor2 = self.make_extractor()
        extractor2.prepare()
        mplus2, raid2, errors2 = extractor2.run_once()
        self.assertEqual((mplus2, raid2, errors2), (1, 0, 0))
        txts = [f for f in self.list_outputs(self.mplus_dir()) if f.endswith(".txt")]
        self.assertEqual(len(txts), 1)
        partials = [f for f in self.list_outputs(self.mplus_dir()) if f.endswith(".partial")]
        self.assertEqual(partials, [])
        meta = self.read_json(os.path.join(self.mplus_dir(), txts[0].replace(".txt", ".json")))
        self.assertTrue(meta["complete"])


# --- 15: partial final line without a trailing newline --------------------------------

class PartialFinalLineTests(ExtractorTestCase):

    def test_partial_trailing_line_is_completed_intact_on_next_run(self):
        start = datetime(2026, 8, 30, 9, 30, 0)
        builder1 = LogBuilder()
        builder1.add(start, "CHALLENGE_MODE_START", q("Foso de Saurfang"), "2002", "112",
                     "9", "[10,9]")
        end = start + timedelta(seconds=45)
        end_line_text = "%s  CHALLENGE_MODE_END,2002,1,9,600000,0.0,0.0" % ts_str(end)
        partial_bytes = end_line_text.encode("utf-8")  # deliberately NO trailing \r\n
        self.write_log(builder1.data() + partial_bytes)

        extractor1 = self.make_extractor()
        extractor1.prepare()
        mplus1, raid1, errors1 = extractor1.run_once()
        self.assertEqual((mplus1, raid1, errors1), (0, 0, 0))
        self.assertNoTxtOrJson(self.mplus_dir())

        full_end_line = end_line_text.encode("utf-8") + b"\r\n"
        trailing = line_bytes(end + timedelta(seconds=1), "SPELL_CAST_SUCCESS",
                              "Player-1-A", q("Post"))
        self.append_log(b"\r\n" + trailing)  # just finish the cut-off line, then one more

        extractor2 = self.make_extractor()
        extractor2.prepare()
        mplus2, raid2, errors2 = extractor2.run_once()
        self.assertEqual((mplus2, raid2, errors2), (1, 0, 0))

        txts = [f for f in self.list_outputs(self.mplus_dir()) if f.endswith(".txt")]
        self.assertEqual(len(txts), 1)
        with open(os.path.join(self.mplus_dir(), txts[0]), "rb") as handle:
            body = handle.read()
        self.assertIn(full_end_line, body)  # intact, byte-for-byte, not corrupted


# --- 16: EOF with END seen but < 10s of trailing data -> COMPLETE --------------------

class EofShortTrailingCompleteTests(ExtractorTestCase):

    def test_eof_with_end_seen_and_short_trailing_is_complete_not_incomplete(self):
        builder = LogBuilder()
        start = datetime(2026, 8, 30, 23, 0, 0)
        builder.add(start, "ENCOUNTER_START", "1100", q("Rushed Boss"), "16", "20", "999")
        end = start + timedelta(seconds=50)
        builder.add(end, "ENCOUNTER_END", "1100", q("Rushed Boss"), "16", "20", "1", "50000")
        builder.add(end + timedelta(seconds=3), "SPELL_CAST_SUCCESS", "Player-1-A",
                    q("Loot"))  # only 3s of trailing data, well under CONTEXT_SECONDS
        self.write_log(builder.data())

        extractor = self.make_extractor()
        extractor.prepare()
        mplus, raid, errors = extractor.run_once()
        self.assertEqual((mplus, raid, errors), (0, 1, 0))
        txts = [f for f in self.list_outputs(self.raids_dir()) if f.endswith(".txt")]
        self.assertEqual(len(txts), 1)
        self.assertNotIn("_INCOMPLETE", txts[0])
        meta = self.read_json(os.path.join(self.raids_dir(), txts[0].replace(".txt", ".json")))
        self.assertTrue(meta["complete"])
        self.assertTrue(meta["success"])


# --- 17 & 18: exact context boundaries + byte-for-byte identity ----------------------

class ContextBoundaryAndByteIdentityTests(ExtractorTestCase):

    def test_exact_pre_and_post_context_boundaries_and_byte_identity(self):
        builder = LogBuilder()
        start = datetime(2026, 8, 30, 13, 0, 0, 500000)
        limit = start - timedelta(seconds=wle.CONTEXT_SECONDS)

        excluded_pre = limit - timedelta(microseconds=1)
        included_pre = limit
        builder.add(excluded_pre, "SPELL_CAST_SUCCESS", "Player-1-A", q("TooEarly"))
        builder.add(included_pre, "SPELL_CAST_SUCCESS", "Player-1-A", q("JustInTime"),
                    mark="pre_start")

        builder.add(start, "ENCOUNTER_START", "1000", q("Boundary Boss"), "16", "20", "999")
        builder.add(start + timedelta(seconds=15), "SPELL_CAST_SUCCESS", "Player-1-A",
                    q("MidFight"))
        end = start + timedelta(seconds=200)
        builder.add(end, "ENCOUNTER_END", "1000", q("Boundary Boss"), "16", "20", "1",
                    "200000")

        trailing_limit = end + timedelta(seconds=wle.CONTEXT_SECONDS)
        included_post = trailing_limit
        excluded_post = trailing_limit + timedelta(microseconds=1)
        builder.add(included_post, "SPELL_CAST_SUCCESS", "Player-1-A",
                    q("JustBeforeCutoff"), mark="post_end")
        builder.add(excluded_post, "SPELL_CAST_SUCCESS", "Player-1-A", q("TooLate"))
        builder.add(excluded_post + timedelta(seconds=1), "SPELL_CAST_SUCCESS",
                    "Player-1-A", q("Filler"))

        self.write_log(builder.data())
        extractor = self.make_extractor()
        extractor.prepare()
        mplus, raid, errors = extractor.run_once()
        self.assertEqual((mplus, raid, errors), (0, 1, 0))

        txts = [f for f in self.list_outputs(self.raids_dir()) if f.endswith(".txt")]
        self.assertEqual(len(txts), 1)
        with open(os.path.join(self.raids_dir(), txts[0]), "rb") as handle:
            body = handle.read()

        pre_start_offset, _ = builder.marks["pre_start"]
        _, post_end_offset = builder.marks["post_end"]
        expected = builder.data()[pre_start_offset:post_end_offset]
        self.assertEqual(body, expected)

        self.assertNotIn(b"TooEarly", body)
        self.assertIn(b"JustInTime", body)
        self.assertIn(b"JustBeforeCutoff", body)
        self.assertNotIn(b"TooLate", body)


# --- 19: crash-injection at publication boundaries -------------------------------------

class CrashInjectionTests(ExtractorTestCase):

    def _build_simple_raid_log(self):
        builder = LogBuilder()
        start = datetime(2026, 8, 30, 11, 0, 0)
        builder.add(start - timedelta(seconds=5), "SPELL_CAST_SUCCESS", "Player-1-A",
                    q("Filler"))
        builder.add(start, "ENCOUNTER_START", "100", q("Test Boss"), "15", "20", "999")
        end = start + timedelta(seconds=30)
        builder.add(end, "ENCOUNTER_END", "100", q("Test Boss"), "15", "20", "1", "30000")
        self.write_log(builder.data())
        return builder

    def _assert_single_clean_output(self):
        raids = self.list_outputs(self.raids_dir())
        txts = [f for f in raids if f.endswith(".txt")]
        jsons = [f for f in raids if f.endswith(".json")]
        partials = [f for f in raids if f.endswith(".partial")]
        self.assertEqual(len(txts), 1, raids)
        self.assertEqual(len(jsons), 1, raids)
        self.assertEqual(len(partials), 0, raids)
        return txts[0], jsons[0]

    def test_crash_after_json_published_before_txt_renamed(self):
        self._build_simple_raid_log()
        extractor = self.make_extractor()
        extractor.prepare()

        real_replace = os.replace
        state = {"raised": False}

        def flaky_replace(src, dst):
            if (not state["raised"]) and dst.endswith(".txt") and not dst.endswith(".tmp"):
                state["raised"] = True
                raise OSError("simulated crash: txt rename")
            return real_replace(src, dst)

        with mock.patch("WoWLogExtractor.os.replace", side_effect=flaky_replace):
            mplus, raid, errors = extractor.run_once()

        self.assertEqual(errors, 1)
        self.assertEqual((mplus, raid), (0, 0))

        raids_listing = self.list_outputs(self.raids_dir())
        self.assertTrue(any(name.endswith(".json") for name in raids_listing), raids_listing)
        self.assertTrue(any(name.endswith(".partial") for name in raids_listing), raids_listing)
        self.assertFalse(any(name.endswith(".txt") for name in raids_listing), raids_listing)

        extractor2 = self.make_extractor()
        extractor2.prepare()  # cleans up the stray .partial orphan
        self.assertFalse(any(n.endswith(".partial") for n in self.list_outputs(self.raids_dir())))
        mplus2, raid2, errors2 = extractor2.run_once()
        self.assertEqual(errors2, 0)
        self.assertEqual(raid2, 1)
        self._assert_single_clean_output()

    def test_crash_before_json_published(self):
        self._build_simple_raid_log()
        extractor = self.make_extractor()
        extractor.prepare()

        real_replace = os.replace
        state = {"raised": False}

        def flaky_replace(src, dst):
            if (not state["raised"]) and dst.endswith(".json") and \
                    os.path.basename(dst) != wle.STATE_FILENAME:
                state["raised"] = True
                raise OSError("simulated crash: json publish")
            return real_replace(src, dst)

        with mock.patch("WoWLogExtractor.os.replace", side_effect=flaky_replace):
            mplus, raid, errors = extractor.run_once()

        self.assertEqual(errors, 1)
        self.assertEqual((mplus, raid), (0, 0))
        raids_listing = self.list_outputs(self.raids_dir())
        self.assertFalse(any(name.endswith(".txt") for name in raids_listing), raids_listing)
        self.assertFalse(any(name.endswith(".json") for name in raids_listing), raids_listing)

        extractor2 = self.make_extractor()
        extractor2.prepare()
        mplus2, raid2, errors2 = extractor2.run_once()
        self.assertEqual(errors2, 0)
        self.assertEqual(raid2, 1)
        self._assert_single_clean_output()

    def test_crash_after_txt_renamed_before_state_advances(self):
        self._build_simple_raid_log()
        extractor = self.make_extractor()
        extractor.prepare()
        log_path = self.log_path()

        # Manually drive one file through the pipeline and deliberately skip the
        # state.update()/state.save() calls that run_once() would normally make --
        # this is what a hard process kill right after step (3) (txt renamed) but
        # before step (4) (state advances) looks like: outputs are fully published,
        # state.json is untouched.
        processor = wle.FileProcessor(log_path, extractor.publisher,
                                      extractor.state.get_offset(log_path))
        processor.process_new_data()
        processor.finish(is_latest=True)
        mplus, raid = processor.counts()
        self.assertEqual((mplus, raid), (0, 1))
        txt_name, json_name = self._assert_single_clean_output()

        extractor2 = self.make_extractor()
        extractor2.prepare()
        self.assertEqual(extractor2.state.get_offset(log_path), 0)
        mplus2, raid2, errors2 = extractor2.run_once()
        self.assertEqual(errors2, 0)
        self.assertEqual(raid2, 1)
        txt_name2, json_name2 = self._assert_single_clean_output()
        self.assertEqual(txt_name, txt_name2)
        self.assertEqual(json_name, json_name2)


# --- 20: scan_for_log_dirs resilience to an inaccessible candidate -------------------

class ScanResilienceTests(unittest.TestCase):

    def test_inaccessible_candidate_does_not_abort_scan(self):
        real_isdir = os.path.isdir
        real_listdir = os.listdir

        def fake_isdir(path):
            if path == "C:\\":
                raise PermissionError("simulated: cannot access C:\\")
            if path == "D:\\":
                return True
            return real_isdir(path)

        def fake_listdir(path):
            if path == "D:\\":
                return []
            return real_listdir(path)

        with mock.patch("os.path.isdir", side_effect=fake_isdir), \
             mock.patch("os.listdir", side_effect=fake_listdir):
            candidates = wle.scan_for_log_dirs()

        self.assertTrue(any(c.startswith("D:\\") for c in candidates), candidates)
        self.assertFalse(any(c.startswith("C:\\") for c in candidates), candidates)


# --- 21: difficulty and map-id fallbacks ----------------------------------------------

class FallbackTests(ExtractorTestCase):

    def test_unknown_difficulty_id_falls_back_to_generic_label(self):
        builder = LogBuilder()
        start = datetime(2026, 8, 30, 6, 0, 0)
        builder.add(start, "ENCOUNTER_START", "1200", q("Weird Difficulty Boss"), "99",
                    "20", "999")
        end = start + timedelta(seconds=60)
        builder.add(end, "ENCOUNTER_END", "1200", q("Weird Difficulty Boss"), "99", "20",
                    "1", "60000")
        self.write_log(builder.data())

        extractor = self.make_extractor()
        extractor.prepare()
        extractor.run_once()
        jsons = [f for f in self.list_outputs(self.raids_dir()) if f.endswith(".json")]
        meta = self.read_json(os.path.join(self.raids_dir(), jsons[0]))
        self.assertEqual(meta["difficulty"], "Difficulty99")
        txts = [f for f in self.list_outputs(self.raids_dir()) if f.endswith(".txt")]
        self.assertIn("Difficulty99", txts[0])

    def test_missing_dungeon_name_uses_map_id_fallback(self):
        builder = LogBuilder()
        start = datetime(2026, 8, 30, 6, 30, 0)
        builder.add(start, "CHALLENGE_MODE_START", '""', "424242", "500", "5", "[]")
        end = start + timedelta(minutes=10)
        builder.add(end, "CHALLENGE_MODE_END", "424242", "1", "5", "600000", "0.0", "0.0")
        self.write_log(builder.data())

        extractor = self.make_extractor()
        extractor.prepare()
        extractor.run_once()
        jsons = [f for f in self.list_outputs(self.mplus_dir()) if f.endswith(".json")]
        meta = self.read_json(os.path.join(self.mplus_dir(), jsons[0]))
        self.assertIsNone(meta["dungeon"])
        self.assertEqual(meta["map_id"], 424242)
        txts = [f for f in self.list_outputs(self.mplus_dir()) if f.endswith(".txt")]
        self.assertIn("Map424242", txts[0])


# --- watch mode: rotation to a new log file ------------------------------------------

class WatchRotationTests(ExtractorTestCase):
    """Plan case: rotation in --watch with an open segment and with pending trailing.
    Uses the max_polls test hook; a second log file appears between poll 1 and poll 2
    (injected via a patched time.sleep, which watch() calls once per poll)."""

    OLD_LOG = "WoWCombatLog-083026_100000.txt"
    NEW_LOG = "WoWCombatLog-083026_110000.txt"

    def _run_watch_with_rotation(self, old_data: bytes, new_data: bytes):
        old_path = self.write_log(old_data, name=self.OLD_LOG)
        now = time.time()
        os.utime(old_path, (now - 120, now - 120))

        def appear_new_log(_interval):
            if not os.path.exists(self.log_path(self.NEW_LOG)):
                self.write_log(new_data, name=self.NEW_LOG)

        extractor = self.make_extractor()
        extractor.prepare()
        with mock.patch.object(wle.time, "sleep", side_effect=appear_new_log):
            counts = extractor.watch(interval=0, max_polls=2)
        return counts

    def _build_new_log_with_kill(self):
        t1 = datetime(2026, 8, 30, 11, 0, 0)
        c = LogBuilder()
        c.add(t1, "COMBAT_LOG_VERSION", "22", "ADVANCED_LOG_ENABLED", "1",
              "BUILD_VERSION", "12.1.0", "PROJECT_ID", "1")
        c.add(t1 + timedelta(seconds=5), "ENCOUNTER_START", "3001", q("New Boss"),
              "15", "20", "2600")
        c.add(t1 + timedelta(seconds=65), "ENCOUNTER_END", "3001", q("New Boss"),
              "15", "20", "1", "60000")
        c.add(t1 + timedelta(seconds=68), "SPELL_CAST_SUCCESS", "x", "y")
        return c.data()

    def test_rotation_with_open_segment_finalizes_incomplete_and_processes_new_once(self):
        t0 = datetime(2026, 8, 30, 10, 0, 0)
        b = LogBuilder()
        b.add(t0, "COMBAT_LOG_VERSION", "22", "ADVANCED_LOG_ENABLED", "1",
              "BUILD_VERSION", "12.1.0", "PROJECT_ID", "1")
        b.add(t0 + timedelta(seconds=10), "ENCOUNTER_START", "3000", q("Old Boss"),
              "15", "20", "2600")
        b.add(t0 + timedelta(seconds=15), "SPELL_CAST_SUCCESS", "x", "y")
        # No ENCOUNTER_END: the segment is open when the rotation happens.

        mplus, raids, errors = self._run_watch_with_rotation(
            b.data(), self._build_new_log_with_kill())
        self.assertEqual(errors, 0)
        self.assertEqual((mplus, raids), (0, 2))
        outputs = self.list_outputs(self.raids_dir())
        txts = [f for f in outputs if f.endswith(".txt")]
        self.assertEqual(len(txts), 2, txts)
        self.assertIn("2026-08-30_10-00_Raid_Old-Boss_Heroic_INCOMPLETE.txt", txts)
        self.assertIn("2026-08-30_11-00_Raid_New-Boss_Heroic_Kill.txt", txts)

        # Re-running watch finds nothing new: each file was processed exactly once.
        extractor = self.make_extractor()
        extractor.prepare()
        self.assertEqual(extractor.watch(interval=0, max_polls=1), (0, 0, 0))
        self.assertEqual(self.list_outputs(self.raids_dir()), outputs)

    def test_rotation_with_pending_trailing_finalizes_complete(self):
        t0 = datetime(2026, 8, 30, 10, 0, 0)
        b = LogBuilder()
        b.add(t0, "COMBAT_LOG_VERSION", "22", "ADVANCED_LOG_ENABLED", "1",
              "BUILD_VERSION", "12.1.0", "PROJECT_ID", "1")
        b.add(t0 + timedelta(seconds=10), "ENCOUNTER_START", "3000", q("Old Boss"),
              "15", "20", "2600")
        b.add(t0 + timedelta(seconds=70), "ENCOUNTER_END", "3000", q("Old Boss"),
              "15", "20", "0", "60000")
        # Only 2 s of trailing context exists: still COMPLETE, never _INCOMPLETE.
        b.add(t0 + timedelta(seconds=72), "SPELL_CAST_SUCCESS", "x", "y")

        mplus, raids, errors = self._run_watch_with_rotation(
            b.data(), self._build_new_log_with_kill())
        self.assertEqual(errors, 0)
        self.assertEqual((mplus, raids), (0, 2))
        txts = [f for f in self.list_outputs(self.raids_dir()) if f.endswith(".txt")]
        self.assertIn("2026-08-30_10-00_Raid_Old-Boss_Heroic_Wipe.txt", txts)
        self.assertIn("2026-08-30_11-00_Raid_New-Boss_Heroic_Kill.txt", txts)
        self.assertNotIn("2026-08-30_10-00_Raid_Old-Boss_Heroic_INCOMPLETE.txt", txts)


# --- regression: an _INCOMPLETE segment that later completes keeps ONE pair ----------

class IncompleteBecomesCompleteTests(ExtractorTestCase):
    """A pull first published as _INCOMPLETE and later reprocessed with its END must
    leave exactly one pair: the stale outcome-named pair is purged by segment_id."""

    def test_incomplete_pair_is_replaced_not_duplicated(self):
        t0 = datetime(2026, 8, 30, 10, 0, 0)
        b = LogBuilder()
        b.add(t0, "COMBAT_LOG_VERSION", "22", "ADVANCED_LOG_ENABLED", "1",
              "BUILD_VERSION", "12.1.0", "PROJECT_ID", "1")
        b.add(t0 + timedelta(seconds=10), "ENCOUNTER_START", "3000", q("Late Boss"),
              "15", "20", "2600")
        b.add(t0 + timedelta(seconds=20), "SPELL_CAST_SUCCESS", "x", "y")
        path = self.write_log(b.data())
        stale = time.time() - (wle.STALE_SECONDS + 60)
        os.utime(path, (stale, stale))

        extractor = self.make_extractor()
        extractor.prepare()
        extractor.run_once()
        txts = [f for f in self.list_outputs(self.raids_dir()) if f.endswith(".txt")]
        self.assertEqual(txts, ["2026-08-30_10-00_Raid_Late-Boss_Heroic_INCOMPLETE.txt"])

        # The rest of the fight shows up later; reprocess the whole log.
        b.add(t0 + timedelta(seconds=70), "ENCOUNTER_END", "3000", q("Late Boss"),
              "15", "20", "1", "60000")
        b.add(t0 + timedelta(seconds=75), "SPELL_CAST_SUCCESS", "a", "b")
        self.write_log(b.data())

        extractor = self.make_extractor()
        extractor.prepare(reset_state=True)
        extractor.run_once()
        outputs = self.list_outputs(self.raids_dir())
        txts = [f for f in outputs if f.endswith(".txt")]
        jsons = [f for f in outputs if f.endswith(".json")]
        self.assertEqual(txts, ["2026-08-30_10-00_Raid_Late-Boss_Heroic_Kill.txt"])
        self.assertEqual(jsons, ["2026-08-30_10-00_Raid_Late-Boss_Heroic_Kill.json"])


# --- regression: watch revalidates fingerprints (replace + regrow between polls) -----

class WatchReplacementTests(ExtractorTestCase):

    def _pull(self, t0: datetime, encounter_id: str, boss: str) -> bytes:
        b = LogBuilder()
        b.add(t0, "COMBAT_LOG_VERSION", "22", "ADVANCED_LOG_ENABLED", "1",
              "BUILD_VERSION", "12.1.0", "PROJECT_ID", "1")
        b.add(t0 + timedelta(seconds=10), "ENCOUNTER_START", encounter_id, q(boss),
              "15", "20", "2600")
        b.add(t0 + timedelta(seconds=70), "ENCOUNTER_END", encounter_id, q(boss),
              "15", "20", "1", "60000")
        b.add(t0 + timedelta(seconds=75), "SPELL_CAST_SUCCESS", "x", "y")
        return b.data()

    def test_replacement_regrown_past_offset_is_reprocessed_from_zero(self):
        first = self._pull(datetime(2026, 8, 30, 10, 0, 0), "3000", "First Boss")
        # The replacement is larger than the committed offset and starts differently,
        # so only the head/tail fingerprints can catch it.
        second = self._pull(datetime(2026, 8, 31, 20, 0, 0), "3001", "Second Boss")
        second += b"x" * (max(0, len(first) - len(second)) + 4096)
        self.write_log(first)

        def replace_log(_interval):
            if os.path.getsize(self.log_path()) <= len(first):
                self.write_log(second)

        extractor = self.make_extractor()
        extractor.prepare()
        with mock.patch.object(wle.time, "sleep", side_effect=replace_log):
            mplus, raids, errors = extractor.watch(interval=0, max_polls=2)

        self.assertEqual(errors, 0)
        self.assertEqual((mplus, raids), (0, 2))
        txts = sorted(f for f in self.list_outputs(self.raids_dir()) if f.endswith(".txt"))
        self.assertEqual(txts, [
            "2026-08-30_10-00_Raid_First-Boss_Heroic_Kill.txt",
            "2026-08-31_20-00_Raid_Second-Boss_Heroic_Kill.txt",
        ])


    def test_replacement_after_restart_from_persisted_offset_is_detected(self):
        """A watch session that starts at EOF of an already-processed log must still
        notice a replacement: the prefix is fingerprinted even when nothing is read."""
        first = self._pull(datetime(2026, 8, 30, 10, 0, 0), "3000", "First Boss")
        second = self._pull(datetime(2026, 8, 31, 20, 0, 0), "3001", "Second Boss")
        second += b"x" * (max(0, len(first) - len(second)) + 4096)
        path = self.write_log(first)
        stale = time.time() - (wle.STALE_SECONDS + 60)
        os.utime(path, (stale, stale))

        # First session consumes the whole log and persists its offset.
        extractor = self.make_extractor()
        extractor.prepare()
        extractor.run_once()
        committed = extractor.state.get_offset(path)
        self.assertGreater(committed, 0)

        def replace_log(_interval):
            if os.path.getsize(self.log_path()) <= len(first):
                self.write_log(second)

        # New session: poll 1 reads nothing (already at EOF), poll 2 sees the swap.
        extractor = self.make_extractor()
        extractor.prepare()
        with mock.patch.object(wle.time, "sleep", side_effect=replace_log):
            mplus, raids, errors = extractor.watch(interval=0, max_polls=2)

        self.assertEqual((mplus, raids, errors), (0, 1, 0))
        txts = sorted(f for f in self.list_outputs(self.raids_dir()) if f.endswith(".txt"))
        self.assertEqual(txts, [
            "2026-08-30_10-00_Raid_First-Boss_Heroic_Kill.txt",
            "2026-08-31_20-00_Raid_Second-Boss_Heroic_Kill.txt",
        ])


# --- regression: a failed stale purge must not destroy the segment_id record --------

class PurgeFailureTests(ExtractorTestCase):

    def _log_with_late_end(self, with_end: bool) -> bytes:
        t0 = datetime(2026, 8, 30, 10, 0, 0)
        b = LogBuilder()
        b.add(t0, "COMBAT_LOG_VERSION", "22", "ADVANCED_LOG_ENABLED", "1",
              "BUILD_VERSION", "12.1.0", "PROJECT_ID", "1")
        b.add(t0 + timedelta(seconds=10), "ENCOUNTER_START", "3000", q("Locked Boss"),
              "15", "20", "2600")
        b.add(t0 + timedelta(seconds=20), "SPELL_CAST_SUCCESS", "x", "y")
        if with_end:
            b.add(t0 + timedelta(seconds=70), "ENCOUNTER_END", "3000", q("Locked Boss"),
                  "15", "20", "1", "60000")
            b.add(t0 + timedelta(seconds=75), "SPELL_CAST_SUCCESS", "a", "b")
        return b.data()

    def test_locked_stale_txt_keeps_its_json_and_retries_next_run(self):
        path = self.write_log(self._log_with_late_end(False))
        stale = time.time() - (wle.STALE_SECONDS + 60)
        os.utime(path, (stale, stale))
        extractor = self.make_extractor()
        extractor.prepare()
        extractor.run_once()
        incomplete = "2026-08-30_10-00_Raid_Locked-Boss_Heroic_INCOMPLETE"
        self.assertIn(incomplete + ".txt", self.list_outputs(self.raids_dir()))

        self.write_log(self._log_with_late_end(True))
        real_remove = os.remove

        def locked_remove(target, *a, **kw):
            if os.path.basename(target) == incomplete + ".txt":
                raise PermissionError(13, "locked")
            return real_remove(target, *a, **kw)

        extractor = self.make_extractor()
        extractor.prepare(reset_state=True)
        with mock.patch.object(wle.os, "remove", side_effect=locked_remove):
            mplus, raids, errors = extractor.run_once()

        # The purge failed loudly: the stale pair is intact (its json still carries the
        # segment_id) and the state offset did not advance past the segment.
        self.assertEqual(errors, 1)
        outputs = self.list_outputs(self.raids_dir())
        self.assertIn(incomplete + ".txt", outputs)
        self.assertIn(incomplete + ".json", outputs)

        # Once the lock is gone, a plain rerun converges to exactly one pair.
        extractor = self.make_extractor()
        extractor.prepare(reset_state=True)
        extractor.run_once()
        outputs = self.list_outputs(self.raids_dir())
        self.assertEqual(sorted(f for f in outputs if f.endswith(".txt")),
                         ["2026-08-30_10-00_Raid_Locked-Boss_Heroic_Kill.txt"])


# --- analysis bundles: public output contract --------------------------------------

class AnalysisBundleTests(ExtractorTestCase):
    """End-to-end fixtures for the opt-in analysis representation.

    These use the Retail common actor header (rather than the deliberately tiny
    legacy fixtures above) so the relevance graph and advanced payload parsing are
    exercised without depending on private parser/container implementations.
    """

    PLAYER = "Player-1-00000001"
    HEALER = "Player-1-00000002"
    PET = "Pet-0-0001-0002-0003-000000000001"
    # Never summoned and never linked to an owner: the canonical irrelevant unit.
    STRAY_PET = "Pet-0-0001-0002-0003-000000000007"
    ENEMY = "Creature-0-0001-0002-0003-000000000099"
    OTHER_ENEMY = "Creature-0-0001-0002-0003-000000000098"

    def options(self, **overrides):
        values = {"analysis": False, "analysis_only": False,
                  "gzip": False, "bundle": False}
        values.update(overrides)
        return wle.OutputOptions(**values)

    @staticmethod
    def header(source_guid, source_name, source_flags, dest_guid, dest_name,
               dest_flags):
        return (source_guid, q(source_name), str(source_flags), "0",
                dest_guid, q(dest_name), str(dest_flags), "0")

    def add_event(self, builder, timestamp, event, source_guid, source_name,
                  source_flags, dest_guid, dest_name, dest_flags, *payload):
        builder.add(timestamp, event, *self.header(source_guid, source_name,
                    source_flags, dest_guid, dest_name, dest_flags), *payload)

    def _build_raid_with_actor_events(self, boss="Señor Ñandú"):
        start = datetime(2026, 8, 30, 22, 0, 0)
        builder = LogBuilder()
        builder.add(start, "COMBAT_LOG_VERSION", "22", "ADVANCED_LOG_ENABLED", "1",
                    "BUILD_VERSION", "12.1.0", "PROJECT_ID", "1")
        builder.add(start + timedelta(seconds=1), "ENCOUNTER_START", "9200", q(boss),
                    "16", "20", "2900")
        # COMBATANT_INFO has its own layout; it is deliberately retained even though
        # it does not use the common source/destination header.
        combatant = [self.PLAYER] + ["0"] * 23 + [
            "65", "[(1,2,1)]", "()", "[]", "[]", "85", "0", "0", "0"]
        builder.add(start + timedelta(seconds=2), "COMBATANT_INFO", *combatant)
        self.add_event(builder, start + timedelta(seconds=3), "SPELL_CAST_START",
                       self.ENEMY, boss, 68168, self.PLAYER, "Álvaro", 1297,
                       "9001", q("Dark Bolt"), "32")
        self.add_event(builder, start + timedelta(seconds=4), "SPELL_DAMAGE",
                       self.ENEMY, boss, 68168, self.PLAYER, "Álvaro", 1297,
                       "9001", q("Dark Bolt"), "32", "12345", "0", "32", "0", "0",
                       "0", "0", "nil", "nil", "nil")
        self.add_event(builder, start + timedelta(seconds=5), "SPELL_HEAL",
                       self.HEALER, "Béatrice", 1297, self.PLAYER, "Álvaro", 1297,
                       "2061", q("Flash Heal"), "2", "8000", "3000", "0", "nil")
        self.add_event(builder, start + timedelta(seconds=6), "SPELL_AURA_APPLIED",
                       self.HEALER, "Béatrice", 1297, self.PLAYER, "Álvaro", 1297,
                       "17", q("Power Word: Shield"), "2", "BUFF")
        self.add_event(builder, start + timedelta(seconds=7), "SPELL_INTERRUPT",
                       self.PLAYER, "Álvaro", 1297, self.ENEMY, boss, 68168,
                       "1766", q("Kick"), "1", "9001", q("Dark Bolt"), "32")
        self.add_event(builder, start + timedelta(seconds=8), "SPELL_DISPEL",
                       self.HEALER, "Béatrice", 1297, self.PLAYER, "Álvaro", 1297,
                       "527", q("Purify"), "2", "123", q("Debuff"), "32", "DEBUFF")
        self.add_event(builder, start + timedelta(seconds=9), "SPELL_SUMMON",
                       self.PLAYER, "Álvaro", 1297, self.PET, "Lobo", 4370,
                       "883", q("Call Pet"), "1")
        self.add_event(builder, start + timedelta(seconds=10), "SPELL_DAMAGE",
                       self.PET, "Lobo", 4370, self.ENEMY, boss, 68168,
                       "17253", q("Bite"), "1", "500", "0", "1", "0", "0", "0", "0")
        self.add_event(builder, start + timedelta(seconds=10, milliseconds=500), "SPELL_DAMAGE",
                       self.PLAYER, "Álvaro", 1297, self.ENEMY, boss, 68168,
                       "1752", q("Sinister Strike"), "1", "700", "0", "1", "0", "0",
                       "0", "0")
        # These are unrelated and must not leak into the compact combat body.
        self.add_event(builder, start + timedelta(seconds=11), "SPELL_DAMAGE",
                       self.OTHER_ENEMY, "Trash A", 68168, self.ENEMY, boss, 68168,
                       "1", q("NPC noise"), "1", "1", "0", "1", "0", "0", "0", "0")
        self.add_event(builder, start + timedelta(seconds=12), "SPELL_HEAL",
                       self.STRAY_PET, "Perro callejero", 4370,
                       self.STRAY_PET, "Perro callejero", 4370,
                       "1", q("Pet noise"), "1", "20", "0", "0", "0")
        self.add_event(builder, start + timedelta(seconds=13), "UNIT_DIED",
                       "0000000000000000", "nil", 0, self.PLAYER, "Álvaro", 1297)
        builder.add(start + timedelta(seconds=70), "ENCOUNTER_END", "9200", q(boss),
                    "16", "20", "1", "69000")
        builder.add(start + timedelta(seconds=81), "SPELL_CAST_SUCCESS", self.PLAYER,
                    q("Álvaro"))
        return builder

    def _run_analysis_raid(self, **option_values):
        builder = self._build_raid_with_actor_events()
        self.write_log(builder.data())
        extractor = self.make_extractor(self.options(**option_values))
        extractor.prepare()
        self.assertEqual(extractor.run_once(), (0, 1, 0))
        legacy = [f for f in self.list_outputs(self.raids_dir()) if f.endswith(".json")]
        basename = os.path.splitext(legacy[0])[0] if legacy else next(
            f for f in self.list_outputs(self.raids_dir()) if os.path.isdir(
                os.path.join(self.raids_dir(), f)))
        return builder, basename

    def _analysis_dir(self, basename):
        return os.path.join(self.raids_dir(), basename, "analysis")

    def test_analysis_keeps_relevant_raw_events_and_drops_unrelated_npc_noise(self):
        _, basename = self._run_analysis_raid(analysis=True)
        analysis_dir = self._analysis_dir(basename)
        with open(os.path.join(analysis_dir, "combat.txt"), encoding="utf-8") as handle:
            combat = handle.read()
        for expected in ("COMBATANT_INFO", "Dark Bolt", "Flash Heal", "Power Word: Shield",
                         "SPELL_INTERRUPT", "SPELL_DISPEL", "Call Pet", "UNIT_DIED"):
            self.assertIn(expected, combat)
        self.assertNotIn("NPC noise", combat)
        self.assertNotIn("Pet noise", combat)
        # Outgoing pet damage is aggregated but not written unless asked for.
        self.assertNotIn("Bite", combat)
        self.assertLess(combat.index("SPELL_CAST_START"), combat.index("SPELL_DAMAGE"))

    def test_pet_to_npc_damage_is_written_only_with_keep_player_damage(self):
        self.assertIn("Bite", self._combat_text(analysis=True, keep_player_damage=True))

    def test_death_window_players_and_pet_ownership_are_objective(self):
        _, basename = self._run_analysis_raid(analysis=True)
        analysis_dir = self._analysis_dir(basename)
        deaths = self.read_json(os.path.join(analysis_dir, "deaths.json"))
        self.assertEqual(len(deaths), 1)
        death = deaths[0]
        self.assertEqual(death["player_guid"], self.PLAYER)
        self.assertIn("UNIT_DIED", death["raw"])
        self.assertGreaterEqual(death["window_seconds"], 12)
        self.assertLessEqual(death["window_seconds"], 20)
        self.assertNotIn("fault", death)
        self.assertNotIn("avoidable", death)
        self.assertNotIn("missed_interrupt", death)
        encoded = json.dumps(death, ensure_ascii=False)
        self.assertIn("Dark Bolt", encoded)
        self.assertIn("Flash Heal", encoded)
        self.assertIn("Power Word: Shield", encoded)

        players = self.read_json(os.path.join(analysis_dir, "players.json"))["players"]
        player = next(row for row in players if row["guid"] == self.PLAYER)
        self.assertEqual(player["name"], "Álvaro")
        self.assertIn(self.PET, json.dumps(player, ensure_ascii=False))
        self.assertGreaterEqual(player["interrupts"], 1)
        self.assertGreaterEqual(player["deaths"], 1)

    def test_analysis_only_has_no_legacy_full_and_full_analysis_preserves_legacy(self):
        _, basename = self._run_analysis_raid(analysis_only=True)
        outputs = self.list_outputs(self.raids_dir())
        self.assertFalse(any(f.endswith((".txt", ".txt.gz", ".json")) for f in outputs), outputs)
        self.assertTrue(os.path.isfile(os.path.join(self._analysis_dir(basename), "metadata.json")))

        # A separate clean output proves --analysis still publishes the unchanged full pair.
        with tempfile.TemporaryDirectory() as second:
            output = os.path.join(second, "Output")
            self.write_log(self._build_raid_with_actor_events().data())
            extractor = wle.Extractor(self.log_dir, output,
                                      state_path=os.path.join(output, wle.STATE_FILENAME),
                                      verbose=False, output_options=self.options(analysis=True))
            extractor.prepare()
            self.assertEqual(extractor.run_once(), (0, 1, 0))
            roots = os.listdir(os.path.join(output, wle.RAID_DIR_NAME))
            self.assertTrue(any(name.endswith(".txt") for name in roots), roots)
            self.assertTrue(any(name.endswith(".json") for name in roots), roots)

    def test_gzip_is_lossless_and_deterministic_between_clean_runs(self):
        builder, basename = self._run_analysis_raid(analysis_only=True, gzip=True)
        compressed = os.path.join(self._analysis_dir(basename), "combat.txt.gz")
        with gzip.open(compressed, "rb") as handle:
            first = handle.read()
        self.assertIn(b"SPELL_INTERRUPT", first)

        # A reset in a separate output must reproduce the exact gzip container bytes,
        # not merely equivalent decompressed content.
        with tempfile.TemporaryDirectory() as second:
            output = os.path.join(second, "Output")
            self.write_log(builder.data())
            extractor = wle.Extractor(self.log_dir, output,
                                      state_path=os.path.join(output, wle.STATE_FILENAME),
                                      verbose=False,
                                      output_options=self.options(analysis_only=True, gzip=True))
            extractor.prepare()
            self.assertEqual(extractor.run_once(), (0, 1, 0))
            root = next(f for f in os.listdir(os.path.join(output, wle.RAID_DIR_NAME))
                        if os.path.isdir(os.path.join(output, wle.RAID_DIR_NAME, f)))
            with open(os.path.join(output, wle.RAID_DIR_NAME, root, "analysis", "combat.txt.gz"),
                      "rb") as handle:
                second_bytes = handle.read()
            with open(compressed, "rb") as handle:
                self.assertEqual(second_bytes, handle.read())

    def test_bundle_contains_analysis_payload_and_metadata_marker_has_real_zip_size(self):
        _, basename = self._run_analysis_raid(analysis=True, bundle=True)
        analysis_dir = self._analysis_dir(basename)
        archive = os.path.join(self.raids_dir(), basename + "_analysis.zip")
        self.assertTrue(os.path.isfile(archive))
        with zipfile.ZipFile(archive) as bundle:
            self.assertEqual(sorted(bundle.namelist()),
                             ["combat.txt", "deaths.json", "metadata.json", "players.json",
                              "summary.json"])
            for name in ("combat.txt", "deaths.json", "players.json", "summary.json"):
                with open(os.path.join(analysis_dir, name), "rb") as handle:
                    self.assertEqual(bundle.read(name), handle.read())
            embedded = json.loads(bundle.read("metadata.json"))
        marker = self.read_json(os.path.join(analysis_dir, "metadata.json"))
        self.assertIsNone(embedded["analysis_zip_bytes"])
        self.assertEqual(marker["analysis_zip_bytes"], os.path.getsize(archive))

    def test_mplus_encounter_is_kept_inside_mplus_and_incremental_profile_does_not_duplicate(self):
        start = datetime(2026, 8, 30, 23, 0, 0)
        builder = LogBuilder()
        builder.add(start, "CHALLENGE_MODE_START", q("Valle Cegador"), "2859", "584",
                    "10", "[158]")
        builder.add(start + timedelta(seconds=4), "ENCOUNTER_START", "3199", q("Boss M+"),
                    "16", "5", "2859")
        builder.add(start + timedelta(seconds=20), "ENCOUNTER_END", "3199", q("Boss M+"),
                    "16", "5", "1", "16000")
        builder.add(start + timedelta(seconds=30), "CHALLENGE_MODE_END", "2859", "1", "10",
                    "30000", "0", "0")
        builder.add(start + timedelta(seconds=42), "SPELL_CAST_SUCCESS", self.PLAYER, q("Álvaro"))
        self.write_log(builder.data())
        extractor = self.make_extractor(self.options(analysis_only=True))
        extractor.prepare()
        self.assertEqual(extractor.run_once(), (1, 0, 0))
        self.assertFalse(os.path.exists(self.raids_dir()))
        first = self.list_outputs(self.mplus_dir())
        self.assertEqual(extractor.run_once(), (0, 0, 0))
        self.assertEqual(self.list_outputs(self.mplus_dir()), first)

    # Keep the required filter cases independently named: a failure pinpoints the
    # policy regression instead of leaving a reviewer to infer it from one omnibus
    # fixture assertion.
    def _combat_text(self, **option_values):
        option_values.setdefault("analysis", True)
        _, basename = self._run_analysis_raid(**option_values)
        with open(os.path.join(self._analysis_dir(basename), "combat.txt"), encoding="utf-8") as h:
            return h.read()

    def test_filter_npc_to_player_damage_is_retained(self):
        self.assertIn("Dark Bolt", self._combat_text())

    def test_filter_player_to_npc_interrupt_is_retained(self):
        self.assertIn("SPELL_INTERRUPT", self._combat_text())

    def test_filter_direct_player_to_npc_damage_is_aggregated_not_written_by_default(self):
        _, basename = self._run_analysis_raid(analysis=True)
        with open(os.path.join(self._analysis_dir(basename), "combat.txt"),
                  encoding="utf-8") as handle:
            self.assertNotIn("Sinister Strike", handle.read())
        summary = self.read_json(os.path.join(self._analysis_dir(basename), "summary.json"))
        players = self.read_json(os.path.join(self._analysis_dir(basename),
                                              "players.json"))["players"]
        player = next(row for row in players if row["guid"] == self.PLAYER)
        # 700 (Sinister Strike) + 500 (the pet's Bite, attributed to its owner).
        self.assertEqual(player["damage_done"], 1200)
        self.assertEqual(summary["event_counts"]["SPELL_DAMAGE"], 3)

    def test_filter_direct_player_to_npc_damage_is_written_with_keep_player_damage(self):
        self.assertIn("Sinister Strike",
                      self._combat_text(analysis=True, keep_player_damage=True))

    def test_filter_unknown_npc_to_npc_is_discarded(self):
        self.assertNotIn("NPC noise", self._combat_text())

    def test_filter_irrelevant_pet_to_pet_healing_is_discarded(self):
        self.assertNotIn("Pet noise", self._combat_text())

    def test_filter_player_healing_and_aura_are_retained(self):
        combat = self._combat_text()
        self.assertIn("Flash Heal", combat)
        self.assertIn("Power Word: Shield", combat)

    def test_filter_dispel_is_retained(self):
        self.assertIn("SPELL_DISPEL", self._combat_text())

    def test_combatant_info_identifies_player_and_preserves_unicode(self):
        _, basename = self._run_analysis_raid(analysis=True)
        players = self.read_json(
            os.path.join(self._analysis_dir(basename), "players.json"))["players"]
        self.assertTrue(any(row["guid"] == self.PLAYER and row["name"] == "Álvaro"
                            for row in players))

    def test_pet_owner_link_is_retained_without_creating_pet_player(self):
        _, basename = self._run_analysis_raid(analysis=True)
        players = self.read_json(
            os.path.join(self._analysis_dir(basename), "players.json"))["players"]
        self.assertFalse(any(row["guid"] == self.PET for row in players))
        self.assertIn(self.PET, json.dumps(players, ensure_ascii=False))

    def test_raid_kill_routes_to_raids_with_analysis(self):
        _, basename = self._run_analysis_raid(analysis=True)
        self.assertIn("Raid_Señor-Ñandú_Mythic_Kill", basename)
        self.assertTrue(os.path.isdir(self._analysis_dir(basename)))
        self.assertFalse(os.path.exists(self.mplus_dir()))

    def test_analysis_mode_publishes_all_five_analysis_artifacts(self):
        _, basename = self._run_analysis_raid(analysis=True)
        self.assertEqual(sorted(os.listdir(self._analysis_dir(basename))),
                         ["combat.txt", "deaths.json", "metadata.json", "players.json",
                          "summary.json"])

    def test_profile_transition_from_full_to_analysis_backfills_once_and_preserves_full(self):
        builder = self._build_raid_with_actor_events()
        self.write_log(builder.data())
        full = self.make_extractor(self.options())
        full.prepare()
        self.assertEqual(full.run_once(), (0, 1, 0))
        original = self.list_outputs(self.raids_dir())
        analysis = self.make_extractor(self.options(analysis_only=True))
        analysis.prepare()
        self.assertEqual(analysis.run_once(), (0, 1, 0))
        after_first_backfill = self.list_outputs(self.raids_dir())
        self.assertTrue(any(name.endswith(".txt") for name in after_first_backfill))
        self.assertTrue(any(os.path.isdir(os.path.join(self.raids_dir(), name))
                            for name in after_first_backfill))
        self.assertEqual(analysis.run_once(), (0, 0, 0))
        self.assertEqual(self.list_outputs(self.raids_dir()), after_first_backfill)
        self.assertTrue(set(original).issubset(after_first_backfill))
        # The analysis publication owns the shared destinations, so it also owns the
        # state entry: the full profile's offset is dropped (it would republish once)
        # while every artifact the full profile wrote stays untouched on disk.
        entry = self.read_json(self.state_path)["files"][LOG_NAME]
        self.assertEqual(list(entry["profiles"]),
                         [self.options(analysis_only=True).profile])
        self.assertNotIn("offset", entry)

    def test_analysis_watch_finalizes_and_repeating_watch_creates_no_duplicate(self):
        builder = self._build_raid_with_actor_events()
        self.write_log(builder.data())
        extractor = self.make_extractor(self.options(analysis_only=True))
        extractor.prepare()
        self.assertEqual(extractor.watch(interval=0, max_polls=1), (0, 1, 0))
        first = self.list_outputs(self.raids_dir())
        repeated = self.make_extractor(self.options(analysis_only=True))
        repeated.prepare()
        self.assertEqual(repeated.watch(interval=0, max_polls=1), (0, 0, 0))
        self.assertEqual(self.list_outputs(self.raids_dir()), first)

    def test_state_v1_migrates_to_isolated_output_profiles(self):
        builder = self._build_raid_with_actor_events()
        data = builder.data()
        log_path = self.write_log(data)
        offset = len(data)
        head_hash, tail_hash = wle.StateStore._hashes(log_path, offset)
        os.makedirs(self.output_dir, exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "files": {os.path.basename(log_path): {
                "offset": offset, "size": offset, "mtime": os.path.getmtime(log_path),
                "head_hash": head_hash, "tail_hash": tail_hash}}}, handle)
        legacy = wle.StateStore(self.state_path)
        legacy.load()
        self.assertEqual(legacy.get_offset(log_path), offset)
        analysis = self.make_extractor(self.options(analysis_only=True))
        analysis.prepare()
        self.assertEqual(analysis.run_once(), (0, 1, 0))
        state = self.read_json(self.state_path)
        self.assertEqual(state["version"], 2)
        entry = state["files"][os.path.basename(log_path)]
        self.assertIn("profiles", entry)
        self.assertEqual(list(entry["profiles"]),
                         [self.options(analysis_only=True).profile])
        # The v1 top-level mirror belongs to the full profile alone: an analysis-only
        # publication must not claim that offset for a binary that ignores profiles.
        self.assertNotIn("offset", entry)

    def test_real_retail_advanced_payload_offsets_are_parsed_objectively(self):
        damage_line = (
            '8/30/2026 10:25:36.8911  SPELL_DAMAGE,Player-1,"Attacker",0x512,0x0,'
            'Creature-1,"Target",0xa48,0x0,195292,"Death Caress",0x20,'
            'Creature-1,0000000000000000,6441154,6444997,0,0,1470,0,0,0,0,'
            '9916,9916,0,1309.42,2057.04,2500,1.0133,90,3843,1846,-1,32,0,0,0,1,nil,nil,ST')
        _, event, args = wle.parse_line(damage_line, 2026)
        parsed = wle.parse_combat_event(event, args)
        self.assertEqual(parsed.amount, 3843)
        self.assertEqual(parsed.absorbed, 0)
        self.assertEqual((parsed.target_hp, parsed.target_max_hp), (6441154, 6444997))
        self.assertEqual((parsed.x, parsed.y), (1309.42, 2057.04))

        heal_line = (
            '8/30/2026 10:25:28.6591  SPELL_HEAL,Player-2,"Healer",0x511,0x0,'
            'Player-1,"Target",0x512,0x0,142421,"Quick Relief",0x8,Player-1,'
            '0000000000000000,666840,666840,2728,2623,968,1288,0,0,0,239075,'
            '250000,0,1286.35,2120.52,2500,4.9869,303,6405,8000,1595,0,nil')
        _, event, args = wle.parse_line(heal_line, 2026)
        parsed = wle.parse_combat_event(event, args)
        self.assertEqual((parsed.amount, parsed.overheal, parsed.absorbed), (8000, 1595, 0))
        self.assertEqual((parsed.target_hp, parsed.target_max_hp), (666840, 666840))

    def test_modern_suffixes_parse_without_advanced_state(self):
        damage_args = list(self.header(self.ENEMY, "Enemy", 68168,
                                       self.PLAYER, "Player", 1297)) + [
            "195292", q("Death Caress"), "0x20", "3843", "5689", "-1",
            "32", "0", "0", "777", "nil", "nil", "nil", "ST"]
        damage = wle.parse_combat_event("SPELL_DAMAGE", damage_args)
        self.assertEqual((damage.amount, damage.absorbed), (3843, 777))

        heal_args = list(self.header(self.HEALER, "Healer", 1297,
                                     self.PLAYER, "Player", 1297)) + [
            "142421", q("Quick Relief"), "0x8", "6405", "8000", "1595", "0", "nil"]
        heal = wle.parse_combat_event("SPELL_HEAL", heal_args)
        self.assertEqual((heal.amount, heal.overheal, heal.absorbed), (8000, 1595, 0))

    def test_real_retail_combatant_info_finds_spec_before_talents(self):
        args = [self.PLAYER] + ["0"] * 23 + ["1480", "[(90929,112839,1)]",
                                                     "()", "[]", "[]"]
        parsed = wle.parse_combat_event("COMBATANT_INFO", args)
        self.assertEqual(parsed.spec_id, 1480)
        self.assertIsNone(parsed.spell_id)
        self.assertEqual(wle.SPEC_ROLES[parsed.spec_id], "DAMAGER")

    def test_real_retail_absorb_forms_keep_amount_and_shield(self):
        swing_args = list(self.header(self.ENEMY, "Enemy", 68168,
                                      self.PLAYER, "Player", 1297)) + [
            self.PLAYER, q("Player"), "0x512", "0x0", "207203",
            q("Ice Barrier"), "0x10", "4780", "281043", "nil"]
        parsed = wle.parse_combat_event("SPELL_ABSORBED", swing_args)
        self.assertEqual((parsed.amount, parsed.extra_spell_id), (4780, 207203))

        spell_args = list(self.header(self.PLAYER, "Player", 1297,
                                      self.ENEMY, "Enemy", 68168)) + [
            "52212", q("Death and Decay"), "0x20", self.ENEMY, q("Enemy"),
            "0xa48", "0x0", "1238158", q("Pollination"), "0x1", "1373",
            "1256", "nil"]
        parsed = wle.parse_combat_event("SPELL_ABSORBED", spell_args)
        self.assertEqual((parsed.amount, parsed.spell_id, parsed.extra_spell_id),
                         (1373, 52212, 1238158))

    def test_enemy_pet_interaction_does_not_invent_player_ownership(self):
        with tempfile.TemporaryDirectory() as stage:
            session = wle.AnalysisSession(stage, wle.KIND_RAID)
            timestamp = datetime(2026, 8, 30, 20, 0, 0)
            args = list(self.header(self.PET, "Hostile pet", 4168,
                                    self.PLAYER, "Player", 1297)) + [
                "1", q("Bite"), "1", "100", "0", "1", "0", "0", "0"]
            session.consume(line_bytes(timestamp, "SPELL_DAMAGE", *args), timestamp,
                            "SPELL_DAMAGE", args)
            self.assertNotIn(self.PET, session.pet_owners)
            self.assertIn(self.PET, session.hostiles)
            session.close_streams()

    def test_mplus_death_records_active_internal_boss(self):
        start = datetime(2026, 8, 30, 23, 30, 0)
        builder = LogBuilder()
        builder.add(start, "CHALLENGE_MODE_START", q("Dungeon"), "2859", "584", "10", "[158]")
        builder.add(start + timedelta(seconds=1), "ENCOUNTER_START", "3199", q("Boss M+"),
                    "16", "5", "2859")
        self.add_event(builder, start + timedelta(seconds=2), "SPELL_DAMAGE", self.ENEMY,
                       "Boss M+", 68168, self.PLAYER, "Player", 1297,
                       "9001", q("Lethal"), "32", "12345", "0", "32", "0", "0", "0")
        self.add_event(builder, start + timedelta(seconds=3), "UNIT_DIED",
                       "0000000000000000", "nil", 0, self.PLAYER, "Player", 1297)
        builder.add(start + timedelta(seconds=4), "ENCOUNTER_END", "3199", q("Boss M+"),
                    "16", "5", "0", "3000")
        builder.add(start + timedelta(seconds=5), "CHALLENGE_MODE_END", "2859", "1", "10",
                    "5000", "0", "0")
        builder.add(start + timedelta(seconds=16), "SPELL_CAST_SUCCESS", self.PLAYER, q("Player"))
        self.write_log(builder.data())
        extractor = self.make_extractor(self.options(analysis_only=True))
        extractor.prepare()
        self.assertEqual(extractor.run_once(), (1, 0, 0))
        basename = next(name for name in self.list_outputs(self.mplus_dir())
                        if os.path.isdir(os.path.join(self.mplus_dir(), name)))
        deaths = self.read_json(os.path.join(self.mplus_dir(), basename, "analysis", "deaths.json"))
        self.assertEqual(deaths[0]["encounter"]["type"], "mythic_plus")
        self.assertEqual(deaths[0]["encounter"]["boss"], "Boss M+")

    def test_summary_players_and_metadata_have_objective_aggregates(self):
        _, basename = self._run_analysis_raid(analysis=True)
        analysis_dir = self._analysis_dir(basename)
        players = self.read_json(os.path.join(analysis_dir, "players.json"))["players"]
        player = next(row for row in players if row["guid"] == self.PLAYER)
        self.assertEqual(player["spec_id"], 65)
        self.assertEqual(player["role"], "HEALER")
        self.assertEqual(player["damage_taken"], 12345)
        self.assertEqual(player["healing_received"], 5000)
        summary = self.read_json(os.path.join(analysis_dir, "summary.json"))
        self.assertEqual(summary["player_deaths"], 1)
        self.assertEqual(summary["interrupt_count"], 1)
        self.assertEqual(summary["dispel_count"], 1)
        metadata = self.read_json(os.path.join(analysis_dir, "metadata.json"))
        self.assertGreater(metadata["full_uncompressed_bytes"],
                           metadata["combat_uncompressed_bytes"])
        self.assertAlmostEqual(metadata["reduction_percent"],
                               100 * (metadata["full_uncompressed_bytes"] -
                                      metadata["combat_uncompressed_bytes"]) /
                               metadata["full_uncompressed_bytes"], places=2)

    def test_required_party_kill_and_relevant_failed_dispel_are_retained(self):
        start = datetime(2026, 8, 30, 21, 0, 0)
        builder = LogBuilder()
        builder.add(start, "ENCOUNTER_START", "9201", q("Boss"), "15", "10", "2900")
        self.add_event(builder, start + timedelta(seconds=1), "SPELL_DISPEL_FAILED",
                       self.ENEMY, "Boss", 68168, self.OTHER_ENEMY, "Add", 68168,
                       "1", q("Dispel"), "1", "2", q("Debuff"), "1")
        self.add_event(builder, start + timedelta(seconds=2), "SPELL_DAMAGE", self.ENEMY,
                       "Boss", 68168, self.PLAYER, "Player", 1297,
                       "3", q("Hit"), "1", "10", "0", "1", "0", "0", "0")
        self.add_event(builder, start + timedelta(seconds=3), "PARTY_KILL",
                       "0000000000000000", "nil", 0, self.OTHER_ENEMY, "Add", 68168)
        builder.add(start + timedelta(seconds=4), "ENCOUNTER_END", "9201", q("Boss"),
                    "15", "10", "0", "4000")
        builder.add(start + timedelta(seconds=15), "SPELL_CAST_SUCCESS", self.PLAYER, q("Player"))
        self.write_log(builder.data())
        extractor = self.make_extractor(self.options(analysis_only=True))
        extractor.prepare()
        self.assertEqual(extractor.run_once(), (0, 1, 0))
        basename = next(name for name in self.list_outputs(self.raids_dir())
                        if os.path.isdir(os.path.join(self.raids_dir(), name)))
        self.assertIn("_Wipe", basename)
        with open(os.path.join(self.raids_dir(), basename, "analysis", "combat.txt"),
                  encoding="utf-8") as handle:
            combat = handle.read()
        self.assertIn("SPELL_DISPEL_FAILED", combat)
        self.assertIn("PARTY_KILL", combat)
        summary = self.read_json(os.path.join(self.raids_dir(), basename, "analysis",
                                              "summary.json"))
        self.assertFalse(summary["success"])

    def test_full_plus_analysis_gzip_is_valid_and_full_is_lossless(self):
        builder = self._build_raid_with_actor_events()
        raw = builder.data()
        self.write_log(raw)
        extractor = self.make_extractor(self.options(analysis=True, gzip=True))
        extractor.prepare()
        self.assertEqual(extractor.run_once(), (0, 1, 0))
        full_path = next(os.path.join(self.raids_dir(), name)
                         for name in self.list_outputs(self.raids_dir())
                         if name.endswith(".txt.gz"))
        with gzip.open(full_path, "rb") as handle:
            uncompressed = handle.read()
        # The final synthetic line is 11 seconds after ENCOUNTER_END and triggers
        # publication without becoming part of the 10-second lossless context.
        self.assertEqual(uncompressed, b"".join(raw.splitlines(keepends=True)[:-1]))

    def test_output_lock_rejects_a_second_writer(self):
        first = wle.OutputLock(self.output_dir)
        second = wle.OutputLock(self.output_dir)
        first.acquire()
        try:
            with self.assertRaises(RuntimeError):
                second.acquire()
        finally:
            first.release()

    def test_analysis_only_completion_preserves_prior_incomplete_full(self):
        start = datetime(2026, 8, 30, 19, 0, 0)
        initial = LogBuilder()
        initial.add(start, "ENCOUNTER_START", "9300", q("Growing Boss"), "15", "10", "2900")
        log_path = self.write_log(initial.data())
        old_time = time.time() - wle.STALE_SECONDS - 5
        os.utime(log_path, (old_time, old_time))
        full = self.make_extractor(self.options())
        full.prepare()
        self.assertEqual(full.run_once(), (0, 1, 0))
        incomplete_body = next(os.path.join(self.raids_dir(), name)
                               for name in self.list_outputs(self.raids_dir())
                               if name.endswith("_INCOMPLETE.txt"))
        with open(incomplete_body, "rb") as handle:
            original_body = handle.read()

        tail = LogBuilder()
        tail.add(start + timedelta(seconds=5), "ENCOUNTER_END", "9300", q("Growing Boss"),
                 "15", "10", "0", "5000")
        tail.add(start + timedelta(seconds=16), "SPELL_CAST_SUCCESS", self.PLAYER, q("Player"))
        self.append_log(tail.data())
        analysis = self.make_extractor(self.options(analysis_only=True))
        analysis.prepare()
        self.assertEqual(analysis.run_once(), (0, 1, 0))
        self.assertTrue(os.path.isfile(incomplete_body))
        with open(incomplete_body, "rb") as handle:
            self.assertEqual(handle.read(), original_body)
        self.assertTrue(any("_Wipe" in name and
                            os.path.isfile(os.path.join(self.raids_dir(), name, "analysis",
                                                        "metadata.json"))
                            for name in self.list_outputs(self.raids_dir())))

    def test_hostile_ttl_retires_destination_auras_without_incomplete_warning(self):
        with tempfile.TemporaryDirectory() as stage:
            session = wle.AnalysisSession(stage, wle.KIND_MPLUS)
            start = datetime(2026, 8, 30, 20, 0, 0)
            damage_args = list(self.header(self.PLAYER, "Player", 1297,
                                           self.ENEMY, "Enemy", 68168)) + [
                "1", q("Hit"), "1", "10", "0", "1", "0", "0", "0"]
            session.consume(line_bytes(start, "SPELL_DAMAGE", *damage_args), start,
                            "SPELL_DAMAGE", damage_args)
            aura_args = list(self.header(self.PLAYER, "Player", 1297,
                                         self.ENEMY, "Enemy", 68168)) + [
                "2", q("Debuff"), "1", "DEBUFF"]
            session.consume(line_bytes(start, "SPELL_AURA_APPLIED", *aura_args), start,
                            "SPELL_AURA_APPLIED", aura_args)
            self.assertTrue(session.active_auras)
            expired = start + timedelta(seconds=wle.HOSTILE_TTL_SECONDS + 1)
            session.consume(line_bytes(expired, "ZONE_CHANGE", q("Elsewhere")), expired,
                            "ZONE_CHANGE", [q("Elsewhere")])
            self.assertFalse(session.active_auras)
            self.assertFalse(session.persistent_incomplete)
            session.close_streams()

    def test_cleanup_partials_removes_nested_analysis_temp(self):
        publisher = wle.SegmentPublisher(self.output_dir, verbose=False,
                                         output_options=self.options(analysis_only=True))
        analysis_dir = os.path.join(publisher.raids_dir, "Example", "analysis")
        os.makedirs(analysis_dir, exist_ok=True)
        temp_path = os.path.join(analysis_dir, ".combat.txt.deadbeef.tmp")
        with open(temp_path, "wb") as handle:
            handle.write(b"partial")
        self.assertGreaterEqual(publisher.cleanup_partials(), 1)
        self.assertFalse(os.path.exists(temp_path))

    def test_active_aura_cap_marks_only_affected_death_and_unit_died_cleans_up(self):
        with tempfile.TemporaryDirectory() as stage, mock.patch.object(
                wle, "MAX_ACTIVE_AURAS", 1):
            session = wle.AnalysisSession(stage, wle.KIND_RAID)
            start = datetime(2026, 8, 30, 20, 0, 0)
            for spell_id in ("1", "2"):
                args = list(self.header(self.ENEMY, "Enemy", 68168,
                                        self.PLAYER, "Player", 1297)) + [
                    spell_id, q("Debuff " + spell_id), "1", "DEBUFF"]
                session.consume(line_bytes(start, "SPELL_AURA_APPLIED", *args), start,
                                "SPELL_AURA_APPLIED", args)
            death_args = list(self.header("0000000000000000", "nil", 0,
                                          self.PLAYER, "Player", 1297))
            death_time = start + timedelta(seconds=1)
            session.consume(line_bytes(death_time, "UNIT_DIED", *death_args), death_time,
                            "UNIT_DIED", death_args)
            self.assertFalse(session.active_auras)
            session.close_streams()
            deaths = session.deaths()
            self.assertTrue(deaths[0]["analysis_incomplete"])
            self.assertIn("active_auras_truncated", deaths[0]["incomplete_reasons"])

    def test_player_aggregate_cap_never_drops_raw_player_death(self):
        with tempfile.TemporaryDirectory() as stage, mock.patch.object(
                wle, "MAX_PLAYER_AGGREGATES", 1):
            session = wle.AnalysisSession(stage, wle.KIND_RAID)
            start = datetime(2026, 8, 30, 20, 0, 0)
            first = self.header(self.PLAYER, "First", 1297, self.ENEMY, "Enemy", 68168)
            session.consume(line_bytes(start, "SPELL_CAST_SUCCESS", *first,
                                       "1", q("Cast"), "1"), start,
                            "SPELL_CAST_SUCCESS", list(first) + ["1", q("Cast"), "1"])
            second_guid = "Player-1-00000099"
            second = self.header(self.ENEMY, "Enemy", 68168, second_guid, "Second", 1297)
            session.consume(line_bytes(start, "SPELL_DAMAGE", *second,
                                       "2", q("Hit"), "1", "100", "0", "1", "0", "0", "0"),
                            start, "SPELL_DAMAGE",
                            list(second) + ["2", q("Hit"), "1", "100", "0", "1",
                                            "0", "0", "0"])
            death_args = list(self.header("0000000000000000", "nil", 0,
                                          second_guid, "Second", 1297))
            death_time = start + timedelta(seconds=1)
            session.consume(line_bytes(death_time, "UNIT_DIED", *death_args), death_time,
                            "UNIT_DIED", death_args)
            session.close_streams()
            with open(session.combat_raw_path, "rb") as handle:
                combat = handle.read()
            self.assertIn(second_guid.encode(), combat)
            self.assertEqual(len(session.deaths()), 1)
            self.assertEqual(len(session.players), 1)
            self.assertIn("player_aggregates_truncated", session.warnings)

    def test_analysis_marker_crash_retries_without_duplicate_bundle(self):
        builder = self._build_raid_with_actor_events()
        self.write_log(builder.data())
        extractor = self.make_extractor(self.options(analysis_only=True))
        extractor.prepare()
        real_atomic = wle._atomic_write_bytes

        def fail_marker(path, data):
            if path.endswith(os.path.join("analysis", "metadata.json")):
                raise OSError("simulated analysis marker crash")
            return real_atomic(path, data)

        with mock.patch.object(wle, "_atomic_write_bytes", side_effect=fail_marker):
            self.assertEqual(extractor.run_once(), (0, 0, 1))
        retry = self.make_extractor(self.options(analysis_only=True))
        retry.prepare()
        self.assertEqual(retry.run_once(), (0, 1, 0))
        bundles = [name for name in self.list_outputs(self.raids_dir())
                   if os.path.isfile(os.path.join(self.raids_dir(), name, "analysis",
                                                  "metadata.json"))]
        self.assertEqual(len(bundles), 1)

    # --- v2 relevance policy, parser fields and JSON shapes ------------------------

    # The 17 fields that follow (infoGUID, ownerGUID) in Retail's advanced block,
    # copied from a real 12.x line so the offsets are not invented.
    ADVANCED_TAIL = ["375809", "375809", "7580", "15292", "3186", "0", "0", "20902",
                     "3", "140", "200", "0", "1321.13", "2058.02", "2500", "5.7692",
                     "302"]
    REAL_PET = "Pet-0-3891-2859-182905-417-0204A8C513"
    REAL_OWNER = "Player-1408-0B450687"
    REAL_SWING = (
        '8/30/2026 10:23:24.1000  SWING_DAMAGE,Pet-0-3891-2859-182905-417-0204A8C513,'
        '"Ghaazun",0x1112,0x80000000,Creature-0-3891-2859-182905-245336-000513F705,'
        '"Siembrahechizos radiante",0x10a48,0x80000000,'
        'Pet-0-3891-2859-182905-417-0204A8C513,Player-1408-0B450687,375809,375809,'
        '7580,15292,3186,0,0,20902,3,140,200,0,1321.13,2058.02,2500,5.7692,302,2131,'
        '3044,-1,1,0,0,0,nil,nil,nil')
    REAL_PET_CAST = (
        '8/30/2026 10:23:25.1000  SPELL_CAST_SUCCESS,'
        'Pet-0-3891-2859-182905-417-0204A8C513,"Ghaazun",0x1112,0x80000000,'
        '0000000000000000,nil,0x80000000,0x80000000,108446,"Enlace de alma",0x20,'
        'Pet-0-3891-2859-182905-417-0204A8C513,Player-1408-0B450687,375809,375809,'
        '7580,15292,3186,0,0,0,3,200,200,0,1288.25,2128.34,2500,4.8837,302')
    REAL_LANDED = (
        '8/30/2026 10:23:26.1000  SWING_DAMAGE_LANDED,'
        'Creature-0-3891-2859-182905-245345-000813F705,"Azotador atiborrado de Luz",'
        '0xa48,0x80000000,Player-3674-07CF2453,"Greendecay-TwistingNether-EU",0x20512,'
        '0x80000020,Player-3674-07CF2453,0000000000000000,1045159,1154320,3535,344,'
        '7160,474,301,0,6,100,1250,0,1310.69,2068.63,2500,5.1383,306,109161,288458,-1,'
        '1,0,0,0,nil,nil,nil')
    REAL_COMBATANT_INFO = (
        '8/30/2026 10:23:27.1000  COMBATANT_INFO,Player-1929-09EA2DF6,1,513,622,33109,'
        '3062,0,0,0,0,864,864,864,0,233,827,827,827,59,671,406,406,406,981,1480,'
        '[(90929,112839,1),(90931,112841,1)],(0,354489,1261697,205596),'
        '[(250033,289,(7961,0,0),(13338,13440,6652,13575,12806,13534),(240908,295)),'
        '(273781,311,(),(12843,13440,41,13668,12699),(240898,295)),'
        '(273774,311,(),(12843,13440,6652,13662,12699),()),(0,0,(),(),()),'
        '(251159,311,(),(12843,13440,41,13662,12699),()),'
        '(251235,311,(),(12843,13440,40,13696,13662,12699),()),'
        '(159313,311,(),(12843,13440,6652,13662,12699),()),'
        '(251153,311,(),(12843,13440,6652,13662,12699),()),'
        '(268240,308,(),(6652,13696,13662,13333,12838),()),'
        '(156489,305,(),(8902,12841,7756,13662,12699),()),'
        '(252258,311,(7965,0,0),(12843,13440,6652,13668,12699),(240898,295)),'
        '(158366,311,(7965,0,0),(12843,13440,6652,13668,12699),(240898,295)),'
        '(270164,305,(),(6652,13333,12841,13662,13696),()),'
        '(250215,311,(),(12843,13440,6652,12699),()),'
        '(156339,308,(),(7756,13662,12838),()),'
        '(160216,311,(0,8051,0),(12843,13440,6652,12701),()),'
        '(251225,311,(0,8051,0),(12843,13440,41,12701),()),(0,0,(),(),())],'
        '[Player-1929-09EA2DF6,1284644,1,Player-1378-0B26B0FA,1126,1,'
        'Player-1379-0AF0E3DD,1459,1],85,0,0,0')

    def _session(self, **kwargs):
        """A session in its own stage, closed before that stage is removed."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        session = wle.AnalysisSession(temp.name, wle.KIND_RAID, **kwargs)
        self.addCleanup(session.close_streams)
        return session

    def _feed(self, session, timestamp, event, args):
        args = list(args)
        session.consume(line_bytes(timestamp, event, *args), timestamp, event, args)

    def _feed_real(self, session, timestamp, text):
        _, event, args = wle.parse_line(text, 2026)
        session.consume(text.encode("utf-8") + b"\r\n", timestamp, event, args)

    @staticmethod
    def _combat_of(session):
        session.close_streams()
        with open(session.combat_raw_path, "rb") as handle:
            return handle.read().decode("utf-8")

    def _spell_args(self, source, source_flags, destination, dest_flags, spell_id,
                    name, *payload):
        return list(self.header(source, "Src", source_flags, destination, "Dst",
                                dest_flags)) + [spell_id, q(name), "1", *payload]

    def _damage_args(self, source, source_flags, destination, dest_flags, name, amount):
        return self._spell_args(source, source_flags, destination, dest_flags, "7",
                                name, amount, "0", "1", "0", "0", "0")

    def _swing_args(self, event_source, source_flags, destination, dest_flags,
                    info_guid, owner, amount):
        """A swing with the 19-field advanced block describing `info_guid`."""
        return list(self.header(event_source, "Src", source_flags, destination, "Dst",
                                dest_flags)) + [info_guid, owner] + self.ADVANCED_TAIL + \
            [amount, "3044", "-1", "1", "0", "0", "0", "nil", "nil", "nil"]

    def _summon_args(self, pet, spell_id="883", name="Call Pet"):
        return list(self.header(self.PLAYER, "Player", 1297, pet, "Pet", 4370)) + \
            [spell_id, q(name), "1"]

    def test_keep_policy_table_rows_decide_count_and_write(self):
        player, enemy, other, pet = self.PLAYER, self.ENEMY, self.OTHER_ENEMY, self.PET
        second_pet = "Pet-0-0001-0002-0003-000000000002"
        summon = ("SPELL_SUMMON", self._summon_args(pet))
        cases = [
            dict(name="structural COMBATANT_INFO", event="COMBATANT_INFO",
                 args=[player] + ["0"] * 23 + ["65", "[(1,2,1)]", "()", "[]", "[]"],
                 marker="COMBATANT_INFO", count=True, write=True),
            dict(name="parse fallback", event="SPELL_DAMAGE",
                 args=["truncated-fallback-line"],
                 marker="truncated-fallback-line", count=True, write=True),
            dict(name="unit died", event="UNIT_DIED",
                 args=list(self.header("0000000000000000", "nil", 0, other, "Add",
                                       68168)),
                 marker="UNIT_DIED", count=True, write=True),
            dict(name="resource energize", event="SPELL_ENERGIZE",
                 args=self._spell_args(player, 1297, player, 1297, "1242475",
                                       "Soul Immolation", "6.0000", "0.0000", "17",
                                       "120"),
                 marker="Soul Immolation", count=False, write=False),
            dict(name="landed on player", event="SWING_DAMAGE_LANDED",
                 args=self._swing_args(enemy, 68168, player, 1297, player,
                                       "0000000000000000", "9170011"),
                 marker="9170011", count=False, write=True),
            dict(name="landed on npc, default", event="SWING_DAMAGE_LANDED",
                 args=self._swing_args(player, 1297, enemy, 68168, enemy,
                                       "0000000000000000", "9170012"),
                 marker="9170012", count=False, write=False),
            dict(name="landed on npc, keep flag", keep=True,
                 event="SWING_DAMAGE_LANDED",
                 args=self._swing_args(player, 1297, enemy, 68168, enemy,
                                       "0000000000000000", "9170013"),
                 marker="9170013", count=False, write=True),
            dict(name="player damage to npc, default", event="SPELL_DAMAGE",
                 args=self._damage_args(player, 1297, enemy, 68168, "Outgoing Strike",
                                        "700"),
                 marker="Outgoing Strike", count=True, write=False, damage_done=700),
            dict(name="player damage to npc, keep flag", keep=True,
                 event="SPELL_DAMAGE",
                 args=self._damage_args(player, 1297, enemy, 68168, "Outgoing Strike",
                                        "700"),
                 marker="Outgoing Strike", count=True, write=True, damage_done=700),
            dict(name="own pet damage to npc", prime=[summon], event="SPELL_DAMAGE",
                 args=self._damage_args(pet, 4370, enemy, 68168, "Pet Strike", "500"),
                 marker="Pet Strike", count=True, write=False, damage_done=500),
            dict(name="outgoing absorb on npc shield", event="SPELL_ABSORBED",
                 args=list(self.header(player, "Src", 1297, enemy, "Dst", 68168)) + [
                     "52212", q("Death and Decay"), "0x20", enemy, q("Dst"), "0xa48",
                     "0x0", "1238158", q("Pollination"), "0x1", "1373", "1256", "nil"],
                 marker="Pollination", count=True, write=False, damage_done=0),
            dict(name="npc damage on player", event="SPELL_DAMAGE",
                 args=self._damage_args(enemy, 68168, player, 1297, "Incoming Bolt",
                                        "900"),
                 marker="Incoming Bolt", count=True, write=True),
            dict(name="player miss on npc", event="SPELL_MISSED",
                 args=self._spell_args(player, 1297, enemy, 68168, "8", "Bad Shot",
                                       "IMMUNE"),
                 marker="Bad Shot", count=True, write=True),
            dict(name="player aura on npc", event="SPELL_AURA_APPLIED",
                 args=self._spell_args(player, 1297, enemy, 68168, "9", "Crowd Control",
                                       "DEBUFF"),
                 marker="Crowd Control", count=True, write=True),
            dict(name="unowned pet heals unowned pet", event="SPELL_HEAL",
                 args=self._spell_args(self.STRAY_PET, 4370, second_pet, 4370, "10",
                                       "Pet Lick", "20", "0", "0", "0"),
                 marker="Pet Lick", count=False, write=False),
            dict(name="owned pet heals itself", prime=[summon], event="SPELL_HEAL",
                 args=self._spell_args(pet, 4370, pet, 4370, "13", "Pet Mend",
                                       "30", "0", "0", "0"),
                 marker="Pet Mend", count=True, write=True),
            dict(name="unknown npc to unknown npc", event="SPELL_DAMAGE",
                 args=self._damage_args(other, 68168, enemy, 68168, "NPC chatter", "1"),
                 marker="NPC chatter", count=False, write=False),
            dict(name="player cast on npc", event="SPELL_CAST_SUCCESS",
                 args=self._spell_args(player, 1297, enemy, 68168, "11", "Big Cast"),
                 marker="Big Cast", count=True, write=True),
            dict(name="heal landing on own pet", prime=[summon], event="SPELL_HEAL",
                 args=self._spell_args(enemy, 68168, pet, 4370, "12", "Mend Pet",
                                       "40", "0", "0", "0"),
                 marker="Mend Pet", count=True, write=True),
        ]
        for case in cases:
            with self.subTest(row=case["name"]):
                session = self._session(
                    keep_player_damage=case.get("keep", False))
                start = datetime(2026, 8, 30, 20, 0, 0)
                for index, (event, args) in enumerate(case.get("prime", [])):
                    self._feed(session, start + timedelta(milliseconds=index), event,
                               args)
                self._feed(session, start + timedelta(seconds=1), case["event"],
                           case["args"])
                self.assertEqual(case["event"] in session.event_counts, case["count"],
                                 "count for %s" % case["name"])
                if "damage_done" in case:
                    self.assertEqual(session.players[self.PLAYER]["damage_done"],
                                     case["damage_done"], "damage for %s" % case["name"])
                combat = self._combat_of(session)
                self.assertEqual(case["marker"] in combat, case["write"],
                                 "write for %s" % case["name"])

    def test_resource_events_leave_no_trace_in_combat_counts_or_death_window(self):
        session = self._session()
        start = datetime(2026, 8, 30, 20, 0, 0)
        self._feed(session, start, "SPELL_DAMAGE",
                   self._damage_args(self.ENEMY, 68168, self.PLAYER, 1297,
                                     "Incoming Bolt", "900"))
        energize = self._spell_args(self.PLAYER, 1297, self.PLAYER, 1297,
                                    "1242475", "Soul Immolation", self.PLAYER,
                                    "0000000000000000")
        self._feed(session, start + timedelta(seconds=1), "SPELL_ENERGIZE",
                   energize + self.ADVANCED_TAIL + ["6.0000", "0.0000", "17", "120"])
        death_time = start + timedelta(seconds=2)
        self._feed(session, death_time, "UNIT_DIED",
                   list(self.header("0000000000000000", "nil", 0, self.PLAYER,
                                    "Dst", 1297)))
        self.assertNotIn("SPELL_ENERGIZE", session.event_counts)
        combat = self._combat_of(session)
        self.assertNotIn("Soul Immolation", combat)
        death = session.deaths()[0]
        self.assertNotIn("SPELL_ENERGIZE",
                         [event["event"] for event in death["events"]])
        self.assertNotIn("Soul Immolation", json.dumps(death, ensure_ascii=False))

    def test_swing_and_landed_pair_report_one_amount_and_the_victim_state(self):
        session = self._session()
        start = datetime(2026, 8, 30, 20, 0, 0)
        self._feed(session, start, "SWING_DAMAGE",
                   self._swing_args(self.ENEMY, 68168, self.PLAYER, 1297,
                                    self.ENEMY, "0000000000000000", "2131"))
        self._feed(session, start + timedelta(milliseconds=1),
                   "SWING_DAMAGE_LANDED",
                   self._swing_args(self.ENEMY, 68168, self.PLAYER, 1297,
                                    self.PLAYER, "0000000000000000", "2131"))
        death_time = start + timedelta(seconds=2)
        self._feed(session, death_time, "UNIT_DIED",
                   list(self.header("0000000000000000", "nil", 0, self.PLAYER,
                                    "Dst", 1297)))
        self.assertEqual(session.players[self.PLAYER]["damage_taken"], 2131)
        self.assertEqual(session.event_counts.get("SWING_DAMAGE"), 1)
        self.assertNotIn("SWING_DAMAGE_LANDED", session.event_counts)
        combat = self._combat_of(session)
        self.assertEqual(combat.count("  SWING_DAMAGE,"), 1)
        self.assertEqual(combat.count("  SWING_DAMAGE_LANDED,"), 1)
        events = [event for event in session.deaths()[0]["events"]
                  if event["event"].startswith("SWING_DAMAGE")]
        self.assertEqual(len(events), 2)
        self.assertEqual([event for event in events if "amount" in event][0]["event"],
                         "SWING_DAMAGE")
        landed = next(event for event in events
                      if event["event"] == "SWING_DAMAGE_LANDED")
        self.assertTrue(landed["supplemental_state"])
        self.assertIn("target_hp", landed)
        self.assertEqual(landed["target_hp"], 375809)
        self.assertEqual(landed["target_max_hp"], 375809)
        self.assertNotIn("amount", landed)
        self.assertNotIn("absorbed", landed)

    def test_outgoing_results_hidden_by_default_keep_identical_damage_done(self):
        totals = {}
        for keep in (False, True):
            with self.subTest(keep_player_damage=keep):
                session = self._session(keep_player_damage=keep)
                start = datetime(2026, 8, 30, 20, 0, 0)
                self._feed(session, start, "SPELL_SUMMON", self._summon_args(self.PET))
                self._feed(session, start + timedelta(seconds=1), "SPELL_DAMAGE",
                           self._damage_args(self.PLAYER, 1297, self.ENEMY, 68168,
                                             "Outgoing Strike", "700"))
                self._feed(session, start + timedelta(seconds=2), "SPELL_DAMAGE",
                           self._damage_args(self.PET, 4370, self.ENEMY, 68168,
                                             "Pet Strike", "500"))
                self._feed(session, start + timedelta(seconds=3), "SWING_DAMAGE",
                           self._swing_args(self.PLAYER, 1297, self.ENEMY, 68168,
                                            self.PLAYER, "0000000000000000", "300777"))
                self._feed(session, start + timedelta(seconds=4),
                           "SWING_DAMAGE_LANDED",
                           self._swing_args(self.PLAYER, 1297, self.ENEMY, 68168,
                                            self.ENEMY, "0000000000000000", "300778"))
                totals[keep] = session.players[self.PLAYER]["damage_done"]
                combat = self._combat_of(session)
                for marker in ("Outgoing Strike", "Pet Strike", "300777", "300778"):
                    self.assertEqual(marker in combat, keep, marker)
        self.assertEqual(totals[False], totals[True])
        self.assertEqual(totals[False], 700 + 500 + 300777)

    def test_real_pet_lines_link_the_owner_through_the_source_block(self):
        _, event, args = wle.parse_line(self.REAL_SWING, 2026)
        swing = wle.parse_combat_event(event, args)
        self.assertEqual(swing.source_owner_guid, self.REAL_OWNER)
        self.assertEqual(swing.amount, 2131)
        self.assertIsNone(swing.target_hp)
        _, event, args = wle.parse_line(self.REAL_PET_CAST, 2026)
        cast = wle.parse_combat_event(event, args)
        self.assertEqual(cast.source_owner_guid, self.REAL_OWNER)
        self.assertEqual(cast.spell_id, 108446)
        self.assertIsNone(cast.target_hp)

    def test_real_swing_tail_reads_absorbed_from_the_modern_offset(self):
        # Modern swing tail: amount, base, overkill, school, resisted, blocked,
        # absorbed, ... (10 fields, no ST/AOE marker). blocked=5 and absorbed=7 make
        # an off-by-one read observable.
        line = self.REAL_SWING.replace("2131,3044,-1,1,0,0,0,nil,nil,nil",
                                       "2131,3044,-1,1,0,5,7,nil,nil,nil")
        _, event, args = wle.parse_line(line, 2026)
        parsed = wle.parse_combat_event(event, args)
        self.assertEqual(parsed.amount, 2131)
        self.assertEqual(parsed.absorbed, 7)
        landed = self.REAL_LANDED.replace("109161,288458,-1,1,0,0,0,nil,nil,nil",
                                          "109161,288458,-1,1,0,5,7,nil,nil,nil")
        _, event, args = wle.parse_line(landed, 2026)
        self.assertEqual(wle.parse_combat_event(event, args).absorbed, 7)

    def test_real_landed_line_is_parsed_as_supplemental_victim_state(self):
        _, event, args = wle.parse_line(self.REAL_LANDED, 2026)
        parsed = wle.parse_combat_event(event, args)
        self.assertEqual(parsed.amount, 109161)
        self.assertEqual((parsed.target_hp, parsed.target_max_hp), (1045159, 1154320))
        data = parsed.as_dict(datetime(2026, 8, 30, 10, 23, 26), b"raw")
        self.assertTrue(data["supplemental_state"])
        self.assertNotIn("amount", data)
        self.assertNotIn("absorbed", data)
        self.assertEqual(data["target_hp"], 1045159)

    def test_pet_is_linked_by_its_source_block_before_any_summon(self):
        session = self._session()
        start = datetime(2026, 8, 30, 20, 0, 0)
        self._feed_real(session, start, self.REAL_PET_CAST)
        self.assertEqual(session.pet_owners.get(self.REAL_PET), self.REAL_OWNER)
        self.assertIn(self.REAL_OWNER, session.players)
        self.assertEqual(session.players[self.REAL_OWNER]["pets"], [self.REAL_PET])
        self._feed(session, start + timedelta(seconds=1), "SPELL_DAMAGE",
                   self._damage_args(self.REAL_PET, 4370, self.ENEMY, 68168,
                                     "Pet Strike", "500"))
        self.assertEqual(session.players[self.REAL_OWNER]["damage_done"], 500)

    def test_source_block_owner_is_ignored_when_it_is_not_a_player(self):
        for owner in ("nil", "0000000000000000",
                      "Creature-0-3891-2859-182905-245336-000513F705"):
            with self.subTest(owner=owner):
                session = self._session()
                start = datetime(2026, 8, 30, 20, 0, 0)
                args = list(self.header(self.REAL_PET, "Ghaazun", 4370,
                                        "0000000000000000", "nil", 0)) + [
                    "108446", q("Enlace de alma"), "0x20", self.REAL_PET, owner] + \
                    self.ADVANCED_TAIL
                self._feed(session, start, "SPELL_CAST_SUCCESS", args)
                self.assertNotIn(self.REAL_PET, session.pet_owners)

    def test_hostile_pet_with_a_player_owner_stays_hostile_and_unowned(self):
        session = self._session()
        start = datetime(2026, 8, 30, 20, 0, 0)
        hostile_pet_flags = 4370 | wle.REACTION_HOSTILE
        args = list(self.header(self.REAL_PET, "Enemy pet", hostile_pet_flags,
                                self.PLAYER, "Dst", 1297)) + [
            "7", q("Enemy Bite"), "1", self.REAL_PET, self.REAL_OWNER] + \
            self.ADVANCED_TAIL + ["400", "0", "1", "0", "0", "0"]
        self._feed(session, start, "SPELL_DAMAGE", args)
        self.assertNotIn(self.REAL_PET, session.pet_owners)
        self.assertNotIn(self.REAL_OWNER, session.players)
        self.assertIn(self.REAL_PET, session.hostiles)
        self.assertEqual(session.players[self.PLAYER]["damage_taken"], 400)
        self.assertIn("Enemy Bite", self._combat_of(session))

    def test_pet_owner_beyond_the_player_cap_neither_breaks_nor_attributes(self):
        with mock.patch.object(wle, "MAX_PLAYER_AGGREGATES", 1):
            session = self._session()
            start = datetime(2026, 8, 30, 20, 0, 0)
            self._feed(session, start, "SPELL_CAST_SUCCESS",
                       self._spell_args(self.PLAYER, 1297, self.ENEMY, 68168, "11",
                                        "Big Cast"))
            self._feed_real(session, start + timedelta(seconds=1), self.REAL_PET_CAST)
            self._feed(session, start + timedelta(seconds=2), "SPELL_DAMAGE",
                       self._damage_args(self.REAL_PET, 4370, self.ENEMY, 68168,
                                         "Pet Strike", "500"))
            self.assertEqual(session.pet_owners.get(self.REAL_PET), self.REAL_OWNER)
            self.assertEqual(list(session.players), [self.PLAYER])
            self.assertEqual(session.players[self.PLAYER]["damage_done"], 0)
            self.assertIn("player_aggregates_truncated", session.warnings)

    def test_combatant_info_item_level_is_deterministic_and_tolerant(self):
        prefix = [self.PLAYER] + ["0"] * 23 + ["65", "[(90929,112839,1)]",
                                               "(0,354489,1261697,205596)"]
        cases = [
            ("[(1,300,(),(),()),(2,320,(),(),())]", 310),
            ("[]", None),
            ("[(1,0,(),(),()),(2,0,(),(),())]", None),
            ("[(1,300,(),(),()),(2),(3,0,(),(),()),(4,320,(),(),())]", 310),
            ("[(1,300,(),(),()", None),
            (None, None),
        ]
        for equipment, expected in cases:
            with self.subTest(equipment=equipment):
                args = list(prefix)
                if equipment is not None:
                    args.append(equipment)
                args.extend(["[]", "85", "0", "0", "0"])
                parsed = wle.parse_combat_event("COMBATANT_INFO", args)
                self.assertEqual(parsed.spec_id, 65)
                self.assertEqual(parsed.item_level, expected)
                self.assertIsNone(parsed.amount)
                self.assertIsNone(parsed.spell_id)
        # One stat column fewer (an older client) must not move spec or equipment.
        shorter = [self.PLAYER] + ["0"] * 22 + ["65", "[(90929,112839,1)]",
                                                "(0,0,0,0)",
                                                "[(1,300,(),(),()),(2,320,(),(),())]",
                                                "[]", "85", "0", "0", "0"]
        parsed = wle.parse_combat_event("COMBATANT_INFO", shorter)
        self.assertEqual((parsed.spec_id, parsed.item_level), (65, 310))

    def test_combatant_info_with_a_broken_equipment_array_is_still_retained(self):
        session = self._session()
        start = datetime(2026, 8, 30, 20, 0, 0)
        info = [self.PLAYER] + ["0"] * 23 + [
            "65", "[(90929,112839,1)]", "(0,0,0,0)", "[(1,300,(),(),()", "[]", "85"]
        self._feed(session, start, "COMBATANT_INFO", info)
        self.assertEqual(session.players[self.PLAYER]["spec_id"], 65)
        self.assertIsNone(session.players[self.PLAYER]["item_level"])
        self.assertIn("COMBATANT_INFO", self._combat_of(session))

    def test_real_combatant_info_yields_spec_and_a_plausible_item_level(self):
        _, event, args = wle.parse_line(self.REAL_COMBATANT_INFO, 2026)
        parsed = wle.parse_combat_event(event, args)
        self.assertEqual(parsed.spec_id, 1480)
        self.assertEqual(parsed.item_level, 309)
        self.assertIsNone(parsed.spell_id)
        self.assertIsNone(parsed.amount)

    def test_combatant_info_in_a_death_window_serializes_spec_and_item_level(self):
        session = self._session()
        start = datetime(2026, 8, 30, 20, 0, 0)
        info = [self.PLAYER] + ["0"] * 23 + [
            "65", "[(90929,112839,1)]", "(0,0,0,0)",
            "[(1,300,(),(),()),(2,320,(),(),())]", "[]", "85", "0", "0", "0"]
        self._feed(session, start, "COMBATANT_INFO", info)
        self._feed(session, start + timedelta(seconds=1), "SPELL_DAMAGE",
                   self._damage_args(self.ENEMY, 68168, self.PLAYER, 1297,
                                     "Incoming Bolt", "900"))
        death_time = start + timedelta(seconds=2)
        self._feed(session, death_time, "UNIT_DIED",
                   list(self.header("0000000000000000", "nil", 0, self.PLAYER,
                                    "Dst", 1297)))
        self.assertEqual(session.players[self.PLAYER]["spec_id"], 65)
        self.assertEqual(session.players[self.PLAYER]["class_id"], 2)
        self.assertEqual(session.players[self.PLAYER]["item_level"], 310)
        session.close_streams()
        serialized = next(event for event in session.deaths()[0]["events"]
                          if event["event"] == "COMBATANT_INFO")
        self.assertEqual(serialized["spec_id"], 65)
        self.assertEqual(serialized["item_level"], 310)
        self.assertNotIn("spell_id", serialized)
        self.assertNotIn("amount", serialized)

    def test_enemy_cast_successes_are_named_and_deterministically_ordered(self):
        session = self._session()
        start = datetime(2026, 8, 30, 20, 0, 0)
        self._feed(session, start, "SPELL_DAMAGE",
                   self._damage_args(self.ENEMY, 68168, self.PLAYER, 1297,
                                     "Incoming Bolt", "900"))
        casts = [("1238066", "Alpha")] * 3 + [("7", "Beta"), ("9", "Delta"),
                                              ("nil", "Gamma")]
        for index, (spell_id, name) in enumerate(casts):
            self._feed(session, start + timedelta(seconds=index + 1),
                       "SPELL_CAST_SUCCESS",
                       self._spell_args(self.ENEMY, 68168, self.PLAYER, 1297,
                                        spell_id, name))
        summary, _ = session.summary_and_players({})
        self.assertEqual(summary["enemy_cast_successes"], [
            {"spell_id": 1238066, "spell_name": "Alpha", "count": 3},
            {"spell_id": 7, "spell_name": "Beta", "count": 1},
            {"spell_id": 9, "spell_name": "Delta", "count": 1},
            {"spell_id": None, "spell_name": "Gamma", "count": 1},
        ])

    def test_analysis_v2_shapes_are_exact_in_the_files_and_in_the_zip(self):
        _, basename = self._run_analysis_raid(analysis=True, bundle=True)
        analysis_dir = self._analysis_dir(basename)
        players_document = self.read_json(os.path.join(analysis_dir, "players.json"))
        self.assertEqual(list(players_document), ["players"])
        self.assertTrue(all(set(row) >= {"guid", "class_id", "item_level", "spec_id"}
                            for row in players_document["players"]))
        summary = self.read_json(os.path.join(analysis_dir, "summary.json"))
        self.assertEqual(summary["analysis_schema_version"], 2)
        self.assertIsInstance(summary["enemy_cast_successes"], list)
        for row in summary["enemy_cast_successes"]:
            self.assertEqual(sorted(row), ["count", "spell_id", "spell_name"])
        self.assertEqual(summary["interrupts"][0]["interrupted_spell_id"], 9001)
        self.assertEqual(summary["interrupts"][0]["interrupted_spell"], "Dark Bolt")
        self.assertEqual(summary["dispels"][0]["dispelled_spell_id"], 123)
        self.assertNotIn("extra_spell", json.dumps(summary, ensure_ascii=False))
        death = self.read_json(os.path.join(analysis_dir, "deaths.json"))[0]
        self.assertNotIn("extra_spell", json.dumps(death, ensure_ascii=False))
        interrupt = next(event for event in death["events"]
                         if event["event"] == "SPELL_INTERRUPT")
        self.assertEqual(interrupt["interrupted_spell_id"], 9001)
        dispel = next(event for event in death["events"]
                      if event["event"] == "SPELL_DISPEL")
        self.assertEqual(dispel["dispelled_spell"], "Debuff")
        metadata = self.read_json(os.path.join(analysis_dir, "metadata.json"))
        self.assertEqual(metadata["options"]["keep_player_damage"], False)
        archive = os.path.join(self.raids_dir(), basename + "_analysis.zip")
        with zipfile.ZipFile(archive) as bundle:
            self.assertEqual(list(json.loads(bundle.read("players.json"))), ["players"])
            self.assertIsInstance(
                json.loads(bundle.read("summary.json"))["enemy_cast_successes"], list)

    def test_hostile_lookback_neither_resurrects_dropped_lines_nor_double_counts(self):
        session = self._session()
        start = datetime(2026, 8, 30, 20, 0, 0)
        self._feed(session, start, "SPELL_CAST_SUCCESS",
                   self._spell_args(self.ENEMY, 68168, self.OTHER_ENEMY, 68168,
                                    "11", "Silent Cast"))
        energize = self._spell_args(self.ENEMY, 68168, self.ENEMY, 68168,
                                    "1242475", "Soul Immolation", self.ENEMY,
                                    "0000000000000000")
        self._feed(session, start + timedelta(seconds=1), "SPELL_ENERGIZE",
                   energize + self.ADVANCED_TAIL + ["6.0000", "0.0000", "17", "120"])
        self._feed(session, start + timedelta(seconds=2), "SPELL_DAMAGE",
                   self._damage_args(self.ENEMY, 68168, self.PLAYER, 1297,
                                     "Incoming Bolt", "900"))
        self.assertEqual(session.event_counts["SPELL_CAST_SUCCESS"], 1)
        self.assertNotIn("SPELL_ENERGIZE", session.event_counts)
        # Re-running the look-back must be a no-op, not a second count.
        session.hostiles.pop(self.ENEMY)
        session._mark_hostile(self.ENEMY, start + timedelta(seconds=3))
        self.assertEqual(session.event_counts["SPELL_CAST_SUCCESS"], 1)
        combat = self._combat_of(session)
        self.assertIn("Silent Cast", combat)
        self.assertNotIn("Soul Immolation", combat)

    def _published_analysis_dir(self):
        basename = next(name for name in self.list_outputs(self.raids_dir())
                        if os.path.isdir(os.path.join(self.raids_dir(), name)))
        return self._analysis_dir(basename)

    def _published_marker(self):
        return self.read_json(os.path.join(self._published_analysis_dir(),
                                           "metadata.json"))

    def _published_combat(self):
        with open(os.path.join(self._published_analysis_dir(), "combat.txt"),
                  encoding="utf-8") as handle:
            return handle.read()

    def test_switching_keep_player_damage_backfills_once_in_each_direction(self):
        """Flags own the shared artifacts, so switching back must republish them."""
        self.write_log(self._build_raid_with_actor_events().data())
        compact = self.options(analysis_only=True)
        verbose = self.options(analysis_only=True, keep_player_damage=True)
        for options, expected, marker_profile in ((compact, False, compact.profile),
                                                  (verbose, True, verbose.profile),
                                                  (compact, False, compact.profile)):
            extractor = self.make_extractor(options)
            extractor.prepare()
            self.assertEqual(extractor.run_once(), (0, 1, 0), marker_profile)
            self.assertEqual(extractor.run_once(), (0, 0, 0), marker_profile)
            self.assertEqual(self._published_marker()["profile"], marker_profile)
            self.assertEqual("Sinister Strike" in self._published_combat(), expected)
        entry = self.read_json(self.state_path)["files"][LOG_NAME]
        self.assertEqual(list(entry["profiles"]), [compact.profile])

    def test_republish_under_another_profile_leaves_no_stale_marker_on_crash(self):
        self.write_log(self._build_raid_with_actor_events().data())
        compact = self.options(analysis_only=True)
        verbose = self.options(analysis_only=True, keep_player_damage=True)
        first = self.make_extractor(compact)
        first.prepare()
        self.assertEqual(first.run_once(), (0, 1, 0))
        analysis_dir = self._published_analysis_dir()
        real_copy = wle._copy_atomic

        def fail_on_deaths(source, destination):
            if destination.endswith("deaths.json"):
                raise OSError("simulated crash: deaths.json copy")
            return real_copy(source, destination)

        crashing = self.make_extractor(verbose)
        crashing.prepare()
        with mock.patch.object(wle, "_copy_atomic", side_effect=fail_on_deaths):
            self.assertEqual(crashing.run_once(), (0, 0, 1))
        # The old marker described the compact package that is now half replaced.
        self.assertFalse(os.path.exists(os.path.join(analysis_dir, "metadata.json")),
                         self.list_outputs(analysis_dir))
        retry = self.make_extractor(verbose)
        retry.prepare()
        self.assertEqual(retry.run_once(), (0, 1, 0))
        self.assertEqual(len([name for name in self.list_outputs(self.raids_dir())
                              if os.path.isdir(os.path.join(self.raids_dir(), name))]), 1)
        self.assertEqual(self._published_analysis_dir(), analysis_dir)
        self.assertEqual(sorted(self.list_outputs(analysis_dir)),
                         ["combat.txt", "deaths.json", "metadata.json", "players.json",
                          "summary.json"])
        self.assertEqual(self._published_marker()["profile"], verbose.profile)
        self.assertIn("Sinister Strike", self._published_combat())

    def test_previous_profile_repairs_a_package_left_broken_by_another_profiles_crash(self):
        # A publishes, B crashes half-way through replacing A's package, then the user
        # goes back to A. A's old EOF offset must not suppress the repair.
        self.write_log(self._build_raid_with_actor_events().data())
        compact = self.options(analysis_only=True)
        verbose = self.options(analysis_only=True, keep_player_damage=True)
        first = self.make_extractor(compact)
        first.prepare()
        self.assertEqual(first.run_once(), (0, 1, 0))
        analysis_dir = self._published_analysis_dir()
        real_copy = wle._copy_atomic

        def fail_on_deaths(source, destination):
            if destination.endswith("deaths.json"):
                raise OSError("simulated crash: deaths.json copy")
            return real_copy(source, destination)

        crashing = self.make_extractor(verbose)
        crashing.prepare()
        with mock.patch.object(wle, "_copy_atomic", side_effect=fail_on_deaths):
            self.assertEqual(crashing.run_once(), (0, 0, 1))
        self.assertFalse(os.path.exists(os.path.join(analysis_dir, "metadata.json")))
        back = self.make_extractor(compact)
        back.prepare()
        self.assertEqual(back.run_once(), (0, 1, 0))
        self.assertEqual(self._published_analysis_dir(), analysis_dir)
        self.assertEqual(sorted(self.list_outputs(analysis_dir)),
                         ["combat.txt", "deaths.json", "metadata.json", "players.json",
                          "summary.json"])
        self.assertEqual(self._published_marker()["profile"], compact.profile)
        self.assertNotIn("Sinister Strike", self._published_combat())
        again = self.make_extractor(compact)
        again.prepare()
        self.assertEqual(again.run_once(), (0, 0, 0))
        state = self.read_json(self.state_path)
        self.assertEqual(list(state["files"][LOG_NAME]["profiles"]), [compact.profile])

    def test_gzip_republish_removes_the_other_containers_combat_body(self):
        self.write_log(self._build_raid_with_actor_events().data())
        plain = self.make_extractor(self.options(analysis_only=True))
        plain.prepare()
        self.assertEqual(plain.run_once(), (0, 1, 0))
        compressed = self.make_extractor(self.options(analysis_only=True, gzip=True))
        compressed.prepare()
        self.assertEqual(compressed.run_once(), (0, 1, 0))
        self.assertEqual(sorted(self.list_outputs(self._published_analysis_dir())),
                         ["combat.txt.gz", "deaths.json", "metadata.json",
                          "players.json", "summary.json"])

    def test_owned_pet_self_heal_is_kept_and_credited_to_its_owner(self):
        session = self._session()
        start = datetime(2026, 8, 30, 20, 0, 0)
        self._feed(session, start, "SPELL_SUMMON", self._summon_args(self.PET))
        self._feed(session, start + timedelta(seconds=1), "SPELL_HEAL",
                   self._spell_args(self.PET, 4370, self.PET, 4370, "13", "Pet Mend",
                                    "300", "100", "0", "nil"))
        owner = session.players[self.PLAYER]
        self.assertEqual(owner["healing_done"], 200)
        self.assertEqual(owner["healing_received"], 200)
        self.assertEqual(owner["self_healing"], 200)
        self.assertEqual(session.event_counts["SPELL_HEAL"], 1)
        self.assertIn("Pet Mend", self._combat_of(session))

    def test_unparseable_line_is_written_and_counted(self):
        start = datetime(2026, 8, 30, 22, 0, 0)
        builder = LogBuilder()
        builder.add(start, "ENCOUNTER_START", "9300", q("Boss"), "16", "20", "2900")
        self.add_event(builder, start + timedelta(seconds=1), "SPELL_DAMAGE",
                       self.ENEMY, "Boss", 68168, self.PLAYER, "Player", 1297,
                       "9001", q("Dark Bolt"), "32", "100", "0", "32", "0", "0", "0",
                       "0")
        builder.add_raw(b"no timestamp and no event marker\r\n")
        builder.add(start + timedelta(seconds=3), "ENCOUNTER_END", "9300", q("Boss"),
                    "16", "20", "1", "3000")
        self.write_log(builder.data())
        extractor = self.make_extractor(self.options(analysis_only=True))
        extractor.prepare()
        self.assertEqual(extractor.run_once(), (0, 1, 0))
        self.assertIn("no timestamp and no event marker", self._published_combat())
        summary = self.read_json(os.path.join(self._published_analysis_dir(),
                                              "summary.json"))
        self.assertEqual(summary["event_counts"]["UNPARSEABLE"], 1)
        self.assertEqual(summary["parse_fallbacks"]["UNPARSEABLE"], 1)

    def test_help_lists_all_analysis_modes(self):
        help_text = wle.build_parser().format_help()
        for flag in ("--analysis", "--analysis-only", "--gzip", "--bundle", "--watch",
                     "--keep-player-damage"):
            self.assertIn(flag, help_text)

    def test_output_options_reject_invalid_analysis_combinations(self):
        with self.assertRaises(ValueError):
            wle.OutputOptions(analysis=True, analysis_only=True)
        with self.assertRaises(ValueError):
            wle.OutputOptions(bundle=True)
        with self.assertRaises(ValueError) as raised:
            wle.OutputOptions(keep_player_damage=True)
        self.assertIn("--keep-player-damage", str(raised.exception))

    def test_cli_rejects_invalid_analysis_combinations_before_path_resolution(self):
        with self.assertRaises(SystemExit):
            wle.run(["--analysis", "--analysis-only"])
        with self.assertRaises(SystemExit):
            wle.run(["--bundle"])
        with self.assertRaises(SystemExit):
            wle.run(["--keep-player-damage"])


# --- raid performance diagnostics: parsing primitives ------------------------------

REAL_TS = "10/1/2026 21:38:02.9501  "
DKYAM = "Player-1378-0B46B91A"
REAL_SPELL_DAMAGE = (
    'SPELL_DAMAGE,Player-1378-0B46B91A,"Dkyam-DunModr-EU",0x512,0x80000000,'
    'Creature-0-3109-3004-27445-261477-00013EBFBB,"Gigante colmillo de veneno",0xa48,'
    '0x80000000,44425,"Tromba Arcana",0x40,Creature-0-3109-3004-27445-261477-00013EBFBB,'
    '0000000000000000,7416650,16138470,0,0,1470,0,0,0,1,0,0,0,579.52,-2.48,2607,4.0936,'
    '92,40958,40957,-1,64,0,0,0,nil,nil,nil,ST')
REAL_SPELL_ENERGIZE = (
    'SPELL_ENERGIZE,Player-1378-0B46B91A,"Dkyam-DunModr-EU",0x512,0x80000000,'
    'Player-1378-0B46B91A,"Dkyam-DunModr-EU",0x512,0x80000000,321507,"Toque de los magi",'
    '0x40,Player-1378-0B46B91A,0000000000000000,758480,758480,425,3331,649,1155,0,0,0,'
    '349978,349978,0,557.48,2.32,2607,0.0571,320,4.0000,0.0000,16,4')
REAL_CAST_SUCCESS = (
    'SPELL_CAST_SUCCESS,Player-1378-0B46B91A,"Dkyam-DunModr-EU",0x512,0x80000000,'
    'Creature-0-3109-3004-27445-261477-00013EBFBB,"Gigante colmillo de veneno",0xa48,'
    '0x80000000,321507,"Toque de los magi",0x40,Player-1378-0B46B91A,0000000000000000,'
    '758480,758480,425,3331,649,1155,0,0,0,349978,349978,12500,557.48,2.32,2607,0.0571,320')
REAL_APPLIED_DOSE = (
    'SPELL_AURA_APPLIED_DOSE,Player-1378-0B46B91A,"Dkyam-DunModr-EU",0x512,0x80000000,'
    'Player-1378-0B46B91A,"Dkyam-DunModr-EU",0x512,0x80000000,263725,"Lanzamiento libre",'
    '0x1,BUFF,2')
REAL_REMOVED_DOSE = (
    'SPELL_AURA_REMOVED_DOSE,Player-1378-0B46B91A,"Dkyam-DunModr-EU",0x512,0x80000000,'
    'Player-1378-0B46B91A,"Dkyam-DunModr-EU",0x512,0x80000000,263725,"Lanzamiento libre",'
    '0x1,BUFF,1')
# Same target block as REAL_SPELL_DAMAGE with the tails of other real lines: a
# killing blow (HP 0, overkill 87283), a critical hit, and a partially absorbed
# modern melee swing (block = attacker, no ST/AOE marker).
OVERKILL_SPELL_DAMAGE = REAL_SPELL_DAMAGE.replace(
    "0000000000000000,7416650,16138470", "0000000000000000,0,16138470").replace(
    "40958,40957,-1,64,0,0,0,nil,nil,nil,ST", "116194,116194,87283,64,0,0,0,nil,nil,nil,ST")
CRIT_SPELL_DAMAGE = REAL_SPELL_DAMAGE.replace(
    "40958,40957,-1,64,0,0,0,nil,nil,nil,ST", "81916,40957,-1,64,0,0,0,1,nil,nil,ST")
ABSORBED_SWING = (
    'SWING_DAMAGE,Creature-0-3109-3004-27445-261477-00013EBFBB,"Gigante colmillo de veneno",'
    '0xa48,0x80000000,Player-1378-0B46B91A,"Dkyam-DunModr-EU",0x512,0x80000000,'
    'Creature-0-3109-3004-27445-261477-00013EBFBB,0000000000000000,7416650,16138470,0,0,'
    '1470,0,0,0,1,0,0,0,579.52,-2.48,2607,4.0936,92,76467,77715,-1,1,0,0,1248,nil,nil,nil')
# A real COMBATANT_INFO line (2.9 KB): 24 spec, 25 talents, 26 pvp tuple,
# 27 equipment, 28 pre-pull auras as flat (caster, spell, stacks) triples.
REAL_COMBATANT_INFO = (
    "COMBATANT_INFO,Player-1378-0B46B91A,1,273,422,39923,3386,0,0,0,0,602,602,602,0,227,"
    "1362,1362,1362,0,608,462,462,462,649,62,[(62085,80141,1),(62086,80142,1),"
    "(62091,80147,1),(62096,80153,1),(62098,80155,1),(62100,80157,1),(62102,80159,2),"
    "(62104,80161,1),(62114,80173,2),(62115,80174,1),(62122,80181,1),(62123,80182,2),"
    "(62124,80183,1),(62127,80187,1),(93524,115877,1),(94643,117246,1),(94644,117247,1),"
    "(94645,117248,1),(94646,117249,1),(94648,117251,1),(94649,117252,1),(94650,117253,1),"
    "(94651,117254,1),(94652,117255,1),(94653,117256,1),(99830,123341,1),(62094,126060,1),"
    "(102445,126515,1),(102446,126516,1),(102449,126519,1),(102451,126521,1),"
    "(102467,126537,1),(102468,126538,1),(102470,126540,1),(102471,126541,1),"
    "(102472,126542,1),(102474,126544,1),(102475,126545,1),(102480,126550,2),"
    "(104113,128689,1),(102465,134020,1),(108535,134023,2),(108536,134024,1),"
    "(102460,134025,1),(108537,134026,2),(108538,134027,1),(108539,134028,1),"
    "(108541,134030,1),(108654,134183,1),(108657,134187,1),(108658,134188,1),"
    "(108660,134190,1),(108661,134191,1),(108662,134192,2),(108664,134194,1),"
    "(108665,134196,1),(109002,134834,1),(109478,135698,1),(109673,135924,1),"
    "(109674,135925,1),(109675,135926,1),(110081,136579,1),(110420,137026,1),"
    "(110420,137027,2),(110420,137028,1),(110442,137084,1),(110597,137410,1),"
    "(110849,137841,1),(110850,137842,1),(62084,80140,1),(62105,134197,1),(62116,80175,1),"
    "(101883,125818,1),(108659,134189,1),(62121,80180,1),(94647,117250,1)],(0,0,0,0),"
    "[(271564,318,(7959,0,0),(13334,6652,13696,13692,13698,12845),()),"
    "(251173,311,(),(13440,6652,13668,12699,12843),(240863,278)),"
    "(271562,321,(8031,0,0),(6652,13333,13694,13697,13696,12846),()),(0,0,(),(),()),"
    "(272231,321,(7956,0,0),(6652,13662,12846),()),"
    "(193691,334,(),(13440,6652,13696,13662,12699,12854),()),"
    "(271563,321,(7937,0,0),(12846,13334,6652,13693,13698,1574),()),"
    "(268218,318,(7993,0,0),(6652,13662,13334,12849),()),"
    "(239648,331,(),(12214,13667,12497,13751,14001,8960,12384,8792,13836,13696),()),"
    "(271565,308,(),(13333,13691,6652,13697,12838),()),"
    "(251148,311,(7967,0,0),(12843,13440,6652,13668,12699),(240892,295)),"
    "(268266,321,(7967,0,0),(6652,13668,13334,12846),(240967,295)),"
    "(250215,321,(),(13440,6652,12699,12846),()),(250214,321,(),(13440,6652,12699,12846),()),"
    "(193763,308,(),(12842,13440,6652,13662,12699),()),"
    "(159636,334,(8689,8052,0),(13440,6652,12701,12854),()),(0,0,(),(),()),"
    "(168100,2,(),(),())],[Player-1378-0B46B91A,1296934,1,Player-1378-0B46B91A,384612,1,"
    "Player-1378-0B46B91A,1244329,1,Player-1378-0B46B91A,384651,1,Player-1378-0B46B91A,"
    "384452,1,Player-1378-0B46B91A,1309497,1,Player-1378-0B46B91A,384858,1,"
    "Player-1378-0A3532D6,166646,1,Player-1378-049F8E1D,465,1,Player-1378-0B46B91A,1459,1,"
    "Player-1378-08699F44,6673,1,Player-1378-0B26B0FA,1126,1,Player-1379-0B47CD72,21562,1,"
    "Player-1378-015243CC,465,1,Player-1378-0B46B91A,1235110,1,Player-1379-06AFA74E,"
    "462854,1,Player-1378-0B46B91A,1285644,1],1,0,0,0")


def real_args(text: str) -> tuple[str, list[str]]:
    _, event, args = wle.parse_line(REAL_TS + text, 2026)
    return event, args


def damage_of(text: str) -> tuple[dict, "wle.ParsedCombatEvent"]:
    """Run _damage_suffix exactly where parse_combat_event reads the damage tail."""
    event, args = real_args(text)
    payload = args[8:]
    value_index = 0 if event.startswith("SWING_") else 3
    advanced = wle._advanced_state(payload, value_index)
    if advanced is not None:
        value_index = advanced[0]
    return wle._damage_suffix(event, payload, value_index), \
        wle.parse_combat_event(event, args)


class PerformanceParsingTests(unittest.TestCase):

    def test_log_header_real_line(self):
        _, args = real_args("COMBAT_LOG_VERSION,22,ADVANCED_LOG_ENABLED,1,BUILD_VERSION,"
                            "12.1.0,PROJECT_ID,1")
        self.assertEqual(wle.parse_log_header(args),
                         {"combat_log_version": 22, "advanced_logging": True,
                          "build_version": "12.1.0", "project_id": 1})

    def test_log_header_tolerates_missing_and_odd_fields(self):
        self.assertEqual(wle.parse_log_header([]),
                         {"combat_log_version": None, "advanced_logging": None,
                          "build_version": None, "project_id": None})
        header = wle.parse_log_header(["x", "ADVANCED_LOG_ENABLED", "0", "NEW_KEY", "7",
                                       "BUILD_VERSION"])
        self.assertEqual(header, {"combat_log_version": None, "advanced_logging": False,
                                  "build_version": None, "project_id": None})
        self.assertIsNone(wle.parse_log_header(["22", "PROJECT_ID", "one"])["project_id"])

    def test_damage_suffix_real_spell_damage(self):
        suffix, parsed = damage_of(REAL_SPELL_DAMAGE)
        self.assertEqual(suffix, {"amount": 40958, "overkill": -1, "absorbed": 0,
                                  "critical": False})
        self.assertEqual((parsed.amount, parsed.absorbed), (40958, 0))

    def test_damage_suffix_overkill_is_included_in_amount(self):
        suffix, parsed = damage_of(OVERKILL_SPELL_DAMAGE)
        self.assertEqual(suffix, {"amount": 116194, "overkill": 87283, "absorbed": 0,
                                  "critical": False})
        self.assertEqual(parsed.target_hp, 0)

    def test_damage_suffix_critical(self):
        suffix, _ = damage_of(CRIT_SPELL_DAMAGE)
        self.assertEqual(suffix["critical"], True)
        self.assertEqual(suffix["amount"], 81916)

    def test_damage_suffix_partially_absorbed_modern_swing(self):
        suffix, parsed = damage_of(ABSORBED_SWING)
        self.assertEqual(suffix, {"amount": 76467, "overkill": -1, "absorbed": 1248,
                                  "critical": False})
        # Same detection as the parser: both read the same absorbed field.
        self.assertEqual((parsed.amount, parsed.absorbed), (76467, 1248))

    def test_damage_suffix_legacy_layout(self):
        # amount, overkill, school, resisted, blocked, absorbed, critical, ...
        payload = ["1752", '"Sinister Strike"', "1", "700", "5", "1", "0", "0", "40",
                   "1", "nil", "nil"]
        self.assertEqual(wle._damage_suffix("SPELL_DAMAGE", payload, 3),
                         {"amount": 700, "overkill": 5, "absorbed": 40, "critical": True})

    def test_damage_suffix_malformed_fields_are_none(self):
        event, args = real_args(REAL_SPELL_DAMAGE.replace(
            "40958,40957,-1,64,0,0,0,nil,nil,nil,ST", "40958,40957,x,64,0,0,?,maybe,nil,nil,ST"))
        payload = args[8:]
        suffix = wle._damage_suffix(event, payload, 22)
        self.assertEqual(suffix, {"amount": 40958, "overkill": None, "absorbed": None,
                                  "critical": None})
        truncated = wle._damage_suffix("SPELL_DAMAGE", ["1", '"x"', "1", "500"], 3)
        self.assertEqual(truncated, {"amount": 500, "overkill": None, "absorbed": None,
                                     "critical": None})

    def test_power_state_caster_block(self):
        _, args = real_args(REAL_CAST_SUCCESS)
        self.assertEqual(wle._power_state(args[8:], 3),
                         (DKYAM, 0, 349978, 349978, 12500))
        _, args = real_args(REAL_SPELL_ENERGIZE)
        self.assertEqual(wle._power_state(args[8:], 3), (DKYAM, 0, 349978, 349978, 0))

    def test_power_state_rejects_unrecognised_block_and_pipe_power_type(self):
        _, args = real_args(REAL_CAST_SUCCESS)
        payload = args[8:]
        piped = list(payload)
        piped[3 + 10] = "0|3"
        self.assertIsNone(wle._power_state(piped, 3))
        self.assertIsNone(wle._power_state(payload[:10], 3))
        no_guid = list(payload)
        no_guid[3] = "12345"
        self.assertIsNone(wle._power_state(no_guid, 3))

    def test_energize_suffix_real_line(self):
        _, args = real_args(REAL_SPELL_ENERGIZE)
        self.assertEqual(wle._energize_suffix(args[8:]),
                         {"amount": 4, "over_energize": 0, "power_type": 16,
                          "max_power": 4})

    def test_energize_suffix_without_advanced_block_and_malformed(self):
        self.assertEqual(wle._energize_suffix(["1", '"x"', "0x1", "2.5000", "0.5000",
                                               "0", "100"]),
                         {"amount": 2.5, "over_energize": 0.5, "power_type": 0,
                          "max_power": 100})
        _, args = real_args(REAL_SPELL_ENERGIZE)
        self.assertIsNone(wle._energize_suffix(args[8:-1]))
        broken = list(args[8:])
        broken[-2] = "16|0"
        self.assertIsNone(wle._energize_suffix(broken))

    def test_aura_stacks_real_dose_lines(self):
        _, args = real_args(REAL_APPLIED_DOSE)
        self.assertEqual(wle._aura_stacks(args[8:]), 2)
        _, args = real_args(REAL_REMOVED_DOSE)
        self.assertEqual(wle._aura_stacks(args[8:]), 1)
        self.assertIsNone(wle._aura_stacks(args[8:-1]))
        self.assertIsNone(wle._aura_stacks(args[8:-1] + ["two"]))

    def test_combatant_details_real_line(self):
        event, args = real_args(REAL_COMBATANT_INFO)
        details = wle.parse_combatant_details(args)
        talents_text = REAL_COMBATANT_INFO[REAL_COMBATANT_INFO.index("[(62085"):
                                           REAL_COMBATANT_INFO.index(",(0,0,0,0)")]
        equipment_text = REAL_COMBATANT_INFO[REAL_COMBATANT_INFO.index("[(271564"):
                                             REAL_COMBATANT_INFO.index(",[Player-")]
        self.assertEqual(details["guid"], DKYAM)
        self.assertEqual(details["spec_id"], 62)
        self.assertEqual(details["talents"],
                         {"status": "ok", "count": 76,
                          "fingerprint": wle._sha1(talents_text.encode())[:12]})
        equipment = details["equipment"]
        self.assertEqual(equipment["status"], "ok")
        self.assertEqual(equipment["fingerprint"], wle._sha1(equipment_text.encode())[:12])
        self.assertEqual(len(equipment["items"]), 18)
        self.assertEqual(equipment["items"][:4],
                         [[271564, 318], [251173, 311], [271562, 321], [0, 0]])
        self.assertEqual(equipment["items"][-1], [168100, 2])
        auras = details["initial_auras"]
        self.assertEqual(auras["status"], "ok")
        self.assertEqual(len(auras["auras"]), 17)
        self.assertEqual(auras["auras"][0], (DKYAM, 1296934, 1))
        self.assertEqual(auras["auras"][7], ("Player-1378-0A3532D6", 166646, 1))
        # The existing parser keeps its own reading of the same line.
        parsed = wle.parse_combat_event(event, args)
        self.assertEqual((parsed.spec_id, parsed.item_level), (62, 300))

    def test_combatant_details_malformed_block_only_affects_that_block(self):
        bad_talents = REAL_COMBATANT_INFO.replace("(62085,80141,1)", "(62085,80141)")
        details = wle.parse_combatant_details(real_args(bad_talents)[1])
        self.assertEqual(details["talents"], {"status": "unsupported_layout",
                                              "fingerprint": None, "count": None})
        self.assertEqual(details["equipment"]["status"], "ok")
        self.assertEqual(details["initial_auras"]["status"], "ok")

        bad_item = REAL_COMBATANT_INFO.replace("(271564,318,", "(271564,ilvl,")
        details = wle.parse_combatant_details(real_args(bad_item)[1])
        self.assertEqual(details["equipment"], {"status": "unsupported_layout",
                                                "fingerprint": None, "items": None})
        self.assertEqual(details["talents"]["count"], 76)
        self.assertEqual(details["initial_auras"]["status"], "ok")

        bad_auras = REAL_COMBATANT_INFO.replace("Player-1378-0B46B91A,1285644,1]",
                                                "Player-1378-0B46B91A,1285644]")
        details = wle.parse_combatant_details(real_args(bad_auras)[1])
        self.assertEqual(details["initial_auras"], {"status": "unsupported_layout",
                                                    "auras": None})
        self.assertEqual(details["equipment"]["status"], "ok")

    def test_combatant_details_absent_blocks(self):
        _, args = real_args(REAL_COMBATANT_INFO)
        details = wle.parse_combatant_details(args[:25])
        self.assertEqual(details["guid"], DKYAM)
        self.assertIsNone(details["spec_id"])
        self.assertEqual(details["talents"]["status"], "absent")
        self.assertEqual(details["equipment"]["status"], "absent")
        self.assertEqual(details["initial_auras"]["status"], "absent")
        # Equipment is the last argument: the aura block is absent, the rest ok.
        details = wle.parse_combatant_details(args[:28])
        self.assertEqual(details["equipment"]["status"], "ok")
        self.assertEqual(details["initial_auras"], {"status": "absent", "auras": None})
        empty = wle.parse_combatant_details(args[:27] + ["[]", "[]"])
        self.assertEqual(empty["talents"]["status"], "ok")
        self.assertEqual(empty["equipment"]["status"], "absent")


# --- raid performance diagnostics: options, CLI and game header ---------------------

class PerformanceOptionsTests(ExtractorTestCase):

    BOSS = "Ithraz"

    def perf(self, player="Dkyam", **overrides):
        values = {"analysis_only": True, "performance_player": player}
        values.update(overrides)
        return wle.OutputOptions(**values)

    def test_profile_and_as_dict_unchanged_without_the_flag(self):
        self.assertEqual(wle.OutputOptions().profile, "full")
        self.assertEqual(wle.OutputOptions(analysis=True, gzip=True).profile,
                         "full+analysis+gzip")
        options = wle.OutputOptions(analysis_only=True, bundle=True,
                                    keep_player_damage=True)
        self.assertEqual(options.profile, "analysis-only+bundle+keep-player-damage")
        self.assertEqual(options.as_dict(),
                         {"full": False, "analysis": True, "gzip": False, "bundle": True,
                          "keep_player_damage": True,
                          "profile": "analysis-only+bundle+keep-player-damage"})
        self.assertIsNone(options.performance_fingerprint)

    def test_performance_profile_suffix_and_as_dict(self):
        options = self.perf()
        fingerprint = options.performance_fingerprint
        self.assertRegex(fingerprint, r"^[0-9a-f]{12}$")
        self.assertEqual(options.profile, "analysis-only+perf-" + fingerprint)
        self.assertEqual(options.as_dict()["performance"],
                         {"player": "Dkyam", "fingerprint": fingerprint})
        # The selector is normalised: case and surrounding blanks do not matter.
        self.assertEqual(self.perf("  dKYAM ").profile, options.profile)
        self.assertNotEqual(self.perf("Other").profile, options.profile)
        self.assertNotEqual(self.perf(DKYAM).profile, options.profile)

    def test_fingerprint_follows_rule_versions(self):
        base = self.perf().profile
        with mock.patch.object(wle, "PERFORMANCE_RULES_VERSION",
                               wle.PERFORMANCE_RULES_VERSION + 1):
            self.assertNotEqual(self.perf().profile, base)
        with mock.patch.object(wle, "PERFORMANCE_SCHEMA_VERSION",
                               wle.PERFORMANCE_SCHEMA_VERSION + 1):
            self.assertNotEqual(self.perf().profile, base)
        with mock.patch.dict(wle.SPEC_RULES, {62: {"id": "arcane", "version": 1}}):
            registered = self.perf().profile
            self.assertNotEqual(registered, base)
        with mock.patch.dict(wle.SPEC_RULES, {62: {"id": "arcane", "version": 2}}):
            self.assertNotEqual(self.perf().profile, registered)
        self.assertEqual(self.perf().profile, base)

    def test_performance_player_requires_analysis_and_a_selector(self):
        with self.assertRaises(ValueError) as raised:
            wle.OutputOptions(performance_player="Dkyam")
        self.assertIn("--performance-player requires", str(raised.exception))
        with self.assertRaises(ValueError):
            wle.OutputOptions(gzip=True, performance_player="Dkyam")
        with self.assertRaises(ValueError):
            wle.OutputOptions(analysis=True, performance_player="   ")

    def test_cli_flags(self):
        help_text = wle.build_parser().format_help()
        for flag in ("--performance-player", "--packet-max-bytes", "--session-gap-minutes"):
            self.assertIn(flag, help_text)
        defaults = wle.build_parser().parse_args([])
        self.assertEqual((defaults.performance_player, defaults.packet_max_bytes,
                          defaults.session_gap_minutes), (None, 200000, 120))
        for argv in (["--performance-player", "Dkyam"],
                     ["--analysis-only", "--packet-max-bytes", "0"],
                     ["--analysis-only", "--session-gap-minutes", "-5"],
                     ["--analysis-only", "--session-gap-minutes", "two"]):
            with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                wle.run(argv)
        with mock.patch.object(wle.argparse.ArgumentParser, "error",
                               side_effect=SystemExit) as error:
            with self.assertRaises(SystemExit):
                wle.run(["--performance-player", "Dkyam"])
        self.assertIn("--analysis or --analysis-only", error.call_args.args[0])

    def test_extractor_keeps_packet_settings_outside_the_profile(self):
        extractor = wle.Extractor(self.log_dir, self.output_dir, verbose=False,
                                  output_options=self.perf(), packet_max_bytes=1234,
                                  session_gap_minutes=45)
        self.assertEqual((extractor.packet_max_bytes, extractor.session_gap_minutes),
                         (1234, 45))
        self.assertEqual(extractor.state.profile, self.perf().profile)
        default = self.make_extractor()
        self.assertEqual((default.packet_max_bytes, default.session_gap_minutes),
                         (200000, 120))

    # -- game header -------------------------------------------------------------
    def add_header(self, builder, timestamp, build="12.1.0"):
        builder.add(timestamp, "COMBAT_LOG_VERSION", "22", "ADVANCED_LOG_ENABLED", "1",
                    "BUILD_VERSION", build, "PROJECT_ID", "1")

    def add_pull(self, builder, start, encounter="3421"):
        builder.add(start, "ENCOUNTER_START", encounter, q(self.BOSS), "15", "20", "2900")
        builder.add(start + timedelta(seconds=5), "SPELL_CAST_SUCCESS", DKYAM,
                    q("Dkyam-DunModr-EU"), "0x512", "0x80000000", "0000000000000000",
                    "nil", "0x80000000", "0x80000000", "5143", q("Misiles Arcanos"), "0x40")
        builder.add(start + timedelta(seconds=30), "ENCOUNTER_END", encounter,
                    q(self.BOSS), "15", "20", "0", "30000")

    def run_capturing(self, extractor):
        """run_once, returning the segments handed to the publisher."""
        with mock.patch.object(wle.SegmentPublisher, "publish", autospec=True,
                               side_effect=wle.SegmentPublisher.publish) as publish:
            result = extractor.run_once()
        return result, [call.args[1] for call in publish.call_args_list]

    def profile_entry(self, profile):
        state = self.read_json(self.state_path)
        return state["files"][LOG_NAME]["profiles"][profile]

    def test_header_reaches_segment_beyond_the_pre_context(self):
        start = datetime(2026, 10, 1, 21, 28, 57)
        builder = LogBuilder()
        self.add_header(builder, start)
        for second in range(1, 60, 5):
            builder.add(start + timedelta(seconds=second), "SPELL_CAST_SUCCESS", DKYAM,
                        q("Dkyam-DunModr-EU"), "0x512", "0x80000000")
        self.add_pull(builder, start + timedelta(seconds=60))
        self.write_log(builder.data())
        extractor = self.make_extractor(self.perf())
        extractor.prepare()
        result, segments = self.run_capturing(extractor)
        self.assertEqual(result, (0, 1, 0))
        self.assertEqual(segments[0].game_context,
                         {"combat_log_version": 22, "advanced_logging": True,
                          "build_version": "12.1.0", "project_id": 1,
                          "header_source": "stream"})

    def _resume_fixture(self):
        start = datetime(2026, 10, 1, 21, 28, 57)
        builder = LogBuilder()
        # The first line differs from the header in force: only the stored copy
        # can give the second pull "12.1.0".
        self.add_header(builder, start, build="12.0.7")
        self.add_header(builder, start + timedelta(seconds=10))
        self.add_pull(builder, start + timedelta(seconds=60))
        self.write_log(builder.data())
        first = self.make_extractor(self.perf())
        first.prepare()
        self.assertEqual(first.run_once(), (0, 1, 0))
        appended = LogBuilder()
        self.add_pull(appended, start + timedelta(seconds=600))
        self.append_log(appended.data())
        return start

    def test_resume_takes_header_from_state(self):
        # A tiny warm-up keeps the header lines out of the re-read window, as on a
        # real log where the header is far more than 512 KiB before the offset.
        with mock.patch.object(wle, "WARMUP_BYTES", 64):
            self._resume_fixture()
            entry = self.profile_entry(self.perf().profile)
            self.assertEqual(entry["log_header"]["build_version"], "12.1.0")
            second = self.make_extractor(self.perf())
            second.prepare()
            result, segments = self.run_capturing(second)
        self.assertEqual(result, (0, 1, 0))
        self.assertEqual(segments[0].game_context["header_source"], "state")
        self.assertEqual(segments[0].game_context["build_version"], "12.1.0")

    def test_resume_without_stored_header_reads_the_file_start(self):
        with mock.patch.object(wle, "WARMUP_BYTES", 64):
            self._resume_fixture()
            state = self.read_json(self.state_path)
            del state["files"][LOG_NAME]["profiles"][self.perf().profile]["log_header"]
            with open(self.state_path, "w", encoding="utf-8") as handle:
                json.dump(state, handle)
            second = self.make_extractor(self.perf())
            second.prepare()
            result, segments = self.run_capturing(second)
        self.assertEqual(result, (0, 1, 0))
        self.assertEqual(segments[0].game_context["header_source"], "file_start")
        self.assertEqual(segments[0].game_context["build_version"], "12.0.7")

    def test_log_without_header_is_unknown_also_on_resume(self):
        start = datetime(2026, 10, 1, 21, 28, 57)
        builder = LogBuilder()
        self.add_pull(builder, start)
        self.write_log(builder.data())
        first = self.make_extractor(self.perf())
        first.prepare()
        result, segments = self.run_capturing(first)
        self.assertEqual(result, (0, 1, 0))
        self.assertEqual(segments[0].game_context,
                         {"combat_log_version": None, "advanced_logging": None,
                          "build_version": None, "project_id": None,
                          "header_source": "unknown"})
        self.assertNotIn("log_header", self.profile_entry(self.perf().profile))
        appended = LogBuilder()
        self.add_pull(appended, start + timedelta(seconds=600))
        self.append_log(appended.data())
        second = self.make_extractor(self.perf())
        second.prepare()
        result, segments = self.run_capturing(second)
        self.assertEqual(result, (0, 1, 0))
        self.assertEqual(segments[0].game_context["header_source"], "unknown")

    def test_state_without_the_flag_has_no_header(self):
        start = datetime(2026, 10, 1, 21, 28, 57)
        builder = LogBuilder()
        self.add_header(builder, start)
        self.add_pull(builder, start + timedelta(seconds=60))
        self.write_log(builder.data())
        legacy_keys = {"offset", "size", "mtime", "head_hash", "tail_hash"}
        for options, profile in ((None, "full"),
                                 (wle.OutputOptions(analysis_only=True), "analysis-only")):
            extractor = self.make_extractor(options)
            extractor.prepare()
            self.assertEqual(extractor.run_once()[2], 0)
            state = self.read_json(self.state_path)
            self.assertEqual(set(state), {"version", "files"})
            entry = state["files"][LOG_NAME]
            self.assertEqual(set(entry["profiles"]), {profile})
            self.assertEqual(set(entry["profiles"][profile]), legacy_keys)
            expected_top = {"profiles"} | (legacy_keys if profile == "full" else set())
            self.assertEqual(set(entry), expected_top)
        extractor = self.make_extractor(self.perf())
        extractor.prepare()
        extractor.run_once()
        entry = self.profile_entry(self.perf().profile)
        self.assertEqual(set(entry), legacy_keys | {"log_header"})
        self.assertEqual(entry["log_header"],
                         {"combat_log_version": 22, "advanced_logging": True,
                          "build_version": "12.1.0", "project_id": 1})

    def test_watch_stores_the_header(self):
        start = datetime(2026, 10, 1, 21, 28, 57)
        builder = LogBuilder()
        self.add_header(builder, start)
        self.add_pull(builder, start + timedelta(seconds=60))
        self.write_log(builder.data())
        extractor = self.make_extractor(self.perf())
        extractor.prepare()
        with mock.patch("sys.stdout"):
            extractor.watch(interval=0, max_polls=1)
        self.assertEqual(self.profile_entry(self.perf().profile)["log_header"]
                         ["build_version"], "12.1.0")


# --- raid performance diagnostics: accumulator ---------------------------------------

PERF_NAME = "Dkyam-DunModr-EU"
PERF_HEALER = "Player-1378-00000002"
PERF_HEALER_NAME = "Sanadora-DunModr-EU"
PERF_BOSS = "Creature-0-3109-3004-27445-261477-00013EBFBB"
PERF_BOSS_NAME = "Ithraz"
PERF_ENCOUNTER_NAME = "Los colmillos gemelos"
PERF_FRIEND = "Creature-0-3109-3004-27445-111111-0000000001"
PERF_PET = "Creature-0-3109-3004-27445-99999-00000000AA"
PERF_HOSTILE, PERF_FRIENDLY, PERF_PLAYER_FLAGS = "0xa48", "0xa18", "0x512"
PERF_AURA_LIST_START = REAL_COMBATANT_INFO.index("[Player-1378-0B46B91A,1296934")


def perf_header(source, source_name, source_flags, dest, dest_name, dest_flags,
                dest_raid="0x80000000"):
    return [source, q(source_name), source_flags, "0x80000000",
            dest, q(dest_name), dest_flags, dest_raid]


def perf_block(guid, power_type=0, current=500, maximum=1000, max_hp=1000):
    """The 19-field advanced block describing `guid`."""
    return [guid, "0000000000000000", str(max_hp), str(max_hp), "0", "0", "0", "0", "0",
            "0", str(power_type), str(current), str(maximum), "0", "1.0", "2.0", "2607",
            "0.0", "320"]


def perf_combatant(guid=DKYAM, auras=None):
    """REAL_COMBATANT_INFO (spec 62) for `guid` with its pre-pull aura list replaced."""
    text = REAL_COMBATANT_INFO
    if auras is not None:
        entries = ",".join("%s,%d,%d" % aura for aura in auras)
        text = text[:PERF_AURA_LIST_START] + "[%s],1,0,0,0" % entries
    return text.replace("COMBATANT_INFO," + DKYAM, "COMBATANT_INFO," + guid, 1)


class PerformanceAccumulatorTests(ExtractorTestCase):
    """Hand-computable fixtures for PerformanceAccumulator (performance.json v1)."""

    START = datetime(2026, 10, 1, 21, 40, 0)

    def at(self, seconds):
        return self.START + timedelta(seconds=seconds)

    # -- line constructors: (seconds, event, fields) ---------------------------------
    def start(self, seconds=0, encounter="3421"):
        return (seconds, "ENCOUNTER_START",
                [encounter, q(PERF_ENCOUNTER_NAME), "15", "20", "2900"])

    def end(self, seconds, encounter="3421", success="0", fight_ms=None):
        fight_ms = int(seconds * 1000) if fight_ms is None else fight_ms
        return (seconds, "ENCOUNTER_END", [encounter, q(PERF_ENCOUNTER_NAME), "15", "20",
                                           success, str(fight_ms)])

    def damage(self, seconds, spell, amount, overkill="-1", critical="nil", absorbed="0",
               source=DKYAM, source_name=PERF_NAME, source_flags=PERF_PLAYER_FLAGS,
               dest=PERF_BOSS, dest_name=PERF_BOSS_NAME, dest_flags=PERF_HOSTILE,
               dest_raid="0x80000000", event="SPELL_DAMAGE", block=None):
        payload = [str(spell), q("Spell %s" % spell), "0x40"] + \
            (block or perf_block(dest, max_hp=5000000)) + \
            [str(amount), str(amount), str(overkill), "64", "0", "0", str(absorbed),
             critical, "nil", "nil", "ST"]
        return (seconds, event, perf_header(source, source_name, source_flags, dest,
                                            dest_name, dest_flags, dest_raid) + payload)

    def swing(self, seconds, amount, event="SWING_DAMAGE"):
        # Modern swing: block = attacker, 10-field tail without ST/AOE.
        return (seconds, event,
                perf_header(DKYAM, PERF_NAME, PERF_PLAYER_FLAGS, PERF_BOSS,
                            PERF_BOSS_NAME, PERF_HOSTILE) + perf_block(DKYAM) +
                [str(amount), str(amount), "-1", "1", "0", "0", "0", "nil", "nil", "nil"])

    def cast(self, seconds, kind, spell, power=None, block_guid=DKYAM,
             dest=PERF_BOSS, dest_name=PERF_BOSS_NAME, reason="Not yet recovered"):
        event = {"start": "SPELL_CAST_START", "success": "SPELL_CAST_SUCCESS",
                 "failed": "SPELL_CAST_FAILED"}[kind]
        fields = perf_header(DKYAM, PERF_NAME, PERF_PLAYER_FLAGS, dest, dest_name,
                             PERF_HOSTILE) + [str(spell), q("Spell %s" % spell), "0x40"]
        if kind == "failed":
            fields.append(q(reason))
        elif power is not None:
            fields += perf_block(block_guid, power[0], power[1], power[2])
        return (seconds, event, fields)

    def aura(self, seconds, event, spell, source=DKYAM, source_name=PERF_NAME,
             dest=DKYAM, dest_name=PERF_NAME, stacks=None, dest_flags=PERF_PLAYER_FLAGS):
        fields = perf_header(source, source_name, PERF_PLAYER_FLAGS, dest, dest_name,
                             dest_flags) + [str(spell), q("Aura %s" % spell), "0x1", "BUFF"]
        if stacks is not None:
            fields.append(str(stacks))
        return (seconds, event, fields)

    def absorbed(self, seconds, spell, amount, source=DKYAM, source_name=PERF_NAME,
                 swing=False):
        attacker = [] if swing else [str(spell), q("Spell %s" % spell), "0x40"]
        return (seconds, "SPELL_ABSORBED",
                perf_header(source, source_name, PERF_PLAYER_FLAGS, PERF_BOSS,
                            PERF_BOSS_NAME, PERF_HOSTILE) + attacker +
                [PERF_BOSS, q(PERF_BOSS_NAME), PERF_HOSTILE, "0x0", "1000", q("Shield"),
                 "0x1", str(amount), "50000", "nil"])

    def energize(self, seconds, spell, amount, over, power_type, block_guid=DKYAM,
                 source=DKYAM, source_name=PERF_NAME, current=700):
        return (seconds, "SPELL_ENERGIZE",
                perf_header(source, source_name, PERF_PLAYER_FLAGS, DKYAM, PERF_NAME,
                            PERF_PLAYER_FLAGS) + [str(spell), q("Spell %s" % spell), "0x40"] +
                perf_block(block_guid, 0, current, 1000) +
                [str(amount), str(over), str(power_type), "4"])

    def died(self, seconds, guid=DKYAM, name=PERF_NAME):
        return (seconds, "UNIT_DIED", perf_header("0000000000000000", "nil", "0x80000000",
                                                  guid, name, PERF_PLAYER_FLAGS) + ["0"])

    def resurrect(self, seconds):
        return (seconds, "SPELL_RESURRECT",
                perf_header(PERF_HEALER, PERF_HEALER_NAME, PERF_PLAYER_FLAGS, DKYAM,
                            PERF_NAME, PERF_PLAYER_FLAGS) + ["20484", q("Rebirth"), "0x8"])

    def heal_on_player(self, seconds):
        return (seconds, "SPELL_HEAL",
                perf_header(PERF_HEALER, PERF_HEALER_NAME, PERF_PLAYER_FLAGS, DKYAM,
                            PERF_NAME, PERF_PLAYER_FLAGS) +
                ["2061", q("Flash Heal"), "0x2", "100", "100", "0", "0", "nil"])

    def summon(self, seconds, pet):
        return (seconds, "SPELL_SUMMON",
                perf_header(DKYAM, PERF_NAME, PERF_PLAYER_FLAGS, pet, "Fénix", "0x2111") +
                ["321507", q("Fénix Arcano"), "0x40"])

    def combatant(self, seconds, guid=DKYAM, auras=None):
        text = perf_combatant(guid, auras)
        return (seconds, "COMBATANT_INFO", [text[len("COMBATANT_INFO,"):]])

    # -- feeding -------------------------------------------------------------------------
    def run_lines(self, lines, selector="Dkyam", spec_rules=None, game_context=None):
        """Feed lines through AnalysisSession (the real hook); return (result, offsets)."""
        accumulator = wle.PerformanceAccumulator(selector, 3421, self.START,
                                                 spec_rules=spec_rules)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        session = wle.AnalysisSession(temp.name, wle.KIND_RAID, performance=accumulator)
        offset, offsets, end = 0, [], None
        for seconds, event, fields in lines:
            raw = line_bytes(self.at(seconds), event, *fields)
            _, parsed_event, args = wle.parse_line(raw.decode("utf-8").rstrip("\r\n"), 2026)
            session.consume(raw, self.at(seconds), parsed_event, args, offset)
            offsets.append(offset)
            offset += len(raw)
            if event == "ENCOUNTER_END" and end is None:
                end = int(fields[5])
        session.close_streams()
        metadata = {"segment_id": "raid|test|3421", "source_file": LOG_NAME,
                    "encounter_id": 3421, "boss": PERF_ENCOUNTER_NAME,
                    "complete": end is not None, "success": False if end else None,
                    "duration_ms": end}
        result = accumulator.result(metadata, game_context, "fp", segment_start_offset=0)
        # Strict JSON: no NaN/Infinity, and the published bytes parse back the same.
        encoded = wle.performance_json_bytes(result)
        self.assertEqual(json.loads(encoded, parse_constant=self.fail), result)
        return result, offsets

    def result_of(self, lines, **kwargs):
        return self.run_lines(lines, **kwargs)[0]

    @staticmethod
    def spell_row(rows, spell_id, key="spell_id"):
        matches = [row for row in rows if row[key] == spell_id]
        return matches[0] if matches else None

    def aura_row(self, result, spell_id, scope="on_player"):
        return self.spell_row(result["auras"][scope], spell_id)

    # -- real-path helpers -----------------------------------------------------------
    def perf_options(self, selector="Dkyam"):
        return wle.OutputOptions(analysis_only=True, performance_player=selector)

    def extract(self, builder, options):
        """Run once over the builder's log; return the segments handed to publish."""
        self.write_log(builder.data())
        extractor = self.make_extractor(options)
        extractor.prepare()
        with mock.patch.object(wle.SegmentPublisher, "publish", autospec=True,
                               side_effect=wle.SegmentPublisher.publish) as publish:
            result = extractor.run_once()
        self.assertEqual(result[2], 0)
        return [call.args[1] for call in publish.call_args_list]

    def build(self, lines, builder=None):
        builder = builder or LogBuilder()
        for index, (seconds, event, fields) in enumerate(lines):
            mark = {"ENCOUNTER_START": "start", "ENCOUNTER_END": "end"}.get(event)
            if mark in builder.marks:
                mark = None
            builder.add(self.at(seconds), event, *fields, mark=mark or "line%d" % index)
        return builder

    # -- boundaries ------------------------------------------------------------------
    def boundary_lines(self):
        return [
            self.cast(-5, "start", 100),
            self.damage(-4, 300, 50),
            self.aura(-3, "SPELL_AURA_APPLIED", 200),
            # Same timestamp as the START, but before it: pre-context by order.
            self.damage(0, 300, 70),
            self.start(0),
            self.damage(0, 300, 30),
            self.cast(1, "success", 100),
            self.damage(2, 300, 1000),
            self.end(30),
            self.damage(31, 300, 999),
        ]

    def test_boundaries_through_the_extractor(self):
        builder = self.build(self.boundary_lines())
        segments = self.extract(builder, self.perf_options())
        self.assertEqual(len(segments), 1)
        result = segments[0].performance_result()
        damage = result["damage"]
        self.assertEqual(damage["player"]["effective"], 1030)
        self.assertEqual(damage["excluded"], {"self_damage": 0, "non_hostile_target": 0,
                                              "pre_context": 120, "post_context": 999})
        cast = self.spell_row(result["casts"]["by_spell"], 100)
        self.assertEqual((cast["start"], cast["success"], cast["started_before_pull"],
                          cast["start_without_outcome"]), (0, 1, 1, 0))
        aura = self.aura_row(result, 200)
        self.assertEqual(aura["intervals"], [{"start": 0.0, "end": 30.0,
                                              "start_basis": "pre_context",
                                              "end_basis": "encounter_end",
                                              "max_stacks": 1}])
        self.assertEqual(aura["uptime_observed_s"], 30.0)
        self.assertEqual(result["segment"]["duration_basis"], "encounter_end")
        self.assertEqual(result["segment"]["result"], "wipe")
        self.assertEqual(result["source"], {
            "file": LOG_NAME, "segment_start_offset": builder.marks["line0"][0],
            "encounter_start_offset": builder.marks["start"][0],
            "encounter_end_offset": builder.marks["end"][0],
            "observation_end_offset": builder.marks["line9"][0]})
        self.assertEqual(result["opener"]["entries"],
                         [[-5000, "start", 100, "261477"], [1000, "success", 100, "261477"]])
        self.assertEqual(result["opener"]["signature"], [100])

    def test_earlier_short_pull_in_the_pre_context(self):
        lines = [self.start(-8), self.damage(-7, 300, 500), self.end(-6, fight_ms=2000),
                 self.start(0), self.damage(1, 300, 10), self.end(20)]
        builder = LogBuilder()
        for index, (seconds, event, fields) in enumerate(lines):
            builder.add(self.at(seconds), event, *fields, mark="line%d" % index)
        segments = self.extract(builder, self.perf_options())
        self.assertEqual(len(segments), 2)
        result = segments[1].performance_result()
        self.assertEqual(result["source"]["encounter_start_offset"],
                         builder.marks["line3"][0])
        self.assertEqual(result["source"]["encounter_end_offset"], builder.marks["line5"][0])
        self.assertEqual(result["source"]["segment_start_offset"], builder.marks["line0"][0])
        self.assertEqual(result["damage"]["player"]["effective"], 10)
        self.assertEqual(result["damage"]["excluded"]["pre_context"], 500)
        self.assertEqual(result["segment"]["observed_seconds"], 20.0)

    def test_flag_absent_creates_nothing_and_changes_no_output(self):
        lines = self.boundary_lines() + [self.died(15)]
        outputs = {}
        for label, options in (("plain", wle.OutputOptions(analysis_only=True)),
                               ("perf", self.perf_options())):
            self.output_dir = os.path.join(self.root, "Output-" + label)
            self.state_path = os.path.join(self.output_dir, wle.STATE_FILENAME)
            segments = self.extract(self.build(lines), options)
            if label == "plain":
                self.assertIsNone(segments[0].performance)
                self.assertIsNone(segments[0].performance_result())
            else:
                self.assertIsNotNone(segments[0].performance)
            [name] = [entry for entry in self.list_outputs(self.raids_dir())
                      if os.path.isdir(os.path.join(self.raids_dir(), entry))]
            analysis_dir = os.path.join(self.raids_dir(), name, "analysis")
            outputs[label] = {}
            for artifact in ("combat.txt", "summary.json", "players.json", "deaths.json"):
                with open(os.path.join(analysis_dir, artifact), "rb") as handle:
                    outputs[label][artifact] = handle.read()
        self.assertEqual(outputs["plain"], outputs["perf"])

    def test_mythic_plus_never_gets_an_accumulator(self):
        builder = LogBuilder()
        builder.add(self.at(0), "CHALLENGE_MODE_START", q("Dungeon"), "2290", "375", "10",
                    "[9,152]")
        builder.add(self.at(1), "ENCOUNTER_START", "3421", q(PERF_ENCOUNTER_NAME), "8", "5",
                    "2290")
        self.add_lines(builder, [self.damage(2, 300, 10)])
        builder.add(self.at(3), "ENCOUNTER_END", "3421", q(PERF_ENCOUNTER_NAME), "8", "5",
                    "1", "2000")
        builder.add(self.at(4), "CHALLENGE_MODE_END", "2290", "1", "10", "4000")
        segments = self.extract(builder, self.perf_options())
        self.assertEqual([segment.kind for segment in segments], [wle.KIND_MPLUS])
        self.assertIsNone(segments[0].performance)

    def add_lines(self, builder, lines):
        for seconds, event, fields in lines:
            builder.add(self.at(seconds), event, *fields)

    # -- incomplete pulls ----------------------------------------------------------------
    def test_incomplete_pull_uses_the_observation_end(self):
        result, offsets = self.run_lines([
            self.start(0), self.aura(1, "SPELL_AURA_APPLIED", 200),
            self.damage(2, 300, 100), self.heal_on_player(10)])
        segment = result["segment"]
        self.assertEqual((segment["duration_ms"], segment["complete"], segment["result"],
                          segment["duration_basis"], segment["observed_seconds"]),
                         (None, False, "incomplete", "observation_end", 10.0))
        rates = result["damage"]["rates"]
        self.assertEqual(rates["dps_observed"],
                         {"value": 10.0, "numerator": 100, "denominator_s": 10.0})
        self.assertIsNone(rates["dps_encounter"]["value"])
        self.assertIn("incomplete", rates["dps_encounter"]["reason"])
        self.assertEqual(self.aura_row(result, 200)["intervals"][0]["end_basis"],
                         "observation_end")
        self.assertEqual(self.aura_row(result, 200)["intervals"][0]["end"], 10.0)
        self.assertEqual(result["source"]["observation_end_offset"], offsets[-1])
        self.assertIsNone(result["source"]["encounter_end_offset"])

    def test_complete_pull_has_no_dps_observed(self):
        result = self.result_of([self.start(0), self.damage(2, 300, 300), self.end(30)])
        rates = result["damage"]["rates"]
        self.assertNotIn("dps_observed", rates)
        self.assertEqual(rates["dps_encounter"],
                         {"value": 10.0, "numerator": 300, "denominator_s": 30.0})

    # -- casts versus hits ---------------------------------------------------------------
    def test_channel_periodic_and_casts_without_outcome(self):
        result = self.result_of([
            self.start(0),
            self.cast(1, "success", 5143),
            self.damage(1.2, 7268, 100), self.damage(1.4, 7268, 100),
            self.damage(1.6, 7268, 100),
            self.damage(2, 210833, 40, event="SPELL_PERIODIC_DAMAGE"),
            self.damage(4, 210833, 40, event="SPELL_PERIODIC_DAMAGE"),
            self.cast(3, "start", 44425),
            self.cast(5, "start", 30451), self.cast(5.5, "failed", 30451),
            self.end(30)])
        casts = result["casts"]["by_spell"]
        self.assertEqual(self.spell_row(casts, 5143)["success"], 1)
        ticks = self.spell_row(result["damage"]["by_spell"], 7268)
        self.assertEqual((ticks["hits"], ticks["ticks"], ticks["amount"]), (3, 0, 300))
        periodic = self.spell_row(result["damage"]["by_spell"], 210833)
        self.assertEqual((periodic["hits"], periodic["ticks"]), (0, 2))
        self.assertEqual(result["damage"]["player"]["ticks"], 2)
        no_outcome = self.spell_row(casts, 44425)
        self.assertEqual((no_outcome["start"], no_outcome["success"],
                          no_outcome["start_without_outcome"]), (1, 0, 1))
        self.assertNotIn("cancel", json.dumps(result))
        failed = self.spell_row(casts, 30451)
        self.assertEqual((failed["failed"], failed["start_without_outcome"]),
                         ({"Not yet recovered": 1}, 0))
        self.assertEqual(result["damage"]["damage_without_cast"], [7268, 210833])
        self.assertEqual(result["casts"]["total_success"], 1)

    # -- damage --------------------------------------------------------------------------
    def damage_lines(self):
        vehicle = "Vehicle-0-3109-3004-27445-250000-0000000001"
        return [
            self.start(0),
            self.damage(1, 44425, 1000, dest_raid="0x80000008"),
            self.damage(2, 44425, 500, overkill="200", critical="1"),
            self.absorbed(3, 44425, 700),                 # fully absorbed: no damage line
            self.damage(4, 44425, 400, absorbed="50"),    # partially absorbed ...
            self.absorbed(4, 44425, 50),                  # ... and its SPELL_ABSORBED
            self.damage(5, 1309786, 999, dest=DKYAM, dest_name=PERF_NAME,
                        dest_flags=PERF_PLAYER_FLAGS, block=perf_block(DKYAM)),
            self.damage(6, 44425, 333, dest=PERF_FRIEND, dest_name="Amigo",
                        dest_flags=PERF_FRIENDLY),
            self.swing(7, 200), self.swing(7, 200, event="SWING_DAMAGE_LANDED"),
            self.absorbed(8, None, 25, swing=True),
            self.damage(9, 153640, 10, dest=vehicle, dest_name=PERF_ENCOUNTER_NAME),
            self.end(30)]

    def test_damage_semantics(self):
        damage = self.result_of(self.damage_lines())["damage"]
        self.assertEqual(damage["player"], {"hits": 5, "ticks": 0, "crits": 1,
                                            "amount": 2110, "overkill": 200,
                                            "effective": 1910, "absorbed_by_target": 775})
        self.assertEqual(damage["excluded"], {"self_damage": 999, "non_hostile_target": 333,
                                              "pre_context": 0, "post_context": 0})
        arcane = self.spell_row(damage["by_spell"], 44425)
        self.assertEqual((arcane["hits"], arcane["amount"], arcane["overkill"],
                          arcane["effective"], arcane["absorbed_by_target"]),
                         (3, 1900, 200, 1700, 750))
        melee = self.spell_row(damage["by_spell"], wle.MELEE_SPELL_ID)
        self.assertEqual((melee["hits"], melee["amount"], melee["absorbed_by_target"]),
                         (1, 200, 25))
        self.assertIsNone(self.spell_row(damage["by_spell"], 1309786))
        self.assertEqual([row["spell_id"] for row in damage["by_spell"]],
                         [44425, wle.MELEE_SPELL_ID, 153640])
        boss = self.spell_row(damage["by_target"], "261477", key="key")
        self.assertEqual((boss["npc_id"], boss["kind"], boss["instances"], boss["hits"],
                          boss["amount"], boss["effective"], boss["role"]),
                         (261477, "creature", 1, 4, 2100, 1900, "unknown"))
        self.assertEqual(boss["evidence"], {"max_hp": 5000000, "raid_marker": 8,
                                            "name_matches_encounter": False})
        named = self.spell_row(damage["by_target"], "250000", key="key")
        self.assertEqual((named["kind"], named["role"]), ("vehicle", "boss"))
        self.assertEqual(damage["total_effective"], 1910)
        # Auto-attack is never cast: it is not reported as damage without a cast.
        self.assertEqual(damage["damage_without_cast"], [44425, 153640])

    def test_overkill_minus_one_is_no_overkill(self):
        row = self.spell_row(self.result_of([self.start(0), self.damage(1, 7, 1000),
                                             self.end(30)])["damage"]["by_spell"], 7)
        self.assertEqual((row["amount"], row["overkill"], row["effective"]), (1000, 0, 1000))

    # -- auras ---------------------------------------------------------------------------
    def test_aura_lifecycle(self):
        result = self.result_of([
            self.start(0),
            self.aura(2, "SPELL_AURA_APPLIED", 17, source=PERF_HEALER,
                      source_name=PERF_HEALER_NAME),
            self.aura(4, "SPELL_AURA_REMOVED", 17, source=PERF_HEALER,
                      source_name=PERF_HEALER_NAME),
            self.aura(5, "SPELL_AURA_APPLIED", 1001),
            self.aura(8, "SPELL_AURA_REFRESH", 1001),
            self.aura(9, "SPELL_AURA_APPLIED_DOSE", 1001, stacks=3),
            self.aura(10, "SPELL_AURA_REMOVED_DOSE", 1001, stacks=2),
            self.aura(15, "SPELL_AURA_REMOVED", 1001),
            self.aura(20, "SPELL_AURA_APPLIED", 1002),
            self.aura(1, "SPELL_AURA_APPLIED", 210824, dest=PERF_BOSS,
                      dest_name=PERF_BOSS_NAME, dest_flags=PERF_HOSTILE),
            self.aura(11, "SPELL_AURA_REMOVED", 210824, dest=PERF_BOSS,
                      dest_name=PERF_BOSS_NAME, dest_flags=PERF_HOSTILE),
            self.end(40)])
        cycle = self.aura_row(result, 1001)
        self.assertEqual((cycle["applications"], cycle["refreshes"], cycle["max_stacks"],
                          cycle["uptime_observed_s"], cycle["uptime_upper_bound_s"],
                          cycle["unknown_start_intervals"], cycle["source"]),
                         (1, 1, 3, 10.0, 10.0, 0, "self"))
        self.assertEqual(cycle["intervals"], [{"start": 5.0, "end": 15.0,
                                               "start_basis": "observed",
                                               "end_basis": "observed",
                                               "max_stacks": 3}])
        still_open = self.aura_row(result, 1002)
        self.assertEqual(still_open["intervals"][0]["end_basis"], "encounter_end")
        self.assertEqual(still_open["uptime_observed_s"], 20.0)
        other = self.aura_row(result, 17)
        self.assertEqual((other["source"], other["uptime_observed_s"]), ("other", 2.0))
        debuff = self.aura_row(result, 210824, scope="from_player")
        self.assertEqual((debuff["applications"], debuff["removals"], debuff["targets"]),
                         (1, 1, 1))
        self.assertNotIn("intervals", debuff)
        self.assertEqual(result["auras"]["coverage"],
                         {"complete": True, "rejected_keys": 0,
                          "rejected_keys_is_lower_bound": False, "first_rejected_s": None})

    def test_removal_only_aura_has_an_unknown_start(self):
        aura = self.aura_row(self.result_of([
            self.start(0), self.heal_on_player(1),
            self.aura(30, "SPELL_AURA_REMOVED", 1003), self.end(40)]), 1003)
        self.assertEqual(aura["intervals"], [{"start": None, "end": 30.0,
                                              "start_basis": "unknown",
                                              "end_basis": "observed", "max_stacks": 0}])
        self.assertEqual((aura["uptime_observed_s"], aura["uptime_upper_bound_s"],
                          aura["unknown_start_intervals"]), (0.0, 30.0, 1))

    def test_partial_initial_snapshot(self):
        result = self.result_of([
            self.start(0),
            self.combatant(0, auras=[(DKYAM, 1004, 1), (PERF_HEALER, 1005, 2)]),
            self.heal_on_player(0.5),
            self.aura(6, "SPELL_AURA_REMOVED", 1006),
            self.aura(12, "SPELL_AURA_REMOVED", 1004),
            self.end(40)])
        listed = self.aura_row(result, 1004)
        self.assertEqual(listed["intervals"], [{"start": 0.0, "end": 12.0,
                                                "start_basis": "combatant_info",
                                                "end_basis": "observed", "max_stacks": 1}])
        self.assertEqual(listed["uptime_observed_s"], 12.0)
        from_other = self.aura_row(result, 1005)
        self.assertEqual((from_other["source"], from_other["max_stacks"],
                          from_other["intervals"][0]["end_basis"],
                          from_other["uptime_observed_s"]), ("other", 2, "encounter_end", 40.0))
        unlisted = self.aura_row(result, 1006)
        self.assertEqual((unlisted["intervals"][0]["start_basis"],
                          unlisted["uptime_observed_s"], unlisted["uptime_upper_bound_s"]),
                         ("unknown", 0.0, 6.0))
        self.assertEqual(result["character"]["initial_auras"], {"status": "ok", "count": 2})

    def test_aura_is_not_closed_by_death(self):
        aura = self.aura_row(self.result_of([
            self.start(0), self.aura(1, "SPELL_AURA_APPLIED", 1001), self.died(5),
            self.end(30)]), 1001)
        self.assertEqual(aura["intervals"][0]["end"], 30.0)

    # -- life ------------------------------------------------------------------------------
    def test_death_resurrection_and_damage_after_death(self):
        result = self.result_of([
            self.start(0), self.cast(1, "success", 5143), self.damage(2, 7, 100),
            self.died(10), self.damage(11, 7, 50), self.resurrect(20),
            self.damage(25, 7, 200), self.end(30)])
        life = result["life"]
        self.assertEqual((life["alive_at_start"], life["basis"], life["deaths"],
                          life["resurrections"], life["alive_seconds"],
                          life["dead_seconds"]),
                         (True, "first_action", [{"t_s": 10.0}], [{"t_s": 20.0}], 20.0, 10.0))
        damage = result["damage"]
        self.assertEqual((damage["total_effective"], damage["after_death_effective"]),
                         (350, 50))
        self.assertEqual(damage["rates"]["dps_while_alive"],
                         {"value": 15.0, "numerator": 300, "denominator_s": 20.0})

    def test_death_in_post_context_is_evidence_only(self):
        life = self.result_of([self.start(0), self.cast(1, "success", 5143), self.end(30),
                               self.died(30.33)])["life"]
        self.assertEqual((life["deaths"], life["death_in_post_context"], life["dead_seconds"]),
                         ([], [{"after_end_s": 0.33}], 0.0))

    def test_dead_at_start_known_from_the_pre_context(self):
        result = self.result_of([self.died(-2), self.start(0), self.end(30)])
        life = result["life"]
        self.assertEqual((life["alive_at_start"], life["basis"], life["alive_seconds"],
                          life["dead_seconds"]), (False, "pre_context", 0.0, 30.0))
        self.assertIsNone(result["damage"]["rates"]["dps_while_alive"]["value"])
        self.assertIn("reason", result["damage"]["rates"]["dps_while_alive"])

    def test_unknown_start_followed_by_resurrection(self):
        life = self.result_of([self.start(0), self.resurrect(5),
                               self.cast(6, "success", 5143), self.end(30)])["life"]
        self.assertEqual((life["alive_at_start"], life["basis"], life["dead_seconds"],
                          life["alive_seconds"]), (False, "resurrection", 5.0, 25.0))

    def test_no_life_evidence_is_unknown(self):
        result = self.result_of([self.start(0), self.heal_on_player(3), self.end(30)])
        life = result["life"]
        self.assertEqual((life["alive_at_start"], life["basis"], life["alive_seconds"]),
                         ("unknown", None, None))
        self.assertEqual(result["damage"]["rates"]["dps_while_alive"]["value"], None)
        self.assertIn("reason", result["damage"]["rates"]["dps_while_alive"])
        self.assertIsNone(result["continuity"]["value"])
        self.assertIn("reason", result["continuity"])

    # -- denominators ----------------------------------------------------------------------
    def test_zero_denominators_are_null_with_reason(self):
        result = self.result_of([self.died(-1), self.start(0)])
        self.assertEqual(result["segment"]["observed_seconds"], 0.0)
        rates = [result["damage"]["rates"]["dps_observed"],
                 result["damage"]["rates"]["dps_encounter"],
                 result["damage"]["rates"]["dps_while_alive"],
                 result["casts"]["rates"]["casts_per_minute"]]
        for rate in rates:
            self.assertIsNone(rate["value"])
            self.assertTrue(rate["reason"])
        with self.assertRaises(ValueError):
            wle.performance_json_bytes({"value": float("nan")})

    # -- pets ------------------------------------------------------------------------------
    def test_pet_damage_is_attributed_only_with_a_known_owner(self):
        stray = "Creature-0-3109-3004-27445-99999-00000000BB"
        result = self.result_of([
            self.start(0), self.summon(1, PERF_PET),
            self.damage(2, 1234, 300, source=PERF_PET, source_name="Fénix",
                        source_flags="0x2111"),
            # Same NPC id, another GUID than the summoned one: no owner evidence.
            self.damage(3, 1234, 400, source=stray, source_name="Fénix",
                        source_flags="0x2111"),
            self.damage(4, 7, 100), self.end(30)])
        damage = result["damage"]
        self.assertEqual(damage["pets"]["effective"], 300)
        self.assertEqual(damage["pets"]["by_pet"],
                         [{"npc_id": 99999, "name": "Fénix", "effective": 300,
                           "by_spell": [{"spell_id": 1234, "name": "Spell 1234", "hits": 1,
                                         "ticks": 0, "crits": 0, "amount": 300,
                                         "overkill": 0, "effective": 300,
                                         "absorbed_by_target": 0}]}])
        self.assertIsNone(self.spell_row(damage["by_spell"], 1234))
        self.assertEqual((damage["player"]["effective"], damage["total_effective"]),
                         (100, 400))

    # -- resources -------------------------------------------------------------------------
    def test_resources_from_the_caster_block_only(self):
        result = self.result_of([
            self.start(0),
            self.cast(1, "success", 1, power=(0, 900, 1000)),
            self.damage(2, 7, 10, block=perf_block(PERF_BOSS, 0, 7777, 9999)),
            self.cast(3, "success", 2, power=(0, 1, 1000), block_guid=PERF_BOSS),
            self.cast(3.5, "success", 3),                           # no block at all
            self.cast(4, "success", 4, power=(0, 800, 1000)),
            self.cast(5, "success", 5, power=(0, 850, 1000)),
            self.energize(6, 321507, 4, 1, 16),
            self.energize(7, 321507, 0, 4, 16),
            # Energized by someone else: the block describes that caster.
            self.energize(8, 999, 100, 0, 0, block_guid=PERF_HEALER, source=PERF_HEALER,
                          source_name=PERF_HEALER_NAME, current=12345),
            self.end(30)])
        mana = result["resources"]["by_power_type"]["0"]
        self.assertEqual((mana["samples"], mana["max_gap_s"], mana["min_observed"],
                          mana["max_observed"], mana["partial"]), (5, 3.0, 700, 900, False))
        self.assertEqual(mana["first"], {"t_s": 1.0, "current": 900, "max": 1000})
        self.assertEqual(mana["last"], {"t_s": 7.0, "current": 700, "max": 1000})
        self.assertEqual(mana["series"], [[1.0, 900], [4.0, 800], [5.0, 850], [6.0, 700],
                                          [7.0, 700]])
        self.assertEqual(list(result["resources"]["by_power_type"]), ["0"])
        charges = self.spell_row(result["resources"]["energize"], 321507)
        self.assertEqual((charges["power_type"], charges["events"], charges["amount"],
                          charges["over_energize"]), (16, 2, 4, 5))
        self.assertEqual(result["casts"]["total_success"], 5)

    def test_resources_from_every_block_that_describes_the_player(self):
        heal = (2, "SPELL_HEAL",
                perf_header(PERF_HEALER, PERF_HEALER_NAME, PERF_PLAYER_FLAGS, DKYAM,
                            PERF_NAME, PERF_PLAYER_FLAGS) +
                ["2061", q("Flash Heal"), "0x2"] + perf_block(DKYAM, 0, 600, 1000) +
                ["100", "100", "0", "0", "nil"])
        tail = ["100", "100", "-1", "1", "0", "0", "0", "nil", "nil", "nil"]
        # SWING_DAMAGE's block is the attacker (the boss); LANDED's is the victim.
        boss_swing = (4, "SWING_DAMAGE",
                      perf_header(PERF_BOSS, PERF_BOSS_NAME, PERF_HOSTILE, DKYAM, PERF_NAME,
                                  PERF_PLAYER_FLAGS) + perf_block(PERF_BOSS, 0, 7, 9) + tail)
        landed = (5, "SWING_DAMAGE_LANDED",
                  perf_header(PERF_BOSS, PERF_BOSS_NAME, PERF_HOSTILE, DKYAM, PERF_NAME,
                              PERF_PLAYER_FLAGS) + perf_block(DKYAM, 0, 550, 1000) + tail)
        result = self.result_of([
            self.start(0),
            self.cast(1, "success", 1, power=(0, 900, 1000)),
            heal,
            # The player's damage to the boss: the block describes the boss.
            self.damage(3, 7, 10, block=perf_block(PERF_BOSS, 0, 7777, 9999)),
            boss_swing, landed,
            self.end(30)])
        mana = result["resources"]["by_power_type"]["0"]
        self.assertEqual(mana["series"], [[1.0, 900], [2.0, 600], [5.0, 550]])
        self.assertEqual((mana["samples"], mana["max_gap_s"], mana["min_observed"],
                          mana["max_observed"]), (3, 3.0, 550, 900))

    def test_character_item_level_excludes_shirt_and_tabard(self):
        character = self.result_of([self.start(0), self.combatant(0),
                                    self.cast(1, "success", 1), self.end(30)])["character"]
        self.assertEqual((character["item_level"], character["item_level_basis"]),
                         (320, "equipped_slots_excluding_shirt_tabard"))
        self.assertEqual([item[1] for item in character["equipment"]["items"]],
                         [318, 311, 321, 0, 321, 334, 321, 318, 331, 308, 311, 321, 321,
                          321, 308, 334, 0, 2])
        # players.json keeps its own reading of the same line.
        event, args = real_args(REAL_COMBATANT_INFO)
        self.assertEqual(wle.parse_combat_event(event, args).item_level, 300)
        # Any other slot count: the all-positive-slots average, labelled as such.
        short = REAL_COMBATANT_INFO.replace(
            "[(271564,318,(7959,0,0),(13334,6652,13696,13692,13698,12845),()),", "[", 1)
        expected = wle.parse_combat_event(*real_args(short)).item_level
        line = (0, "COMBATANT_INFO", [short[len("COMBATANT_INFO,"):]])
        character = self.result_of([self.start(0), line, self.cast(1, "success", 1),
                                    self.end(30)])["character"]
        self.assertEqual(len(character["equipment"]["items"]), 17)
        self.assertEqual((character["item_level"], character["item_level_basis"]),
                         (expected, "all_positive_slots"))
        self.assertEqual(expected, 299)

    # -- identity --------------------------------------------------------------------------
    def identity_lines(self):
        return [self.start(0), self.combatant(0, auras=[(DKYAM, 1004, 1)]),
                self.cast(1, "success", 5143), self.damage(2, 7268, 100), self.end(30)]

    def test_selector_forms_give_identical_results(self):
        results = {}
        for selector in (DKYAM, PERF_NAME, "dkyam", "DKYAM-dunmodr-eu"):
            segments = self.extract(self.build(self.identity_lines()),
                                    self.perf_options(selector))
            result = segments[0].performance_result()
            self.assertEqual(result["player"].pop("selector"), selector)
            result.pop("fingerprint")
            results[selector] = result
        reference = results[DKYAM]
        self.assertEqual(reference["player"]["status"], "resolved")
        self.assertEqual(reference["character"]["spec_id"], 62)
        self.assertEqual(reference["character"]["combatant_info"], "ok")
        self.assertEqual(self.aura_row(reference, 1004)["intervals"][0]["start_basis"],
                         "combatant_info")
        for selector, result in results.items():
            self.assertEqual(result, reference, selector)

    def test_exact_guid_in_combatant_info_resolves_the_player(self):
        lines = [self.start(0), self.combatant(0, auras=[]), self.end(30)]
        result = self.result_of(lines, selector=DKYAM)
        self.assertEqual(result["player"], {"selector": DKYAM, "status": "resolved",
                                            "guid": DKYAM, "name": None,
                                            "candidates": [{"guid": DKYAM, "name": None}]})
        self.assertEqual(result["character"]["spec_id"], 62)
        self.assertEqual((result["casts"]["total_success"], result["life"]["alive_at_start"]),
                         (0, "unknown"))
        # The name comes with the first event that carries it.
        named = self.result_of(lines[:2] + [self.cast(1, "success", 5143), self.end(30)],
                               selector=DKYAM)["player"]
        self.assertEqual((named["name"], named["candidates"]),
                         (PERF_NAME, [{"guid": DKYAM, "name": PERF_NAME}]))
        # COMBATANT_INFO carries no name: a name selector is not resolved by it.
        self.assertEqual(self.result_of(lines)["player"]["status"], "absent")

    def test_absent_player_has_no_metrics(self):
        segments = self.extract(self.build(self.identity_lines()),
                                self.perf_options("Nadie"))
        result = segments[0].performance_result()
        self.assertEqual(result["player"], {"selector": "Nadie", "status": "absent",
                                            "guid": None, "name": None, "candidates": []})
        for key in ("character", "damage", "casts", "warnings", "spec"):
            self.assertNotIn(key, result)

    def test_two_guids_with_one_short_name_are_ambiguous(self):
        twin = "Player-1379-0000000A"
        lines = self.identity_lines()[:-1] + [
            self.damage(3, 7268, 50, source=twin, source_name="Dkyam-Otro-EU"),
            self.end(30)]
        segments = self.extract(self.build(lines), self.perf_options("Dkyam"))
        result = segments[0].performance_result()
        self.assertEqual(result["player"]["status"], "ambiguous")
        self.assertEqual(result["player"]["candidates"],
                         [{"guid": DKYAM, "name": PERF_NAME},
                          {"guid": twin, "name": "Dkyam-Otro-EU"}])
        self.assertNotIn("damage", result)
        full = self.extract(self.build(lines), self.perf_options(PERF_NAME))
        self.assertEqual(full[-1].performance_result()["player"]["status"], "resolved")

    def test_second_match_after_the_checked_guid_cache_is_full(self):
        twin = "Player-1379-0000000A"
        # Dkyam resolves first; three other players then fill the two-entry cache,
        # so the second Dkyam is only caught by comparing its name directly.
        lines = [self.start(0), self.cast(0.2, "success", 5143)] + [
            self.damage(0.5 + index / 10, 7, 1, source="Player-1-0000000%d" % index,
                        source_name="Otro%d-Realm-EU" % index) for index in range(3)] + \
            [self.damage(2, 7, 1, source=twin, source_name="Dkyam-Otro-EU"), self.end(30)]
        with mock.patch.object(wle, "MAX_PERF_CHECKED_GUIDS", 2):
            segments = self.extract(self.build(lines), self.perf_options("Dkyam"))
            result = segments[0].performance_result()
        self.assertEqual(result["player"]["status"], "ambiguous")
        self.assertEqual(len(result["player"]["candidates"]), 2)

    def test_config_cap_never_adopts_another_players_config(self):
        lines = [self.start(0), self.combatant(0, guid=PERF_HEALER),
                 self.combatant(0, guid=DKYAM), self.cast(1, "success", 5143), self.end(30)]
        with mock.patch.object(wle, "MAX_PERF_CONFIGS", 1):
            result = self.result_of(lines)
        character = result["character"]
        self.assertEqual((character["combatant_info"], character["spec_id"]),
                         ("not_retained", None))
        self.assertIn({"code": "perf_configs_truncated", "cap": 1, "dropped": 1},
                      result["warnings"])
        self.assertEqual(self.result_of(lines)["character"]["combatant_info"], "ok")

    # -- spec section and hook -------------------------------------------------------------
    def test_spec_section_placeholder(self):
        with_spec = self.result_of(self.identity_lines(), spec_rules={})
        self.assertEqual(with_spec["spec"], {"status": "not_applied",
                                             "reason": "no rules registered for spec 62"})
        self.assertIsNone(with_spec["rules"]["spec"])
        without = self.result_of([self.start(0), self.cast(1, "success", 5143),
                                  self.end(30)])
        self.assertEqual(without["spec"], {"status": "not_applied", "reason": "spec unknown"})

    def test_spec_rules_hook(self):
        records = []

        def build(accumulator, rules, build_version):
            return {"status": "applied", "build": build_version,
                    "burst": accumulator.aura_intervals("on_player", 365362),
                    "debuff": accumulator.aura_intervals("from_player", 210824),
                    "energize": [list(event) for event in accumulator.energize_events]}

        rules = {62: {"id": "test", "version": 3, "validated_builds": ("12.1.0",),
                      "tracked_self_auras": (365362,), "tracked_target_auras": (210824,),
                      "channel_tick_spells": (7268,),
                      "observe": lambda acc, rules, record: records.append(record),
                      "build": build}}
        result = self.result_of([
            self.start(0), self.combatant(0), self.cast(1, "success", 5143),
            self.damage(1.5, 7268, 10), self.damage(5, 7268, 10),
            self.aura(6, "SPELL_AURA_APPLIED", 365362), self.aura(9, "SPELL_AURA_REMOVED",
                                                                  365362),
            self.aura(7, "SPELL_AURA_APPLIED", 210824, dest=PERF_BOSS,
                      dest_name=PERF_BOSS_NAME, dest_flags=PERF_HOSTILE),
            self.energize(8, 321507, 4, 0, 16), self.end(30)], spec_rules=rules)
        self.assertEqual(result["rules"]["spec"], {"id": "test", "version": 3,
                                                   "validated_for_build": False})
        spec = result["spec"]
        self.assertEqual(spec["status"], "applied")
        self.assertEqual(spec["burst"][0]["intervals"],
                         [[6000, 9000, "observed", "observed", 1]])
        self.assertEqual(spec["debuff"][0]["intervals"],
                         [[7000, 30000, "observed", "encounter_end", 1]])
        self.assertEqual(spec["energize"], [[8000, 321507, 16, 4, 0]])
        debuff = self.aura_row(result, 210824, scope="from_player")
        self.assertEqual(debuff["intervals"][0]["end_basis"], "encounter_end")
        # Channel ticks are actions: 1.5 -> 5 is the only gap above 2.5 s.
        self.assertEqual((result["continuity"]["gaps_count"],
                          result["continuity"]["gap_max_s"]), (1, 3.5))
        self.assertEqual([record["kind"] for record in records],
                         ["cast", "damage", "damage", "aura", "aura", "aura", "energize"])

    # -- continuity --------------------------------------------------------------------------
    def test_continuity_gaps_within_alive_periods(self):
        continuity = self.result_of([
            self.start(0), self.cast(1, "success", 1), self.cast(2, "start", 2),
            self.cast(6, "success", 2), self.cast(7, "success", 3), self.died(8),
            self.resurrect(20), self.cast(21, "success", 4), self.cast(25, "success", 5),
            self.end(30)])["continuity"]
        self.assertEqual((continuity["gaps_count"], continuity["gaps_total_s"],
                          continuity["gap_max_s"], continuity["threshold_s"]),
                         (2, 8.0, 4.0, 2.5))
        self.assertEqual(continuity["longest"],
                         [{"start_s": 2.0, "end_s": 6.0, "duration_s": 4.0},
                          {"start_s": 21.0, "end_s": 25.0, "duration_s": 4.0}])

    # -- caps --------------------------------------------------------------------------------
    def test_spell_and_target_caps_keep_totals(self):
        lines = [self.start(0)]
        for index, amount in enumerate((10, 20, 30, 40)):
            target = "Creature-0-1-2-3-%d-0000000001" % (500 + index)
            lines.append(self.damage(1 + index, 900 + index, amount, dest=target))
        lines.append(self.end(30))
        with mock.patch.object(wle, "MAX_PERF_SPELLS", 2), \
                mock.patch.object(wle, "MAX_PERF_TARGETS", 3):
            result = self.result_of(lines)
        warnings = {warning["code"]: warning for warning in result["warnings"]}
        damage = result["damage"]
        self.assertEqual([(row["spell_id"], row["effective"]) for row in damage["by_spell"]],
                         [(901, 20), (900, 10), (None, 70)])
        self.assertEqual(damage["player"]["effective"], 100)
        self.assertEqual(sum(row["effective"] for row in damage["by_spell"]), 100)
        self.assertEqual([row["key"] for row in damage["by_target"]],
                         ["502", "501", "500", "other"])
        self.assertEqual(damage["by_target"][-1]["effective"], 40)
        self.assertEqual(warnings["perf_spells_truncated"],
                         {"code": "perf_spells_truncated", "cap": 2, "dropped": 2})
        self.assertEqual(warnings["perf_targets_truncated"]["dropped"], 1)

    def test_timeline_cap_keeps_cast_totals(self):
        lines = [self.start(0)] + [self.cast(1 + index, "success", 5143)
                                   for index in range(5)] + [self.end(30)]
        with mock.patch.object(wle, "MAX_PERF_TIMELINE", 3):
            result = self.result_of(lines)
        self.assertEqual(len(result["timeline"]["entries"]), 3)
        self.assertEqual((result["timeline"]["partial"],
                          result["timeline"]["covered_until_s"]), (True, 4.0))
        self.assertEqual((result["opener"]["partial"], result["opener"]["covered_until_s"]),
                         (True, 4.0))
        self.assertEqual(result["casts"]["total_success"], 5)
        self.assertEqual(self.spell_row(result["casts"]["by_spell"], 5143)["success"], 5)
        self.assertIn({"code": "perf_timeline_truncated", "cap": 3, "dropped": 2},
                      result["warnings"])

    def test_aura_interval_history_saturation(self):
        lines = [self.start(0)]
        for start in (1, 3, 5):
            lines += [self.aura(start, "SPELL_AURA_APPLIED", 1001),
                      self.aura(start + 1, "SPELL_AURA_REMOVED", 1001)]
        lines.append(self.end(30))
        unlimited = self.aura_row(self.result_of(lines), 1001)
        with mock.patch.object(wle, "MAX_PERF_AURA_INTERVALS", 2):
            result = self.result_of(lines)
        aura = self.aura_row(result, 1001)
        self.assertEqual(len(aura["intervals"]), 2)
        self.assertEqual((aura["partial"], aura["covered_until_s"]), (True, 5.0))
        self.assertEqual((unlimited["partial"], len(unlimited["intervals"])), (False, 3))
        for key in ("uptime_observed_s", "uptime_upper_bound_s", "applications"):
            self.assertEqual(aura[key], unlimited[key])
        self.assertEqual(aura["uptime_observed_s"], 3.0)
        self.assertTrue(result["auras"]["coverage"]["complete"])

    def test_aura_key_rejection_spares_reserved_spec_auras(self):
        rules = {62: {"id": "test", "version": 1, "tracked_self_auras": (365362,)}}
        lines = [self.start(0), self.combatant(0, auras=[]),
                 self.aura(1, "SPELL_AURA_APPLIED", 1001),
                 self.aura(2, "SPELL_AURA_APPLIED", 1002),
                 self.aura(8, "SPELL_AURA_REMOVED", 1002),
                 self.aura(10, "SPELL_AURA_APPLIED", 1003),
                 self.aura(12, "SPELL_AURA_APPLIED", 365362),
                 self.aura(30, "SPELL_AURA_REMOVED", 1003),
                 self.end(40)]
        unlimited = self.result_of(lines, spec_rules=rules)
        with mock.patch.object(wle, "MAX_PERF_AURAS", 2):
            result = self.result_of(lines, spec_rules=rules)
        self.assertIsNone(self.aura_row(result, 1003))
        self.assertNotIn('"1003"', json.dumps(result["spell_names"]))
        self.assertEqual(result["auras"]["coverage"],
                         {"complete": False, "rejected_keys": 1,
                          "rejected_keys_is_lower_bound": False, "first_rejected_s": 10.0})
        self.assertIn({"code": "perf_aura_keys_truncated", "cap": 2, "dropped": 1},
                      result["warnings"])
        for spell_id in (1001, 1002):
            self.assertEqual(self.aura_row(result, spell_id),
                             self.aura_row(unlimited, spell_id))
        self.assertEqual(self.aura_row(result, 1002)["uptime_observed_s"], 6.0)
        burst = self.aura_row(result, 365362)
        self.assertEqual(burst["intervals"][0]["start"], 12.0)
        self.assertTrue(unlimited["auras"]["coverage"]["complete"])

    def test_rejected_key_count_stops_when_the_rejected_set_is_full(self):
        lines = [self.start(0), self.aura(1, "SPELL_AURA_APPLIED", 1001),
                 self.aura(2, "SPELL_AURA_APPLIED", 1002),     # rejected, remembered
                 self.aura(3, "SPELL_AURA_APPLIED", 1003),     # rejected set full
                 self.aura(4, "SPELL_AURA_REFRESH", 1003),
                 self.aura(5, "SPELL_AURA_REFRESH", 1002), self.end(30)]
        with mock.patch.object(wle, "MAX_PERF_AURAS", 1):
            result = self.result_of(lines)
        self.assertEqual(result["auras"]["coverage"],
                         {"complete": False, "rejected_keys": 1,
                          "rejected_keys_is_lower_bound": True, "first_rejected_s": 2.0})
        self.assertIn({"code": "perf_aura_keys_truncated", "cap": 1, "dropped": 1},
                      result["warnings"])

    def test_untracked_aura_holder_makes_the_row_partial(self):
        third = "Player-1379-0000000C"
        lines = [self.start(0)]
        for seconds, caster in ((1, PERF_HEALER), (2, PERF_FRIEND), (3, third)):
            lines.append(self.aura(seconds, "SPELL_AURA_APPLIED", 1001, source=caster,
                                   source_name="Caster%d" % seconds))
        for seconds, caster in ((4, third), (5, PERF_HEALER), (6, PERF_FRIEND)):
            lines.append(self.aura(seconds, "SPELL_AURA_REMOVED", 1001, source=caster,
                                   source_name="Caster"))
        lines.append(self.end(30))
        unlimited = self.aura_row(self.result_of(lines), 1001)
        self.assertEqual((unlimited["partial"], unlimited["holders_truncated"],
                          len(unlimited["intervals"])), (False, False, 1))
        with mock.patch.object(wle, "MAX_PERF_INSTANCES", 2):
            result = self.result_of(lines)
        aura = self.aura_row(result, 1001)
        # The third caster's removal is not seen: from 3 s on the row is not reliable.
        self.assertEqual((aura["partial"], aura["covered_until_s"], aura["holders_truncated"],
                          aura["intervals"]), (True, 3.0, True, []))
        codes = {warning["code"]: warning for warning in result["warnings"]}
        self.assertEqual(codes["perf_aura_holders_truncated"]["dropped"], 1)
        self.assertNotIn("perf_aura_intervals_truncated", codes)

    def test_pre_context_casts_stay_out_of_timeline_opener_and_signature(self):
        # A fast re-pull: the previous pull's casts and END are in the pre-context.
        result = self.result_of([
            self.start(-9), self.cast(-8, "success", 44425),
            self.cast(-7, "start", 30451), self.cast(-6, "success", 30451),
            self.end(-5, fight_ms=4000),
            self.cast(-1, "start", 365350),          # a real precast
            self.start(0), self.cast(0.5, "success", 365350),
            self.cast(2, "success", 5143), self.end(30)])
        expected = [[-1000, "start", 365350, "261477"], [500, "success", 365350, "261477"],
                    [2000, "success", 5143, "261477"]]
        self.assertEqual(result["timeline"]["entries"], expected)
        self.assertEqual(result["opener"]["entries"], expected)
        self.assertEqual(result["opener"]["signature"], [365350, 5143])
        row = self.spell_row(result["casts"]["by_spell"], 365350)
        self.assertEqual((row["start"], row["success"], row["started_before_pull"]), (0, 1, 1))
        self.assertIsNone(self.spell_row(result["casts"]["by_spell"], 44425))

    def test_retention_caps_keep_streaming_totals(self):
        lines = [self.start(0)]
        for index in range(4):
            lines.append(self.cast(1 + index, "success", 1, power=(0, 100 + index, 1000)))
            lines.append(self.energize(1.5 + index, 321507, 1, 0, 16, current=500 + index))
            target = "Creature-0-3109-3004-27445-261477-000000000%d" % index
            lines.append(self.damage(1.7 + index, 7, 10, dest=target))
        lines.append(self.end(30))
        with mock.patch.object(wle, "MAX_PERF_RESOURCE_SAMPLES", 2), \
                mock.patch.object(wle, "MAX_PERF_ENERGIZE_EVENTS", 2), \
                mock.patch.object(wle, "MAX_PERF_INSTANCES", 2):
            result = self.result_of(lines)
        mana = result["resources"]["by_power_type"]["0"]
        self.assertEqual((mana["samples"], mana["min_observed"], mana["max_observed"],
                          mana["last"]["current"], mana["partial"], len(mana["series"])),
                         (8, 100, 503, 503, True, 2))
        charges = self.spell_row(result["resources"]["energize"], 321507)
        self.assertEqual((charges["events"], charges["amount"]), (4, 4))
        boss = self.spell_row(result["damage"]["by_target"], "261477", key="key")
        self.assertEqual((boss["instances"], boss["hits"], boss["effective"]), (2, 4, 40))
        warnings = {warning["code"]: warning for warning in result["warnings"]}
        self.assertEqual(warnings["perf_resource_samples_truncated"]["dropped"], 6)
        self.assertEqual(warnings["perf_energize_events_truncated"]["dropped"], 2)
        self.assertEqual(warnings["perf_target_instances_truncated"]["dropped"], 2)

    def test_resource_series_is_downsampled_deterministically(self):
        lines = [self.start(0)] + [
            self.cast(1 + index, "success", 1, power=(0, 100 * (index + 1), 1000))
            for index in range(5)] + [self.end(30)]
        with mock.patch.object(wle, "MAX_PERF_RESOURCE_POINTS", 3):
            mana = self.result_of(lines)["resources"]["by_power_type"]["0"]
        self.assertEqual((mana["samples"], mana["partial"]), (5, True))
        self.assertEqual(mana["series"], [[1.0, 100], [3.0, 300], [5.0, 500]])


# --- raid performance diagnostics: Arcane Mage rules (spec 62) -----------------------

class ArcaneRulesTests(ExtractorTestCase):
    """SPEC_RULES[62] through the real AnalysisSession hook; hand-computable sections."""

    # The accumulator fixture builders, reused without re-running that class's tests.
    START = PerformanceAccumulatorTests.START
    at = PerformanceAccumulatorTests.at
    start = PerformanceAccumulatorTests.start
    end = PerformanceAccumulatorTests.end
    damage = PerformanceAccumulatorTests.damage
    cast = PerformanceAccumulatorTests.cast
    aura = PerformanceAccumulatorTests.aura
    energize = PerformanceAccumulatorTests.energize
    died = PerformanceAccumulatorTests.died
    combatant = PerformanceAccumulatorTests.combatant
    run_lines = PerformanceAccumulatorTests.run_lines
    result_of = PerformanceAccumulatorTests.result_of
    build = PerformanceAccumulatorTests.build
    extract = PerformanceAccumulatorTests.extract
    perf_options = PerformanceAccumulatorTests.perf_options

    def head(self):
        return [self.start(0), self.combatant(0, auras=[])]

    def spec_of(self, lines, **kwargs):
        return self.result_of(lines, **kwargs)["spec"]

    def touch_aura(self, seconds, event):
        return self.aura(seconds, event, 210824, dest=PERF_BOSS, dest_name=PERF_BOSS_NAME,
                         dest_flags=PERF_HOSTILE)

    def burst_lines(self):
        return self.head() + [
            self.aura(1, "SPELL_AURA_APPLIED", 263725),
            self.aura(1.5, "SPELL_AURA_APPLIED_DOSE", 263725, stacks=2),
            self.cast(2, "start", 365350),
            # The Surge SUCCESS precedes its buff at the same timestamp: outside.
            self.cast(3, "success", 365350, power=(0, 900, 1000)),
            self.aura(3, "SPELL_AURA_APPLIED", 365362),
            self.cast(4, "success", 321507, power=(0, 850, 1000)),
            self.touch_aura(4, "SPELL_AURA_APPLIED"),
            self.cast(5, "success", 5143, power=(0, 800, 1000)),
            self.damage(5.1, 7268, 100),
            self.damage(5.3, 7268, 150, overkill="50"),
            self.cast(6, "success", 44425, power=(0, 700, 1000)),
            self.damage(6, 44425, 300),
            self.aura(10, "SPELL_AURA_REMOVED", 365362),
            self.cast(11.5, "success", 44425, power=(0, 600, 1000)),
            self.damage(12, 44425, 999),
            self.end(30)]

    def test_registered_for_spec_62(self):
        self.assertIs(wle.SPEC_RULES[62], wle.ARCANE_RULES)
        self.assertEqual((wle.ARCANE_RULES["id"], wle.ARCANE_RULES["version"],
                          wle.ARCANE_RULES["validated_builds"]),
                         ("mage-arcane", 1, ("12.1.0",)))

    def test_burst_window_statistics(self):
        spec = self.spec_of(self.burst_lines())
        self.assertEqual(spec["status"], "applied")
        burst = spec["burst_windows"]
        self.assertEqual((burst["count"], burst["partial"], burst["covered_until_s"]),
                         (1, False, None))
        window = burst["windows"][0]
        self.assertEqual(window, {
            "start_s": 3.0, "end_s": 10.0, "duration_s": 7.0, "start_basis": "observed",
            "end_basis": "observed", "casts": {"5143": 1, "44425": 1, "321507": 1},
            "casts_partial": False, "damage_effective": 500,
            "mana_at_start": {"current": 900, "max": 1000, "sample_age_s": 0.0},
            "mana_at_end": {"current": 600, "max": 1000, "sample_age_s": -1.5},
            "clearcasting_stacks_at_start": 2,
            "touch": {"applied_s": 4.0, "offset_from_start_s": 1.0},
            "death_inside": False})
        self.assertEqual(spec["partial_windows"]["count"], 0)
        self.assertEqual(spec["touch_windows"]["windows"], [{
            "start_s": 4.0, "start_basis": "observed", "end_s": 30.0,
            "end_basis": "encounter_end", "target_key": "261477",
            "casts": {"44425": 2, "5143": 1}, "casts_partial": False,
            "damage_effective_to_target": 1499}])
        self.assertEqual(spec["opener"], {"surge_first_success_s": 3.0,
                                          "touch_first_success_s": 4.0,
                                          "order": "surge_first", "surge_precast": False})
        self.assertEqual(len(spec["limitations"]), len(wle.ARCANE_LIMITATIONS))

    def test_opener_precast_touch_first_and_touch_before_the_window(self):
        spec = self.spec_of([
            self.cast(-1, "start", 365350), self.start(0), self.combatant(0, auras=[]),
            self.cast(0.2, "success", 321507), self.touch_aura(0.2, "SPELL_AURA_APPLIED"),
            self.cast(0.5, "success", 365350), self.aura(0.5, "SPELL_AURA_APPLIED", 365362),
            # A later application, farther from the window start, is not the pair.
            self.touch_aura(12, "SPELL_AURA_APPLIED"), self.end(30)])
        self.assertEqual(spec["opener"], {"surge_first_success_s": 0.5,
                                          "touch_first_success_s": 0.2,
                                          "order": "touch_first", "surge_precast": True})
        self.assertEqual(spec["burst_windows"]["windows"][0]["touch"],
                         {"applied_s": 0.2, "offset_from_start_s": -0.3})
        self.assertIsNone(spec["burst_windows"]["windows"][0]["mana_at_start"])
        without = self.spec_of(self.head() + [self.cast(1, "success", 30451),
                                              self.end(30)])
        self.assertEqual(without["opener"], {"surge_first_success_s": None,
                                             "touch_first_success_s": None,
                                             "order": None, "surge_precast": None})

    def test_unknown_start_goes_to_partial_windows(self):
        result = self.result_of(self.head() + [
            self.cast(1, "success", 30451),
            self.aura(5, "SPELL_AURA_REMOVED", 365362),       # never seen applied
            self.aura(8, "SPELL_AURA_REFRESH", 365362),       # active since unknown
            self.cast(9, "success", 5143),
            self.aura(12, "SPELL_AURA_REMOVED", 365362),
            self.end(30)])
        spec = result["spec"]
        self.assertEqual((spec["burst_windows"]["count"], spec["burst_windows"]["windows"]),
                         (0, []))
        self.assertEqual(spec["partial_windows"]["count"], 2)
        self.assertEqual(spec["partial_windows"]["windows"], [
            {"start_s": None, "start_basis": "unknown", "first_evidence_s": 5.0,
             "end_s": 5.0, "end_basis": "observed"},
            {"start_s": None, "start_basis": "unknown", "first_evidence_s": 8.0,
             "end_s": 12.0, "end_basis": "observed"}])
        aura = PerformanceAccumulatorTests.spell_row(result["auras"]["on_player"], 365362)
        self.assertEqual(aura["unknown_start_intervals"], 2)

    def test_window_established_before_the_pull(self):
        # Applied in the pre-context: start 0, basis pre_context, whether the first
        # in-encounter record is its own removal or another event.
        for first in ([], [self.cast(1, "success", 30451)]):
            spec = self.spec_of([self.aura(-3, "SPELL_AURA_APPLIED", 365362),
                                 self.start(0), self.combatant(0, auras=[])] + first +
                                [self.aura(2, "SPELL_AURA_REMOVED", 365362),
                                 self.cast(3, "success", 30451), self.end(30)])
            window = spec["burst_windows"]["windows"][0]
            self.assertEqual((spec["burst_windows"]["count"], window["start_s"],
                              window["end_s"], window["start_basis"], window["casts"]),
                             (1, 0.0, 2.0, "pre_context", {"30451": 1} if first else {}))
            self.assertEqual(spec["partial_windows"]["count"], 0)

    def test_tracked_keys_seen_before_the_rules_are_upgraded(self):
        # Surge and Touch first seen in the pre-context, before COMBATANT_INFO activates
        # the rules: Touch gains its interval history from the live state, and neither
        # key counts against MAX_PERF_AURAS once reserved.
        lines = [self.aura(-3, "SPELL_AURA_APPLIED", 365362),
                 self.touch_aura(-2, "SPELL_AURA_APPLIED"),
                 self.start(0), self.combatant(0, auras=[]),
                 self.cast(1, "success", 44425, power=(0, 900, 1000)),
                 self.damage(1.2, 44425, 300),
                 self.aura(4, "SPELL_AURA_REMOVED", 365362),
                 self.touch_aura(6, "SPELL_AURA_REMOVED"),
                 self.aura(8, "SPELL_AURA_APPLIED", 1001), self.end(30)]
        with mock.patch.object(wle, "MAX_PERF_AURAS", 2):
            result = self.result_of(lines)
        spec = result["spec"]
        window = spec["burst_windows"]["windows"][0]
        self.assertEqual((spec["burst_windows"]["count"], window["start_s"], window["end_s"],
                          window["start_basis"], window["casts"],
                          window["damage_effective"]),
                         (1, 0.0, 4.0, "pre_context", {"44425": 1}, 300))
        self.assertEqual(spec["touch_windows"]["windows"], [{
            "start_s": 0.0, "start_basis": "pre_context", "end_s": 6.0,
            "end_basis": "observed", "target_key": "261477", "casts": {"44425": 1},
            "casts_partial": False, "damage_effective_to_target": 300}])
        touch = PerformanceAccumulatorTests.spell_row(result["auras"]["from_player"], 210824)
        self.assertEqual(touch["intervals"], [{"start": 0.0, "end": 6.0,
                                               "start_basis": "pre_context",
                                               "end_basis": "observed", "max_stacks": 1}])
        self.assertTrue(result["auras"]["coverage"]["complete"])
        self.assertIsNotNone(PerformanceAccumulatorTests.spell_row(
            result["auras"]["on_player"], 1001))

    def test_windows_beyond_the_cap_are_counted_without_detail(self):
        lines = self.head()
        for start in (1, 3, 5):
            lines += [self.aura(start, "SPELL_AURA_APPLIED", 365362),
                      self.cast(start + 0.5, "success", 30451),
                      self.aura(start + 1, "SPELL_AURA_REMOVED", 365362)]
        lines.append(self.end(30))
        with mock.patch.object(wle, "MAX_PERF_WINDOWS", 2):
            result = self.result_of(lines)
        burst = result["spec"]["burst_windows"]
        self.assertEqual((burst["count"], len(burst["windows"]), burst["partial"],
                          burst["covered_until_s"]), (3, 2, True, 5.0))
        self.assertEqual([window["casts"] for window in burst["windows"]],
                         [{"30451": 1}, {"30451": 1}])
        self.assertIn({"code": "perf_burst_windows_truncated", "cap": 2, "dropped": 1},
                      result["warnings"])
        unlimited = self.spec_of(lines)["burst_windows"]
        self.assertEqual((unlimited["count"], len(unlimited["windows"]),
                          unlimited["partial"]), (3, 3, False))

    def test_clearcasting_stack_arithmetic(self):
        procs = self.spec_of(self.head() + [
            self.cast(0.5, "success", 5143),                             # no decrement
            self.aura(1, "SPELL_AURA_APPLIED", 263725),
            self.aura(2, "SPELL_AURA_APPLIED_DOSE", 263725, stacks=2),
            self.aura(3, "SPELL_AURA_APPLIED_DOSE", 263725, stacks=3),
            self.aura(4, "SPELL_AURA_REFRESH", 263725),                  # at max (3)
            self.cast(5, "success", 5143),
            self.aura(5, "SPELL_AURA_REMOVED_DOSE", 263725, stacks=2),   # Missiles, same ts
            self.aura(6, "SPELL_AURA_REMOVED_DOSE", 263725, stacks=1),   # unexplained
            self.aura(7, "SPELL_AURA_REFRESH", 263725),                  # at 1
            self.aura(8, "SPELL_AURA_REMOVED", 263725),                  # final, from 1
            self.cast(8, "success", 5143),                               # same ts, after
            self.end(30)])["procs"]
        self.assertEqual(procs, [{
            "spell_id": 263725, "applications": 1, "refreshes": 2,
            "refreshes_at_max_stacks": 1, "refreshes_with_unknown_stacks": 0,
            "stack_increments": 2, "decrements": 3,
            "decrements_with_missiles_cast_same_timestamp": 2,
            "decrements_unexplained": 1, "max_stacks_observed": 3}])

    def charges_of(self, lines, **kwargs):
        return self.spec_of(self.head() + lines + [self.end(30)], **kwargs)["charges"]

    def test_charges_self_check_confirms(self):
        charges = self.charges_of([
            self.energize(1, 321507, 1, 0, 16),        # counter unknown: no check
            self.cast(2, "success", 44425),            # unknown -> 0
            self.energize(3, 321507, 2, 0, 16),        # check: 0 -> 2, consistent
            self.energize(4, 153626, 2, 0, 16),        # check: 2 -> 4, consistent
            self.energize(5, 321507, 0, 1, 16),        # check: at 4, amount 0: confirmed
            self.cast(6, "success", 44425),            # 4 -> 0
            self.energize(7, 321507, 1, 0, 16),        # check: 0 -> 1
            self.cast(8, "success", 44425)])           # 1 -> 0
        inferred = charges["inferred"]
        self.assertEqual(inferred["kind"], "inferred")
        self.assertEqual((inferred["checks"], inferred["confirmed"],
                          inferred["contradicted"]), (4, 1, 0))
        self.assertEqual(inferred["agreement_rate"],
                         {"value": 1.0, "numerator": 4, "denominator": 4})
        self.assertEqual(inferred["barrage_casts_by_inferred_charges"],
                         {"0": 0, "1": 1, "2": 0, "3": 0, "4": 1, "unknown": 1})
        self.assertEqual(charges["observed"]["by_spell"], [
            {"spell_id": 321507, "events": 4, "gains": 4, "over_energize": 1},
            {"spell_id": 153626, "events": 1, "gains": 2, "over_energize": 0}])

    def test_charges_self_check_contradicts(self):
        inferred = self.charges_of([
            self.energize(1, 321507, 0, 1, 16),        # unknown, reveals max: 4
            self.energize(2, 321507, 1, 0, 16),        # check: at 4, +1: contradicted
            self.energize(3, 321507, 1, 0, 16),        # unknown again: no check
            self.cast(4, "success", 44425),            # unknown -> 0
            self.energize(5, 321507, 1, 1, 16),        # check: at 0, capped with room 4
            self.energize(6, 321507, 0, 1, 16),        # check: at 4 (implied): confirmed
            self.cast(7, "failed", 44425),             # a failed cast spends nothing
            self.cast(8, "success", 44425)])["inferred"]
        self.assertEqual((inferred["checks"], inferred["confirmed"],
                          inferred["contradicted"]), (3, 1, 2))
        self.assertEqual(inferred["agreement_rate"],
                         {"value": 0.333, "numerator": 1, "denominator": 3})
        self.assertEqual(inferred["barrage_casts_by_inferred_charges"],
                         {"0": 0, "1": 0, "2": 0, "3": 0, "4": 1, "unknown": 1})
        empty = self.charges_of([self.cast(1, "success", 30451)])["inferred"]
        self.assertEqual((empty["checks"], empty["agreement_rate"]["value"]), (0, None))
        self.assertIn("reason", empty["agreement_rate"])

    def test_build_flag(self):
        lines = self.burst_lines()
        reference = self.result_of(lines)
        for build, validated in (("12.1.0", True), ("12.0.7", False), (None, False)):
            context = None if build is None else dict(wle.parse_log_header([]),
                                                      build_version=build)
            result = self.result_of(lines, game_context=context)
            self.assertEqual(result["rules"]["spec"],
                             {"id": "mage-arcane", "version": 1,
                              "validated_for_build": validated})
            spec = result["spec"]
            self.assertEqual((spec["id"], spec["version"],
                              spec["rules_validated_for_build"]),
                             ("mage-arcane", 1, validated))
            # Observed metrics are published whatever the build.
            self.assertEqual(spec["burst_windows"], reference["spec"]["burst_windows"])

    def test_build_flag_from_the_log_header(self):
        builder = LogBuilder()
        builder.add(self.at(-10), "COMBAT_LOG_VERSION", "22", "ADVANCED_LOG_ENABLED", "1",
                    "BUILD_VERSION", "12.1.0", "PROJECT_ID", "1")
        segments = self.extract(self.build(self.burst_lines(), builder),
                                self.perf_options())
        result = segments[0].performance_result()
        self.assertTrue(result["spec"]["rules_validated_for_build"])
        self.assertEqual(result["spec"]["burst_windows"]["windows"][0]["damage_effective"],
                         500)
        wle.performance_json_bytes(result)

    def test_other_spec_or_no_combatant_info_is_not_applied(self):
        args = wle.split_args(perf_combatant()[len("COMBATANT_INFO,"):])
        self.assertEqual(args[24], "62")
        args[24] = "63"
        other = (0, "COMBATANT_INFO", [",".join(args)])
        body = [self.cast(1, "success", 5143), self.damage(2, 7268, 100),
                self.aura(3, "SPELL_AURA_APPLIED", 365362), self.end(30)]
        result = self.result_of([self.start(0), other] + body)
        self.assertEqual(result["character"]["spec_id"], 63)
        self.assertEqual(result["spec"], {"status": "not_applied",
                                          "reason": "no rules registered for spec 63"})
        self.assertIsNone(result["rules"]["spec"])
        self.assertEqual(result["damage"]["player"]["effective"], 100)
        self.assertEqual(result["casts"]["total_success"], 1)
        absent = self.result_of([self.start(0)] + body)
        self.assertEqual(absent["spec"], {"status": "not_applied", "reason": "spec unknown"})
        # The general metrics do not depend on the rules (only the Touch debuff row in
        # auras.from_player gains its interval list, by contract).
        with_rules = self.result_of(self.burst_lines())
        without_rules = self.result_of(self.burst_lines(), spec_rules={})
        for key in ("character", "life", "damage", "casts", "resources", "timeline",
                    "opener", "spell_names", "warnings"):
            self.assertEqual(with_rules[key], without_rules[key], key)
        self.assertEqual(with_rules["auras"]["on_player"],
                         without_rules["auras"]["on_player"])

    def test_missiles_ticks_are_actions(self):
        lines = self.head() + [self.cast(1, "success", 5143)] + [
            self.damage(1 + 0.15 * tick, 7268, 10) for tick in range(1, 21)] + [
            self.cast(4.5, "success", 44425), self.end(30)]
        continuity = self.result_of(lines)["continuity"]
        self.assertEqual((continuity["gaps_count"], continuity["gaps_total_s"]), (0, 0.0))
        unruled = self.result_of(lines, spec_rules={})["continuity"]
        self.assertEqual((unruled["gaps_count"], unruled["gap_max_s"]), (1, 3.5))

    def test_death_inside_a_window(self):
        spec = self.spec_of(self.head() + [
            self.aura(2, "SPELL_AURA_APPLIED", 365362), self.cast(3, "success", 30451),
            self.died(4), self.aura(6, "SPELL_AURA_REMOVED", 365362),
            self.aura(8, "SPELL_AURA_APPLIED", 365362), self.end(30)])
        windows = spec["burst_windows"]["windows"]
        self.assertEqual([(window["start_s"], window["end_s"], window["end_basis"],
                           window["death_inside"], window["casts"],
                           window["clearcasting_stacks_at_start"]) for window in windows],
                         [(2.0, 6.0, "observed", True, {"30451": 1}, None),
                          (8.0, 30.0, "encounter_end", False, {}, None)])

    def test_spec_state_is_seeded_whatever_event_comes_first(self):
        # Clearcasting at 3 stacks in the snapshot: its own decrement as the first
        # record must not hide the 3 (the refresh at 2 is then not at the maximum).
        clearcasting = [self.aura(1, "SPELL_AURA_REMOVED_DOSE", 263725, stacks=2),
                        self.aura(2, "SPELL_AURA_REFRESH", 263725), self.end(30)]
        # Surge established before the pull, removed by the first record: the window
        # still enters with the Clearcasting stacks of the snapshot.
        surge = [self.aura(2, "SPELL_AURA_REMOVED", 365362), self.end(30)]
        for first in ([], [self.cast(0.5, "success", 30451)]):
            result = self.result_of([self.start(0),
                                     self.combatant(0, auras=[(DKYAM, 263725, 3)])] +
                                    first + clearcasting)
            procs = result["spec"]["procs"][0]
            self.assertEqual(PerformanceAccumulatorTests.spell_row(
                result["auras"]["on_player"], 263725)["max_stacks"], 3)
            self.assertEqual((procs["max_stacks_observed"], procs["refreshes_at_max_stacks"],
                              procs["decrements"]), (3, 0, 1), first)
            spec = self.spec_of([self.aura(-3, "SPELL_AURA_APPLIED", 365362), self.start(0),
                                 self.combatant(0, auras=[(DKYAM, 263725, 2)])] +
                                first + surge)
            window = spec["burst_windows"]["windows"][0]
            self.assertEqual((spec["burst_windows"]["count"], window["start_basis"],
                              window["end_s"], window["clearcasting_stacks_at_start"]),
                             (1, "pre_context", 2.0, 2), first)

    def test_spec_auras_are_reserved_before_the_spec_is_known(self):
        # An ordinary aura fills the allowance in the pre-context, then Surge is applied;
        # the spec is learnt afterwards and the snapshot omits Surge.
        lines = [self.aura(-5, "SPELL_AURA_APPLIED", 1001),
                 self.aura(-3, "SPELL_AURA_APPLIED", 365362),
                 self.start(0), self.combatant(0, auras=[]),
                 self.aura(10, "SPELL_AURA_REMOVED", 365362), self.end(30)]
        with mock.patch.object(wle, "MAX_PERF_AURAS", 1):
            result = self.result_of(lines)
        spec = result["spec"]
        self.assertEqual((spec["burst_windows"]["count"], spec["partial_windows"]["count"]),
                         (1, 0))
        window = spec["burst_windows"]["windows"][0]
        self.assertEqual((window["start_s"], window["end_s"], window["start_basis"]),
                         (0.0, 10.0, "pre_context"))
        self.assertTrue(result["auras"]["coverage"]["complete"])
        # A target aura reserved for another spec's rules publishes no interval list.
        args = wle.split_args(perf_combatant()[len("COMBATANT_INFO,"):])
        args[24] = "63"
        touch = [self.aura(2, "SPELL_AURA_APPLIED", 210824, dest=PERF_BOSS,
                           dest_name=PERF_BOSS_NAME, dest_flags=PERF_HOSTILE), self.end(30)]
        for head in ([self.start(0), (0, "COMBATANT_INFO", [",".join(args)])],
                     [self.start(0)]):
            row = PerformanceAccumulatorTests.spell_row(
                self.result_of(head + touch)["auras"]["from_player"], 210824)
            self.assertEqual(row["applications"], 1)
            self.assertNotIn("intervals", row)

    def test_window_casts_past_the_cap_go_to_other(self):
        lines = self.head() + [self.aura(1, "SPELL_AURA_APPLIED", 365362)] + \
            [self.cast(2 + 0.1 * index, "success", 1000 + index) for index in range(5)] + \
            [self.cast(3, "success", 1000), self.aura(5, "SPELL_AURA_REMOVED", 365362),
             self.end(30)]
        with mock.patch.object(wle, "MAX_PERF_WINDOW_SPELLS", 3):
            result = self.result_of(lines)
        burst = result["spec"]["burst_windows"]
        window = burst["windows"][0]
        self.assertEqual(list(window["casts"].items()),
                         [("1000", 2), ("1001", 1), ("1002", 1), ("other", 2)])
        self.assertEqual((window["casts_partial"], burst["casts_partial"], burst["partial"]),
                         (True, True, False))
        self.assertIn({"code": "perf_window_spells_truncated", "cap": 3, "dropped": 2},
                      result["warnings"])
        self.assertFalse(self.spec_of(lines)["burst_windows"]["casts_partial"])


# --- raid performance diagnostics: publication of performance.json ----------------------

class PerformancePublicationTests(ExtractorTestCase):
    """performance.json shares the analysis folder and marker with every other profile."""

    START = datetime(2026, 10, 1, 21, 40, 0)
    # The analysis marker exactly as published before performance.json existed.
    PLAIN_MARKER_KEYS = {"analysis_schema_version", "segment_id", "profile", "options",
                         "artifacts", "warnings", "full_uncompressed_bytes",
                         "full_stored_bytes", "combat_uncompressed_bytes",
                         "combat_stored_bytes", "analysis_bundle_bytes",
                         "analysis_zip_bytes", "reduction_percent"}
    PAYLOAD = ("combat.txt", "summary.json", "players.json", "deaths.json")

    def at(self, seconds):
        return self.START + timedelta(seconds=seconds)

    @staticmethod
    def plain(**overrides):
        return wle.OutputOptions(analysis_only=True, **overrides)

    @staticmethod
    def perf(player="Dkyam", **overrides):
        return wle.OutputOptions(analysis_only=True, performance_player=player, **overrides)

    def add_pull(self, builder, end=True):
        builder.add(self.at(-10), "COMBAT_LOG_VERSION", "22", "ADVANCED_LOG_ENABLED", "1",
                    "BUILD_VERSION", "12.1.0", "PROJECT_ID", "1")
        builder.add(self.at(0), "ENCOUNTER_START", "3421", q(PERF_BOSS_NAME), "15", "20",
                    "2900")
        builder.add(self.at(1), "COMBATANT_INFO", perf_combatant()[len("COMBATANT_INFO,"):])
        builder.add(self.at(2), "SPELL_CAST_SUCCESS",
                    *perf_header(DKYAM, PERF_NAME, PERF_PLAYER_FLAGS, PERF_BOSS,
                                 PERF_BOSS_NAME, PERF_HOSTILE),
                    "5143", q("Misiles Arcanos"), "0x40", *perf_block(DKYAM, 0, 900, 1000))
        builder.add(self.at(3), "SPELL_DAMAGE",
                    *perf_header(DKYAM, PERF_NAME, PERF_PLAYER_FLAGS, PERF_BOSS,
                                 PERF_BOSS_NAME, PERF_HOSTILE),
                    "7268", q("Misiles Arcanos"), "0x40",
                    *perf_block(PERF_BOSS, max_hp=5000000),
                    "400", "400", "-1", "64", "0", "0", "0", "nil", "nil", "nil", "ST")
        builder.add(self.at(4), "SPELL_HEAL",
                    *perf_header(PERF_HEALER, PERF_HEALER_NAME, PERF_PLAYER_FLAGS, DKYAM,
                                 PERF_NAME, PERF_PLAYER_FLAGS),
                    "2061", q("Flash Heal"), "0x2", *perf_block(DKYAM, 0, 800, 1000),
                    "100", "100", "0", "0", "nil")
        if end:
            builder.add(self.at(30), "ENCOUNTER_END", "3421", q(PERF_BOSS_NAME), "15", "20",
                        "1", "30000")
        return builder

    def write_pull(self):
        return self.write_log(self.add_pull(LogBuilder()).data())

    def run_profile(self, options, expected):
        extractor = self.make_extractor(options)
        extractor.prepare()
        with mock.patch.object(wle.SegmentPublisher, "publish", autospec=True,
                               side_effect=wle.SegmentPublisher.publish) as publish:
            self.assertEqual(extractor.run_once(), expected, options.profile)
        return [call.args[1] for call in publish.call_args_list]

    def package_dirs(self, directory=None):
        directory = directory or self.raids_dir()
        return [name for name in self.list_outputs(directory)
                if os.path.isdir(os.path.join(directory, name))]

    def analysis_dir(self, directory=None):
        directory = directory or self.raids_dir()
        names = self.package_dirs(directory)
        self.assertEqual(len(names), 1, names)
        return os.path.join(directory, names[0], "analysis")

    def marker_path(self):
        return os.path.join(self.analysis_dir(), "metadata.json")

    def performance_path(self):
        return os.path.join(self.analysis_dir(), "performance.json")

    @staticmethod
    def read_bytes(path):
        with open(path, "rb") as handle:
            return handle.read()

    def snapshot(self, directory):
        files = {}
        for root, _, names in os.walk(directory):
            for name in names:
                path = os.path.join(root, name)
                files[os.path.relpath(path, directory)] = (
                    os.stat(path).st_mtime_ns, self.read_bytes(path))
        return files

    # -- content ------------------------------------------------------------------------
    def test_performance_json_is_published_and_listed_in_the_marker(self):
        self.write_pull()
        options = self.perf()
        segments = self.run_profile(options, (0, 1, 0))
        analysis_dir = self.analysis_dir()
        name = os.path.basename(os.path.dirname(analysis_dir))
        self.assertEqual(sorted(self.list_outputs(analysis_dir)),
                         ["combat.txt", "deaths.json", "metadata.json", "performance.json",
                          "players.json", "summary.json"])
        published = self.read_bytes(self.performance_path())
        result = segments[0].performance_result()
        self.assertEqual(published, wle.performance_json_bytes(result))
        self.assertEqual(result["player"]["status"], "resolved")
        self.assertTrue(result["segment"]["complete"])
        marker = self.read_json(self.marker_path())
        self.assertEqual(set(marker), self.PLAIN_MARKER_KEYS |
                         {"performance", "performance_bytes"})
        self.assertIn(name + "/analysis/performance.json", marker["artifacts"])
        self.assertEqual(marker["performance"], {
            "fingerprint": options.performance_fingerprint,
            "schema_version": wle.PERFORMANCE_SCHEMA_VERSION,
            "rules_version": wle.PERFORMANCE_RULES_VERSION,
            "player": "Dkyam", "player_status": "resolved", "player_guid": DKYAM})
        self.assertEqual(marker["performance_bytes"], len(published))
        # The bundle total keeps its definition: combat + summary + deaths + players.
        self.assertEqual(marker["analysis_bundle_bytes"], sum(
            os.path.getsize(os.path.join(analysis_dir, item)) for item in self.PAYLOAD))
        before = self.snapshot(self.raids_dir())
        self.run_profile(options, (0, 0, 0))
        self.assertEqual(self.snapshot(self.raids_dir()), before)

    def test_bundle_zip_carries_performance_json(self):
        self.write_pull()
        options = self.perf(bundle=True)
        self.run_profile(options, (0, 1, 0))
        analysis_dir = self.analysis_dir()
        name = os.path.basename(os.path.dirname(analysis_dir))
        zip_path = os.path.join(self.raids_dir(), name + "_analysis.zip")
        with zipfile.ZipFile(zip_path) as archive:
            self.assertIn("performance.json", archive.namelist())
            self.assertEqual(archive.read("performance.json"),
                             self.read_bytes(self.performance_path()))
            embedded = json.loads(archive.read("metadata.json"))
        self.assertIn(name + "/analysis/performance.json", embedded["artifacts"])
        self.assertEqual(embedded["performance"],
                         self.read_json(self.marker_path())["performance"])
        before = self.snapshot(self.raids_dir())
        self.run_profile(options, (0, 0, 0))
        self.assertEqual(self.snapshot(self.raids_dir()), before)

    def test_mythic_plus_with_the_flag_has_no_performance_output(self):
        builder = LogBuilder()
        start = datetime(2026, 8, 30, 10, 25, 24)
        builder.add(start, "CHALLENGE_MODE_START", q("Valle Cegador"), "2859", "584", "10",
                    "[158,9,10]")
        builder.add(start + timedelta(seconds=2), "SPELL_CAST_SUCCESS",
                    *perf_header(DKYAM, PERF_NAME, PERF_PLAYER_FLAGS, PERF_BOSS,
                                 PERF_BOSS_NAME, PERF_HOSTILE),
                    "5143", q("Misiles Arcanos"), "0x40", *perf_block(DKYAM, 0, 900, 1000))
        builder.add(start + timedelta(seconds=60), "CHALLENGE_MODE_END", "2859", "1", "10",
                    "60000", "301.663300", "2205.470703")
        self.write_log(builder.data())
        segments = self.run_profile(self.perf(), (1, 0, 0))
        self.assertIsNone(segments[0].performance_result())
        analysis_dir = self.analysis_dir(self.mplus_dir())
        self.assertNotIn("performance.json", self.list_outputs(analysis_dir))
        marker = self.read_json(os.path.join(analysis_dir, "metadata.json"))
        self.assertEqual(set(marker), self.PLAIN_MARKER_KEYS)
        self.assertFalse(any("performance" in item for item in marker["artifacts"]))

    # -- the flag only adds -------------------------------------------------------------
    def run_into(self, subdir, options):
        """One run over the shared log into its own output tree; returns the tree."""
        output_dir = os.path.join(self.root, subdir)
        extractor = wle.Extractor(self.log_dir, output_dir, verbose=False,
                                  state_path=os.path.join(output_dir, wle.STATE_FILENAME),
                                  output_options=options)
        extractor.prepare()
        self.assertEqual(extractor.run_once(), (0, 1, 0), subdir)
        return output_dir

    def test_without_the_flag_outputs_are_unchanged_and_the_flag_only_adds(self):
        builder = self.add_pull(LogBuilder())
        # The source byte range of the pull: from the header (exactly CONTEXT_SECONDS
        # before ENCOUNTER_START) to ENCOUNTER_END; a later line is outside every body.
        source_range = builder.data()
        builder.add(self.at(30 + wle.CONTEXT_SECONDS + 20), "SPELL_CAST_SUCCESS",
                    *perf_header(DKYAM, PERF_NAME, PERF_PLAYER_FLAGS, PERF_BOSS,
                                 PERF_BOSS_NAME, PERF_HOSTILE),
                    "5143", q("Misiles Arcanos"), "0x40", *perf_block(DKYAM, 0, 900, 1000))
        self.write_log(builder.data())
        legacy_keys = {"offset", "size", "mtime", "head_hash", "tail_hash"}
        bodies = {}
        for subdir, options in (
                ("full", None),
                ("analysis", wle.OutputOptions(analysis=True)),
                ("analysis_gzip", wle.OutputOptions(analysis=True, gzip=True)),
                ("analysis_perf", wle.OutputOptions(analysis=True,
                                                    performance_player="Dkyam")),
                ("only", self.plain()), ("only_perf", self.perf())):
            output_dir = self.run_into(subdir, options)
            raids = os.path.join(output_dir, wle.RAID_DIR_NAME)
            body = [item for item in self.list_outputs(raids)
                    if item.endswith((".txt", ".txt.gz"))]
            if body:
                data = self.read_bytes(os.path.join(raids, body[0]))
                bodies[subdir] = gzip.decompress(data) if body[0].endswith(".gz") else data
            state = self.read_json(os.path.join(output_dir, wle.STATE_FILENAME))
            profile = (options or wle.OutputOptions()).profile
            entry = state["files"][LOG_NAME]["profiles"][profile]
            if options is None or options.performance_player is None:
                self.assertEqual(set(entry), legacy_keys, subdir)
                if options is None:
                    continue
                analysis_dir = self.analysis_dir(raids)
                marker = self.read_json(os.path.join(analysis_dir, "metadata.json"))
                self.assertEqual(set(marker), self.PLAIN_MARKER_KEYS, subdir)
                self.assertNotIn("performance", marker["options"])
                name = os.path.basename(os.path.dirname(analysis_dir))
                combat = "combat.txt.gz" if options.gzip else "combat.txt"
                expected = [name + ".json", name + (".txt.gz" if options.gzip else ".txt")] \
                    if options.wants_full else []
                expected += [name + "/analysis/" + item for item in sorted(
                    (combat, "deaths.json", "players.json", "summary.json"))]
                expected.append(name + "/analysis/metadata.json")
                self.assertEqual(marker["artifacts"], expected, subdir)
                self.assertNotIn("performance.json", self.list_outputs(analysis_dir))
        self.assertEqual(len(set(bodies.values())), 1, sorted(bodies))
        self.assertEqual(len(bodies), 4)
        # full, --analysis, --analysis --gzip (decompressed) and --analysis with the flag:
        # the body is the source byte range, copied byte for byte.
        self.assertEqual(sorted(bodies), ["analysis", "analysis_gzip", "analysis_perf",
                                          "full"])
        self.assertEqual(bodies["full"], source_range)
        # Same log, flag on vs off: identical payload bytes in both containers.
        for plain, flagged in (("analysis", "analysis_perf"), ("only", "only_perf")):
            plain_dir = self.analysis_dir(os.path.join(self.root, plain, wle.RAID_DIR_NAME))
            flagged_dir = self.analysis_dir(os.path.join(self.root, flagged,
                                                         wle.RAID_DIR_NAME))
            for item in self.PAYLOAD:
                self.assertEqual(self.read_bytes(os.path.join(plain_dir, item)),
                                 self.read_bytes(os.path.join(flagged_dir, item)),
                                 (plain, item))
            self.assertIn("performance.json", self.list_outputs(flagged_dir))

    # -- profile matrix (CLAUDE.md) -----------------------------------------------------
    def crash_on(self, point):
        if point == "marker":
            real_atomic = wle._atomic_write_bytes

            def fail_marker(path, data):
                if path.endswith(os.path.join("analysis", "metadata.json")):
                    raise OSError("simulated crash: marker")
                return real_atomic(path, data)
            return mock.patch.object(wle, "_atomic_write_bytes", side_effect=fail_marker)
        real_copy = wle._copy_atomic

        def fail_copy(source, destination):
            if destination.endswith(point):
                raise OSError("simulated crash: %s copy" % point)
            return real_copy(source, destination)
        return mock.patch.object(wle, "_copy_atomic", side_effect=fail_copy)

    @staticmethod
    def rules_bump():
        return mock.patch.object(wle, "PERFORMANCE_RULES_VERSION",
                                 wle.PERFORMANCE_RULES_VERSION + 1)

    def published_state(self):
        """(marker fingerprint or None, marker profile, performance.json bytes or None)."""
        marker = self.read_json(self.marker_path())
        path = self.performance_path()
        data = self.read_bytes(path) if os.path.exists(path) else None
        return marker.get("performance", {}).get("fingerprint"), marker["profile"], data

    def test_profile_matrix_crash_and_return_to_the_previous_profile(self):
        no_patch = contextlib.nullcontext()
        player_a, player_b, plain = self.perf("Dkyam"), self.perf(PERF_HEALER_NAME), \
            self.plain()
        transitions = [
            ("A->none", (player_a, no_patch), (plain, no_patch)),
            ("none->A", (plain, no_patch), (player_a, no_patch)),
            ("A->B", (player_a, no_patch), (player_b, no_patch)),
            ("B->A", (player_b, no_patch), (player_a, no_patch)),
            ("rules v1->v2", (player_a, no_patch), (player_a, self.rules_bump())),
            ("rules v2->v1", (player_a, self.rules_bump()), (player_a, no_patch)),
        ]
        self.write_pull()
        scenarios = 0
        for label, (options1, patch1), (options2, patch2) in transitions:
            points = ["deaths.json", "marker"]
            with patch2:
                if options2.performance_player is not None:
                    points.insert(0, "performance.json")
            for point in points:
                with self.subTest(transition=label, crash=point):
                    scenarios += 1
                    self.output_dir = os.path.join(self.root, "matrix%d" % scenarios)
                    self.state_path = os.path.join(self.output_dir, wle.STATE_FILENAME)
                    with patch1:
                        profile1 = options1.profile
                        self.run_profile(options1, (0, 1, 0))
                        first = self.published_state()
                    with patch2:
                        profile2 = options2.profile
                        self.assertNotEqual(profile1, profile2)
                        with self.crash_on(point):
                            self.run_profile(options2, (0, 0, 1))
                    analysis_dir = self.analysis_dir()
                    leftovers = self.list_outputs(analysis_dir)
                    self.assertNotIn("metadata.json", leftovers)
                    if options2.performance_player is None:
                        # Retired together with the old marker.
                        self.assertNotIn("performance.json", leftovers)
                    with patch1:
                        self.run_profile(options1, (0, 1, 0))
                        repaired = self.published_state()
                        self.assertEqual(repaired, first)
                        self.assertEqual(repaired[1], profile1)
                        if options1.performance_player is None:
                            self.assertIsNone(repaired[0])
                            self.assertNotIn("performance.json",
                                             self.list_outputs(analysis_dir))
                        else:
                            self.assertEqual(repaired[0], options1.performance_fingerprint)
                            self.assertEqual(json.loads(repaired[2])["player"]["selector"],
                                             options1.performance_player)
                        self.run_profile(options1, (0, 0, 0))
                    self.assertEqual(len(self.package_dirs()), 1)
                    state = self.read_json(self.state_path)
                    self.assertEqual(list(state["files"][LOG_NAME]["profiles"]), [profile1])
        self.assertEqual(scenarios, 17)

    def test_clean_switches_remove_and_restore_performance_json(self):
        self.write_pull()
        self.run_profile(self.perf(), (0, 1, 0))
        first = self.read_bytes(self.performance_path())
        self.run_profile(self.plain(), (0, 1, 0))
        self.assertEqual(sorted(self.list_outputs(self.analysis_dir())),
                         ["combat.txt", "deaths.json", "metadata.json", "players.json",
                          "summary.json"])
        self.assertEqual(set(self.read_json(self.marker_path())), self.PLAIN_MARKER_KEYS)
        self.run_profile(self.perf(), (0, 1, 0))
        self.assertEqual(self.read_bytes(self.performance_path()), first)
        self.assertIn("performance", self.read_json(self.marker_path()))
        self.assertEqual(len(self.package_dirs()), 1)

    # -- recovery helpers need no change for the extra file -------------------------------
    def test_cleanup_partials_removes_a_performance_temp_and_keeps_the_package(self):
        self.write_pull()
        self.run_profile(self.perf(), (0, 1, 0))
        analysis_dir = self.analysis_dir()
        stray = os.path.join(analysis_dir, ".performance.json.abc123.tmp")
        with open(stray, "wb") as handle:
            handle.write(b"half")
        publisher = wle.SegmentPublisher(self.output_dir, verbose=False,
                                         output_options=self.perf())
        self.assertEqual(publisher.cleanup_partials(), 1)
        self.assertEqual(sorted(self.list_outputs(analysis_dir)),
                         ["combat.txt", "deaths.json", "metadata.json", "performance.json",
                          "players.json", "summary.json"])

    def test_incomplete_pull_completed_later_leaves_one_package(self):
        builder = self.add_pull(LogBuilder(), end=False)
        path = self.write_log(builder.data())
        stale = time.time() - (wle.STALE_SECONDS + 60)
        os.utime(path, (stale, stale))
        options = self.perf()
        self.run_profile(options, (0, 1, 0))
        self.assertTrue(self.package_dirs()[0].endswith("_INCOMPLETE"))
        incomplete = json.loads(self.read_bytes(self.performance_path()))
        self.assertFalse(incomplete["segment"]["complete"])
        self.write_log(self.add_pull(LogBuilder()).data())
        extractor = self.make_extractor(options)
        extractor.prepare(reset_state=True)
        self.assertEqual(extractor.run_once(), (0, 1, 0))
        self.assertEqual(len(self.package_dirs()), 1, self.package_dirs())
        self.assertTrue(self.package_dirs()[0].endswith("_Kill"))
        complete = json.loads(self.read_bytes(self.performance_path()))
        self.assertEqual((complete["segment"]["complete"], complete["segment"]["result"],
                          complete["segment"]["duration_ms"]), (True, "kill", 30000))
        marker = self.read_json(self.marker_path())
        self.assertEqual(marker["performance_bytes"],
                         os.path.getsize(self.performance_path()))


# --- raid performance diagnostics: common windows and the session packet -------------

class PerformanceWindowsTests(ExtractorTestCase):
    """damage.windows: streaming totals in [0, N] s per COMMON_WINDOWS value."""

    START = PerformanceAccumulatorTests.START
    at = PerformanceAccumulatorTests.at
    start = PerformanceAccumulatorTests.start
    end = PerformanceAccumulatorTests.end
    damage = PerformanceAccumulatorTests.damage
    cast = PerformanceAccumulatorTests.cast
    died = PerformanceAccumulatorTests.died
    summon = PerformanceAccumulatorTests.summon
    heal_on_player = PerformanceAccumulatorTests.heal_on_player
    run_lines = PerformanceAccumulatorTests.run_lines
    result_of = PerformanceAccumulatorTests.result_of

    def windows(self, lines):
        rows = self.result_of(lines)["damage"]["windows"]
        self.assertEqual([row["seconds"] for row in rows], list(wle.COMMON_WINDOWS))
        return {row["seconds"]: row for row in rows}

    def test_player_and_pet_damage_and_casts_up_to_n_seconds(self):
        windows = self.windows([
            self.damage(-2, 300, 999), self.start(0), self.summon(1, PERF_PET),
            self.cast(5, "success", 100), self.damage(10, 300, 100),
            self.damage(20, 400, 50, source=PERF_PET, source_name="Fénix",
                        source_flags="0x2111"),
            self.damage(30, 300, 7, overkill="2"),    # at exactly N: inside, effective 5
            self.damage(45, 300, 200), self.cast(60, "success", 100),
            self.damage(100, 300, 400), self.cast(121, "success", 100),
            self.end(150), self.damage(151, 300, 999)])
        self.assertEqual(windows, {
            30: {"seconds": 30, "effective": 155, "casts_success": 1, "covered": True},
            60: {"seconds": 60, "effective": 355, "casts_success": 2, "covered": True},
            120: {"seconds": 120, "effective": 755, "casts_success": 2, "covered": True}})

    def test_short_pull_death_and_unknown_life_are_not_covered(self):
        short = self.windows([self.start(0), self.cast(1, "success", 100),
                              self.damage(10, 300, 100), self.end(50)])
        self.assertEqual([short[n]["covered"] for n in (30, 60, 120)], [True, False, False])
        # The totals are kept even where the row is not comparable.
        self.assertEqual([short[n]["effective"] for n in (30, 60, 120)], [100, 100, 100])
        dead = self.windows([self.start(0), self.cast(1, "success", 100),
                             self.damage(10, 300, 100), self.died(40),
                             self.damage(45, 300, 50), self.end(200)])
        self.assertEqual([dead[n]["covered"] for n in (30, 60, 120)], [True, False, False])
        self.assertEqual(dead[60]["effective"], 150)
        at_n = self.windows([self.start(0), self.cast(1, "success", 100), self.died(60),
                             self.end(200)])
        self.assertEqual([at_n[n]["covered"] for n in (30, 60, 120)], [True, True, False])
        unknown = self.windows([self.start(0), self.heal_on_player(5), self.end(200)])
        self.assertEqual([unknown[n]["covered"] for n in (30, 60, 120)],
                         [False, False, False])


ALT_GUID = "Player-1379-0000000B"
ALT_NAME = "Dkyam-Otro-EU"
STATS_KEYS = ("n", "min", "q1", "median", "q3", "max")


def variant_combatant(talents=False, equipment=False, guid=DKYAM):
    text = perf_combatant(guid, auras=[])
    if talents:
        text = text.replace("(62085,80141,1)", "(62085,80141,2)", 1)
    if equipment:
        text = text.replace("(271564,318,", "(271564,330,", 1)
    return (0, "COMBATANT_INFO", [text[len("COMBATANT_INFO,"):]])


class DiagnosticPacketTests(ExtractorTestCase):
    """diagnostic_packet.json v1 over results built by the real accumulator."""

    T0 = datetime(2026, 10, 1, 21, 40, 0)
    start = PerformanceAccumulatorTests.start
    end = PerformanceAccumulatorTests.end
    damage = PerformanceAccumulatorTests.damage
    cast = PerformanceAccumulatorTests.cast
    aura = PerformanceAccumulatorTests.aura
    died = PerformanceAccumulatorTests.died
    heal_on_player = PerformanceAccumulatorTests.heal_on_player
    energize = PerformanceAccumulatorTests.energize

    def result(self, start, lines, file=LOG_NAME, encounter_id=3421, name=None):
        """One published-shape result: performance.json through a JSON round trip."""
        accumulator = wle.PerformanceAccumulator("Dkyam", encounter_id, start)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        session = wle.AnalysisSession(temp.name, wle.KIND_RAID, performance=accumulator)
        end_ms = success = None
        for offset, (seconds, event, fields) in enumerate(lines):
            when = start + timedelta(seconds=seconds)
            raw = line_bytes(when, event, *fields)
            _, parsed_event, args = wle.parse_line(raw.decode("utf-8").rstrip("\r\n"), 2026)
            session.consume(raw, when, parsed_event, args, offset)
            if event == "ENCOUNTER_END" and end_ms is None:
                end_ms, success = int(fields[5]), fields[4] == "1"
        session.close_streams()
        metadata = {
            "segment_id": "raid|%s|%s|%d" % (file, wle.format_timestamp(start), encounter_id),
            "source_file": file, "encounter_id": encounter_id,
            "boss": "Boss %d" % encounter_id, "difficulty_id": 15, "difficulty": "Heroic",
            "raid_size": 20, "start_time": wle.format_timestamp(start),
            "end_time": None if end_ms is None else
            wle.format_timestamp(start + timedelta(milliseconds=end_ms)),
            "complete": end_ms is not None, "success": success, "duration_ms": end_ms}
        game = {"combat_log_version": 22, "advanced_logging": True,
                "build_version": "12.1.0", "project_id": 1, "header_source": "stream"}
        performance = accumulator.result(metadata, game, "fp", segment_start_offset=0)
        performance = json.loads(wle.performance_json_bytes(performance))
        return {"name": name or "%s_%s" % (start.strftime("%Y-%m-%d_%H-%M-%S"), file),
                "performance": performance}

    def pull(self, minutes, duration, damage=10000, success=False, deaths=(),
             encounter=3421, file=LOG_NAME, combatant=None, extra=(), observe_until=None,
             base=None, name=None):
        """A pull of the selected player: cast at 1 s, `damage` at 10 s, `extra` lines."""
        start = (base or self.T0) + timedelta(minutes=minutes)
        lines = [self.cast(1, "success", 5143), self.damage(10, 7268, damage)] + \
            [self.died(t) for t in deaths] + list(extra)
        if observe_until is not None:
            lines.append(self.heal_on_player(observe_until))
        lines.sort(key=lambda line: line[0])
        lines = [self.start(0, str(encounter)), combatant or variant_combatant()] + lines
        if duration is not None:
            lines.append(self.end(duration, str(encounter), "1" if success else "0"))
        return self.result(start, lines, file=file, encounter_id=encounter, name=name)

    def burst(self, at=3):
        """Arcane Surge buff for 7 s with one Touch of the Magi cast inside."""
        return [self.aura(at, "SPELL_AURA_APPLIED", 365362),
                self.cast(at + 1, "success", 321507),
                self.aura(at + 7, "SPELL_AURA_REMOVED", 365362)]

    def packets(self, results, gap=120, max_bytes=200000, skipped=()):
        out = []
        for session in wle.build_sessions(results, gap):
            packet, data = wle.build_packet(session, max_bytes, skipped)
            # Strict JSON: no NaN/Infinity, and the bytes parse back to the packet.
            self.assertEqual(json.loads(data, parse_constant=self.fail), packet)
            self.assertEqual(packet["budget"]["actual_bytes"], len(data))
            out.append((packet, data))
        return out

    def packet(self, results, **kwargs):
        packets = self.packets(results, **kwargs)
        self.assertEqual(len(packets), 1)
        return packets[0][0]

    @staticmethod
    def stats(row):
        return {key: row[key] for key in STATS_KEYS}

    # -- aggregates ---------------------------------------------------------------------
    def test_stats_quartiles_and_empty_sample(self):
        self.assertEqual(wle.packet_stats([4, 1, 3, 2]), {
            "n": 4, "min": 1, "q1": 1.75, "median": 2.5, "q3": 3.25, "max": 4})
        self.assertEqual(wle.packet_stats([7]), {
            "n": 1, "min": 7, "q1": 7, "median": 7, "q3": 7, "max": 7})
        self.assertEqual(wle.packet_stats([]), {
            "n": 0, "min": None, "q1": None, "median": None, "q3": None, "max": None})

    def test_pooled_rate_differs_from_the_median_and_incomplete_pulls_are_excluded(self):
        packet = self.packet([
            self.pull(0, 100, damage=10000), self.pull(5, 25, damage=6000),
            self.pull(10, 200, damage=10000, success=True),
            self.pull(15, None, damage=4000, observe_until=40)])
        group, = packet["groups"]
        self.assertEqual((group["attempts"], group["kills"], group["wipes"],
                          group["incomplete"]), (4, 1, 2, 1))
        dps = group["metrics"]["dps_encounter"]
        # Per-pull rates 100, (240) and 50: the 25 s pull is shorter than
        # MIN_COMPARABLE_SECONDS, so the median is 75; pooled 26000 / 325 s = 80 keeps it.
        self.assertEqual(self.stats(dps), {"n": 2, "min": 50.0, "q1": 62.5,
                                           "median": 75.0, "q3": 87.5, "max": 100.0})
        self.assertEqual(dps["excluded"], [
            {"pull_id": "p02", "reason": "pull_shorter_than_min_comparable"},
            {"pull_id": "p04", "reason": "incomplete"}])
        self.assertEqual(group["pooled"]["dps_encounter_pooled"], {
            "value": 80.0, "numerator": 26000, "denominator": 325.0, "n": 3,
            "pulls": ["p01", "p02", "p03"]})
        self.assertEqual(self.stats(group["duration_s"]), {
            "n": 3, "min": 25.0, "q1": 62.5, "median": 100.0, "q3": 150.0, "max": 200.0})
        window30, window60, window120 = group["common_windows"]
        self.assertEqual((window30["seconds"], window30["n"], window30["pulls"]),
                         (30, 2, ["p01", "p03"]))
        self.assertEqual(window30["excluded"], [
            {"pull_id": "p02", "reason": "pull_shorter_than_window"},
            {"pull_id": "p04", "reason": "incomplete"}])
        self.assertEqual(self.stats(window30["effective"]), {
            "n": 2, "min": 10000, "q1": 10000, "median": 10000, "q3": 10000, "max": 10000})
        self.assertEqual(window120["pulls"], ["p03"])
        self.assertIn({"pull_id": "p01", "reason": "pull_shorter_than_window"},
                      window120["excluded"])
        incomplete = packet["pulls"][3]
        self.assertEqual((incomplete["result"], incomplete["complete"],
                          incomplete["duration_basis"], incomplete["dps_encounter"],
                          incomplete["dps_observed"]),
                         ("incomplete", False, "observation_end", None, 100.0))
        self.assertEqual(group["opener_signatures"]["denominator"], 3)
        self.assertEqual(group["opener_signatures"]["signatures"],
                         [{"signature": [5143], "count": 3, "pulls": ["p01", "p02", "p03"]}])
        self.assertEqual(group["opener_signatures"]["excluded"],
                         [{"pull_id": "p04", "reason": "incomplete"}])
        self.assertEqual(group["representative_pulls"], [
            {"pull_id": "p03", "reason": "most_recent_kill"},
            {"pull_id": "p01", "reason": "longest_wipe"}])
        observation = [row for row in packet["observations"]
                       if row["id"] == "g1.deaths_before_end"][0]
        self.assertEqual((observation["numerator"], observation["denominator"],
                          observation["excluded"]),
                         (0, 3, [{"pull_id": "p04", "reason": "incomplete"}]))

    def observation(self, packet, name):
        rows = [row for row in packet["observations"] if row["id"] == "g1." + name]
        self.assertEqual(len(rows), 1, name)
        return rows[0]

    def test_short_pull_is_left_out_of_rates_and_representatives(self):
        # The real-log case: an 18 s wipe without damage became the "lowest dps" pull.
        packet = self.packet([self.pull(0, 100, damage=10000),
                              self.pull(5, 18, damage=0),
                              self.pull(10, 60, damage=12000, success=True)])
        group, = packet["groups"]
        self.assertEqual((group["attempts"], group["kills"], group["wipes"]), (3, 1, 2))
        self.assertEqual(self.stats(group["duration_s"])["n"], 3)
        self.assertEqual(group["duration_s"]["min"], 18.0)
        short = {"pull_id": "p02", "reason": "pull_shorter_than_min_comparable"}
        for name in ("dps_encounter", "dps_while_alive", "casts_per_minute"):
            self.assertEqual(group["metrics"][name]["excluded"], [short], name)
            self.assertEqual(group["metrics"][name]["n"], 2, name)
        self.assertEqual(group["metrics"]["total_effective"]["n"], 3)
        self.assertEqual((group["metrics"]["dps_encounter"]["min"],
                          group["metrics"]["dps_encounter"]["max"]), (100.0, 200.0))
        # Pooled rates keep the short pull: 22000 / 178 s.
        self.assertEqual(group["pooled"]["dps_encounter_pooled"], {
            "value": 123.596, "numerator": 22000, "denominator": 178.0, "n": 3,
            "pulls": ["p01", "p02", "p03"]})
        self.assertEqual(group["pooled"]["casts_per_minute_pooled"]["pulls"],
                         ["p01", "p02", "p03"])
        self.assertEqual(group["representative_pulls"], [
            {"pull_id": "p03", "reason": "most_recent_kill"},
            {"pull_id": "p01", "reason": "longest_wipe"}])
        # A short wipe is not the longest wipe even when it is the only one.
        only = self.packet([self.pull(0, 100, success=True), self.pull(5, 18)])
        self.assertEqual(only["groups"][0]["representative_pulls"],
                         [{"pull_id": "p01", "reason": "most_recent_kill"}])
        self.assertEqual(only["pulls"][1]["dps_encounter"], round(10000 / 18, 3))

    def test_opener_prefixes_and_consistency(self):
        def opener(*spells):
            return [self.cast(2 + index, "success", spell)
                    for index, spell in enumerate(spells)]
        # Every pull opens with 5143 at 1 s (see pull()).
        packet = self.packet([
            self.pull(0, 60, extra=opener(321507, 44425, 5143, 30451, 44425)),
            self.pull(5, 60, extra=opener(321507, 44425, 5143, 1449, 44425)),
            self.pull(10, 60, extra=opener(321507, 1449)),
            self.pull(15, 60, extra=opener(1449, 44425, 5143))])
        openers = packet["groups"][0]["opener_signatures"]
        self.assertEqual(openers["denominator"], 4)
        self.assertEqual(openers["prefixes"], [
            {"length": 2, "count": 3, "denominator": 4, "signature": [5143, 321507],
             "pulls": ["p01", "p02", "p03"]},
            {"length": 3, "count": 2, "denominator": 4,
             "signature": [5143, 321507, 44425], "pulls": ["p01", "p02"]},
            {"length": 4, "count": 2, "denominator": 3,
             "signature": [5143, 321507, 44425, 5143], "pulls": ["p01", "p02"]},
            # A 1-1 tie: the smaller sequence (1449 < 30451) wins.
            {"length": 6, "count": 1, "denominator": 2,
             "signature": [5143, 321507, 44425, 5143, 1449, 44425], "pulls": ["p02"]}])
        self.assertEqual([row["count"] for row in openers["signatures"]], [1, 1, 1, 1])
        observation = self.observation(packet, "opener_consistency")
        self.assertEqual(observation["statement"],
                         "Among 4 eligible pulls, the first 2 casts match the most common "
                         "sequence in 3 of 4 pulls with at least 2 opener casts, the first "
                         "3 in 2 of 4, the first 4 in 2 of 3, the first 6 in 1 of 2; most "
                         "common first-4 sequence: 5143 > 321507 > 44425 > 5143.")
        self.assertEqual((observation["numerator"], observation["denominator"],
                          observation["pulls"], observation["excluded"]),
                         (2, 3, ["p01", "p02"],
                          [{"pull_id": "p03", "reason": "signature_shorter_than_prefix"}]))

    def test_charge_over_energize_is_over_the_generated_total(self):
        packet = self.packet([
            self.pull(0, 60, extra=[self.energize(2, 321507, 3, 1, 16),
                                    self.energize(3, 153626, 1, 0, 16)]),
            self.pull(5, 60, extra=[self.energize(2, 321507, 2, 2, 16)])])
        observation = self.observation(packet, "charge_over_energize")
        # 321507: 3 over of 3 + 1 + 2 + 2 = 8 generated; 153626: 0 of 1.
        self.assertEqual((observation["numerator"], observation["denominator"]), (3, 9))
        self.assertEqual(observation["statement"],
                         "3 of 9 generated Arcane Charges were over the cap (33.3%), as "
                         "reported by Arcane Charge energize events; by spell "
                         "(over-cap/generated): 321507 3/8, 153626 0/1.")
        self.assertEqual(packet["definitions"]["charge_over_energize"]["denominator"],
                         "sum of gains + sum of over_energize")

    def test_deaths_before_end_carry_the_pull_result(self):
        packet = self.packet([self.pull(0, 100, deaths=(99,)),
                              self.pull(5, 50, success=True, deaths=(21,)),
                              self.pull(10, 60)])
        observation = self.observation(packet, "deaths_before_end")
        self.assertEqual(observation["statement"],
                         "The player died before the encounter ended in 2 of 3 eligible "
                         "pulls; first death as a fraction of the pull duration, with the "
                         "pull result: p01 at 0.99 (wipe), p02 at 0.42 (kill).")
        self.assertEqual(observation["evidence"], [
            {"pull_id": "p01", "t_s": 99.0, "result": "wipe", "ref": "life.deaths[0]"},
            {"pull_id": "p02", "t_s": 21.0, "result": "kill", "ref": "life.deaths[0]"}])

    def test_groups_split_by_talents_and_equipment(self):
        packet = self.packet([
            self.pull(0, 60), self.pull(5, 60),
            self.pull(10, 60, combatant=variant_combatant(talents=True)),
            self.pull(15, 60, combatant=variant_combatant(equipment=True))])
        configs = packet["character_configs"]
        self.assertEqual([(row["config_id"], row["pulls"]) for row in configs],
                         [("c1", ["p01", "p02"]), ("c2", ["p03"]), ("c3", ["p04"])])
        self.assertNotEqual(configs[0]["talents_fingerprint"],
                            configs[1]["talents_fingerprint"])
        self.assertEqual(configs[0]["equipment_fingerprint"],
                         configs[1]["equipment_fingerprint"])
        self.assertNotEqual(configs[0]["equipment_fingerprint"],
                            configs[2]["equipment_fingerprint"])
        self.assertEqual([(group["group_id"], group["config_id"], group["pulls"])
                          for group in packet["groups"]],
                         [("g1", "c1", ["p01", "p02"]), ("g2", "c2", ["p03"]),
                          ("g3", "c3", ["p04"])])
        self.assertEqual([pull["group_id"] for pull in packet["pulls"]],
                         ["g1", "g1", "g2", "g3"])

    def test_every_metric_has_a_definition_and_statements_attribute_no_cause(self):
        results = [self.pull(0, 100, deaths=(50,), extra=self.burst()),
                   self.pull(5, 60, success=True, extra=self.burst(5)),
                   self.pull(10, None, observe_until=30)]
        packet = self.packet(results)
        definitions = packet["definitions"]
        for group in packet["groups"]:
            for name in list(group["metrics"]) + list(group["pooled"]):
                self.assertIn(name, definitions)
        for observation in packet["observations"]:
            self.assertIn(observation["id"].split(".", 1)[1], definitions)
            self.assertIn(observation["kind"], ("observed", "inferred"))
            statement = observation["statement"].lower()
            for word in ("lost", "wasted", "movement"):
                self.assertNotIn(word, statement, observation["id"])
        ids = [row["id"] for row in packet["observations"]]
        self.assertEqual(ids, ["g1." + name for name in (
            "deaths_before_end", "dead_time_share", "action_gap_share", "opener_consistency",
            "damage_spell_concentration", "surge_first_use", "surge_touch_order",
            "burst_window_casts", "clearcasting_refresh_at_max", "charge_over_energize",
            "barrage_inferred_charges")])
        deaths = packet["observations"][0]
        self.assertEqual(deaths["statement"],
                         "The player died before the encounter ended in 1 of 2 eligible "
                         "pulls; first death as a fraction of the pull duration, with the "
                         "pull result: p01 at 0.5 (wipe).")
        self.assertEqual(deaths["evidence"], [{"pull_id": "p01", "t_s": 50.0,
                                               "result": "wipe", "ref": "life.deaths[0]"}])
        burst = packet["observations"][7]
        self.assertEqual((burst["numerator"], burst["denominator"]), (2, 2))
        self.assertEqual(packet["pulls"][0]["deaths_s"], [50.0])
        self.assertEqual(sorted(packet["evidence"]["burst_windows"]), ["p01", "p02"])
        # No Arcane observation without the spec section applied.
        plain = self.packet([self.pull(0, 60, combatant=self.heal_on_player(0))])
        self.assertEqual([row["id"] for row in plain["observations"]],
                         ["g1." + name for name in (
                             "deaths_before_end", "dead_time_share", "action_gap_share",
                             "opener_consistency", "damage_spell_concentration")])
        self.assertNotIn("burst_windows", plain["groups"][0]["metrics"])

    def test_partial_metrics_propagate_as_exclusions(self):
        with mock.patch.object(wle, "MAX_PERF_AURAS", 1):
            truncated = self.pull(5, 60, extra=self.burst() + [
                self.aura(1.5, "SPELL_AURA_APPLIED", 1001),
                self.aura(2, "SPELL_AURA_APPLIED", 1002)])
        with mock.patch.object(wle, "MAX_PERF_TIMELINE", 1):
            cut = self.pull(10, 60, extra=[self.cast(5, "success", 30451)])
        packet = self.packet([self.pull(0, 60, extra=self.burst()), truncated, cut])
        self.assertFalse(truncated["performance"]["auras"]["coverage"]["complete"])
        metrics = packet["groups"][0]["metrics"]
        for name in ("burst_windows", "burst_buff_uptime_share"):
            self.assertIn({"pull_id": "p02", "reason": "aura_keys_truncated"},
                          metrics[name]["excluded"], name)
        self.assertEqual(metrics["burst_windows"]["n"], 2)
        burst = [row for row in packet["observations"]
                 if row["id"] == "g1.burst_window_casts"][0]
        self.assertIn({"pull_id": "p02", "reason": "aura_keys_truncated"}, burst["excluded"])
        self.assertNotIn("p02", burst["pulls"])
        openers = packet["groups"][0]["opener_signatures"]
        self.assertEqual(openers["excluded"], [{"pull_id": "p03", "reason": "opener_partial"}])
        self.assertEqual(openers["denominator"], 2)
        self.assertEqual(packet["pulls"][1]["partial"], ["aura_keys"])
        self.assertEqual(packet["pulls"][2]["partial"], ["timeline", "opener"])
        self.assertIn({"pull_id": "p03", "code": "perf_timeline_truncated", "cap": 1,
                       "dropped": 1}, packet["data_quality"]["warnings"])

    def test_untracked_burst_buff_holder_excludes_the_pull_from_burst_comparisons(self):
        others = [(PERF_HEALER, PERF_HEALER_NAME), ("Player-1379-0000000C", "Otro-DunModr-EU")]
        extra = self.burst()
        for index, (caster, name) in enumerate(others):
            extra += [self.aura(20 + index, "SPELL_AURA_APPLIED", 365362, source=caster,
                                source_name=name),
                      self.aura(25 + index, "SPELL_AURA_REMOVED", 365362, source=caster,
                                source_name=name)]
        with mock.patch.object(wle, "MAX_PERF_INSTANCES", 1):
            overflow = self.pull(5, 60, extra=extra)
        packet = self.packet([self.pull(0, 60, extra=self.burst()), overflow])
        reason = {"pull_id": "p02", "reason": "metric_partial: aura_holders"}
        metrics = packet["groups"][0]["metrics"]
        for name in ("burst_windows", "burst_buff_uptime_share"):
            self.assertIn(reason, metrics[name]["excluded"], name)
            self.assertEqual(metrics[name]["n"], 1, name)
        burst = [row for row in packet["observations"]
                 if row["id"] == "g1.burst_window_casts"][0]
        self.assertIn(reason, burst["excluded"])
        self.assertEqual(burst["pulls"], ["p01"])
        self.assertIn("aura_holders", packet["pulls"][1]["partial"])
        self.assertNotIn("aura_holders", packet["pulls"][0]["partial"])

    def test_window_casts_past_the_cap_keep_their_total(self):
        extra = [self.aura(3, "SPELL_AURA_APPLIED", 365362)] + \
            [self.cast(4 + 0.1 * index, "success", 2000 + index) for index in range(4)] + \
            [self.aura(9, "SPELL_AURA_REMOVED", 365362)]
        with mock.patch.object(wle, "MAX_PERF_WINDOW_SPELLS", 2):
            packet = self.packet([self.pull(0, 60, extra=extra)])
        burst = self.observation(packet, "burst_window_casts")
        self.assertEqual((burst["numerator"], burst["denominator"], burst["pulls"],
                          burst["excluded"]), (4, 1, ["p01"], []))
        self.assertEqual(packet["evidence"]["burst_windows"]["p01"][0]["casts"],
                         {"2000": 1, "2001": 1, "other": 2})
        self.assertEqual(packet["pulls"][0]["partial"], ["burst_window_casts"])

    def test_a_pull_resolved_without_a_name_takes_the_guids_name(self):
        nameless = self.pull(0, 60)
        nameless["performance"]["player"]["name"] = None     # resolved by COMBATANT_INFO
        packets = [packet for packet, _ in self.packets([nameless, self.pull(300, 60)])]
        self.assertEqual([packet["player"]["name"] for packet in packets],
                         [PERF_NAME, PERF_NAME])
        self.assertIn("_%s_" % PERF_NAME, wle.packet_file_name(packets[0]))
        alone = self.packet([nameless])
        self.assertIsNone(alone["player"]["name"])
        self.assertIn("_Dkyam_", wle.packet_file_name(alone))

    def test_skipped_results_are_scoped_to_the_session_and_capped(self):
        results = [self.pull(0, 60)]
        inside = [{"name": "pkg%d" % index, "reason": "marker_missing",
                   "start": self.T0 + timedelta(minutes=30 + index)}
                  for index in (4, 3, 2, 1, 0)]
        outside = [{"name": "old", "reason": "performance_not_published",
                    "start": self.T0 - timedelta(days=10)},
                   {"name": "undated", "reason": "marker_unreadable", "start": None}]
        with mock.patch.object(wle, "MAX_PACKET_SKIPPED", 2):
            quality = self.packet(results, skipped=inside + outside)["data_quality"]
        self.assertEqual(quality["skipped_results"],
                         [{"name": "pkg0", "reason": "marker_missing"},
                          {"name": "pkg1", "reason": "marker_missing"}])
        self.assertEqual(quality["skipped_results_omitted"], 3)
        quality = self.packet(results, skipped=outside)["data_quality"]
        self.assertEqual(quality["skipped_results"], [])
        self.assertNotIn("skipped_results_omitted", quality)

    # -- sessions -----------------------------------------------------------------------
    def test_session_crossing_midnight_is_one_session(self):
        base = datetime(2026, 10, 1, 23, 50, 0)
        packet = self.packet([self.pull(0, 100, base=base), self.pull(30, 100, base=base)])
        session = packet["session"]
        self.assertEqual((session["id"], session["start_time"], session["end_time"],
                          session["crosses_midnight"], session["pull_count"]),
                         ("2026-10-01_23-50-00", "2026-10-01 23:50:00.000",
                          "2026-10-02 00:21:40.000", True, 2))

    def test_session_spread_over_two_log_files(self):
        other = "WoWCombatLog-100126_230000.txt"
        packet = self.packet([self.pull(0, 100), self.pull(10, 100, file=other)])
        self.assertEqual(packet["session"]["source_files"], [LOG_NAME, other])
        self.assertEqual(packet["session"]["pull_count"], 2)
        self.assertFalse(packet["session"]["crosses_midnight"])

    def test_gap_larger_than_the_threshold_splits_the_session(self):
        results = [self.pull(0, 100), self.pull(180, 100)]
        split = self.packets(results, gap=120)
        self.assertEqual([packet["session"]["id"] for packet, _ in split],
                         ["2026-10-01_21-40-00", "2026-10-02_00-40-00"])
        merged = self.packets(results, gap=240)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0][0]["session"]["gap_minutes"], 240)
        # The gap runs from the observed end (21:41:40) to the next start (00:40):
        # 178 min 20 s.
        self.assertEqual(len(self.packets(results, gap=179)), 1)
        self.assertEqual(len(self.packets(results, gap=178)), 2)

    def test_incomplete_pull_ends_at_its_observation_end_for_sessions(self):
        results = [self.pull(0, None, observe_until=600), self.pull(131, 60)]
        # 21:40 + 600 s = 21:50; the next start 23:51 is 121 minutes later.
        self.assertEqual(len(self.packets(results, gap=120)), 2)
        self.assertEqual(len(self.packets(results, gap=121)), 1)

    # -- identity -----------------------------------------------------------------------
    def test_two_guids_for_one_short_name_give_two_packets(self):
        def other(minutes):
            start = self.T0 + timedelta(minutes=minutes)
            return self.result(start, [
                self.start(0), self.damage(5, 7268, 300, source=ALT_GUID, source_name=ALT_NAME),
                self.end(60)])
        absent = self.result(self.T0 + timedelta(minutes=20), [
            self.start(0), self.damage(5, 7268, 300, source=PERF_HEALER,
                                       source_name=PERF_HEALER_NAME), self.end(60)])
        ambiguous = self.result(self.T0 + timedelta(minutes=25), [
            self.start(0), self.damage(4, 7268, 300),
            self.damage(5, 7268, 300, source=ALT_GUID, source_name=ALT_NAME), self.end(60)])
        results = [self.pull(0, 60), self.pull(5, 60), other(10), other(15), absent,
                   ambiguous]
        self.assertEqual([r["performance"]["player"]["status"] for r in results],
                         ["resolved"] * 4 + ["absent", "ambiguous"])
        packets = [packet for packet, _ in self.packets(results)]
        self.assertEqual([packet["player"]["guid"] for packet in packets], [DKYAM, ALT_GUID])
        names = [wle.packet_file_name(packet) for packet in packets]
        self.assertEqual(names, [
            "2026-10-01_21-40-00_Dkyam-DunModr-EU_%s_diagnostic_packet.json"
            % wle._sha1(DKYAM.encode())[:8],
            "2026-10-01_21-50-00_Dkyam-Otro-EU_%s_diagnostic_packet.json"
            % wle._sha1(ALT_GUID.encode())[:8]])
        self.assertEqual([[pull["segment_id"] for pull in packet["pulls"]]
                          for packet in packets],
                         [[r["performance"]["segment"]["segment_id"] for r in results[:2]],
                          [r["performance"]["segment"]["segment_id"] for r in results[2:4]]])
        # No combined statistics: each packet only pools its own GUID's damage.
        self.assertEqual(packets[0]["groups"][0]["metrics"]["total_effective"]["max"], 10000)
        self.assertEqual(packets[1]["groups"][0]["metrics"]["total_effective"]["max"], 300)
        for packet in packets:
            self.assertEqual(
                [(row["status"], [c["guid"] for c in row["candidates"]])
                 for row in packet["pulls_without_player"]],
                [("absent", []), ("ambiguous", [DKYAM, ALT_GUID])])

    # -- duplicates ---------------------------------------------------------------------
    def test_duplicate_incomplete_and_complete_copies_prefer_the_complete_one(self):
        other = "WoWCombatLog-100126_200000.txt"
        incomplete = self.pull(0, None, observe_until=40, file=LOG_NAME,
                               name="A_INCOMPLETE")
        complete = self.pull(0, 100, file=other, name="B_Wipe")
        forward = self.packets([incomplete, complete, self.pull(5, 60)])
        backward = self.packets([self.pull(5, 60), complete, incomplete])
        self.assertEqual(forward[0][1], backward[0][1])
        packet = forward[0][0]
        self.assertEqual(len(packet["pulls"]), 2)
        first = packet["pulls"][0]
        self.assertEqual((first["complete"], first["segment_id"]),
                         (True, complete["performance"]["segment"]["segment_id"]))
        self.assertEqual([(row["file"], row["published_name"]) for row in first["sources"]],
                         [(other, "B_Wipe"), (LOG_NAME, "A_INCOMPLETE")])
        self.assertEqual([row["pull_id"] for row in packet["data_quality"]["duplicates"]],
                         ["p01"])
        self.assertEqual(packet["data_quality"]["duplicate_conflicts"], [])

    def test_duplicate_complete_copies_with_different_totals_are_declared(self):
        other = "WoWCombatLog-100126_200000.txt"
        low = self.pull(0, 100, damage=10000, file=LOG_NAME, name="A")
        high = self.pull(0, 100, damage=12000, file=other, name="B")
        forward = self.packets([low, high])
        backward = self.packets([high, low])
        self.assertEqual(forward[0][1], backward[0][1])
        packet = forward[0][0]
        winner = packet["pulls"][0]
        # Same completeness and end: the lower log file name wins.
        self.assertEqual((winner["effective"], winner["sources"][0]["file"]),
                         (10000, LOG_NAME))
        self.assertEqual(packet["data_quality"]["duplicate_conflicts"], [
            {"pull_id": "p01", "fields": {"total_effective": [10000, 12000]}}])
        # A later observed end beats the file name.
        longer = self.pull(0, 101, damage=12000, file=other, name="B")
        packet = self.packet([low, longer])
        self.assertEqual(packet["pulls"][0]["sources"][0]["file"], other)
        self.assertEqual(packet["data_quality"]["duplicate_conflicts"][0]["fields"],
                         {"duration_ms": [101000, 100000],
                          "total_effective": [12000, 10000]})

    # -- budget -------------------------------------------------------------------------
    def budget_results(self):
        """Five pulls: p05 kill, p01 longest wipe, p03 median; ten damage spells each."""
        spells = [self.damage(12 + index, 9001 + index, 100 - index) for index in range(9)]
        return [self.pull(minutes, 100, deaths=(60,) if minutes % 10 == 0 else (),
                          success=minutes == 20, extra=self.burst() + spells)
                for minutes in range(0, 25, 5)]

    def test_generous_budget_is_complete(self):
        packet = self.packet(self.budget_results())
        self.assertTrue(packet["complete"])
        self.assertEqual(packet["budget"]["omitted"], [])
        self.assertFalse(packet["budget"]["budget_exceeded"])
        self.assertEqual(packet["budget"]["max_bytes"], 200000)

    def test_small_budget_omits_evidence_in_order_and_keeps_every_pull(self):
        results = self.budget_results()
        full, full_data = self.packets(results)[0]
        representatives = sorted(row["pull_id"] for group in full["groups"]
                                 for row in group["representative_pulls"])
        others = sorted(set(pull["pull_id"] for pull in full["pulls"]) -
                        set(representatives))
        self.assertTrue(others)
        # Below the full size by less than the non-representative burst windows.
        max_bytes = len(full_data) - 50
        packet, data = self.packets(results, max_bytes=max_bytes)[0]
        self.assertLessEqual(len(data), max_bytes)
        self.assertFalse(packet["complete"])
        self.assertFalse(packet["budget"]["budget_exceeded"])
        self.assertEqual(packet["budget"]["omitted"], [
            {"what": "evidence.burst_windows", "pulls": others,
             "reason": "packet budget: non-representative pulls"}])
        self.assertEqual(sorted(packet["evidence"]["burst_windows"]), representatives)
        self.assertEqual(packet["pulls"], full["pulls"])
        self.assertEqual(packet["groups"], full["groups"])
        self.assertEqual(packet["observations"], full["observations"])

    def test_budget_too_small_for_the_essentials_is_exceeded(self):
        results = self.budget_results()
        full = self.packet(results)
        packet, data = self.packets(results, max_bytes=100)[0]
        self.assertTrue(packet["budget"]["budget_exceeded"])
        self.assertFalse(packet["complete"])
        self.assertGreater(len(data), 100)
        others, chosen, every = ["p02", "p04"], ["p01", "p03", "p05"],             ["p01", "p02", "p03", "p04", "p05"]
        other_reason = "packet budget: non-representative pulls"
        chosen_reason = "packet budget: representative pulls"
        self.assertEqual(packet["budget"]["omitted"], [
            {"what": "evidence.burst_windows", "pulls": others, "reason": other_reason},
            {"what": "evidence.openers.entries", "pulls": others, "reason": other_reason},
            {"what": "evidence.gaps", "pulls": others, "reason": other_reason},
            {"what": "groups.spells beyond the first 8 rows", "pulls": every,
             "reason": "packet budget"},
            {"what": "pulls.top_spells beyond the first 5 rows", "pulls": every,
             "reason": "packet budget"},
            {"what": "evidence.burst_windows", "pulls": chosen, "reason": chosen_reason},
            {"what": "evidence.openers.entries", "pulls": chosen, "reason": chosen_reason},
            {"what": "evidence.gaps", "pulls": chosen, "reason": chosen_reason},
            {"what": "evidence.deaths", "pulls": chosen, "reason": chosen_reason}])
        self.assertEqual(len(packet["pulls"]), 5)
        for pull, original in zip(packet["pulls"], full["pulls"]):
            self.assertEqual(pull["top_spells"], original["top_spells"][:5])
            self.assertEqual({key: value for key, value in pull.items()
                              if key != "top_spells"},
                             {key: value for key, value in original.items()
                              if key != "top_spells"})
        group, original = packet["groups"][0], full["groups"][0]
        self.assertEqual(group["spells"], original["spells"][:8])
        self.assertEqual({key: value for key, value in group.items() if key != "spells"},
                         {key: value for key, value in original.items() if key != "spells"})
        for key in ("observations", "definitions", "data_quality"):
            self.assertEqual(packet[key], full[key], key)
        self.assertEqual(packet["evidence"]["burst_windows"], {})
        self.assertTrue(all(set(row) == {"signature"}
                            for row in packet["evidence"]["openers"].values()))


class DiagnosticRebuildTests(ExtractorTestCase):
    """rebuild_diagnostics through the Extractor: writes, recovery, isolation."""

    T0 = datetime(2026, 10, 1, 21, 40, 0)
    SUFFIX = "_diagnostic_packet.json"

    def add_pull(self, builder, start, end=True, success="1"):
        def at(seconds):
            return start + timedelta(seconds=seconds)
        builder.add(at(-10), "COMBAT_LOG_VERSION", "22", "ADVANCED_LOG_ENABLED", "1",
                    "BUILD_VERSION", "12.1.0", "PROJECT_ID", "1")
        builder.add(at(0), "ENCOUNTER_START", "3421", q(PERF_BOSS_NAME), "15", "20", "2900")
        builder.add(at(1), "COMBATANT_INFO", perf_combatant()[len("COMBATANT_INFO,"):])
        builder.add(at(2), "SPELL_CAST_SUCCESS",
                    *perf_header(DKYAM, PERF_NAME, PERF_PLAYER_FLAGS, PERF_BOSS,
                                 PERF_BOSS_NAME, PERF_HOSTILE),
                    "5143", q("Misiles Arcanos"), "0x40", *perf_block(DKYAM, 0, 900, 1000))
        builder.add(at(3), "SPELL_DAMAGE",
                    *perf_header(DKYAM, PERF_NAME, PERF_PLAYER_FLAGS, PERF_BOSS,
                                 PERF_BOSS_NAME, PERF_HOSTILE),
                    "7268", q("Misiles Arcanos"), "0x40",
                    *perf_block(PERF_BOSS, max_hp=5000000),
                    "400", "400", "-1", "64", "0", "0", "0", "nil", "nil", "nil", "ST")
        builder.add(at(4), "SPELL_HEAL",
                    *perf_header(PERF_HEALER, PERF_HEALER_NAME, PERF_PLAYER_FLAGS, DKYAM,
                                 PERF_NAME, PERF_PLAYER_FLAGS),
                    "2061", q("Flash Heal"), "0x2", *perf_block(DKYAM, 0, 800, 1000),
                    "100", "100", "0", "0", "nil")
        if end:
            builder.add(at(30), "ENCOUNTER_END", "3421", q(PERF_BOSS_NAME), "15", "20",
                        success, "30000")
        return builder

    def write_pulls(self, *minutes, name=LOG_NAME):
        builder = LogBuilder()
        for value in minutes:
            self.add_pull(builder, self.T0 + timedelta(minutes=value))
        return self.write_log(builder.data(), name=name)

    def extractor(self, player="Dkyam", **kwargs):
        options = wle.OutputOptions(analysis_only=True, performance_player=player) \
            if player else wle.OutputOptions(analysis_only=True)
        extractor = wle.Extractor(self.log_dir, self.output_dir, state_path=self.state_path,
                                  verbose=False, output_options=options, **kwargs)
        extractor.prepare()
        return extractor

    def run_extract(self, expected, player="Dkyam", **kwargs):
        extractor = self.extractor(player, **kwargs)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(extractor.run_once(), expected)
        return extractor

    def diag_dir(self):
        return os.path.join(self.output_dir, wle.DIAGNOSTICS_DIR_NAME)

    def packet_names(self):
        return [name for name in self.list_outputs(self.diag_dir())
                if name.endswith(self.SUFFIX)]

    def packet_path(self):
        names = self.packet_names()
        self.assertEqual(len(names), 1, names)
        return os.path.join(self.diag_dir(), names[0])

    snapshot = PerformancePublicationTests.snapshot
    read_bytes = staticmethod(PerformancePublicationTests.read_bytes)

    def test_packet_written_idempotent_and_its_size_matches(self):
        self.write_pulls(0, 5)
        extractor = self.run_extract((0, 2, 0))
        path = self.packet_path()
        name = os.path.basename(path)
        self.assertTrue(name.startswith("2026-10-01_21-40-00_Dkyam-DunModr-EU_"))
        data = self.read_bytes(path)
        packet = json.loads(data, parse_constant=self.fail)
        self.assertEqual(packet["budget"]["actual_bytes"], os.path.getsize(path))
        self.assertEqual(len(packet["pulls"]), 2)
        self.assertEqual(packet["data_quality"]["skipped_results"], [])
        self.assertEqual(extractor.diagnostics_summary, {
            "written": 1, "unchanged": 0, "deleted": 0, "held": 0, "sessions": 1, "pulls": 2,
            "resolved": 2, "absent": 0, "ambiguous": 0, "failed": 0, "skipped": 0})
        before = self.snapshot(self.output_dir)
        extractor = self.run_extract((0, 0, 0))
        self.assertEqual(extractor.diagnostics_summary["unchanged"], 1)
        self.assertEqual(self.snapshot(self.diag_dir()),
                         {name: before[os.path.join(wle.DIAGNOSTICS_DIR_NAME, name)]})

    def test_deleted_packet_is_restored_by_run_once_without_new_data(self):
        self.write_pulls(0)
        self.run_extract((0, 1, 0))
        path = self.packet_path()
        data = self.read_bytes(path)
        os.remove(path)
        raids = self.snapshot(self.raids_dir())
        self.run_extract((0, 0, 0))
        self.assertEqual(self.read_bytes(path), data)
        self.assertEqual(self.snapshot(self.raids_dir()), raids)

    WATCH_LINE = "Diagnostics: not rebuilt in --watch mode; run once without --watch " \
                 "to rebuild the packets"

    def diag_state(self):
        """(Diagnostics/ exists, snapshot): watch must leave both exactly as they were."""
        return os.path.isdir(self.diag_dir()), self.snapshot(self.diag_dir())

    def assert_watch_left_diagnostics(self, before, output, extractor):
        self.assertEqual(self.diag_state(), before)
        flagged = extractor.output_options.performance_player is not None
        self.assertEqual(output.count(self.WATCH_LINE), 1 if flagged else 0, output)
        self.assertNotIn("diagnostics not rebuilt", output)
        self.assertIsNone(extractor.diagnostics_summary)
        self.assertTrue(extractor.diagnostics_dirty)

    def test_idle_watch_never_rebuilds_and_the_next_run_restores_a_deleted_packet(self):
        self.write_pulls(0)
        self.run_extract((0, 1, 0))
        path = self.packet_path()
        data = self.read_bytes(path)
        os.remove(path)
        raids = self.snapshot(self.raids_dir())
        before = self.diag_state()
        extractor = self.extractor()
        output = io.StringIO()
        with mock.patch.object(wle, "rebuild_diagnostics",
                               side_effect=wle.rebuild_diagnostics) as rebuild, \
                contextlib.redirect_stdout(output):
            self.assertEqual(extractor.watch(interval=0, max_polls=2), (0, 0, 0))
        self.assertEqual(rebuild.call_count, 0)
        self.assert_watch_left_diagnostics(before, output.getvalue(), extractor)
        self.assertFalse(os.path.exists(path))
        extractor = self.run_extract((0, 0, 0))
        self.assertEqual(self.read_bytes(path), data)
        self.assertEqual(self.snapshot(self.raids_dir()), raids)
        self.assertFalse(extractor.diagnostics_dirty)

    def test_watch_without_the_flag_prints_no_diagnostics_line(self):
        self.write_pulls(0)
        before = self.diag_state()
        extractor = self.extractor(None)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(extractor.watch(interval=0, max_polls=1), (0, 1, 0))
        self.assert_watch_left_diagnostics(before, output.getvalue(), extractor)
        self.assertFalse(os.path.exists(self.diag_dir()))

    def test_watch_over_several_polls_writes_no_packet_and_the_next_run_writes_one(self):
        log = self.write_pulls(0)
        before = self.diag_state()
        extractor = self.extractor()
        polls = []

        def grow(_):
            # After each poll but the last, one more pull is appended to the log.
            polls.append(len(self.pull_markers()))
            if len(polls) < 3:
                self.write_pulls(*range(0, 5 * (len(polls) + 1), 5))
        output = io.StringIO()
        with mock.patch.object(wle.time, "sleep", side_effect=grow), \
                contextlib.redirect_stdout(output):
            mplus, raid, errors = extractor.watch(interval=0, max_polls=3)
        self.assertEqual((mplus, raid, errors), (0, 3, 0))
        # Published pulls after each poll: the latest pull waits for the next one (or
        # for the shutdown), so polls 2 and 3 each publish one.
        self.assertEqual(polls, [0, 1, 2])
        self.assertEqual(len(self.pull_markers()), 3)
        self.assert_watch_left_diagnostics(before, output.getvalue(), extractor)
        extractor = self.run_extract((0, 0, 0))
        self.assertEqual(len(self.packet_names()), 1)
        self.assertEqual(len(json.loads(self.read_bytes(self.packet_path()))["pulls"]), 3)
        self.assertEqual(extractor.diagnostics_summary["written"], 1)
        self.assertEqual(extractor.state.get_offset(log), os.path.getsize(log))

    def fail_diagnostics_writes(self, times=None):
        real = wle._atomic_write_bytes
        calls = {"failed": 0}

        def fail(path, data):
            if wle.DIAGNOSTICS_DIR_NAME in path.split(os.sep) and \
                    (times is None or calls["failed"] < times):
                calls["failed"] += 1
                raise OSError("simulated packet write failure")
            return real(path, data)
        return mock.patch.object(wle, "_atomic_write_bytes", side_effect=fail)

    def test_failed_packet_write_is_reported_and_retried_by_the_next_run(self):
        self.write_pulls(0)
        self.run_extract((0, 1, 0))
        path = self.packet_path()
        data = self.read_bytes(path)
        os.remove(path)
        before = self.diag_state()
        extractor = self.extractor()
        output = io.StringIO()
        with self.fail_diagnostics_writes() as write, contextlib.redirect_stdout(output):
            self.assertEqual(extractor.watch(interval=0, max_polls=2), (0, 0, 0))
        # watch never even tries to write a packet.
        self.assertEqual([call for call in write.call_args_list
                          if wle.DIAGNOSTICS_DIR_NAME in call.args[0].split(os.sep)], [])
        self.assert_watch_left_diagnostics(before, output.getvalue(), extractor)
        output = io.StringIO()
        with self.fail_diagnostics_writes(times=1), contextlib.redirect_stdout(output):
            extractor = self.extractor()
            self.assertEqual(extractor.run_once(), (0, 0, 1))
        self.assertIn("error rebuilding diagnostics", output.getvalue())
        self.assertTrue(extractor.diagnostics_dirty)
        self.assertFalse(os.path.exists(path))
        extractor = self.run_extract((0, 0, 0))
        self.assertEqual(self.read_bytes(path), data)
        self.assertFalse(extractor.diagnostics_dirty)

    def test_failed_replacement_keeps_old_packets_and_deletes_nothing(self):
        self.write_pulls(0, 180)
        self.run_extract((0, 2, 0), session_gap_minutes=120)
        names = self.packet_names()
        self.assertEqual(len(names), 2)
        before = self.snapshot(self.diag_dir())
        raids = self.snapshot(self.raids_dir())
        with self.fail_diagnostics_writes():
            self.run_extract((0, 0, 1), session_gap_minutes=240)
        self.assertEqual(self.snapshot(self.diag_dir()), before)
        # Changing the gap re-partitions from the published results: nothing is
        # reprocessed or republished.
        extractor = self.run_extract((0, 0, 0), session_gap_minutes=240)
        self.assertEqual(self.snapshot(self.raids_dir()), raids)
        self.assertEqual(self.packet_names(), [names[0]])
        self.assertEqual(len(json.loads(self.read_bytes(self.packet_path()))["pulls"]), 2)
        self.assertEqual((extractor.diagnostics_summary["written"],
                          extractor.diagnostics_summary["deleted"]), (1, 1))

    def test_switching_selector_removes_the_previous_players_packet(self):
        self.write_pulls(0)
        self.run_extract((0, 1, 0))
        first = self.packet_names()
        self.run_extract((0, 1, 0), player=PERF_HEALER_NAME)
        names = self.packet_names()
        self.assertEqual(len(names), 1)
        self.assertNotEqual(names, first)
        self.assertIn("Sanadora-DunModr-EU", names[0])

    def test_foreign_files_in_diagnostics_are_never_touched(self):
        self.write_pulls(0)
        self.run_extract((0, 1, 0))
        foreign = {"notes.txt": b"mine", "old_diagnostic_packet.json": b"{\"x\": 1}",
                   "broken_diagnostic_packet.json": b"not json",
                   "stray.tmp": b"user temp"}
        for name, data in foreign.items():
            with open(os.path.join(self.diag_dir(), name), "wb") as handle:
                handle.write(data)
        own_temp = os.path.join(self.diag_dir(), "." + self.packet_names()[0] + ".abc.tmp")
        with open(own_temp, "wb") as handle:
            handle.write(b"half")
        self.run_extract((0, 1, 0), player=PERF_HEALER_NAME)    # deletes the Dkyam packet
        self.assertFalse(os.path.exists(own_temp))      # prepare() removed it
        for name, data in foreign.items():
            self.assertEqual(self.read_bytes(os.path.join(self.diag_dir(), name)), data)

    def test_pull_without_marker_is_skipped_until_the_package_is_repaired(self):
        self.write_pulls(0)
        real = wle._atomic_write_bytes

        def fail_marker(path, data):
            if path.endswith(os.path.join("analysis", "metadata.json")):
                raise OSError("simulated crash: marker")
            return real(path, data)
        with mock.patch.object(wle, "_atomic_write_bytes", side_effect=fail_marker):
            extractor = self.run_extract((0, 0, 1))
        # A run with a processing error does not rebuild at all.
        self.assertEqual(self.packet_names(), [])
        self.assertIsNone(extractor.diagnostics_summary)
        self.assertTrue(extractor.diagnostics_dirty)
        results, skipped = wle.collect_performance_results(
            self.raids_dir(), extractor.output_options.performance_fingerprint)
        self.assertEqual(results, [])
        self.assertEqual([row["reason"] for row in skipped], ["marker_missing"])
        self.run_extract((0, 1, 0))
        self.assertEqual(len(json.loads(self.read_bytes(self.packet_path()))["pulls"]), 1)

    # -- hold rule: a packet that lists a package left without its marker -----------------
    def package_dir(self, tag):
        names = [name for name in self.list_outputs(self.raids_dir()) if tag in name]
        self.assertEqual(len(names), 1, names)
        return os.path.join(self.raids_dir(), names[0])

    def marker_path(self, tag):
        return os.path.join(self.package_dir(tag), "analysis", "metadata.json")

    def assert_held(self, extractor, held=1):
        summary = extractor.diagnostics_summary
        self.assertEqual((summary["held"], summary["written"], summary["deleted"]),
                         (held, 0, 0))
        self.assertTrue(wle.diagnostics_summary_line(summary).endswith(
            "; %d packet(s) kept: a pull is being republished" % held))

    def fail_republication(self, expected):
        """run_once with --reset-state whose copy of deaths.json fails: marker gone."""
        real_copy = wle._copy_atomic

        def fail(source, destination):
            if destination.endswith("deaths.json"):
                raise OSError("simulated crash: deaths.json copy")
            return real_copy(source, destination)
        extractor = self.extractor()
        extractor.prepare(reset_state=True)
        with mock.patch.object(wle, "_copy_atomic", side_effect=fail), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(extractor.run_once(), expected)
        self.assertEqual(self.pull_markers(), [])

    def test_packet_listing_a_package_without_its_marker_is_held_until_repaired(self):
        self.write_pulls(0, 5)
        self.run_extract((0, 2, 0))
        path = self.packet_path()
        data = self.read_bytes(path)
        marker = self.marker_path("_21-45_")
        marker_data = self.read_bytes(marker)
        os.remove(marker)
        extractor = self.run_extract((0, 0, 0))
        # Not shrunk to the pull that still has its marker: held as it is.
        self.assertEqual(self.packet_names(), [os.path.basename(path)])
        self.assertEqual(self.read_bytes(path), data)
        self.assert_held(extractor)
        self.assertIn("; 1 published result(s) skipped; 1 packet(s) kept: a pull is being "
                      "republished\n", self.run_cli("Dkyam"))
        self.assertEqual(self.read_bytes(path), data)
        # Repaired by hand: the packet is rebuilt normally (same pulls, same bytes).
        with open(marker, "wb") as handle:
            handle.write(marker_data)
        extractor = self.run_extract((0, 0, 0))
        self.assertEqual(self.read_bytes(path), data)
        self.assertEqual((extractor.diagnostics_summary["held"],
                          extractor.diagnostics_summary["unchanged"]), (0, 1))
        self.assertNotIn("kept", wle.diagnostics_summary_line(extractor.diagnostics_summary))
        # Repaired by republishing the log.
        os.remove(marker)
        extractor = self.extractor()
        extractor.prepare(reset_state=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(extractor.run_once(), (0, 2, 0))
        self.assertEqual(self.read_bytes(path), data)
        self.assertEqual((extractor.diagnostics_summary["held"],
                          extractor.diagnostics_summary["unchanged"]), (0, 1))

    def test_failed_republication_of_a_vanished_log_keeps_the_packet(self):
        log = self.write_pulls(0)
        self.run_extract((0, 1, 0))
        path = self.packet_path()
        data = self.read_bytes(path)
        self.fail_republication((0, 0, 1))
        self.assertEqual(self.read_bytes(path), data)
        os.remove(log)      # nothing left to replay the pull from
        extractor = self.run_extract((0, 0, 0))
        self.assertEqual(self.read_bytes(path), data)
        self.assert_held(extractor)
        self.assertEqual(extractor.diagnostics_summary["pulls"], 0)

    def test_failed_republication_retained_as_pending_keeps_the_packet(self):
        # A pull without its END, finalized as incomplete while its log was rotated away.
        old = self.write_rotated_pull(end=False)
        self.run_extract((0, 1, 0))
        path = self.packet_path()
        data = self.read_bytes(path)
        self.fail_republication((0, 0, 1))
        # The log becomes the latest (and fresh) again: re-read without error, but its
        # pull stays pending, so the package is still without its marker.
        future = time.time() + 60
        os.utime(old, (future, future))
        extractor = self.run_extract((0, 0, 0))
        self.assertEqual(self.pull_markers(), [])
        self.assertEqual(self.read_bytes(path), data)
        self.assert_held(extractor)
        # Rotated away again: the pull is published and the packet is rebuilt.
        past = time.time() - 3600
        os.utime(old, (past, past))
        extractor = self.run_extract((0, 1, 0))
        self.assertEqual(len(self.pull_markers()), 1)
        self.assertEqual(self.read_bytes(path), data)
        self.assertEqual((extractor.diagnostics_summary["held"],
                          extractor.diagnostics_summary["unchanged"]), (0, 1))

    def test_packet_listing_a_package_that_is_gone_is_not_held(self):
        self.write_pulls(0, 5)
        self.run_extract((0, 2, 0))
        path = self.packet_path()
        os.remove(self.marker_path("_21-45_"))
        os.rename(self.package_dir("_21-45_"), os.path.join(self.root, "moved-away"))
        extractor = self.run_extract((0, 0, 0))
        self.assertEqual(len(json.loads(self.read_bytes(path))["pulls"]), 1)
        self.assertEqual((extractor.diagnostics_summary["held"],
                          extractor.diagnostics_summary["written"]), (0, 1))
        os.rename(self.package_dir("_21-40_"), os.path.join(self.root, "moved-too"))
        extractor = self.run_extract((0, 0, 0))
        self.assertEqual(self.packet_names(), [])
        self.assertEqual((extractor.diagnostics_summary["held"],
                          extractor.diagnostics_summary["deleted"]), (0, 1))

    def test_hold_never_deletes_a_previous_selectors_packet(self):
        log = self.write_pulls(0)
        self.run_extract((0, 1, 0))
        path = self.packet_path()
        data = self.read_bytes(path)
        os.remove(log)
        marker = self.marker_path("_21-40_")
        marker_data = self.read_bytes(marker)
        os.remove(marker)
        extractor = self.run_extract((0, 0, 0), player=PERF_HEALER_NAME)
        self.assertEqual(self.packet_names(), [os.path.basename(path)])
        self.assertEqual(self.read_bytes(path), data)
        self.assert_held(extractor)
        # Repaired (a marker of the other selector): now obsolete, and deleted.
        with open(marker, "wb") as handle:
            handle.write(marker_data)
        extractor = self.run_extract((0, 0, 0), player=PERF_HEALER_NAME)
        self.assertEqual(self.packet_names(), [])
        self.assertEqual((extractor.diagnostics_summary["held"],
                          extractor.diagnostics_summary["deleted"]), (0, 1))

    def add_package(self, name, marker=None):
        """A package under Raids/ that is not a valid result of this selector."""
        analysis = os.path.join(self.raids_dir(), name, "analysis")
        os.makedirs(analysis)
        with open(os.path.join(analysis, "combat.txt"), "wb") as handle:
            handle.write(b"x")
        if marker is not None:
            with open(os.path.join(analysis, "metadata.json"), "w", encoding="utf-8") as h:
                json.dump(marker, h)

    def test_skipped_packages_belong_only_to_their_session(self):
        self.write_pulls(0, 5)
        self.run_extract((0, 2, 0))
        data = self.read_bytes(self.packet_path())
        # An old package published without the flag, from another night.
        self.add_package("2026-09-20_21-00_Raid_Old_Heroic_Kill", {
            "segment_id": "raid|WoWCombatLog-092026_200000.txt|2026-09-20 21:00:00.000|3421",
            "artifacts": ["2026-09-20_21-00_Raid_Old_Heroic_Kill/analysis/combat.txt"]})
        extractor = self.run_extract((0, 0, 0))
        self.assertEqual(self.read_bytes(self.packet_path()), data)
        self.assertEqual((extractor.diagnostics_summary["skipped"],
                          extractor.diagnostics_summary["unchanged"]), (1, 1))
        # Inside the session window: a broken package (start from its name) and one
        # renamed by hand (start from its marker's segment id).
        self.add_package("2026-10-01_21-50_Raid_Broken_Heroic_Wipe")
        self.add_package("renamed by hand", {
            "segment_id": "raid|%s|2026-10-01 22:10:00.000|3421" % LOG_NAME,
            "artifacts": []})
        extractor = self.run_extract((0, 0, 0))
        packet = json.loads(self.read_bytes(self.packet_path()))
        self.assertEqual(packet["data_quality"]["skipped_results"], [
            {"name": "2026-10-01_21-50_Raid_Broken_Heroic_Wipe", "reason": "marker_missing"},
            {"name": "renamed by hand", "reason": "performance_not_published"}])
        self.assertEqual(extractor.diagnostics_summary["skipped"], 3)

    def test_failed_read_of_the_results_keeps_every_packet(self):
        self.write_pulls(0)
        self.run_extract((0, 1, 0))
        path = self.packet_path()
        data = self.read_bytes(path)
        raids = os.path.normcase(os.path.abspath(self.raids_dir()))
        real_listdir, real_open = os.listdir, open

        def deny_listing(target):
            if os.path.normcase(os.path.abspath(target)) == raids:
                raise PermissionError(13, "simulated: listing denied", target)
            return real_listdir(target)

        def deny_marker(target, *args, **kwargs):
            if isinstance(target, str) and \
                    target.endswith(os.path.join("analysis", "metadata.json")):
                raise PermissionError(13, "simulated: read denied", target)
            return real_open(target, *args, **kwargs)
        for patch in (mock.patch.object(wle.os, "listdir", side_effect=deny_listing),
                      mock.patch("builtins.open", side_effect=deny_marker)):
            output = io.StringIO()
            with patch, contextlib.redirect_stdout(output):
                extractor = self.extractor()
                self.assertEqual(extractor.run_once(), (0, 0, 1))
            self.assertIn("error rebuilding diagnostics", output.getvalue())
            self.assertTrue(extractor.diagnostics_dirty)
            self.assertEqual(self.read_bytes(path), data)
        extractor = self.run_extract((0, 0, 0))
        self.assertEqual(self.read_bytes(path), data)
        self.assertEqual((extractor.diagnostics_summary["written"],
                          extractor.diagnostics_summary["unchanged"],
                          extractor.diagnostics_summary["deleted"]), (0, 1, 0))

    def test_denied_package_stat_fails_the_rebuild_and_keeps_the_packet(self):
        self.write_pulls(0)
        self.run_extract((0, 1, 0))
        path = self.packet_path()
        data = self.read_bytes(path)
        package = os.path.normcase(os.path.join(self.raids_dir(), [
            name for name in self.list_outputs(self.raids_dir()) if "_21-40_" in name][0]))
        real_stat = os.stat

        def deny_stat(target, *args, **kwargs):
            if isinstance(target, str) and \
                    os.path.normcase(os.path.abspath(target)) == package:
                raise PermissionError(13, "simulated: stat denied", target)
            return real_stat(target, *args, **kwargs)
        extractor = self.extractor()
        output = io.StringIO()
        with mock.patch.object(wle.os, "stat", side_effect=deny_stat), \
                contextlib.redirect_stdout(output):
            self.assertEqual(extractor.run_once(), (0, 0, 1))
        self.assertIn("error rebuilding diagnostics", output.getvalue())
        self.assertTrue(extractor.diagnostics_dirty)
        self.assertEqual(self.read_bytes(path), data)
        extractor = self.run_extract((0, 0, 0))
        self.assertEqual(self.read_bytes(path), data)
        self.assertEqual((extractor.diagnostics_summary["written"],
                          extractor.diagnostics_summary["unchanged"],
                          extractor.diagnostics_summary["deleted"]), (0, 1, 0))

    def test_unreadable_obsolete_packet_is_kept_and_reported(self):
        self.write_pulls(0)
        self.run_extract((0, 1, 0))
        old = os.path.normcase(self.packet_path())
        data = self.read_bytes(old)
        real_open = open

        def deny_packet(target, *args, **kwargs):
            if isinstance(target, str) and os.path.normcase(os.path.abspath(target)) == old:
                raise PermissionError(13, "simulated: read denied", target)
            return real_open(target, *args, **kwargs)
        output = io.StringIO()
        with mock.patch("builtins.open", side_effect=deny_packet), \
                contextlib.redirect_stdout(output):
            extractor = self.extractor(PERF_HEALER_NAME)
            self.assertEqual(extractor.run_once(), (0, 1, 1))
        self.assertIn("error rebuilding diagnostics", output.getvalue())
        self.assertEqual(self.read_bytes(old), data)
        # Readable again: the next run removes it as obsolete.
        extractor = self.run_extract((0, 0, 0), player=PERF_HEALER_NAME)
        self.assertEqual(extractor.diagnostics_summary["deleted"], 1)
        self.assertFalse(os.path.exists(old))

    def watch_old_log_with_one_failed_publication(self, player):
        """Watch two polls over a rotated-away log whose pull fails to publish once.

        Returns (packet before, [(committed offset, marker present, packet) per poll]).
        """
        old = self.write_pulls(0)
        self.run_extract((0, 1, 0), player=player)
        before = self.read_bytes(self.packet_path()) if player else None
        newer = LogBuilder()
        newer.add(self.T0 + timedelta(hours=2), "COMBAT_LOG_VERSION", "22",
                  "ADVANCED_LOG_ENABLED", "1", "BUILD_VERSION", "12.1.0", "PROJECT_ID", "1")
        self.write_log(newer.data(), name="WoWCombatLog-100126_234000.txt")
        past = time.time() - 3600
        os.utime(old, (past, past))       # the old log is no longer the latest
        extractor = self.extractor(player)
        extractor.prepare(reset_state=True)
        real_copy = wle._copy_atomic
        failed = []

        def fail_once(source, destination):
            if destination.endswith("deaths.json") and "_21-40_" in destination \
                    and not failed:
                failed.append(destination)
                raise OSError("simulated crash: deaths.json copy")
            return real_copy(source, destination)
        polls = []

        def after_poll(_):
            package = [name for name in self.list_outputs(self.raids_dir())
                       if "_21-40_" in name]
            marker = bool(package) and os.path.exists(os.path.join(
                self.raids_dir(), package[0], "analysis", "metadata.json"))
            names = self.packet_names() if player else []
            polls.append((extractor.state.get_offset(old), marker,
                          self.read_bytes(os.path.join(self.diag_dir(), names[0]))
                          if names else None))
        output = io.StringIO()
        diagnostics = self.diag_state()
        with mock.patch.object(wle, "_copy_atomic", side_effect=fail_once), \
                mock.patch.object(wle.time, "sleep", side_effect=after_poll), \
                contextlib.redirect_stdout(output):
            self.assertEqual(extractor.watch(interval=0, max_polls=2), (0, 1, 1))
        self.assert_watch_left_diagnostics(diagnostics, output.getvalue(), extractor)
        self.assertEqual(len(failed), 1)
        self.assertIn("simulated crash: deaths.json copy", output.getvalue())
        size = os.path.getsize(old)
        # Poll 1: the failed pull is not committed; poll 2 replays and republishes it.
        self.assertEqual([poll[:2] for poll in polls], [(0, False), (size, True)])
        return before, [poll[2] for poll in polls]

    def test_watch_replays_a_failed_publication_on_the_next_poll(self):
        before, packets = self.watch_old_log_with_one_failed_publication("Dkyam")
        self.assertEqual(packets, [before, before])
        self.assertEqual(len(json.loads(before)["pulls"]), 1)
        extractor = self.run_extract((0, 0, 0))
        self.assertEqual(extractor.diagnostics_summary["unchanged"], 1)

    def test_watch_replays_a_failed_publication_without_the_flag(self):
        _, packets = self.watch_old_log_with_one_failed_publication(None)
        self.assertEqual(packets, [None, None])
        self.assertFalse(os.path.exists(self.diag_dir()))

    # -- Ctrl+C in --watch -----------------------------------------------------------------
    # Ctrl+C can land between any two statements. The worker it catches mid-poll may be
    # half-updated, so the shared shutdown lets it publish what already saw its END (the
    # replay reuses the same names) but never checkpoints it.
    def pull_markers(self):
        return sorted(name for name in self.list_outputs(self.raids_dir())
                      if os.path.exists(os.path.join(self.raids_dir(), name, "analysis",
                                                     "metadata.json")))

    def watch_until_ctrl_c(self, *patches, player="Dkyam", reset_state=False):
        extractor = self.extractor(player)
        if reset_state:
            extractor.prepare(reset_state=True)
        output = io.StringIO()
        diagnostics = self.diag_state()
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(contextlib.redirect_stdout(output))
            result = extractor.watch(interval=0, max_polls=3)
        # Whatever the interruption, watch never creates, writes or deletes a packet.
        self.assert_watch_left_diagnostics(diagnostics, output.getvalue(), extractor)
        return extractor, result, output.getvalue()

    def write_rotated_pull(self, end=True):
        """One pull in a log that is no longer the latest: published at EOF in-poll."""
        old = self.write_pulls(0) if end else \
            self.write_log(self.add_pull(LogBuilder(), self.T0, end=False).data())
        newer = LogBuilder()
        newer.add(self.T0 + timedelta(hours=2), "COMBAT_LOG_VERSION", "22",
                  "ADVANCED_LOG_ENABLED", "1", "BUILD_VERSION", "12.1.0", "PROJECT_ID", "1")
        self.write_log(newer.data(), name="WoWCombatLog-100126_234000.txt")
        past = time.time() - 3600
        os.utime(old, (past, past))
        return old

    def assert_replay_converges(self, log, expected=(0, 1, 0)):
        self.run_extract(expected)
        self.assertEqual(len(self.pull_markers()), 1)
        self.assertEqual(len(self.packet_names()), 1)
        extractor = self.run_extract((0, 0, 0))
        self.assertEqual(extractor.state.get_offset(log), os.path.getsize(log))

    def test_ctrl_c_mid_poll_publishes_the_ended_pull_without_checkpointing(self):
        log = self.write_pulls(0)
        real_process = wle.FileProcessor.process_new_data

        def interrupt_after_reading(processor):
            real_process(processor)
            raise KeyboardInterrupt
        extractor, result, output = self.watch_until_ctrl_c(mock.patch.object(
            wle.FileProcessor, "process_new_data", interrupt_after_reading))
        # The clean stop still publishes the pull that had its END...
        self.assertEqual(result, (0, 1, 0))
        self.assertEqual(len(self.pull_markers()), 1)
        # ...but the interrupted worker never checkpoints, and the packets wait for a
        # run without --watch.
        self.assertEqual(extractor.state.get_offset(log), 0)
        self.assertEqual(self.packet_names(), [])
        self.assertIn(self.WATCH_LINE, output)
        self.assert_replay_converges(log)

    def test_ctrl_c_at_the_start_of_a_poll_still_finalizes_the_retained_pull(self):
        log = self.write_pulls(0)

        def interrupt(processor):       # only reached for a worker kept from poll 1
            raise KeyboardInterrupt
        extractor, result, _ = self.watch_until_ctrl_c(mock.patch.object(
            wle.FileProcessor, "identity_changed", interrupt))
        self.assertEqual(result, (0, 1, 0))
        self.assertEqual(len(self.pull_markers()), 1)
        self.assertEqual(extractor.state.get_offset(log), 0)
        self.assert_replay_converges(log)

    def ctrl_c_during_publication(self, patch):
        old = self.write_rotated_pull()
        extractor, result, _ = self.watch_until_ctrl_c(patch)
        # Nothing reached the output and nothing past the pull is committed.
        self.assertEqual(result, (0, 0, 0))
        self.assertEqual(self.pull_markers(), [])
        self.assertEqual(extractor.state.get_offset(old), 0)
        self.assertEqual(self.packet_names(), [])
        self.assert_replay_converges(old)

    def test_ctrl_c_during_a_publication_never_commits_past_the_pull(self):
        real_copy = wle._copy_atomic

        def interrupt(source, destination):
            if destination.endswith("deaths.json"):
                raise KeyboardInterrupt
            return real_copy(source, destination)
        self.ctrl_c_during_publication(
            mock.patch.object(wle, "_copy_atomic", side_effect=interrupt))

    def test_ctrl_c_right_after_a_segment_is_detached_never_commits_past_it(self):
        # The tracker has already let go of the segment when the publication starts.
        def interrupt(publisher, segment):
            segment.abandon()
            raise KeyboardInterrupt
        self.ctrl_c_during_publication(
            mock.patch.object(wle.SegmentPublisher, "publish", interrupt))

    def test_ctrl_c_while_dropping_a_replaced_log_never_checkpoints_the_replacement(self):
        log = self.write_pulls(0)
        real_drop = wle.SegmentTracker.drop_open_segment

        def replace_log(_):
            self.write_pulls(5, 10)     # same name, other pulls, twice the size

        def interrupt_after_salvage(tracker):
            real_drop(tracker)
            raise KeyboardInterrupt
        extractor, result, _ = self.watch_until_ctrl_c(
            mock.patch.object(wle.time, "sleep", side_effect=replace_log),
            mock.patch.object(wle.SegmentTracker, "drop_open_segment",
                              interrupt_after_salvage))
        # The old contents' pull was salvaged; the old worker's offset (the old EOF)
        # is not committed against the replacement.
        self.assertEqual(result, (0, 1, 0))
        self.assertEqual(extractor.state.get_offset(log), 0)
        # Both pulls of the replacement are extracted by the next run: none skipped.
        self.run_extract((0, 2, 0))
        markers = self.pull_markers()
        self.assertTrue(any("_21-45_" in name for name in markers), markers)
        self.assertTrue(any("_21-50_" in name for name in markers), markers)

    def test_ctrl_c_between_polls_never_checkpoints_a_log_replaced_meanwhile(self):
        # No worker is running, yet the retained one is stale: the log was replaced
        # after its last poll, so its offset describes the previous contents.
        log = self.write_pulls(0)

        def replace_then_ctrl_c(_):
            self.write_pulls(5, 10)     # same name, other pulls, twice the size
            raise KeyboardInterrupt
        extractor, result, _ = self.watch_until_ctrl_c(
            mock.patch.object(wle.time, "sleep", side_effect=replace_then_ctrl_c))
        self.assertEqual(result, (0, 1, 0))     # the old contents' pull is salvaged
        self.assertEqual(extractor.state.get_offset(log), 0)
        # Both pulls of the replacement are extracted by the next run: none skipped.
        self.run_extract((0, 2, 0))
        markers = self.pull_markers()
        self.assertTrue(any("_21-45_" in name for name in markers), markers)
        self.assertTrue(any("_21-50_" in name for name in markers), markers)

    def test_ctrl_c_after_a_failed_publication_keeps_the_packet(self):
        # A Ctrl+C right after a failed republication: the shutdown never rebuilds from
        # the package left half-way, and the next run replays it.
        old = self.write_rotated_pull()
        self.run_extract((0, 1, 0))
        before = self.read_bytes(self.packet_path())
        real_copy, real_save = wle._copy_atomic, wle.StateStore.save
        failed = []

        def fail_once(source, destination):
            if destination.endswith("deaths.json") and not failed:
                failed.append(destination)
                raise OSError("simulated crash: deaths.json copy")
            return real_copy(source, destination)

        def ctrl_c_after_the_failure(store):
            if failed == failed[:1] and failed:
                failed.append("ctrl+c")     # once: the save that closes the failed poll
                raise KeyboardInterrupt
            return real_save(store)
        extractor, result, output = self.watch_until_ctrl_c(
            mock.patch.object(wle, "_copy_atomic", side_effect=fail_once),
            mock.patch.object(wle.StateStore, "save", ctrl_c_after_the_failure),
            reset_state=True)
        self.assertEqual(result, (0, 0, 1))
        self.assertEqual(self.pull_markers(), [])       # republication cut half-way
        self.assertIn(self.WATCH_LINE, output)
        self.assertTrue(extractor.diagnostics_dirty)
        self.assertEqual(self.read_bytes(self.packet_path()), before)
        # The next clean run replays the pull; the packet comes out the same.
        self.run_extract((0, 1, 0))
        self.assertEqual(len(self.pull_markers()), 1)
        self.assertEqual(self.read_bytes(self.packet_path()), before)
        self.assertEqual(self.run_extract((0, 0, 0)).state.get_offset(old),
                         os.path.getsize(old))

    def test_ctrl_c_while_finalizing_a_vanished_log_still_publishes_its_pull(self):
        # The worker of a deleted log holds the only copy of the pull it retained.
        log = self.write_pulls(0)

        def delete_log(_):
            os.remove(log)

        def interrupt(processor, is_latest, now=None):
            raise KeyboardInterrupt
        extractor, result, _ = self.watch_until_ctrl_c(
            mock.patch.object(wle.time, "sleep", side_effect=delete_log),
            mock.patch.object(wle.FileProcessor, "finish", interrupt))
        self.assertEqual(result, (0, 1, 0))
        self.assertEqual(len(self.pull_markers()), 1)
        # Nothing to replay any more: the next run only builds the packet.
        self.run_extract((0, 0, 0))
        self.assertEqual(len(self.pull_markers()), 1)
        self.assertEqual(len(json.loads(self.read_bytes(self.packet_path()))["pulls"]), 1)

    def test_ctrl_c_after_counting_never_reports_a_pull_twice(self):
        old = self.write_rotated_pull()
        real_take = wle.FileProcessor.take_counts

        def interrupt_after_counting(processor):
            counts = real_take(processor)
            if counts != (0, 0):
                raise KeyboardInterrupt
            return counts
        extractor, result, _ = self.watch_until_ctrl_c(mock.patch.object(
            wle.FileProcessor, "take_counts", interrupt_after_counting))
        # Counted at most once (the interrupted count itself may be lost).
        self.assertLessEqual(result[1], 1)
        self.assertEqual(result[2], 0)
        self.assertEqual(len(self.pull_markers()), 1)
        # The poll had committed after publishing, before it was interrupted.
        self.assertEqual(extractor.state.get_offset(old), os.path.getsize(old))
        self.run_extract((0, 0, 0))

    def test_take_counts_is_single_use(self):
        self.write_pulls(0, 5)
        extractor = self.extractor()
        with contextlib.redirect_stdout(io.StringIO()):
            processor = extractor._new_processor(self.log_path(LOG_NAME))
            processor.process_new_data()
            processor.finish(is_latest=False)
        self.assertEqual(processor.take_counts(), (0, 2))
        self.assertEqual(processor.take_counts(), (0, 0))
        self.assertEqual(processor.counts(), (0, 0))

    def fail_second_pull_publication(self):
        real = wle._copy_atomic

        def fail(source, destination):
            if destination.endswith("deaths.json") and "_21-45_" in destination:
                raise OSError("simulated crash: deaths.json copy")
            return real(source, destination)
        return mock.patch.object(wle, "_copy_atomic", side_effect=fail)

    def test_processing_error_leaves_diagnostics_untouched_until_a_clean_run(self):
        self.write_pulls(0, 5)
        self.run_extract((0, 2, 0))
        path = self.packet_path()
        data = self.read_bytes(path)
        for mode in ("run_once", "watch"):
            extractor = self.extractor()
            extractor.prepare(reset_state=True)      # republish both pulls
            output = io.StringIO()
            diagnostics = self.diag_state()
            with self.fail_second_pull_publication(), contextlib.redirect_stdout(output):
                if mode == "run_once":
                    self.assertEqual(extractor.run_once(), (0, 0, 1))
                else:
                    self.assertGreaterEqual(extractor.watch(interval=0, max_polls=1)[2], 1)
            text = output.getvalue()
            if mode == "run_once":
                self.assertIn("diagnostics not rebuilt because of 1 processing error(s)",
                              text)
                self.assertNotIn("Diagnostics", text.replace("diagnostics not", ""))
            else:
                self.assert_watch_left_diagnostics(diagnostics, text, extractor)
            # The half republished pull is still in the untouched packet.
            self.assertFalse(os.path.exists(os.path.join(
                self.raids_dir(), [name for name in self.list_outputs(self.raids_dir())
                                   if "_21-45_" in name][0], "analysis", "metadata.json")))
            self.assertEqual(self.read_bytes(path), data, mode)
            self.assertEqual(len(json.loads(data)["pulls"]), 2)
            self.assertTrue(extractor.diagnostics_dirty)
            # The next clean run republishes the pull and rebuilds the same packet (watch
            # had already committed the offset past the first pull).
            extractor = self.run_extract((0, 2 if mode == "run_once" else 1, 0))
            self.assertEqual(self.read_bytes(path), data, mode)
            self.assertEqual(extractor.diagnostics_summary["unchanged"], 1)

    def run_full_profile(self, subdir, player):
        """run_once with --analysis (full body too) into Output-<subdir>."""
        self.output_dir = os.path.join(self.root, "Output-" + subdir)
        self.state_path = os.path.join(self.output_dir, wle.STATE_FILENAME)
        options = wle.OutputOptions(analysis=True, performance_player=player)
        extractor = wle.Extractor(self.log_dir, self.output_dir, state_path=self.state_path,
                                  verbose=False, output_options=options)
        extractor.prepare()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(extractor.run_once(), (0, 2, 0))
        outputs = {}
        for name in self.list_outputs(self.raids_dir()):
            directory = os.path.join(self.raids_dir(), name)
            if not os.path.isdir(directory):
                outputs[name] = self.read_bytes(directory)
                continue
            for artifact in ("combat.txt", "summary.json", "players.json", "deaths.json"):
                outputs[name + "/" + artifact] = self.read_bytes(
                    os.path.join(directory, "analysis", artifact))
        return extractor, outputs

    def test_failing_performance_analysis_never_breaks_the_extraction(self):
        self.write_pulls(0, 5)
        _, plain = self.run_full_profile("plain", None)
        real_observe = wle.PerformanceAccumulator.observe
        calls = {"observe": 0, "build": 0}

        def observe(accumulator, *args):
            calls["observe"] += 1
            if calls["observe"] == 4:
                raise KeyError("simulated observe bug")
            return real_observe(accumulator, *args)

        def build(*args):
            calls["build"] += 1
            if calls["build"] == 1:
                raise RuntimeError("x" * 400)
            return wle._arcane_section(*args)
        cases = (("observe", mock.patch.object(wle.PerformanceAccumulator, "observe",
                                                autospec=True, side_effect=observe),
                  "KeyError", "'simulated observe bug'"),
                 ("result", mock.patch.dict(wle.ARCANE_RULES, {"build": build}),
                  "RuntimeError", "x" * 300))
        for phase, patch, error_type, message in cases:
            with patch:
                extractor, outputs = self.run_full_profile(phase, "Dkyam")
            # Body, combat.txt and the other analysis files: as without the flag.
            self.assertEqual(outputs, plain, phase)
            failed, healthy = sorted(name for name in self.list_outputs(self.raids_dir())
                                     if os.path.isdir(os.path.join(self.raids_dir(), name)))
            analysis = os.path.join(self.raids_dir(), failed, "analysis")
            performance = self.read_json(os.path.join(analysis, "performance.json"))
            self.assertEqual(performance["player"], {
                "selector": "Dkyam", "status": "error", "guid": None, "name": None,
                "candidates": []}, phase)
            self.assertEqual(performance["error"], {"type": error_type, "message": message,
                                                    "phase": phase})
            for key in ("performance_schema_version", "extractor_version", "fingerprint",
                        "rules", "segment", "source", "game"):
                self.assertIn(key, performance)
            self.assertNotIn("damage", performance)
            self.assertEqual(self.read_json(os.path.join(analysis, "metadata.json"))
                             ["performance"]["player_status"], "error")
            self.assertEqual(self.read_json(os.path.join(
                self.raids_dir(), healthy, "analysis", "performance.json"))
                ["player"]["status"], "resolved")
            packet = json.loads(self.read_bytes(self.packet_path()))
            self.assertEqual([pull["segment_id"] for pull in packet["pulls"]],
                             [self.read_json(os.path.join(self.raids_dir(), healthy,
                                                          "analysis", "metadata.json"))
                              ["segment_id"]])
            self.assertEqual([(row["segment_id"], row["status"])
                              for row in packet["pulls_without_player"]],
                             [(performance["segment"]["segment_id"], "error")])
            self.assertEqual(extractor.diagnostics_summary["failed"], 1)
            self.assertIn("1 of 2 pulls: 0 absent, 0 ambiguous, 1 failed",
                          wle.diagnostics_summary_line(extractor.diagnostics_summary))

    def test_incomplete_pull_completed_later_has_one_entry(self):
        builder = self.add_pull(LogBuilder(), self.T0, end=False)
        path = self.write_log(builder.data())
        stale = time.time() - (wle.STALE_SECONDS + 60)
        os.utime(path, (stale, stale))
        self.run_extract((0, 1, 0))
        packet = json.loads(self.read_bytes(self.packet_path()))
        self.assertEqual([(pull["result"], pull["complete"]) for pull in packet["pulls"]],
                         [("incomplete", False)])
        self.assertEqual(packet["groups"][0]["incomplete"], 1)
        self.assertEqual(packet["groups"][0]["metrics"]["dps_encounter"]["excluded"],
                         [{"pull_id": "p01", "reason": "incomplete"}])
        self.write_pulls(0)
        extractor = self.extractor()
        extractor.prepare(reset_state=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(extractor.run_once(), (0, 1, 0))
        packet = json.loads(self.read_bytes(self.packet_path()))
        self.assertEqual([(pull["result"], pull["complete"]) for pull in packet["pulls"]],
                         [("kill", True)])
        self.assertEqual(len(packet["pulls"][0]["sources"]), 1)

    def test_duplicate_logs_give_identical_packets_whatever_the_listing_order(self):
        other = "WoWCombatLog-100126_200000.txt"
        self.write_pulls(0)
        partial = self.add_pull(LogBuilder(), self.T0, end=False)
        path = self.write_log(partial.data(), name=other)
        stale = time.time() - (wle.STALE_SECONDS + 60)
        os.utime(path, (stale, stale))
        self.run_extract((0, 2, 0))
        data = self.read_bytes(self.packet_path())
        packet = json.loads(data)
        self.assertEqual(len(packet["pulls"]), 1)
        self.assertEqual([row["file"] for row in packet["pulls"][0]["sources"]],
                         [LOG_NAME, other])
        os.remove(self.packet_path())
        real = os.listdir
        with mock.patch.object(wle.os, "listdir",
                               side_effect=lambda p: list(reversed(real(p)))):
            self.run_extract((0, 0, 0))
        self.assertEqual(self.read_bytes(self.packet_path()), data)

    def test_isolation_from_cleanup_purge_and_runs_without_the_flag(self):
        self.write_pulls(0)
        self.run_extract((0, 1, 0))
        before = self.snapshot(self.diag_dir())
        publisher = wle.SegmentPublisher(self.output_dir, verbose=False,
                                         output_options=wle.OutputOptions(analysis_only=True))
        publisher.cleanup_partials()
        package = self.list_outputs(self.raids_dir())[0]
        segment_id = self.read_json(os.path.join(self.raids_dir(), package, "analysis",
                                                 "metadata.json"))["segment_id"]
        publisher._purge_stale(self.raids_dir(), segment_id, "another-name")
        self.assertEqual(self.list_outputs(self.raids_dir()), [])
        self.assertEqual(self.snapshot(self.diag_dir()), before)
        self.run_extract((0, 1, 0), player=None)
        self.assertEqual(self.snapshot(self.diag_dir()), before)

    def test_run_without_the_flag_never_creates_diagnostics(self):
        self.write_pulls(0)
        self.run_extract((0, 1, 0), player=None)
        self.assertFalse(os.path.exists(self.diag_dir()))

    def run_cli(self, player):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = wle.run(["--log-dir", self.log_dir, "--output", self.output_dir,
                            "--config", os.path.join(self.root, "config.json"),
                            "--analysis-only", "--performance-player", player])
        self.assertEqual(code, 0)
        return output.getvalue()

    def test_cli_summary_line(self):
        self.write_pulls(0, 5)
        text = self.run_cli("Nobody")
        self.assertIn("Diagnostics: 0 packet(s) written, 0 unchanged, 0 removed; player "
                      "resolved in 0 of 2 pulls: 2 absent, 0 ambiguous", text)
        self.assertEqual(self.packet_names(), [])
        text = self.run_cli("Dkyam")
        self.assertIn("Diagnostics: 1 packet(s) written, 0 unchanged, 0 removed; player "
                      "resolved in 2 of 2 pulls\n", text)


# --- raid performance diagnostics: end to end and the checked-in example -----------------

EXAMPLE_PACKET = os.path.join(PACKAGE_DIR, "examples", "diagnostic_packet_example.json")
SECOND_BOSS = "Creature-0-3109-3004-27445-261478-00013EBFBC"
SECOND_BOSS_NAME = "Vexmora"


class DiagnosticEndToEndTests(ExtractorTestCase):
    """A synthetic raid night through `run()`: performance.json and the packet agree.

    Fixed timestamps and amounts only, so the packet is byte-stable and is checked in as
    examples/diagnostic_packet_example.json (WLE_UPDATE_EXAMPLES=1 rewrites it).
    """

    START = datetime(2026, 10, 1, 21, 0, 0)
    BOSSES = {3421: (PERF_BOSS, PERF_BOSS_NAME), 3422: (SECOND_BOSS, SECOND_BOSS_NAME)}
    # (minutes after START, encounter, duration s, kill, player death s, damage scale)
    PULLS = ((0, 3421, 85, False, None, 3),
             (4, 3421, 18, False, None, 0),       # shorter than MIN_COMPARABLE_SECONDS
             (8, 3421, 120, True, None, 5),
             (14, 3422, 70, False, 31.5, 2),      # the player dies mid-fight
             (19, 3422, 100, True, None, 4))

    damage = PerformanceAccumulatorTests.damage
    cast = PerformanceAccumulatorTests.cast
    aura = PerformanceAccumulatorTests.aura
    energize = PerformanceAccumulatorTests.energize
    died = PerformanceAccumulatorTests.died
    heal_on_player = PerformanceAccumulatorTests.heal_on_player
    combatant = PerformanceAccumulatorTests.combatant
    read_bytes = staticmethod(PerformancePublicationTests.read_bytes)

    def pull_lines(self, encounter, duration, kill, death, scale):
        """(seconds from ENCOUNTER_START, event, fields) of one pull, in time order."""
        boss, boss_name = self.BOSSES[encounter]

        def hit(t, spell, amount):
            return self.damage(t, spell, amount * scale, dest=boss, dest_name=boss_name)

        def cast(t, spell, mana):
            return self.cast(t, "success", spell, power=(0, mana, 1000), dest=boss,
                             dest_name=boss_name)
        player = [
            self.aura(-4, "SPELL_AURA_APPLIED", 263725),                    # Clearcasting
            self.aura(1, "SPELL_AURA_APPLIED_DOSE", 263725, stacks=2),
            self.aura(1.2, "SPELL_AURA_REFRESH", 263725),                  # at 2 stacks
            self.cast(1.5, "start", 365350, dest=boss, dest_name=boss_name),
            cast(2, 365350, 950),                                           # Arcane Surge
            self.aura(2, "SPELL_AURA_APPLIED", 365362),
            cast(3, 321507, 900),                                           # Touch
            self.aura(3, "SPELL_AURA_APPLIED", 210824, dest=boss, dest_name=boss_name,
                      dest_flags=PERF_HOSTILE),
            self.energize(3, 321507, 4, 0, 16),
            cast(4, 5143, 880),                                             # Missiles
            self.aura(4, "SPELL_AURA_REMOVED_DOSE", 263725, stacks=1),
            hit(4.2, 7268, 1000), hit(4.4, 7268, 1000), hit(4.6, 7268, 1100),
            cast(6, 44425, 860), hit(6.3, 44425, 4000),                     # Barrage
            self.energize(7, 153626, 2, 0, 16), self.energize(7.5, 153626, 2, 0, 16),
            self.energize(8, 153626, 0, 1, 16),
            self.aura(9, "SPELL_AURA_REFRESH", 263725),
            self.aura(12, "SPELL_AURA_REMOVED", 365362)]
        for t in range(14, duration - 1, 5):
            # Barrage at 4 charges, Missiles, charges back to 4 (one over the cap).
            player += [cast(t, 44425, 780), hit(t + 0.3, 44425, 2500 + t),
                       cast(t + 1, 5143, 800), hit(t + 1.2, 7268, 900),
                       hit(t + 1.4, 7268, 900), self.energize(t + 2, 153626, 2, 0, 16),
                       self.energize(t + 2.5, 153626, 2, 0, 16),
                       self.energize(t + 3, 153626, 0, 1, 16), cast(t + 3.2, 1449, 760)]
        if death is not None:
            player = [line for line in player if line[0] < death] + [self.died(death)]
        if scale == 0:
            player = [line for line in player if line[1] != "SPELL_DAMAGE"]
        noise = [
            # A second player and a friendly NPC hitting the boss, and a heal on us.
            self.damage(5, 585, 3000, source=PERF_HEALER, source_name=PERF_HEALER_NAME,
                        dest=boss, dest_name=boss_name),
            self.damage(5.5, 1000, 777, source=PERF_FRIEND, source_name="Guardian",
                        source_flags=PERF_FRIENDLY, dest=boss, dest_name=boss_name),
            self.heal_on_player(8.5),
            # Post-context: neither in the totals nor in the pull duration.
            self.damage(duration + 2, 44425, 9999, dest=boss, dest_name=boss_name)]
        frame = [(0, "ENCOUNTER_START", [str(encounter), q(boss_name), "15", "20", "2900"]),
                 self.combatant(0.5, auras=[]),
                 (duration, "ENCOUNTER_END", [str(encounter), q(boss_name), "15", "20",
                                              "1" if kill else "0", str(duration * 1000)])]
        lines = sorted(frame[:2] + player + noise[:-1], key=lambda line: line[0])
        return lines + frame[2:] + noise[-1:]

    def write_session(self):
        builder = LogBuilder()
        builder.add(self.START - timedelta(seconds=30), "COMBAT_LOG_VERSION", "22",
                    "ADVANCED_LOG_ENABLED", "1", "BUILD_VERSION", "12.1.0", "PROJECT_ID", "1")
        for index, (minutes, *pull) in enumerate(self.PULLS):
            base = self.START + timedelta(minutes=minutes)
            for seconds, event, fields in self.pull_lines(*pull):
                mark = {"ENCOUNTER_START": "start%d" % index,
                        "ENCOUNTER_END": "end%d" % index}.get(event)
                builder.add(base + timedelta(seconds=seconds), event, *fields, mark=mark)
        self.write_log(builder.data())
        return builder

    def run_cli(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = wle.run(["--log-dir", self.log_dir, "--output", self.output_dir,
                            "--config", os.path.join(self.root, "config.json"),
                            "--analysis-only", "--performance-player", "Dkyam"])
        self.assertEqual(code, 0, output.getvalue())
        return output.getvalue()

    def packet_bytes(self):
        directory = os.path.join(self.output_dir, wle.DIAGNOSTICS_DIR_NAME)
        names = self.list_outputs(directory)
        self.assertEqual(len(names), 1, names)
        self.assertTrue(names[0].endswith(wle.PACKET_SUFFIX))
        return self.read_bytes(os.path.join(directory, names[0]))

    def performance_files(self):
        """{package name: performance.json bytes}, one per published pull."""
        files = {}
        for name in self.list_outputs(self.raids_dir()):
            path = os.path.join(self.raids_dir(), name, "analysis", "performance.json")
            if os.path.isdir(os.path.join(self.raids_dir(), name)):
                files[name] = self.read_bytes(path)
        return files

    def assertNoTempPath(self, data, label):
        for path in (self.root, os.path.basename(self.root)):
            for text in (path, json.dumps(path)[1:-1], path.replace("\\", "/")):
                self.assertNotIn(text.encode("utf-8"), data, label)

    def test_packet_agrees_with_every_performance_json(self):
        builder = self.write_session()
        source = builder.data()
        text = self.run_cli()
        self.assertIn("Processed: 0 Mythic+ runs, 5 raid pulls, 0 errors", text)
        files = self.performance_files()
        self.assertEqual(len(files), 5, sorted(files))
        performances = {}
        for name, data in files.items():
            self.assertNoTempPath(data, name)
            performances[name] = json.loads(data, parse_constant=self.fail)
        data = self.packet_bytes()
        self.assertNoTempPath(data, "packet")
        packet = json.loads(data, parse_constant=self.fail)
        self.assertEqual(packet["budget"]["actual_bytes"], len(data))
        self.assertTrue(packet["complete"])
        self.assertEqual([row["build_version"] for row in packet["game"]], ["12.1.0"])
        self.assertEqual(packet["player"]["guid"], DKYAM)

        # Every pull summary carries the values of its own performance.json.
        self.assertEqual(len(packet["pulls"]), 5)
        by_pull = {}
        for index, pull in enumerate(packet["pulls"]):
            source_ref, = pull["sources"]
            performance = performances[source_ref["published_name"]]
            by_pull[pull["pull_id"]] = performance
            segment = performance["segment"]
            self.assertEqual(pull["segment_id"], segment["segment_id"])
            self.assertEqual(pull["effective"], performance["damage"]["total_effective"])
            self.assertEqual(pull["duration_s"], segment["duration_ms"] / 1000)
            self.assertEqual(pull["deaths_s"],
                             [death["t_s"] for death in performance["life"]["deaths"]])
            self.assertEqual(pull["casts_success"], performance["casts"]["total_success"])
            self.assertEqual(pull["result"], segment["result"])
            # The offsets point at this pull's ENCOUNTER_START / ENCOUNTER_END lines.
            self.assertEqual(source_ref["file"], LOG_NAME)
            for key, mark, event in (
                    ("encounter_start_offset", "start%d" % index, "ENCOUNTER_START"),
                    ("encounter_end_offset", "end%d" % index, "ENCOUNTER_END")):
                self.assertEqual(source_ref[key], builder.marks[mark][0], key)
                line = source[source_ref[key]:].split(b"\r\n", 1)[0].decode("utf-8")
                self.assertTrue(line.split("  ", 1)[1].startswith(
                    "%s,%d," % (event, segment["encounter_id"])), (key, line))
                self.assertEqual(wle.format_timestamp(
                    wle.parse_timestamp(line.split("  ", 1)[0], 2026)),
                    segment["start_time"] if event == "ENCOUNTER_START" else
                    segment["end_time"])
        self.assertEqual([pull["result"] for pull in packet["pulls"]],
                         ["wipe", "wipe", "kill", "wipe", "kill"])
        self.assertEqual(packet["pulls"][3]["deaths_s"], [31.5])
        self.assertTrue(all(pull["effective"] > 0 for index, pull in
                            enumerate(packet["pulls"]) if index != 1))
        self.assertEqual(packet["pulls"][1]["effective"], 0)

        # Groups: one per boss, counts recomputed from the performance.json files.
        self.assertEqual([group["encounter_id"] for group in packet["groups"]], [3421, 3422])
        for group in packet["groups"]:
            members = [by_pull[pull_id] for pull_id in group["pulls"]]
            self.assertTrue(all(performance["segment"]["encounter_id"] ==
                                group["encounter_id"] for performance in members))
            results = [performance["segment"]["result"] for performance in members]
            self.assertEqual((group["attempts"], group["kills"], group["wipes"]),
                             (len(members), results.count("kill"), results.count("wipe")))
            effective = sum(performance["damage"]["total_effective"]
                            for performance in members)
            seconds = sum(performance["segment"]["duration_ms"] for performance in members)
            pooled = group["pooled"]["dps_encounter_pooled"]
            self.assertEqual((pooled["numerator"], pooled["denominator"], pooled["value"]),
                             (effective, seconds / 1000,
                              round(effective / (seconds / 1000), 3)))
            self.assertEqual(pooled["pulls"], group["pulls"])
        first, second = packet["groups"]
        self.assertEqual(first["pulls"], ["p01", "p02", "p03"])
        self.assertEqual(second["pulls"], ["p04", "p05"])

        # The short pull: in attempts and duration_s, out of the per-pull rates.
        short = {"pull_id": "p02", "reason": "pull_shorter_than_min_comparable"}
        self.assertEqual(first["duration_s"]["n"], 3)
        for name in ("dps_encounter", "dps_while_alive", "casts_per_minute"):
            self.assertIn(short, first["metrics"][name]["excluded"], name)
            self.assertEqual(first["metrics"][name]["n"], 2, name)
        self.assertNotIn("p02", [row["pull_id"] for row in first["representative_pulls"]])

        # The Arcane section is populated in the observations.
        observations = {row["id"]: row for row in packet["observations"]}
        self.assertGreater(observations["g1.burst_window_casts"]["numerator"], 0)
        self.assertGreater(observations["g1.charge_over_energize"]["denominator"], 0)
        self.assertGreater(observations["g1.clearcasting_refresh_at_max"]["denominator"], 0)
        self.assertIn("p04 at 0.45 (wipe)", observations["g2.deaths_before_end"]["statement"])

        # Byte for byte the checked-in example.
        if os.environ.get("WLE_UPDATE_EXAMPLES") == "1":
            os.makedirs(os.path.dirname(EXAMPLE_PACKET), exist_ok=True)
            with open(EXAMPLE_PACKET, "wb") as handle:
                handle.write(data)
        expected = self.read_bytes(EXAMPLE_PACKET) if os.path.exists(EXAMPLE_PACKET) \
            else None
        self.assertTrue(
            expected == data,
            "%s is missing or differs from the packet this fixture produces. If the change "
            "is intended, regenerate it by running this test with the environment variable "
            "WLE_UPDATE_EXAMPLES=1 (from the repo root: WLE_UPDATE_EXAMPLES=1 python -m "
            "unittest discover -s WoWLogExtractor/tests -k DiagnosticEndToEndTests; see "
            "examples/README.md) and review the diff." % EXAMPLE_PACKET)


if __name__ == "__main__":
    unittest.main()
