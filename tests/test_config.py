import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from zentra.checkpoint import load_checkpoint, save_checkpoint  # noqa: E402
from zentra.config import (  # noqa: E402
    ConfigError,
    index_storage_exists,
    parse_device_sns,
    parse_input_xml,
    resolve_api_token,
    stanza_settings,
    validate_api_base_url,
)


RUN_XML = """<?xml version="1.0" encoding="utf-8"?>
<input>
  <server_host>tiny</server_host>
  <server_uri>https://127.0.0.1:8089</server_uri>
  <checkpoint_dir>/tmp/zentra_ckpt</checkpoint_dir>
  <session_key>not-a-real-session</session_key>
  <configuration>
    <stanza name="zentra_weather://field_loggers">
      <param name="index">weather</param>
      <param name="interval">300</param>
      <param name="device_sns">z6-30302</param>
      <param name="api_token">from-stanza</param>
      <param name="api_base_url">https://zentracloud.com</param>
      <param name="lookback_hours">12</param>
    </stanza>
  </configuration>
</input>
"""

VALIDATE_XML = """<?xml version="1.0" encoding="utf-8"?>
<items>
  <server_host>tiny</server_host>
  <server_uri>https://127.0.0.1:8089</server_uri>
  <checkpoint_dir>/tmp/zentra_ckpt</checkpoint_dir>
  <item name="zentra_weather://field_loggers">
    <param name="device_sns">z6-30302, z6-11111</param>
    <param name="api_token">from-item</param>
    <param name="index">weather</param>
  </item>
</items>
"""


class DeviceListTests(unittest.TestCase):
    def test_default_serial(self):
        self.assertEqual(parse_device_sns(""), ["z6-30302"])
        self.assertEqual(parse_device_sns(None), ["z6-30302"])

    def test_splits_and_dedupes(self):
        self.assertEqual(
            parse_device_sns("z6-30302, z6-11111 z6-30302"),
            ["z6-30302", "z6-11111"],
        )

    def test_accepts_more_than_three(self):
        self.assertEqual(len(parse_device_sns("a-1, a-2, a-3, a-4")), 4)

    def test_rejects_more_than_max(self):
        with self.assertRaises(ConfigError):
            parse_device_sns(",".join("a-%d" % n for n in range(26)))

    def test_rejects_bad_serial(self):
        with self.assertRaises(ConfigError):
            parse_device_sns("../etc/passwd")


class TokenTests(unittest.TestCase):
    def test_prefers_explicit_param(self):
        token = resolve_api_token("Token abc", environ={"ZENTRA_API_TOKEN": "env"})
        self.assertEqual(token, "abc")

    def test_falls_back_to_env(self):
        token = resolve_api_token("", environ={"ZENTRA_API_TOKEN": "env-token"})
        self.assertEqual(token, "env-token")

    def test_missing_token(self):
        with self.assertRaises(ConfigError):
            resolve_api_token("", environ={})


class IndexStorageTests(unittest.TestCase):
    def test_missing_index_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"SPLUNK_HOME": tmp}
            self.assertFalse(index_storage_exists("zentra_validate", environ=env))

    def test_existing_index_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "var", "lib", "splunk", "zentra_validate"))
            env = {"SPLUNK_HOME": tmp}
            self.assertTrue(index_storage_exists("zentra_validate", environ=env))

    def test_unknown_without_splunk_home(self):
        self.assertIsNone(index_storage_exists("zentra_validate", environ={}))


class UrlTests(unittest.TestCase):
    def test_requires_https(self):
        self.assertEqual(
            validate_api_base_url("https://zentracloud.eu/"),
            "https://zentracloud.eu",
        )
        with self.assertRaises(ConfigError):
            validate_api_base_url("http://evil.example")
        validate_api_base_url("http://localhost:8080")


class XmlTests(unittest.TestCase):
    def test_run_xml(self):
        cfg = parse_input_xml(RUN_XML)
        self.assertEqual(cfg.checkpoint_dir, "/tmp/zentra_ckpt")
        settings = stanza_settings(cfg.stanzas[0])
        self.assertEqual(settings["device_sns"], ["z6-30302"])
        self.assertEqual(settings["api_token"], "from-stanza")
        self.assertEqual(settings["lookback_hours"], 12)
        self.assertFalse(settings["ignore_checkpoint"])
        self.assertNotIn("session_key", settings)

    def test_validation_xml(self):
        cfg = parse_input_xml(VALIDATE_XML)
        settings = stanza_settings(cfg.stanzas[0])
        self.assertEqual(settings["device_sns"], ["z6-30302", "z6-11111"])


class CheckpointTests(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            save_checkpoint(tmp, "z6-30302", last_mrid=10, last_timestamp_utc=100)
            save_checkpoint(tmp, "z6-30302", last_mrid=9, last_timestamp_utc=200)
            data = load_checkpoint(tmp, "z6-30302")
            self.assertEqual(data["last_mrid"], 10)
            self.assertEqual(data["last_timestamp_utc"], 200)

    def test_extra_fields_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            save_checkpoint(
                tmp, "z6-30302", last_mrid=1, last_timestamp_utc=10, extra={"gap_fill_complete": True}
            )
            data = load_checkpoint(tmp, "z6-30302")
            self.assertTrue(data["gap_fill_complete"])
            self.assertEqual(data["last_mrid"], 1)

    def test_namespace_is_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            save_checkpoint(tmp, "z6-30302", last_mrid=1, namespace="zentra_weather://field_loggers")
            save_checkpoint(tmp, "z6-30302", last_mrid=50, namespace="zentra_weather://validate")
            old = load_checkpoint(tmp, "z6-30302", namespace="zentra_weather://field_loggers")
            fresh = load_checkpoint(tmp, "z6-30302", namespace="zentra_weather://validate")
            self.assertEqual(old["last_mrid"], 1)
            self.assertEqual(fresh["last_mrid"], 50)


if __name__ == "__main__":
    unittest.main()
