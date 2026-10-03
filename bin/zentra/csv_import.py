"""Turn a ZENTRA Cloud configuration CSV into reading events.

The export has three header rows (port, sensor, measurement) and one data
row per 15-minute timestamp. Values are already in display units such as
°F, inches, and mph. Raw-configuration files are a different format and
are not handled here.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterator, List, Optional, TextIO, Tuple
from zoneinfo import ZoneInfo

from zentra.normalize import aggregation_for, canonical_measurement_name

LOGGER_TZ = ZoneInfo("America/Chicago")
TIMESTAMP_FORMAT = "%m/%d/%Y %I:%M:%S %p"

# Longest prefix first so "in/h" is not read as "in".
_UNIT_PREFIXES: Tuple[Tuple[str, str], ...] = (
    ("W/m²", "W/m²"),
    ("W/m2", "W/m²"),
    ("in/hr", "in/h"),
    ("in/h", "in/h"),
    ("mS/cm", "mS/cm"),
    ("degree_F", "°F"),
    ("degrees", "°"),
    ("miles", "miles"),
    ("mph", "mph"),
    ("kPa", "kPa"),
    ("mV", "mV"),
    ("in", "in"),
    ("%", "%"),
)


@dataclass
class CsvColumn:
    port_number: Optional[int]
    sensor_name: str
    measurement: str
    units: Optional[str]


@dataclass
class ImportStats:
    rows: int = 0
    duplicate_rows: int = 0
    cutoff_rows: int = 0
    blank_values: int = 0
    events: int = 0
    first_datetime: Optional[str] = None
    last_datetime: Optional[str] = None
    last_timestamp_utc: Optional[int] = None
    measurements: Dict[str, int] = field(default_factory=dict)


def split_measurement_header(header: str) -> Tuple[str, Optional[str]]:
    """Split ``in Precipitation`` into ``("Precipitation", "in")``."""
    text = (header or "").strip()
    for prefix, units in _UNIT_PREFIXES:
        if text == prefix:
            return text, units
        lead = prefix + " "
        if text.startswith(lead):
            return text[len(lead) :].strip(), units
    return text, None


def short_sensor_name(name: str) -> str:
    text = (name or "").strip()
    upper = text.upper()
    if upper.startswith("ATMOS 41"):
        return "ATMOS 41 G2"
    if upper.startswith("TEROS 12"):
        return "TEROS 12"
    return text


def parse_port(value: str) -> Optional[int]:
    text = (value or "").strip()
    if text.lower().startswith("port"):
        text = text[4:]
    if not text.isdigit():
        return None
    return int(text)


def parse_csv_timestamp(value: str, zone: ZoneInfo = LOGGER_TZ) -> datetime:
    """Interpret a ZENTRA export clock time as America/Chicago local time."""
    local = datetime.strptime(value.strip(), TIMESTAMP_FORMAT)
    return local.replace(tzinfo=zone)


def format_logger_datetime(moment: datetime) -> str:
    """Match indexed events: ``2026-10-03 10:15:00-05:00``."""
    stamp = moment.strftime("%Y-%m-%d %H:%M:%S")
    offset = moment.strftime("%z")
    if len(offset) == 5:
        offset = offset[:3] + ":" + offset[3:]
    return stamp + offset


def parse_reading_value(text: str):
    raw = (text or "").strip()
    if not raw or raw.lower() in ("nan", "null", "none"):
        return None
    number = float(raw)
    if "." not in raw and "e" not in raw.lower() and number.is_integer():
        return int(number)
    return number


def read_columns(port_row: List[str], sensor_row: List[str], header_row: List[str]) -> List[Optional[CsvColumn]]:
    width = max(len(port_row), len(sensor_row), len(header_row))
    columns: List[Optional[CsvColumn]] = [None]
    for index in range(1, width):
        header = header_row[index].strip() if index < len(header_row) else ""
        if not header or header.lower() == "timestamps":
            columns.append(None)
            continue
        measurement, units = split_measurement_header(header)
        if not measurement:
            columns.append(None)
            continue
        sensor = sensor_row[index] if index < len(sensor_row) else ""
        port = port_row[index] if index < len(port_row) else ""
        columns.append(
            CsvColumn(
                port_number=parse_port(port),
                sensor_name=short_sensor_name(sensor),
                measurement=measurement,
                units=units,
            )
        )
    return columns


def iter_csv_events(
    handle: TextIO,
    device_sn: str,
    before_timestamp_utc: Optional[int] = None,
    zone: ZoneInfo = LOGGER_TZ,
    stats: Optional[ImportStats] = None,
) -> Iterator[dict]:
    """Yield one event per measurement cell.

    ``before_timestamp_utc`` excludes that instant and anything newer, so a
    file can stop where live collection already has data. A repeated
    timestamp in the file keeps the first row.
    """
    reader = csv.reader(handle)
    try:
        port_row = next(reader)
        sensor_row = next(reader)
        header_row = next(reader)
    except StopIteration as exc:
        raise ValueError("CSV is missing the port, sensor, or measurement header") from exc

    file_sn = (port_row[0] if port_row else "").strip()
    if file_sn and file_sn != device_sn:
        raise ValueError("CSV logger %s does not match %s" % (file_sn, device_sn))

    columns = read_columns(port_row, sensor_row, header_row)
    summary = stats if stats is not None else ImportStats()
    seen_timestamps = set()

    for row in reader:
        if not row or not (row[0] or "").strip():
            continue
        summary.rows += 1
        stamp = row[0].strip()
        if stamp in seen_timestamps:
            summary.duplicate_rows += 1
            continue
        seen_timestamps.add(stamp)
        moment = parse_csv_timestamp(stamp, zone)
        epoch = int(moment.timestamp())
        if before_timestamp_utc is not None and epoch >= int(before_timestamp_utc):
            summary.cutoff_rows += 1
            continue
        dt_str = format_logger_datetime(moment)
        tz_offset = int(moment.utcoffset().total_seconds()) if moment.utcoffset() else 0
        logger_day = dt_str[:10]
        if summary.first_datetime is None:
            summary.first_datetime = dt_str
        summary.last_datetime = dt_str
        summary.last_timestamp_utc = epoch
        for index, column in enumerate(columns):
            if column is None or index >= len(row):
                continue
            value = parse_reading_value(row[index])
            if value is None:
                summary.blank_values += 1
                continue
            canonical = canonical_measurement_name(column.measurement)
            event = {
                "vendor": "METER Group",
                "product": "ZENTRA Cloud",
                "vendor_product": "ZENTRA Cloud",
                "measurement": column.measurement,
                "measurement_canonical": canonical,
                "value": value,
                "aggregation": aggregation_for(canonical),
                "value_kind": "interval",
                "interval_seconds": 900,
                "error_flag": False,
                "timestamp_utc": epoch,
                "datetime": dt_str,
                "tz_offset": tz_offset,
                "logger_day": logger_day,
                "device_sn": device_sn,
                "sensor_name": column.sensor_name,
                "port_number": column.port_number,
            }
            if column.units:
                event["units"] = column.units
            summary.events += 1
            summary.measurements[column.measurement] = summary.measurements.get(column.measurement, 0) + 1
            yield event
