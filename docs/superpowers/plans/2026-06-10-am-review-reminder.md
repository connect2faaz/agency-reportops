# AM Review Reminder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop scheduled daily runs from regenerating same-period AM review reports while sending once-per-day AM follow-up reminders.

**Architecture:** Store the latest AM review/reminder timestamp on each `Run` as `last_am_review_sent_at`. Scheduled runs will suppress same-period active runs and optionally send a follow-up, while manual client-specific retries will continue to force regeneration.

**Tech Stack:** Python dataclasses, Google Sheets-backed store, unittest, Modal entrypoints.

---

## File Structure

- Modify `reportops/models.py`: add `Run.last_am_review_sent_at` and timestamp parsing helpers.
- Modify `reportops/sheets.py`: serialize and deserialize `last_am_review_sent_at` in the `Runs` tab.
- Modify `reportops/workflow.py`: add scheduled-run suppression, reminder sending, and forced manual retry path.
- Modify `modal_app.py`: call the forced manual retry path only for client-specific manual runs.
- Modify `tests_py/test_headless_workflow.py`: cover suppression, reminder timing, different-period behavior, and manual force behavior.
- Modify `tests_py/test_adapters.py`: cover Sheets persistence for the new run timestamp.
- Run `python -m unittest discover -s tests_py -v` and `python -m compileall -q reportops modal_app.py`.

### Task 1: Persist `last_am_review_sent_at`

**Files:**
- Modify: `reportops/models.py`
- Modify: `reportops/sheets.py`
- Test: `tests_py/test_adapters.py`

- [ ] **Step 1: Write the failing adapter test**

Add a test in `tests_py/test_adapters.py` that creates a fake `Runs` row with:

```python
"last_am_review_sent_at": "2026-06-09T00:00:00+00:00"
```

Assert:

```python
self.assertEqual(store.runs[0].last_am_review_sent_at, datetime(2026, 6, 9, tzinfo=timezone.utc))
```

Then call `store.flush()` and assert the fake sheet client's written `Runs` row contains the same ISO timestamp under `last_am_review_sent_at`.

- [ ] **Step 2: Run the adapter test to verify it fails**

Run:

```powershell
python -m unittest tests_py.test_adapters -v
```

Expected: fail because `Run` has no `last_am_review_sent_at` field and the Sheets adapter does not read or write the column.

- [ ] **Step 3: Implement timestamp parsing and model field**

In `reportops/models.py`, add:

```python
def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00"))
```

Add this field to `Run`:

```python
last_am_review_sent_at: datetime | None = None
```

- [ ] **Step 4: Implement Sheets persistence**

In `reportops/sheets.py`, import `parse_datetime` from `reportops.models`.

Add `"last_am_review_sent_at"` to the `Runs` header list after `"delivered_at"`.

In `_run_from_row()`, pass:

```python
last_am_review_sent_at=parse_datetime(row.get("last_am_review_sent_at")),
```

The existing `_run_to_row()` will serialize the dataclass field with `_format_value()`.

- [ ] **Step 5: Run the adapter test to verify it passes**

Run:

```powershell
python -m unittest tests_py.test_adapters -v
```

Expected: pass.

### Task 2: Suppress Scheduled Same-Period Regeneration and Send Reminders

**Files:**
- Modify: `reportops/workflow.py`
- Test: `tests_py/test_headless_workflow.py`

- [ ] **Step 1: Add failing workflow tests**

Add tests that cover:

```python
def test_scheduled_due_run_does_not_regenerate_existing_same_period_am_review(self):
    # Existing run: client_1, Feb-2026, AM_REVIEW, html_report="<h1>Old</h1>",
    # gmail_thread_id="thread_1", last_am_review_sent_at=2026-06-09T18:00Z.
    # Run scheduled workflow at 2026-06-10T00:00Z for Feb-2026.
    # Assert AI.generate_report was not called.
    # Assert no Gmail message was sent.
    # Assert html_report remains "<h1>Old</h1>".
```

```python
def test_scheduled_due_run_sends_am_follow_up_after_one_day(self):
    # Existing AM_REVIEW run with last_am_review_sent_at=2026-06-08T23:00Z.
    # Run scheduled workflow at 2026-06-10T00:00Z.
    # Assert one Gmail message was sent to account_manager_email in thread_1.
    # Assert message body includes "Following up on this report review".
    # Assert body includes hidden "Reference: run:run_1".
    # Assert run.last_am_review_sent_at == 2026-06-10T00:00Z.
```

```python
def test_open_am_review_for_prior_period_does_not_block_new_period(self):
    # Existing AM_REVIEW run for Feb-2026.
    # Metrics exist for Feb-2026 and Mar-2026.
    # Run scheduled workflow for Mar-2026.
    # Assert a second run exists for Mar-2026 and AI was called.
```

