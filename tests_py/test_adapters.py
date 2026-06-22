from datetime import date, datetime, timezone
import unittest

from reportops.ai import OpenRouterClient, StructuredOutputError
from reportops.gmail_api import GmailApiClient
from reportops.models import Client, MessageRecord, MetricRow, Run, RunStatus
from reportops.pubsub import GmailPubSubAuthError, decode_gmail_pubsub_notification, handle_gmail_pubsub_push
from reportops.sheets import GoogleSheetsStore, SheetClient


class FakeSheetClient(SheetClient):
    def __init__(self) -> None:
        self.tabs = {
            "Clients": [
                {
                    "client_id": "client_1",
                    "client_name": "BrightSmile Dental",
                    "contact_name": "Ava",
                    "contact_email": "ava@example.com",
                    "account_manager_email": "am@example.com",
                    "cadence": "monthly",
                    "next_report_date": "2026-06-08",
                    "run_now": "",
                    "paused": "",
                }
            ],
            "Metrics": [
                {
                    "client_id": "client_1",
                    "client_name": "BrightSmile Dental",
                    "month": "Feb-2026",
                    "ad_spend": "3,450",
                    "impressions": "131000",
                    "clicks": "3290",
                    "ctr": "2.51",
                    "leads": "91",
                    "cpl": "37.91",
                    "conversions": "31",
                    "conversion_rate": "0.94",
                    "revenue": "20100",
                    "roas": "5.83",
                }
            ],
            "Runs": [],
            "Messages": [],
            "Questions": [],
        }

    def read_rows(self, tab_name: str) -> list[dict[str, str]]:
        return list(self.tabs[tab_name])

    def replace_rows(self, tab_name: str, rows: list[dict[str, str]]) -> None:
        self.tabs[tab_name] = rows


class GoogleSheetsStoreTests(unittest.TestCase):
    def test_loads_clients_and_metrics_from_named_tabs(self):
        store = GoogleSheetsStore(FakeSheetClient())

        self.assertEqual(store.clients[0].client_id, "client_1")
        self.assertEqual(store.clients[0].next_report_date, date(2026, 6, 8))
        self.assertEqual(store.metrics[0].ad_spend, 3450)

    def test_sheet_date_formatted_metric_month_matches_report_period(self):
        fake = FakeSheetClient()
        fake.tabs["Metrics"][0]["month"] = "2/1/2026"
        store = GoogleSheetsStore(fake)

        metrics = store.metrics_for_report_period("client_1", "Feb-2026")

        self.assertEqual([metric.month for metric in metrics], ["Feb-2026"])

    def test_upserts_runs_and_messages_back_to_sheet_rows(self):
        fake = FakeSheetClient()
        store = GoogleSheetsStore(fake)

        store.runs.append(
            Run(
                run_id="run_1",
                client_id="client_1",
                period="Feb-2026",
                status=RunStatus.AM_REVIEW,
                html_report="<h1>Report</h1>",
                gmail_thread_id="thread_1",
            )
        )
        store.save_message(
            MessageRecord(
                message_id="message_1",
                run_id="run_1",
                message_type="account_manager_review",
                to="am@example.com",
                subject="Ready",
                gmail_message_id="gmail_1",
                gmail_thread_id="thread_1",
                status="sent",
            )
        )
        store.flush()

        self.assertEqual(fake.tabs["Runs"][0]["run_id"], "run_1")
        self.assertEqual(fake.tabs["Runs"][0]["status"], "am_review")
        self.assertEqual(fake.tabs["Messages"][0]["gmail_message_id"], "gmail_1")

    def test_persists_run_last_am_review_sent_at(self):
        fake = FakeSheetClient()
        fake.tabs["Runs"] = [
            {
                "run_id": "run_1",
                "client_id": "client_1",
                "period": "Feb-2026",
                "status": "am_review",
                "attempt_count": "1",
                "last_am_review_sent_at": "2026-06-09T00:00:00+00:00",
            }
        ]
        store = GoogleSheetsStore(fake)

        self.assertEqual(store.runs[0].last_am_review_sent_at, datetime(2026, 6, 9, tzinfo=timezone.utc))

        store.flush()

        self.assertEqual(fake.tabs["Runs"][0]["last_am_review_sent_at"], "2026-06-09T00:00:00+00:00")


