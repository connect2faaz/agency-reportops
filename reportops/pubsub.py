from __future__ import annotations

import base64
import json
from dataclasses import asdict, dataclass
from typing import Any, Callable


@dataclass(frozen=True, slots=True)
class GmailPubSubNotification:
    email_address: str
    history_id: str
    pubsub_message_id: str = ""
    publish_time: str = ""
    subscription: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


class GmailPubSubAuthError(PermissionError):
    pass


def decode_gmail_pubsub_notification(payload: dict[str, Any]) -> GmailPubSubNotification:
    message = payload.get("message")
    if not isinstance(message, dict):
        raise ValueError("Pub/Sub payload is missing message.")
    encoded_data = message.get("data")
    if not isinstance(encoded_data, str) or not encoded_data:
        raise ValueError("Pub/Sub payload is missing message.data.")
    try:
        decoded = _decode_base64url_json(encoded_data)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("Pub/Sub message.data is not valid Gmail notification JSON.") from error
    email_address = str(decoded.get("emailAddress", "")).strip()
    history_id = str(decoded.get("historyId", "")).strip()
    if not email_address or not history_id:
        raise ValueError("Gmail Pub/Sub notification is missing emailAddress or historyId.")
    return GmailPubSubNotification(
        email_address=email_address,
        history_id=history_id,
        pubsub_message_id=str(message.get("messageId", "")),
        publish_time=str(message.get("publishTime", "")),
        subscription=str(payload.get("subscription", "")),
    )


def handle_gmail_pubsub_push(
    payload: dict[str, Any],
    *,
    token: str | None,
    expected_token: str | None,
    spawn_processor: Callable[[GmailPubSubNotification], Any],
) -> dict[str, str]:
    if not expected_token:
        raise GmailPubSubAuthError("GMAIL_PUBSUB_TOKEN is not configured.")
    if token != expected_token:
        raise GmailPubSubAuthError("Invalid Gmail Pub/Sub token.")
    notification = decode_gmail_pubsub_notification(payload)
    spawn_processor(notification)
    return {
        "status": "accepted",
        "email_address": notification.email_address,
        "history_id": notification.history_id,
    }


def _decode_base64url_json(value: str) -> dict[str, Any]:
    padding = "=" * (-len(value) % 4)
    decoded = base64.urlsafe_b64decode(f"{value}{padding}").decode("utf-8")
    parsed = json.loads(decoded)
    if not isinstance(parsed, dict):
        raise ValueError("Decoded Pub/Sub payload is not a JSON object.")
    return parsed
