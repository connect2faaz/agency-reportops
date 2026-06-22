from __future__ import annotations

import json
import os
from dataclasses import asdict
from json import JSONDecodeError
from typing import Any, Callable
from urllib import request

from .models import Client, MetricRow, QuestionAnswerOutput, ReportOutput


REPORT_SYSTEM_PROMPT = """You are a senior PPC account manager creating a monthly performance report for the client in the supplied JSON.

Using only the client and metrics data provided by the user, generate a professional client-facing report for the supplied reporting period or month range.

Requirements:
- Use clear, non-technical language where possible.
- Highlight key performance metrics.
- Compare performance to the previous month when available in the supplied metrics.
- Identify positive trends, negative trends, and noteworthy changes.
- Explain what the metrics mean for the business.
- Avoid simply repeating numbers. Provide insights.
- Keep the tone professional, concise, and confident.
- Do not make assumptions beyond the provided data.
- If previous-month data is not supplied, do not invent comparisons; state trends only from the available data.

Use this exact HTML structure for html_report so every email keeps the same brand/style consistency:
<div class="report-shell" style="font-family:Arial,sans-serif;color:#172033;line-height:1.5;max-width:680px;margin:0 auto;">
  <div class="report-header" style="border-bottom:1px solid #d9dee8;padding:0 0 16px 0;margin-bottom:20px;">
    <p class="eyebrow" style="font-size:12px;text-transform:uppercase;color:#697386;margin:0 0 6px 0;">Monthly PPC Performance Report</p>
    <h1 style="font-size:24px;line-height:1.25;margin:0;color:#111827;">[Client Name] - [Reporting Period]</h1>
  </div>
  <section class="executive-summary" style="margin-bottom:20px;">
    <h2 style="font-size:18px;margin:0 0 8px 0;color:#111827;">Executive Summary</h2>
    <p style="margin:0;">[Brief overview of overall performance and most important takeaways]</p>
  </section>
  <section class="campaign-performance" style="margin-bottom:20px;">
    <h2 style="font-size:18px;margin:0 0 8px 0;color:#111827;">Campaign Performance</h2>
    <table style="width:100%;border-collapse:collapse;font-size:14px;">[Rows for Ad Spend, Impressions, Clicks, CTR, Leads, CPL, Conversions, Conversion Rate, Revenue, ROAS]</table>
    <p style="margin:12px 0 0 0;">[Insightful performance analysis, not just repeated numbers]</p>
  </section>
  <section class="key-wins" style="margin-bottom:20px;">
    <h2 style="font-size:18px;margin:0 0 8px 0;color:#111827;">Key Wins</h2>
    <ul style="margin:0;padding-left:20px;">[List strongest improvements or achievements]</ul>
  </section>
  <section class="areas-for-attention" style="margin-bottom:20px;">
    <h2 style="font-size:18px;margin:0 0 8px 0;color:#111827;">Areas for Attention</h2>
    <ul style="margin:0;padding-left:20px;">[List declined metrics or optimization areas]</ul>
  </section>
  <section class="recommended-next-steps">
    <h2 style="font-size:18px;margin:0 0 8px 0;color:#111827;">Recommended Next Steps</h2>
    <ol style="margin:0;padding-left:20px;">[3-5 actionable recommendations]</ol>
  </section>
</div>

Preserve the section class names and inline style pattern. Replace bracketed placeholders with real content. Do not add new top-level sections unless review_notes explicitly request it.

Return strict JSON that satisfies the provided schema:
- executive_summary: concise summary string.
- highlights: list of key wins.
- concerns: list of areas for attention; use an empty list if there are no meaningful concerns.
- next_actions: list of 3-5 recommended next steps.
- html_report: polished client-facing HTML email body containing the full report structure above.
"""


QUESTION_ANSWER_SYSTEM_PROMPT = """You are a client Q&A assistant for an agency PPC reporting workflow.

Use only the approved report, client profile, and client question supplied by the user. Do not invent performance data, campaign details, guarantees, pricing, legal advice, or platform access details that are not present in the supplied context.

Classify the client reply and draft a concise HTML email answer.

Risk rules:
- low: factual explanation of the approved report, simple metric definitions, or pointing to a stated recommendation.
- high: guarantees, legal/compliance/medical/financial advice, refunds, contracts, complaints, metric discrepancy questions, unhappy-client language, budget commitments, strategy changes, unclear requests, or anything requiring account-manager judgment.
- ignore: non-question acknowledgements or unrelated content.

Return strict JSON that satisfies the provided schema:
- intent: one of question, acknowledgement, unrelated.
- risk_level: low, high, or ignore.
- risk_reason: short reason for the risk decision.
- answer_html: polished client-facing HTML answer. For high or ignore, still draft the safest possible internal-review answer.
- requires_am_review: true for high or ignore, false only for low.
"""