class GmailApiClientTests(unittest.TestCase):
    def test_send_html_posts_gmail_raw_message(self):
        calls = []

        def post(url, payload, headers):
            calls.append((url, payload, headers))
            return {"id": "gmail_1", "threadId": "thread_1"}

        client = GmailApiClient(sender_email="sender@example.com", access_token_provider=lambda: "token", post_json=post)

        result = client.send_html(
            to="am@example.com",
            subject="Ready",
            html_body="<h1>Report</h1>",
            headers={"X-ReportOps-Run-Id": "run_1"},
        )

        self.assertEqual(result, {"id": "gmail_1", "thread_id": "thread_1"})
        self.assertIn("/messages/send", calls[0][0])
        self.assertIn("raw", calls[0][1])
        self.assertEqual(calls[0][2]["Authorization"], "Bearer token")

    def test_send_html_can_attach_to_existing_gmail_thread(self):
        calls = []

        def post(url, payload, headers):
            calls.append((url, payload, headers))
            return {"id": "gmail_reply", "threadId": "thread_1"}

        client = GmailApiClient(sender_email="sender@example.com", access_token_provider=lambda: "token", post_json=post)

        result = client.send_html(
            to="ava@example.com",
            subject="Re: performance report",
            html_body="<p>ROAS means return on ad spend.</p>",
            headers={"X-ReportOps-Run-Id": "run_1"},
            thread_id="thread_1",
        )

        self.assertEqual(result, {"id": "gmail_reply", "thread_id": "thread_1"})
        self.assertEqual(calls[0][1]["threadId"], "thread_1")

    def test_list_recent_replies_decodes_plain_text_body(self):
        listing_urls = []

        def get(url, headers):
            if "format=full" not in url:
                listing_urls.append(url)
                return {"messages": [{"id": "gmail_1"}]}
            return {
                "id": "gmail_1",
                "threadId": "thread_1",
                "snippet": "Approved",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Re: performance report"},
                        {"name": "From", "value": "am@example.com"},
                    ],
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "body": {"data": "QXBwcm92ZWQgdG8gc2VuZA"},
                        }
                    ],
                },
            }

        client = GmailApiClient(sender_email="sender@example.com", access_token_provider=lambda: "token", get_json=get)

        replies = client.list_recent_replies()

        self.assertEqual(replies[0].body, "Approved to send")
        self.assertIn("newer_than%3A30d", listing_urls[0])
        self.assertIn("report+ready+for+review", listing_urls[0])
        self.assertNotIn("OR+newer_than%3A14d", listing_urls[0])

    def test_list_recent_replies_fetches_only_requested_threads(self):
        fetched_urls = []

        def get(url, headers):
            fetched_urls.append(url)
            return {
                "id": "thread_1",
                "messages": [
                    {
                        "id": "gmail_1",
                        "threadId": "thread_1",
                        "snippet": "Approved",
                        "payload": {
                            "headers": [
                                {"name": "Subject", "value": "Re: BrightSmile report ready for review"},
                                {"name": "From", "value": "am@example.com"},
                            ],
                            "parts": [
                                {
                                    "mimeType": "text/plain",
                                    "body": {"data": "QXBwcm92ZWQ"},
                                }
                            ],
                        },
                    }
                ],
            }

        client = GmailApiClient(sender_email="sender@example.com", access_token_provider=lambda: "token", get_json=get)

        replies = client.list_recent_replies(thread_ids=["thread_1"])

        self.assertEqual(replies[0].thread_id, "thread_1")
        self.assertEqual(replies[0].body, "Approved")
        self.assertIn("/threads/thread_1", fetched_urls[0])
        self.assertNotIn("/messages?", fetched_urls[0])

    def test_watch_mailbox_posts_gmail_watch_payload(self):
        calls = []

        def post(url, payload, headers):
            calls.append((url, payload, headers))
            return {"historyId": "1234567890", "expiration": "1760000000000"}

        client = GmailApiClient(sender_email="sender@example.com", access_token_provider=lambda: "token", post_json=post)

        result = client.watch_mailbox("projects/demo/topics/gmail-replies")

        self.assertEqual(result, {"historyId": "1234567890", "expiration": "1760000000000"})
        self.assertTrue(calls[0][0].endswith("/watch"))
        self.assertEqual(
            calls[0][1],
            {
                "topicName": "projects/demo/topics/gmail-replies",
                "labelIds": ["INBOX"],
                "labelFilterBehavior": "INCLUDE",
            },
        )
        self.assertEqual(calls[0][2]["Authorization"], "Bearer token")


