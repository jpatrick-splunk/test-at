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
* Hours of history requested on a date-range poll.
* Default: 720 (30 days). Official v4 docs use start_date and end_date together.

output_format = <value>
* ZENTRA output_format: json, df, or csv.
* Default: df. This matches the official v4 Python example, which loads
* pandas.DataFrame(**json.loads(response["data"])) for a full date range.

ignore_checkpoint = <value>
* If true, ignore saved MRIDs and pull start_date/end_date for lookback_hours.
* Default: true during validation so a 30-day dump is not reduced to one sample.

per_page = <value>
* Readings per API page. Maximum 2000.
* Default: 2000

index = <value>
* Splunk index that receives the 15-minute readings.
* Default: zentra_validate
* Create this index under Settings → Indexes before enabling the input.
* If the index does not exist, Splunk drops the events and searches stay empty.

interval = <value>
* Seconds between polls. Default 60 during validation. ZENTRA allows one call per device per minute.
