from __future__ import annotations

import json
import os
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib import parse, request


TOKEN_URL = "https://oauth2.googleapis.com/token"
TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}


def refresh_google_access_token(
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    refresh_token: str | None = None,
    http_post_form: Callable[[str, dict[str, str]], dict[str, Any]] | None = None,
    retry_delays: tuple[float, ...] = (1.0, 3.0),
) -> str:
    google_client_id = client_id or os.getenv("GOOGLE_CLIENT_ID", "")
    google_client_secret = client_secret or os.getenv("GOOGLE_CLIENT_SECRET", "")
    google_refresh_token = refresh_token or os.getenv("GOOGLE_REFRESH_TOKEN", "")
    if not google_client_id or not google_client_secret or not google_refresh_token:
        raise RuntimeError("GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and GOOGLE_REFRESH_TOKEN are required.")
    post_form = http_post_form or _post_form
    form = {
        "client_id": google_client_id,
        "client_secret": google_client_secret,
        "refresh_token": google_refresh_token,
        "grant_type": "refresh_token",
    }
    payload = _post_form_with_retries(post_form, TOKEN_URL, form, retry_delays)
    access_token = str(payload.get("access_token", "")).strip()
    if not access_token:
        raise RuntimeError("Google OAuth refresh did not return an access token.")
    return access_token


def _post_form_with_retries(
    post_form: Callable[[str, dict[str, str]], dict[str, Any]],
    url: str,
    form: dict[str, str],
    retry_delays: tuple[float, ...],
) -> dict[str, Any]:
    max_attempts = len(retry_delays) + 1
    for attempt in range(1, max_attempts + 1):
        try:
            return post_form(url, form)
        except HTTPError as error:
            _close_http_error(error)
            if error.code in TRANSIENT_HTTP_STATUSES and attempt < max_attempts:
                _sleep_before_retry(retry_delays[attempt - 1])
                continue
            raise RuntimeError(
                f"Google OAuth refresh failed after {attempt} attempts: HTTP {error.code} {error.reason}"
            ) from None
        except URLError as error:
            if attempt < max_attempts:
                _sleep_before_retry(retry_delays[attempt - 1])
                continue
            raise RuntimeError(f"Google OAuth refresh failed after {attempt} attempts: {error.reason}") from None
    raise RuntimeError("Google OAuth refresh failed before receiving a response.")


def _sleep_before_retry(delay_seconds: float) -> None:
    if delay_seconds > 0:
        time.sleep(delay_seconds)


def _close_http_error(error: HTTPError) -> None:
    try:
        error.close()
    except Exception:
        pass


def _post_form(url: str, form: dict[str, str]) -> dict[str, Any]:
    req = request.Request(
        url,
        data=parse.urlencode(form).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))
