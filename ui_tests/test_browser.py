"""Browser smoke tests with fictitious identity, evidence and email. No Google calls."""

import json
import unittest
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
REPORT = {
    "report_id": "fixture-report", "filename": "identity-risk.xlsx", "format": "xlsx",
    "download_url": "/web/reports/fixture-report",
    "mime_type": "application/octet-stream",
}
GOOGLE_STUB = """
window.google = {accounts: {
  id: {initialize(config) {setTimeout(() => {
    config.callback({credential: 'fixture.' +
      btoa(JSON.stringify({email: 'analyst@example.com'})) + '.fixture'});
  });}, renderButton() {}, disableAutoSelect() {}},
  oauth2: {initTokenClient(config) {return {requestAccessToken() {
    config.callback({access_token: 'fixture-token'});
  }};}, hasGrantedAllScopes() {return true;}}
}};
"""


class BrowserWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = sync_playwright().start()
        cls.browser = cls.runtime.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.runtime.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.requests = []
        self.page_errors = []
        self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        self.page.add_init_script(GOOGLE_STUB)
        self.page.route("**/*", self.route)
        self.page.goto("http://localhost:8765")
        expect(
            self.page.get_by_placeholder("Ask anything about the IAM analytics data…")
        ).to_be_enabled()

    def tearDown(self):
        self.context.close()

    def route(self, route):
        request = route.request
        if not request.url.startswith("http://localhost:8765"):
            route.abort()  # Never load the live Google client or call a real mailbox.
            return
        if request.method == "GET":
            route.fulfill(
                content_type="text/html", body=(ROOT / "iam_agent/web_ui.html").read_text(),
            )
            return
        data = request.post_data_json
        self.requests.append((request.url, data))
        if request.url.endswith("/web/messages"):
            if "email" in data["text"].lower():
                payload = {
                    "text": "Draft ready. Enter a recipient and preview.", "email_draft": {
                        "draft_id": "fixture-draft", "recipient": "",
                        "subject": "Identity risk review",
                        "body": "Please review the historical risk analysis in the spreadsheet.",
                        "available_attachments": [REPORT],
                        "attachment_ids": [REPORT["report_id"]],
                    },
                }
            elif "download" in data["text"].lower():
                payload = {
                    "text": "The saved analysis is ready to download.",
                    "reports": [REPORT], "result_id": "fixture-result",
                    "suggested_questions": ["Draft an email with this Excel"],
                }
            else:
                payload = {
                    "text": "user12 has an observed risk score of 91.",
                    "result_id": "fixture-result",
                    "suggested_questions": ["Download this as Excel", "Why is user12 risky?"],
                }
        elif request.url.endswith("/preview"):
            payload = {
                **data, "draft_id": "fixture-draft", "review_token": "fixture-review",
                "available_attachments": [REPORT],
                "attachments": [
                    {"filename": REPORT["filename"], "size_bytes": 2048},
                ] if data["attachment_ids"] else [],
            }
        elif request.url.endswith("/send"):
            payload = {"state": "sent", "message_id": "fixture-only-no-real-send"}
        else:
            route.fulfill(status=404, body="Unknown test endpoint")
            return
        route.fulfill(content_type="application/json", body=json.dumps(payload))

    def ask(self, text):
        self.page.get_by_placeholder("Ask anything about the IAM analytics data…").fill(text)
        self.page.get_by_role("button", name="Send", exact=True).click()

    def test_suggestion_posts_immediately_with_correct_result_anchor(self):
        self.ask("Who is riskiest?")
        self.page.get_by_role("button", name="Download this as Excel", exact=True).click()
        expect(self.page.get_by_role("button", name="Download Excel", exact=True)).to_be_visible()
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[1][1]["text"], "Download this as Excel")
        self.assertEqual(self.requests[1][1]["source_result_id"], "fixture-result")
        self.assertEqual(
            self.requests[0][1]["conversation_id"], self.requests[1][1]["conversation_id"],
        )
        self.assertFalse(self.page_errors)

    def test_email_requires_recipient_then_preview_then_explicit_send(self):
        self.ask("Draft an email with the previous Excel")
        expect(self.page.get_by_role("button", name="Preview email")).to_be_visible()
        expect(self.page.get_by_role("button", name="Send email", exact=True)).to_have_count(0)
        self.page.get_by_role("button", name="Preview email").click()
        self.assertFalse(any(url.endswith("/preview") for url, _ in self.requests))
        self.page.get_by_label("To — who should receive this?").fill("recipient@example.com")
        self.page.get_by_label("Subject", exact=True).fill("Reviewed subject")
        self.page.get_by_role("button", name="Preview email").click()
        expect(self.page.get_by_role("heading", name="Review before sending")).to_be_visible()
        expect(self.page.get_by_text("identity-risk.xlsx · 2.0 KB", exact=True)).to_be_visible()
        self.assertFalse(any(url.endswith("/send") for url, _ in self.requests))
        self.page.get_by_role("button", name="Send email", exact=True).click()
        expect(self.page.get_by_text(
            "Sent to recipient@example.com. You can find it in Gmail Sent.", exact=True,
        )).to_be_visible()
        sends = [data for url, data in self.requests if url.endswith("/send")]
        self.assertEqual(len(sends), 1)
        self.assertTrue(sends[0]["confirmed"])
        self.assertEqual(sends[0]["review_token"], "fixture-review")
        self.assertNotIn("recipient", sends[0])  # Send uses only the frozen server-side draft.
        self.assertFalse(self.page_errors)

    def test_editing_preview_requires_another_preview(self):
        self.ask("Draft an email")
        self.page.get_by_label("To — who should receive this?").fill("first@example.com")
        self.page.get_by_role("button", name="Preview email").click()
        self.page.get_by_role("button", name="Edit draft").click()
        expect(self.page.get_by_role("button", name="Send email", exact=True)).to_have_count(0)
        self.page.get_by_label("To — who should receive this?").fill("second@example.com")
        self.page.get_by_label("identity-risk.xlsx", exact=True).uncheck()
        self.page.get_by_role("button", name="Preview email").click()
        expect(self.page.get_by_text("To: second@example.com", exact=True)).to_be_visible()
        expect(self.page.get_by_text("Attachments (0)", exact=True)).to_be_visible()
        self.assertFalse(any(url.endswith("/send") for url, _ in self.requests))


if __name__ == "__main__":
    unittest.main()
