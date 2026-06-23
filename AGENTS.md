# AGENTS.md

Compressed handoff for agents working in this workspace.

## Workspace

This is now a headless Python Modal workflow for agency report automation. Public-facing docs call the system `Agency's ReportOps`. It has no frontend, no Next.js runtime, and no SaaS dashboard. The product reads Google Sheets, generates HTML email reports with OpenRouter structured output, routes reports through Gmail account-manager approval, sends approved reports to clients, processes Gmail replies through Pub/Sub push notifications when configured, and keeps a five-minute Gmail reply poller available as a manual/fallback function.

The previous TypeScript/Next implementation was removed. Do not reintroduce React, Next, local dashboard UI, local JSON workflow state, or npm scripts unless the user explicitly asks.

Ignored local leftovers from the retired frontend or generated test/report runs can be removed during workspace cleanup: `.next/`, `node_modules/`, `app/`, `data/`, `artifacts/`, `.test-data/`, `references/`, and Python `__pycache__/` folders. Keep `.env` and `.env.local` local; do not read, print, delete, or commit them.

Deployed Modal app:

```text
https://modal.com/apps/connect2faaz/main/deployed/reportops-headless
```

## Core Flow

Daily UTC flow:

Client schedule in Google Sheets -> due report detection -> metrics read from `Metrics` tab -> target reporting month plus previous month selected for the same `client_id` -> OpenRouter structured report generation -> fixed-structure HTML report email to account manager -> wait for Gmail reply.

If a scheduled daily run finds an existing same-period `am_review` run for the client, do not call OpenRouter again and do not replace the existing report. If the latest AM review/reminder was sent more than 24 hours ago, send a short account-manager follow-up in the same Gmail thread with the hidden `run:{run_id}` marker. This suppression only applies to the same reporting period; an open prior-period AM review must not block a new later-period report.

Reply polling flow:

Active run Gmail thread IDs -> fetch only those Gmail threads -> ignore already processed Gmail message IDs -> match by headers/thread/body marker -> AM approval sends client report -> AM change request regenerates and resends to AM -> client replies become Q&A -> low-risk auto-reply in the same client email thread or high-risk AM review.

Optional Gmail Pub/Sub push flow:

Gmail mailbox change -> Google Pub/Sub topic -> Pub/Sub push subscription -> Modal `gmail_pubsub_push` webhook -> `process_gmail_push` background function -> existing `ReportingWorkflow.sync_replies()` logic. Pub/Sub notifications only wake the app up; they do not include the full email body, and v1 does not persist Gmail history cursors. Preserve processed Gmail message ID dedupe.

Metric periods are canonicalized to `Mon-YYYY` in code, so common Sheet values like `Feb-2026`, `Feb 2026`, `2026-02`, and `2/1/2026` can match the requested report period. If no matching metrics are found for the requested period, block the run with a clear `No metrics found` error and do not call OpenRouter or send email.

After the approved report is actually sent to the client, clear the client's `run_now` flag and advance `next_report_date` by one month. Do not advance schedule state for blocked runs or account-manager-review-only sends.

Manual `run_now` is an explicit override and may regenerate/resend same-period AM reviews. With `--client-id`, it runs only that client. Without `--client-id`, it runs every non-paused client regardless of `next_report_date` or the Sheet `run_now` flag. AM change requests also regenerate and resend the AM review. Scheduled daily retries should not.

Manual and scheduled runs both use the `Runs` tab. The `Runs` tab is not append-only history: a same-client, same-period retry reuses the existing run row and increments `attempt_count` instead of creating a new run. `Messages` is separate and can have many rows per run because it records AM review emails, follow-ups, client deliveries, Q&A replies, and processed-reply dedupe markers.

Email identities are separate: `SYSTEM_SENDER_EMAIL` is the Gmail account used to send mail, each client row's `account_manager_email` is the recipient who approves reports, and each client row's `support_email` is the internal address for blocked-run failure notices. The system does not infer the account manager or support recipient from the sender email.