class StructuredOutputError(RuntimeError):
    pass


class OpenRouterClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "openai/gpt-oss-120b:free",
        base_url: str | None = None,
        http_post: Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]] | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("OPENROUTER_API_KEY", "")
        self.model = os.getenv("OPENROUTER_MODEL", model)
        self.base_url = (base_url or os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")).rstrip("/")
        self._http_post = http_post or self._post_json

    @classmethod
    def fake_report(cls) -> "OpenRouterClient":
        def post(_: str, __: dict[str, Any], ___: dict[str, str]) -> dict[str, Any]:
            schema_name = __.get("response_format", {}).get("json_schema", {}).get("name")
            if schema_name == "question_answer_output":
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "intent": "question",
                                        "risk_level": "low",
                                        "risk_reason": "The client asks about a metric in the approved report.",
                                        "answer_html": "<p>Search had the best ROAS based on the approved report.</p>",
                                        "requires_am_review": False,
                                    }
                                )
                            }
                        }
                    ]
                }
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "executive_summary": "Performance improved across the tracked period.",
                                    "highlights": ["ROAS remained healthy."],
                                    "concerns": ["Lead cost should continue to be monitored."],
                                    "next_actions": ["Prioritize the strongest channel next month."],
                                    "html_report": "<h1>Performance report</h1><p>ROAS remained healthy.</p>",
                                }
                            )
                        }
                    }
                ]
            }

        return cls(api_key="fake", http_post=post)

    @classmethod
    def fake_failure(cls) -> "OpenRouterClient":
        def post(_: str, __: dict[str, Any], ___: dict[str, str]) -> dict[str, Any]:
            return {"choices": [{"message": {"content": "{}"}}]}

        return cls(api_key="fake", http_post=post)

    def generate_report(self, client: Client, metrics: list[MetricRow], review_notes: list[str]) -> ReportOutput:
        if not self.api_key:
            raise StructuredOutputError("OpenRouter API key is missing; structured output cannot be generated.")
        payload = self._report_payload(client, metrics, review_notes)
        response = self._http_post(f"{self.base_url}/chat/completions", payload, self._headers())
        try:
            return self.parse_report_payload(response)
        except StructuredOutputError as first_error:
            retry_payload = self._report_payload(client, metrics, review_notes)
            retry_payload["messages"].append(
                {
                    "role": "user",
                    "content": (
                        f"Previous response was invalid: {first_error}. "
                        "Return only a JSON object that satisfies the schema. Do not return an empty object."
                    ),
                }
            )
            retry_response = self._http_post(f"{self.base_url}/chat/completions", retry_payload, self._headers())
            try:
                return self.parse_report_payload(retry_response)
            except StructuredOutputError as retry_error:
                repair_payload = self._report_repair_payload(client, metrics, review_notes, first_error, retry_error)
                repair_response = self._http_post(f"{self.base_url}/chat/completions", repair_payload, self._headers())
                try:
                    return self.parse_report_payload(repair_response)
                except StructuredOutputError as repair_error:
                    raise StructuredOutputError(
                        f"final repair attempt failed with: {repair_error}; "
                        f"retry attempt failed with: {retry_error}; "
                        f"first attempt also failed with: {first_error}"
                    ) from repair_error

    def _report_payload(self, client: Client, metrics: list[MetricRow], review_notes: list[str]) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": REPORT_SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "client": asdict(client),
                            "metrics": [asdict(metric) for metric in metrics],
                            "review_notes": review_notes,
                        },
                        default=str,
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "report_output",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["executive_summary", "highlights", "concerns", "next_actions", "html_report"],
                        "properties": {
                            "executive_summary": {"type": "string"},
                            "highlights": {"type": "array", "items": {"type": "string"}},
                            "concerns": {"type": "array", "items": {"type": "string"}},
                            "next_actions": {"type": "array", "items": {"type": "string"}},
                            "html_report": {"type": "string"},
                        },
                    },
                },
            },
        }
        return payload

    def _report_repair_payload(
        self,
        client: Client,
        metrics: list[MetricRow],
        review_notes: list[str],
        first_error: StructuredOutputError,
        retry_error: StructuredOutputError,
    ) -> dict[str, Any]:
        payload = self._report_payload(client, metrics, review_notes)
        payload["messages"].append(
            {
                "role": "user",
                "content": (
                    "FINAL REPAIR ATTEMPT.\n"
                    f"The first response failed validation with: {first_error}.\n"
                    f"The second response failed validation with: {retry_error}.\n"
                    "Return exactly one JSON object and nothing else. The object must include these five keys: "
                    "executive_summary, highlights, concerns, next_actions, html_report. "
                    "highlights, concerns, and next_actions must be arrays of strings. "
                    "concerns may be an empty array. html_report must be a non-empty string containing "
                    "the required div.report-shell email report HTML."
                ),
            }
        )
        return payload

    def draft_question_answer(self, client: Client, question: str, run_html: str) -> QuestionAnswerOutput:
        if not self.api_key:
            raise StructuredOutputError("OpenRouter API key is missing; question answer cannot be generated.")
        payload = self._question_answer_payload(client, question, run_html)
        response = self._http_post(f"{self.base_url}/chat/completions", payload, self._headers())
        try:
            return self.parse_question_answer_payload(response)
        except StructuredOutputError as first_error:
            retry_payload = self._question_answer_payload(client, question, run_html)
            retry_payload["messages"].append(
                {
                    "role": "user",
                    "content": (
                        f"Previous response was invalid: {first_error}. "
                        "Return only a JSON object that satisfies the schema. Do not return an empty object."
                    ),
                }
            )
            retry_response = self._http_post(f"{self.base_url}/chat/completions", retry_payload, self._headers())
            try:
                return self.parse_question_answer_payload(retry_response)
            except StructuredOutputError as retry_error:
                raise StructuredOutputError(f"{retry_error}; first attempt also failed with: {first_error}") from retry_error

    def _question_answer_payload(self, client: Client, question: str, run_html: str) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": QUESTION_ANSWER_SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "client": asdict(client),
                            "client_question": question,
                            "approved_report_html": run_html,
                        },
                        default=str,
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "question_answer_output",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["intent", "risk_level", "risk_reason", "answer_html", "requires_am_review"],
                        "properties": {
                            "intent": {"type": "string", "enum": ["question", "acknowledgement", "unrelated"]},
                            "risk_level": {"type": "string", "enum": ["low", "high", "ignore"]},
                            "risk_reason": {"type": "string"},
                            "answer_html": {"type": "string"},
                            "requires_am_review": {"type": "boolean"},
                        },
                    },
                },
            },
        }

    @staticmethod
    def parse_report_payload(payload: dict[str, Any]) -> ReportOutput:
        content: Any = payload
        choices = payload.get("choices")
        if choices:
            content = choices[0].get("message", {}).get("content", "")
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except JSONDecodeError as error:
                    raise StructuredOutputError("OpenRouter structured output was not valid JSON.") from error
        if not isinstance(content, dict):
            raise StructuredOutputError("OpenRouter structured output was not a JSON object.")
        required = ["executive_summary", "highlights", "concerns", "next_actions", "html_report"]
        missing = [
            key
            for key in required
            if key not in content or content[key] is None or (isinstance(content[key], str) and not content[key].strip())
        ]
        if missing:
            raise StructuredOutputError(f"OpenRouter structured output missing fields: {', '.join(missing)}")
        if not isinstance(content["highlights"], list) or not isinstance(content["concerns"], list):
            raise StructuredOutputError("OpenRouter structured output list fields were invalid.")
        return ReportOutput(
            executive_summary=str(content["executive_summary"]),
            highlights=[str(item) for item in content["highlights"]],
            concerns=[str(item) for item in content["concerns"]],
            next_actions=[str(item) for item in content["next_actions"]],
            html_report=str(content["html_report"]),
        )

    @staticmethod
    def parse_question_answer_payload(payload: dict[str, Any]) -> QuestionAnswerOutput:
        content: Any = payload
        choices = payload.get("choices")
        if choices:
            content = choices[0].get("message", {}).get("content", "")
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except JSONDecodeError as error:
                    raise StructuredOutputError("OpenRouter question answer output was not valid JSON.") from error
        if not isinstance(content, dict):
            raise StructuredOutputError("OpenRouter question answer output was not a JSON object.")
        required = ["intent", "risk_level", "risk_reason", "answer_html", "requires_am_review"]
        missing = [
            key
            for key in required
            if key not in content or content[key] is None or (isinstance(content[key], str) and not content[key].strip())
        ]
        if missing:
            raise StructuredOutputError(f"OpenRouter question answer output missing fields: {', '.join(missing)}")
        risk_level = str(content["risk_level"])
        intent = str(content["intent"])
        if intent not in {"question", "acknowledgement", "unrelated"}:
            raise StructuredOutputError("OpenRouter question answer intent was invalid.")
        if risk_level not in {"low", "high", "ignore"}:
            raise StructuredOutputError("OpenRouter question answer risk level was invalid.")
        if not isinstance(content["requires_am_review"], bool):
            raise StructuredOutputError("OpenRouter question answer review flag was invalid.")
        return QuestionAnswerOutput(
            intent=intent,
            risk_level=risk_level,
            risk_reason=str(content["risk_reason"]),
            answer_html=str(content["answer_html"]),
            requires_am_review=bool(content["requires_am_review"]),
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        req = request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        with request.urlopen(req, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
