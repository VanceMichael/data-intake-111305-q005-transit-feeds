import tempfile
import unittest
from pathlib import Path

from transit_feeds.archive import Archive
from transit_feeds.client import Client, collect
from transit_feeds.decoding import records
from transit_feeds.ingestion import ingest_records
from transit_feeds.origin import load_scenario, serve
from transit_feeds.repository import (
    REVIEW_AMBIGUOUS_DISPATCH,
    REVIEW_CALENDAR_CHANGE,
    REVIEW_OPEN,
    REVIEW_UNRESOLVABLE_DISPATCH,
    ServiceStore,
)


SCENARIO_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "winter_switch.json"


class EndToEndIngestionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_split_feed_over_real_http_assembles_one_bundle(self):
        scenario = load_scenario(SCENARIO_PATH)
        with serve(scenario) as origin:
            archive = Archive(self.root / "raw.sqlite3")
            store = ServiceStore(self.root / "service.sqlite3")
            try:
                tt_id, tt_response = collect(Client(origin.base_url), archive, "op", "/feed")
                first = ingest_records(
                    store, "op",
                    records(tt_response.body, tt_response.headers["content-type"]),
                    capture_id=tt_id,
                )
                self.assertIsNone(store.effective_bundle("border-west"))
                self.assertEqual(first["pending_operators"], ["border-west:winter-2026"])

                cal_id, cal_response = collect(Client(origin.base_url), archive, "op", "/calendar")
                second = ingest_records(
                    store, "op",
                    records(cal_response.body, cal_response.headers["content-type"]),
                    capture_id=cal_id,
                )
                bundle = store.effective_bundle("border-west")
                self.assertIsNotNone(bundle)
                self.assertEqual(second["promotions"][0]["calendar_version"], "winter-2026")

                statuses = {
                    row["trip"]: row["interp_status"]
                    for row in store.effective_instances("border-west")
                }
                self.assertEqual(statuses, {"N12": "definite", "N13": "ambiguous", "N14": "nonexistent"})

                # 原始字节归档与解释库互链但独立保存
                self.assertEqual(archive.body(tt_id)[:1], b"{")
                self.assertEqual(len(archive.captures()), 2)
            finally:
                archive.close()
                store.close()

    def test_conditional_get_304_and_backfill_are_safe(self):
        scenario = load_scenario(SCENARIO_PATH)
        with serve(scenario) as origin:
            client = Client(origin.base_url)
            archive = Archive(self.root / "raw.sqlite3")
            store = ServiceStore(self.root / "service.sqlite3")
            try:
                tt_id, response = collect(client, archive, "op", "/feed")
                payload = records(response.body, response.headers["content-type"])
                # ETag 命中返回 304，没有新正文，摄入层不被触发
                not_modified = client.fetch("/feed", {"If-None-Match": "feed-winter-2026"})
                self.assertEqual(not_modified.status, 304)

                ingest_records(store, "op", payload, capture_id=tt_id)
                runs_before = len(store.list_runs())
                # 补抓重复内容：部件去重，无新 bundle，无新解释
                repeat = ingest_records(store, "op", payload, capture_id=tt_id)
                self.assertEqual(repeat["parts_new"], 0)
                self.assertEqual(repeat["bundles_assembled"], 0)
                self.assertEqual(len(store.list_runs()), runs_before + 1)
            finally:
                archive.close()
                store.close()

    def test_calendar_upgrade_flags_only_affected_dispatch_without_cancelling(self):
        scenario = load_scenario(SCENARIO_PATH)
        with serve(scenario) as origin:
            archive = Archive(self.root / "raw.sqlite3")
            store = ServiceStore(self.root / "service.sqlite3")
            try:
                _, tt_response = collect(Client(origin.base_url), archive, "op", "/feed")
                _, cal_response = collect(Client(origin.base_url), archive, "op", "/calendar")
                ingest_records(store, "op", records(tt_response.body, tt_response.headers["content-type"]))
                ingest_records(store, "op", records(cal_response.body, cal_response.headers["content-type"]))

                n12 = store.get_effective_instance("border-west", "N12", "2026-10-31")
                n13 = store.get_effective_instance("border-west", "N13", "2026-10-25")
                n14 = store.get_effective_instance("border-west", "N14", "2027-03-28")
                store.record_dispatch("JOB-1", n12)
                store.record_dispatch("JOB-2", n13)
                store.record_dispatch("JOB-3", n14)
                before_reviews = len(store.list_reviews(status=REVIEW_OPEN))

                # 新版冬季时刻：N12 改点，N13 完全不变
                ingest_records(store, "op", [
                    {"part": "timetable", "operator": "border-west",
                     "calendar_version": "winter-2027", "trips": [
                        {"operator": "border-west", "trip": "N12", "service_day": "2026-10-31",
                         "departure": "28:05", "zone": "Europe/Berlin", "calendar_version": "winter-2027"},
                        {"operator": "border-west", "trip": "N13", "service_day": "2026-10-25",
                         "departure": "02:30", "zone": "Europe/Berlin", "calendar_version": "winter-2027"},
                     ]},
                    {"part": "calendar", "operator": "border-west",
                     "calendar_version": "winter-2027", "exceptions": []},
                ])
                self.assertEqual(store.effective_bundle("border-west")["calendar_version"], "winter-2027")
                calendar_reviews = [
                    r for r in store.list_reviews(status=REVIEW_OPEN)
                    if r["reason"] == REVIEW_CALENDAR_CHANGE
                ]
                # N12 改点、N14 在新版中消失，均为已派班次必须复核；N13 无变化不打扰
                self.assertEqual(
                    sorted(r["dispatch_id"] for r in calendar_reviews),
                    ["JOB-1", "JOB-3"],
                )
                # 歧义复核仍然挂着，且派班记录一条都没被删
                reasons = {r["reason"] for r in store.list_reviews(status=REVIEW_OPEN)}
                self.assertIn(REVIEW_AMBIGUOUS_DISPATCH, reasons)
                self.assertIn(REVIEW_UNRESOLVABLE_DISPATCH, reasons)
                self.assertGreaterEqual(len(store.list_reviews(status=REVIEW_OPEN)), before_reviews)
                trace = store.trace_dispatch("JOB-1")
                self.assertEqual(trace["dispatch"]["source_departure"], "26:30")
                versions = [h["calendar_version"] for h in trace["history"]]
                self.assertEqual(versions, ["winter-2026", "winter-2027"])
            finally:
                archive.close()
                store.close()


if __name__ == "__main__":
    unittest.main()
