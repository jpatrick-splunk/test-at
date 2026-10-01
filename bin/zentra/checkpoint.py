"""Per-device checkpoint files (last MRID / timestamp) for incremental polls."""

from __future__ import annotations

import json
import os
import re
import tempfile
from typing import Any, Dict, Optional

SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def checkpoint_path(checkpoint_dir: str, device_sn: str) -> str:
    safe = SAFE_NAME_RE.sub("_", device_sn).strip("._") or "device"
    return os.path.join(checkpoint_dir, "%s.json" % safe)


def load_checkpoint(checkpoint_dir: str, device_sn: str) -> Dict[str, Any]:
    if not checkpoint_dir:
        return {}
    path = checkpoint_path(checkpoint_dir, device_sn)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_checkpoint(
    checkpoint_dir: str,
    device_sn: str,
    last_mrid: Optional[int] = None,
    last_timestamp_utc: Optional[int] = None,
) -> Dict[str, Any]:
    if not checkpoint_dir:
        return {}
    os.makedirs(checkpoint_dir, exist_ok=True)
    current = load_checkpoint(checkpoint_dir, device_sn)
    if last_mrid is not None:
        previous = current.get("last_mrid")
        if previous is None or int(last_mrid) > int(previous):
            current["last_mrid"] = int(last_mrid)
    if last_timestamp_utc is not None:
        previous_ts = current.get("last_timestamp_utc")
        if previous_ts is None or int(last_timestamp_utc) > int(previous_ts):
            current["last_timestamp_utc"] = int(last_timestamp_utc)
    current["device_sn"] = device_sn
    path = checkpoint_path(checkpoint_dir, device_sn)
    fd, tmp_path = tempfile.mkstemp(prefix=".ckpt-", dir=checkpoint_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(current, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return current
