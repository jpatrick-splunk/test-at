"""HTTPS client for ZENTRA Cloud v4 ``/api/v4/get_readings/``."""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from zentra import DEFAULT_API_BASE_URL

READINGS_PATH = "/api/v4/get_readings/"
USER_AGENT = "zentra_weather/1.0 (Splunk modular input)"
DEFAULT_TIMEOUT = 60
DEVICE_CALL_GAP_SECONDS = 61
MAX_PAGES = 24

UrlOpen = Callable[..., Any]


class ZentraAPIError(RuntimeError):
    """Raised when ZENTRA Cloud returns an error or unexpected payload."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def _ssl_context() -> ssl.SSLContext:
    # Use the platform trust store. Do not disable verification and do not
    # embed certificates in the app.
    return ssl.create_default_context()


def authorization_header(token: str) -> str:
    cleaned = token.strip()
    if cleaned.lower().startswith("token "):
        return cleaned
    return "Token %s" % cleaned


def build_readings_url(
    api_base_url: str,
    device_sn: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    start_mrid: Optional[int] = None,
    end_mrid: Optional[int] = None,
    page_num: int = 1,
    per_page: int = 2000,
    sort_by: str = "asc",
    location: bool = True,
) -> str:
    base = api_base_url.rstrip("/") + READINGS_PATH
    query: Dict[str, Any] = {
        "device_sn": device_sn,
        "output_format": "json",
        "page_num": str(page_num),
        "per_page": str(per_page),
        "sort_by": sort_by,
        "location": "true" if location else "false",
    }
    if start_mrid is not None:
        query["start_mrid"] = str(int(start_mrid))
        if end_mrid is not None:
            query["end_mrid"] = str(int(end_mrid))
    else:
        if start_date:
            query["start_date"] = start_date
        if end_date:
            query["end_date"] = end_date
    return base + "?" + urllib.parse.urlencode(query)


def fetch_readings_page(
    api_base_url: str,
    token: str,
    device_sn: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    start_mrid: Optional[int] = None,
    end_mrid: Optional[int] = None,
    page_num: int = 1,
    per_page: int = 2000,
    timeout: int = DEFAULT_TIMEOUT,
    urlopen: Optional[UrlOpen] = None,
) -> Dict[str, Any]:
    url = build_readings_url(
        api_base_url=api_base_url or DEFAULT_API_BASE_URL,
        device_sn=device_sn,
        start_date=start_date,
        end_date=end_date,
        start_mrid=start_mrid,
        end_mrid=end_mrid,
        page_num=page_num,
        per_page=per_page,
    )
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": authorization_header(token),
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="GET",
    )
    opener = urlopen or urllib.request.urlopen
    open_kwargs = {"timeout": timeout}
    if urlopen is None:
        open_kwargs["context"] = _ssl_context()
    try:
        with opener(request, **open_kwargs) as response:
            status = getattr(response, "status", None) or response.getcode()
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = _safe_error_body(exc)
        raise ZentraAPIError(
            "ZENTRA Cloud HTTP %s for device %s: %s" % (exc.code, device_sn, detail),
            status=exc.code,
        ) from exc
    except urllib.error.URLError as exc:
        raise ZentraAPIError(
            "ZENTRA Cloud request failed for device %s: %s" % (device_sn, exc.reason)
        ) from exc

    if status and int(status) >= 400:
        raise ZentraAPIError(
            "ZENTRA Cloud HTTP %s for device %s" % (status, device_sn), status=int(status)
        )

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ZentraAPIError(
            "ZENTRA Cloud returned non-JSON for device %s" % device_sn
        ) from exc

    if isinstance(payload, dict) and payload.get("error"):
        raise ZentraAPIError(
            "ZENTRA Cloud error for device %s: %s" % (device_sn, payload.get("error"))
        )
    return payload if isinstance(payload, dict) else {"data": payload}


def iter_readings_pages(
    api_base_url: str,
    token: str,
    device_sn: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    start_mrid: Optional[int] = None,
    per_page: int = 2000,
    sleep_fn: Callable[[float], None] = time.sleep,
    urlopen: Optional[UrlOpen] = None,
    page_gap_seconds: float = DEVICE_CALL_GAP_SECONDS,
) -> Iterator[Dict[str, Any]]:
    """Yield JSON pages, sleeping between calls to honor the 1 request/device/min limit."""
    page_num = 1
    while page_num <= MAX_PAGES:
        payload = fetch_readings_page(
            api_base_url=api_base_url,
            token=token,
            device_sn=device_sn,
            start_date=start_date,
            end_date=end_date,
            start_mrid=start_mrid,
            page_num=page_num,
            per_page=per_page,
            urlopen=urlopen,
        )
        yield payload
        next_page = next_page_number(payload, page_num, per_page)
        if next_page is None:
            return
        page_num = next_page
        sleep_fn(page_gap_seconds)


def next_page_number(payload: Dict[str, Any], current_page: int, per_page: int) -> Optional[int]:
    pagination = payload.get("pagination") if isinstance(payload, dict) else None
    if isinstance(pagination, dict):
        for key in ("next_page", "next_page_num", "next"):
            value = pagination.get(key)
            if isinstance(value, int) and value > current_page:
                return value
            if isinstance(value, str) and value.isdigit() and int(value) > current_page:
                return int(value)
        if pagination.get("has_next") is True:
            return current_page + 1
        last_page = pagination.get("last_page") or pagination.get("pages")
        try:
            if last_page is not None and int(last_page) > current_page:
                return current_page + 1
        except (TypeError, ValueError):
            pass
        if "next_page" in pagination and pagination.get("next_page") in (None, False, 0, "null"):
            return None

    measurement_map, _ = extract_measurement_map(payload)
    reading_count = count_readings(measurement_map)
    if reading_count <= 0:
        return None
    if reading_count < per_page:
        return None
    return current_page + 1


def extract_measurement_map(payload: Any) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Return (measurement_name -> sensor entries, location_history)."""
    location_history = _find_location_history(payload)
    mapping = _as_measurement_map(payload)
    if mapping is not None:
        return mapping, location_history

    if isinstance(payload, dict):
        for key in ("data", "readings", "result", "device"):
            inner = payload.get(key)
            mapping = _as_measurement_map(inner)
            if mapping is not None:
                return mapping, location_history or _find_location_history(inner)
            if isinstance(inner, dict):
                nested, nested_loc = extract_measurement_map(inner)
                if nested:
                    return nested, location_history or nested_loc
        timeseries = _find_timeseries(payload)
        if timeseries:
            return _timeseries_to_map(timeseries), location_history
    return {}, location_history


