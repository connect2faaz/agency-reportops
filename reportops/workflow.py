from __future__ import annotations

import secrets
from calendar import monthrange
from datetime import date, datetime, timedelta

from .ai import OpenRouterClient, StructuredOutputError
from .gmail import (
    GmailInboundMessage,
    InMemoryGmailClient,
    classify_am_reply,
    classify_client_question_risk,
    extract_newest_reply_text,
    extract_run_id_from_reply,
    is_reply,
)
from .models import Client, MessageRecord, MetricRow, Question, Run, RunStatus, parse_month_period, utc_now


HIDDEN_REFERENCE_STYLE = "display:none;color:#ffffff;font-size:1px;line-height:1px;opacity:0;max-height:0;overflow:hidden;"
AM_FOLLOW_UP_INTERVAL = timedelta(hours=24)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def select_due_clients(clients: list[Client], today: date) -> list[Client]:
    return [
        client
        for client in clients
        if not client.paused and (client.run_now or (client.next_report_date is not None and client.next_report_date <= today))
    ]


def previous_period(period: str) -> str | None:
    period = parse_month_period(period)
    try:
        target = datetime.strptime(period, "%b-%Y").date()
    except ValueError:
        return None
    first_of_month = target.replace(day=1)
    previous_month = first_of_month - timedelta(days=1)
    return previous_month.strftime("%b-%Y")


def next_monthly_date(current: date) -> date:
    month = current.month + 1
    year = current.year
    if month > 12:
        month = 1
        year += 1
    day = min(current.day, monthrange(year, month)[1])
    return date(year, month, day)


class InMemorySheetStore:
    def __init__(
        self,
        clients: list[Client] | None = None,
        metrics: list[MetricRow] | None = None,
        runs: list[Run] | None = None,
    ) -> None:
        self.clients = clients or []
        self.metrics = metrics or []
        self.runs = runs or []
        self.messages: list[MessageRecord] = []
        self.questions: list[Question] = []
        self.processed_message_ids: list[str] = []
        self.gmail = InMemoryGmailClient()

    def metrics_for(self, client_id: str, period: str) -> list[MetricRow]:
        period = parse_month_period(period)
        return [
            metric
            for metric in self.metrics
            if metric.client_id == client_id and parse_month_period(metric.month) == period
        ]

    def metrics_for_report_period(self, client_id: str, period: str) -> list[MetricRow]:
        period = parse_month_period(period)
        periods = [period]
        prior_period = previous_period(period)
        if prior_period is not None:
            periods = [prior_period, period]
        return [
            metric
            for selected_period in periods
            for metric in self.metrics
            if metric.client_id == client_id and parse_month_period(metric.month) == selected_period
        ]

    def client_by_id(self, client_id: str) -> Client:
        for client in self.clients:
            if client.client_id == client_id:
                return client
        raise KeyError(f"Unknown client_id: {client_id}")

    def run_by_id(self, run_id: str) -> Run:
        for run in self.runs:
            if run.run_id == run_id:
                return run
        raise KeyError(f"Unknown run_id: {run_id}")

    def active_run_for(self, client_id: str, period: str) -> Run | None:
        for run in self.runs:
            if run.client_id == client_id and run.period == period and run.status in {RunStatus.AM_REVIEW, RunStatus.BLOCKED}:
                return run
        return None

    def run_for_report_period(self, client_id: str, period: str) -> Run | None:
        for run in self.runs:
            if run.client_id == client_id and run.period == period:
                return run
        return None

    def save_message(self, message: MessageRecord) -> None:
        self.messages.append(message)

    def record_processed(self, gmail_message_id: str) -> bool:
        if gmail_message_id in self.processed_message_ids:
            return False
        self.processed_message_ids.append(gmail_message_id)
        return True


