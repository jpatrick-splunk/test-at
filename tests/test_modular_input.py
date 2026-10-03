import io
import json
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import zentra_weather as mi  # noqa: E402
from zentra.config import ConfigError  # noqa: E402


FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "readings_v4.json").read_text())

RUN_XML_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<input>
  <checkpoint_dir>{checkpoint_dir}</checkpoint_dir>
  <configuration>
    <stanza name="zentra_weather://validate">
      <param name="index">zentra_validate</param>
      <param name="sourcetype">zentra:reading</param>
      <param name="device_sns">{devices}</param>
      <param name="api_token">test-token</param>
      <param name="api_base_url">https://zentracloud.com</param>
      <param name="lookback_hours">24</param>
      <param name="per_page">2000</param>
      <param name="ignore_checkpoint">0</param>
      <param name="output_format">json</param>
    </stanza>
  </configuration>
</input>
"""


class PollLockTests(unittest.TestCase):
    def test_second_poll_is_skipped_while_the_first_holds_the_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = mi._acquire_poll_lock(tmp)
            self.assertIsNotNone(first)
            second = mi._acquire_poll_lock(tmp)
            self.assertIsNone(second)
            mi._release_poll_lock(first)
            third = mi._acquire_poll_lock(tmp)
            self.assertIsNotNone(third)
            mi._release_poll_lock(third)


class SchemeTests(unittest.TestCase):
    def test_scheme_xml(self):
        buf = io.StringIO()
        mi.do_scheme(buf)
        root = ET.fromstring(buf.getvalue())
        self.assertEqual(root.findtext("title"), "ZENTRA Cloud Weather")
        self.assertEqual(root.findtext("streaming_mode"), "xml")
        self.assertEqual(root.findtext("use_single_instance"), "true")
        arg_names = [arg.get("name") for arg in root.findall("./endpoint/args/arg")]
        self.assertIn("device_sns", arg_names)
        self.assertIn("api_token", arg_names)
        self.assertIn("api_base_url", arg_names)
        self.assertIn("output_format", arg_names)
        self.assertIn("ignore_checkpoint", arg_names)


class ValidationTests(unittest.TestCase):
    def test_rejects_more_than_max_devices(self):
        devices = ",".join("a-%d" % n for n in range(26))
        xml = """<items><item name="zentra_weather://x">
            <param name="device_sns">{devices}</param>
            <param name="api_token">t</param>
        </item></items>""".format(devices=devices)
        with self.assertRaises(ConfigError):
            mi.validate_arguments(xml)

    def test_accepts_default_logger(self):
        xml = """<items><item name="zentra_weather://x">
            <param name="api_token">t</param>
        </item></items>"""
        mi.validate_arguments(xml)


class RunTests(unittest.TestCase):
    def test_indexes_interval_readings(self):
        with tempfile.TemporaryDirectory() as tmp:
            xml = RUN_XML_TEMPLATE.format(checkpoint_dir=tmp, devices="z6-30302")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch("zentra_weather.iter_readings_pages", return_value=[FIXTURE]), patch(
                "zentra_weather.start_stream"
            ), patch("zentra_weather.end_stream"), patch(
                "zentra_weather.write_event"
            ) as write_event, patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                rc = mi.run(xml)
            self.assertEqual(rc, 0)
            self.assertIn("index=zentra_validate", stderr.getvalue())
            self.assertIn("token=set", stderr.getvalue())
            self.assertEqual(write_event.call_count, 9)
            first = write_event.call_args_list[0].kwargs.get("data") or write_event.call_args_list[0][0][0]
            if not isinstance(first, dict):
                first = write_event.call_args_list[0][0][0]
            self.assertEqual(first["device_sn"], "z6-30302")
            self.assertIn("measurement", first)
            self.assertIn("value", first)
            precip = [
                (c.kwargs.get("data") or c[0][0])
                for c in write_event.call_args_list
                if (c.kwargs.get("data") or c[0][0]).get("measurement") == "Precipitation"
            ]
            self.assertEqual(len(precip), 2)
            self.assertEqual(sum(e["value"] for e in precip), 0.2)
            ckpt = json.loads(
                Path(tmp, "zentra_weather_validate", "z6-30302.json").read_text()
            )
            self.assertEqual(ckpt["last_mrid"], 101)

    def test_second_poll_skips_seen_mrids(self):
        with tempfile.TemporaryDirectory() as tmp:
            ckpt_dir = Path(tmp, "zentra_weather_validate")
            ckpt_dir.mkdir()
            (ckpt_dir / "z6-30302.json").write_text(
                json.dumps({"last_mrid": 100, "device_sn": "z6-30302"}), encoding="utf-8"
            )
            xml = RUN_XML_TEMPLATE.format(checkpoint_dir=tmp, devices="z6-30302")
            with patch("zentra_weather.iter_readings_pages", return_value=[FIXTURE]) as pages, patch(
                "zentra_weather.start_stream"
            ), patch("zentra_weather.end_stream"), patch("zentra_weather.write_event") as write_event:
                mi.run(xml)
            self.assertEqual(write_event.call_count, 4)
            kwargs = pages.call_args.kwargs
            self.assertIsNone(kwargs.get("start_mrid"))
            self.assertIsNotNone(kwargs.get("start_date"))
            self.assertIsNotNone(kwargs.get("end_date"))

    def test_api_dates_use_logger_local_clock(self):
        # 2026-09-24 04:30:00 UTC is 2026-09-23 23:30:00 in Central time.
        moment = datetime(2026, 9, 24, 4, 30, tzinfo=timezone.utc)
        self.assertEqual(mi._api_local_clock(moment), "2026-09-23 23:30:00")

    def test_september_23_checkpoint_catches_up_with_date_range(self):
        # Last indexed ATMOS 41 sample on z6-30302: 2026-09-23 23:45:00-05:00.
        last_ts = 1790225100
        with tempfile.TemporaryDirectory() as tmp:
            ckpt_dir = Path(tmp, "zentra_weather_validate")
            ckpt_dir.mkdir()
            (ckpt_dir / "z6-30302.json").write_text(
                json.dumps(
                    {
                        "last_mrid": 59781,
                        "last_timestamp_utc": last_ts,
                        "device_sn": "z6-30302",
                    }
                ),
                encoding="utf-8",
            )
            xml = RUN_XML_TEMPLATE.format(checkpoint_dir=tmp, devices="z6-30302")

            class FrozenDateTime(datetime):
                @classmethod
                def now(cls, tz=None):
                    aware = datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc)
                    return aware if tz is None else aware.astimezone(tz)

            stderr = io.StringIO()
            with patch("zentra_weather.datetime", FrozenDateTime), patch(
                "time.sleep"
            ), patch(
                "zentra_weather.iter_readings_pages", return_value=[FIXTURE]
            ) as pages, patch("zentra_weather.start_stream"), patch(
                "zentra_weather.end_stream"
            ), patch("zentra_weather.write_event"), patch("sys.stderr", stderr):
                mi.run(xml)
            self.assertGreaterEqual(pages.call_count, 1)
            kwargs = pages.call_args_list[0].kwargs
            self.assertIsNone(kwargs.get("start_mrid"))
            self.assertEqual(kwargs.get("start_date"), "2026-09-23 23:30:00")
            self.assertEqual(kwargs.get("end_date"), "2026-10-02 15:00:00")
            logs = stderr.getvalue()
            self.assertIn("window=catch-up", logs)
            self.assertIn("last_mrid=59781", logs)
            self.assertIn("without skip_mrid", logs)

    def test_recent_checkpoint_does_not_retry_720h_lookback(self):
        last_ts = int(datetime(2026, 10, 2, 19, 45, tzinfo=timezone.utc).timestamp())
        with tempfile.TemporaryDirectory() as tmp:
            ckpt_dir = Path(tmp, "zentra_weather_validate")
            ckpt_dir.mkdir()
            (ckpt_dir / "z6-30302.json").write_text(
                json.dumps(
                    {
                        "last_mrid": 60612,
                        "last_timestamp_utc": last_ts,
                        "device_sn": "z6-30302",
                    }
                ),
                encoding="utf-8",
            )
            xml = RUN_XML_TEMPLATE.format(checkpoint_dir=tmp, devices="z6-30302")

            class FrozenDateTime(datetime):
                @classmethod
                def now(cls, tz=None):
                    aware = datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc)
                    return aware if tz is None else aware.astimezone(tz)

            stderr = io.StringIO()
            with patch("zentra_weather.datetime", FrozenDateTime), patch(
                "zentra_weather.time.sleep"
            ), patch(
                "zentra_weather.iter_readings_pages", return_value=[FIXTURE]
            ) as pages, patch("zentra_weather.start_stream"), patch(
                "zentra_weather.end_stream"
            ), patch("zentra_weather.write_event"), patch("sys.stderr", stderr):
                mi.run(xml)
            logs = stderr.getvalue()
            self.assertIn("not retrying", logs)
            self.assertNotIn("retrying 720h lookback from logger datetime", logs)
            self.assertIn("window=gap-fill", logs)
            self.assertGreaterEqual(pages.call_count, 2)
            fill = pages.call_args_list[1].kwargs
            self.assertEqual(fill.get("start_date"), "2026-09-23 14:45:00")
            self.assertEqual(fill.get("end_date"), "2026-10-02 14:30:00")
            ckpt = json.loads((ckpt_dir / "z6-30302.json").read_text())
            self.assertTrue(ckpt.get("gap_fill_complete"))

    def test_gap_fill_complete_skips_second_pull(self):
        last_ts = int(datetime(2026, 10, 2, 19, 45, tzinfo=timezone.utc).timestamp())
        with tempfile.TemporaryDirectory() as tmp:
            ckpt_dir = Path(tmp, "zentra_weather_validate")
            ckpt_dir.mkdir()
            (ckpt_dir / "z6-30302.json").write_text(
                json.dumps(
                    {
                        "last_mrid": 60612,
                        "last_timestamp_utc": last_ts,
                        "device_sn": "z6-30302",
                        "gap_fill_complete": True,
                    }
                ),
                encoding="utf-8",
            )
            xml = RUN_XML_TEMPLATE.format(checkpoint_dir=tmp, devices="z6-30302")

            class FrozenDateTime(datetime):
                @classmethod
                def now(cls, tz=None):
                    aware = datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc)
                    return aware if tz is None else aware.astimezone(tz)

            with patch("zentra_weather.datetime", FrozenDateTime), patch(
                "zentra_weather.time.sleep"
            ) as slept, patch(
                "zentra_weather.iter_readings_pages", return_value=[FIXTURE]
            ) as pages, patch("zentra_weather.start_stream"), patch(
                "zentra_weather.end_stream"
            ), patch("zentra_weather.write_event"):
                mi.run(xml)
            self.assertEqual(pages.call_count, 1)
            slept.assert_not_called()

    def test_ignore_checkpoint_with_stale_ckpt_resumes_from_last_datetime(self):
        last_ts = 1790225100
        with tempfile.TemporaryDirectory() as tmp:
            ckpt_dir = Path(tmp, "zentra_weather_validate")
            ckpt_dir.mkdir()
            (ckpt_dir / "z6-30302.json").write_text(
                json.dumps(
                    {
                        "last_mrid": 59781,
                        "last_timestamp_utc": last_ts,
                        "device_sn": "z6-30302",
                    }
                ),
                encoding="utf-8",
            )
            xml = RUN_XML_TEMPLATE.format(checkpoint_dir=tmp, devices="z6-30302").replace(
                "<param name=\"ignore_checkpoint\">0</param>",
                "<param name=\"ignore_checkpoint\">1</param>",
            )

            class FrozenDateTime(datetime):
                @classmethod
                def now(cls, tz=None):
                    aware = datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc)
                    return aware if tz is None else aware.astimezone(tz)

            stderr = io.StringIO()
            with patch("zentra_weather.datetime", FrozenDateTime), patch(
                "time.sleep"
            ), patch(
                "zentra_weather.iter_readings_pages", return_value=[FIXTURE]
            ) as pages, patch("zentra_weather.start_stream"), patch(
                "zentra_weather.end_stream"
            ), patch("zentra_weather.write_event"), patch("sys.stderr", stderr):
                mi.run(xml)
            kwargs = pages.call_args_list[0].kwargs
            self.assertIsNone(kwargs.get("start_mrid"))
            self.assertEqual(kwargs.get("start_date"), "2026-09-23 23:30:00")
            logs = stderr.getvalue()
            self.assertIn("resuming from last logger datetime", logs)
            self.assertNotIn("window=lookback ", logs)

    def test_ignore_checkpoint_still_skips_already_indexed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ckpt_dir = Path(tmp, "zentra_weather_validate")
            ckpt_dir.mkdir()
            (ckpt_dir / "z6-30302.json").write_text(
                json.dumps(
                    {
                        "last_mrid": 101,
                        "last_timestamp_utc": 2000000000,
                        "device_sn": "z6-30302",
                    }
                ),
                encoding="utf-8",
            )
            xml = RUN_XML_TEMPLATE.format(checkpoint_dir=tmp, devices="z6-30302").replace(
                "<param name=\"ignore_checkpoint\">0</param>",
                "<param name=\"ignore_checkpoint\">1</param>",
            )
            with patch("zentra_weather.iter_readings_pages", return_value=[FIXTURE]) as pages, patch(
                "zentra_weather.start_stream"
            ), patch("zentra_weather.end_stream"), patch("zentra_weather.write_event") as write_event:
                mi.run(xml)
            self.assertEqual(write_event.call_count, 0)
            self.assertIsNone(pages.call_args.kwargs.get("start_mrid"))
            self.assertIsNotNone(pages.call_args.kwargs.get("start_date"))


class StreamTests(unittest.TestCase):
    def test_event_xml_escapes_payload(self):
        from zentra.stream import write_event

        buf = io.StringIO()
        write_event(
            {"timestamp_utc": 1, "device_sn": "z6-30302", "note": "<alert>"},
            stanza="zentra_weather://field_loggers",
            sourcetype="zentra:reading",
            index="zentra_validate",
            source="zentra_weather://z6-30302",
            out=buf,
        )
        xml = buf.getvalue()
        self.assertNotIn("unbroken", xml)
        tree = ET.fromstring(xml)
        self.assertEqual(tree.findtext("sourcetype"), "zentra:reading")
        self.assertEqual(tree.findtext("index"), "zentra_validate")
        payload = json.loads(tree.findtext("data"))
        self.assertEqual(payload["note"], "<alert>")

    def test_each_reading_is_a_complete_event(self):
        from zentra.stream import write_event

        buf = io.StringIO()
        write_event(
            {"timestamp_utc": 1, "measurement": "Precipitation", "value": 0.1},
            stanza="zentra_weather://validate",
            sourcetype="zentra:reading",
            index="zentra_validate",
            out=buf,
        )
        write_event(
            {"timestamp_utc": 2, "measurement": "Precipitation", "value": 0.2},
            stanza="zentra_weather://validate",
            sourcetype="zentra:reading",
            index="zentra_validate",
            out=buf,
        )
        xml = buf.getvalue()
        self.assertNotIn('unbroken="1"', xml)
        root = ET.fromstring("<stream>%s</stream>" % xml)
        events = root.findall("event")
        self.assertEqual(len(events), 2)
        self.assertEqual(json.loads(events[0].findtext("data"))["value"], 0.1)
        self.assertEqual(json.loads(events[1].findtext("data"))["value"], 0.2)


class AppPackagingTests(unittest.TestCase):
    def test_required_files_exist(self):
        app = ROOT
        required = [
            app / "default" / "app.conf",
            app / "default" / "inputs.conf",
            app / "default" / "props.conf",
            app / "default" / "datamodels.conf",
            app / "default" / "data" / "models" / "Weather.json",
            app / "default" / "data" / "ui" / "nav" / "default.xml",
            app / "default" / "data" / "ui" / "views" / "collection_status.xml",
            app / "default" / "data" / "ui" / "views" / "rainfall_totals.xml",
            app / "default" / "data" / "ui" / "views" / "reading_validation.xml",
            app / "default" / "data" / "ui" / "views" / "weather_overview.xml",
            app / "default" / "data" / "ui" / "views" / "logger_detail.xml",
            app / "default" / "data" / "ui" / "views" / "data_quality.xml",
            app / "README" / "inputs.conf.spec",
            app / "bin" / "zentra_weather.py",
            app / "metadata" / "default.meta",
        ]
        for path in required:
            self.assertTrue(path.is_file(), msg="missing %s" % path)

    def test_weather_model_and_dashboards_parse(self):
        app = ROOT
        model = json.loads((app / "default" / "data" / "models" / "Weather.json").read_text())
        self.assertEqual(model["modelName"], "Weather")
        self.assertEqual(model["objects"][0]["parentName"], "BaseEvent")
        field_names = {f["fieldName"] for f in model["objects"][0]["fields"]}
        for name in (
            "device_sn",
            "measurement",
            "measurement_canonical",
            "value",
            "datetime",
            "logger_day",
            "units",
            "error_flag",
        ):
            self.assertIn(name, field_names)
        self.assertNotIn("air_temperature", field_names)
        object_names = {obj["objectName"] for obj in model["objects"]}
        self.assertIn("Precipitation", object_names)
        self.assertIn("AirTemperature", object_names)
        self.assertEqual(model["objects"][0]["constraints"][0]["search"], "sourcetype=zentra:reading measurement=*")
        for view in (
            "collection_status.xml",
            "rainfall_totals.xml",
            "reading_validation.xml",
            "weather_overview.xml",
            "logger_detail.xml",
            "data_quality.xml",
            "ranch_overview.xml",
            "place_detail.xml",
            "places.xml",
            "logger_health.xml",
        ):
            ET.parse(app / "default" / "data" / "ui" / "views" / view)
            text = (app / "default" / "data" / "ui" / "views" / view).read_text()
            self.assertNotIn("| datamodel Weather Weather search", text)
            self.assertNotIn("_time=_time+coalesce", text)
        nav = (app / "default" / "data" / "ui" / "nav" / "default.xml").read_text()
        ET.parse(app / "default" / "data" / "ui" / "nav" / "default.xml")
        self.assertIn('view name="weather_overview"', nav)
        self.assertIn('view name="rainfall_totals"', nav)
        self.assertIn('view name="ranch_overview" default="true"', nav)
        self.assertIn('view name="places"', nav)
        macros = (app / "default" / "macros.conf").read_text()
        self.assertIn("[zentra_canonical(1)]", macros)
        self.assertIn("[zentra_precip_rain]", macros)
        self.assertIn('measurement="$name$"', macros)
        validation = (app / "default" / "data" / "ui" / "views" / "reading_validation.xml").read_text()
        self.assertIn("`zentra_precip_rain`", validation)
        self.assertIn("| table datetime,", validation)
        overview = (app / "default" / "data" / "ui" / "views" / "weather_overview.xml").read_text()
        self.assertNotIn("geostats", overview)
        self.assertIn("`zentra_canonical(air_temperature)`", overview)

    def test_default_inputs_have_no_token(self):
        text = (ROOT / "default" / "inputs.conf").read_text()
        self.assertIn("device_sns = z6-30302", text)
        self.assertIn("index = zentra_validate", text)
        self.assertIn("sourcetype = zentra:reading", text)
        self.assertIn("ignore_checkpoint = 0", text)
        self.assertIn("interval = 900", text)
        self.assertNotRegex(text, r"(?i)^api_token\s*=\s*\S+", "token must not be hardcoded")

    def test_python_sources_compile(self):
        import py_compile

        bin_dir = ROOT / "bin"
        for path in list(bin_dir.glob("*.py")) + list((bin_dir / "zentra").glob("*.py")):
            py_compile.compile(str(path), doraise=True)


if __name__ == "__main__":
    unittest.main()
