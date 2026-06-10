# Agency's ReportOps Setup Guide

Agency's ReportOps sends monthly client reports from Google Sheets through Gmail after account-manager approval.

In plain English:

1. You keep clients and metrics in a Google Sheet.
2. The app checks which clients need reports.
3. OpenRouter writes the report as an HTML email.
4. Gmail sends the report to the account manager first.
5. The account manager replies `Approved`.
6. Gmail sends the approved report to the client.
7. The app keeps checking report email threads for replies and follows up with the account manager if a review waits too long.

There is no dashboard or website to run. Google Sheets is the control panel.

## What You Need Before Starting

- A Google account.
- A Gmail inbox that will send the emails.
- A Google Cloud project.
- An OpenRouter account.
- A Modal account.
- Python installed on your computer.
- This project folder.
- An IDE agent such as Codex, Claude Code, or Cursor. This is optional, but it makes the Sheet setup easier.

Official reference links:

- Google OAuth docs: https://developers.google.com/identity/protocols/oauth2
- Gmail API scopes: https://developers.google.com/workspace/gmail/api/auth/scopes
- Google Sheets API scopes: https://developers.google.com/sheets/api/scopes
- OpenRouter quickstart: https://openrouter.ai/docs/quickstart
- Modal secrets: https://modal.com/docs/guide/secrets
- Modal deploy command: https://modal.com/docs/reference/cli/deploy

## Setup Order

Follow this order:

1. Create Google API credentials.
2. Create your `.env` file.
3. Create the Google Sheet.
4. Add your OpenRouter API key.
5. Deploy to Modal.
6. Run a test report.

## 1. Create Google API Credentials

The app needs permission to use Gmail and Google Sheets. The most confusing value is the refresh token. A refresh token lets this workflow keep using Gmail and Sheets without you logging in every time it runs.

### Create Or Select A Google Cloud Project

1. Go to https://console.cloud.google.com/.
2. Create a new project, or select an existing project.
3. Use the same Google account that owns the sender Gmail inbox, or an account that can safely authorize that inbox.

### Turn On The APIs

In Google Cloud, enable these APIs:

- Gmail API
- Google Sheets API

You can find them by searching for each API name in Google Cloud's API Library.

### Configure The OAuth Consent Screen

1. Go to `APIs & Services` -> `OAuth consent screen`.
2. Choose the user type that fits your account. For a private/internal setup, use the simplest private/testing option Google allows for your account.
3. Add your own Google account as a test user if Google asks for test users.
4. Save the consent screen.

### Create OAuth Client Credentials

1. Go to `APIs & Services` -> `Credentials`.
2. Click `Create Credentials`.
3. Choose `OAuth client ID`.
4. Choose a desktop app or web app client type.
5. Copy the client id and client secret into `.env` later:

```text
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
```

### Get The Google Refresh Token

Use the OAuth client credentials to authorize the Gmail sender account and get a refresh token.

The easiest way is Google's OAuth 2.0 Playground:

1. Open https://developers.google.com/oauthplayground/.
2. Click the gear icon in the top right.
3. Check `Use your own OAuth credentials`.
4. Paste your `GOOGLE_CLIENT_ID` into `OAuth Client ID`.
5. Paste your `GOOGLE_CLIENT_SECRET` into `OAuth Client secret`.
6. Close the settings panel.
7. In the scopes box on the left, paste the scopes below.
8. Click `Authorize APIs`.
9. Sign in as the Gmail sender account.
10. Approve the requested access.
11. Click `Exchange authorization code for tokens`.
12. Copy the `refresh_token` value.

Copy these scopes exactly when generating the token:

```text
https://www.googleapis.com/auth/gmail.send
https://www.googleapis.com/auth/gmail.modify
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/spreadsheets
```

What the scopes mean:

- `gmail.send`: send report emails.
- `gmail.modify`: mark processed replies and manage Gmail message state.
- `gmail.readonly`: read replies in report threads.
- `spreadsheets`: read and update the Google Sheet.

After you finish the OAuth flow, paste the refresh token into `.env`:

```text
GOOGLE_REFRESH_TOKEN=
```

Important: do not share or print the refresh token. Treat it like a password.

## 2. Create `.env`

Copy `.env.example` to a new file named `.env`, then fill in the values.

```powershell
Copy-Item .env.example .env
```

Your `.env` should contain:

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

What each value means:

- `GOOGLE_CLIENT_ID`: from the Google OAuth client.
- `GOOGLE_CLIENT_SECRET`: from the Google OAuth client.
- `GOOGLE_REFRESH_TOKEN`: lets the app keep using Gmail and Sheets.
- `GOOGLE_SHEETS_SPREADSHEET_ID`: the long id from the Google Sheet URL.
- `SYSTEM_SENDER_EMAIL`: the Gmail address that sends reports.
- `OPENROUTER_API_KEY`: lets OpenRouter write the report content.
- `OPENROUTER_MODEL`: the model used for reports. Keep the default unless you intentionally change models.
- `OPENROUTER_BASE_URL`: OpenRouter API URL. Keep the default.

