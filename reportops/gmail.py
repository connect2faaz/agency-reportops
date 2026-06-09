from __future__ import annotations

import base64
import html
import re
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from typing import Iterable

from .models import Run


@dataclass(slots=True)
class GmailInboundMessage:
    message_id: str
    thread_id: str
    subject: str
    from_email: str
    headers: dict[str, str]
    body: str
    snippet: str
    received_at: datetime


class InMemoryGmailClient:
    def __init__(self) -> None:
        self.sent_messages: list[dict[str, str]] = []
        self.inbound_messages: list[GmailInboundMessage] = []

    def send_html(
        self, to: str, subject: str, html_body: str, headers: dict[str, str], thread_id: str | None = None
    ) -> dict[str, str]:
        message_id = f"gmail_{len(self.sent_messages) + 1}"
        resolved_thread_id = thread_id or headers.get("X-ReportOps-Run-Id", message_id)
        self.sent_messages.append(
            {
                "id": message_id,
                "thread_id": resolved_thread_id,
                "to": to,
                "subject": subject,
                "body": html_body,
                "raw": build_raw_email(
                    sender="sender@example.com",
                    to=to,
                    subject=subject,
                    html_body=html_body,
                    headers=headers,
                ),
            }
        )
        return {"id": message_id, "thread_id": resolved_thread_id}

    def list_recent_replies(self, thread_ids: list[str] | None = None) -> list[GmailInboundMessage]:
        if thread_ids is None:
            return list(self.inbound_messages)
        if not thread_ids:
            return []
        allowed_threads = set(thread_ids)
        return [message for message in self.inbound_messages if message.thread_id in allowed_threads]

    def mark_read(self, _: str) -> None:
        return None


def build_raw_email(sender: str, to: str, subject: str, html_body: str, headers: dict[str, str]) -> str:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    for key, value in headers.items():
        message[key] = value
    message.set_content(html.unescape(re.sub(r"<[^>]+>", " ", html_body)))
    message.add_alternative(html_body, subtype="html")
    return message.as_string()


def build_raw_email_base64url(sender: str, to: str, subject: str, html_body: str, headers: dict[str, str]) -> str:
    return base64.urlsafe_b64encode(build_raw_email(sender, to, subject, html_body, headers).encode("utf-8")).decode(
        "ascii"
    )


def classify_am_reply(text: str) -> str:
    value = text.lower()
    if re.search(r"\b(approve|approved|looks good|send it|ship it|go ahead)\b", value):
        return "approve"
    if re.search(r"\b(change|revise|edit|update|fix|remove|disapprove|not approved)\b", value):
        return "request_changes"
    return "unclear"


def classify_client_question_risk(text: str) -> str:
    value = text.lower()
    high_risk = (
        r"\b(guarantee|guaranteed|legal|lawsuit|refund|contract|compliance|medical|financial advice|promise)\b"
        r"|why\s+.*\b(you|we|report|said|promised|expected)\b"
        r"|\b(not match|doesn'?t match|didn'?t match|wrong|incorrect|inaccurate|unhappy|upset|disappointed)\b"
        r"|\b(you said|we expected|as promised|was supposed to|should have been)\b"
    )
    return "high" if re.search(high_risk, value) else "low"


def is_reply(message: GmailInboundMessage) -> bool:
    return bool(re.match(r"^\s*(re|fw|fwd)\s*:", message.subject, re.I))


def extract_newest_reply_text(body: str) -> str:
    return re.split(r"\nOn .+wrote:|\nFrom:|\n-{2,}Original Message-{2,}", body, maxsplit=1)[0].strip()


def extract_run_id_from_reply(message: GmailInboundMessage, runs: Iterable[Run]) -> str | None:
    runs_by_id = {run.run_id: run for run in runs}
    header_text = "\n".join(
        [
            message.headers.get("x-reportops-run-id", ""),
            message.headers.get("X-ReportOps-Run-Id", ""),
            message.headers.get("references", ""),
            message.headers.get("References", ""),
            message.headers.get("in-reply-to", ""),
            message.headers.get("In-Reply-To", ""),
        ]
    )
    matched = _extract_run_marker(header_text)
    if matched and matched in runs_by_id:
        return matched
    for run in runs_by_id.values():
        if message.thread_id and message.thread_id in {run.gmail_thread_id, run.client_thread_id}:
            return run.run_id
    matched = _extract_run_marker(f"{message.subject}\n{message.body}")
    if matched and matched in runs_by_id:
        return matched
    return None


def _extract_run_marker(value: str) -> str | None:
    patterns = [
        r"\b(?:run|client):(run[_a-zA-Z0-9-]+)\b",
        r"reportops-(?:client-)?(run[_a-zA-Z0-9-]+)@",
        r"\b(run[_a-zA-Z0-9-]+)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    return None