class GmailPubSubTests(unittest.TestCase):
    def test_decodes_gmail_pubsub_notification_data(self):
        payload = {
            "message": {
                "data": "eyJlbWFpbEFkZHJlc3MiOiAic2VuZGVyQGV4YW1wbGUuY29tIiwgImhpc3RvcnlJZCI6ICIxMjM0NSJ9",
                "messageId": "pubsub_1",
                "publishTime": "2026-06-12T12:00:00Z",
            },
            "subscription": "projects/demo/subscriptions/reportops",
        }

        notification = decode_gmail_pubsub_notification(payload)

        self.assertEqual(notification.email_address, "sender@example.com")
        self.assertEqual(notification.history_id, "12345")
        self.assertEqual(notification.pubsub_message_id, "pubsub_1")

    def test_rejects_missing_pubsub_data(self):
        with self.assertRaises(ValueError):
            decode_gmail_pubsub_notification({"message": {"messageId": "pubsub_1"}})

    def test_rejects_bad_pubsub_data(self):
        with self.assertRaises(ValueError):
            decode_gmail_pubsub_notification({"message": {"data": "not-valid-json"}})

    def test_token_mismatch_rejects_without_spawning_processor(self):
        spawned = []

        with self.assertRaises(GmailPubSubAuthError):
            handle_gmail_pubsub_push(
                {"message": {"data": "e30"}},
                token="wrong",
                expected_token="secret",
                spawn_processor=lambda _: spawned.append("spawned"),
            )

        self.assertEqual(spawned, [])

    def test_valid_token_spawns_processor_after_decoding_notification(self):
        spawned = []
        payload = {
            "message": {
                "data": "eyJlbWFpbEFkZHJlc3MiOiAic2VuZGVyQGV4YW1wbGUuY29tIiwgImhpc3RvcnlJZCI6ICIxMjM0NSJ9",
                "messageId": "pubsub_1",
            }
        }

        response = handle_gmail_pubsub_push(
            payload,
            token="secret",
            expected_token="secret",
            spawn_processor=lambda notification: spawned.append(notification),
        )

        self.assertEqual(response["status"], "accepted")
        self.assertEqual(response["history_id"], "12345")
        self.assertEqual(spawned[0].email_address, "sender@example.com")


