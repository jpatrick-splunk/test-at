"""HTTPS client for ZENTRA Cloud v4 ``/api/v4/get_readings/``."""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from zentra.normalize import entry_readings

READINGS_PATH = "/api/v4/get_readings/"
USER_AGENT = "zentra_weather/1.1 (Splunk modular input)"
DEFAULT_TIMEOUT = 120
DEVICE_CALL_GAP_SECONDS = 61
MAX_PAGES = 80

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
    sort_by: str = "ascending",
    location: bool = True,
    output_format: str = "df",
) -> str:
    base = api_base_url.rstrip("/") + READINGS_PATH
    fmt = (output_format or "df").strip().lower() or "df"
    if fmt not in ("df", "json"):
        fmt = "df"
    query: Dict[str, Any] = {
        "device_sn": device_sn,
        "output_format": fmt,
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
    output_format: str = "df",
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
        output_format=output_format,
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
    output_format: str = "df",
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
            output_format=output_format,
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

    # Count dataframe rows, not exploded cells. A long-format table has one
    # reading per row (measurement, value, units, …); a wide table has one
    # timestamp per row. Either way the API's per_page applies to those rows.
    row_count = _payload_split_row_count(payload)
    if row_count is not None:
        return current_page + 1 if row_count == per_page else None

    measurement_map, _ = extract_measurement_map(payload)
    reading_count = count_readings(measurement_map)
    if reading_count == per_page:
        return current_page + 1
    return None


def extract_measurement_map(payload: Any) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Return (measurement_name -> sensor entries, location_history)."""
    payload = _coerce_json_container(payload)
    location_history = _find_location_history(payload)

    candidates: List[Any] = [payload]
    if isinstance(payload, dict):
        for key in ("data", "readings", "result", "device"):
            if key in payload:
                candidates.append(_coerce_json_container(payload.get(key)))

    for candidate in candidates:
        mapping = _as_measurement_map(candidate)
        if mapping:
            return mapping, location_history or _find_location_history(candidate)
        mapping = _pandas_split_to_map(candidate)
        if mapping:
            return mapping, location_history
        values = candidate if isinstance(candidate, list) else (
            candidate.get("values") if isinstance(candidate, dict) else None
        )
        mapping = _values_list_to_map(values)
        if mapping:
            return mapping, location_history
        timeseries = _find_timeseries(candidate)
        if timeseries:
            return _timeseries_to_map(timeseries), location_history or _find_location_history(candidate)

    return {}, location_history


def _coerce_json_container(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return value
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in "{[":
            try:
                return json.loads(text)
            except ValueError:
                return value
    return value


# Columns that describe a reading in a long-format dataframe. These are not
# ZENTRA sensor names; treating them as measurements produced index counts
# like latitude=73922 instead of Precipitation.
_SPLIT_META_COLUMNS = {
    "timestamp_utc",
    "timestamp",
    "time",
    "datetime",
    "date",
    "mrid",
    "reading_id",
    "tz_offset",
    "error_flag",
    "error_description",
    "precision",
    "index",
    "latitude",
    "longitude",
    "altitude",
    "lat",
    "lon",
    "measurement",
    "measurement_name",
    "port_num",
    "port_number",
    "sensor_meta_errors",
    "sensor_name",
    "sensor_sn",
    "sub_sensor_index",
    "units",
    "unit",
    "value",
    "values",
    "device_sn",
    "device_name",
    "device_id",
}


def _as_split_table(obj: Any) -> Optional[Tuple[List[Any], List[Any]]]:
    obj = _coerce_json_container(obj)
    if not isinstance(obj, dict):
        return None
    columns = obj.get("columns")
    rows = obj.get("data")
    if not isinstance(columns, list) or not isinstance(rows, list) or not columns:
        return None
    if obj.get("readings") or obj.get("metadata"):
        return None
    return columns, rows


def _payload_split_row_count(payload: Any) -> Optional[int]:
    payload = _coerce_json_container(payload)
    candidates: List[Any] = [payload]
    if isinstance(payload, dict) and "data" in payload:
        candidates.append(_coerce_json_container(payload.get("data")))
    for obj in candidates:
        table = _as_split_table(obj)
        if table is not None:
            return len(table[1])
    return None


def _split_rows_to_records(columns: List[Any], rows: List[Any]) -> List[Dict[str, Any]]:
    names: List[str] = []
    for col in columns:
        if isinstance(col, (list, tuple)):
            col = " ".join(str(part) for part in col if part not in (None, ""))
        names.append(str(col))
    records: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        records.append({names[index]: row[index] for index in range(min(len(names), len(row)))})
    return records


def _record_get(item: Dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in item and item[name] is not None:
            return item[name]
    lowered = {str(key).strip().lower(): value for key, value in item.items()}
    for name in names:
        value = lowered.get(str(name).strip().lower())
        if value is not None:
            return value
    return None


def _pandas_split_to_map(obj: Any) -> Optional[Dict[str, Any]]:
    """Convert a pandas ``orient=split`` table (ZENTRA output_format=df/json)."""
    table = _as_split_table(obj)
    if table is None:
        return None
    columns, rows = table
    lower = [str(col).strip().lower() for col in columns]
    if ("measurement" in lower or "measurement_name" in lower) and (
        "value" in lower or "values" in lower
    ):
        return _values_list_to_map(_split_rows_to_records(columns, rows))

    def _idx(*names: str) -> Optional[int]:
        for name in names:
            if name in lower:
                return lower.index(name)
        return None

    ts_idx = _idx("timestamp_utc", "timestamp", "time")
    dt_idx = _idx("datetime", "date")
    mrid_idx = _idx("mrid", "reading_id")
    skip = {i for i, name in enumerate(lower) if name in _SPLIT_META_COLUMNS}
    mapping: Dict[str, Any] = {}
    for col_idx, col in enumerate(columns):
        if col_idx in skip:
            continue
        if isinstance(col, (list, tuple)):
            col = " ".join(str(part) for part in col if part not in (None, ""))
        readings = []
        for row in rows:
            if not isinstance(row, list) or col_idx >= len(row):
                continue
            cell = row[col_idx]
            if isinstance(cell, dict) and ("value" in cell or "timestamp_utc" in cell):
                readings.append(cell)
                continue
            reading: Dict[str, Any] = {"value": cell}
            if ts_idx is not None and ts_idx < len(row):
                reading["timestamp_utc"] = row[ts_idx]
            if dt_idx is not None and dt_idx < len(row):
                reading["datetime"] = row[dt_idx]
            if mrid_idx is not None and mrid_idx < len(row):
                reading["mrid"] = row[mrid_idx]
            readings.append(reading)
        if readings:
            mapping[str(col)] = [{"metadata": {}, "readings": readings}]
    return mapping or None


def _normalize_meta_errors(raw: Any) -> List[Any]:
    if raw is None or raw is False:
        return []
    if isinstance(raw, list):
        return [item for item in raw if item not in (None, "", [])]
    if isinstance(raw, str):
        text = raw.strip()
        if not text or text in ("[]", "{}", "null", "None", "nan"):
            return []
        return [text]
    return [raw]


def _values_list_to_map(values: Any) -> Optional[Dict[str, Any]]:
    """Convert long rows: ``{measurement, value, timestamp, ...}``."""
    if not isinstance(values, list) or not values or not isinstance(values[0], dict):
        return None
    sample = values[0]
    if _record_get(sample, "measurement", "measurement_name") is None:
        return None
    mapping: Dict[str, Any] = {}
    for item in values:
        if not isinstance(item, dict):
            continue
        name = _record_get(item, "measurement", "measurement_name")
        if not name:
            continue
        metadata = {
            "device_sn": _record_get(item, "device_sn", "device_id"),
            "device_name": _record_get(item, "device_name"),
            "sensor_name": _record_get(item, "sensor_name"),
            "sensor_sn": _record_get(item, "sensor_sn"),
            "port_number": _record_get(item, "port_num", "port_number"),
            "units": _record_get(item, "units", "unit"),
            "sub_sensor_index": _record_get(item, "sub_sensor_index"),
            "latitude": _record_get(item, "latitude", "Latitude", "lat"),
            "longitude": _record_get(item, "longitude", "Longitude", "lon"),
            "errors": _normalize_meta_errors(
                _record_get(item, "sensor_meta_errors", "errors", "metadata_errors")
            ),
        }
        error_code = _record_get(item, "error_code")
        error_flag = _record_get(item, "error_flag")
        reading = {
            "timestamp_utc": _record_get(item, "timestamp_utc", "timestamp"),
            "datetime": _record_get(item, "datetime"),
            "value": _record_get(item, "value", "values"),
            "mrid": _record_get(item, "mrid", "reading_id"),
            "error_flag": bool(error_code) if error_code not in (None, 0, "0") else bool(error_flag),
            "error_description": _record_get(item, "error_description"),
            "tz_offset": _record_get(item, "tz_offset"),
            "precision": _record_get(item, "precision"),
        }
        mapping.setdefault(str(name), []).append({"metadata": metadata, "readings": [reading]})
    return mapping or None


def count_readings(measurement_map: Dict[str, Any]) -> int:
    total = 0
    for entries in measurement_map.values():
        if not isinstance(entries, list):
            entries = [entries]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            total = max(total, len(entry_readings(entry)))
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
            if "metadata" in sample or "readings" in sample or "values" in sample or "data" in sample:
                looks_mapped += 1
        elif isinstance(value, dict) and (
            "readings" in value or "metadata" in value or "values" in value or "data" in value
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
