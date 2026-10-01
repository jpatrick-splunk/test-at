# ZENTRA Weather for Splunk 10

Splunk app that polls **ZENTRA Cloud v4** for up to three field loggers (default `z6-30302`), indexes one normalized weather event per observation timestamp, and ships a custom **Weather** data model plus Simple XML dashboards.

This repository **is** the Splunk app. Splunk loads apps from `$SPLUNK_HOME/etc/apps/<app_id>/` and looks for `default/app.conf` at that root. The folder name should be `zentra_weather` so it matches `[package] id` in `default/app.conf`.

## What you get

| Piece | Location |
| --- | --- |
| Modular input `zentra_weather` | `bin/zentra_weather.py`, `README/inputs.conf.spec` |
| Sourcetype `zentra:weather` | `default/props.conf` |
| Weather data model | `default/data/models/Weather.json` |
| Dashboards | Weather Overview, Logger Detail, Data Quality |
| Optional `weather` index | `default/indexes.conf` |

Each indexed event is JSON with CIM-style weather fields (`air_temperature`, `relative_humidity`, `wind_speed`, `precipitation`, `solar_radiation`, location, `error_flag`, and so on) rather than ZENTRA's measurement-keyed arrays.

## Install

1. Place this repository at `$SPLUNK_HOME/etc/apps/zentra_weather` (clone, copy, or symlink). Do not nest it one directory deeper — Splunk will not see `default/app.conf`.
2. Confirm a `weather` index exists, or keep the app's `indexes.conf` on the indexer tier.
3. Restart Splunk.

The default input stanza is **disabled** until you add a token.

## Configure the poller

Create a ZENTRA Cloud personal API token (ZENTRA Cloud → API → Keys). Do not put the token in source control.

Set it in one of these ways (first match wins):

1. Splunk Web → **Settings → Data inputs → ZENTRA Cloud Weather** → enable `field_loggers` → **API token**.
2. `$SPLUNK_HOME/etc/apps/zentra_weather/local/inputs.conf` (not `default/`):

   ```
   [zentra_weather://field_loggers]
   disabled = 0
   api_token = <your-token>
   device_sns = z6-30302
   ```

3. Environment variable `ZENTRA_API_TOKEN` or `ZENTRACLOUD_TOKEN` on the Splunk process.

| Parameter | Default | Notes |
| --- | --- | --- |
| `device_sns` | `z6-30302` | Comma-separated, **maximum three** loggers |
| `api_base_url` | `https://zentracloud.com` | Use `https://zentracloud.eu` for the EU server. HTTPS is required. |
| `lookback_hours` | `24` | Used only before a per-logger checkpoint exists |
| `per_page` | `2000` | ZENTRA maximum |
| `interval` | `300` | Seconds. ZENTRA allows one call per device per minute. |

After the first successful poll the input checkpoints the highest measurement record ID (`mrid`) under `$SPLUNK_HOME/var/lib/splunk/modinputs/zentra_weather/` and requests `start_mrid` on later runs.

## Search and dashboards

```
`zentra_weather_search`
| timechart avg(air_temperature) by device_sn

| datamodel Weather Weather search
| rename "Weather.*" as *
```

Open the **ZENTRA Weather** app for:

- **Weather Overview** — latest values, temperature/humidity/wind/precip, map, logger table
- **Logger Detail** — one serial (defaults to `z6-30302`)
- **Data Quality** — freshness, error flags, volume

## TLS, secrets, and certificates

- API calls use HTTPS with the platform trust store (`ssl.create_default_context()`). The app does not bundle or hardcode X.509 certificates or private keys.
- Tokens are read from Splunk input parameters or the process environment. Nothing in `default/` contains credentials.
- Non-HTTPS API URLs are rejected except `http://localhost` for lab use.

## Develop / test

From the repository root (stdlib `unittest`, no extra packages):

```
python -m unittest discover -s tests -v
```

The modular input is Python 3 only (Splunk 10). It uses the standard library (`urllib`, `ssl`, `xml`) so it runs inside Splunk without `pip install`.

## ZENTRA Cloud v4

Polls `GET {api_base_url}/api/v4/get_readings/` with `Authorization: Token …`, `output_format=json`, and either `start_mrid` or `start_date`/`end_date`. Measurement names such as `Air Temperature` are mapped onto the Weather model. Unknown measurements are kept as snake_case extra fields.
