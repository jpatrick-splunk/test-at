import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from zentra.client import extract_measurement_map  # noqa: E402
from zentra.normalize import (  # noqa: E402
    aggregation_for,
    canonical_measurement_name,
    flatten_readings,
    location_at,
    normalize_readings,
)

FIXTURE = ROOT / "tests" / "fixtures" / "readings_v4.json"


class CanonicalNameTests(unittest.TestCase):
    def test_known_aliases(self):
        self.assertEqual(canonical_measurement_name("Air Temperature"), "air_temperature")
        self.assertEqual(canonical_measurement_name("Wind Gust"), "gust_speed")
        self.assertEqual(canonical_measurement_name("Signal Strength"), "signal_percent")
        self.assertEqual(canonical_measurement_name("Precipitation"), "precipitation")
        self.assertEqual(canonical_measurement_name("Precipitation (mm)"), "precipitation")
        self.assertEqual(canonical_measurement_name("Precipitation mm"), "precipitation")
        self.assertEqual(canonical_measurement_name("Daily Rainfall"), "daily_rainfall")
        self.assertEqual(canonical_measurement_name("Cumulative Precipitation"), "cumulative_precipitation")
        self.assertEqual(canonical_measurement_name("Max Precipitation Rate"), "max_precipitation_rate")

    def test_interval_precip_sums_to_period_total(self):
        self.assertEqual(aggregation_for("precipitation"), "sum")
        self.assertEqual(aggregation_for("air_temperature"), "avg")
        self.assertEqual(aggregation_for("gust_speed"), "max")

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

    def test_flattens_one_event_per_measurement_sample(self):
        events = flatten_readings(
            self.measurement_map,
            location_history=self.location_history,
            device_sn_fallback="z6-30302",
        )
        self.assertEqual(len(events), 9)
        precip = [e for e in events if e["measurement"] == "Precipitation"]
        self.assertEqual(len(precip), 2)
        self.assertEqual(precip[0]["value"], 0.2)
        self.assertEqual(precip[0]["units"], "mm")
        self.assertEqual(precip[0]["aggregation"], "sum")
        self.assertEqual(precip[0]["measurement_canonical"], "precipitation")
        self.assertEqual(precip[1]["value"], 0.0)
        daily_total = sum(e["value"] for e in precip)
        self.assertAlmostEqual(daily_total, 0.2)
        temp = [e for e in events if e["measurement"] == "Air Temperature"]
        self.assertEqual(temp[0]["aggregation"], "avg")
        self.assertEqual(temp[0]["value"], 24.7)

    def test_flatten_skips_checkpointed_mrids(self):
        events = flatten_readings(
            self.measurement_map,
            location_history=self.location_history,
            min_mrid=100,
        )
        self.assertEqual(len(events), 4)
        self.assertTrue(all(e["mrid"] == 101 for e in events))

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

    def test_pandas_split_payload(self):
        payload = {
            "data": json.dumps(
                {
                    "columns": ["timestamp_utc", "Air Temperature", "Precipitation"],
                    "data": [
                        [1720000000, 24.7, 1.2],
                        [1720000900, 25.1, 0.0],
                    ],
                }
            )
        }
        mapping, _history = extract_measurement_map(payload)
        events = normalize_readings(mapping, device_sn_fallback="z6-30302")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["air_temperature"], 24.7)
        self.assertEqual(events[0]["precipitation"], 1.2)

    def test_v5_values_payload(self):
        payload = {
            "metadata": {"device_id": "z6-30302"},
            "values": [
                {
                    "measurement": "Precipitation",
                    "unit": "mm",
                    "value": 3.4,
                    "timestamp": 1720000000,
                    "reading_id": 50,
                    "error_code": 0,
                    "device_id": "z6-30302",
                },
                {
                    "measurement": "Air Temperature",
                    "unit": "°C",
                    "value": 18.1,
                    "timestamp": 1720000000,
                    "reading_id": 50,
                    "error_code": 0,
                    "device_id": "z6-30302",
                },
            ],
        }
        mapping, _history = extract_measurement_map(payload)
        events = normalize_readings(mapping, device_sn_fallback="z6-30302")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["precipitation"], 3.4)
        self.assertEqual(events[0]["air_temperature"], 18.1)

    def test_location_picks_latest_not_after_timestamp(self):
        history = [
            {"Latitude": 1.0, "Longitude": 2.0, "timestamp_utc": 100},
            {"Latitude": 3.0, "Longitude": 4.0, "timestamp_utc": 300},
        ]
        coords = location_at(history, 200)
        self.assertEqual(coords["latitude"], 1.0)
        later = location_at(history, 400)
        self.assertEqual(later["latitude"], 3.0)

    def test_columnar_readings_become_interval_events(self):
        mapping = {
            "Precipitation": [
                {
                    "metadata": {"device_sn": "z6-30302", "units": "mm"},
                    "readings": {
                        "timestamp_utc": [1720000000, 1720000900],
                        "value": [0.2, 0.4],
                        "mrid": [100, 101],
                    },
                }
            ]
        }
        events = flatten_readings(mapping, device_sn_fallback="z6-30302")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["value"], 0.2)
        self.assertEqual(events[1]["value"], 0.4)
        self.assertAlmostEqual(sum(e["value"] for e in events), 0.6)

    def test_data_key_readings(self):
        mapping = {
            "Air Temperature": [
                {
                    "metadata": {"device_sn": "z6-30302", "units": "°C"},
                    "data": [{"timestamp_utc": 1720000000, "value": 24.7, "mrid": 1}],
                }
            ]
        }
        events = flatten_readings(mapping)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["value"], 24.7)
        self.assertEqual(events[0]["measurement"], "Air Temperature")

    def test_list_of_one_columnar_dict(self):
        mapping = {
            "Air Temperature": [
                {
                    "metadata": {"device_sn": "z6-30302", "units": "°C"},
                    "readings": [
                        {
                            "timestamp_utc": [1720000000, 1720000900],
                            "value": [24.7, 25.1],
                            "mrid": [100, 101],
                        }
                    ],
                }
            ]
        }
        events = flatten_readings(mapping, device_sn_fallback="z6-30302")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["value"], 24.7)
        self.assertEqual(events[1]["value"], 25.1)

    def test_datetime_only_reading(self):
        mapping = {
            "Air Temperature": [
                {
                    "metadata": {"units": "°C"},
                    "readings": [
                        {
                            "datetime": "2026-10-01 12:00:00-05:00",
                            "value": 18.2,
                            "mrid": 9,
                        }
                    ],
                }
            ]
        }
        events = flatten_readings(mapping)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["value"], 18.2)
        self.assertTrue(events[0]["timestamp_utc"])


if __name__ == "__main__":
    unittest.main()
