from __future__ import annotations

import json
import os
from typing import Any, Callable
from urllib import parse, request


TOKEN_URL = "https://oauth2.googleapis.com/token"


def refresh_google_access_token(
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    refresh_token: str | None = None,
    http_post_form: Callable[[str, dict[str, str]], dict[str, Any]] | None = None,
) -> str:
    google_client_id = client_id or os.getenv("GOOGLE_CLIENT_ID", "")
    google_client_secret = client_secret or os.getenv("GOOGLE_CLIENT_SECRET", "")
    google_refresh_token = refresh_token or os.getenv("GOOGLE_REFRESH_TOKEN", "")
    if not google_client_id or not google_client_secret or not google_refresh_token:
        raise RuntimeError("GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and GOOGLE_REFRESH_TOKEN are required.")
    post_form = http_post_form or _post_form
    payload = post_form(
        TOKEN_URL,
        {
            "client_id": google_client_id,
            "client_secret": google_client_secret,
            "refresh_token": google_refresh_token,
            "grant_type": "refresh_token",
        },
    )
    access_token = str(payload.get("access_token", "")).strip()
    if not access_token:
        raise RuntimeError("Google OAuth refresh did not return an access token.")
    return access_token


def _post_form(url: str, form: dict[str, str]) -> dict[str, Any]:
    req = request.Request(
        url,
        data=parse.urlencode(form).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))