def count_readings(measurement_map: Dict[str, Any]) -> int:
    total = 0
    for entries in measurement_map.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            readings = entry.get("readings") or entry.get("values") or []
            if isinstance(readings, list):
                total = max(total, len(readings))
    return total


def _as_measurement_map(payload: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict) or not payload:
        return None
    skip = {
        "pagination",
        "location_history",
        "error",
        "errors",
        "message",
        "status",
        "device",
        "metadata",
        "page_num",
        "per_page",
        "timeseries",
        "time_series",
        "sensors",
    }
    candidate_keys = [k for k in payload.keys() if k not in skip]
    if not candidate_keys:
        return None
    looks_mapped = 0
    for key in candidate_keys:
        value = payload[key]
        if isinstance(value, list) and value and isinstance(value[0], dict):
            sample = value[0]
            if "measurement" in sample or "measurement_name" in sample:
                continue
            if "metadata" in sample or "readings" in sample or "values" in sample:
                looks_mapped += 1
        elif isinstance(value, dict) and (
            "readings" in value or "metadata" in value or "values" in value
        ):
            looks_mapped += 1
    if looks_mapped == 0:
        return None
    normalized: Dict[str, Any] = {}
    for key in candidate_keys:
        value = payload[key]
        if isinstance(value, dict) and ("readings" in value or "values" in value):
            normalized[key] = [value]
        else:
            normalized[key] = value
    return normalized


def _find_location_history(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, dict):
        history = payload.get("location_history")
        if isinstance(history, list):
            return [item for item in history if isinstance(item, dict)]
        for key in ("data", "device", "readings"):
            inner = payload.get(key)
            found = _find_location_history(inner)
            if found:
                return found
    return []


def _find_timeseries(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        if "measurement" in payload[0] or "values" in payload[0]:
            return payload
    if isinstance(payload, dict):
        for key in ("timeseries", "time_series", "sensors"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        for key in ("data", "device"):
            found = _find_timeseries(payload.get(key))
            if found:
                return found
    return []


def _timeseries_to_map(timeseries: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for item in timeseries:
        name = item.get("measurement") or item.get("measurement_name") or item.get("name")
        if not name:
            continue
        metadata = dict(item.get("metadata") or {})
        if item.get("units") and "units" not in metadata:
            metadata["units"] = item.get("units")
        if item.get("unit") and "units" not in metadata:
            metadata["units"] = item.get("unit")
        readings = item.get("readings") or item.get("values") or []
        out.setdefault(str(name), []).append({"metadata": metadata, "readings": readings})
    return out


def _safe_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except Exception:
        return exc.reason or ""
    raw = raw.strip().replace("\n", " ")
    if len(raw) > 300:
        raw = raw[:300] + "..."
    # Never echo likely tokens if a proxy injected them.
    lowered = raw.lower()
    if "token" in lowered and "authorization" in lowered:
        return "redacted error body"
    return raw
