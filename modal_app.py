from __future__ import annotations

from datetime import date
import os

from reportops.ai import OpenRouterClient
from reportops.gmail_api import GmailApiClient
from reportops.pubsub import GmailPubSubAuthError, GmailPubSubNotification, handle_gmail_pubsub_push
from reportops.sheets import GoogleSheetsApiClient, GoogleSheetsStore
from reportops.workflow import ReportingWorkflow


SCHEDULE_PLAN = {
    "poll_gmail_replies": "*/5 9-18 * * *",
    "run_daily_reports": "0 0 * * *",
    "renew_gmail_watch": "0 1 * * *",
    "run_now": None,
}

FUNCTION_TIMEOUTS = {
    "run_now": 3600,
}


try:
    import modal
except ModuleNotFoundError:
    modal = None


def _noop_decorator(func=None, *_args, **_kwargs):
    def decorate(func):
        return func

    if callable(func) and not _args and not _kwargs:
        return func
    return decorate


if modal is not None:
    app = modal.App("reportops-headless")
    image = modal.Image.debian_slim().pip_install("fastapi[standard]").add_local_python_source("reportops")
    secret = modal.Secret.from_name("reportops-secrets")

    def modal_function(**kwargs):
        return app.function(image=image, secrets=[secret], **kwargs)

    if os.getenv("REPORTOPS_DEPLOY_SCHEDULES", "1") == "0":
        poll_schedule = modal_function()
        daily_schedule = modal_function()
        watch_schedule = modal_function()
    else:
        poll_schedule = modal_function(schedule=modal.Cron(SCHEDULE_PLAN["poll_gmail_replies"]))
        daily_schedule = modal_function(schedule=modal.Cron(SCHEDULE_PLAN["run_daily_reports"]))
        watch_schedule = modal_function(schedule=modal.Cron(SCHEDULE_PLAN["renew_gmail_watch"]))
    pubsub_endpoint = modal.fastapi_endpoint(method="POST")
else:
    app = None
    modal_function = _noop_decorator
    daily_schedule = _noop_decorator
    poll_schedule = _noop_decorator
    watch_schedule = _noop_decorator
    pubsub_endpoint = _noop_decorator


def _build_workflow() -> tuple[ReportingWorkflow, GoogleSheetsStore]:
    store = GoogleSheetsStore(GoogleSheetsApiClient())
    gmail = GmailApiClient()
    workflow = ReportingWorkflow(store=store, ai=OpenRouterClient(), gmail=gmail)
    return workflow, store


@daily_schedule
def run_daily_reports(period: str | None = None) -> None:
    workflow, store = _build_workflow()
    workflow.run_due_reports(today=date.today(), period=period)
    store.flush()


@poll_schedule
def poll_gmail_replies() -> None:
    workflow, store = _build_workflow()
    workflow.sync_replies()
    store.flush()


@modal_function()
def process_gmail_push(notification: dict[str, str] | None = None) -> None:
    print(f"Processing Gmail Pub/Sub notification: {notification or {}}")
    workflow, store = _build_workflow()
    workflow.sync_replies()
    store.flush()


@watch_schedule
def renew_gmail_watch(gmail_client=None) -> dict:
    topic_name = os.getenv("GMAIL_PUBSUB_TOPIC", "").strip()
    if not topic_name:
        raise RuntimeError("GMAIL_PUBSUB_TOPIC is required to renew Gmail push notifications.")
    gmail = gmail_client or GmailApiClient()
    result = gmail.watch_mailbox(topic_name)
    print(f"Renewed Gmail watch for topic {topic_name}: {result}")
    return result


@modal_function()
@pubsub_endpoint
def gmail_pubsub_push(item: dict, token: str | None = None) -> dict[str, str]:
    try:
        return handle_gmail_pubsub_push(
            item,
            token=token,
            expected_token=os.getenv("GMAIL_PUBSUB_TOKEN"),
            spawn_processor=_spawn_process_gmail_push,
        )
    except GmailPubSubAuthError as error:
        raise _http_error(403, str(error)) from error
    except ValueError as error:
        raise _http_error(400, str(error)) from error


@modal_function(timeout=FUNCTION_TIMEOUTS["run_now"])
def run_now(client_id: str | None = None, period: str | None = None) -> None:
    workflow, store = _build_workflow()
    report_period = period or os.getenv("REPORTOPS_PERIOD")
    if client_id:
        workflow.run_client_report(client_id, today=date.today(), period=report_period)
    else:
        for client in store.clients:
            if not client.paused:
                workflow.run_client_report(client.client_id, today=date.today(), period=report_period)
    store.flush()


def _spawn_process_gmail_push(notification: GmailPubSubNotification) -> None:
    payload = notification.to_dict()
    spawn = getattr(process_gmail_push, "spawn", None)
    if callable(spawn):
        spawn(payload)
    else:
        process_gmail_push(payload)


def _http_error(status_code: int, detail: str) -> Exception:
    try:
        from fastapi import HTTPException
    except ModuleNotFoundError:
        return RuntimeError(f"HTTP {status_code}: {detail}")
    return HTTPException(status_code=status_code, detail=detail)
