#!/usr/bin/env python3
"""Write ZENTRA configuration-CSV history as JSON lines for Splunk.

Example:
  python3 bin/import_zentra_csv.py \
    --device-sn z6-30302 \
    --csv /path/Configuration-4.csv \
    --before-timestamp-utc 1788401700 \
    --output /tmp/z6-30302.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from zentra.csv_import import ImportStats, iter_csv_events  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-sn", required=True)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--before-timestamp-utc",
        type=int,
        default=None,
        help="Skip this epoch and anything newer. Use the first live reading.",
    )
    args = parser.parse_args(argv)

    stats = ImportStats()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.csv.open("r", encoding="utf-8-sig", newline="") as handle, args.output.open(
        "w", encoding="utf-8"
    ) as out:
        for event in iter_csv_events(
            handle,
            device_sn=args.device_sn,
            before_timestamp_utc=args.before_timestamp_utc,
            stats=stats,
        ):
            out.write(json.dumps(event, separators=(",", ":"), ensure_ascii=True))
            out.write("\n")

    print(
        "%s rows=%d events=%d duplicate_rows=%d cutoff_rows=%d blank_values=%d "
        "first=%s last=%s last_timestamp_utc=%s"
        % (
            args.device_sn,
            stats.rows,
            stats.events,
            stats.duplicate_rows,
            stats.cutoff_rows,
            stats.blank_values,
            stats.first_datetime,
            stats.last_datetime,
            stats.last_timestamp_utc,
        ),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
