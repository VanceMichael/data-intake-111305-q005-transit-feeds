import unittest

from transit_feeds.catalog import (
    ACTION_ADDED,
    ACTION_CANCEL,
    FeedPart,
    apply_calendar,
    assemble_bundles,
    normalize_part,
)


def timetable_part(operator="border-west", version="winter-1", trips=None, observed_at="2026-11-01T00:00:00Z"):
    return normalize_part({
        "part": "timetable",
        "operator": operator,
        "calendar_version": version,
        "trips": trips or [
            {"operator": operator, "trip": "N12", "service_day": "2026-10-31",
             "departure": "26:30", "zone": "Europe/Berlin", "calendar_version": version},
            {"operator": operator, "trip": "N13", "service_day": "2026-10-25",
             "departure": "02:30", "zone": "Europe/Berlin", "calendar_version": version},
        ],
    }, observed_at=observed_at)


def calendar_part(operator="border-west", version="winter-1", exceptions=None, observed_at="2026-11-02T00:00:00Z"):
    return normalize_part({
        "part": "calendar",
        "operator": operator,
        "calendar_version": version,
        "exceptions": exceptions if exceptions is not None else [
            {"trip": "N12", "service_day": "2026-10-31", "action": ACTION_CANCEL},
        ],
    }, observed_at=observed_at)


class NormalizationTests(unittest.TestCase):
    def test_timetable_requires_calendar_version(self):
        with self.assertRaisesRegex(Exception, "calendar_version"):
            normalize_part({"part": "timetable", "operator": "op", "trips": []}, observed_at="t")

    def test_unknown_part_and_bad_action_are_rejected(self):
        with self.assertRaisesRegex(Exception, "部件类型"):
            normalize_part({"part": "mystery", "operator": "op", "calendar_version": "v1"}, observed_at="t")
        with self.assertRaisesRegex(Exception, "例外动作"):
            normalize_part({"part": "calendar", "operator": "op", "calendar_version": "v1",
                            "exceptions": [{"trip": "X", "service_day": "2026-11-01", "action": "pause"}]},
                           observed_at="t")

    def test_identical_content_has_same_digest(self):
        self.assertEqual(timetable_part().digest, timetable_part().digest)


class AssemblyTests(unittest.TestCase):
    def test_single_arrival_stays_pending_and_is_not_a_bundle(self):
        bundles, pending = assemble_bundles([timetable_part()])
        self.assertEqual(bundles, [])
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].part, "timetable")

    def test_split_arrivals_assemble_one_compatible_bundle(self):
        bundles, pending = assemble_bundles([timetable_part(), calendar_part()])
        self.assertEqual(len(bundles), 1)
        self.assertEqual(pending, [])
        bundle = bundles[0]
        self.assertEqual((bundle.operator, bundle.calendar_version), ("border-west", "winter-1"))

    def test_versions_never_mix_across_calendar_versions(self):
        tt_v1 = timetable_part(version="winter-1", observed_at="2026-11-01T00:00:00Z")
        cal_v2 = calendar_part(version="winter-2", observed_at="2026-11-02T00:00:00Z")
        bundles, pending = assemble_bundles([tt_v1, cal_v2])
        self.assertEqual(bundles, [])
        self.assertEqual({part.calendar_version for part in pending}, {"winter-1", "winter-2"})

    def test_latest_part_wins_but_old_part_remains_in_history(self):
        old = timetable_part(observed_at="2026-11-01T00:00:00Z")
        new_trips = [{
            "operator": "border-west", "trip": "N12", "service_day": "2026-10-31",
            "departure": "27:30", "zone": "Europe/Berlin", "calendar_version": "winter-1",
        }]
        new = timetable_part(trips=new_trips, observed_at="2026-11-05T00:00:00Z")
        cal = calendar_part()
        bundles, pending = assemble_bundles([old, new, cal])
        self.assertEqual(len(bundles), 1)
        self.assertEqual(bundles[0].timetable.digest, new.digest)
        self.assertNotEqual(old.digest, new.digest)
        self.assertEqual(pending, [])

    def test_operators_are_assembled_independently(self):
        east_tt = timetable_part(operator="east", version="autumn", trips=[
            {"operator": "east", "trip": "E8", "service_day": "2026-11-01",
             "departure": "00:10", "zone": "Asia/Shanghai", "calendar_version": "autumn"}])
        east_cal = calendar_part(operator="east", version="autumn", exceptions=[])
        bundles, pending = assemble_bundles([timetable_part(), east_tt, east_cal])
        self.assertEqual({b.operator for b in bundles}, {"east"})
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].operator, "border-west")


class CalendarApplicationTests(unittest.TestCase):
    def test_cancel_exception_marks_instance_not_operating_but_keeps_source(self):
        bundle = assemble_bundles([timetable_part(), calendar_part()])[0][0]
        instances, warnings = apply_calendar(bundle)
        n12 = next(item for item in instances if item.trip == "N12")
        n13 = next(item for item in instances if item.trip == "N13")
        self.assertFalse(n12.operates)
        self.assertEqual(n12.action, ACTION_CANCEL)
        self.assertEqual(n12.departure, "26:30")  # 来源写法不被覆盖
        self.assertTrue(n13.operates)
        self.assertEqual(warnings, [])

    def test_added_exception_creates_extra_instance(self):
        cal = calendar_part(exceptions=[
            {"trip": "N99", "service_day": "2026-12-24", "action": ACTION_ADDED,
             "departure": "23:00", "zone": "Europe/Berlin"},
        ])
        bundle = assemble_bundles([timetable_part(), cal])[0][0]
        instances, _ = apply_calendar(bundle)
        added = next(item for item in instances if item.trip == "N99")
        self.assertTrue(added.operates)
        self.assertEqual(added.action, ACTION_ADDED)

    def test_orphan_cancel_warns_without_dropping_data(self):
        cal = calendar_part(exceptions=[
            {"trip": "GHOST", "service_day": "2026-12-25", "action": ACTION_CANCEL},
        ])
        bundle = assemble_bundles([timetable_part(), cal])[0][0]
        instances, warnings = apply_calendar(bundle)
        self.assertEqual({item.trip for item in instances}, {"N12", "N13"})
        self.assertEqual(len(warnings), 1)
        self.assertIn("GHOST", warnings[0])

    def test_missing_trip_field_is_hard_error(self):
        bad = FeedPart(part="timetable", operator="border-west", calendar_version="winter-1",
                       trips=({"no_trip": True},), exceptions=(), digest="x",
                       observed_at="t", capture_id=None, source_text="{}")
        good = calendar_part()
        bundle = assemble_bundles([bad, good])[0][0]
        with self.assertRaisesRegex(Exception, "缺少字段"):
            apply_calendar(bundle)


if __name__ == "__main__":
    unittest.main()
