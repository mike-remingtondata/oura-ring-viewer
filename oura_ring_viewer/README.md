# oura_ring_viewer

## What it does

`oura_ring_viewer` brings a patient's Oura Ring data into the Canvas chart
drawer. Staff send the patient a secure connection link, the patient approves
sharing in Oura, and the chart then shows last night's sleep score, total
sleep, resting heart rate and HRV, plus readiness, steps and overnight blood
oxygen, with trends over the last 7, 14, 30 or 90 days. Sync is on demand
through the Oura API v2; there is no nightly job.

## Scope and clinical framing

Oura data is **consumer wellness data**, so this plugin presents it as
patient-generated health data and never as clinical vitals. The chart carries a
"not a medical device" disclaimer.

**v1 does not write Observations into the chart.** Charting wearable data is a
clinical-workflow decision that belongs to the practice, not to the plugin
author, so it is deliberately left out of the first release and can be offered
later as an opt-in command.

## How a patient connects

1. Staff open the Oura Ring tile in the patient's chart and choose **Send via
   patient portal** or **Create link to copy**.
2. The patient opens the link on their phone within 15 minutes and is sent to
   Oura's own consent screen, where they sign in and approve sharing.
3. Oura returns them to the plugin, which stores the connection and shows a
   confirmation page explaining how to stop sharing.
4. Staff press **Sync now** to pull data. Opening the chart afterwards reads
   only stored data and costs no Oura API call.

Creating a new link cancels any earlier one, and a link stops working once the
connection completes.

## Status

Feature complete for v1 and tested end to end against mocked Oura responses:
every module is at 100% line coverage, strict mypy is clean, and
`canvas validate` loads every handler in the Canvas sandbox.

Not yet done, and not possible without credentials:

- Verifying endpoint paths, scope strings and response shapes against a live
  Oura API application (see the note at the top of `services/settings.py`)
- Installing on a Canvas instance and running the Canvas Plugin Assistant
  security and performance reviews

## Permissions requested

Only `daily` and `spo2`. Those cover the sleep, readiness and activity
summaries, the sleep periods, and overnight SpO2, which is everything v1 reads.

The plugin deliberately does **not** request `personal` (age, weight, sex,
email), intraday `heartrate`, or `workout`. Not holding data is a stronger
position than holding it and ignoring it, and it means no Oura user identifier
is stored either.

## Partial access

Oura's consent screen lets a patient untick individual scopes. If an endpoint
returns 403, the sync skips that metric family, keeps the rest, and tells staff
which data the patient is not sharing. A 401 is different: it means the access
token expired, so the sync refreshes once and retries.

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
| `OURA_REDIRECT_URI` | no | `https://<your-instance>/plugin-io/api/oura_ring_viewer/callback`, registered with Oura exactly |
| `OURA_ENVIRONMENT` | no | `production`, or anything else for sandbox |
| `OURA_MAGIC_LINK_SECRET` | yes | Random string of at least 32 characters, used to sign patient links |

Each route checks only the settings it needs and reports anything missing or
unusable to staff by name, rather than failing when a patient taps a link.

- `OURA_ENVIRONMENT` fails safe: any value other than `production` routes to
  the sandbox, so a typo cannot reach live patient data.
- `OURA_REDIRECT_URI` must be `https`, since the connection link it derives
  carries a bearer token. Plain `http` is accepted only for `localhost`.
- `OURA_MAGIC_LINK_SECRET` shorter than 32 characters is refused.

## Security notes

- **Staff routes** require a Canvas staff session. **Patient routes**
  (`/connect`, `/callback`) are public by necessity and are protected instead
  by signed, expiring tokens bound to a single-use nonce.
- Link and OAuth state tokens carry a type claim, so neither can be replayed as
  the other. The signing algorithm is pinned, so an unsigned `alg: none` token
  is rejected.
- `patient_id` is accepted only as letters, digits and hyphens, on every route,
  before it reaches the database or a template.
- No route returns token material. Tokens are stored without encryption at
  rest because the Canvas sandbox blocks `cryptography`, the same documented
  constraint as `dexcom-cgm-viewer`; `services/crypto.py` is the seam for real
  encryption when the platform allows it.
- The chart page loads no external scripts and writes every value with
  `textContent`, never `innerHTML`.
- **Disconnect** deletes every row this plugin holds for the patient, but does
  not yet revoke the grant at Oura. Patients can revoke it in their Oura
  account, as the confirmation page tells them.

## Open questions

- **Per-patient authorization.** Any authenticated staff session can call the
  staff routes for any patient, matching `dexcom-cgm-viewer`. Confirm whether
  Canvas expects plugins to add their own check.
- **Revoking at Oura on disconnect.** Add once Oura's revoke endpoint is
  confirmed against a live application.
- **Lapsed membership.** Confirm whether Oura returns 403 for a ring owner whose
  membership has lapsed, and adjust the staff wording if not.

## Development

Requires Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest -q --cov=oura_ring_viewer
uv run mypy oura_ring_viewer tests
canvas validate oura_ring_viewer
```

`canvas validate` loads every handler in the real Canvas sandbox, which
enforces import and builtin restrictions that ordinary tests do not, so run it
after any change to imports.

The Canvas SDK's pytest plugin creates the plugin's tables in a SQLite test
database using the same DDL generation as a production install, so storage,
sync and handler tests run against the real schema. Handler tests render the
real templates through the SDK's own template engine. The Oura client is mocked
at the HTTP layer; nothing in the test suite touches the network.

## License

MIT. See `../LICENSE`.
