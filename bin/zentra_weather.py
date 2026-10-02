#!/usr/bin/env python3
"""Splunk 10 modular input: poll ZENTRA Cloud v4 field loggers."""

from __future__ import annotations

import sys
import traceback
from datetime import datetime, timedelta, timezone
from typing import List, Optional, TextIO

from zentra import SCHEME_NAME
from zentra.checkpoint import load_checkpoint, save_checkpoint
from zentra.client import (
    DEVICE_CALL_GAP_SECONDS,
    extract_measurement_map,
    iter_readings_pages,
)
from zentra.config import (
    ConfigError,
    index_storage_exists,
    parse_input_xml,
    stanza_settings,
)
from zentra.normalize import describe_measurement_map, flatten_readings, reading_skip_summary
from zentra.stream import end_stream, start_stream, write_event

SCHEME = """<scheme>
    <title>ZENTRA Cloud Weather</title>
    <description>Poll ZENTRA Cloud v4 for up to three field loggers and index 15-minute readings so daily and weekly rainfall can be totaled in Splunk.</description>
    <use_external_validation>true</use_external_validation>
    <use_single_instance>true</use_single_instance>
    <streaming_mode>xml</streaming_mode>
    <endpoint>
        <args>
            <arg name="device_sns">
                <title>Field logger serial numbers</title>
                <description>Up to three comma-separated serial numbers. Default is z6-30302.</description>
                <required_on_create>false</required_on_create>
                <required_on_edit>false</required_on_edit>
            </arg>
            <arg name="api_token">
                <title>ZENTRA Cloud API token</title>
                <description>Personal API token from ZENTRA Cloud. Stored by Splunk; alternatively set ZENTRA_API_TOKEN. Never commit tokens to source control. Required for the input to poll.</description>
                <required_on_create>true</required_on_create>
                <required_on_edit>false</required_on_edit>
            </arg>
            <arg name="api_base_url">
                <title>API base URL</title>
                <description>HTTPS origin for ZENTRA Cloud (https://zentracloud.com or https://zentracloud.eu).</description>
                <required_on_create>false</required_on_create>
                <required_on_edit>false</required_on_edit>
            </arg>
            <arg name="lookback_hours">
                <title>Initial lookback hours</title>
                <description>Hours of history to request on a date-range poll. Default 720 (30 days). Official v4 docs use start_date and end_date together.</description>
                <required_on_create>false</required_on_create>
                <required_on_edit>false</required_on_edit>
            </arg>
            <arg name="output_format">
                <title>ZENTRA output format</title>
                <description>json, df, or csv. Official v4 examples use df (pandas table of the full date range). Default df.</description>
                <required_on_create>false</required_on_create>
                <required_on_edit>false</required_on_edit>
            </arg>
            <arg name="ignore_checkpoint">
                <title>Ignore checkpoint (full date-range pull)</title>
                <description>If true, request start_date/end_date for the lookback window instead of start_mrid. Use this to pull 30 days at once. Default true during validation.</description>
                <required_on_create>false</required_on_create>
                <required_on_edit>false</required_on_edit>
            </arg>
            <arg name="per_page">
                <title>Readings per page</title>
                <description>ZENTRA page size (max 2000). Default 2000.</description>
                <required_on_create>false</required_on_create>
                <required_on_edit>false</required_on_edit>
            </arg>
        </args>
    </endpoint>
</scheme>
"""


def log(message: str, stream: Optional[TextIO] = None) -> None:
    handle = stream or sys.stderr
    handle.write("zentra_weather: %s\n" % message)
    handle.flush()


def do_scheme(out: Optional[TextIO] = None) -> None:
    handle = out or sys.stdout
    handle.write(SCHEME)
    handle.flush()


def validate_arguments(xml_text: Optional[str] = None) -> None:
    payload = xml_text if xml_text is not None else sys.stdin.read()
    cfg = parse_input_xml(payload)
    if not cfg.stanzas:
        # Splunk may send a single item wrapper.
        raise ConfigError("No input stanza found to validate")
    for stanza in cfg.stanzas:
        stanza_settings(stanza)


def run(xml_text: Optional[str] = None) -> int:
    payload = xml_text if xml_text is not None else sys.stdin.read()
    cfg = parse_input_xml(payload)
    if not cfg.stanzas:
        log("no input stanzas configured")
        return 0

    start_stream()
    event_count = 0
    try:
        for stanza in cfg.stanzas:
            event_count += _run_stanza(stanza, cfg.checkpoint_dir)
    finally:
        end_stream()
    log("indexed %d reading events" % event_count)
    return 0