Do not commit `.env` or paste it into chat.

## 3. Create The Google Sheet

Create a Google Sheet that the app can read and update.

After creating it, copy the spreadsheet id into `.env`:

```text
GOOGLE_SHEETS_SPREADSHEET_ID=
```

The spreadsheet id is the long part of the Sheet URL:

```text
https://docs.google.com/spreadsheets/d/SPREADSHEET_ID_IS_HERE/edit
```

### Option A: Easiest Setup With An IDE Agent

After `.env` is filled in, paste this prompt into Codex, Claude Code, Cursor, or another IDE agent inside this project folder:

```text
Using the credentials in `.env`, create or initialize the Google Sheet for Agency's ReportOps with the required tabs and headers. Do not read `.env.local`. Do not print any secret values. Use the existing Python Google Sheets client in this repo if possible.
```

The agent should create the required tabs and headers for you.

### Option B: Manual Sheet Setup

Create these exact tabs in the Google Sheet:

- `Clients`
- `Metrics`
- `Runs`
- `Messages`
- `Questions`

Paste these headers into row 1 of each tab.

#### `Clients`

```text
client_id, client_name, contact_name, contact_email, account_manager_email, cadence, next_report_date, status, run_now, paused, notes
```

Important columns:

- `client_id`: a stable unique name, like `client_brightsmile_dental`.
- `client_name`: the client name shown in the report.
- `contact_email`: the client's email address.
- `account_manager_email`: the person who approves the report before the client sees it.
- `next_report_date`: the next date this client should be checked, like `2026-06-08`.
- `run_now`: type `TRUE` to force a report on the next run. The app clears this after the approved report is sent to the client.
- `paused`: type `TRUE` to stop reports for that client.

Example row:

```text
client_brightsmile_dental, BrightSmile Dental, Ava, ava@example.com, am@example.com, monthly, 2026-06-08, active, TRUE, , Demo client
```

#### `Metrics`

```text
client_id, client_name, month, ad_spend, impressions, clicks, ctr, leads, cpl, conversions, conversion_rate, revenue, roas
```

Important columns:

- `client_id`: must match the `Clients` tab.
- `month`: the report month. Recommended format is `Feb-2026`.
- Metric columns: use numbers only where possible.

The app can read common month formats like `Feb-2026`, `Feb 2026`, `2026-02`, and `2/1/2026`, but `Feb-2026` is the easiest format to maintain.

If a report month has no matching metric rows, no email will be sent. The run is marked blocked with `No metrics found`.

Example row:

```text
client_brightsmile_dental, BrightSmile Dental, Feb-2026, 3450, 131000, 3290, 2.51, 91, 37.91, 31, 0.94, 20100, 5.83
```

#### `Runs`

```text
run_id, client_id, period, status, attempt_count, last_error, am_review_notes, html_report, gmail_thread_id, client_thread_id, created_at, updated_at, approved_at, delivered_at, last_am_review_sent_at
```

You normally do not fill this tab manually. The app writes report run state here.

Useful columns:

- `status`: where the report is in the workflow.
- `last_error`: why a run was blocked or failed.
- `html_report`: the generated report body.
- `gmail_thread_id`: the account-manager review email thread.
- `client_thread_id`: the client email thread.
- `last_am_review_sent_at`: the last time the app sent the first AM review or a follow-up reminder.

#### `Messages`

```text
message_id, run_id, type, to, subject, gmail_message_id, gmail_thread_id, status, created_at
```

You normally do not fill this tab manually. The app writes sent messages and processed replies here.

This tab helps prevent duplicate reply processing.

#### `Questions`

```text
question_id, run_id, client_id, question, risk_level, answer_html, status, created_at, sent_at
```

You normally do not fill this tab manually. The app writes client questions and AI-drafted answers here.

Low-risk questions can be answered automatically. High-risk questions go to the account manager first.

## 4. Add OpenRouter

OpenRouter writes the report content.

1. Go to https://openrouter.ai/.
2. Sign up or log in.
3. Go to your API keys page.
4. Create an API key.
5. Paste it into `.env`:

```text
OPENROUTER_API_KEY=
```

Keep these defaults unless you intentionally want to change models:

```text
OPENROUTER_MODEL=openai/gpt-oss-120b:free
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
```

If OpenRouter is unavailable, rate-limited, or returns invalid report data, the app blocks the run and does not send the report.

## 5. Deploy To Modal

Modal runs the workflow on a schedule in the cloud.

