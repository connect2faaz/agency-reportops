from __future__ import annotations

import json
import os
import base64
from datetime import datetime, timezone
from typing import Any, Callable
from urllib import parse, request

from .gmail import GmailInboundMessage, build_raw_email_base64url
from .google_auth import refresh_google_access_token


GMAIL_MESSAGES_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
GMAIL_THREADS_URL = "https://gmail.googleapis.com/gmail/v1/users/me/threads"
DEFAULT_REPLY_QUERY = (
    'newer_than:30d '
    '(subject:"performance report" OR subject:"report ready for review" OR subject:"AM review needed for client reply")'
)


class GmailApiClient:
    def __init__(
        self,
        sender_email: str | None = None,
        access_token_provider: Callable[[], str] | None = None,
        post_json: Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]] | None = None,
        get_json: Callable[[str, dict[str, str]], dict[str, Any]] | None = None,
    ) -> None:
        self.sender_email = sender_email or os.getenv("SYSTEM_SENDER_EMAIL", "")
        if not self.sender_email:
            raise RuntimeError("SYSTEM_SENDER_EMAIL is required for Gmail sending.")
        self.access_token_provider = access_token_provider or refresh_google_access_token
        self._post_json = post_json or _post_json
        self._get_json = get_json or _get_json

    def send_html(
        self, to: str, subject: str, html_body: str, headers: dict[str, str], thread_id: str | None = None
    ) -> dict[str, str]:
        token = self.access_token_provider()
        raw = build_raw_email_base64url(
            sender=self.sender_email,
            to=to,
            subject=subject,
            html_body=html_body,
            headers=headers,
        )
        send_payload = {"raw": raw}
        if thread_id:
            send_payload["threadId"] = thread_id
        payload = self._post_json(
            f"{GMAIL_MESSAGES_URL}/send",
            send_payload,
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        message_id = str(payload.get("id", "")).strip()
        thread_id = str(payload.get("threadId", "")).strip()
        if not message_id:
            raise RuntimeError("Gmail send did not return a message id.")
        return {"id": message_id, "thread_id": thread_id or message_id}

    def list_recent_replies(
        self, query: str = DEFAULT_REPLY_QUERY, thread_ids: list[str] | None = None
    ) -> list[GmailInboundMessage]:
        token = self.access_token_provider()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if thread_ids is not None:
            replies = []
            for thread_id in dict.fromkeys(thread_ids):
                replies.extend(self._fetch_thread_messages(thread_id, headers))
            return replies
        params = parse.urlencode({"q": query, "maxResults": "50"})
        listing = self._get_json(f"{GMAIL_MESSAGES_URL}?{params}", headers)
        messages = []
        for item in listing.get("messages", []) or []:
            message_id = item.get("id")
            if message_id:
                messages.append(self._fetch_message(str(message_id), headers))
        return messages

    def mark_read(self, message_id: str) -> None:
        token = self.access_token_provider()
        self._post_json(
            f"{GMAIL_MESSAGES_URL}/{parse.quote(message_id)}/modify",
            {"removeLabelIds": ["UNREAD"]},
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )

    def _fetch_message(self, message_id: str, headers: dict[str, str]) -> GmailInboundMessage:
        payload = self._get_json(f"{GMAIL_MESSAGES_URL}/{parse.quote(message_id)}?format=full", headers)
        return _message_from_payload(payload, message_id)

    def _fetch_thread_messages(self, thread_id: str, headers: dict[str, str]) -> list[GmailInboundMessage]:
        payload = self._get_json(f"{GMAIL_THREADS_URL}/{parse.quote(thread_id)}?format=full", headers)
        return [_message_from_payload(message, str(message.get("id", ""))) for message in payload.get("messages", []) or []]


def _message_from_payload(payload: dict[str, Any], fallback_message_id: str) -> GmailInboundMessage:
    header_rows = payload.get("payload", {}).get("headers", []) or []
    header_map = {str(item.get("name", "")).lower(): str(item.get("value", "")) for item in header_rows}
    return GmailInboundMessage(
        message_id=str(payload.get("id", fallback_message_id)),
        thread_id=str(payload.get("threadId", "")),
        subject=header_map.get("subject", ""),
        from_email=header_map.get("from", ""),
        headers=header_map,
        body=_extract_body(payload.get("payload", {})),
        snippet=str(payload.get("snippet", "")),
        received_at=datetime.now(timezone.utc),
    )


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    req = request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    with request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    req = request.Request(url, headers=headers, method="GET")
    with request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _extract_body(payload: dict[str, Any]) -> str:
    parts = payload.get("parts") or []
    if payload.get("body", {}).get("data"):
        return _decode_base64url(str(payload["body"]["data"]))
    for part in parts:
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return _decode_base64url(str(part["body"]["data"]))
    return ""


def _decode_base64url(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}").decode("utf-8")