class ReportingWorkflow:
    def __init__(self, store: InMemorySheetStore, ai: OpenRouterClient, gmail: InMemoryGmailClient) -> None:
        self.store = store
        self.ai = ai
        self.gmail = gmail

    def run_due_reports(self, today: date, period: str | None = None) -> None:
        for client in select_due_clients(self.store.clients, today):
            self._start_or_retry_report(client, period or self._default_period(today), force=False)

    def run_client_report(self, client_id: str, today: date, period: str | None = None) -> None:
        client = self.store.client_by_id(client_id)
        self._start_or_retry_report(client, period or self._default_period(today), force=True)

    def sync_replies(self) -> None:
        outbound_message_ids = {
            message.gmail_message_id for message in self.store.messages if message.message_type != "processed_reply"
        }
        for inbound in self.gmail.list_recent_replies(thread_ids=self._active_thread_ids()):
            if inbound.message_id in self.store.processed_message_ids:
                continue
            if inbound.message_id in outbound_message_ids:
                continue
            if not is_reply(inbound):
                continue
            run_id = extract_run_id_from_reply(inbound, self.store.runs)
            if not run_id:
                continue
            run = self.store.run_by_id(run_id)
            client = self.store.client_by_id(run.client_id)
            reply_text = extract_newest_reply_text(inbound.body or inbound.snippet)
            if run.status == RunStatus.AM_REVIEW:
                self._handle_am_report_reply(run, client, inbound, reply_text)
            elif run.status == RunStatus.CLIENT_DELIVERED:
                self._handle_client_reply(run, client, inbound, reply_text)
            elif run.status == RunStatus.REPLY_REVIEW:
                self._handle_am_question_reply(run, inbound, reply_text)

    def _start_or_retry_report(self, client: Client, period: str, force: bool = False) -> None:
        run = self.store.run_for_report_period(client.client_id, period)
        if run is not None and not force:
            if run.status == RunStatus.AM_REVIEW:
                self._send_am_follow_up_if_due(client, run)
            return
        if run is None:
            run = Run(run_id=new_id("run"), client_id=client.client_id, period=period, status=RunStatus.AM_REVIEW)
            self.store.runs.append(run)
        run.attempt_count += 1
        run.updated_at = utc_now()
        metrics = self.store.metrics_for_report_period(client.client_id, period)
        if not metrics:
            run.status = RunStatus.BLOCKED
            run.last_error = f"No metrics found for client_id={client.client_id} period={period}."
            return
        try:
            report = self.ai.generate_report(client, metrics, run.am_review_notes)
        except StructuredOutputError as error:
            run.status = RunStatus.BLOCKED
            run.last_error = str(error)
            return
        run.status = RunStatus.AM_REVIEW
        run.last_error = ""
        run.html_report = report.html_report
        self._send_am_review(client, run)

    def _send_am_review(self, client: Client, run: Run) -> None:
        body = (
            f"{run.html_report}"
            f"<hr><p><strong>AM review:</strong> reply Approved to send, or reply with requested changes.</p>"
            f"{self._hidden_reference(f'run:{run.run_id}')}"
        )
        sent = self.gmail.send_html(
            to=client.account_manager_email,
            subject=f"{client.client_name} report ready for review",
            html_body=body,
            headers={
                "Message-ID": f"<reportops-{run.run_id}@local.reportops>",
                "X-ReportOps-Run-Id": run.run_id,
                "X-ReportOps-Message-Type": "account_manager_review",
            },
        )
        sent_at = utc_now()
        run.last_am_review_sent_at = sent_at
        run.updated_at = sent_at
        run.gmail_thread_id = sent["thread_id"]
        self.store.save_message(
            MessageRecord(
                message_id=new_id("message"),
                run_id=run.run_id,
                message_type="account_manager_review",
                to=client.account_manager_email,
                subject=f"{client.client_name} report ready for review",
                gmail_message_id=sent["id"],
                gmail_thread_id=sent["thread_id"],
                status="sent",
            )
        )

    def _send_am_follow_up_if_due(self, client: Client, run: Run) -> None:
        now = utc_now()
        if run.last_am_review_sent_at is not None and now - run.last_am_review_sent_at < AM_FOLLOW_UP_INTERVAL:
            return
        self._send_am_follow_up(client, run, now)

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

    def _handle_am_report_reply(
        self, run: Run, client: Client, inbound: GmailInboundMessage, reply_text: str
    ) -> None:
        intent = classify_am_reply(reply_text)
        if intent == "approve":
            run.status = RunStatus.CLIENT_DELIVERED
            run.approved_at = utc_now()
            self._send_client_report(client, run)
            self.store.record_processed(inbound.message_id)
            self.gmail.mark_read(inbound.message_id)
            return
        if intent == "request_changes":
            run.am_review_notes.append(reply_text)
            self.store.record_processed(inbound.message_id)
            self._start_or_retry_report(client, run.period, force=True)
            return
        run.last_error = f"Unclear AM reply: {reply_text}"
        self.store.record_processed(inbound.message_id)

    def _send_client_report(self, client: Client, run: Run) -> None:
        body = f"{run.html_report}<hr>{self._hidden_reference(f'client:{run.run_id}')}"
        sent = self.gmail.send_html(
            to=client.contact_email,
            subject=self._client_report_subject(client, run),
            html_body=body,
            headers={
                "Message-ID": self._client_report_message_id(run),
                "X-ReportOps-Run-Id": run.run_id,
                "X-ReportOps-Client-Id": client.client_id,
                "X-ReportOps-Message-Type": "client_delivery",
            },
        )
        run.client_thread_id = sent["thread_id"]
        run.delivered_at = utc_now()
        self._mark_client_report_delivered(client, run.delivered_at.date())
        self.store.save_message(
            MessageRecord(
                message_id=new_id("message"),
                run_id=run.run_id,
                message_type="client_delivery",
                to=client.contact_email,
                subject=self._client_report_subject(client, run),
                gmail_message_id=sent["id"],
                gmail_thread_id=sent["thread_id"],
                status="sent",
            )
        )

    @staticmethod
    def _mark_client_report_delivered(client: Client, delivered_date: date) -> None:
        client.run_now = False
        next_report_date = next_monthly_date(client.next_report_date or delivered_date)
        while next_report_date <= delivered_date:
            next_report_date = next_monthly_date(next_report_date)
        client.next_report_date = next_report_date

    def _handle_client_reply(self, run: Run, client: Client, inbound: GmailInboundMessage, reply_text: str) -> None:
        regex_risk = classify_client_question_risk(reply_text)
        try:
            draft = self.ai.draft_question_answer(client, reply_text, run.html_report)
            risk = "high" if regex_risk == "high" or draft.requires_am_review or draft.risk_level != "low" else "low"
            answer = draft.answer_html
        except StructuredOutputError as error:
            risk = "high"
            answer = (
                "<p>This client question needs account-manager review before a response is sent.</p>"
                f"<p><strong>AI draft error:</strong> {error}</p>"
            )
        question = Question(
            question_id=new_id("question"),
            run_id=run.run_id,
            client_id=client.client_id,
            question=reply_text,
            risk_level=risk,
            answer_html=answer,
            status="open",
        )
        self.store.questions.append(question)
        if risk == "low":
            question.status = "auto_replied"
            question.sent_at = utc_now()
            self._send_client_question_answer(client, run, answer)
        else:
            question.status = "needs_review"
            run.status = RunStatus.REPLY_REVIEW
            self.gmail.send_html(
                to=client.account_manager_email,
                subject="AM review needed for client reply",
                html_body=f"{answer}{self._hidden_reference(f'run:{run.run_id}')}",
                headers={
                    "X-ReportOps-Run-Id": run.run_id,
                    "X-ReportOps-Message-Type": "reply_review",
                },
                thread_id=run.gmail_thread_id,
            )
        self.store.record_processed(inbound.message_id)

    def _handle_am_question_reply(self, run: Run, inbound: GmailInboundMessage, reply_text: str) -> None:
        if classify_am_reply(reply_text) != "approve":
            run.last_error = f"Unclear or change-request AM reply for client answer: {reply_text}"
            self.store.record_processed(inbound.message_id)
            return
        for question in self.store.questions:
            if question.run_id == run.run_id and question.status == "needs_review":
                client = self.store.client_by_id(run.client_id)
                question.status = "sent"
                question.sent_at = utc_now()
                self._send_client_question_answer(client, run, question.answer_html)
                run.status = RunStatus.CLIENT_DELIVERED
                break
        self.store.record_processed(inbound.message_id)

    def _send_client_question_answer(self, client: Client, run: Run, answer_html: str) -> None:
        subject = self._client_report_reply_subject(client, run)
        sent = self.gmail.send_html(
            to=client.contact_email,
            subject=subject,
            html_body=f"{answer_html}{self._hidden_reference(f'client:{run.run_id}')}",
            headers={
                "In-Reply-To": self._client_report_message_id(run),
                "References": self._client_report_message_id(run),
                "X-ReportOps-Run-Id": run.run_id,
                "X-ReportOps-Client-Id": client.client_id,
                "X-ReportOps-Message-Type": "question_answer",
            },
            thread_id=run.client_thread_id,
        )
        self.store.save_message(
            MessageRecord(
                message_id=new_id("message"),
                run_id=run.run_id,
                message_type="question_answer",
                to=client.contact_email,
                subject=subject,
                gmail_message_id=sent["id"],
                gmail_thread_id=sent["thread_id"],
                status="sent",
            )
        )

    @staticmethod
    def _default_period(today: date) -> str:
        first = today.replace(day=1)
        previous = first - timedelta(days=1)
        return previous.strftime("%b-%Y")

    def _active_thread_ids(self) -> list[str]:
        thread_ids = []
        for run in self.store.runs:
            if run.status == RunStatus.AM_REVIEW and run.gmail_thread_id:
                thread_ids.append(run.gmail_thread_id)
            elif run.status == RunStatus.CLIENT_DELIVERED and run.client_thread_id:
                thread_ids.append(run.client_thread_id)
            elif run.status == RunStatus.REPLY_REVIEW and run.gmail_thread_id:
                thread_ids.append(run.gmail_thread_id)
        return list(dict.fromkeys(thread_ids))

    @staticmethod
    def _hidden_reference(marker: str) -> str:
        return f'<div style="{HIDDEN_REFERENCE_STYLE}" aria-hidden="true">Reference: {marker}</div>'

    @staticmethod
    def _client_report_message_id(run: Run) -> str:
        return f"<reportops-client-{run.run_id}@local.reportops>"

    @staticmethod
    def _client_report_subject(client: Client, run: Run) -> str:
        return f"{client.client_name} {run.period} performance report"

    @classmethod
    def _client_report_reply_subject(cls, client: Client, run: Run) -> str:
        return f"Re: {cls._client_report_subject(client, run)}"