### Install Modal

```powershell
python -m pip install modal
```

### Log In To Modal

```powershell
modal setup
```

Follow the browser login flow.

### Create Or Update The Modal Secret

The app expects a Modal secret named `reportops-secrets`.

The easiest option is to create it from `.env`:

```powershell
modal secret create reportops-secrets --from-dotenv .env --force
```

If your Modal CLI version does not support `--from-dotenv`, create it by passing the values manually:

```powershell
modal secret create reportops-secrets GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... GOOGLE_REFRESH_TOKEN=... GOOGLE_SHEETS_SPREADSHEET_ID=... SYSTEM_SENDER_EMAIL=... OPENROUTER_API_KEY=... OPENROUTER_MODEL=openai/gpt-oss-120b:free OPENROUTER_BASE_URL=https://openrouter.ai/api/v1 --force
```

### Deploy The App

```powershell
$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'; py -3.13 -m modal deploy modal_app.py
```

## 6. Run A Test Report

Run all due/manual clients:

```powershell
py -3.13 -m modal run modal_app.py::run_now
```

Run a specific client:

```powershell
py -3.13 -m modal run modal_app.py::run_now --client-id client_brightsmile_dental
```

Run a specific report period:

```powershell
py -3.13 -m modal run modal_app.py::run_now --period Feb-2026
```

Run a specific client and report period:

```powershell
py -3.13 -m modal run modal_app.py::run_now --client-id client_brightsmile_dental --period Feb-2026
```

Important: if you do not pass `--period`, the app uses the previous month. For example, a run in June looks for `May-2026` metrics. If there are no matching metric rows, no email is sent.

## How The Schedule Works

After deployment:

- Daily reports run at `0 0 * * *` UTC.
- Gmail replies are checked every five minutes during UTC hours `09:00` through `18:59`.
- `run_now` is for forcing manual runs. If it is left as `TRUE`, the next scheduled run can also pick it up.

The account manager must reply `Approved` before the client gets the report.

After the client report is sent, the app clears `run_now` and advances `next_report_date` by one month.

If a daily run finds that a same-period report is already waiting for account-manager review, it does not regenerate the report and does not call OpenRouter again. If the account manager has not received an AM review or follow-up in more than 24 hours, the app sends a short follow-up in the same Gmail thread.

This duplicate protection is only for the same report period. For example, an open `Feb-2026` AM review does not block a later `Mar-2026` report.

Manual `run_now --client-id ... --period ...` is an explicit retry and can regenerate/resend that same-period AM review.

## Optional Developer Checks

These are useful if a developer is changing the code.

Run tests:

```powershell
python -m unittest discover -s tests_py -v
```

Run a compile check:

```powershell
python -m compileall -q reportops modal_app.py
```

## Common Problems

### I Did Not Receive An Email

Check these first:

- The report period has matching rows in `Metrics`.
- The client is due, or `run_now` is set to `TRUE`.
- `account_manager_email` is correct.
- `SYSTEM_SENDER_EMAIL` is the Gmail account that was authorized.
- The run was not blocked in the `Runs` tab.

### `No metrics found`

Add rows to the `Metrics` tab for that client and report month.

Example: if the run is for `May-2026`, the client needs a `Metrics` row with `month` set to `May-2026`.

### Report Is Waiting

The account-manager review email was sent, but the client will not receive the report until the account manager replies with `Approved`.

If the report is still waiting during the next daily run and the last AM review or reminder is more than 24 hours old, the app sends the account manager a follow-up. It does not create a new report for the same period unless you run an explicit manual retry.

### Google Auth Failed

Usually this means:

- `GOOGLE_CLIENT_ID` is wrong.
- `GOOGLE_CLIENT_SECRET` is wrong.
- `GOOGLE_REFRESH_TOKEN` is missing or expired.
- The refresh token was created without the required Gmail and Sheets scopes.
- The sender Gmail account is different from the account that authorized the token.

### OpenRouter Failed

Check:

- `OPENROUTER_API_KEY` is filled in.
- The OpenRouter account can use the selected model.
- The model was not temporarily rate-limited or unavailable.

If OpenRouter fails or returns invalid structured output, the app blocks the run and does not send the report.

### Sheet Headers Do Not Match

The tab names and row 1 headers must match this README exactly. A missing or renamed header can stop data from loading correctly.

## Final Setup Checklist

Before relying on the scheduled workflow, confirm:

- `.env` is filled in.
- Google APIs are enabled.
- The refresh token was created with the required scopes.
- The Google Sheet has all five tabs.
- At least one client row exists.
- Metrics exist for the report month you want to test.
- OpenRouter API key is filled in.
- Modal secret `reportops-secrets` exists.
- The app has been deployed.
- A manual test run has been completed.
