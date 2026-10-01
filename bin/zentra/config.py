"""Parse Splunk modular-input configuration and logger serial lists."""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional
from urllib.parse import urlparse

from zentra import DEFAULT_API_BASE_URL, DEFAULT_DEVICE_SN, MAX_DEVICES

DEVICE_SN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TOKEN_ENV_VARS = ("ZENTRA_API_TOKEN", "ZENTRACLOUD_TOKEN")


class ConfigError(ValueError):
    """Invalid modular-input configuration."""


@dataclass
class InputStanza:
    name: str
    params: Dict[str, str] = field(default_factory=dict)

    @property
    def index(self) -> str:
        return self.params.get("index", "weather")

    @property
    def sourcetype(self) -> str:
        return self.params.get("sourcetype", "zentra:weather")

    @property
    def host(self) -> str:
        return self.params.get("host", "")

    @property
    def interval(self) -> int:
        try:
            return int(self.params.get("interval", "300"))
        except ValueError as exc:
            raise ConfigError("interval must be an integer number of seconds") from exc


@dataclass
class InputConfig:
    server_host: str = ""
    server_uri: str = ""
    session_key: str = ""
    checkpoint_dir: str = ""
    stanzas: List[InputStanza] = field(default_factory=list)


def parse_device_sns(
    value: Optional[str],
    default: str = DEFAULT_DEVICE_SN,
    max_devices: int = MAX_DEVICES,
) -> List[str]:
    """Split a comma/space-separated serial list, cap at ``max_devices``."""
    raw = (value or "").strip()
    parts = [p.strip() for p in re.split(r"[,;\s]+", raw) if p.strip()] if raw else []
    if not parts:
        parts = [default]

    seen = set()
    devices: List[str] = []
    for sn in parts:
        if not DEVICE_SN_RE.match(sn):
            raise ConfigError(
                "Invalid device serial '%s'. Use values such as %s." % (sn, default)
            )
        key = sn.lower()
        if key in seen:
            continue
        seen.add(key)
        devices.append(sn)

    if len(devices) > max_devices:
        raise ConfigError(
            "At most %d field loggers are supported per input, got %d: %s"
            % (max_devices, len(devices), ", ".join(devices))
        )
    return devices


def resolve_api_token(explicit: Optional[str] = None, environ: Optional[dict] = None) -> str:
    """Resolve the API token from the input param or process environment.

    Tokens are never hardcoded. Splunk stores the modular-input param in
    local/inputs.conf (encrypted when set through Splunk Web / REST).
    Operators may also inject ZENTRA_API_TOKEN or ZENTRACLOUD_TOKEN.
    """
    env = environ if environ is not None else os.environ
    token = (explicit or "").strip()
    if token:
        return _strip_token_prefix(token)
    for name in TOKEN_ENV_VARS:
        candidate = (env.get(name) or "").strip()
        if candidate:
            return _strip_token_prefix(candidate)
    raise ConfigError(
        "ZENTRA Cloud API token is not configured. Set the api_token input "
        "parameter or the ZENTRA_API_TOKEN environment variable."
    )


def _strip_token_prefix(token: str) -> str:
    lowered = token.lower()
    if lowered.startswith("token "):
        return token.split(None, 1)[1].strip()
    return token


def validate_api_base_url(url: str, allow_http_localhost: bool = True) -> str:
    candidate = (url or DEFAULT_API_BASE_URL).strip().rstrip("/")
    parsed = urlparse(candidate)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise ConfigError("api_base_url must be an absolute http(s) URL")
    if parsed.scheme == "http":
        host = (parsed.hostname or "").lower()
        if not (allow_http_localhost and host in ("localhost", "127.0.0.1", "::1")):
            raise ConfigError("api_base_url must use HTTPS")
    return candidate


def parse_positive_int(value: Optional[str], default: int, name: str, maximum: Optional[int] = None) -> int:
    raw = default if value is None or str(value).strip() == "" else value
    try:
        parsed = int(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError("%s must be an integer" % name) from exc
    if parsed <= 0:
        raise ConfigError("%s must be greater than zero" % name)
    if maximum is not None and parsed > maximum:
        raise ConfigError("%s must be at most %d" % (name, maximum))
    return parsed


def parse_input_xml(xml_text: str) -> InputConfig:
    """Parse Splunk run (``<input>``) or validation (``<items>``) XML from stdin."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ConfigError("Unable to parse Splunk input XML: %s" % exc) from exc

    cfg = InputConfig()
    cfg.server_host = _child_text(root, "server_host")
    cfg.server_uri = _child_text(root, "server_uri")
    cfg.session_key = _child_text(root, "session_key")
    cfg.checkpoint_dir = _child_text(root, "checkpoint_dir")

    tag = (root.tag or "").split("}")[-1]
    if tag == "items":
        for item in root.findall("item"):
            cfg.stanzas.append(_stanza_from_element(item, item.get("name") or ""))
        return cfg

    configuration = root.find("configuration")
    if configuration is None:
        # Some Splunk versions wrap a single stanza at the root.
        if root.find("param") is not None:
            cfg.stanzas.append(_stanza_from_element(root, root.get("name") or ""))
        return cfg
    for stanza_el in configuration.findall("stanza"):
        cfg.stanzas.append(_stanza_from_element(stanza_el, stanza_el.get("name") or ""))
    return cfg


def _stanza_from_element(element: ET.Element, name: str) -> InputStanza:
    params = {}
    for param in element.findall("param"):
        pname = param.get("name")
        if not pname:
            continue
        params[pname] = (param.text or "").strip()
    return InputStanza(name=name, params=params)


def stanza_devices(stanza: InputStanza) -> List[str]:
    return parse_device_sns(stanza.params.get("device_sns"))


def stanza_settings(stanza: InputStanza) -> dict:
    token = resolve_api_token(stanza.params.get("api_token"))
    return {
        "api_token": token,
        "api_base_url": validate_api_base_url(
            stanza.params.get("api_base_url", DEFAULT_API_BASE_URL)
        ),
        "device_sns": stanza_devices(stanza),
        "lookback_hours": parse_positive_int(
            stanza.params.get("lookback_hours"), 168, "lookback_hours", maximum=24 * 30
        ),
        "per_page": parse_positive_int(
            stanza.params.get("per_page"), 2000, "per_page", maximum=2000
        ),
        "index": stanza.index,
        "sourcetype": stanza.sourcetype,
        "host": stanza.host,
    }


def iter_stanzas_to_validate(xml_text: str) -> Iterable[InputStanza]:
    cfg = parse_input_xml(xml_text)
    return cfg.stanzas


def _child_text(parent: ET.Element, tag: str) -> str:
    child = parent.find(tag)
    if child is None or child.text is None:
        return ""
    return child.text.strip()
