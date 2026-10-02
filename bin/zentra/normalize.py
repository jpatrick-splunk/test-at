"""Turn ZENTRA Cloud v4 readings into interval events and optional pivoted observations."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

MEASUREMENT_ALIASES = {
    "air temperature": "air_temperature",
    "air temp": "air_temperature",
    "temperature": "air_temperature",
    "tmin": "min_air_temperature",
    "tmax": "max_air_temperature",
    "relative humidity": "relative_humidity",
    "humidity": "relative_humidity",
    "rh": "relative_humidity",
    "atmospheric pressure": "atmospheric_pressure",
    "atm pressure": "atmospheric_pressure",
    "barometric pressure": "atmospheric_pressure",
    "pressure": "atmospheric_pressure",
    "vapor pressure": "vapor_pressure",
    "saturation vapor pressure": "saturation_vapor_pressure",
    "wind speed": "wind_speed",
    "wind direction": "wind_direction",
    "gust speed": "gust_speed",
    "wind gust": "gust_speed",
    "gust wind speed": "gust_speed",
    "precipitation": "precipitation",
    "precip": "precipitation",
    "rain": "precipitation",
    "rainfall": "precipitation",
    "precipitation mm": "precipitation",
    "precipitation in": "precipitation",
    "max precipitation rate": "max_precipitation_rate",
    "precipitation rate": "max_precipitation_rate",
    "solar radiation": "solar_radiation",
    "lightning activity": "lightning_activity",
    "lightning strikes": "lightning_activity",
    "strikes": "lightning_activity",
    "lightning distance": "lightning_distance",
    "strike distance": "lightning_distance",
    "dew point": "dew_point",
    "battery percent": "battery_percent",
    "battery": "battery_percent",
    "battery percentage": "battery_percent",
    "signal strength": "signal_percent",
    "signal": "signal_percent",
    "x-axis level": "x_axis_level",
    "y-axis level": "y_axis_level",
    "hourly reference et": "hourly_reference_et",
    "daily reference et": "daily_reference_et",
    "water content": "water_content",
    "rh sensor temp": "rh_sensor_temperature",
    "rh sensor temperature": "rh_sensor_temperature",
}

SNAKE_RE = re.compile(r"[^a-z0-9]+")
UNIT_SUFFIX_RE = re.compile(
    r"\s+\(?((mm|cm|in|inch|inches|mm/h|in/h|mm/hr|in/hr))\)?\s*$"
)

# ATMOS 41 Precipitation is rainfall during the measurement interval (typically
# 15 minutes), not a running daily total. Sum those interval samples to get
# daily/weekly accumulation. Do not fold daily/cumulative names into this set.
SUM_CANONICAL = {
    "precipitation",
    "lightning_activity",
    "lightning_strikes",
    "precip_drops",
    "precip_tips",
    "precipitation_drop_counter",
    "precipitation_tip_counter",
}
MAX_CANONICAL = {
    "gust_speed",
    "max_precipitation_rate",
    "max_air_temperature",
}
MIN_CANONICAL = {
    "min_air_temperature",
}


def canonical_measurement_name(name: str) -> str:
    cleaned = _clean_measurement_name(name)
    if cleaned in MEASUREMENT_ALIASES:
        return MEASUREMENT_ALIASES[cleaned]
    return SNAKE_RE.sub("_", cleaned).strip("_") or "unknown_measurement"


def aggregation_for(canonical: str) -> str:
    """How to roll 15-minute samples up to a day or week."""
    if canonical in SUM_CANONICAL:
        return "sum"
    if canonical in MAX_CANONICAL:
        return "max"
    if canonical in MIN_CANONICAL:
        return "min"
    return "avg"


def _clean_measurement_name(name: str) -> str:
    cleaned = (name or "").strip().lower()
    cleaned = re.sub(r"\([^)]*\)", " ", cleaned)
    cleaned = UNIT_SUFFIX_RE.sub("", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def location_at(
    location_history: Iterable[Dict[str, Any]], timestamp_utc: Optional[int]
) -> Dict[str, Any]:
    """Pick the most recent location at or before ``timestamp_utc``."""
    parsed: List[Tuple[int, Dict[str, float]]] = []
    for entry in location_history or []:
        coords = _coords_from_entry(entry)
        if coords is None:
            continue
        ts = _entry_timestamp(entry)
        parsed.append((ts if ts is not None else -1, coords))
    if not parsed:
        return {}
    parsed.sort(key=lambda item: item[0])
    chosen = parsed[0][1]
    if timestamp_utc is None:
        return parsed[-1][1]
    for ts, coords in parsed:
        if ts <= int(timestamp_utc):
            chosen = coords
        else:
            break
    return chosen


READING_LIST_KEYS = (
    "readings",
    "values",
    "data",
    "series",
    "points",
    "samples",
    "records",
)


def entry_readings(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return sample rows from a ZENTRA measurement entry.

    ZENTRA Cloud json may use a list of reading objects, a ``data`` list, or a
    columnar dict of parallel arrays. Metadata-only entries yield no rows.
    """
    if not isinstance(entry, dict):
        return []
    for key in READING_LIST_KEYS:
        if key not in entry:
            continue
        rows = _coerce_reading_list(entry.get(key))
        if rows:
            return rows
    if _reading_timestamp(entry) is not None and "value" in entry:
        return [entry]
    return []