class OpenRouterClientTests(unittest.TestCase):
    def test_report_payload_uses_ppc_account_manager_prompt(self):
        payload = OpenRouterClient(api_key="fake")._report_payload(
            Client("client_1", "BrightSmile Dental", "Ava", "ava@example.com", "am@example.com", "monthly", date(2026, 6, 8)),
            [MetricRow("client_1", "BrightSmile Dental", "Feb-2026", 1, 1, 1, 1, 1, 1, 1, 1, 1, 1)],
            [],
        )

        system_prompt = payload["messages"][0]["content"]
        self.assertIn("senior PPC account manager", system_prompt)
        self.assertIn("Compare performance to the previous month when available", system_prompt)
        self.assertIn("Monthly PPC Performance Report", system_prompt)
        self.assertIn("Return strict JSON", system_prompt)
        self.assertIn("html_report", system_prompt)
        self.assertIn("Use this exact HTML structure", system_prompt)
        self.assertIn("report-shell", system_prompt)

    def test_report_payload_allows_empty_concerns_list(self):
        output = OpenRouterClient.parse_report_payload(
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"executive_summary":"Summary","highlights":["Win"],'
                                '"concerns":[],"next_actions":["Action"],'
                                '"html_report":"<h1>Report</h1>"}'
                            )
                        }
                    }
                ]
            }
        )

        self.assertEqual(output.concerns, [])

    def test_generate_report_retries_once_when_first_structured_output_is_empty(self):
        calls = []

        def post(url, payload, headers):
            calls.append(payload)
            if len(calls) == 1:
                return {"choices": [{"message": {"content": "{}"}}]}
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"executive_summary":"Summary","highlights":["Win"],'
                                '"concerns":["Concern"],"next_actions":["Action"],'
                                '"html_report":"<h1>Report</h1>"}'
                            )
                        }
                    }
                ]
            }

        client = OpenRouterClient(api_key="fake", http_post=post)
        output = client.generate_report(
            Client(
                client_id="client_1",
                client_name="BrightSmile Dental",
                contact_name="Ava",
                contact_email="ava@example.com",
                account_manager_email="am@example.com",
                cadence="monthly",
                next_report_date=date(2026, 6, 8),
            ),
            [
                MetricRow(
                    client_id="client_1",
                    client_name="BrightSmile Dental",
                    month="Feb-2026",
                    ad_spend=3450,
                    impressions=131000,
                    clicks=3290,
                    ctr=2.51,
                    leads=91,
                    cpl=37.91,
                    conversions=31,
                    conversion_rate=0.94,
                    revenue=20100,
                    roas=5.83,
                )
            ],
            [],
        )

        self.assertEqual(output.executive_summary, "Summary")
        self.assertEqual(len(calls), 2)
        self.assertIn("Previous response was invalid", calls[1]["messages"][-1]["content"])

    def test_generate_report_retries_when_first_response_is_not_json(self):
        calls = []

        def post(url, payload, headers):
            calls.append(payload)
            if len(calls) == 1:
                return {"choices": [{"message": {"content": "I cannot generate that."}}]}
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"executive_summary":"Summary","highlights":["Win"],'
                                '"concerns":[],"next_actions":["Action"],'
                                '"html_report":"<h1>Report</h1>"}'
                            )
                        }
                    }
                ]
            }

        client = OpenRouterClient(api_key="fake", http_post=post)
        output = client.generate_report(
            Client("client_1", "BrightSmile Dental", "Ava", "ava@example.com", "am@example.com", "monthly", date(2026, 6, 8)),
            [MetricRow("client_1", "BrightSmile Dental", "Feb-2026", 1, 1, 1, 1, 1, 1, 1, 1, 1, 1)],
            [],
        )

        self.assertEqual(output.html_report, "<h1>Report</h1>")
        self.assertEqual(len(calls), 2)

    def test_generate_report_uses_final_repair_prompt_after_two_invalid_outputs(self):
        calls = []

        def post(url, payload, headers):
            calls.append(payload)
            if len(calls) < 3:
                return {"choices": [{"message": {"content": "{}"}}]}
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"executive_summary":"Summary","highlights":["Win"],'
                                '"concerns":[],"next_actions":["Action"],'
                                '"html_report":"<h1>Report</h1>"}'
                            )
                        }
                    }
                ]
            }

        client = OpenRouterClient(api_key="fake", http_post=post)
        output = client.generate_report(
            Client("client_1", "BrightSmile Dental", "Ava", "ava@example.com", "am@example.com", "monthly", date(2026, 6, 8)),
            [MetricRow("client_1", "BrightSmile Dental", "Feb-2026", 1, 1, 1, 1, 1, 1, 1, 1, 1, 1)],
            [],
        )

        self.assertEqual(output.html_report, "<h1>Report</h1>")
        self.assertEqual(len(calls), 3)
        self.assertIn("FINAL REPAIR ATTEMPT", calls[2]["messages"][-1]["content"])
        self.assertIn("executive_summary", calls[2]["messages"][-1]["content"])

    def test_generate_report_blocks_after_final_repair_attempt_fails(self):
        calls = []

        def post(url, payload, headers):
            calls.append(payload)
            return {"choices": [{"message": {"content": "{}"}}]}

        client = OpenRouterClient(api_key="fake", http_post=post)

        with self.assertRaises(StructuredOutputError) as context:
            client.generate_report(
                Client("client_1", "BrightSmile Dental", "Ava", "ava@example.com", "am@example.com", "monthly", date(2026, 6, 8)),
                [MetricRow("client_1", "BrightSmile Dental", "Feb-2026", 1, 1, 1, 1, 1, 1, 1, 1, 1, 1)],
                [],
            )

        self.assertEqual(len(calls), 3)
        self.assertIn("final repair attempt failed", str(context.exception))
        self.assertIn("first attempt also failed", str(context.exception))

    def test_question_answer_payload_uses_grounded_client_qna_prompt(self):
        payload = OpenRouterClient(api_key="fake")._question_answer_payload(
            Client("client_1", "BrightSmile Dental", "Ava", "ava@example.com", "am@example.com", "monthly", date(2026, 6, 8)),
            "Which channel had the best ROAS?",
            "<h1>Report</h1><p>ROAS was strongest on search.</p>",
        )

        system_prompt = payload["messages"][0]["content"]
        self.assertIn("client Q&A assistant", system_prompt)
        self.assertIn("Use only the approved report", system_prompt)
        self.assertIn("requires_am_review", system_prompt)
        self.assertEqual(payload["response_format"]["json_schema"]["name"], "question_answer_output")
        self.assertIn("complaints", system_prompt)
        self.assertIn("metric discrepancy", system_prompt)

    def test_question_answer_payload_parses_structured_low_risk_answer(self):
        output = OpenRouterClient.parse_question_answer_payload(
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"intent":"question","risk_level":"low",'
                                '"risk_reason":"The client asks for a report metric explanation.",'
                                '"answer_html":"<p>Search had the best ROAS.</p>",'
                                '"requires_am_review":false}'
                            )
                        }
                    }
                ]
            }
        )

        self.assertEqual(output.risk_level, "low")
        self.assertFalse(output.requires_am_review)
        self.assertIn("Search had the best ROAS", output.answer_html)

    def test_question_answer_payload_rejects_empty_answer(self):
        with self.assertRaises(StructuredOutputError):
            OpenRouterClient.parse_question_answer_payload(
                {
                    "intent": "question",
                    "risk_level": "low",
                    "risk_reason": "Safe question.",
                    "answer_html": "",
                    "requires_am_review": False,
                }
            )


if __name__ == "__main__":
    unittest.main()
