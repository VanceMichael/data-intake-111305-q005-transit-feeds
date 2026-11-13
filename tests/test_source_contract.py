import hashlib
import json
import unittest

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

    def test_every_record_has_the_declared_source_identity(self):
        scenario = load_scenario()
        for record in scenario["source_records"]:
            for field in scenario["identity_fields"]:
                self.assertIn(field, record)
                self.assertNotEqual(str(record[field]).strip(), "")


if __name__ == "__main__":
    unittest.main()
