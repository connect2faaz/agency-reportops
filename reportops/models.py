from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "run", "x"}


def parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    return date.fromisoformat(text)


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def parse_float(value: Any) -> float:
    text = str(value or "").replace("$", "").replace(",", "").strip()
    return float(text or 0)


def parse_month_period(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%b-%Y")
    if isinstance(value, date):
        return value.strftime("%b-%Y")
    text = str(value or "").strip()
    if not text:
        return ""
    for date_format in ("%b-%Y", "%b %Y", "%B %Y", "%Y-%m", "%Y-%m-%d", "%Y/%m", "%m/%d/%Y", "%m/%Y"):
        try:
            return datetime.strptime(text, date_format).strftime("%b-%Y")
        except ValueError:
            continue
    return text


class RunStatus(StrEnum):
    AM_REVIEW = "am_review"
    BLOCKED = "blocked"
    CLIENT_DELIVERED = "client_delivered"
    COMPLETE = "complete"
    REPLY_REVIEW = "reply_review"


@dataclass(slots=True)
class Client:
    client_id: str
    client_name: str
    contact_name: str
    contact_email: str
    account_manager_email: str
    cadence: str
    next_report_date: date | None
    status: str = ""
    run_now: bool = False
    paused: bool = False
    notes: str = ""
    support_email: str = ""

    @classmethod
    def from_sheet_row(cls, row: dict[str, Any]) -> "Client":
        return cls(
            client_id=str(row.get("client_id", "")).strip(),
            client_name=str(row.get("client_name", "")).strip(),
            contact_name=str(row.get("contact_name", "")).strip(),
            contact_email=str(row.get("contact_email", "")).strip(),
            account_manager_email=str(row.get("account_manager_email", "")).strip(),
            cadence=str(row.get("cadence", "monthly")).strip() or "monthly",
            next_report_date=parse_date(row.get("next_report_date")),
            status=str(row.get("status", "")).strip(),
            run_now=parse_bool(row.get("run_now")),
            paused=parse_bool(row.get("paused")),
            notes=str(row.get("notes", "")).strip(),
            support_email=str(row.get("support_email", "")).strip(),
        )


@dataclass(slots=True)
class MetricRow:
    client_id: str
    client_name: str
    month: str
    ad_spend: float
    impressions: float
    clicks: float
    ctr: float
    leads: float
    cpl: float
    conversions: float
    conversion_rate: float
    revenue: float
    roas: float

    @classmethod
    def from_sheet_row(cls, row: dict[str, Any]) -> "MetricRow":
        return cls(
            client_id=str(row.get("client_id", "")).strip(),
            client_name=str(row.get("client_name", "")).strip(),
            month=parse_month_period(row.get("month")),
            ad_spend=parse_float(row.get("ad_spend")),
            impressions=parse_float(row.get("impressions")),
            clicks=parse_float(row.get("clicks")),
            ctr=parse_float(row.get("ctr")),
            leads=parse_float(row.get("leads")),
            cpl=parse_float(row.get("cpl")),
            conversions=parse_float(row.get("conversions")),
            conversion_rate=parse_float(row.get("conversion_rate")),
            revenue=parse_float(row.get("revenue")),
            roas=parse_float(row.get("roas")),
        )


@dataclass(slots=True)
class Run:
    run_id: str
    client_id: str
    period: str
    status: RunStatus
    attempt_count: int = 0
    last_error: str = ""
    am_review_notes: list[str] = field(default_factory=list)
    html_report: str = ""
    gmail_thread_id: str = ""
    client_thread_id: str = ""
    client_message_id: str = ""
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    approved_at: datetime | None = None
    delivered_at: datetime | None = None
    last_am_review_sent_at: datetime | None = None


@dataclass(slots=True)
class MessageRecord:
    message_id: str
    run_id: str
    message_type: str
    to: str
    subject: str
    gmail_message_id: str
    gmail_thread_id: str
    status: str
    created_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class Question:
    question_id: str
    run_id: str
    client_id: str
    question: str
    risk_level: str
    answer_html: str
    status: str
    gmail_thread_id: str = ""
    client_reply_message_id: str = ""
    created_at: datetime = field(default_factory=utc_now)
    sent_at: datetime | None = None


@dataclass(slots=True)
class ReportOutput:
    executive_summary: str
    highlights: list[str]
    concerns: list[str]
    next_actions: list[str]
    html_report: str


@dataclass(slots=True)
class QuestionAnswerOutput:
    intent: str
    risk_level: str
    risk_reason: str
    answer_html: str
    requires_am_review: bool
