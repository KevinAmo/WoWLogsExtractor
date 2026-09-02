#!/usr/bin/env python3
"""Unit tests for WoWLogExtractor.

Stdlib unittest only. Run from the repo root with:

    python -m unittest discover -s WoWLogExtractor/tests -v

Every test runs inside a tempfile.TemporaryDirectory() sandbox (fake log dir,
output dir and state.json path) -- nothing here touches the real script
directory, config.json, or D:\\BattleNet.
"""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
