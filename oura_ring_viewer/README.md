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

Early scaffold. Settled and in place:

- Manifest, application entry point, and the three custom data models
- Environment routing between the Oura sandbox and production collections
- Token storage seam (`services/crypto.py`)
- Route shapes and the auth model for both protocols

Not yet written: the handler bodies, the Oura API client, sync, aggregation,
and the chart template. Routes currently raise `NotImplementedError` rather
than returning a plausible empty response, so an unfinished route fails loudly.

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

Requires Python 3.12 or newer.

```bash
uv run --python 3.12 --with pytest pytest -q
```

## License

MIT. See `../LICENSE`.
