import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from transit_feeds.serviceday import (
    ISSUE_AMBIGUOUS_FOLD,
    ISSUE_BAD_CLOCK_FORMAT,
    ISSUE_CLOCK_OUT_OF_RANGE,
    ISSUE_NONEXISTENT_GAP,
    ISSUE_UNKNOWN_ZONE,
    STATUS_AMBIGUOUS,
    STATUS_DEFINITE,
    STATUS_INVALID,
    STATUS_NONEXISTENT,
    cache_key_for,
    interpret_departure,
    parse_clock,
    parse_service_day,
    tzdata_version,
)


class ClockParsingTests(unittest.TestCase):
    def test_26_30_keeps_verbatim_and_elapsed_minutes(self):
        clock = parse_clock("26:30")
        self.assertEqual(clock.verbatim, "26:30")
        self.assertEqual(clock.elapsed_minutes, 26 * 60 + 30)
        self.assertEqual((clock.hour, clock.minute), (26, 30))

    def test_rejects_bad_formats_explicitly(self):
        for raw in ("9点", "24:60", "", "abc", "1:2", 2630):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    parse_clock(raw)

    def test_hour_above_limit_is_out_of_range_not_wraparound(self):
        with self.assertRaisesRegex(Exception, "录入错误"):
            parse_clock("48:00")

    def test_service_day_requires_real_calendar_date(self):
        parse_service_day("2026-11-01")
        for raw in ("2026-13-01", "2026/11/01", "not-a-date"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    parse_service_day(raw)


class InterpretationTests(unittest.TestCase):
    def test_past_midnight_26_30_is_definite_and_preserves_source(self):
        result = interpret_departure("2026-10-31", "26:30", "Europe/Berlin", "winter-2")
        self.assertEqual(result.status, STATUS_DEFINITE)
        self.assertEqual(result.source_departure, "26:30")
        self.assertTrue(result.crosses_midnight)
        self.assertEqual(result.wall_local_iso, "2026-11-01T02:30:00")
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].utc_iso, "2026-11-01T01:30:00Z")
        self.assertIn("tzdata", result.zone_rule)

    def test_fold_keeps_two_distinct_utc_candidates(self):
        result = interpret_departure("2026-10-25", "02:30", "Europe/Berlin", "winter-1")
        self.assertEqual(result.status, STATUS_AMBIGUOUS)
        self.assertEqual(result.issue_code, ISSUE_AMBIGUOUS_FOLD)
        self.assertEqual(len(result.candidates), 2)
        offsets = {candidate.utc_offset_minutes for candidate in result.candidates}
        self.assertEqual(offsets, {120, 60})
        utc_values = [candidate.utc_iso for candidate in result.candidates]
        self.assertEqual(utc_values, sorted(utc_values))
        # 两个候选与 Python 原生 fold 语义一致
        naive = datetime(2026, 10, 25, 2, 30)
        expected = {
            naive.replace(tzinfo=ZoneInfo("Europe/Berlin"), fold=fold).astimezone(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
            for fold in (0, 1)
        }
        self.assertEqual(set(utc_values), expected)

    def test_spring_forward_gap_is_nonexistent_without_silent_default(self):
        result = interpret_departure("2027-03-28", "02:30", "Europe/Berlin", "spring")
        self.assertEqual(result.status, STATUS_NONEXISTENT)
        self.assertEqual(result.candidates, ())
        self.assertEqual(result.issue_code, ISSUE_NONEXISTENT_GAP)
        self.assertIsNotNone(result.gap_bracket)
        self.assertLess(result.gap_bracket.earliest_utc_iso, result.gap_bracket.latest_utc_iso)
        self.assertIn("禁止", result.detail)

    def test_zone_without_dst_is_definite(self):
        result = interpret_departure("2026-11-01", "00:10", "Asia/Shanghai", "autumn")
        self.assertEqual(result.status, STATUS_DEFINITE)
        self.assertEqual(result.candidates[0].utc_iso, "2026-10-31T16:10:00Z")
        self.assertEqual(result.candidates[0].utc_offset_minutes, 480)
        self.assertFalse(result.crosses_midnight)

    def test_24_00_lands_on_next_calendar_day(self):
        result = interpret_departure("2026-11-15", "24:00", "Asia/Shanghai")
        self.assertEqual(result.status, STATUS_DEFINITE)
        self.assertEqual(result.wall_local_iso, "2026-11-16T00:00:00")
        self.assertTrue(result.crosses_midnight)

    def test_invalid_inputs_are_marked_not_defaulted(self):
        cases = [
            ("2026-11-01", "9点", "Europe/Berlin", ISSUE_BAD_CLOCK_FORMAT),
            ("2026-11-01", "48:00", "Europe/Berlin", ISSUE_CLOCK_OUT_OF_RANGE),
            ("2026-13-01", "01:00", "Europe/Berlin", "bad_service_day"),
            ("2026-11-01", "01:00", "Moon/Sea", ISSUE_UNKNOWN_ZONE),
        ]
        for day, departure, zone, issue in cases:
            with self.subTest(day=day, departure=departure, zone=zone):
                result = interpret_departure(day, departure, zone)
                self.assertEqual(result.status, STATUS_INVALID)
                self.assertEqual(result.issue_code, issue)
                self.assertEqual(result.candidates, ())
                self.assertIsNone(result.wall_local_iso)

    def test_cache_key_stable_and_sensitive_to_tz_rule(self):
        first = interpret_departure("2026-10-31", "26:30", "Europe/Berlin", "winter-2")
        second = interpret_departure("2026-10-31", "26:30", "Europe/Berlin", "winter-3")
        self.assertEqual(cache_key_for(first), cache_key_for(interpret_departure(
            "2026-10-31", "26:30", "Europe/Berlin", "winter-2")))
        # 日历版本是解释依据的一部分，版本变化不沿用旧缓存键
        self.assertNotEqual(cache_key_for(first), cache_key_for(second))
        self.assertNotEqual("unknown", tzdata_version())


if __name__ == "__main__":
    unittest.main()