OpenRouter model:

```text
openai/gpt-4o-mini
```

If OpenRouter is unavailable, rate-limited, or returns invalid structured output after the structured-output retry/repair attempts, mark only that client run blocked in Sheets, send a blocked-run notice to the client's `support_email` when present, and do not send the report. Report generation currently makes the initial structured-output call, one normal retry after invalid output, and one stricter final repair attempt that explicitly requires the five report JSON fields before blocking. OpenRouter requests should use structured-output-capable routing (`provider.require_parameters=true`) and response healing.

Report generation returns a JSON envelope for validation, but the client-facing content is `html_report`. The report prompt requires a stable HTML structure rooted at `div.report-shell` with fixed section class names and inline styles so email layout stays consistent across generations.

Client Q&A uses a separate OpenRouter structured-output prompt with `intent`, `risk_level`, `risk_reason`, `answer_html`, and `requires_am_review`. Low-risk report/metric explanations are auto-sent immediately. High-risk, unclear, complaint, budget/strategy, guarantee, legal/compliance, and metric-discrepancy messages route to account-manager review.

## Important Files

- `modal_app.py`: Modal app, daily schedule, unscheduled/manual five-minute Gmail poller fallback, Pub/Sub webhook/background processor, Gmail watch renewal, manual `run_now`.
- `reportops/workflow.py`: Workflow state transitions and reply handling.
- `reportops/models.py`: Dataclasses and status types.
- `reportops/sheets.py`: Google Sheets API client and Sheet-backed store.
- `reportops/gmail_api.py`: Gmail REST API client.
- `reportops/gmail.py`: Email building, reply matching, thread-scoped polling helpers, and conservative classifiers.
- `reportops/google_auth.py`: OAuth refresh-token helper.
- `reportops/pubsub.py`: Gmail Pub/Sub notification decoding and shared-token webhook guard.
- `reportops/ai.py`: OpenRouter structured-output client.
- `tests_py/*.py`: Python unittest coverage.
- `README.md`: Beginner-friendly setup guide for semi-technical/non-technical operators, including Google refresh-token setup, Sheet headers, OpenRouter setup, Modal deploy, troubleshooting, and final checklist.

## Sheet Tabs

Required tabs:

- `Clients`
- `Metrics`
- `Runs`
- `Messages`
- `Questions`

The workflow never infers client grouping from row order. Always join by `client_id`, `run_id`, Gmail `thread_id`, or stored Gmail message IDs.

Recommended `Metrics.month` format is `Feb-2026`, although the parser accepts several date-like formats. A run without `--period` uses the previous month, so test/demo Sheet data must include that month or the run will block with no emails sent.

Demo workbook:

```text
https://docs.google.com/spreadsheets/d/1wuqrC8luw4WXOghHxDX9ZbwNbhMAvld2eADLOM2Y-ew/edit
```

## Env

Do not read or print `.env.local`.

Required Modal secret values:

```text
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REFRESH_TOKEN=
GOOGLE_SHEETS_SPREADSHEET_ID=
SYSTEM_SENDER_EMAIL=
OPENROUTER_API_KEY=
OPENROUTER_MODEL=openai/gpt-4o-mini
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
```

Optional Modal secret values for instant Gmail reply detection:

```text
GMAIL_PUBSUB_TOPIC=projects/<project-id>/topics/reportops-gmail-push
GMAIL_PUBSUB_TOKEN=
```

Google OAuth needs Gmail send/modify/read scopes and Google Sheets read/write scopes.

Google OAuth token refresh has transient retry handling for 429/5xx responses. Keep final failures wrapped in plain serializable exceptions so Modal does not fail while serializing raw `urllib` exceptions.

Gmail Pub/Sub setup notes:

