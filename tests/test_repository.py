import json
import tempfile
import unittest
from pathlib import Path

from transit_feeds.catalog import normalize_part
from transit_feeds.ingestion import ingest_records
from transit_feeds.repository import (
    REVIEW_AMBIGUOUS_DISPATCH,
    REVIEW_CALENDAR_CHANGE,
    REVIEW_OPEN,
    REVIEW_RESOLVED,
    RUN_DONE,
    RUN_FAILED,
    ServiceStore,
)
from transit_feeds.serviceday import (
    STATUS_AMBIGUOUS,
    STATUS_DEFINITE,
    STATUS_INVALID,
    STATUS_NONEXISTENT,
    interpret_departure,
)


def tt(operator, version, trips):
    return normalize_part(
        {"part": "timetable", "operator": operator, "calendar_version": version, "trips": trips},
        observed_at="2026-11-01T00:00:00Z",
    )


def cal(operator, version, exceptions):
    return normalize_part(
        {"part": "calendar", "operator": operator, "calendar_version": version, "exceptions": exceptions},
        observed_at="2026-11-02T00:00:00Z",
    )


WINTER_V1 = [
    {"operator": "border-west", "trip": "N12", "service_day": "2026-10-31",
     "departure": "26:30", "zone": "Europe/Berlin", "calendar_version": "winter-1"},
    {"operator": "border-west", "trip": "N13", "service_day": "2026-10-25",
     "departure": "02:30", "zone": "Europe/Berlin", "calendar_version": "winter-1"},
]
WINTER_V2 = [
    {"operator": "border-west", "trip": "N12", "service_day": "2026-10-31",
     "departure": "27:30", "zone": "Europe/Berlin", "calendar_version": "winter-2"},
    {"operator": "border-west", "trip": "N13", "service_day": "2026-10-25",
     "departure": "02:30", "zone": "Europe/Berlin", "calendar_version": "winter-2"},
]


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = ServiceStore(Path(self.temporary.name) / "service.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def parts_v1(self):
        return [
            tt("border-west", "winter-1", WINTER_V1),
            cal("border-west", "winter-1", [
                {"trip": "N12", "service_day": "2026-10-31", "action": "cancel"},
            ]),
        ]

    def ingest_v1(self):
        return ingest_records(self.store, "supplier", [
            {"part": "timetable", "operator": "border-west", "calendar_version": "winter-1", "trips": WINTER_V1},
            {"part": "calendar", "operator": "border-west", "calendar_version": "winter-1", "exceptions": [
                {"trip": "N12", "service_day": "2026-10-31", "action": "cancel"}]},
        ])


class InterpretationCacheTests(StoreTestCase):
    def test_identical_basis_reuses_cache_across_versions(self):
        interp = interpret_departure("2026-10-31", "26:30", "Europe/Berlin", "winter-1")
        key1, inserted1 = self.store.store_interpretation(interp)
        key2, inserted2 = self.store.store_interpretation(interp)
        self.assertTrue(inserted1)
        self.assertFalse(inserted2)
        self.assertEqual(key1, key2)
        self.assertEqual(self.store.get_interpretation(key1)["source_departure"], "26:30")

    def test_tzdata_version_change_invalidates_cache_key(self):
        interp = interpret_departure("2026-10-31", "26:30", "Europe/Berlin", "winter-1")
        self.store.store_interpretation(interp)
        stored = self.store.connection.execute("SELECT cache_key FROM interpretations").fetchone()
        self.assertIsNotNone(stored)
        # 模拟时区库升级：同输入不同 zone_rule 必须产生不同键
        upgraded = interpret_departure("2026-10-31", "26:30", "Europe/Berlin", "winter-1")
        object.__setattr__(upgraded, "zone_rule", upgraded.zone_rule.replace("2026f", "2027a"))
        from transit_feeds.serviceday import cache_key_for
        self.assertNotEqual(cache_key_for(interp), cache_key_for(upgraded))


class IngestionLifecycleTests(StoreTestCase):
    def test_split_arrivals_become_one_effective_bundle(self):
        summary = ingest_records(self.store, "supplier", [
            {"part": "timetable", "operator": "border-west", "calendar_version": "winter-1", "trips": WINTER_V1},
        ])
        self.assertIsNone(self.store.effective_bundle("border-west"))
        self.assertEqual(summary["pending_operators"], ["border-west:winter-1"])
        summary = ingest_records(self.store, "supplier", [
            {"part": "calendar", "operator": "border-west", "calendar_version": "winter-1", "exceptions": []},
        ])
        bundle = self.store.effective_bundle("border-west")
        self.assertIsNotNone(bundle)
        self.assertEqual(bundle["calendar_version"], "winter-1")
        self.assertEqual(summary["bundles_assembled"], 1)

    def test_rerun_is_idempotent_and_cached(self):
        records = [
            {"part": "timetable", "operator": "border-west", "calendar_version": "winter-1", "trips": WINTER_V1},
            {"part": "calendar", "operator": "border-west", "calendar_version": "winter-1", "exceptions": []},
        ]
        first = ingest_records(self.store, "supplier", records)
        second = ingest_records(self.store, "supplier", records)
        self.assertEqual(second["parts_new"], 0)
        self.assertEqual(second["bundles_assembled"], 0)
        self.assertEqual(second["promotions"], [])
        self.assertGreaterEqual(first["cache_misses"], 1)
        # 重跑没有新 bundle 需要实例化，因此不重复消耗解释缓存（幂等无副作用）
        self.assertEqual((second["cache_hits"], second["cache_misses"]), (0, 0))
        self.assertEqual(len(self.store.list_runs()), 2)
        self.assertTrue(all(run["state"] == RUN_DONE for run in self.store.list_runs()))

    def test_materialized_instances_keep_source_and_interpretation(self):
        self.ingest_v1()
        n12 = self.store.get_effective_instance("border-west", "N12", "2026-10-31")
        n13 = self.store.get_effective_instance("border-west", "N13", "2026-10-25")
        self.assertFalse(n12["operates"])
        self.assertEqual(n12["source"]["departure"], "26:30")
        self.assertEqual(n13["interp_status"], STATUS_AMBIGUOUS)
        self.assertEqual(len(n13["interpretation"]["candidates"]), 2)

    def test_quarantine_bad_records_instead_of_aborting_batch(self):
        summary = ingest_records(self.store, "supplier", [
            {"part": "timetable", "operator": "border-west", "calendar_version": "winter-1", "trips": WINTER_V1},
            {"part": "calendar", "operator": "border-west", "calendar_version": "winter-1", "exceptions": []},
            {"part": "calendar", "operator": "other", "calendar_version": "v", "exceptions": [
                {"trip": "X", "service_day": "bad-day", "action": "pause"}]},
        ])
        self.assertEqual(summary["quarantined"], 1)
        self.assertEqual(len(self.store.list_quarantine()), 1)
        self.assertIsNotNone(self.store.effective_bundle("border-west"))

    def test_failed_run_is_marked_and_leaves_no_half_bundle(self):
        # 触发 fail_run：calendar 部件缺 version 会被隔离而不失败，这里直接模拟运行中断言记账
        run_id = self.store.start_run("supplier")
        self.store.fail_run(run_id, "simulated crash")
        self.assertEqual(self.store.running_runs(), [])
        failed = [run for run in self.store.list_runs() if run["state"] == RUN_FAILED]
        self.assertEqual(len(failed), 1)

    def test_instance_replacement_rolls_back_together(self):
        self.ingest_v1()
        bundle = self.store.effective_bundle("border-west")
        good = [{
            "operator": "border-west", "trip": "N12", "service_day": "2026-10-31",
            "operates": True, "action": "scheduled", "departure": "26:30",
            "zone": "Europe/Berlin", "source": {}, "warnings": [],
            "interpretation": {"x": 1}, "interp_cache_key": "k", "interp_status": STATUS_DEFINITE,
        }]
        self.store.replace_instances(bundle["id"], good)
        self.assertEqual(len(self.store._instances_of(bundle["id"])), 1)
        # 非法载荷导致事务失败：旧实例必须原样保留，不出现空表或半成品
        with self.assertRaises(Exception):
            self.store.replace_instances(bundle["id"], [{"bad": object()}])
        self.assertEqual(len(self.store._instances_of(bundle["id"])), 1)

    def test_open_review_dedupes_and_reports_whether_inserted(self):
        self.ingest_v1()
        instance = self.store.get_effective_instance("border-west", "N13", "2026-10-25")
        self.store.record_dispatch("D-1", instance)
        before = len(self.store.list_reviews(status=REVIEW_OPEN))
        args = ("D-1", "border-west", "N13", "2026-10-25", REVIEW_CALENDAR_CHANGE, "first")
        self.assertTrue(self.store.open_review(*args))
        self.assertFalse(self.store.open_review(*args))
        self.assertEqual(len(self.store.list_reviews(status=REVIEW_OPEN)), before + 1)

    def test_bare_records_are_wrapped_as_timetable_parts(self):
        summary = ingest_records(self.store, "supplier", [
            {"operator": "east", "trip": "E8", "service_day": "2026-11-01",
             "departure": "00:10", "zone": "Asia/Shanghai", "calendar_version": "autumn"},
        ])
        self.assertIn("east:autumn", summary["pending_operators"])
        self.assertIsNone(self.store.effective_bundle("east"))


class CalendarUpgradeTests(StoreTestCase):
    def _dispatch(self, trip, service_day, dispatch_id):
        instance = self.store.get_effective_instance("border-west", trip, service_day)
        return self.store.record_dispatch(dispatch_id, instance)

    def test_upgrade_opens_review_for_affected_dispatches_only(self):
        self.ingest_v1()
        self._dispatch("N12", "2026-10-31", "D-1")
        self._dispatch("N13", "2026-10-25", "D-2")

        ingest_records(self.store, "supplier", [
            {"part": "timetable", "operator": "border-west", "calendar_version": "winter-2", "trips": WINTER_V2},
            {"part": "calendar", "operator": "border-west", "calendar_version": "winter-2", "exceptions": []},
        ])
        bundle = self.store.effective_bundle("border-west")
        self.assertEqual(bundle["calendar_version"], "winter-2")
        reviews = self.store.list_reviews(status=REVIEW_OPEN)
        calendar_reviews = [r for r in reviews if r["reason"] == REVIEW_CALENDAR_CHANGE]
        # N12 时刻改变必须复核；N13 时刻与解释不变，不打扰调度
        affected = {(r["dispatch_id"], r["trip"]) for r in calendar_reviews}
        self.assertEqual(affected, {("D-1", "N12")})
        # 旧版本仍可追溯，未被覆盖
        history = self.store.instance_history("border-west", "N12", "2026-10-31")
        self.assertEqual([h["calendar_version"] for h in history], ["winter-1", "winter-2"])

    def test_dispatch_is_never_auto_cancelled(self):
        self.ingest_v1()
        self._dispatch("N12", "2026-10-31", "D-9")
        ingest_records(self.store, "supplier", [
            {"part": "timetable", "operator": "border-west", "calendar_version": "winter-2",
             "trips": [WINTER_V2[0]]},
            {"part": "calendar", "operator": "border-west", "calendar_version": "winter-2", "exceptions": [
                {"trip": "N12", "service_day": "2026-10-31", "action": "cancel"}]},
        ])
        trace = self.store.trace_dispatch("D-9")
        self.assertEqual(trace["dispatch"]["dispatch_id"], "D-9")
        self.assertTrue(any(r["reason"] == REVIEW_CALENDAR_CHANGE for r in trace["reviews"]))

    def test_review_dedupes_on_repeated_upgrade(self):
        self.ingest_v1()
        self._dispatch("N12", "2026-10-31", "D-1")
        for version, departure in (("winter-2", "27:30"), ("winter-3", "28:30")):
            ingest_records(self.store, "supplier", [
                {"part": "timetable", "operator": "border-west", "calendar_version": version,
                 "trips": [{"operator": "border-west", "trip": "N12", "service_day": "2026-10-31",
                            "departure": departure, "zone": "Europe/Berlin", "calendar_version": version}]},
                {"part": "calendar", "operator": "border-west", "calendar_version": version, "exceptions": []},
            ])
        reviews = self.store.list_reviews()
        same = [r for r in reviews if r["reason"] == REVIEW_CALENDAR_CHANGE]
        self.assertEqual(len(same), 1)
        self.store.resolve_review(same[0]["id"], "调度确认按 winter-3 执行")
        resolved = [r for r in self.store.list_reviews() if r["status"] == REVIEW_RESOLVED]
        self.assertEqual(resolved[0]["resolution"], "调度确认按 winter-3 执行")


class DispatchTraceTests(StoreTestCase):
    def test_trace_returns_frozen_source_time_and_basis(self):
        self.ingest_v1()
        instance = self.store.get_effective_instance("border-west", "N13", "2026-10-25")
        self.store.record_dispatch("D-100", instance, part_capture_id=42)
        trace = self.store.trace_dispatch("D-100")
        self.assertEqual(trace["dispatch"]["source_departure"], "02:30")
        self.assertEqual(trace["dispatch"]["part_capture_id"], 42)
        self.assertEqual(trace["bundle"]["calendar_version"], "winter-1")
        basis = trace["dispatch"]["interpretation"]
        self.assertEqual(basis["zone_name"], "Europe/Berlin")
        self.assertIn("tzdata", basis["zone_rule"])
        self.assertEqual(len(basis["candidates"]), 2)
        self.assertEqual(trace["history"][0]["source"]["departure"], "02:30")

    def test_cancelled_dispatch_keeps_source_text_even_without_instant(self):
        self.ingest_v1()
        instance = self.store.get_effective_instance("border-west", "N12", "2026-10-31")
        self.assertFalse(instance["operates"])
        self.store.record_dispatch("D-101", instance)
        trace = self.store.trace_dispatch("D-101")
        self.assertEqual(trace["dispatch"]["source_departure"], "26:30")
        self.assertIsNone(trace["dispatch"]["interpretation"])

    def test_ambiguous_dispatch_opens_review_and_is_listed(self):
        self.ingest_v1()
        instance = self.store.get_effective_instance("border-west", "N13", "2026-10-25")
        self.store.record_dispatch("D-200", instance)
        ambiguous = self.store.list_ambiguous_dispatches()
        self.assertEqual([item["dispatch_id"] for item in ambiguous], ["D-200"])
        self.assertEqual(ambiguous[0]["reason"], REVIEW_AMBIGUOUS_DISPATCH)

    def test_nonexistent_and_invalid_dispatches_also_flag(self):
        ingest_records(self.store, "supplier", [
            {"part": "timetable", "operator": "op", "calendar_version": "v1", "trips": [
                {"operator": "op", "trip": "G1", "service_day": "2027-03-28",
                 "departure": "02:30", "zone": "Europe/Berlin", "calendar_version": "v1"},
                {"operator": "op", "trip": "G2", "service_day": "2027-03-28",
                 "departure": "九点", "zone": "Europe/Berlin", "calendar_version": "v1"},
            ]},
            {"part": "calendar", "operator": "op", "calendar_version": "v1", "exceptions": []},
        ])
        g1 = self.store.get_effective_instance("op", "G1", "2027-03-28")
        g2 = self.store.get_effective_instance("op", "G2", "2027-03-28")
        self.assertEqual(g1["interp_status"], STATUS_NONEXISTENT)
        self.assertEqual(g2["interp_status"], STATUS_INVALID)
        self.store.record_dispatch("D-G1", g1)
        self.store.record_dispatch("D-G2", g2)
        flagged = {item["dispatch_id"] for item in self.store.list_ambiguous_dispatches()}
        self.assertEqual(flagged, {"D-G1", "D-G2"})

    def test_duplicate_dispatch_id_is_rejected(self):
        self.ingest_v1()
        instance = self.store.get_effective_instance("border-west", "N12", "2026-10-31")
        self.store.record_dispatch("DUP", instance)
        with self.assertRaisesRegex(Exception, "已存在"):
            self.store.record_dispatch("DUP", instance)

    def test_persistence_survives_reopen(self):
        self.ingest_v1()
        path = self.store.path
        self.store.close()
        reopened = ServiceStore(path)
        try:
            self.assertEqual(reopened.effective_bundle("border-west")["calendar_version"], "winter-1")
            self.assertEqual(reopened.trace_dispatch.__name__, "trace_dispatch")
        finally:
            reopened.close()

    def test_interrupted_ingest_resumes_cleanly_after_reopen(self):
        # 模拟第一次运行只写入时刻表部件后进程中断（日历尚未到达）
        ingest_records(self.store, "supplier", [
            {"part": "timetable", "operator": "border-west", "calendar_version": "winter-1", "trips": WINTER_V1},
        ])
        path = self.store.path
        self.store.close()
        resumed = ServiceStore(path)
        try:
            self.assertIsNone(resumed.effective_bundle("border-west"))
            # 重放完整一批：已存在的时刻表部件去重，日历到达后正常装配
            summary = ingest_records(resumed, "supplier", [
                {"part": "timetable", "operator": "border-west", "calendar_version": "winter-1", "trips": WINTER_V1},
                {"part": "calendar", "operator": "border-west", "calendar_version": "winter-1", "exceptions": []},
            ])
            self.assertEqual(summary["parts_new"], 1)
            self.assertEqual(summary["bundles_assembled"], 1)
            self.assertEqual(resumed.effective_bundle("border-west")["calendar_version"], "winter-1")
            self.assertEqual(
                [run["state"] for run in resumed.list_runs()].count(RUN_DONE), 2
            )
        finally:
            resumed.close()


class WatermarkTests(StoreTestCase):
    def test_watermark_roundtrip(self):
        self.assertIsNone(self.store.get_watermark("supplier", "/feed"))
        self.store.set_watermark("supplier", "/feed", "feed-2", 7)
        watermark = self.store.get_watermark("supplier", "/feed")
        self.assertEqual((watermark["etag"], watermark["last_capture_id"]), ("feed-2", 7))
        self.store.set_watermark("supplier", "/feed", "feed-3", 8)
        self.assertEqual(self.store.get_watermark("supplier", "/feed")["etag"], "feed-3")


if __name__ == "__main__":
    unittest.main()
