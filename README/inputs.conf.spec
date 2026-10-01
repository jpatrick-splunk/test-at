[zentra_weather://<name>]
* Poll ZENTRA Cloud v4 for field logger weather readings.

device_sns = <value>
* Comma-separated logger serial numbers. At most three.
* Default: z6-30302

api_token = <value>
* ZENTRA Cloud personal API token. Stored by Splunk when set in the UI.
* Prefer Splunk Web / REST so the value is not stored in plaintext source control.
* You may also set the ZENTRA_API_TOKEN or ZENTRACLOUD_TOKEN environment variable.

api_base_url = <value>
* HTTPS origin, for example https://zentracloud.com or https://zentracloud.eu.
* Default: https://zentracloud.com

lookback_hours = <value>
* Hours of history requested on the first poll for a logger.
* Default: 336 (14 days). Increase and clear the logger checkpoint to backfill.

per_page = <value>
* Readings per API page. Maximum 2000.
* Default: 2000