- Pub/Sub topic should grant `gmail-api-push@system.gserviceaccount.com` the `Pub/Sub Publisher` role.
- Pub/Sub push subscription should point to `https://connect2faaz--reportops-headless-gmail-pubsub-push.modal.run?token=<GMAIL_PUBSUB_TOKEN>`.
- Set the Pub/Sub subscription expiration period to `Never expire`; this only keeps the subscription alive.
- Gmail watches still expire separately. Gmail requires watch renewal at least every seven days; this app renews daily.

## Commands

Local tests:

```powershell
python -m unittest discover -s tests_py -v
```

Compile check:

```powershell
python -m compileall -q reportops modal_app.py
```

Install Modal CLI if needed:

```powershell
python -m pip install modal
```

Deploy:

```powershell
$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'; py -3.13 -m modal deploy modal_app.py
```

Deploy with the five-minute Gmail reply poller schedule restored:

```powershell
$env:REPORTOPS_DEPLOY_GMAIL_REPLY_POLLER_SCHEDULE='1'; $env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'; py -3.13 -m modal deploy modal_app.py
```

Renew Gmail Pub/Sub watch manually:

```powershell
$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'; py -3.13 -m modal run modal_app.py::renew_gmail_watch
```

Run the Gmail reply poller manually:

```powershell
py -3.13 -m modal run modal_app.py::poll_gmail_replies
```

Manual run:

```powershell
py -3.13 -m modal run modal_app.py::run_now --client-id client_brightsmile_dental --period Feb-2026
```

Manual run for all non-paused clients, whether due or not:

```powershell
py -3.13 -m modal run modal_app.py::run_now
```

Manual all-client runs generate reports sequentially and can take several minutes. `run_now` has an explicit one-hour Modal timeout; keep it longer than the default five-minute timeout unless the workflow is redesigned for per-client parallelism.

## Modal Schedules

- Daily reports: `0 0 * * *` UTC.
- Gmail reply polling: currently not scheduled by default. `poll_gmail_replies` still exists and works manually; set `REPORTOPS_DEPLOY_GMAIL_REPLY_POLLER_SCHEDULE=1` before deploy to restore its `*/5 9-18 * * *` cron.
- Gmail watch renewal: `0 1 * * *` UTC. This keeps optional Gmail Pub/Sub push notifications active.
- `run_now` is a manual Modal function only; it is not scheduled.

## Guardrails

- No frontend.
- No paid model fallback by default.
- No ad-platform integrations in v1; Google Ads and Meta Ads should be added later behind a data-source adapter.
- Keep Google Sheets as the v1 control and state plane.
- Preserve `Runs` timestamp fields when loading from and flushing back to Sheets. Do not rewrite historical `created_at`, `updated_at`, `approved_at`, or `delivered_at` values merely because a later Modal function loaded and flushed the workbook.
- Preserve duplicate protection through processed Gmail message IDs.
- Keep reply matching strict: headers first, then stored Gmail thread IDs, then body fallback markers. Do not reintroduce the old "only active run" fallback.
- Poll active Gmail threads directly. Do not rely on broad subject search for normal reply processing.
- Treat Gmail Pub/Sub as a wake-up accelerator, not the source of email contents. Do not remove the `poll_gmail_replies` fallback function; its Modal cron is intentionally disabled by default for current Pub/Sub testing.
- Protect the Pub/Sub webhook with `GMAIL_PUBSUB_TOKEN`; do not make it unauthenticated.
- Do not persist or rely on Gmail history IDs in v1 unless a deliberate history-cursor design is added.
- Keep hidden fallback markers in outgoing HTML, but do not show raw run IDs visibly to clients.
- Keep same-period AM review suppression and 24-hour AM follow-up reminders. Do not reintroduce daily scheduled OpenRouter regeneration for an already open same-period `am_review` run.
- Auto-answer only low-risk client Q&A. Escalate unhappy-client language and metric discrepancies to account-manager review.
- Do not send reports when AI output validation fails after all configured structured-output retry/repair attempts.
