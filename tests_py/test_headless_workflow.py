from datetime import date, datetime, timezone
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
from reportops.models import Client, MetricRow, QuestionAnswerOutput, Run, RunStatus
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
                headers={"references": "<reportops-client-run_1@local.reportops>"},
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
        self.assertEqual(store.gmail.sent_messages[1]["to"], "am@example.com")

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
