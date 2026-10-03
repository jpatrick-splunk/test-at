import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from zentra.csv_import import (  # noqa: E402
    ImportStats,
    format_logger_datetime,
    iter_csv_events,
    parse_csv_timestamp,
    split_measurement_header,
)

SAMPLE = """\
z6-30302,Port1,Port1,Port2,Port3,Port7
# Records: 3,ATMOS 41 G2 All-in-one Weather Station,ATMOS 41 G2 All-in-one Weather Station,TEROS 12 Moisture/Temp/EC,TEROS 12 Moisture/Temp/EC,Battery
Timestamps, in Precipitation, degree_F Air Temperature, % Water Content, % Water Content, % Battery Percent
02/26/2025 11:15:00 AM,0.0,40.1,22.0,18.5,90
10/03/2026 10:15:00 AM,0.01,70.0,12.0,,88
10/03/2026 10:15:00 AM,9.9,1.0,1.0,1.0,1
11/25/2025 10:45:00 AM,0.0,55.7,12.1,11.0,100
"""


class HeaderTests(unittest.TestCase):
    def test_splits_unit_prefix(self):
        self.assertEqual(split_measurement_header(" in Precipitation"), ("Precipitation", "in"))
        self.assertEqual(split_measurement_header("degree_F Air Temperature"), ("Air Temperature", "°F"))
        self.assertEqual(split_measurement_header("in/h Max Precip Rate"), ("Max Precip Rate", "in/h"))
        self.assertEqual(split_measurement_header("W/m2 Solar Radiation"), ("Solar Radiation", "W/m²"))
        self.assertEqual(split_measurement_header("Drop Counts"), ("Drop Counts", None))


class TimestampTests(unittest.TestCase):
    def test_matches_indexed_live_reading(self):
        moment = parse_csv_timestamp("10/03/2026 10:15:00 AM")
        self.assertEqual(int(moment.timestamp()), 1791040500)
        self.assertEqual(format_logger_datetime(moment), "2026-10-03 10:15:00-05:00")

    def test_winter_offset_is_central_standard_time(self):
        moment = parse_csv_timestamp("02/26/2025 11:15:00 AM")
        self.assertEqual(format_logger_datetime(moment), "2025-02-26 11:15:00-06:00")
        self.assertEqual(moment.utcoffset().total_seconds(), -6 * 3600)


class EventTests(unittest.TestCase):
    def test_expands_ports_skips_blanks_and_duplicate_timestamps(self):
        stats = ImportStats()
        events = list(iter_csv_events(iter(SAMPLE.splitlines(keepends=True)), "z6-30302", stats=stats))
        precip = [event for event in events if event["measurement"] == "Precipitation"]
        self.assertEqual([event["value"] for event in precip], [0.0, 0.01, 0.0])
        self.assertEqual(precip[1]["timestamp_utc"], 1791040500)
        self.assertEqual(precip[1]["units"], "in")
        self.assertEqual(precip[1]["sensor_name"], "ATMOS 41 G2")
        self.assertEqual(precip[1]["aggregation"], "sum")
        self.assertEqual(precip[0]["datetime"], "2025-02-26 11:15:00-06:00")
        water = [event for event in events if event["measurement"] == "Water Content"]
        self.assertEqual([event["port_number"] for event in water], [2, 3, 2, 2, 3])
        self.assertEqual(stats.duplicate_rows, 1)
        self.assertGreater(stats.blank_values, 0)
        battery = [event for event in events if event["measurement"] == "Battery Percent"]
        self.assertEqual(battery[0]["value"], 90)
        self.assertEqual(battery[0]["units"], "%")
        self.assertEqual(battery[0]["sensor_name"], "Battery")

    def test_cutoff_drops_live_overlap(self):
        stats = ImportStats()
        events = list(
            iter_csv_events(
                iter(SAMPLE.splitlines(keepends=True)),
                "z6-30302",
                before_timestamp_utc=1791040500,
                stats=stats,
            )
        )
        self.assertTrue(all(event["timestamp_utc"] < 1791040500 for event in events))
        self.assertEqual(stats.cutoff_rows, 1)

    def test_rejects_wrong_logger(self):
        with self.assertRaises(ValueError):
            list(iter_csv_events(iter(SAMPLE.splitlines(keepends=True)), "z6-30022"))


if __name__ == "__main__":
    unittest.main()