def describe_measurement_map(measurement_map: Dict[str, Any]) -> str:
    """Short diagnostic for why flatten might emit zero events."""
    if not measurement_map:
        return "empty-map"
    name, entries = next(iter(measurement_map.items()))
    if not isinstance(entries, list):
        entries = [entries]
    entry = entries[0] if entries else None
    if not isinstance(entry, dict):
        return "measurement=%s entry_type=%s" % (name, type(entry).__name__)
    raw = None
    raw_key = None
    for key in READING_LIST_KEYS:
        if key in entry:
            raw = entry.get(key)
            raw_key = key
            break
    rows = entry_readings(entry)
    first = rows[0] if rows else None
    first_keys = sorted(first.keys()) if isinstance(first, dict) else None
    ts = _reading_timestamp(first) if isinstance(first, dict) else None
    return (
        "measurement=%s entry_keys=%s sample_key=%s sample_type=%s "
        "sample_len=%s parsed_rows=%s first_row_keys=%s timestamp=%s"
        % (
            name,
            sorted(entry.keys()),
            raw_key,
            type(raw).__name__,
            len(raw) if isinstance(raw, (list, dict)) else None,
            len(rows),
            first_keys,
            ts,
        )
    )


def reading_skip_summary(
    measurement_map: Dict[str, Any], min_mrid: Optional[int] = None
) -> str:
    """Count rows that flatten would keep vs skip (timestamp / checkpoint)."""
    rows = with_ts = skipped_mrid = no_ts = 0
    for entries in (measurement_map or {}).values():
        if not isinstance(entries, list):
            entries = [entries]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for reading in entry_readings(entry):
                if not isinstance(reading, dict):
                    continue
                rows += 1
                ts = _reading_timestamp(reading)
                if ts is None:
                    no_ts += 1
                    continue
                with_ts += 1
                mrid = _as_int(reading.get("mrid") or reading.get("reading_id"))
                if min_mrid is not None and mrid is not None and mrid <= int(min_mrid):
                    skipped_mrid += 1
    return "rows=%s with_timestamp=%s skipped_mrid=%s no_timestamp=%s min_mrid=%s" % (
        rows,
        with_ts,
        skipped_mrid,
        no_ts,
        min_mrid,
    )


