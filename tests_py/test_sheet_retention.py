from datetime import timedelta
import unittest

from reportops.gmail import InMemoryGmailClient
from reportops.models import MessageRecord, Run, RunStatus, utc_now
from reportops.sheets import MAX_SHEET_ROWS, GoogleSheetsApiClient, GoogleSheetsStore
from reportops.workflow import REPLY_RETENTION, ReportingWorkflow


def bare_store():
    """A GoogleSheetsStore with no network access, for exercising retention logic directly."""
    store = GoogleSheetsStore.__new__(GoogleSheetsStore)
    store.sheet_client = FakeSheetClient()
    store.clients, store.metrics, store.runs, store.questions, store.messages = [], [], [], [], []
    store.processed_message_ids = []
    store.gmail = InMemoryGmailClient()
    return store


class FakeSheetClient:
    """Records every read range and every written payload."""

    def __init__(self, tabs=None) -> None:
        self.tabs = tabs or {}
        self.written: dict[str, list[dict[str, str]]] = {}

    def read_rows(self, tab_name):
        return [dict(row) for row in self.tabs.get(tab_name, [])]

    def replace_rows(self, tab_name, rows):
        self.written[tab_name] = rows


class RecordingRequestClient(GoogleSheetsApiClient):
    """Exercises the real range building and padding without touching the network."""

    def __init__(self, existing_rows):
        self.requests = []
        self._existing_rows = existing_rows
        super().__init__(spreadsheet_id="sheet_1", access_token_provider=lambda: "token")

    def _request(self, method, range_name, payload=None):
        self.requests.append((method, range_name, payload))
        if method == "GET":
            return {"values": self._existing_rows}
        return {}


def marker(message_id, age_days):
    return MessageRecord(
        message_id=f"processed_{message_id}",
        run_id="",
        message_type="processed_reply",
        to="",
        subject="",
        gmail_message_id=message_id,
        gmail_thread_id="",
        status="processed",
        created_at=utc_now() - timedelta(days=age_days),
    )


def real_message(message_id, age_days):
    return MessageRecord(
        message_id=message_id,
        run_id="run_1",
        message_type="client_delivery",
        to="client@example.com",
        subject="May-2026 performance report",
        gmail_message_id=message_id,
        gmail_thread_id="thread_1",
        status="sent",
        created_at=utc_now() - timedelta(days=age_days),
    )


class SheetReadRangeTests(unittest.TestCase):
    def test_read_range_is_far_above_any_realistic_tab_size(self):
        client = RecordingRequestClient([["client_id"], ["client_1"]])
        client.read_rows("Clients")

        method, range_name, _ = client.requests[0]
        self.assertEqual(method, "GET")
        self.assertEqual(range_name, f"Clients!A1:Z{MAX_SHEET_ROWS}")
        self.assertGreaterEqual(MAX_SHEET_ROWS, 50000)

    def test_shrinking_a_tab_blanks_the_rows_it_no_longer_uses(self):
        existing = [["message_id", "run_id", "type", "to", "subject",
                     "gmail_message_id", "gmail_thread_id", "status", "created_at"]]
        existing += [[f"message_{index}", "", "processed_reply", "", "", f"g{index}", "", "processed", ""]
                     for index in range(9)]
        client = RecordingRequestClient(existing)
        client.read_rows("Messages")

        client.replace_rows("Messages", [{"message_id": "message_kept"}])

        _, range_name, payload = client.requests[-1]
        self.assertEqual(range_name, "Messages!A1")
        written = payload["values"]
        self.assertEqual(len(written), 10, "must cover the previous 10 rows, not just the 2 kept")
        self.assertEqual(written[1][0], "message_kept")
        self.assertTrue(
            all(cell == "" for row in written[2:] for cell in row),
            "rows beyond the new data must be blanked out",
        )


class ProcessedMarkerRetentionTests(unittest.TestCase):
    def build_store(self, messages):
        store = bare_store()
        store.messages = list(messages)
        store.processed_message_ids = [
            message.gmail_message_id for message in messages if message.message_type == "processed_reply"
        ]
        return store

    def test_old_markers_are_pruned_and_recent_ones_are_kept(self):
        store = self.build_store([marker("old_1", 200), marker("old_2", 91), marker("fresh", 5)])

        removed = store.prune_processed_markers()

        self.assertEqual(removed, 2)
        self.assertEqual(store.processed_message_ids, ["fresh"])

    def test_real_message_records_are_never_pruned(self):
        store = self.build_store([real_message("delivery_1", 400), marker("old", 400)])

        store.prune_processed_markers()

        kept_types = [message.message_type for message in store.messages]
        self.assertEqual(kept_types, ["client_delivery"], "audit rows must survive forever")

    def test_flush_prunes_before_writing(self):
        store = self.build_store([marker("old", 300), marker("fresh", 1)])

        store.flush()

        written = store.sheet_client.written["Messages"]
        self.assertEqual([row["gmail_message_id"] for row in written], ["fresh"])


class RetentionSafetyInvariantTests(unittest.TestCase):
    """A marker may only expire once its thread has stopped being polled."""

    def _thread_ids_for_run_age(self, age_days):
        store = bare_store()
        created = utc_now() - timedelta(days=age_days)
        store.runs = [
            Run(
                run_id="run_1",
                client_id="client_1",
                period="May-2026",
                status=RunStatus.CLIENT_DELIVERED,
                gmail_thread_id="am_thread",
                client_thread_id="client_thread",
                created_at=created,
                updated_at=utc_now(),
            )
        ]
        workflow = ReportingWorkflow(store=store, ai=None, gmail=store.gmail)
        return workflow._active_thread_ids()

    def test_recent_delivered_run_is_still_polled(self):
        self.assertEqual(self._thread_ids_for_run_age(10), ["client_thread"])

    def test_run_older_than_retention_is_no_longer_polled(self):
        self.assertEqual(self._thread_ids_for_run_age(REPLY_RETENTION.days + 5), [])

    def test_polling_window_matches_marker_retention_exactly(self):
        """If these ever diverge, pruning could resurrect an already-answered email."""
        stale_age = REPLY_RETENTION.days + 1
        self.assertEqual(self._thread_ids_for_run_age(stale_age), [])

        store = bare_store()
        store.messages = [marker("m1", stale_age)]
        store.processed_message_ids = ["m1"]
        self.assertEqual(store.prune_processed_markers(), 1)

    def test_a_chatty_thread_cannot_outlive_its_markers(self):
        """Window is measured from creation, so recent replies cannot keep an old run polled."""
        self.assertEqual(self._thread_ids_for_run_age(REPLY_RETENTION.days + 30), [])


if __name__ == "__main__":
    unittest.main()
