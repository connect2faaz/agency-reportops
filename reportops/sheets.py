from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
import json
import os
from typing import Any, Protocol
from urllib import parse, request

from .google_auth import refresh_google_access_token
from .models import Client, MessageRecord, MetricRow, Question, Run, RunStatus
from .workflow import InMemorySheetStore


class SheetClient(Protocol):
    def read_rows(self, tab_name: str) -> list[dict[str, str]]:
        ...

    def replace_rows(self, tab_name: str, rows: list[dict[str, str]]) -> None:
        ...


class GoogleSheetsApiClient:
    def __init__(self, spreadsheet_id: str | None = None, access_token_provider=refresh_google_access_token) -> None:
        self.spreadsheet_id = spreadsheet_id or os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "")
        if not self.spreadsheet_id:
            raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is required.")
        self.access_token_provider = access_token_provider

    def read_rows(self, tab_name: str) -> list[dict[str, str]]:
        values = self._request("GET", f"{tab_name}!A1:Z1000").get("values", [])
        if not values:
            return []
        headers = [str(header).strip() for header in values[0]]
        rows = []
        for value_row in values[1:]:
            padded = list(value_row) + [""] * max(0, len(headers) - len(value_row))
            rows.append({headers[index]: str(padded[index]) for index in range(len(headers))})
        return rows

    def replace_rows(self, tab_name: str, rows: list[dict[str, str]]) -> None:
        headers = _headers_for_tab(tab_name)
        values = [headers] + [[row.get(header, "") for header in headers] for row in rows]
        self._request("PUT", f"{tab_name}!A1", {"values": values})

    def _request(self, method: str, range_name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        token = self.access_token_provider()
        encoded_range = parse.quote(range_name, safe="!:")
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}/values/{encoded_range}"
        if method == "PUT":
            url = f"{url}?valueInputOption=USER_ENTERED"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = request.Request(
            url,
            data=data,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method=method,
        )
        with request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))


class GoogleSheetsStore(InMemorySheetStore):
    def __init__(self, sheet_client: SheetClient) -> None:
        self.sheet_client = sheet_client
        super().__init__(
            clients=[Client.from_sheet_row(row) for row in sheet_client.read_rows("Clients")],
            metrics=[MetricRow.from_sheet_row(row) for row in sheet_client.read_rows("Metrics")],
            runs=[_run_from_row(row) for row in sheet_client.read_rows("Runs")],
        )
        self.messages = [_message_from_row(row) for row in sheet_client.read_rows("Messages")]
        self.questions = [_question_from_row(row) for row in sheet_client.read_rows("Questions")]
        self.processed_message_ids = [
            message.gmail_message_id for message in self.messages if message.message_type == "processed_reply"
        ]

    def record_processed(self, gmail_message_id: str) -> bool:
        created = super().record_processed(gmail_message_id)
        if created:
            self.messages.append(
                MessageRecord(
                    message_id=f"processed_{gmail_message_id}",
                    run_id="",
                    message_type="processed_reply",
                    to="",
                    subject="",
                    gmail_message_id=gmail_message_id,
                    gmail_thread_id="",
                    status="processed",
                )
            )
        return created

    def flush(self) -> None:
        self.sheet_client.replace_rows("Runs", [_run_to_row(run) for run in self.runs])
        self.sheet_client.replace_rows("Messages", [_message_to_row(message) for message in self.messages])
        self.sheet_client.replace_rows("Questions", [_question_to_row(question) for question in self.questions])
        self.sheet_client.replace_rows("Clients", [_client_to_row(client) for client in self.clients])


def _headers_for_tab(tab_name: str) -> list[str]:
    return {
        "Clients": [
            "client_id",
            "client_name",
            "contact_name",
            "contact_email",
            "account_manager_email",
            "cadence",
            "next_report_date",
            "status",
            "run_now",
            "paused",
            "notes",
        ],
        "Runs": [
            "run_id",
            "client_id",
            "period",
            "status",
            "attempt_count",
            "last_error",
            "am_review_notes",
            "html_report",
            "gmail_thread_id",
            "client_thread_id",
            "created_at",
            "updated_at",
            "approved_at",
            "delivered_at",
        ],
        "Messages": [
            "message_id",
            "run_id",
            "type",
            "to",
            "subject",
            "gmail_message_id",
            "gmail_thread_id",
            "status",
            "created_at",
        ],
        "Questions": [
            "question_id",
            "run_id",
            "client_id",
            "question",
            "risk_level",
            "answer_html",
            "status",
            "created_at",
            "sent_at",
        ],
        "Metrics": [
            "client_id",
            "client_name",
            "month",
            "ad_spend",
            "impressions",
            "clicks",
            "ctr",
            "leads",
            "cpl",
            "conversions",
            "conversion_rate",
            "revenue",
            "roas",
        ],
    }[tab_name]


def _format_value(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, list):
        return "\n".join(str(item) for item in value)
    return "" if value is None else str(value)


def _client_to_row(client: Client) -> dict[str, str]:
    row = {key: _format_value(value) for key, value in asdict(client).items()}
    row["run_now"] = "TRUE" if client.run_now else ""
    row["paused"] = "TRUE" if client.paused else ""
    return row


def _run_to_row(run: Run) -> dict[str, str]:
    row = {key: _format_value(value) for key, value in asdict(run).items()}
    row["status"] = run.status.value
    return row


def _message_to_row(message: MessageRecord) -> dict[str, str]:
    row = {key: _format_value(value) for key, value in asdict(message).items()}
    row["type"] = message.message_type
    return row


def _question_to_row(question: Question) -> dict[str, str]:
    return {key: _format_value(value) for key, value in asdict(question).items()}


def _run_from_row(row: dict[str, str]) -> Run:
    return Run(
        run_id=row.get("run_id", ""),
        client_id=row.get("client_id", ""),
        period=row.get("period", ""),
        status=RunStatus(row.get("status", "am_review") or "am_review"),
        attempt_count=int(row.get("attempt_count", "0") or "0"),
        last_error=row.get("last_error", ""),
        am_review_notes=[line for line in row.get("am_review_notes", "").splitlines() if line],
        html_report=row.get("html_report", ""),
        gmail_thread_id=row.get("gmail_thread_id", ""),
        client_thread_id=row.get("client_thread_id", ""),
    )


def _message_from_row(row: dict[str, str]) -> MessageRecord:
    return MessageRecord(
        message_id=row.get("message_id", ""),
        run_id=row.get("run_id", ""),
        message_type=row.get("type", ""),
        to=row.get("to", ""),
        subject=row.get("subject", ""),
        gmail_message_id=row.get("gmail_message_id", ""),
        gmail_thread_id=row.get("gmail_thread_id", ""),
        status=row.get("status", ""),
    )


def _question_from_row(row: dict[str, str]) -> Question:
    return Question(
        question_id=row.get("question_id", ""),
        run_id=row.get("run_id", ""),
        client_id=row.get("client_id", ""),
        question=row.get("question", ""),
        risk_level=row.get("risk_level", ""),
        answer_html=row.get("answer_html", ""),
        status=row.get("status", ""),
    )

