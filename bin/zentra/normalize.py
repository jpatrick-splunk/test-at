"""Pivot ZENTRA Cloud v4 readings into one normalized weather event per timestamp."""

from __future__ import annotations

import re
from datetime import datetime, timezone
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
    "rain": "precipitation",
    "rainfall": "precipitation",
    "max precipitation rate": "max_precipitation_rate",
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


def canonical_measurement_name(name: str) -> str:
    cleaned = (name or "").strip().lower()
    if cleaned in MEASUREMENT_ALIASES:
        return MEASUREMENT_ALIASES[cleaned]
    return SNAKE_RE.sub("_", cleaned).strip("_") or "unknown_measurement"


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
            readings = entry.get("readings") or entry.get("values") or []
            if not isinstance(readings, list):
                continue
            for reading in readings:
                if not isinstance(reading, dict):
                    continue
                ts = _reading_timestamp(reading)
                if ts is None:
                    continue
                mrid = _as_int(reading.get("mrid") or reading.get("reading_id"))
                if min_mrid is not None and mrid is not None and mrid <= int(min_mrid):
                    continue
                event = by_ts.setdefault(ts, _new_event(ts, metadata, device_sn_fallback))
                _merge_identity(event, metadata, device_sn_fallback)
                event["mrid"] = _max_optional_int(event.get("mrid"), mrid)
                if reading.get("datetime"):
                    event["datetime"] = reading.get("datetime")
                if reading.get("tz_offset") is not None:
                    event["tz_offset"] = _as_int(reading.get("tz_offset"))
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
    for ts in sorted(by_ts):
        event = by_ts[ts]
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
    ):
        value = metadata.get(src)
        if value is not None and dest not in event:
            event[dest] = value


def _reading_timestamp(reading: Dict[str, Any]) -> Optional[int]:
    for key in ("timestamp_utc", "timestamp", "time"):
        ts = _as_int(reading.get(key))
        if ts is not None:
            return ts
    dt = reading.get("datetime")
    if isinstance(dt, str) and dt.strip():
        parsed = _parse_datetime(dt.strip())
        if parsed is not None:
            return parsed
    return None


def _parse_datetime(value: str) -> Optional[int]:
    text = value.replace("Z", "+00:00")
    # ZENTRA often returns "2026-07-11 12:00:00-06:00"
    text = re.sub(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})", r"\1T\2", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                parsed = datetime.strptime(text[:19], fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                parsed = None
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
    if value is None or value is False:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
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
