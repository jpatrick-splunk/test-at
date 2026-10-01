import json
import sys
import unittest
from pathlib import Path
from urllib.error import HTTPError
from io import BytesIO

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from zentra.client import (  # noqa: E402
    authorization_header,
    build_readings_url,
    fetch_readings_page,
    iter_readings_pages,
    next_page_number,
    ZentraAPIError,
)


class FakeResponse:
    def __init__(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8") if not isinstance(payload, bytes) else payload
        self._body = body
        self.status = status

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ClientTests(unittest.TestCase):
    def test_authorization_header_adds_token_prefix(self):
        self.assertEqual(authorization_header("abc123"), "Token abc123")
        self.assertEqual(authorization_header("Token abc123"), "Token abc123")

    def test_build_url_uses_v4_and_mrid_over_dates(self):
        url = build_readings_url(
            "https://zentracloud.com",
            "z6-30302",
            start_date="2024-01-01 00:00:00",
            start_mrid=50,
        )
        self.assertTrue(url.startswith("https://zentracloud.com/api/v4/get_readings/"))
        self.assertIn("device_sn=z6-30302", url)
        self.assertIn("start_mrid=50", url)
        self.assertNotIn("start_date=", url)
        self.assertIn("output_format=json", url)
        self.assertIn("sort_by=ascending", url)
        self.assertIn("location=true", url)

    def test_fetch_readings_page_sends_auth_and_parses_json(self):
        captured = {}

        def fake_urlopen(request, timeout=None, context=None):
            captured["url"] = request.full_url
            captured["auth"] = request.get_header("Authorization")
            return FakeResponse({"Air Temperature": []})

        payload = fetch_readings_page(
            "https://zentracloud.com",
            "secret-token",
            "z6-30302",
            start_mrid=10,
            urlopen=fake_urlopen,
        )
        self.assertEqual(payload["Air Temperature"], [])
        self.assertEqual(captured["auth"], "Token secret-token")
        self.assertIn("z6-30302", captured["url"])
        self.assertNotIn("secret-token", captured["url"])

    def test_http_error_does_not_include_token(self):
        def fake_urlopen(request, timeout=None, context=None):
            raise HTTPError(
                request.full_url,
                401,
                "Unauthorized",
                hdrs=None,
                fp=BytesIO(b'{"error":"bad token xyz"}'),
            )

        with self.assertRaises(ZentraAPIError) as ctx:
            fetch_readings_page(
                "https://zentracloud.com",
                "super-secret",
                "z6-30302",
                urlopen=fake_urlopen,
            )
        self.assertEqual(ctx.exception.status, 401)
        self.assertNotIn("super-secret", str(ctx.exception))

    def test_next_page_from_pagination(self):
        self.assertEqual(next_page_number({"pagination": {"next_page": 2}}, 1, 2000), 2)
        self.assertIsNone(next_page_number({"pagination": {"next_page": None}}, 1, 2000))

    def test_iter_pages_stops_and_sleeps_between_pages(self):
        calls = {"n": 0, "sleeps": []}

        def fake_urlopen(request, timeout=None, context=None):
            calls["n"] += 1
            page = 1 if "page_num=1" in request.full_url else 2
            readings = [{"metadata": {}, "readings": [{"timestamp_utc": i, "value": 1, "mrid": i} for i in range(5)]}]
            if page == 1:
                return FakeResponse(
                    {"Air Temperature": readings, "pagination": {"next_page": 2}}
                )
            return FakeResponse(
                {"Air Temperature": readings, "pagination": {"next_page": None}}
            )

        pages = list(
            iter_readings_pages(
                "https://zentracloud.com",
                "tok",
                "z6-30302",
                start_mrid=1,
                per_page=5,
                sleep_fn=lambda seconds: calls["sleeps"].append(seconds),
                urlopen=fake_urlopen,
                page_gap_seconds=61,
            )
        )
        self.assertEqual(len(pages), 2)
        self.assertEqual(calls["sleeps"], [61])


if __name__ == "__main__":
    unittest.main()
