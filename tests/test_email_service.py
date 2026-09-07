from __future__ import annotations

import asyncio
import base64
import json
from email import policy
from email.parser import BytesParser

import httpx
import pytest

from iam_agent.email_service import EmailError, EmailService, GmailSender, SendUncertain
from iam_agent.models import EmailContent, ReportFormat
from iam_agent.reporting import ReportStore

OWNER = "analyst@example.com"


class Sender:
    def __init__(self):
        self.calls = []
        self.error = None

    async def send(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        await asyncio.sleep(0)
        return "gmail-message-1"


async def setup_draft():
    store = ReportStore()
    report = await store.add(
        owner=OWNER, report_format=ReportFormat.CSV, filename="risk.csv",
        content=b"user,score\nuser12,91\n",
    )
    sender = Sender()
    service = EmailService(store, sender)
    draft = await service.create(
        owner=OWNER, conversation_key="one",
        content=EmailContent(subject="Risk report", body="Review attached."),
        recipient="", available=[report], selected_ids=[report.report_id],
    )
    return service, sender, report, draft


async def review(service, report, draft):
    return await service.preview(
        draft_id=draft["draft_id"], owner=OWNER, recipient="recipient@example.com",
        subject="Reviewed risk report", body="Exact approved body.",
        attachment_ids=[report.report_id],
    )


def send_payload(draft, reviewed):
    return dict(
        draft_id=draft["draft_id"], owner=OWNER, review_token=reviewed["review_token"],
        confirmed=True, access_token="token",
    )


@pytest.mark.asyncio
async def test_send_requires_review_confirmation_and_matching_owner():
    service, sender, report, draft = await setup_draft()
    with pytest.raises(EmailError, match="Preview"):
        await service.send(
            draft_id=draft["draft_id"], owner=OWNER, review_token="invented",
            confirmed=True, access_token="token",
        )
    reviewed = await review(service, report, draft)
    payload = send_payload(draft, reviewed)
    with pytest.raises(EmailError, match="Preview"):
        await service.send(**{**payload, "confirmed": False})
    with pytest.raises(EmailError, match="not found"):
        await service.send(**{**payload, "owner": "other@example.com"})
    assert not sender.calls


@pytest.mark.asyncio
async def test_review_freezes_attachment_bytes_and_duplicate_send_returns_receipt():
    service, sender, report, draft = await setup_draft()
    reviewed = await review(service, report, draft)
    service.reports._reports.clear()  # Expiry after review must not change approved bytes.
    payload = send_payload(draft, reviewed)
    receipt = await service.send(**payload)
    assert receipt["state"] == "sent"
    assert (await service.send(**payload))["message_id"] == "gmail-message-1"
    assert len(sender.calls) == 1
    assert sender.calls[0]["subject"] == "Reviewed risk report"
    assert sender.calls[0]["body"] == "Exact approved body."
    assert sender.calls[0]["attachments"][0].content == b"user,score\nuser12,91\n"


@pytest.mark.asyncio
async def test_edit_requires_a_new_review_token():
    service, sender, report, draft = await setup_draft()
    old = await review(service, report, draft)
    await service.preview(
        draft_id=draft["draft_id"], owner=OWNER, recipient="different@example.com",
        subject="Changed", body="Changed body", attachment_ids=[],
    )
    with pytest.raises(EmailError, match="Preview"):
        await service.send(**send_payload(draft, old))
    assert not sender.calls


@pytest.mark.asyncio
async def test_concurrent_send_and_uncertain_send_do_not_retry():
    service, sender, report, draft = await setup_draft()
    reviewed = await review(service, report, draft)
    payload = send_payload(draft, reviewed)
    await asyncio.gather(service.send(**payload), service.send(**payload), return_exceptions=True)
    assert len(sender.calls) == 1
    service, sender, report, draft = await setup_draft()
    reviewed = await review(service, report, draft)
    sender.error = SendUncertain()
    payload = send_payload(draft, reviewed)
    with pytest.raises(SendUncertain):
        await service.send(**payload)
    with pytest.raises(EmailError):
        await service.send(**payload)
    with pytest.raises(EmailError, match="already"):
        await review(service, report, draft)
    assert len(sender.calls) == 1


@pytest.mark.parametrize("recipient,subject,attachment_ids", [
    ("", "Report", []),
    ("a@example.com\r\nBcc: victim@example.com", "Report", []),
    ("a@example.com", "Report\nBcc: victim@example.com", []),
    ("a@example.com", "Report", ["somebody-elses-file"]),
])
@pytest.mark.asyncio
async def test_preview_rejects_unsafe_or_missing_fields(recipient, subject, attachment_ids):
    service, sender, report, draft = await setup_draft()
    with pytest.raises(EmailError):
        await service.preview(
            draft_id=draft["draft_id"], owner=OWNER, recipient=recipient,
            subject=subject, body="Review", attachment_ids=attachment_ids,
        )
    assert not sender.calls


@pytest.mark.asyncio
async def test_expired_file_is_not_silently_replaced():
    service, sender, report, draft = await setup_draft()
    service.reports._reports.clear()
    with pytest.raises(EmailError, match="expired"):
        await review(service, report, draft)
    assert not sender.calls


@pytest.mark.asyncio
async def test_gmail_mime_and_account_check_with_mock_transport(monkeypatch):
    store = ReportStore()
    report = await store.add(
        owner=OWNER, report_format=ReportFormat.CSV,
        filename="report.csv", content=b"user,score\nuser12,91",
    )
    artifact = await store.get(report.report_id, OWNER)
    requests = []
    account = {"email": OWNER, "email_verified": True}

    def transport(request):
        requests.append(request)
        assert "token" not in str(request.url)
        if request.url.path.endswith("userinfo"):
            return httpx.Response(200, json=account)
        raw = json.loads(request.content)["raw"]
        message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw))
        assert message["To"] == "recipient@example.com"
        attachment = next(message.iter_attachments())
        assert attachment.get_filename() == "report.csv"
        assert attachment.get_payload(decode=True) == artifact.content
        return httpx.Response(200, json={"id": "mock-gmail-id"})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: original_client(transport=httpx.MockTransport(transport), **kw),
    )
    sender = GmailSender()
    kwargs = dict(
        owner=OWNER, access_token="private-token", recipient="recipient@example.com",
        subject="Risk report", body="Review please.", attachments=[artifact],
    )
    assert await sender.send(**kwargs) == "mock-gmail-id"
    account["email"] = "different@example.com"
    with pytest.raises(EmailError, match="same Google account"):
        await sender.send(**kwargs)
    assert len([r for r in requests if r.method == "POST"]) == 1
