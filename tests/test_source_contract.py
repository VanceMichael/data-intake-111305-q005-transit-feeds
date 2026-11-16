import hashlib
import json
import unittest
from pathlib import Path

from transit_feeds.client import Client
from transit_feeds.decoding import parse_page, records as decode_records
from transit_feeds.origin import load_scenario, serve


class SourceContractTests(unittest.TestCase):
    def test_domain_observations_preserve_source_distinctions(self):
        scenario = load_scenario()
        records = scenario["source_records"]
        with serve(scenario) as origin:
            client = Client(origin.base_url)
            from zoneinfo import ZoneInfo
            from datetime import datetime
            self.assertEqual(records[0]["departure"], "26:30")
            local = datetime(2026, 10, 25, 2, 30, tzinfo=ZoneInfo("Europe/Berlin"))
            self.assertNotEqual(local.replace(fold=0).utcoffset(), local.replace(fold=1).utcoffset())
            self.assertNotEqual(records[0]["calendar_version"], records[1]["calendar_version"])

    def test_business_cases_are_observable_from_the_domain_layer(self):
        from datetime import date
        from transit_feeds.service_day import interpret_departure
        cases = json.loads((Path(__file__).parent.parent / "fixtures" / "business_cases.json")
                           .read_text(encoding="utf-8"))
        by_name = {case["name"]: case["observable"] for case in cases}
        extended = by_name["extended-hour-service-day"]
        evidence = interpret_departure({
            "service_day": date(2026, 10, 31), "source_time": "26:30", "seconds": 95400,
            "zone": "Europe/Berlin", "calendar_version": "x", "timetable_version": "y",
            "source_offset": None,
        })
        self.assertEqual(evidence["status"], extended["status"])
        self.assertEqual(evidence["candidates"][0]["utc"], extended["utc"])
        self.assertEqual(evidence["source_time"], extended["source_time"])
        self.assertEqual(evidence["service_day"], extended["service_day"])

        repeated = by_name["repeated-local-time"]
        evidence = interpret_departure({
            "service_day": date(2026, 10, 25), "source_time": "02:30", "seconds": 9000,
            "zone": "Europe/Berlin", "calendar_version": "x", "timetable_version": "y",
            "source_offset": None,
        })
        self.assertEqual(evidence["status"], repeated["status"])
        self.assertEqual(sorted(item["utc"] for item in evidence["candidates"]),
                         repeated["candidates"])
        self.assertTrue(all(item["chosen"] is None for item in evidence["candidates"]))

        missing = by_name["nonexistent-local-time"]
        evidence = interpret_departure({
            "service_day": date(2026, 10, 4), "source_time": "02:30", "seconds": 9000,
            "zone": "Australia/Sydney", "calendar_version": "x", "timetable_version": "y",
            "source_offset": None,
        })
        self.assertEqual(evidence["status"], missing["status"])
        self.assertEqual(evidence["candidates"], missing["candidates"])

    def test_every_record_has_the_declared_source_identity(self):
        scenario = load_scenario()
        for record in scenario["source_records"]:
            for field in scenario["identity_fields"]:
                self.assertIn(field, record)
                self.assertNotEqual(str(record[field]).strip(), "")


if __name__ == "__main__":
    unittest.main()
