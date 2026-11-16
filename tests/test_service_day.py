import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from transit_feeds.intake import Intake
from transit_feeds.origin import load_scenario, serve
from transit_feeds.publishing import PublishError, Store
from transit_feeds.service_day import (
    DocumentError, coverage_gap, interpret_departure, load_calendar,
    load_timetable, parse_clock, parse_date,
)
from transit_feeds.timezones import ZoneRulesError, resolve_local, zone_rules


FIXTURES = Path(__file__).parent.parent / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ClockTests(unittest.TestCase):
    def test_extended_hours_are_not_wrapped(self):
        self.assertEqual(parse_clock("26:30"), 26 * 3600 + 30 * 60)
        self.assertEqual(parse_clock("27:05"), 27 * 3600 + 5 * 60)
        self.assertEqual(parse_clock("00:10"), 600)

    def test_rejects_free_form_or_invalid_times(self):
        for text in ("", "8", "25", "12:60", "12:3", "abc", "12:00:60", None, 1230):
            with self.subTest(text=text), self.assertRaises(DocumentError):
                parse_clock(text)

    def test_rejects_calendar_invalid_dates(self):
        for text in ("2026-02-30", "2026-1-1", "20261001", None):
            with self.subTest(text=text), self.assertRaises(DocumentError):
                parse_date(text)


class ResolveTests(unittest.TestCase):
    def test_2600_style_time_belongs_to_the_next_civil_day(self):
        result = resolve_local(date(2026, 10, 31), 26 * 3600 + 30 * 60, "Europe/Berlin")
        self.assertEqual(result["status"], "unique")
        self.assertTrue(result["extended_day"])
        self.assertEqual(result["civil_day"], "2026-11-01")
        self.assertEqual([item["utc"] for item in result["candidates"]],
                         ["2026-11-01T01:30:00+00:00"])

    def test_daylight_saving_rollback_returns_two_instants(self):
        result = resolve_local(date(2026, 10, 25), 2 * 3600 + 30 * 60, "Europe/Berlin")
        self.assertEqual(result["status"], "repeated")
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual({item["utc"] for item in result["candidates"]},
                         {"2026-10-25T00:30:00+00:00", "2026-10-25T01:30:00+00:00"})
        self.assertTrue(all(item["ambiguous"] and item["chosen"] is None
                            for item in result["candidates"]))

    def test_skipped_wall_time_has_no_candidate(self):
        result = resolve_local(date(2026, 3, 29), 2 * 3600 + 30 * 60, "Europe/Berlin")
        self.assertEqual(result["status"], "non_existent")
        self.assertEqual(result["candidates"], [])

    def test_southern_hemisphere_and_flat_zones(self):
        self.assertEqual(
            resolve_local(date(2026, 10, 4), 2 * 3600 + 30 * 60, "Australia/Sydney")["status"],
            "non_existent",
        )
        shanghai = resolve_local(date(2026, 11, 1), 600, "Asia/Shanghai")
        self.assertEqual(shanghai["status"], "unique")
        self.assertEqual(shanghai["candidates"][0]["utc"], "2026-10-31T16:10:00+00:00")

    def test_source_offset_selects_and_is_never_overridden(self):
        result = resolve_local(date(2026, 10, 25), 9000, "Europe/Berlin", 7200)
        selected = [item for item in result["candidates"] if item["chosen"] == "source_offset"]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["utc"], "2026-10-25T00:30:00+00:00")
        self.assertEqual(result["status"], "unique")
        with self.assertRaises(ZoneRulesError):
            resolve_local(date(2026, 10, 25), 9000, "Europe/Berlin", 5 * 3600)

    def test_rules_are_recorded_not_assumed(self):
        rules = zone_rules("Europe/Berlin")
        self.assertEqual(rules["zone"], "Europe/Berlin")
        self.assertTrue(rules["tzdata_version"])
        self.assertEqual(len(rules["zone_digest"]), 64)
        with self.assertRaises(ZoneRulesError):
            zone_rules("Europe/Nowhere")
        with self.assertRaises(ZoneRulesError):
            zone_rules("../etc/passwd")


