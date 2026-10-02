"""XML event streaming for Splunk modular inputs."""

from __future__ import annotations

import json
import sys
import xml.sax.saxutils
from typing import Any, Dict, Optional, TextIO


def start_stream(out: Optional[TextIO] = None) -> None:
    _write("<stream>", out)


def end_stream(out: Optional[TextIO] = None) -> None:
    _write("</stream>", out)


def write_event(
    data: Dict[str, Any],
    stanza: str,
    sourcetype: str,
    index: Optional[str] = None,
    source: Optional[str] = None,
    host: Optional[str] = None,
    out: Optional[TextIO] = None,
) -> None:
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=True, default=str)
    time_value = data.get("timestamp_utc")
    # Do not set unbroken="1". That flag concatenates <data> from every
    # reading into one Splunk event (the 381k dataframe dump became 1 row).
    parts = ['<event stanza="%s">' % _escape_attr(stanza)]
    if time_value is not None:
        parts.append("<time>%s</time>" % _escape_attr(str(time_value)))
    if source:
        parts.append("<source>%s</source>" % _escape_attr(source))
    if sourcetype:
        parts.append("<sourcetype>%s</sourcetype>" % _escape_attr(sourcetype))
    if index:
        parts.append("<index>%s</index>" % _escape_attr(index))
    if host:
        parts.append("<host>%s</host>" % _escape_attr(host))
    parts.append("<data>%s</data>" % xml.sax.saxutils.escape(payload))
    parts.append("</event>")
    _write("".join(parts), out)


def _escape_attr(value: str) -> str:
    return xml.sax.saxutils.escape(value, {'"': "&quot;"})


def _write(text: str, out: Optional[TextIO]) -> None:
    handle = out or sys.stdout
    handle.write(text)
    handle.write("\n")
    handle.flush()