def _run_stanza(stanza, checkpoint_dir: str) -> int:
    try:
        settings = stanza_settings(stanza)
    except ConfigError as exc:
        log("skipping %s: %s" % (stanza.name, exc))
        log(
            "open the input in Settings → Data inputs → ZENTRA Cloud Weather "
            "and set API token plus index=zentra_validate"
        )
        return 0

    total = 0
    devices: List[str] = settings["device_sns"]
    log(
        "running %s devices=%s index=%s sourcetype=%s token=set"
        % (
            stanza.name,
            ",".join(devices),
            settings["index"],
            settings["sourcetype"],
        )
    )
    storage = index_storage_exists(settings["index"])
    if storage is False:
        log(
            "index '%s' does not exist on this instance. "
            "Create it with: splunk add index %s  then restart. "
            "Until then Splunk drops every event and zentra_validate stays empty."
            % (settings["index"], settings["index"])
        )
    for index, device_sn in enumerate(devices):
        if index:
            # One call per device per minute.
            import time as _time

            _time.sleep(DEVICE_CALL_GAP_SECONDS)
        total += _poll_device(
            stanza_name=stanza.name,
            device_sn=device_sn,
            settings=settings,
            checkpoint_dir=checkpoint_dir,
        )
    return total


def _poll_device(stanza_name: str, device_sn: str, settings: dict, checkpoint_dir: str) -> int:
    ignore_ckpt = bool(settings.get("ignore_checkpoint"))
    ckpt = {} if ignore_ckpt else load_checkpoint(checkpoint_dir, device_sn, namespace=stanza_name)
    last_mrid = ckpt.get("last_mrid")
    start_mrid = int(last_mrid) + 1 if last_mrid is not None else None
    start_date = end_date = None
    lookback = int(settings["lookback_hours"])
    output_format = settings.get("output_format") or "df"
    # Official v4 docs: use start_date/end_date OR start_mrid/end_mrid, not both.
    # start_date overrides start_mrid. A 30-day validation pull always uses dates.
    if start_mrid is None:
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(hours=lookback)
        start_date = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        end_date = end_dt.strftime("%Y-%m-%d %H:%M:%S")
        log(
            "polling %s start_date=%s end_date=%s format=%s lookback=%sh"
            % (device_sn, start_date, end_date, output_format, lookback)
        )
    else:
        log(
            "polling %s from mrid %s format=%s"
            % (device_sn, start_mrid, output_format)
        )

    emitted = 0
    max_mrid = last_mrid
    max_ts = ckpt.get("last_timestamp_utc")
    min_mrid = int(last_mrid) if last_mrid is not None else None

    try:
        pages = iter_readings_pages(
            api_base_url=settings["api_base_url"],
            token=settings["api_token"],
            device_sn=device_sn,
            start_date=start_date,
            end_date=end_date,
            start_mrid=start_mrid,
            per_page=int(settings["per_page"]),
            output_format=output_format,
        )
        for page_num, payload in enumerate(pages, 1):
            measurement_map, location_history = extract_measurement_map(payload)
            if not measurement_map:
                keys = sorted(payload.keys()) if isinstance(payload, dict) else [type(payload).__name__]
                data_obj = payload.get("data") if isinstance(payload, dict) else None
                log(
                    "no measurements parsed for %s page %s; top-level keys=%s data_type=%s"
                    % (device_sn, page_num, keys, type(data_obj).__name__)
                )
            events = flatten_readings(
                measurement_map,
                location_history=location_history,
                device_sn_fallback=device_sn,
                min_mrid=min_mrid,
            )
            page_emitted = 0
            for event in events:
                write_event(
                    event,
                    stanza=stanza_name,
                    sourcetype=settings["sourcetype"],
                    index=settings["index"],
                    source="%s://%s" % (SCHEME_NAME, device_sn),
                    host=settings.get("host") or None,
                )
                emitted += 1
                page_emitted += 1
                mrid = event.get("mrid")
                ts = event.get("timestamp_utc")
                if mrid is not None:
                    max_mrid = mrid if max_mrid is None else max(int(max_mrid), int(mrid))
                if ts is not None:
                    max_ts = ts if max_ts is None else max(int(max_ts), int(ts))
            log(
                "device %s page %s measurements=%s sample=%s events=%s total_emitted=%s index=%s"
                % (
                    device_sn,
                    page_num,
                    len(measurement_map),
                    ",".join(sorted(str(name) for name in measurement_map)[:8]) or "-",
                    page_emitted,
                    emitted,
                    settings["index"],
                )
            )
            if measurement_map and page_emitted == 0:
                log(
                    "parsed measurement names but no sample rows for %s: %s; %s"
                    % (
                        device_sn,
                        describe_measurement_map(measurement_map),
                        reading_skip_summary(measurement_map, min_mrid=min_mrid),
                    )
                )
    except Exception as exc:
        log("poll failed for %s: %s" % (device_sn, exc))
        log(traceback.format_exc())

    if emitted or max_mrid != last_mrid:
        save_checkpoint(
            checkpoint_dir,
            device_sn,
            last_mrid=int(max_mrid) if max_mrid is not None else None,
            last_timestamp_utc=int(max_ts) if max_ts is not None else None,
            namespace=stanza_name,
        )
    log("device %s emitted %d events" % (device_sn, emitted))
    return emitted


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if args and args[0] == "--scheme":
            do_scheme()
            return 0
        if args and args[0] == "--validate-arguments":
            validate_arguments()
            return 0
        return run()
    except ConfigError as exc:
        log(str(exc))
        return 2
    except Exception as exc:
        log("fatal: %s" % exc)
        log(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
