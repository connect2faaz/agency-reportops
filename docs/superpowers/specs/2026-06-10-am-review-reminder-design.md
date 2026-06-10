# AM Review Reminder Design

## Goal

Prevent the daily scheduled workflow from regenerating and resending the same client report while a same-period account-manager review is already open, while still reminding the account manager when they have not responded after roughly one day.

## Current Behavior

The daily Modal job calls `run_due_reports()`, which selects clients whose `run_now` flag is true or whose `next_report_date` is on or before the current date. For each selected client, `_start_or_retry_report()` looks for an active run for the same `client_id` and period. If it finds an `am_review` run, it reuses the run but still increments the attempt count, calls OpenRouter again, replaces the report HTML, and sends another account-manager review email.

This means a due client can receive a new AM review email every daily run until the report is approved and delivered to the client. The schedule is only advanced after client delivery.

## Desired Behavior

For scheduled daily runs, an existing same-period `am_review` run should suppress report regeneration. The workflow should keep the existing generated report and existing run. It should send a lightweight follow-up to the account manager only when the latest AM review or reminder was sent more than 24 hours ago.

The suppression is scoped to the same reporting period. For example, an open `Feb-2026` AM review for a client should not prevent a future `Mar-2026` report from being generated when that client becomes due for March.

## Run State

Add an explicit timestamp field to `Run`:

```text
last_am_review_sent_at
```

This timestamp is updated when the workflow sends the initial AM review email and when it sends an AM follow-up reminder. Keeping this value on the run avoids relying on message timestamps for reminder timing.

The Google Sheets store should serialize and deserialize this field in the `Runs` tab.

## Scheduled Daily Flow

When a client is due for a period:

1. If no active run exists for `client_id + period`, generate the report and send the first AM review.
2. If an active same-period run exists with status `am_review`, do not call OpenRouter and do not replace `html_report`.
3. If that `am_review` run has no `last_am_review_sent_at`, treat it as eligible for a follow-up and set the timestamp after sending.
4. If `last_am_review_sent_at` is older than 24 hours, send a follow-up email in the existing AM Gmail thread.
5. If less than 24 hours have passed, do nothing.
6. If the same-period run is `blocked` or already `client_delivered`, do not regenerate it from the scheduled daily job.

## Follow-Up Email

The follow-up should be sent to the client's `account_manager_email` in `run.gmail_thread_id`.

Subject:

```text
Re: {client_name} report ready for review
```

Body:

```html
<p>Following up on this report review. Please reply Approved to send, or reply with requested changes.</p>
```

The email must include the hidden `run:{run_id}` reference marker so existing reply matching remains strict.

The follow-up should be recorded in the `Messages` tab with a distinct message type such as:

```text
account_manager_follow_up
```

## Manual Retry Behavior

Manual `run_now --client-id X --period P` should remain an explicit override for that client and period. It may regenerate and resend the AM review even if a same-period `am_review` run already exists.

The scheduled daily job should not use that override behavior.

Manual `run_now` without a client ID currently sets every client's `run_now` flag to true. That behavior can remain unchanged for now, but it should still be understood as an explicit manual bulk run rather than the normal daily schedule.

## Error Handling

Follow-up reminders should not call OpenRouter. If Gmail sending fails, the Modal function should fail normally so the problem is visible in logs. It should not advance the client's schedule.

Blocked runs should remain blocked until an explicit manual retry or a later design changes blocked-run handling.

## Tests

Add or update tests for:

1. A due client with no existing run generates an AM review as today.
2. A due client with an existing same-period `am_review` run does not call AI again.
3. A due client with an existing same-period `am_review` run sends a follow-up when the last AM review or reminder is older than 24 hours.
4. The same case does not send a follow-up when less than 24 hours have passed.
5. An open `Feb-2026` `am_review` run does not block generating a `Mar-2026` report.
6. Manual `run_now --client-id X --period P` can still explicitly regenerate and resend for that same period.
7. `last_am_review_sent_at` is persisted to and restored from the `Runs` sheet.

