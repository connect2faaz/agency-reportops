import unittest
from datetime import date
from unittest.mock import patch

import modal_app
from reportops.ai import OpenRouterClient
from reportops.models import Client, MetricRow
from reportops.workflow import InMemorySheetStore, ReportingWorkflow


class ModalEntrypointTests(unittest.TestCase):
    def test_modal_entrypoints_are_importable_without_modal_installed(self):
        self.assertTrue(callable(modal_app.run_daily_reports))
        self.assertTrue(callable(modal_app.poll_gmail_replies))
        self.assertTrue(callable(modal_app.process_gmail_push))
        self.assertTrue(callable(modal_app.renew_gmail_watch))
        self.assertTrue(callable(modal_app.gmail_pubsub_push))
        self.assertTrue(callable(modal_app.run_now))

    def test_schedule_plan_keeps_jobs_separate(self):
        self.assertEqual(
            modal_app.SCHEDULE_PLAN,
            {
                "poll_gmail_replies": "*/5 9-18 * * *",
                "run_daily_reports": "0 0 * * *",
                "renew_gmail_watch": "0 1 * * *",
                "run_now": None,
            },
        )
        self.assertFalse(hasattr(modal_app, "scheduled_tick"))

    def test_run_now_with_client_id_only_runs_that_client_even_when_others_are_due(self):
        store = InMemorySheetStore(
            clients=[
                Client("client_a", "Client A", "Ava", "a@example.com", "am@example.com", "monthly", date(2026, 1, 1)),
                Client("client_b", "Client B", "Bea", "b@example.com", "am@example.com", "monthly", date(2026, 1, 1)),
            ],
            metrics=[
                MetricRow("client_a", "Client A", "Feb-2026", 100, 1000, 100, 10, 5, 20, 2, 2, 500, 5),
                MetricRow("client_b", "Client B", "Feb-2026", 100, 1000, 100, 10, 5, 20, 2, 2, 500, 5),
            ],
        )
        store.flush = lambda: None
        workflow = ReportingWorkflow(store=store, ai=OpenRouterClient.fake_report(), gmail=store.gmail)

        with patch.object(modal_app, "_build_workflow", return_value=(workflow, store)):
            modal_app.run_now(client_id="client_a", period="Feb-2026")

        self.assertEqual([run.client_id for run in store.runs], ["client_a"])

    def test_process_gmail_push_runs_existing_reply_sync_and_flushes_store(self):
        class SyncCapturingWorkflow:
            def __init__(self):
                self.sync_count = 0

            def sync_replies(self):
                self.sync_count += 1

        class FlushCapturingStore:
            def __init__(self):
                self.flush_count = 0

            def flush(self):
                self.flush_count += 1

        workflow = SyncCapturingWorkflow()
        store = FlushCapturingStore()

        with patch.object(modal_app, "_build_workflow", return_value=(workflow, store)):
            modal_app.process_gmail_push({"email_address": "sender@example.com", "history_id": "12345"})

        self.assertEqual(workflow.sync_count, 1)
        self.assertEqual(store.flush_count, 1)

    def test_renew_gmail_watch_uses_configured_pubsub_topic(self):
        class WatchCapturingGmail:
            def __init__(self):
                self.topic_names = []

            def watch_mailbox(self, topic_name):
                self.topic_names.append(topic_name)
                return {"historyId": "12345", "expiration": "1760000000000"}

        gmail = WatchCapturingGmail()

        with patch.dict(modal_app.os.environ, {"GMAIL_PUBSUB_TOPIC": "projects/demo/topics/reportops"}, clear=False):
            result = modal_app.renew_gmail_watch(gmail_client=gmail)

        self.assertEqual(gmail.topic_names, ["projects/demo/topics/reportops"])
        self.assertEqual(result["historyId"], "12345")


if __name__ == "__main__":
    unittest.main()