def _coerce_reading_list(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return []
    if isinstance(raw, str):
        text = raw.strip()
        if text[:1] in "{[":
            try:
                raw = json.loads(text)
            except ValueError:
                return []
        else:
            return []
    if isinstance(raw, dict):
        if "columns" in raw and "data" in raw:
            return []
        for key in READING_LIST_KEYS:
            if key in raw:
                inner = _coerce_reading_list(raw.get(key))
                if inner:
                    return inner
        columnar = _columnar_to_readings(raw)
        if columnar:
            return columnar
        if _reading_timestamp(raw) is not None and "value" in raw:
            return [raw]
        return []
    if not isinstance(raw, list) or not raw:
        return []
    rows: List[Dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            rows.extend(_expand_reading_dict(item))
        elif isinstance(item, (list, tuple)) and item:
            rows.append(_sequence_to_reading(item))
    return rows


def _expand_reading_dict(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    if _looks_columnar(item):
        return _columnar_to_readings(item)
    nested: List[Dict[str, Any]] = []
    for key in READING_LIST_KEYS:
        if key in item:
            nested = _coerce_reading_list(item.get(key))
            if nested:
                return nested
    return [item]


def _looks_columnar(obj: Dict[str, Any]) -> bool:
    for key in ("timestamp_utc", "timestamp", "timestamps", "time", "datetime", "value", "values"):
        if isinstance(obj.get(key), list) and obj.get(key):
            return True
    return False


def _columnar_to_readings(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    lists = {key: value for key, value in obj.items() if isinstance(value, list)}
    time_key = None
    for key in ("timestamp_utc", "timestamp", "timestamps", "time", "datetime"):
        if key in lists:
            time_key = key
            break
    if time_key is None or not lists[time_key]:
        return []
    count = len(lists[time_key])
    rows: List[Dict[str, Any]] = []
    for index in range(count):
        reading: Dict[str, Any] = {}
        for key, values in lists.items():
            if index < len(values):
                reading[key] = values[index]
        if "timestamp_utc" not in reading:
            if reading.get("timestamp") is not None:
                reading["timestamp_utc"] = reading.get("timestamp")
            elif reading.get("timestamps") is not None:
                reading["timestamp_utc"] = reading.get("timestamps")
        if "value" not in reading and "values" in reading:
            reading["value"] = reading.get("values")
        if "mrid" not in reading and reading.get("reading_id") is not None:
            reading["mrid"] = reading.get("reading_id")
        rows.append(reading)
    return rows


def _sequence_to_reading(row: Any) -> Dict[str, Any]:
    reading: Dict[str, Any] = {}
    if len(row) >= 1:
        reading["timestamp_utc"] = row[0]
    if len(row) >= 2:
        reading["value"] = row[1]
    if len(row) >= 3:
        reading["mrid"] = row[2]
    return reading


def flatten_readings(
    measurement_map: Dict[str, Any],
    location_history: Optional[List[Dict[str, Any]]] = None,
    device_sn_fallback: Optional[str] = None,
    min_mrid: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Emit one event per ZENTRA measurement reading (15-minute sample).

    Keeps the original measurement name, value, and units so numbers can be
    compared to ZENTRA Cloud before any dashboard aggregation.
    """
    events: List[Dict[str, Any]] = []
    for measurement_name, entries in (measurement_map or {}).items():
        original = str(measurement_name)
        canonical = canonical_measurement_name(original)
        if not isinstance(entries, list):
            entries = [entries]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
            units = (
                metadata.get("units")
                or metadata.get("unit")
                or entry.get("units")
                or entry.get("unit")
            )
            readings = entry_readings(entry)
            for reading in readings:
                if not isinstance(reading, dict):
                    continue
                ts = _reading_timestamp(reading)
                if ts is None:
                    continue
                mrid = _as_int(reading.get("mrid") or reading.get("reading_id"))
                if min_mrid is not None and mrid is not None and mrid <= int(min_mrid):
                    continue
                event = {
                    "vendor": "METER Group",
                    "product": "ZENTRA Cloud",
                    "vendor_product": "ZENTRA Cloud",
                    "measurement": original,
                    "measurement_canonical": canonical,
                    "value": reading.get("value"),
                    "aggregation": aggregation_for(canonical),
                    "value_kind": "interval",
                    "interval_seconds": 900,
                    "error_flag": bool(reading.get("error_flag")),
                }
                _stamp_event(event, reading)
                _merge_identity(event, metadata, device_sn_fallback)
                if units:
                    event["units"] = str(units).strip()
                if mrid is not None:
                    event["mrid"] = mrid
                if reading.get("precision") is not None:
                    event["precision"] = reading.get("precision")
                if reading.get("error_description"):
                    event["error_description"] = reading.get("error_description")
                meta_errors = metadata.get("errors") or []
                if isinstance(meta_errors, list) and meta_errors:
                    event["metadata_errors"] = ",".join(str(x) for x in meta_errors)
                event.update(location_at(location_history or [], ts))
                events.append(event)
    events.sort(
        key=lambda item: (int(item.get("timestamp_utc") or 0), str(item.get("measurement") or ""))
    )
    return events


def normalize_readings(
    measurement_map: Dict[str, Any],
    location_history: Optional[List[Dict[str, Any]]] = None,
    device_sn_fallback: Optional[str] = None,
    min_mrid: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Collapse measurement-keyed ZENTRA JSON into observation events.

    Events with ``mrid <= min_mrid`` are skipped so checkpointed polls do not
    re-index the last already-seen record.
    """
    by_ts: Dict[int, Dict[str, Any]] = {}

    for measurement_name, entries in (measurement_map or {}).items():
        field_name = canonical_measurement_name(str(measurement_name))
        if not isinstance(entries, list):
            entries = [entries]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
            units = (
                metadata.get("units")
                or metadata.get("unit")
                or entry.get("units")
                or entry.get("unit")
            )
            readings = entry_readings(entry)
            for reading in readings:
                if not isinstance(reading, dict):
                    continue
                ts = _reading_timestamp(reading)
                if ts is None:
                    continue
                mrid = _as_int(reading.get("mrid") or reading.get("reading_id"))
                if min_mrid is not None and mrid is not None and mrid <= int(min_mrid):
                    continue
                group_key = mrid if mrid is not None else ts
                event = by_ts.setdefault(group_key, _new_event(ts, metadata, device_sn_fallback))
                _merge_identity(event, metadata, device_sn_fallback)
                _stamp_event(event, reading)
                event["mrid"] = _max_optional_int(event.get("mrid"), mrid)
                event[field_name] = reading.get("value")
                if units:
                    event["%s_unit" % field_name] = str(units).strip()
                if reading.get("precision") is not None:
                    event["%s_precision" % field_name] = reading.get("precision")
                if reading.get("error_flag"):
                    event["error_flag"] = True
                    desc = reading.get("error_description")
                    if desc:
                        existing = event.get("error_description") or []
                        if isinstance(existing, list):
                            if desc not in existing:
                                existing.append(desc)
                            event["error_description"] = existing
                        else:
                            event["error_description"] = [existing, desc]
                meta_errors = metadata.get("errors") or []
                if isinstance(meta_errors, list) and meta_errors:
                    bucket = event.setdefault("metadata_errors", [])
                    for err in meta_errors:
                        if err not in bucket:
                            bucket.append(err)

    events = []
    for event in sorted(
        by_ts.values(),
        key=lambda item: (int(item.get("timestamp_utc") or 0), str(item.get("mrid") or "")),
    ):
        event.update(location_at(location_history or [], ts))
        if isinstance(event.get("error_description"), list):
            event["error_description"] = "; ".join(str(x) for x in event["error_description"])
        if isinstance(event.get("metadata_errors"), list):
            event["metadata_errors"] = ",".join(str(x) for x in event["metadata_errors"])
        event.setdefault("error_flag", False)
        event.setdefault("vendor", "METER Group")
        event.setdefault("product", "ZENTRA Cloud")
        event.setdefault("vendor_product", "ZENTRA Cloud")
        events.append(event)
    return events


def _new_event(timestamp_utc: int, metadata: Dict[str, Any], device_sn_fallback: Optional[str]) -> Dict[str, Any]:
    event = {
        "timestamp_utc": int(timestamp_utc),
        "error_flag": False,
    }
    _merge_identity(event, metadata, device_sn_fallback)
    return event


def _merge_identity(
    event: Dict[str, Any], metadata: Dict[str, Any], device_sn_fallback: Optional[str]
) -> None:
    device_sn = metadata.get("device_sn") or metadata.get("device_id") or device_sn_fallback
    if device_sn and not event.get("device_sn"):
        event["device_sn"] = device_sn
    for src, dest in (
        ("device_name", "device_name"),
        ("sensor_sn", "sensor_sn"),
        ("sensor_name", "sensor_name"),
        ("port_number", "port_number"),
        ("port_num", "port_number"),
        ("sub_sensor_index", "sub_sensor_index"),
        ("latitude", "latitude"),
        ("longitude", "longitude"),
        ("altitude", "altitude"),
    ):
        value = metadata.get(src)
        if value is not None and dest not in event:
            event[dest] = value


def _stamp_event(event: Dict[str, Any], reading: Dict[str, Any]) -> Optional[int]:
    """Set datetime, logger_day, tz_offset, and timestamp_utc from the reading.

    ZENTRA's ``datetime`` field is the measurement time (logger-local, usually
    with an offset like ``2026-10-01 12:00:00-05:00``). Prefer that over
    ``timestamp_utc`` when both are present.
    """
    epoch, dt_str, tz_offset = _measurement_time(reading)
    has_datetime = reading.get("datetime") not in (None, "") or reading.get("date_time") not in (
        None,
        "",
    )
    if epoch is not None and (has_datetime or event.get("timestamp_utc") is None):
        event["timestamp_utc"] = int(epoch)
    if dt_str and (has_datetime or not event.get("datetime")):
        event["datetime"] = dt_str
    if tz_offset is not None and (has_datetime or event.get("tz_offset") is None):
        event["tz_offset"] = int(tz_offset)
    day = _logger_day(event.get("datetime"), event.get("timestamp_utc"), event.get("tz_offset"))
    if day:
        event["logger_day"] = day
    return event.get("timestamp_utc")


def _measurement_time(
    reading: Dict[str, Any],
) -> Tuple[Optional[int], Optional[str], Optional[int]]:
    """Return ``(utc_epoch, datetime_string, tz_offset_seconds)``."""
    if not isinstance(reading, dict):
        return None, None, None
    tz_offset = _parse_tz_offset(reading.get("tz_offset"))
    raw_dt = reading.get("datetime")
    if raw_dt is None:
        raw_dt = reading.get("date_time")
    parsed = _parse_datetime_obj(raw_dt) if raw_dt is not None else None
    if parsed is not None:
        if parsed.tzinfo is None:
            if tz_offset is not None:
                parsed = parsed.replace(tzinfo=timezone(timedelta(seconds=int(tz_offset))))
            else:
                parsed = parsed.replace(tzinfo=timezone.utc)
        elif tz_offset is None:
            utc_off = parsed.utcoffset()
            if utc_off is not None:
                tz_offset = int(utc_off.total_seconds())
        epoch = int(parsed.timestamp())
        dt_str = str(raw_dt).strip() if raw_dt is not None else parsed.isoformat(sep=" ")
        return epoch, dt_str, tz_offset

    epoch = _epoch_from_numeric_fields(reading)
    if epoch is None:
        return None, None, tz_offset
    if tz_offset is not None:
        dt_str = datetime.fromtimestamp(
            epoch, tz=timezone(timedelta(seconds=int(tz_offset)))
        ).isoformat(sep=" ")
    else:
        dt_str = datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(sep=" ")
    return epoch, dt_str, tz_offset


def _logger_day(
    datetime_str: Optional[str], epoch: Optional[int], tz_offset: Optional[int]
) -> Optional[str]:
    if datetime_str:
        match = re.match(r"(\d{4}-\d{2}-\d{2})", str(datetime_str).strip())
        if match:
            return match.group(1)
    if epoch is None:
        return None
    tz = timezone(timedelta(seconds=int(tz_offset or 0)))
    return datetime.fromtimestamp(int(epoch), tz=tz).strftime("%Y-%m-%d")


def _parse_tz_offset(value: Any) -> Optional[int]:
    """Return offset from UTC in seconds."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        if -14 <= number <= 14:
            return number * 3600
        return number
    text = str(value).strip()
    if not text or text.lower() in ("nan", "none", "null"):
        return None
    if text.upper() == "Z":
        return 0
    match = re.match(r"^([+-])?(\d{1,2}):?(\d{2})$", text)
    if match:
        sign = -1 if match.group(1) == "-" else 1
        return sign * (int(match.group(2)) * 3600 + int(match.group(3)) * 60)
    try:
        number = int(float(text))
    except (TypeError, ValueError):
        return None
    if -14 <= number <= 14:
        return number * 3600
    return number


def _epoch_from_numeric_fields(reading: Dict[str, Any]) -> Optional[int]:
    for key in (
        "timestamp_utc",
        "timestamp",
        "timestamps",
        "time",
        "date",
        "measured_at",
        "ts",
        "epoch",
    ):
        value = reading.get(key)
        if value is None:
            continue
        ts = _as_int(value)
        if ts is not None:
            if ts > 1000000000000:
                ts = ts // 1000
            return ts
        if isinstance(value, str) and value.strip():
            parsed = _parse_datetime_obj(value.strip())
            if parsed is not None:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return int(parsed.timestamp())
    return None


def _reading_timestamp(reading: Dict[str, Any]) -> Optional[int]:
    epoch, _dt_str, _tz_offset = _measurement_time(reading)
    return epoch


def _parse_datetime_obj(value: Any) -> Optional[datetime]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text or text.lower() in ("nan", "nat", "none", "null"):
        return None
    text = text.replace("Z", "+00:00")
    text = re.sub(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})", r"\1T\2", text)
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt, size in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(text[:size], fmt)
        except ValueError:
            continue
    return None


def _parse_datetime(value: str) -> Optional[int]:
    parsed = _parse_datetime_obj(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _entry_timestamp(entry: Dict[str, Any]) -> Optional[int]:
    ts = _as_int(entry.get("timestamp_utc") or entry.get("timestamp"))
    if ts is not None:
        return ts
    dt = entry.get("datetime")
    if isinstance(dt, str):
        return _parse_datetime(dt)
    return None


def _coords_from_entry(entry: Dict[str, Any]) -> Optional[Dict[str, float]]:
    lat = _as_float(entry.get("Latitude", entry.get("latitude", entry.get("lat"))))
    lon = _as_float(entry.get("Longitude", entry.get("longitude", entry.get("lon"))))
    if lat is None or lon is None:
        return None
    coords = {"latitude": lat, "longitude": lon}
    alt = _as_float(entry.get("Altitude", entry.get("altitude", entry.get("elevation"))))
    if alt is not None:
        coords["altitude"] = alt
    return coords


def _as_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, (bool, list, dict, tuple)):
        return None
    try:
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            return int(float(text)) if any(ch in text for ch in ".eE") else int(text)
        if isinstance(value, float):
            return int(value)
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _as_float(value: Any) -> Optional[float]:
    if value is None or value is False:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _max_optional_int(left: Any, right: Any) -> Optional[int]:
    ints = [v for v in (_as_int(left), _as_int(right)) if v is not None]
    return max(ints) if ints else None
