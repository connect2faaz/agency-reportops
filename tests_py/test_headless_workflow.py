from datetime import date, datetime, timezone
from email import message_from_string
import unittest
from unittest.mock import patch

from reportops.ai import OpenRouterClient, StructuredOutputError
from reportops.gmail import (
    GmailInboundMessage,
    build_raw_email,
    classify_am_reply,
    classify_client_question_risk,
    extract_run_id_from_reply,
)
from reportops.models import Client, MessageRecord, MetricRow, Question, QuestionAnswerOutput, Run, RunStatus
from reportops.workflow import InMemorySheetStore, ReportingWorkflow, select_due_clients


class CapturingReportAI:
    def __init__(self) -> None:
        self.report_client = None
        self.report_metrics = []
        self.report_review_notes = []

    def generate_report(self, client, metrics, review_notes):
        self.report_client = client
        self.report_metrics = list(metrics)
        self.report_review_notes = list(review_notes)
        return OpenRouterClient.fake_report().generate_report(client, metrics, review_notes)

    def draft_question_answer(self, client, question, report_html):
        return OpenRouterClient.fake_report().draft_question_answer(client, question, report_html)


class HighRiskQuestionAI:
    def generate_report(self, client, metrics, review_notes):
        return OpenRouterClient.fake_report().generate_report(client, metrics, review_notes)

    def draft_question_answer(self, client, question, report_html):
        return QuestionAnswerOutput(
            intent="question",
            risk_level="high",
            risk_reason="The answer requires account-manager judgment.",
            answer_html="<p>This needs account-manager review before sending.</p>",
            requires_am_review=True,
        )


class HighRiskDefinitionQuestionAI(HighRiskQuestionAI):
    def draft_question_answer(self, client, question, report_html):
        return QuestionAnswerOutput(
            intent="question",
            risk_level="high",
            risk_reason="The model treated this definition question as unclear.",
            answer_html="<p>This needs account-manager review before sending.</p>",
            requires_am_review=True,
        )


class FailingReportAI:
    def generate_report(self, client, metrics, review_notes):
        raise AssertionError("AI should not be called without metrics")

    def draft_question_answer(self, client, question, report_html):
        raise AssertionError("Question drafting is not expected in this test")


class CountingReportAI:
    def __init__(self) -> None:
        self.report_call_count = 0

    def generate_report(self, client, metrics, review_notes):
        self.report_call_count += 1
        return OpenRouterClient.fake_report().generate_report(client, metrics, review_notes)

    def draft_question_answer(self, client, question, report_html):
        return OpenRouterClient.fake_report().draft_question_answer(client, question, report_html)


class NonJsonOpenRouterAI:
    def __init__(self) -> None:
        self.client = OpenRouterClient(api_key="fake", http_post=self._post)

    def _post(self, url, payload, headers):
        raise StructuredOutputError("OpenRouter returned non-JSON response: Expecting value")

    def generate_report(self, client, metrics, review_notes):
        return self.client.generate_report(client, metrics, review_notes)

    def draft_question_answer(self, client, question, report_html):
        raise AssertionError("Question drafting is not expected in this test")


class OneClientCrashingAI:
    def generate_report(self, client, metrics, review_notes):
        if client.client_id == "client_a":
            raise RuntimeError("temporary upstream failure")
        return OpenRouterClient.fake_report().generate_report(client, metrics, review_notes)

    def draft_question_answer(self, client, question, report_html):
        raise AssertionError("Question drafting is not expected in this test")


