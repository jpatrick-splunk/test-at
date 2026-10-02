import io
import json
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
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
    def test_rejects_four_devices(self):
        xml = """<items><item name="zentra_weather://x">
            <param name="device_sns">a-1,a-2,a-3,a-4</param>
            <param name="api_token">t</param>
        </item></items>"""
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
            self.assertEqual(kwargs["start_mrid"], 101)


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
        tree = ET.fromstring(buf.getvalue())
        self.assertEqual(tree.findtext("sourcetype"), "zentra:reading")
        self.assertEqual(tree.findtext("index"), "zentra_validate")
        payload = json.loads(tree.findtext("data"))
        self.assertEqual(payload["note"], "<alert>")


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
        for name in ("device_sn", "air_temperature", "wind_speed", "precipitation", "error_flag"):
            self.assertIn(name, field_names)
        for view in (
            "collection_status.xml",
            "rainfall_totals.xml",
            "reading_validation.xml",
            "weather_overview.xml",
            "logger_detail.xml",
            "data_quality.xml",
        ):
            ET.parse(app / "default" / "data" / "ui" / "views" / view)
        ET.parse(app / "default" / "data" / "ui" / "nav" / "default.xml")

    def test_default_inputs_have_no_token(self):
        text = (ROOT / "default" / "inputs.conf").read_text()
        self.assertIn("device_sns = z6-30302", text)
        self.assertIn("index = zentra_validate", text)
        self.assertIn("sourcetype = zentra:reading", text)
        self.assertNotRegex(text, r"(?i)^api_token\s*=\s*\S+", "token must not be hardcoded")


if __name__ == "__main__":
    unittest.main()
