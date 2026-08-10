from datetime import date, timedelta
import unittest

from reportops.ai import OpenRouterClient, StructuredOutputError
from reportops.models import Client, MetricRow, RunStatus, utc_now
from reportops.workflow import (
    BLOCK_REASON_AI_FAILURE,
    BLOCK_REASON_MISSING_METRICS,
    BLOCKED_MAX_ATTEMPTS,
    SUPPORT_NOTICE_ATTEMPT_LIMIT,
    InMemorySheetStore,
    ReportingWorkflow,
)


class CountingReportAI:
    """Succeeds, but records how many times OpenRouter would actually have been called."""

    def __init__(self) -> None:
        self.calls = 0

    def generate_report(self, client, metrics, review_notes):
        self.calls += 1
        return OpenRouterClient.fake_report().generate_report(client, metrics, review_notes)

    def draft_question_answer(self, client, question, report_html):
        return OpenRouterClient.fake_report().draft_question_answer(client, question, report_html)


class FailingReportAI:
    def __init__(self) -> None:
        self.calls = 0

    def generate_report(self, client, metrics, review_notes):
        self.calls += 1
        raise StructuredOutputError("Invalid structured output after retries.")

    def draft_question_answer(self, client, question, report_html):
        raise StructuredOutputError("Invalid structured output after retries.")


def build_client(**overrides):
    values = dict(
        client_id="client_1",
        client_name="BrightSmile Dental",
        contact_name="Ava",
        contact_email="ava@example.com",
        account_manager_email="am@example.com",
        support_email="support@example.com",
        cadence="monthly",
        next_report_date=date(2026, 8, 8),
    )
    values.update(overrides)
    return Client(**values)


def build_metric(month):
    return MetricRow(
        client_id="client_1",
        client_name="BrightSmile Dental",
        month=month,
        ad_spend=3450,
        impressions=131000,
        clicks=3290,
        ctr=2.51,
        leads=91,
        cpl=37.91,
        conversions=31,
        conversion_rate=0.94,
        revenue=20100,
        roas=5.83,
    )


def age_run(run, hours):
    """Pretend the last attempt happened `hours` ago so the retry interval has elapsed."""
    run.updated_at = utc_now() - timedelta(hours=hours)


def support_emails(store):
    return [
        message
        for message in store.gmail.sent_messages
        if message["subject"].startswith("ReportOps blocked:")
    ]


