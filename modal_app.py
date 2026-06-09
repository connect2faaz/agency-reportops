from __future__ import annotations

from datetime import date
import os

from reportops.ai import OpenRouterClient
from reportops.gmail_api import GmailApiClient
from reportops.sheets import GoogleSheetsApiClient, GoogleSheetsStore
from reportops.workflow import ReportingWorkflow


SCHEDULE_PLAN = {
    "poll_gmail_replies": "*/5 9-18 * * *",
    "run_daily_reports": "0 0 * * *",
    "run_now": None,
}


try:
    import modal
except ModuleNotFoundError:
    modal = None


def _noop_decorator(*_args, **_kwargs):
    def decorate(func):
        return func

    return decorate


if modal is not None:
    app = modal.App("reportops-headless")
    image = modal.Image.debian_slim().add_local_python_source("reportops")
    secret = modal.Secret.from_name("reportops-secrets")

    def modal_function(**kwargs):
        return app.function(image=image, secrets=[secret], **kwargs)

    if os.getenv("REPORTOPS_DEPLOY_SCHEDULES", "1") == "0":
        poll_schedule = modal_function()
        daily_schedule = modal_function()
    else:
        poll_schedule = modal_function(schedule=modal.Cron(SCHEDULE_PLAN["poll_gmail_replies"]))
        daily_schedule = modal_function(schedule=modal.Cron(SCHEDULE_PLAN["run_daily_reports"]))
else:
    app = None
    modal_function = _noop_decorator
    daily_schedule = _noop_decorator
    poll_schedule = _noop_decorator


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
def run_now(client_id: str | None = None, period: str | None = None) -> None:
    workflow, store = _build_workflow()
    if client_id:
        workflow.run_client_report(client_id, today=date.today(), period=period or os.getenv("REPORTOPS_PERIOD"))
    else:
        for client in store.clients:
            client.run_now = True
        workflow.run_due_reports(today=date.today(), period=period or os.getenv("REPORTOPS_PERIOD"))
    store.flush()
