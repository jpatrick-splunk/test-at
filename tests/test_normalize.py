import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "zentra_weather" / "bin"))

from zentra.client import extract_measurement_map  # noqa: E402
from zentra.normalize import (  # noqa: E402
    canonical_measurement_name,
    location_at,
    normalize_readings,
)

FIXTURE = ROOT / "tests" / "fixtures" / "readings_v4.json"


class CanonicalNameTests(unittest.TestCase):
    def test_known_aliases(self):
        self.assertEqual(canonical_measurement_name("Air Temperature"), "air_temperature")
        self.assertEqual(canonical_measurement_name("Wind Gust"), "gust_speed")
        self.assertEqual(canonical_measurement_name("Signal Strength"), "signal_percent")

    def test_unknown_snake_case(self):
        self.assertEqual(canonical_measurement_name("Soil Dielectric"), "soil_dielectric")


class NormalizeTests(unittest.TestCase):
    def setUp(self):
        with FIXTURE.open(encoding="utf-8") as handle:
            self.payload = json.load(handle)
        self.measurement_map, self.location_history = extract_measurement_map(self.payload)

    def test_extracts_measurement_map_and_location(self):
        self.assertIn("Air Temperature", self.measurement_map)
        self.assertEqual(len(self.location_history), 1)

    def test_pivots_one_event_per_timestamp(self):
        events = normalize_readings(
            self.measurement_map,
            location_history=self.location_history,
            device_sn_fallback="z6-30302",
        )
        self.assertEqual(len(events), 2)
        first = events[0]
        self.assertEqual(first["device_sn"], "z6-30302")
        self.assertEqual(first["device_name"], "North Field")
        self.assertEqual(first["air_temperature"], 24.7)
        self.assertEqual(first["air_temperature_unit"], "°C")
        self.assertEqual(first["relative_humidity"], 41.2)
        self.assertIsNone(first["wind_speed"])
        self.assertTrue(first["error_flag"])
        self.assertIn("Sensor is reporting an error", first["error_description"])
        self.assertEqual(first["metadata_errors"], "ORIENTATION_ERROR")
        self.assertAlmostEqual(first["latitude"], 46.7525833)
        self.assertAlmostEqual(first["longitude"], -117.1819223)
        self.assertEqual(first["vendor_product"], "ZENTRA Cloud")
        self.assertEqual(first["mrid"], 100)

        second = events[1]
        self.assertEqual(second["wind_speed"], 2.4)
        self.assertFalse(second["error_flag"])
        self.assertEqual(second["precipitation"], 0.0)
        self.assertNotIn("solar_radiation", second)

    def test_skips_checkpointed_mrids(self):
        events = normalize_readings(
            self.measurement_map,
            location_history=self.location_history,
            min_mrid=100,
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["mrid"], 101)

    def test_wrapped_data_payload(self):
        wrapped = {"data": self.payload, "pagination": {"next_page": None}}
        mapping, history = extract_measurement_map(wrapped)
        events = normalize_readings(mapping, location_history=history)
        self.assertEqual(len(events), 2)

    def test_timeseries_list_payload(self):
        payload = {
            "device": {
                "timeseries": [
                    {
                        "measurement": "Air Temperature",
                        "units": "°C",
                        "metadata": {"device_sn": "z6-30302"},
                        "values": [{"timestamp_utc": 10, "value": 12.5, "mrid": 1}],
                    }
                ]
            }
        }
        mapping, _history = extract_measurement_map(payload)
        events = normalize_readings(mapping, device_sn_fallback="z6-30302")
        self.assertEqual(events[0]["air_temperature"], 12.5)

    def test_location_picks_latest_not_after_timestamp(self):
        history = [
            {"Latitude": 1.0, "Longitude": 2.0, "timestamp_utc": 100},
            {"Latitude": 3.0, "Longitude": 4.0, "timestamp_utc": 300},
        ]
        coords = location_at(history, 200)
        self.assertEqual(coords["latitude"], 1.0)
        later = location_at(history, 400)
        self.assertEqual(later["latitude"], 3.0)


if __name__ == "__main__":
    unittest.main()
