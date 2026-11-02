# oura_ring_viewer

## What it does

`oura_ring_viewer` brings a patient's Oura Ring data into the Canvas chart
drawer. Staff generate a one-tap connection link, deliver it to the patient,
and once the patient authorizes with Oura the chart shows last night's sleep
score, total sleep, resting heart rate, HRV average and SpO2 average, plus
trends over the last 7, 14, 30 or 90 days. Sync is on demand through the Oura
API v2; there is no nightly job.

## Scope and clinical framing

Oura data is **consumer wellness data**, so this plugin presents it as
patient-generated health data and never as clinical vitals. The chart carries a
"not a medical device" disclaimer.

**v1 does not write Observations into the chart.** Charting wearable data is a
clinical-workflow decision that belongs to the practice, not to the plugin
author, so it is deliberately left out of the first release and can be offered
later as an opt-in command.

## Status

In progress. Built and tested:

- Manifest, application entry point, and the three custom data models
- Oura API v2 client: token exchange, refresh, paginated collection reads
- OAuth orchestration with single-use refresh token rotation
- Sync engine: refresh at most once per sync, per-family 403 handling,
  windowing, retention purge
- Normalization of raw Oura records into the plugin's own payload keys
- Persistence: idempotent bulk upserts, patient-scoped reads and disconnect

Not yet written: the two protocols' handler bodies, magic-link signing, and the
chart template. Those routes raise `NotImplementedError` rather than returning a
plausible empty response, so an unfinished route fails loudly.

## Permissions requested

Only `daily` and `spo2`. Those cover the sleep, readiness and activity
summaries, the sleep periods, and overnight SpO2, which is everything v1 reads.

The plugin deliberately does **not** request `personal` (age, weight, sex,
email), intraday `heartrate`, or `workout`. Not holding data is a stronger
position than holding it and ignoring it, and it means no Oura user identifier
is stored either.

## Partial access

Oura's consent screen lets a patient untick individual scopes. If an endpoint
returns 403, the sync skips that metric family and keeps the rest, rather than
treating the whole connection as broken. A 401 is different: it means the
access token expired, so the sync refreshes once and retries.

If every endpoint returns 403, nothing was fetched and the sync records an
`all_forbidden` error. To be confirmed against live behavior: Oura may also
return 403 when a ring owner's Oura membership has lapsed, in which case this
error is the visible symptom.

## Data model

One row per patient per day per metric family (`sleep`, `readiness`,
`activity`, `spo2`). Oura exposes intraday series, but v1 stores daily
granularity only, which keeps the table bounded for the Canvas
database-performance review: a 90 day retention window over four families is at
most 360 rows per patient. Normalized metrics live in `payload`, and the unique
constraint on `(patient_id, day, family)` makes re-syncing an overlapping range
idempotent.

## Configuration

Five manifest variables, two of them sensitive:

| Variable | Sensitive | Notes |
|---|---|---|
| `OURA_CLIENT_ID` | no | From the Oura API application |
| `OURA_CLIENT_SECRET` | yes | From the Oura API application |
| `OURA_REDIRECT_URI` | no | Must match the registered callback exactly |
| `OURA_ENVIRONMENT` | no | `production`, or anything else for sandbox |
| `OURA_MAGIC_LINK_SECRET` | yes | HMAC key for the patient magic link |

`OURA_ENVIRONMENT` fails safe: any value other than `production` routes to the
sandbox collection, so a typo cannot reach live patient data.

## Development

Requires Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest -q --cov=oura_ring_viewer
uv run mypy oura_ring_viewer tests
```

The Canvas SDK's pytest plugin creates the plugin's tables in a SQLite test
database using the same DDL generation as a production install, so the storage
and sync tests run against the real schema. The Oura client is mocked at the
HTTP layer; nothing in the test suite touches the network.

## License

MIT. See `../LICENSE`.