- [ ] **Step 2: Run workflow tests to verify they fail**

Run:

```powershell
python -m unittest tests_py.test_headless_workflow -v
```

Expected: fail because existing same-period `am_review` runs still regenerate and no follow-up behavior exists.

- [ ] **Step 3: Add scheduled-run mode**

Change `ReportingWorkflow.run_due_reports()` to call:

```python
self._start_or_retry_report(client, period or self._default_period(today), force=False)
```

Change `run_client_report()` to call:

```python
self._start_or_retry_report(client, period or self._default_period(today), force=True)
```

Update `_start_or_retry_report()` signature:

```python
def _start_or_retry_report(self, client: Client, period: str, force: bool = False) -> None:
```

- [ ] **Step 4: Add suppression branch**

At the start of `_start_or_retry_report()`, after finding `run = self.store.active_run_for(...)`, add:

```python
if run is not None and run.status == RunStatus.AM_REVIEW and not force:
    self._send_am_follow_up_if_due(client, run)
    return
```

Keep existing generation behavior for new runs and forced manual runs.

- [ ] **Step 5: Add follow-up helpers**

Add:

```python
AM_FOLLOW_UP_INTERVAL = timedelta(hours=24)
```

Add method:

```python
def _send_am_follow_up_if_due(self, client: Client, run: Run) -> None:
    now = utc_now()
    if run.last_am_review_sent_at is not None and now - run.last_am_review_sent_at < AM_FOLLOW_UP_INTERVAL:
        return
    self._send_am_follow_up(client, run, now)
```

Add method:

```python
def _send_am_follow_up(self, client: Client, run: Run, sent_at: datetime) -> None:
    body = (
        "<p>Following up on this report review. Please reply Approved to send, "
        "or reply with requested changes.</p>"
        f"{self._hidden_reference(f'run:{run.run_id}')}"
    )
    sent = self.gmail.send_html(
        to=client.account_manager_email,
        subject=f"Re: {client.client_name} report ready for review",
        html_body=body,
        headers={
            "X-ReportOps-Run-Id": run.run_id,
            "X-ReportOps-Message-Type": "account_manager_follow_up",
        },
        thread_id=run.gmail_thread_id,
    )
    run.last_am_review_sent_at = sent_at
    run.updated_at = sent_at
    self.store.save_message(
        MessageRecord(
            message_id=new_id("message"),
            run_id=run.run_id,
            message_type="account_manager_follow_up",
            to=client.account_manager_email,
            subject=f"Re: {client.client_name} report ready for review",
            gmail_message_id=sent["id"],
            gmail_thread_id=sent["thread_id"],
            status="sent",
        )
    )
```

- [ ] **Step 6: Update initial AM review timestamp**

In `_send_am_review()`, set:

```python
sent_at = utc_now()
run.last_am_review_sent_at = sent_at
run.updated_at = sent_at
```

after Gmail sends successfully and before saving the message.

- [ ] **Step 7: Run workflow tests**

Run:

```powershell
python -m unittest tests_py.test_headless_workflow -v
```

Expected: pass.

### Task 3: Verify Manual Override and Full Suite

**Files:**
- Modify: `tests_py/test_modal_entrypoints.py` if existing coverage needs adjustment.
- Verify: all project tests.

- [ ] **Step 1: Add or update manual override test**

In `tests_py/test_headless_workflow.py`, add:

```python
def test_manual_client_run_regenerates_existing_same_period_am_review(self):
    # Existing AM_REVIEW run for Feb-2026 with html_report="<h1>Old</h1>".
    # Metrics exist for Feb-2026.
    # Call workflow.run_client_report("client_1", today=date(2026, 6, 10), period="Feb-2026").
    # Assert AI.generate_report was called.
    # Assert one AM review email was sent.
    # Assert run.html_report changed from the old value.
```

- [ ] **Step 2: Run targeted tests**

Run:

```powershell
python -m unittest tests_py.test_headless_workflow tests_py.test_adapters tests_py.test_modal_entrypoints -v
```

Expected: pass.

- [ ] **Step 3: Run full verification**

Run:

```powershell
python -m unittest discover -s tests_py -v
python -m compileall -q reportops modal_app.py
```

Expected: both commands pass.

- [ ] **Step 4: Commit implementation**

Run:

```powershell
git status --short
git add reportops/models.py reportops/sheets.py reportops/workflow.py modal_app.py tests_py/test_headless_workflow.py tests_py/test_adapters.py docs/superpowers/plans/2026-06-10-am-review-reminder.md
git commit -m "Add AM review reminder workflow"
```

Expected: commit succeeds with only the planned files.

