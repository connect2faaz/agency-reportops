# AGENTS.md

Compressed handoff for agents working in this workspace.

## Workspace

This is now a headless Python Modal workflow for agency report automation. Public-facing docs call the system `Agency's ReportOps`. It has no frontend, no Next.js runtime, and no SaaS dashboard. The product reads Google Sheets, generates HTML email reports with OpenRouter structured output, routes reports through Gmail account-manager approval, sends approved reports to clients, and polls Gmail replies every five minutes during the configured UTC business-hours window.

The previous TypeScript/Next implementation was removed. Do not reintroduce React, Next, local dashboard UI, local JSON workflow state, or npm scripts unless the user explicitly asks.

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

Metric periods are canonicalized to `Mon-YYYY` in code, so common Sheet values like `Feb-2026`, `Feb 2026`, `2026-02`, and `2/1/2026` can match the requested report period. If no matching metrics are found for the requested period, block the run with a clear `No metrics found` error and do not call OpenRouter or send email.

After the approved report is actually sent to the client, clear the client's `run_now` flag and advance `next_report_date` by one month. Do not advance schedule state for blocked runs or account-manager-review-only sends.

Manual `run_now --client-id ... --period ...` is an explicit override and may regenerate/resend a same-period AM review. AM change requests also regenerate and resend the AM review. Scheduled daily retries should not.

Email identities are separate: `SYSTEM_SENDER_EMAIL` is the Gmail account used to send mail, while each client row's `account_manager_email` is the recipient who approves reports. The system does not infer the account manager from the sender email.

OpenRouter model:

```text
openai/gpt-oss-120b:free
```

If OpenRouter is unavailable, rate-limited, or returns invalid structured output, mark the run blocked in Sheets and do not send the report.

Report generation returns a JSON envelope for validation, but the client-facing content is `html_report`. The report prompt requires a stable HTML structure rooted at `div.report-shell` with fixed section class names and inline styles so email layout stays consistent across generations.

Client Q&A uses a separate OpenRouter structured-output prompt with `intent`, `risk_level`, `risk_reason`, `answer_html`, and `requires_am_review`. Low-risk report/metric explanations are auto-sent immediately. High-risk, unclear, complaint, budget/strategy, guarantee, legal/compliance, and metric-discrepancy messages route to account-manager review.

## Important Files

- `modal_app.py`: Modal app, daily schedule, five-minute Gmail poller, manual `run_now`.
- `reportops/workflow.py`: Workflow state transitions and reply handling.
- `reportops/models.py`: Dataclasses and status types.
- `reportops/sheets.py`: Google Sheets API client and Sheet-backed store.
- `reportops/gmail_api.py`: Gmail REST API client.
- `reportops/gmail.py`: Email building, reply matching, thread-scoped polling helpers, and conservative classifiers.
- `reportops/google_auth.py`: OAuth refresh-token helper.
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
OPENROUTER_MODEL=openai/gpt-oss-120b:free
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
```

Google OAuth needs Gmail send/modify/read scopes and Google Sheets read/write scopes.

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

Manual run:

```powershell
py -3.13 -m modal run modal_app.py::run_now --client-id client_brightsmile_dental --period Feb-2026
```

Manual run for all due/manual clients:

```powershell
py -3.13 -m modal run modal_app.py::run_now
```

## Modal Schedules

- Daily reports: `0 0 * * *` UTC.
- Gmail reply polling: `*/5 9-18 * * *`, meaning every five minutes during UTC hours `09:00` through `18:59`.
- `run_now` is a manual Modal function only; it is not scheduled.

## Guardrails

- No frontend.
- No paid model fallback by default.
- No ad-platform integrations in v1; Google Ads and Meta Ads should be added later behind a data-source adapter.
- Keep Google Sheets as the v1 control and state plane.
- Preserve duplicate protection through processed Gmail message IDs.
- Keep reply matching strict: headers first, then stored Gmail thread IDs, then body fallback markers. Do not reintroduce the old "only active run" fallback.
- Poll active Gmail threads directly. Do not rely on broad subject search for normal reply processing.
- Keep hidden fallback markers in outgoing HTML, but do not show raw run IDs visibly to clients.
- Keep same-period AM review suppression and 24-hour AM follow-up reminders. Do not reintroduce daily scheduled OpenRouter regeneration for an already open same-period `am_review` run.
- Auto-answer only low-risk client Q&A. Escalate unhappy-client language and metric discrepancies to account-manager review.
- Do not send reports when AI output validation fails.
