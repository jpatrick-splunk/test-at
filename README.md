# ZENTRA Weather for Splunk 10

Splunk app that polls **ZENTRA Cloud v4** for up to three field loggers (default `z6-30302`) and indexes each **15-minute measurement** as it appears on the logger dashboard.

This repository **is** the Splunk app. Splunk loads apps from `$SPLUNK_HOME/etc/apps/<app_id>/` and looks for `default/app.conf` at that root. The folder name should be `zentra_weather` so it matches `[package] id` in `default/app.conf`.

## Why Splunk, if ZENTRA already charts the logger?

The [z6-30302 dashboard](https://zentracloud.com/#/dashboard_detail/z6-30302) shows ATMOS / ZL6 samples on a 15-minute clock. **Precipitation is rainfall during that interval, not a running total.** Hovering a bar tells you how much fell in those 15 minutes; it does not tell a customer how much rain accumulated that day or week.

This app:

1. Indexes those interval samples unchanged (`measurement`, `value`, `units`, timestamp).
2. Sums Precipitation over the day and week so accumulated rainfall is a first-class number.

Temperature, humidity, and wind stay interval averages (the logger already averaged them). Only measurements such as Precipitation and lightning strikes are summed across intervals.

## Fresh start (required if earlier data looked wrong)

Do not keep mixing events from `main` or `weather`. This revision writes **one event per measurement reading** to index `zentra_validate`, sourcetype `zentra:reading`.

**The index must exist before the input runs.** Creating a data input named `validate` does not create the index. If `zentra_validate` is missing, Splunk drops every event and searches stay empty.

1. Place this repository at `$SPLUNK_HOME/etc/apps/zentra_weather`.
2. Create the index, then restart Splunk:

   ```
   $SPLUNK_HOME/bin/splunk add index zentra_validate
   $SPLUNK_HOME/bin/splunk restart
   ```

   Or **Settings → Indexes → New Index** → name `zentra_validate` → Save, then restart.
3. Confirm **Settings → Indexes** lists `zentra_validate`.
4. In **Settings → Data inputs → ZENTRA Cloud Weather**:
   - Disable any older `field_loggers` input.
   - Open `validate` (or create it). Set the API token. Set **Index** to `zentra_validate` (not `main`). Keep `device_sns = z6-30302`.
   - Disable, save, then enable to force a poll. Wait about a minute.
5. In the app, open **Collection Status**. It searches every index plus `index=_internal zentra_weather`. If readings landed in `main`, change the input's Index and poll again.

The default input stanza is **disabled** until you add a token. Never put tokens in `default/`.

If Collection Status is empty, run these in Search (All time):

```
index=* (sourcetype=zentra:reading OR source=zentra_weather://*)
index=_internal zentra_weather
```

## Collection stopped after 9/23

The logger is current on ZENTRA Cloud. Splunk stopped at `2026-09-23 23:45:00-05:00` (`mrid` 59781). Two poller mistakes caused that:

1. After the first dump it asked for `start_mrid`, which the dataframe API does not fill.
2. **Ignore checkpoint = true** re-requests 30 days from the **oldest** end. ZENTRA `per_page` is 2000 *readings* (~20 days). Page 1 is already-indexed September data; `next_url` stays set even on empty pages. Nothing after 23:45 is indexed.

This revision always resumes from that 23:45 sample with `start_date`/`end_date`, even when Ignore checkpoint is on. Do **not** delete the 9/23 events.

```
cd /Applications/Splunk/etc/apps/zentra
git pull
```

Restart Splunk. In **Settings → Data inputs → ZENTRA Cloud Weather → validate**:

- Set **Ignore checkpoint** to **false** (unchecked). Leave it off.
- **Interval** `900`.
- Disable, save, then enable `validate` once.

Confirm catch-up (All time):

```
index=zentra_validate sourcetype=zentra:reading device_sn=z6-30302
| stats max(datetime) as last_datetime max(mrid) as last_mrid

index=_internal zentra_weather (window= OR lag_hours= OR catch-up OR page_num_readings)
```

`last_datetime` should move past `2026-09-23 23:45:00-05:00` toward now. Logs should show `window=catch-up`, `page_end` on 10/02, and `start_date=2026-09-24 04:30:00`, not a 30-day lookback.

## Validate 15-minute numbers against ZENTRA Cloud

Open **ZENTRA Weather → Reading Validation** next to [the logger dashboard](https://zentracloud.com/#/dashboard_detail/z6-30302).

- The measurement list must match the chart titles on that page (`Precipitation`, `Air Temperature`, and so on).
- Pick **Precipitation**. Each row is one 15-minute sample. The value and units must match the tooltip at that timestamp on ZENTRA Cloud.
- Do not expect that 15-minute value to equal a day's rain.

Then open **Rainfall Totals**. Daily and weekly columns are `sum` of those interval Precipitation samples for the logger's local day/week.

```
`zentra_interval_precip`
| eval _time=_time+coalesce(tonumber(tz_offset), 0)
| timechart span=1d sum(value) as daily_rainfall
```

## Configure the poller

Create a ZENTRA Cloud personal API token (ZENTRA Cloud → API → Keys).

Set it in one of these ways (first match wins):

1. Splunk Web → **Settings → Data inputs → ZENTRA Cloud Weather** → enable `validate` → **API token**.
2. `$SPLUNK_HOME/etc/apps/zentra_weather/local/inputs.conf` (not `default/`):

   ```
   [zentra_weather://validate]
   disabled = 0
   api_token = <your-token>
   device_sns = z6-30302
   index = zentra_validate
   ```

3. Environment variable `ZENTRA_API_TOKEN` or `ZENTRACLOUD_TOKEN` on the Splunk process.

| Parameter | Default | Notes |
| --- | --- | --- |
| `device_sns` | `z6-30302` | Comma-separated, **maximum three** loggers |
| `api_base_url` | `https://zentracloud.com` | Use `https://zentracloud.eu` for the EU server. HTTPS is required. |
| `lookback_hours` | `720` | Hours of history on a date-range poll (30 days). After the first dump the poller resumes from the last logger datetime, not `start_mrid`. |
| `per_page` | `2000` | ZENTRA maximum |
| `interval` | `900` | Seconds. Matches the typical 15-minute logger measurement interval. ZENTRA allows one API call per device per minute. |
| `index` | `zentra_validate` | Keep validation data out of `main` / `weather` until numbers match the dashboard |

Check `$SPLUNK_HOME/var/log/splunk/splunkd.log` for `zentra_weather` lines such as `no measurements parsed`.

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

Polls `GET {api_base_url}/api/v4/get_readings/` as described in the [v4 API documentation](https://zentracloud.com/api/v4/documentation/) and the [US server guide](https://docs.zentracloud.io/l/en/article/gbv2iyxhar-api-v-3-0-us): `Authorization: Token …`, `start_date`/`end_date` **or** `start_mrid`/`end_mrid` (not both), `per_page` up to 2000. The documented Python example uses `output_format=df` and `pandas.DataFrame(**json.loads(data["data"]))`. This app defaults to that dataframe pull for a 30-day window so interval samples are not reduced to a single json reading per sensor.