class BlockedRunRetryTests(unittest.TestCase):
    def test_missing_metrics_block_is_retried_by_schedule_and_recovers(self):
        store = InMemorySheetStore(clients=[build_client()], metrics=[])
        ai = CountingReportAI()
        workflow = ReportingWorkflow(store=store, ai=ai, gmail=store.gmail)

        workflow.run_due_reports(today=date(2026, 8, 8), period="Jul-2026")
        run = store.runs[0]
        self.assertEqual(run.status, RunStatus.BLOCKED)
        self.assertEqual(run.block_reason, BLOCK_REASON_MISSING_METRICS)
        self.assertEqual(ai.calls, 0)

        # Metrics arrive later; the daily schedule must pick them up without a manual run_now.
        store.metrics.extend([build_metric("Jun-2026"), build_metric("Jul-2026")])
        age_run(run, hours=24)
        workflow.run_due_reports(today=date(2026, 8, 9), period="Jul-2026")

        self.assertEqual(run.status, RunStatus.AM_REVIEW)
        self.assertEqual(run.last_error, "")
        self.assertEqual(run.block_reason, "")
        self.assertFalse(run.retry_abandoned)
        self.assertEqual(ai.calls, 1)
        self.assertEqual(len(store.runs), 1, "retry must reuse the existing run row")

    def test_missing_metrics_retry_costs_no_openrouter_calls_while_blocked(self):
        store = InMemorySheetStore(clients=[build_client()], metrics=[])
        ai = CountingReportAI()
        workflow = ReportingWorkflow(store=store, ai=ai, gmail=store.gmail)

        for day in range(1, 8):
            workflow.run_due_reports(today=date(2026, 8, 7 + day), period="Jul-2026")
            age_run(store.runs[0], hours=24)

        self.assertEqual(ai.calls, 0)
        self.assertEqual(store.runs[0].status, RunStatus.BLOCKED)
        self.assertEqual(store.runs[0].attempt_count, 7)

    def test_blocked_run_is_not_retried_twice_inside_the_interval(self):
        store = InMemorySheetStore(clients=[build_client()], metrics=[])
        workflow = ReportingWorkflow(store=store, ai=CountingReportAI(), gmail=store.gmail)

        workflow.run_due_reports(today=date(2026, 8, 8), period="Jul-2026")
        workflow.run_due_reports(today=date(2026, 8, 8), period="Jul-2026")
        workflow.run_due_reports(today=date(2026, 8, 8), period="Jul-2026")

        self.assertEqual(store.runs[0].attempt_count, 1)
        self.assertEqual(len(support_emails(store)), 1)

    def test_support_notices_stop_while_retries_continue_silently(self):
        store = InMemorySheetStore(clients=[build_client()], metrics=[])
        workflow = ReportingWorkflow(store=store, ai=CountingReportAI(), gmail=store.gmail)

        for _ in range(SUPPORT_NOTICE_ATTEMPT_LIMIT + 6):
            workflow.run_due_reports(today=date(2026, 8, 8), period="Jul-2026")
            age_run(store.runs[0], hours=24)

        run = store.runs[0]
        self.assertEqual(run.attempt_count, SUPPORT_NOTICE_ATTEMPT_LIMIT + 6)
        # Notices land on attempts 1, 4, 7, 10, 13 only.
        self.assertEqual(len(support_emails(store)), 5)
        self.assertEqual(run.status, RunStatus.BLOCKED)
        self.assertFalse(run.retry_abandoned)

    def test_retries_are_abandoned_after_budget_with_one_final_notice(self):
        store = InMemorySheetStore(clients=[build_client()], metrics=[])
        workflow = ReportingWorkflow(store=store, ai=CountingReportAI(), gmail=store.gmail)
        budget = BLOCKED_MAX_ATTEMPTS[BLOCK_REASON_MISSING_METRICS]

        for _ in range(budget + 10):
            workflow.run_due_reports(today=date(2026, 8, 8), period="Jul-2026")
            age_run(store.runs[0], hours=24)

        run = store.runs[0]
        self.assertTrue(run.retry_abandoned)
        self.assertEqual(run.attempt_count, budget, "attempts must stop at the budget")
        notices = support_emails(store)
        self.assertEqual(len(notices), 6, "5 reminders plus one final give-up notice")
        self.assertIn("Automatic retries stopped", notices[-1]["body"])

    def test_ai_failure_gets_a_small_budget_and_stops(self):
        store = InMemorySheetStore(
            clients=[build_client()], metrics=[build_metric("Jun-2026"), build_metric("Jul-2026")]
        )
        ai = FailingReportAI()
        workflow = ReportingWorkflow(store=store, ai=ai, gmail=store.gmail)
        budget = BLOCKED_MAX_ATTEMPTS[BLOCK_REASON_AI_FAILURE]

        for _ in range(budget + 5):
            workflow.run_due_reports(today=date(2026, 8, 8), period="Jul-2026")
            age_run(store.runs[0], hours=24)

        run = store.runs[0]
        self.assertEqual(run.block_reason, BLOCK_REASON_AI_FAILURE)
        self.assertTrue(run.retry_abandoned)
        self.assertEqual(ai.calls, budget, "AI failures must not retry forever")

    def test_legacy_blocked_row_without_block_reason_is_still_retried(self):
        store = InMemorySheetStore(clients=[build_client()], metrics=[])
        workflow = ReportingWorkflow(store=store, ai=CountingReportAI(), gmail=store.gmail)
        workflow.run_due_reports(today=date(2026, 8, 8), period="Jul-2026")

        # Simulate a row written before block_reason existed.
        run = store.runs[0]
        run.block_reason = ""
        store.metrics.extend([build_metric("Jun-2026"), build_metric("Jul-2026")])
        age_run(run, hours=24)

        workflow.run_due_reports(today=date(2026, 8, 9), period="Jul-2026")

        self.assertEqual(run.status, RunStatus.AM_REVIEW)

    def test_abandoned_run_stays_silent_but_manual_run_now_still_works(self):
        store = InMemorySheetStore(clients=[build_client()], metrics=[])
        workflow = ReportingWorkflow(store=store, ai=CountingReportAI(), gmail=store.gmail)
        run = None
        for _ in range(BLOCKED_MAX_ATTEMPTS[BLOCK_REASON_MISSING_METRICS] + 3):
            workflow.run_due_reports(today=date(2026, 8, 8), period="Jul-2026")
            run = store.runs[0]
            age_run(run, hours=24)
        self.assertTrue(run.retry_abandoned)

        notices_before = len(support_emails(store))
        workflow.run_due_reports(today=date(2026, 8, 9), period="Jul-2026")
        self.assertEqual(len(support_emails(store)), notices_before, "abandoned runs stay silent")

        # The operator fixes the data and forces a run: it must work regardless of abandonment.
        store.metrics.extend([build_metric("Jun-2026"), build_metric("Jul-2026")])
        workflow.run_client_report("client_1", today=date(2026, 8, 9), period="Jul-2026")

        self.assertEqual(run.status, RunStatus.AM_REVIEW)
        self.assertFalse(run.retry_abandoned)

    def test_paused_client_stops_retries_and_notices_immediately(self):
        client = build_client()
        store = InMemorySheetStore(clients=[client], metrics=[])
        workflow = ReportingWorkflow(store=store, ai=CountingReportAI(), gmail=store.gmail)
        workflow.run_due_reports(today=date(2026, 8, 8), period="Jul-2026")
        notices_before = len(support_emails(store))

        client.paused = True
        for _ in range(5):
            age_run(store.runs[0], hours=24)
            workflow.run_due_reports(today=date(2026, 8, 9), period="Jul-2026")

        self.assertEqual(store.runs[0].attempt_count, 1)
        self.assertEqual(len(support_emails(store)), notices_before)

    def test_open_am_review_run_is_still_not_regenerated(self):
        """The existing guardrail must survive the blocked-retry change."""
        store = InMemorySheetStore(
            clients=[build_client()], metrics=[build_metric("Jun-2026"), build_metric("Jul-2026")]
        )
        ai = CountingReportAI()
        workflow = ReportingWorkflow(store=store, ai=ai, gmail=store.gmail)

        workflow.run_due_reports(today=date(2026, 8, 8), period="Jul-2026")
        self.assertEqual(store.runs[0].status, RunStatus.AM_REVIEW)
        self.assertEqual(ai.calls, 1)

        workflow.run_due_reports(today=date(2026, 8, 9), period="Jul-2026")
        self.assertEqual(ai.calls, 1, "an open AM review must not trigger regeneration")


if __name__ == "__main__":
    unittest.main()