class HeadlessWorkflowTests(unittest.TestCase):
    def test_select_due_clients_uses_utc_dates_and_manual_controls(self):
        clients = [
            Client(
                client_id="client_due",
                client_name="Due Client",
                contact_name="Dana",
                contact_email="dana@example.com",
                account_manager_email="am@example.com",
                cadence="monthly",
                next_report_date=date(2026, 6, 8),
            ),
            Client(
                client_id="client_future",
                client_name="Future Client",
                contact_name="Finn",
                contact_email="finn@example.com",
                account_manager_email="am@example.com",
                cadence="monthly",
                next_report_date=date(2026, 6, 9),
            ),
            Client(
                client_id="client_manual",
                client_name="Manual Client",
                contact_name="Mina",
                contact_email="mina@example.com",
                account_manager_email="am@example.com",
                cadence="monthly",
                next_report_date=date(2026, 7, 1),
                run_now=True,
            ),
            Client(
                client_id="client_paused",
                client_name="Paused Client",
                contact_name="Pat",
                contact_email="pat@example.com",
                account_manager_email="am@example.com",
                cadence="monthly",
                next_report_date=date(2026, 6, 1),
                paused=True,
            ),
        ]

        due = select_due_clients(clients, today=date(2026, 6, 8))

        self.assertEqual([client.client_id for client in due], ["client_due", "client_manual"])

    def test_daily_workflow_generates_am_review_and_sheet_state(self):
        store = InMemorySheetStore(
            clients=[
                Client(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    contact_name="Ava",
                    contact_email="ava@example.com",
                    account_manager_email="am@example.com",
                    cadence="monthly",
                    next_report_date=date(2026, 6, 8),
                )
            ],
            metrics=[
                MetricRow(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    month="Feb-2026",
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
            ],
        )
        ai = OpenRouterClient.fake_report()
        workflow = ReportingWorkflow(store=store, ai=ai, gmail=store.gmail)

        workflow.run_due_reports(today=date(2026, 6, 8), period="Feb-2026")

        self.assertEqual(len(store.runs), 1)
        self.assertEqual(store.runs[0].status, RunStatus.AM_REVIEW)
        self.assertEqual(len(store.messages), 1)
        self.assertEqual(store.messages[0].message_type, "account_manager_review")
        self.assertIn("Reference: run:", store.gmail.sent_messages[0]["body"])
        self.assertIn("display:none", store.gmail.sent_messages[0]["body"])

    def test_due_reports_block_failed_client_and_continue_to_next_client(self):
        store = InMemorySheetStore(
            clients=[
                Client("client_a", "Client A", "Ava", "a@example.com", "am_a@example.com", "monthly", date(2026, 6, 8)),
                Client("client_b", "Client B", "Bea", "b@example.com", "am_b@example.com", "monthly", date(2026, 6, 8)),
            ],
            metrics=[
                MetricRow("client_a", "Client A", "Feb-2026", 100, 1000, 100, 10, 5, 20, 2, 2, 500, 5),
                MetricRow("client_b", "Client B", "Feb-2026", 100, 1000, 100, 10, 5, 20, 2, 2, 500, 5),
            ],
        )
        workflow = ReportingWorkflow(store=store, ai=OneClientCrashingAI(), gmail=store.gmail)

        workflow.run_due_reports(today=date(2026, 6, 8), period="Feb-2026")

        runs_by_client = {run.client_id: run for run in store.runs}
        self.assertEqual(runs_by_client["client_a"].status, RunStatus.BLOCKED)
        self.assertIn("temporary upstream failure", runs_by_client["client_a"].last_error)
        self.assertEqual(runs_by_client["client_b"].status, RunStatus.AM_REVIEW)
        self.assertEqual([message["to"] for message in store.gmail.sent_messages], ["am_b@example.com"])

    def test_scheduled_due_run_does_not_regenerate_existing_same_period_am_review(self):
        store = InMemorySheetStore(
            clients=[
                Client(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    contact_name="Ava",
                    contact_email="ava@example.com",
                    account_manager_email="am@example.com",
                    cadence="monthly",
                    next_report_date=date(2026, 6, 8),
                )
            ],
            runs=[
                Run(
                    run_id="run_1",
                    client_id="client_1",
                    period="Feb-2026",
                    status=RunStatus.AM_REVIEW,
                    html_report="<h1>Old</h1>",
                    gmail_thread_id="thread_1",
                    last_am_review_sent_at=datetime(2026, 6, 9, 18, tzinfo=timezone.utc),
                )
            ],
        )
        workflow = ReportingWorkflow(store=store, ai=FailingReportAI(), gmail=store.gmail)

        with patch("reportops.workflow.utc_now", return_value=datetime(2026, 6, 10, tzinfo=timezone.utc)):
            workflow.run_due_reports(today=date(2026, 6, 10), period="Feb-2026")

        self.assertEqual(store.runs[0].html_report, "<h1>Old</h1>")
        self.assertEqual(store.gmail.sent_messages, [])

    def test_scheduled_due_run_sends_am_follow_up_after_one_day(self):
        store = InMemorySheetStore(
            clients=[
                Client(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    contact_name="Ava",
                    contact_email="ava@example.com",
                    account_manager_email="am@example.com",
                    cadence="monthly",
                    next_report_date=date(2026, 6, 8),
                )
            ],
            runs=[
                Run(
                    run_id="run_1",
                    client_id="client_1",
                    period="Feb-2026",
                    status=RunStatus.AM_REVIEW,
                    html_report="<h1>Old</h1>",
                    gmail_thread_id="thread_1",
                    last_am_review_sent_at=datetime(2026, 6, 8, 23, tzinfo=timezone.utc),
                )
            ],
        )
        workflow = ReportingWorkflow(store=store, ai=FailingReportAI(), gmail=store.gmail)

        with patch("reportops.workflow.utc_now", return_value=datetime(2026, 6, 10, tzinfo=timezone.utc)):
            workflow.run_due_reports(today=date(2026, 6, 10), period="Feb-2026")

        self.assertEqual(len(store.gmail.sent_messages), 1)
        self.assertEqual(store.gmail.sent_messages[0]["to"], "am@example.com")
        self.assertEqual(store.gmail.sent_messages[0]["thread_id"], "thread_1")
        self.assertIn("Following up on this report review", store.gmail.sent_messages[0]["body"])
        self.assertIn("Reference: run:run_1", store.gmail.sent_messages[0]["body"])
        self.assertEqual(store.messages[0].message_type, "account_manager_follow_up")
        self.assertEqual(store.runs[0].last_am_review_sent_at, datetime(2026, 6, 10, tzinfo=timezone.utc))

    def test_open_am_review_for_prior_period_does_not_block_new_period(self):
        store = InMemorySheetStore(
            clients=[
                Client(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    contact_name="Ava",
                    contact_email="ava@example.com",
                    account_manager_email="am@example.com",
                    cadence="monthly",
                    next_report_date=date(2026, 6, 8),
                )
            ],
            metrics=[
                MetricRow("client_1", "BrightSmile Dental", "Feb-2026", 100, 1000, 100, 10, 5, 20, 2, 2, 500, 5),
                MetricRow("client_1", "BrightSmile Dental", "Mar-2026", 150, 1200, 120, 10, 8, 18.75, 3, 2.5, 700, 4.67),
            ],
            runs=[
                Run(
                    run_id="run_1",
                    client_id="client_1",
                    period="Feb-2026",
                    status=RunStatus.AM_REVIEW,
                    html_report="<h1>Old</h1>",
                    gmail_thread_id="thread_1",
                    last_am_review_sent_at=datetime(2026, 6, 9, tzinfo=timezone.utc),
                )
            ],
        )
        ai = CountingReportAI()
        workflow = ReportingWorkflow(store=store, ai=ai, gmail=store.gmail)

        workflow.run_due_reports(today=date(2026, 6, 10), period="Mar-2026")

        self.assertEqual(ai.report_call_count, 1)
        self.assertEqual([run.period for run in store.runs], ["Feb-2026", "Mar-2026"])

    def test_manual_client_run_regenerates_existing_same_period_am_review(self):
        store = InMemorySheetStore(
            clients=[
                Client(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    contact_name="Ava",
                    contact_email="ava@example.com",
                    account_manager_email="am@example.com",
                    cadence="monthly",
                    next_report_date=date(2026, 6, 8),
                )
            ],
            metrics=[
                MetricRow("client_1", "BrightSmile Dental", "Feb-2026", 100, 1000, 100, 10, 5, 20, 2, 2, 500, 5),
            ],
            runs=[
                Run(
                    run_id="run_1",
                    client_id="client_1",
                    period="Feb-2026",
                    status=RunStatus.AM_REVIEW,
                    html_report="<h1>Old</h1>",
                    gmail_thread_id="thread_1",
                    last_am_review_sent_at=datetime(2026, 6, 9, tzinfo=timezone.utc),
                )
            ],
        )
        ai = CountingReportAI()
        workflow = ReportingWorkflow(store=store, ai=ai, gmail=store.gmail)

        workflow.run_client_report("client_1", today=date(2026, 6, 10), period="Feb-2026")

        self.assertEqual(ai.report_call_count, 1)
        self.assertEqual(len(store.gmail.sent_messages), 1)
        self.assertEqual(store.messages[0].message_type, "account_manager_review")
        self.assertNotEqual(store.runs[0].html_report, "<h1>Old</h1>")

    def test_sync_replies_only_lists_active_run_threads(self):
        class ThreadCapturingGmail:
            def __init__(self) -> None:
                self.requested_thread_ids = None

            def list_recent_replies(self, thread_ids=None):
                self.requested_thread_ids = list(thread_ids or [])
                return []

            def mark_read(self, _):
                return None

            def send_html(self, to, subject, html_body, headers):
                return {"id": "gmail_1", "thread_id": "thread_1"}

        gmail = ThreadCapturingGmail()
        store = InMemorySheetStore(
            runs=[
                Run(
                    run_id="run_am",
                    client_id="client_1",
                    period="Feb-2026",
                    status=RunStatus.AM_REVIEW,
                    gmail_thread_id="am_thread",
                ),
                Run(
                    run_id="run_client",
                    client_id="client_1",
                    period="Feb-2026",
                    status=RunStatus.CLIENT_DELIVERED,
                    client_thread_id="client_thread",
                ),
                Run(
                    run_id="run_blocked",
                    client_id="client_1",
                    period="Feb-2026",
                    status=RunStatus.BLOCKED,
                    gmail_thread_id="blocked_thread",
                ),
            ]
        )
        workflow = ReportingWorkflow(store=store, ai=OpenRouterClient.fake_report(), gmail=gmail)

        workflow.sync_replies()

        self.assertEqual(gmail.requested_thread_ids, ["am_thread", "client_thread"])

    def test_report_generation_includes_target_and_previous_month_for_same_client(self):
        client = Client(
            client_id="client_1",
            client_name="BrightSmile Dental",
            contact_name="Ava",
            contact_email="ava@example.com",
            account_manager_email="am@example.com",
            cadence="monthly",
            next_report_date=date(2026, 6, 8),
        )
        other_client = Client(
            client_id="client_2",
            client_name="GreenLeaf Landscaping",
            contact_name="Gina",
            contact_email="gina@example.com",
            account_manager_email="am@example.com",
            cadence="monthly",
            next_report_date=date(2026, 6, 8),
        )
        store = InMemorySheetStore(
            clients=[client, other_client],
            metrics=[
                MetricRow(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    month="Jan-2026",
                    ad_spend=3200,
                    impressions=125000,
                    clicks=3100,
                    ctr=2.48,
                    leads=84,
                    cpl=38.1,
                    conversions=28,
                    conversion_rate=0.9,
                    revenue=18500,
                    roas=5.78,
                ),
                MetricRow(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    month="Feb-2026",
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
                ),
                MetricRow(
                    client_id="client_2",
                    client_name="GreenLeaf Landscaping",
                    month="Jan-2026",
                    ad_spend=1800,
                    impressions=82000,
                    clicks=1640,
                    ctr=2,
                    leads=42,
                    cpl=42.86,
                    conversions=14,
                    conversion_rate=0.85,
                    revenue=9600,
                    roas=5.33,
                ),
                MetricRow(
                    client_id="client_2",
                    client_name="GreenLeaf Landscaping",
                    month="Feb-2026",
                    ad_spend=1950,
                    impressions=88000,
                    clicks=1790,
                    ctr=2.03,
                    leads=49,
                    cpl=39.8,
                    conversions=17,
                    conversion_rate=0.95,
                    revenue=11800,
                    roas=6.05,
                ),
            ],
        )
        ai = CapturingReportAI()
        workflow = ReportingWorkflow(store=store, ai=ai, gmail=store.gmail)

        workflow.run_client_report("client_1", today=date(2026, 6, 8), period="Feb-2026")

        self.assertEqual([metric.client_id for metric in ai.report_metrics], ["client_1", "client_1"])
        self.assertEqual([metric.month for metric in ai.report_metrics], ["Jan-2026", "Feb-2026"])

    def test_report_generation_uses_target_month_when_previous_month_is_missing(self):
        store = InMemorySheetStore(
            clients=[
                Client(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    contact_name="Ava",
                    contact_email="ava@example.com",
                    account_manager_email="am@example.com",
                    cadence="monthly",
                    next_report_date=date(2026, 6, 8),
                )
            ],
            metrics=[
                MetricRow(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    month="Feb-2026",
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
            ],
        )
        ai = CapturingReportAI()
        workflow = ReportingWorkflow(store=store, ai=ai, gmail=store.gmail)

        workflow.run_client_report("client_1", today=date(2026, 6, 8), period="Feb-2026")

        self.assertEqual([metric.month for metric in ai.report_metrics], ["Feb-2026"])

    def test_report_generation_blocks_without_metrics(self):
        store = InMemorySheetStore(
            clients=[
                Client(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    contact_name="Ava",
                    contact_email="ava@example.com",
                    account_manager_email="am@example.com",
                    cadence="monthly",
                    next_report_date=date(2026, 6, 8),
                )
            ],
            metrics=[],
        )
        workflow = ReportingWorkflow(store=store, ai=FailingReportAI(), gmail=store.gmail)

        workflow.run_client_report("client_1", today=date(2026, 6, 8), period="Feb-2026")

        self.assertEqual(store.runs[0].status, RunStatus.BLOCKED)
        self.assertIn("No metrics found", store.runs[0].last_error)
        self.assertEqual(store.gmail.sent_messages, [])

    def test_ai_failure_blocks_run_without_sending_email(self):
        store = InMemorySheetStore(
            clients=[
                Client(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    contact_name="Ava",
                    contact_email="ava@example.com",
                    account_manager_email="am@example.com",
                    cadence="monthly",
                    next_report_date=date(2026, 6, 8),
                )
            ],
            metrics=[
                MetricRow(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    month="Feb-2026",
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
            ],
        )
        workflow = ReportingWorkflow(store=store, ai=OpenRouterClient.fake_failure(), gmail=store.gmail)

        workflow.run_due_reports(today=date(2026, 6, 8), period="Feb-2026")

        self.assertEqual(store.runs[0].status, RunStatus.BLOCKED)
        self.assertIn("structured output", store.runs[0].last_error)
        self.assertEqual(store.gmail.sent_messages, [])

    def test_ai_failure_sends_internal_error_to_support_email(self):
        store = InMemorySheetStore(
            clients=[
                Client(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    contact_name="Ava",
                    contact_email="ava@example.com",
                    account_manager_email="am@example.com",
                    cadence="monthly",
                    next_report_date=date(2026, 6, 8),
                    support_email="support@example.com",
                )
            ],
            metrics=[
                MetricRow(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    month="Feb-2026",
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
            ],
        )
        workflow = ReportingWorkflow(store=store, ai=OpenRouterClient.fake_failure(), gmail=store.gmail)

        workflow.run_due_reports(today=date(2026, 6, 8), period="Feb-2026")

        self.assertEqual(store.runs[0].status, RunStatus.BLOCKED)
        self.assertEqual(store.gmail.sent_messages[0]["to"], "support@example.com")
        self.assertIn("ReportOps blocked", store.gmail.sent_messages[0]["subject"])
        self.assertIn("BrightSmile Dental", store.gmail.sent_messages[0]["body"])
        self.assertEqual(store.messages[0].message_type, "support_error")

    def test_non_json_openrouter_response_blocks_run_without_crashing(self):
        store = InMemorySheetStore(
            clients=[
                Client(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    contact_name="Ava",
                    contact_email="ava@example.com",
                    account_manager_email="am@example.com",
                    cadence="monthly",
                    next_report_date=date(2026, 6, 8),
                )
            ],
            metrics=[
                MetricRow(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    month="Feb-2026",
                    ad_spend=100,
                    impressions=1000,
                    clicks=50,
                    ctr=5,
                    leads=5,
                    cpl=20,
                    conversions=2,
                    conversion_rate=4,
                    revenue=500,
                    roas=5,
                )
            ],
        )
        workflow = ReportingWorkflow(store=store, ai=NonJsonOpenRouterAI(), gmail=store.gmail)

        workflow.run_client_report("client_1", today=date(2026, 6, 8), period="Feb-2026")

        self.assertEqual(store.runs[0].status, RunStatus.BLOCKED)
        self.assertIn("OpenRouter returned non-JSON response", store.runs[0].last_error)
        self.assertEqual(store.gmail.sent_messages, [])

    def test_reply_matching_prefers_headers_then_thread_then_body_marker(self):
        runs = [
            Run(
                run_id="run_header",
                client_id="client_1",
                period="Feb-2026",
                status=RunStatus.AM_REVIEW,
                gmail_thread_id="thread_header",
            ),
            Run(
                run_id="run_thread",
                client_id="client_2",
                period="Feb-2026",
                status=RunStatus.AM_REVIEW,
                gmail_thread_id="thread_2",
            ),
        ]

        self.assertEqual(
            extract_run_id_from_reply(
                GmailInboundMessage(
                    message_id="msg_1",
                    thread_id="other",
                    subject="Re: review",
                    from_email="am@example.com",
                    headers={"references": "<reportops-run_header@local.reportops>"},
                    body="Approved",
                    snippet="Approved",
                    received_at=datetime.now(timezone.utc),
                ),
                runs,
            ),
            "run_header",
        )
        self.assertEqual(
            extract_run_id_from_reply(
                GmailInboundMessage(
                    message_id="msg_2",
                    thread_id="thread_2",
                    subject="Re: review",
                    from_email="am@example.com",
                    headers={},
                    body="Approved",
                    snippet="Approved",
                    received_at=datetime.now(timezone.utc),
                ),
                runs,
            ),
            "run_thread",
        )
        self.assertEqual(
            extract_run_id_from_reply(
                GmailInboundMessage(
                    message_id="msg_3",
                    thread_id="unknown",
                    subject="Re: review",
                    from_email="am@example.com",
                    headers={},
                    body="Please update this. Reference: run:run_header",
                    snippet="",
                    received_at=datetime.now(timezone.utc),
                ),
                runs,
            ),
            "run_header",
        )

    def test_unrelated_reply_is_not_matched_to_only_active_run(self):
        runs = [
            Run(
                run_id="run_1",
                client_id="client_1",
                period="Feb-2026",
                status=RunStatus.AM_REVIEW,
                gmail_thread_id="report_thread",
            )
        ]

        run_id = extract_run_id_from_reply(
            GmailInboundMessage(
                message_id="gmail_unrelated",
                thread_id="unrelated_thread",
                subject="Re: webhook lead magnet",
                from_email="am@example.com",
                headers={"references": "<some-other-message@example.com>"},
                body="Hey Zeno, I was building a lead magnet and wanted something that triggers on the webhook.",
                snippet="",
                received_at=datetime.now(timezone.utc),
            ),
            runs,
        )

        self.assertIsNone(run_id)

    def test_unmatched_reply_does_not_update_run_error_or_review_notes(self):
        store = InMemorySheetStore(
            clients=[
                Client(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    contact_name="Ava",
                    contact_email="ava@example.com",
                    account_manager_email="am@example.com",
                    cadence="monthly",
                    next_report_date=date(2026, 6, 8),
                )
            ],
            runs=[
                Run(
                    run_id="run_1",
                    client_id="client_1",
                    period="Feb-2026",
                    status=RunStatus.AM_REVIEW,
                    html_report="<h1>Report</h1>",
                    gmail_thread_id="report_thread",
                )
            ],
        )
        store.gmail.inbound_messages = [
            GmailInboundMessage(
                message_id="gmail_unrelated",
                thread_id="unrelated_thread",
                subject="Re: webhook lead magnet",
                from_email="am@example.com",
                headers={"references": "<some-other-message@example.com>"},
                body="Please update the webhook flow.",
                snippet="",
                received_at=datetime.now(timezone.utc),
            )
        ]
        workflow = ReportingWorkflow(store=store, ai=OpenRouterClient.fake_report(), gmail=store.gmail)

        workflow.sync_replies()

        self.assertEqual(store.runs[0].last_error, "")
        self.assertEqual(store.runs[0].am_review_notes, [])
        self.assertEqual(store.processed_message_ids, [])

    def test_am_approval_sends_client_email_and_ignores_duplicates(self):
        store = InMemorySheetStore(
            clients=[
                Client(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    contact_name="Ava",
                    contact_email="ava@example.com",
                    account_manager_email="am@example.com",
                    cadence="monthly",
                    next_report_date=date(2026, 6, 8),
                )
            ],
            runs=[
                Run(
                    run_id="run_1",
                    client_id="client_1",
                    period="Feb-2026",
                    status=RunStatus.AM_REVIEW,
                    html_report="<h1>Report</h1>",
                    gmail_thread_id="thread_1",
                )
            ],
        )
        inbound = GmailInboundMessage(
            message_id="gmail_1",
            thread_id="thread_1",
            subject="Re: report ready",
            from_email="am@example.com",
            headers={"references": "<reportops-run_1@local.reportops>"},
            body="Approved, send it",
            snippet="Approved",
            received_at=datetime.now(timezone.utc),
        )
        store.gmail.inbound_messages = [inbound, inbound]
        workflow = ReportingWorkflow(store=store, ai=OpenRouterClient.fake_report(), gmail=store.gmail)

        workflow.sync_replies()

        self.assertEqual(store.runs[0].status, RunStatus.CLIENT_DELIVERED)
        self.assertEqual(len(store.gmail.sent_messages), 1)
        self.assertEqual(store.gmail.sent_messages[0]["to"], "ava@example.com")
        self.assertIn("Reference: client:run_1", store.gmail.sent_messages[0]["body"])
        self.assertIn("display:none", store.gmail.sent_messages[0]["body"])
        self.assertEqual(store.processed_message_ids, ["gmail_1"])

    def test_am_great_reply_approves_report(self):
        store = InMemorySheetStore(
            clients=[
                Client(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    contact_name="Ava",
                    contact_email="ava@example.com",
                    account_manager_email="am@example.com",
                    cadence="monthly",
                    next_report_date=date(2026, 6, 8),
                )
            ],
            runs=[
                Run(
                    run_id="run_1",
                    client_id="client_1",
                    period="Feb-2026",
                    status=RunStatus.AM_REVIEW,
                    html_report="<h1>Report</h1>",
                    gmail_thread_id="thread_1",
                )
            ],
        )
        store.gmail.inbound_messages = [
            GmailInboundMessage(
                message_id="gmail_great",
                thread_id="thread_1",
                subject="Re: report ready",
                from_email="am@example.com",
                headers={"references": "<reportops-run_1@local.reportops>"},
                body="Great",
                snippet="Great",
                received_at=datetime.now(timezone.utc),
            )
        ]
        workflow = ReportingWorkflow(store=store, ai=OpenRouterClient.fake_report(), gmail=store.gmail)

        workflow.sync_replies()

        self.assertEqual(store.runs[0].status, RunStatus.CLIENT_DELIVERED)
        self.assertEqual(store.gmail.sent_messages[0]["to"], "ava@example.com")
        self.assertEqual(store.processed_message_ids, ["gmail_great"])

    def test_unclear_am_report_reply_gets_clarification_email(self):
        store = InMemorySheetStore(
            clients=[
                Client(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    contact_name="Ava",
                    contact_email="ava@example.com",
                    account_manager_email="am@example.com",
                    cadence="monthly",
                    next_report_date=date(2026, 6, 8),
                )
            ],
            runs=[
                Run(
                    run_id="run_1",
                    client_id="client_1",
                    period="Feb-2026",
                    status=RunStatus.AM_REVIEW,
                    html_report="<h1>Report</h1>",
                    gmail_thread_id="thread_1",
                )
            ],
        )
        store.gmail.inbound_messages = [
            GmailInboundMessage(
                message_id="gmail_unclear",
                thread_id="thread_1",
                subject="Re: report ready",
                from_email="am@example.com",
                headers={"references": "<reportops-run_1@local.reportops>"},
                body="Looks interesting",
                snippet="Looks interesting",
                received_at=datetime.now(timezone.utc),
            )
        ]
        workflow = ReportingWorkflow(store=store, ai=OpenRouterClient.fake_report(), gmail=store.gmail)

        workflow.sync_replies()

        self.assertEqual(store.runs[0].status, RunStatus.AM_REVIEW)
        self.assertEqual(store.gmail.sent_messages[0]["to"], "am@example.com")
        self.assertEqual(store.gmail.sent_messages[0]["thread_id"], "thread_1")
        self.assertIn("Please reply Approved to send", store.gmail.sent_messages[0]["body"])
        self.assertEqual(store.processed_message_ids, ["gmail_unclear"])

    def test_reprocessed_am_approval_does_not_resend_existing_current_delivery(self):
        review_sent_at = datetime(2026, 6, 8, 10, 0, tzinfo=timezone.utc)
        store = InMemorySheetStore(
            clients=[
                Client(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    contact_name="Ava",
                    contact_email="ava@example.com",
                    account_manager_email="am@example.com",
                    cadence="monthly",
                    next_report_date=date(2026, 6, 8),
                )
            ],
            runs=[
                Run(
                    run_id="run_1",
                    client_id="client_1",
                    period="Feb-2026",
                    status=RunStatus.AM_REVIEW,
                    html_report="<h1>Report</h1>",
                    gmail_thread_id="thread_1",
                    client_thread_id="client_thread",
                    client_message_id="<reportops-client-run_1-existing@example.com>",
                    last_am_review_sent_at=review_sent_at,
                )
            ],
        )
        store.messages = [
            MessageRecord(
                message_id="message_delivery",
                run_id="run_1",
                message_type="client_delivery",
                to="ava@example.com",
                subject="BrightSmile Dental Feb-2026 performance report",
                gmail_message_id="gmail_delivery",
                gmail_thread_id="client_thread",
                status="sent",
                created_at=datetime(2026, 6, 8, 10, 5, tzinfo=timezone.utc),
            )
        ]
        store.gmail.inbound_messages = [
            GmailInboundMessage(
                message_id="gmail_approval_seen_again",
                thread_id="thread_1",
                subject="Re: report ready",
                from_email="am@example.com",
                headers={"references": "<reportops-run_1@local.reportops>"},
                body="Approved",
                snippet="Approved",
                received_at=datetime.now(timezone.utc),
            )
        ]
        workflow = ReportingWorkflow(store=store, ai=OpenRouterClient.fake_report(), gmail=store.gmail)

        workflow.sync_replies()

        self.assertEqual(store.runs[0].status, RunStatus.CLIENT_DELIVERED)
        self.assertEqual(store.gmail.sent_messages, [])
        self.assertEqual(store.processed_message_ids, ["gmail_approval_seen_again"])

    def test_repeated_client_deliveries_use_fresh_message_ids(self):
        client = Client(
            client_id="client_1",
            client_name="BrightSmile Dental",
            contact_name="Ava",
            contact_email="ava@example.com",
            account_manager_email="am@example.com",
            cadence="monthly",
            next_report_date=date(2026, 6, 8),
        )
        run = Run(
            run_id="run_1",
            client_id="client_1",
            period="Feb-2026",
            status=RunStatus.CLIENT_DELIVERED,
            html_report="<h1>Report</h1>",
        )
        store = InMemorySheetStore(clients=[client], runs=[run])
        workflow = ReportingWorkflow(store=store, ai=OpenRouterClient.fake_report(), gmail=store.gmail)

        workflow._send_client_report(client, run)
        workflow._send_client_report(client, run)

        first_message_id = message_from_string(store.gmail.sent_messages[0]["raw"])["Message-ID"]
        second_message_id = message_from_string(store.gmail.sent_messages[1]["raw"])["Message-ID"]
        self.assertNotEqual(first_message_id, second_message_id)
        self.assertNotEqual(first_message_id, "<reportops-client-run_1@local.reportops>")
        self.assertNotEqual(second_message_id, "<reportops-client-run_1@local.reportops>")

    def test_sync_replies_ignores_outbound_system_follow_up_in_am_thread(self):
        store = InMemorySheetStore(
            clients=[
                Client(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    contact_name="Ava",
                    contact_email="ava@example.com",
                    account_manager_email="am@example.com",
                    cadence="monthly",
                    next_report_date=date(2026, 6, 8),
                )
            ],
            runs=[
                Run(
                    run_id="run_1",
                    client_id="client_1",
                    period="Feb-2026",
                    status=RunStatus.AM_REVIEW,
                    html_report="<h1>Report</h1>",
                    gmail_thread_id="thread_1",
                )
            ],
        )
        store.messages = [
            MessageRecord(
                message_id="message_followup",
                run_id="run_1",
                message_type="account_manager_follow_up",
                to="am@example.com",
                subject="Re: BrightSmile Dental report ready for review",
                gmail_message_id="gmail_followup",
                gmail_thread_id="thread_1",
                status="sent",
            )
        ]
        store.gmail.inbound_messages = [
            GmailInboundMessage(
                message_id="gmail_followup",
                thread_id="thread_1",
                subject="Re: BrightSmile Dental report ready for review",
                from_email="system@example.com",
                headers={"references": "<reportops-run_1@local.reportops>"},
                body="Please reply Approved to send, or reply with requested changes.",
                snippet="Please reply Approved",
                received_at=datetime.now(timezone.utc),
            )
        ]
        workflow = ReportingWorkflow(store=store, ai=OpenRouterClient.fake_report(), gmail=store.gmail)

        workflow.sync_replies()

        self.assertEqual(store.runs[0].status, RunStatus.AM_REVIEW)
        self.assertEqual(store.gmail.sent_messages, [])
        self.assertEqual(store.processed_message_ids, [])

    def test_am_approval_from_system_sender_address_still_sends_client_email(self):
        store = InMemorySheetStore(
            clients=[
                Client(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    contact_name="Ava",
                    contact_email="ava@example.com",
                    account_manager_email="system@example.com",
                    cadence="monthly",
                    next_report_date=date(2026, 6, 8),
                )
            ],
            runs=[
                Run(
                    run_id="run_1",
                    client_id="client_1",
                    period="Feb-2026",
                    status=RunStatus.AM_REVIEW,
                    html_report="<h1>Report</h1>",
                    gmail_thread_id="thread_1",
                )
            ],
        )
        store.gmail.inbound_messages = [
            GmailInboundMessage(
                message_id="gmail_approval",
                thread_id="thread_1",
                subject="Re: BrightSmile Dental report ready for review",
                from_email="System User <system@example.com>",
                headers={"references": "<reportops-run_1@local.reportops>"},
                body="Approved",
                snippet="Approved",
                received_at=datetime.now(timezone.utc),
            )
        ]
        workflow = ReportingWorkflow(store=store, ai=OpenRouterClient.fake_report(), gmail=store.gmail)

        workflow.sync_replies()

        self.assertEqual(store.runs[0].status, RunStatus.CLIENT_DELIVERED)
        self.assertEqual(len(store.gmail.sent_messages), 1)
        self.assertEqual(store.gmail.sent_messages[0]["to"], "ava@example.com")
        self.assertEqual(store.processed_message_ids, ["gmail_approval"])

    def test_client_schedule_updates_after_client_report_is_sent(self):
        store = InMemorySheetStore(
            clients=[
                Client(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    contact_name="Ava",
                    contact_email="ava@example.com",
                    account_manager_email="am@example.com",
                    cadence="monthly",
                    next_report_date=date(2026, 6, 8),
                    run_now=True,
                )
            ],
            runs=[
                Run(
                    run_id="run_1",
                    client_id="client_1",
                    period="Feb-2026",
                    status=RunStatus.AM_REVIEW,
                    html_report="<h1>Report</h1>",
                    gmail_thread_id="thread_1",
                )
            ],
        )
        store.gmail.inbound_messages = [
            GmailInboundMessage(
                message_id="gmail_1",
                thread_id="thread_1",
                subject="Re: report ready",
                from_email="am@example.com",
                headers={"references": "<reportops-run_1@local.reportops>"},
                body="Approved",
                snippet="Approved",
                received_at=datetime.now(timezone.utc),
            )
        ]
        workflow = ReportingWorkflow(store=store, ai=OpenRouterClient.fake_report(), gmail=store.gmail)

        with patch("reportops.workflow.utc_now", return_value=datetime(2026, 6, 9, tzinfo=timezone.utc)):
            workflow.sync_replies()

        self.assertFalse(store.clients[0].run_now)
        self.assertEqual(store.clients[0].next_report_date, date(2026, 7, 8))

    def test_client_schedule_updates_from_delivery_date_when_no_next_date_exists(self):
        store = InMemorySheetStore(
            clients=[
                Client(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    contact_name="Ava",
                    contact_email="ava@example.com",
                    account_manager_email="am@example.com",
                    cadence="monthly",
                    next_report_date=None,
                    run_now=True,
                )
            ],
            runs=[
                Run(
                    run_id="run_1",
                    client_id="client_1",
                    period="Feb-2026",
                    status=RunStatus.AM_REVIEW,
                    html_report="<h1>Report</h1>",
                    gmail_thread_id="thread_1",
                )
            ],
        )
        store.gmail.inbound_messages = [
            GmailInboundMessage(
                message_id="gmail_1",
                thread_id="thread_1",
                subject="Re: report ready",
                from_email="am@example.com",
                headers={"references": "<reportops-run_1@local.reportops>"},
                body="Approved",
                snippet="Approved",
                received_at=datetime.now(timezone.utc),
            )
        ]
        workflow = ReportingWorkflow(store=store, ai=OpenRouterClient.fake_report(), gmail=store.gmail)

        with patch("reportops.workflow.utc_now", return_value=datetime(2026, 6, 9, tzinfo=timezone.utc)):
            workflow.sync_replies()

        self.assertFalse(store.clients[0].run_now)
        self.assertEqual(store.clients[0].next_report_date, date(2026, 7, 9))

    def test_client_question_low_risk_auto_replies_and_high_risk_goes_to_am(self):
        store = InMemorySheetStore(
            clients=[
                Client(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    contact_name="Ava",
                    contact_email="ava@example.com",
                    account_manager_email="am@example.com",
                    cadence="monthly",
                    next_report_date=date(2026, 6, 8),
                )
            ],
            runs=[
                Run(
                    run_id="run_1",
                    client_id="client_1",
                    period="Feb-2026",
                    status=RunStatus.CLIENT_DELIVERED,
                    html_report="<h1>Report</h1>",
                    client_thread_id="client_thread",
                )
            ],
        )
        store.gmail.inbound_messages = [
            GmailInboundMessage(
                message_id="gmail_low",
                thread_id="client_thread",
                subject="Re: performance report",
                from_email="ava@example.com",
                headers={
                    "message-id": "<client-question-low@example.com>",
                    "references": "<reportops-client-run_1@local.reportops>",
                },
                body="Which channel had the best ROAS?",
                snippet="",
                received_at=datetime.now(timezone.utc),
            ),
            GmailInboundMessage(
                message_id="gmail_high",
                thread_id="client_thread",
                subject="Re: performance report",
                from_email="ava@example.com",
                headers={"references": "<reportops-client-run_1@local.reportops>"},
                body="Can you guarantee this revenue will continue?",
                snippet="",
                received_at=datetime.now(timezone.utc),
            ),
        ]
        workflow = ReportingWorkflow(store=store, ai=OpenRouterClient.fake_report(), gmail=store.gmail)

        workflow.sync_replies()

        self.assertEqual([question.risk_level for question in store.questions], ["low", "high"])
        self.assertEqual(store.questions[0].status, "auto_replied")
        self.assertEqual(store.questions[1].status, "needs_review")
        self.assertIn("Search had the best ROAS", store.questions[0].answer_html)
        self.assertEqual(store.gmail.sent_messages[0]["to"], "ava@example.com")
        self.assertEqual(store.gmail.sent_messages[0]["thread_id"], "client_thread")
        self.assertEqual(store.gmail.sent_messages[0]["subject"], "Re: BrightSmile Dental Feb-2026 performance report")
        self.assertEqual(store.questions[0].client_reply_message_id, "<client-question-low@example.com>")
        self.assertIn("In-Reply-To: <client-question-low@example.com>", store.gmail.sent_messages[0]["raw"])
        self.assertIn("References: <client-question-low@example.com>", store.gmail.sent_messages[0]["raw"])
        self.assertEqual(store.gmail.sent_messages[1]["to"], "am@example.com")

    def test_client_question_auto_reply_is_recorded_and_not_processed_again(self):
        store = InMemorySheetStore(
            clients=[
                Client(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    contact_name="Ava",
                    contact_email="ava@example.com",
                    account_manager_email="am@example.com",
                    cadence="monthly",
                    next_report_date=date(2026, 6, 8),
                )
            ],
            runs=[
                Run(
                    run_id="run_1",
                    client_id="client_1",
                    period="Feb-2026",
                    status=RunStatus.CLIENT_DELIVERED,
                    html_report="<h1>Report</h1>",
                    client_thread_id="client_thread",
                )
            ],
        )
        store.gmail.inbound_messages = [
            GmailInboundMessage(
                message_id="gmail_client_question",
                thread_id="client_thread",
                subject="Re: BrightSmile Dental Feb-2026 performance report",
                from_email="ava@example.com",
                headers={"references": "<reportops-client-run_1@local.reportops>"},
                body="What is ad spend?",
                snippet="",
                received_at=datetime.now(timezone.utc),
            )
        ]
        workflow = ReportingWorkflow(store=store, ai=OpenRouterClient.fake_report(), gmail=store.gmail)

        workflow.sync_replies()
        sent_answer = store.gmail.sent_messages[0]
        store.gmail.inbound_messages = [
            GmailInboundMessage(
                message_id=sent_answer["id"],
                thread_id=sent_answer["thread_id"],
                subject=sent_answer["subject"],
                from_email="connect2faaz@gmail.com",
                headers={"references": "<reportops-client-run_1@local.reportops>"},
                body=sent_answer["body"],
                snippet="",
                received_at=datetime.now(timezone.utc),
            )
        ]

        workflow.sync_replies()

        self.assertEqual(len(store.questions), 1)
        self.assertEqual(store.questions[0].question, "What is ad spend?")
        self.assertEqual(
            [message.message_type for message in store.messages],
            ["question_answer"],
        )
        self.assertEqual(store.processed_message_ids, ["gmail_client_question"])

    def test_client_question_ai_high_risk_routes_to_am_review(self):
        store = InMemorySheetStore(
            clients=[
                Client(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    contact_name="Ava",
                    contact_email="ava@example.com",
                    account_manager_email="am@example.com",
                    cadence="monthly",
                    next_report_date=date(2026, 6, 8),
                )
            ],
            runs=[
                Run(
                    run_id="run_1",
                    client_id="client_1",
                    period="Feb-2026",
                    status=RunStatus.CLIENT_DELIVERED,
                    html_report="<h1>Report</h1>",
                    client_thread_id="client_thread",
                )
            ],
        )
        store.gmail.inbound_messages = [
            GmailInboundMessage(
                message_id="gmail_strategy",
                thread_id="client_thread",
                subject="Re: performance report",
                from_email="ava@example.com",
                headers={"references": "<reportops-client-run_1@local.reportops>"},
                body="Which channel should we double budget on next month?",
                snippet="",
                received_at=datetime.now(timezone.utc),
            )
        ]
        workflow = ReportingWorkflow(store=store, ai=HighRiskQuestionAI(), gmail=store.gmail)

        workflow.sync_replies()

        self.assertEqual(store.questions[0].risk_level, "high")
        self.assertEqual(store.questions[0].status, "needs_review")
        self.assertEqual(store.runs[0].status, RunStatus.REPLY_REVIEW)
        self.assertEqual(store.gmail.sent_messages[0]["to"], "am@example.com")

    def test_simple_roas_definition_question_auto_replies_even_if_ai_marks_high_risk(self):
        store = InMemorySheetStore(
            clients=[
                Client(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    contact_name="Ava",
                    contact_email="ava@example.com",
                    account_manager_email="am@example.com",
                    cadence="monthly",
                    next_report_date=date(2026, 6, 8),
                )
            ],
            runs=[
                Run(
                    run_id="run_1",
                    client_id="client_1",
                    period="Feb-2026",
                    status=RunStatus.CLIENT_DELIVERED,
                    html_report="<h1>Report</h1>",
                    client_thread_id="client_thread",
                )
            ],
        )
        store.gmail.inbound_messages = [
            GmailInboundMessage(
                message_id="gmail_roas_definition",
                thread_id="client_thread",
                subject="Re: performance report",
                from_email="ava@example.com",
                headers={"references": "<reportops-client-run_1@local.reportops>"},
                body="What does ROAS mean?",
                snippet="",
                received_at=datetime.now(timezone.utc),
            )
        ]
        workflow = ReportingWorkflow(store=store, ai=HighRiskDefinitionQuestionAI(), gmail=store.gmail)

        workflow.sync_replies()

        self.assertEqual(store.questions[0].risk_level, "low")
        self.assertEqual(store.questions[0].status, "auto_replied")
        self.assertIn("return on ad spend", store.questions[0].answer_html.lower())
        self.assertEqual(store.runs[0].status, RunStatus.CLIENT_DELIVERED)
        self.assertEqual(store.gmail.sent_messages[0]["to"], "ava@example.com")

    def test_am_approved_client_question_answer_replies_in_original_client_thread(self):
        store = InMemorySheetStore(
            clients=[
                Client(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    contact_name="Ava",
                    contact_email="ava@example.com",
                    account_manager_email="am@example.com",
                    cadence="monthly",
                    next_report_date=date(2026, 6, 8),
                )
            ],
            runs=[
                Run(
                    run_id="run_1",
                    client_id="client_1",
                    period="Feb-2026",
                    status=RunStatus.REPLY_REVIEW,
                    html_report="<h1>Report</h1>",
                    gmail_thread_id="am_thread",
                    client_thread_id="client_thread",
                )
            ],
        )
        store.questions = [
            Question(
                question_id="question_1",
                run_id="run_1",
                client_id="client_1",
                question="Can you explain ROAS?",
                risk_level="high",
                answer_html="<p>ROAS means return on ad spend.</p>",
                status="needs_review",
                client_reply_message_id="<client-question-high@example.com>",
            )
        ]
        store.gmail.inbound_messages = [
            GmailInboundMessage(
                message_id="gmail_am_approval",
                thread_id="am_thread",
                subject="Re: AM review needed for client reply",
                from_email="am@example.com",
                headers={"references": "<reportops-run_1@local.reportops>"},
                body="Approved",
                snippet="Approved",
                received_at=datetime.now(timezone.utc),
            )
        ]
        workflow = ReportingWorkflow(store=store, ai=OpenRouterClient.fake_report(), gmail=store.gmail)

        workflow.sync_replies()

        self.assertEqual(store.questions[0].status, "sent")
        self.assertEqual(store.runs[0].status, RunStatus.CLIENT_DELIVERED)
        self.assertEqual(store.gmail.sent_messages[0]["to"], "ava@example.com")
        self.assertEqual(store.gmail.sent_messages[0]["thread_id"], "client_thread")
        self.assertEqual(store.gmail.sent_messages[0]["subject"], "Re: BrightSmile Dental Feb-2026 performance report")
        self.assertIn("In-Reply-To: <client-question-high@example.com>", store.gmail.sent_messages[0]["raw"])
        self.assertIn("References: <client-question-high@example.com>", store.gmail.sent_messages[0]["raw"])

    def test_am_approved_client_question_answer_uses_question_gmail_thread(self):
        store = InMemorySheetStore(
            clients=[
                Client(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    contact_name="Ava",
                    contact_email="ava@example.com",
                    account_manager_email="am@example.com",
                    cadence="monthly",
                    next_report_date=date(2026, 6, 8),
                )
            ],
            runs=[
                Run(
                    run_id="run_1",
                    client_id="client_1",
                    period="Feb-2026",
                    status=RunStatus.REPLY_REVIEW,
                    html_report="<h1>Report</h1>",
                    gmail_thread_id="am_thread",
                    client_thread_id="older_client_thread",
                )
            ],
        )
        store.questions = [
            Question(
                question_id="question_1",
                run_id="run_1",
                client_id="client_1",
                question="Can you explain ROAS?",
                risk_level="high",
                answer_html="<p>ROAS means return on ad spend.</p>",
                status="needs_review",
                gmail_thread_id="actual_question_thread",
            )
        ]
        store.gmail.inbound_messages = [
            GmailInboundMessage(
                message_id="gmail_am_approval",
                thread_id="am_thread",
                subject="Re: AM review needed for client reply",
                from_email="am@example.com",
                headers={"references": "<reportops-run_1@local.reportops>"},
                body="Approved",
                snippet="Approved",
                received_at=datetime.now(timezone.utc),
            )
        ]
        workflow = ReportingWorkflow(store=store, ai=OpenRouterClient.fake_report(), gmail=store.gmail)

        workflow.sync_replies()

        self.assertEqual(store.gmail.sent_messages[0]["thread_id"], "actual_question_thread")

    def test_client_question_answer_references_stored_client_delivery_message_id(self):
        store = InMemorySheetStore(
            clients=[
                Client(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    contact_name="Ava",
                    contact_email="ava@example.com",
                    account_manager_email="am@example.com",
                    cadence="monthly",
                    next_report_date=date(2026, 6, 8),
                )
            ],
            runs=[
                Run(
                    run_id="run_1",
                    client_id="client_1",
                    period="Feb-2026",
                    status=RunStatus.CLIENT_DELIVERED,
                    html_report="<h1>Report</h1>",
                    client_thread_id="client_thread",
                    client_message_id="<reportops-client-run_1-real123@example.com>",
                )
            ],
        )
        workflow = ReportingWorkflow(store=store, ai=OpenRouterClient.fake_report(), gmail=store.gmail)

        workflow._send_client_question_answer(
            store.clients[0],
            store.runs[0],
            "<p>ROAS means return on ad spend.</p>",
        )

        raw = store.gmail.sent_messages[0]["raw"]
        self.assertIn("In-Reply-To: <reportops-client-run_1-real123@example.com>", raw)
        self.assertIn("References: <reportops-client-run_1-real123@example.com>", raw)

    def test_email_builder_includes_html_and_tracking_headers(self):
        raw = build_raw_email(
            sender="sender@example.com",
            to="am@example.com",
            subject="Report ready",
            html_body="<h1>Report</h1>",
            headers={
                "X-ReportOps-Run-Id": "run_1",
                "X-ReportOps-Message-Type": "account_manager_review",
            },
        )

        self.assertIn("Content-Type: text/html", raw)
        self.assertIn("X-ReportOps-Run-Id: run_1", raw)
        self.assertIn("<h1>Report</h1>", raw)

    def test_reply_classifiers_are_conservative(self):
        self.assertEqual(classify_am_reply("Approved, send it"), "approve")
        self.assertEqual(classify_am_reply("Great"), "approve")
        self.assertEqual(classify_am_reply("Please change the budget section"), "request_changes")
        self.assertEqual(classify_am_reply("Looks interesting"), "unclear")
        self.assertEqual(classify_client_question_risk("Which channel performed best?"), "low")
        self.assertEqual(classify_client_question_risk("Can you guarantee revenue growth?"), "high")
        self.assertEqual(classify_client_question_risk("Why is CTR 6% when you said it would be 10%?"), "high")
        self.assertEqual(classify_client_question_risk("This does not match what you said before."), "high")


class OpenRouterParsingTests(unittest.TestCase):
    def test_structured_output_rejects_missing_html(self):
        with self.assertRaises(StructuredOutputError):
            OpenRouterClient.parse_report_payload({"executive_summary": "Summary"})


if __name__ == "__main__":
    unittest.main()