class DocumentTests(unittest.TestCase):
    def test_timetable_keeps_source_text_and_rejects_bad_input(self):
        timetable = load_timetable(fixture("timetable-v1.json"))
        night = next(item for item in timetable["departures"] if item["trip"] == "N12")
        self.assertEqual(night["source_time"], "26:30")
        self.assertEqual(night["seconds"], 95400)
        broken = fixture("timetable-v1.json")
        broken["departures"][0]["operator"] = "unknown"
        with self.assertRaisesRegex(DocumentError, "not declared"):
            load_timetable(broken)

    def test_duplicate_departures_are_rejected(self):
        broken = fixture("timetable-v1.json")
        broken["departures"].append(dict(broken["departures"][0]))
        with self.assertRaisesRegex(DocumentError, "duplicate"):
            load_timetable(broken)

    def test_zone_conflict_is_rejected(self):
        broken = fixture("timetable-v1.json")
        broken["departures"][0]["zone"] = "Europe/London"
        with self.assertRaisesRegex(DocumentError, "conflicts with operator zone"):
            load_timetable(broken)

    def test_calendar_scope_must_reference_declared_operator(self):
        broken = fixture("calendar-v1.json")
        broken["exceptions"][0]["scope"]["operator"] = "ghost"
        with self.assertRaisesRegex(DocumentError, "conflicts"):
            load_calendar(broken)

    def test_closed_service_is_explicit(self):
        timetable = load_timetable(fixture("timetable-v1.json"))
        calendar = load_calendar(fixture("calendar-v1.json"))
        s7 = next(item for item in timetable["departures"] if item["trip"] == "S7")
        from transit_feeds.service_day import exception_for
        evidence = interpret_departure(s7, exception_for(calendar, s7))
        self.assertEqual(evidence["status"], "service_closed")
        self.assertEqual(evidence["candidates"], [])
        self.assertEqual(evidence["closure"]["reason"], "engineering works")

    def test_coverage_gap_lists_service_days(self):
        timetable = load_timetable(fixture("timetable-v1.json"))
        calendar = load_calendar(fixture("calendar-v1.json"))
        calendar["valid_from"] = date(2026, 10, 5)
        self.assertEqual(coverage_gap(timetable, calendar), [date(2026, 10, 4)])
        calendar["valid_from"] = date(2026, 11, 1)
        self.assertEqual(coverage_gap(timetable, calendar),
                         [date(2026, 10, 4), date(2026, 10, 25), date(2026, 10, 26),
                          date(2026, 10, 31)])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")
        self.addCleanup(self.store.close)

    def record(self, name, route="/doc"):
        document = fixture(name)
        return self.store.record_document(route, 200, document)

    def test_incompatible_pair_leaves_current_version_untouched(self):
        timetable = self.record("timetable-v1.json")
        self.record("calendar-v1.json")
        version = self.store.publish(*self._current_pair())
        self.assertEqual(version, self.store.current_version())
        self.store.assign_shift("A1", "border-west", "D30", "central", "2026-10-31")
        broken = fixture("calendar-v2.json")
        broken["valid_from"] = "2026-11-05"
        broken["exceptions"] = []
        digest, _ = self.store.record_document("/calendar", 200, broken)
        with self.assertRaises(PublishError) as caught:
            self.store.publish(timetable[0], digest)
        self.assertEqual(caught.exception.code, "coverage_gap")
        self.assertEqual(self.store.current_version(), version)
        self.assertEqual(len(self.store.shift_trace("A1")["bindings"]), 1)
        self.assertEqual(self.store.review_queue(), [])

    def test_identical_documents_are_shared(self):
        first = self.record("timetable-v1.json")
        second = self.record("timetable-v1.json")
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.documents("timetable")), 1)

    def test_calendar_change_queues_review_without_cancelling(self):
        self.record("timetable-v1.json")
        self.record("calendar-v1.json")
        self.store.publish(*self._current_pair())
        self.store.assign_shift("A1", "border-west", "D30", "central", "2026-10-31")
        self.store.assign_shift("A2", "border-west", "N12", "central", "2026-10-31")
        self.record("calendar-v2.json")
        self.store.publish(self.store.current_halves()["timetable"],
                           self.store.current_halves()["calendar"])
        queue = self.store.review_queue()
        self.assertEqual({item["external_id"] for item in queue}, {"A1"})
        reasons = {item["reason"] for item in queue}
        self.assertEqual(reasons, {"interpretation_changed", "instants_changed", "service_closed"})
        shift = self.store.shift_trace("A1")["shift"]
        self.assertEqual(shift["external_id"], "A1")
        latest = self.store.shift_trace("A1")["bindings"][-1]
        self.assertEqual(latest["service_state"], "closed_by_source")
        self.assertEqual(latest["source_time"], "03:15")

    def test_acknowledgement_does_not_change_the_shift(self):
        self.record("timetable-v1.json")
        self.record("calendar-v1.json")
        self.store.publish(*self._current_pair())
        self.store.assign_shift("A1", "border-west", "D30", "central", "2026-10-31")
        self.record("calendar-v2.json")
        self.store.publish(self.store.current_halves()["timetable"],
                           self.store.current_halves()["calendar"])
        items = self.store.review_queue()
        self.assertEqual(len(items), 3)
        for item in items:
            self.store.acknowledge(item["id"])
        self.assertEqual(self.store.review_queue(), [])
        self.assertEqual(len(self.store.review_queue(False)), 3)
        self.assertEqual(len(self.store.shift_trace("A1")["bindings"]), 2)
        with self.assertRaises(LookupError):
            self.store.acknowledge(items[0]["id"])

    def test_removed_departure_is_reported_not_deleted(self):
        self.record("timetable-v1.json")
        self.record("calendar-v1.json")
        self.store.publish(*self._current_pair())
        self.store.assign_shift("A1", "border-west", "S7", "harbour", "2026-10-26")
        newer = self.record("timetable-v2.json", "/timetable")
        self.record("calendar-v2.json")
        self.store.publish(newer[0], self.store.current_halves()["calendar"])
        queue = self.store.review_queue()
        self.assertEqual([item["reason"] for item in queue], ["departure_removed"])
        latest = self.store.shift_trace("A1")["bindings"][-1]
        self.assertEqual(latest["service_state"], "departure_missing")
        self.assertEqual(latest["source_time"], "23:50")

    def test_source_rollback_reactivates_previous_pair(self):
        self.record("timetable-v1.json")
        first_calendar = self.record("calendar-v1.json")
        self.store.publish(*self._current_pair())
        self.store.assign_shift("A1", "border-west", "D30", "central", "2026-10-31")
        self.record("calendar-v2.json")
        self.store.publish(self.store.current_halves()["timetable"],
                           self.store.current_halves()["calendar"])
        rollback = self.store.publish(self.store.current_halves()["timetable"], first_calendar[0])
        self.assertEqual(rollback, 1)
        self.assertEqual(self.store.current_version(), 1)
        self.assertEqual([version["state"] for version in self.store.versions()],
                         ["published", "superseded"])
        self.assertEqual(len(self.store.versions()), 2)
        statuses = [binding["interpretation"]["status"]
                    for binding in self.store.shift_trace("A1")["bindings"]]
        self.assertEqual(statuses, ["unique", "service_closed", "unique"])
        self.assertIn("service_reopened", {item["reason"] for item in self.store.review_queue()})

    def test_zone_conflict_between_halves_is_rejected(self):
        timetable = self.record("timetable-v1.json")
        calendar = fixture("calendar-v1.json")
        calendar["operators"]["border-west"] = {"zone": "Europe/London"}
        digest, _ = self.store.record_document("/calendar", 200, calendar)
        with self.assertRaises(PublishError) as caught:
            self.store.publish(timetable[0], digest)
        self.assertEqual(caught.exception.code, "zone_conflict")
        self.assertEqual(self.store.versions(), [])

    def test_repeated_publication_is_idempotent(self):
        self.record("timetable-v1.json")
        self.record("calendar-v1.json")
        first = self.store.publish(*self._current_pair())
        again = self.store.publish(*self._current_pair())
        self.assertEqual(first, again)
        self.assertEqual(len(self.store.versions()), 1)

    def _current_pair(self):
        halves = self.store.current_halves()
        return halves["timetable"], halves["calendar"]


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runtime = Path(self.temporary.name)

    def make(self, origin, **options):
        intake = Intake(origin.base_url, self.runtime / "raw.sqlite3",
                        self.runtime / "state.sqlite3", "test", **options)
        self.addCleanup(intake.close)
        return intake

    def test_two_halves_publish_one_compatible_version(self):
        with serve() as origin:
            intake = self.make(origin)
            result = intake.sync()
            self.assertIsNotNone(result["version_id"])
            self.assertEqual(result["version"]["timetable_version"], "2026-winter-1")
            self.assertEqual(result["ambiguous"], [])

    def test_half_arriving_alone_keeps_previous_version(self):
        with serve() as origin:
            intake = self.make(origin)
            first = intake.sync()
            origin.scenario["routes"]["/timetable"] = {"status": 503, "body": "unavailable"}
            origin.scenario["routes"]["/calendar"] = {"json": fixture("calendar-v1.json")}
            second = intake.sync()
            self.assertEqual(second["version_id"], first["version_id"])
            self.assertEqual(second["timetable"]["status"], 503)
            self.assertEqual(second["calendar"]["status"], 200)
            self.assertEqual(len(intake.store.documents("timetable")), 1)
            self.assertEqual(intake.store.current_version(), first["version_id"])

    def test_retry_recovers_from_transient_failure(self):
        scenario = load_scenario()
        scenario["routes"]["/timetable"] = [
            {"status": 503, "body": "down"},
            {"json": fixture("timetable-v1.json")},
        ]
        delays = []
        with serve(scenario) as origin:
            intake = self.make(origin, sleep=delays.append)
            result = intake.sync()
            self.assertIsNotNone(result["version_id"])
            self.assertEqual(result["timetable"]["status"], 200)
            self.assertTrue(delays)

    def test_rate_limit_is_retried_after_retry_after(self):
        scenario = load_scenario()
        scenario["routes"]["/calendar"] = [
            {"status": 429, "headers": {"Retry-After": "2"}, "json": {"error": "quota"}},
            {"json": fixture("calendar-v1.json")},
        ]
        delays = []
        with serve(scenario) as origin:
            intake = self.make(origin, sleep=delays.append)
            result = intake.sync()
            self.assertIsNotNone(result["version_id"])
            self.assertEqual(delays, [2.0])

    def test_failure_is_recorded_not_empty_feed(self):
        scenario = load_scenario()
        scenario["routes"]["/calendar"] = {"status": 429, "headers": {"Retry-After": "1"},
                                           "json": {"error": "quota"}}
        with serve(scenario) as origin:
            intake = self.make(origin, sleep=lambda _: None, attempts=2)
            result = intake.sync()
            self.assertIsNone(result["version_id"])
            self.assertEqual(result["calendar"]["status"], 429)
            self.assertEqual(result["calendar"]["error"]["retry_after"], "1")
            self.assertEqual(intake.store.documents("calendar"), [])
            self.assertEqual(intake.store.versions(), [])

    def test_malformed_document_is_not_published(self):
        scenario = load_scenario()
        scenario["routes"]["/calendar"] = {"json": {"kind": "calendar", "version": "broken"}}
        with serve(scenario) as origin:
            intake = self.make(origin)
            result = intake.sync()
            self.assertIsNone(result["version_id"])
            self.assertIn("valid_from", result["calendar"]["error"]["error"])

    def test_new_timetable_waits_for_its_declared_calendar(self):
        scenario = load_scenario()
        scenario["routes"]["/timetable"] = {"json": fixture("timetable-v2.json")}
        scenario["routes"]["/calendar"] = {"json": fixture("calendar-v1.json")}
        with serve(scenario) as origin:
            intake = self.make(origin)
            result = intake.sync()
            self.assertIsNone(result["version_id"])
            detail = intake.store.observations("publish")[-1]["detail"]
            self.assertEqual(json.loads(detail)["code"], "calendar_version_pending")
            self.assertEqual(intake.store.documents("timetable")[0]["source_version"], "2026-winter-2")

    def test_winter_timetable_waits_then_publishes_with_its_calendar(self):
        scenario = load_scenario()
        scenario["routes"]["/timetable"] = [
            {"json": fixture("timetable-v1.json")},
            {"json": fixture("timetable-v2.json")},
        ]
        scenario["routes"]["/calendar"] = [
            {"json": fixture("calendar-v1.json")},
            {"json": fixture("calendar-v1.json")},
            {"json": fixture("calendar-v2.json")},
        ]
        with serve(scenario) as origin:
            intake = self.make(origin)
            first = intake.sync()
            intake.store.assign_shift("SHIFT-N12", "border-west", "N12", "central", "2026-10-31")
            waiting = intake.sync()
            self.assertEqual(waiting["version_id"], first["version_id"])
            self.assertEqual(waiting["pending"]["timetable"],
                             [intake.store.documents("timetable")[-1]["digest"]])
            published = intake.sync()
            self.assertNotEqual(published["version_id"], first["version_id"])
            self.assertEqual(published["version"]["timetable_version"], "2026-winter-2")
            self.assertEqual(published["version"]["calendar_version"], "calendar-2026-11")
            reasons = {item["reason"] for item in intake.store.review_queue()}
            self.assertEqual(reasons, {"time_changed", "instants_changed"})
            trace = intake.store.shift_trace("SHIFT-N12")["bindings"]
            self.assertEqual([item["source_time"] for item in trace], ["26:30", "27:05"])

    def test_unchanged_documents_use_conditional_get(self):
        with serve() as origin:
            intake = self.make(origin)
            intake.sync()
            second = intake.sync()
            self.assertEqual(second["timetable"]["status"], 304)
            self.assertEqual(second["calendar"]["status"], 200)
            self.assertEqual(len(intake.store.versions()), 2)

    def test_sync_resumes_after_reopen(self):
        with serve() as origin:
            first = self.make(origin)
            first.sync()
            first.store.assign_shift("A1", "border-west", "D30", "central", "2026-10-31")
        with serve() as origin:
            second = self.make(origin)
            result = second.sync()
            self.assertIsNotNone(result["version_id"])
            trace = second.store.shift_trace("A1")
            self.assertEqual(trace["bindings"][0]["source_time"], "03:15")

    def test_calendar_update_flags_affected_shift(self):
        with serve() as origin:
            intake = self.make(origin)
            first = intake.sync()
            intake.store.assign_shift("SHIFT-D30", "border-west", "D30", "central", "2026-10-31")
            second = intake.sync()
            self.assertNotEqual(first["version_id"], second["version_id"])
            queue = intake.store.review_queue()
            self.assertEqual({item["external_id"] for item in queue}, {"SHIFT-D30"})
            self.assertEqual({item["reason"] for item in queue},
                             {"interpretation_changed", "instants_changed", "service_closed"})
            self.assertEqual(intake.store.shift_trace("SHIFT-D30")["shift"]["trip"], "D30")

    def test_repeated_sync_keeps_one_published_version(self):
        with serve() as origin:
            intake = self.make(origin)
            intake.sync()
            third = intake.sync()
            published = [version for version in intake.store.versions() if version["state"] == "published"]
            self.assertEqual(len(published), 1)


if __name__ == "__main__":
    unittest.main()
