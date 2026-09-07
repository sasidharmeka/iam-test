"""Reviewed, owner-bound Gmail sends. Gemini cannot invoke the send operation."""

from __future__ import annotations

import asyncio
import base64
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.policy import SMTP
from uuid import uuid4

import httpx

from iam_agent.models import EmailContent, ReportDescriptor
from iam_agent.reporting import ReportArtifact, ReportStore


class EmailError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class SendUncertain(EmailError):
    def __init__(self):
        super().__init__(
            "Gmail did not confirm the outcome. Check your Gmail Sent folder before sending "
            "again. This draft is locked to prevent a duplicate email.", 409,
        )


def validate_recipient(value: str) -> str:
    value = value.strip()
    if len(value) > 254 or not re.fullmatch(
        r"[A-Za-z0-9.!#$%&'*+/=?^_{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?"
        r"\.[A-Za-z]{2,}", value,
    ):
        raise EmailError("Enter one valid recipient email address before previewing.")
    return value


@dataclass
class EmailDraft:
    draft_id: str
    owner: str
    conversation_key: str
    subject: str
    body: str
    recipient: str
    available: list[ReportDescriptor]
    selected_ids: list[str]
    expires_at: float
    state: str = "draft"
    review_token: str = ""
    attachments: list[ReportArtifact] = field(default_factory=list)
    message_id: str = ""


class GmailSender:
    async def send(
        self, *, owner: str, access_token: str, recipient: str, subject: str,
        body: str, attachments: list[ReportArtifact],
    ) -> str:
        # Tokens are transient: never persist them, put them in URLs, or log them.
        headers = {"Authorization": f"Bearer {access_token}"}
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                identity = await client.get(
                    "https://openidconnect.googleapis.com/v1/userinfo", headers=headers,
                )
            except httpx.HTTPError as exc:
                raise EmailError(
                    "Could not verify the Gmail account. Please reconnect.", 502,
                ) from exc
            if identity.status_code != 200:
                raise EmailError("Reconnect Gmail and grant email identity permission.", 403)
            profile = identity.json()
            if profile.get("email_verified") is not True or str(
                profile.get("email", "")
            ).casefold() != owner.casefold():
                raise EmailError(
                    "Choose the same Google account used to sign into this app.", 403,
                )
            message = EmailMessage(policy=SMTP)
            message["From"] = owner
            message["To"] = recipient
            message["Subject"] = subject
            message.set_content(body)
            for attachment in attachments:
                major, minor = attachment.mime_type.split(";", 1)[0].split("/", 1)
                message.add_attachment(
                    attachment.content, maintype=major, subtype=minor,
                    filename=attachment.filename,
                )
            raw = message.as_bytes()
            if len(raw) > 20_000_000:
                raise EmailError("The complete email exceeds the app's 20 MB limit.")
            # No transport retry: repeating a timed-out request may send twice.
            try:
                response = await client.post(
                    "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                    headers=headers,
                    json={"raw": base64.urlsafe_b64encode(raw).decode("ascii")},
                )
            except httpx.HTTPError as exc:
                raise SendUncertain() from exc
            if response.status_code >= 500:
                raise SendUncertain()
            if response.status_code >= 400:
                raise EmailError(
                    "Gmail rejected the send. Check that Gmail API is enabled and Gmail sending "
                    "permission was granted, then review the draft again.", 403,
                )
            try:
                message_id = response.json()["id"]
            except (KeyError, ValueError) as exc:
                raise SendUncertain() from exc
            return str(message_id)


class EmailService:
    def __init__(
        self, reports: ReportStore, sender: GmailSender | None = None, *, ttl: int = 900,
    ):
        self.reports = reports
        self.sender = sender or GmailSender()
        self.ttl = ttl
        self._drafts: OrderedDict[str, EmailDraft] = OrderedDict()
        self._lock = asyncio.Lock()

    def _prune(self) -> None:
        for key in list(self._drafts):
            item = self._drafts[key]
            if item.expires_at <= time.monotonic() and item.state != "sending":
                del self._drafts[key]

    def _get(self, draft_id: str, owner: str) -> EmailDraft:
        self._prune()
        item = self._drafts.get(draft_id)
        if item is None or item.owner != owner.casefold():
            raise EmailError("Email draft not found or expired. Ask for a new draft.", 404)
        return item

    @staticmethod
    def _view(item: EmailDraft) -> dict:
        return {
            "draft_id": item.draft_id, "recipient": item.recipient,
            "subject": item.subject, "body": item.body, "state": item.state,
            "available_attachments": [r.model_dump(mode="json") for r in item.available],
            "attachment_ids": list(item.selected_ids), "review_token": item.review_token,
            "attachments": [{
                "report_id": a.report_id, "filename": a.filename, "size_bytes": len(a.content),
            } for a in item.attachments],
            "message_id": item.message_id,
        }

    async def create(
        self, *, owner: str, conversation_key: str, content: EmailContent, recipient: str,
        available: list[ReportDescriptor], selected_ids: list[str],
    ) -> dict:
        available = [r for r in available if await self.reports.get(r.report_id, owner)]
        allowed = {r.report_id for r in available}
        if not set(selected_ids).issubset(allowed):
            raise EmailError("A requested attachment expired. Generate that file again.")
        async with self._lock:
            self._prune()
            if len(self._drafts) >= 100:
                raise EmailError(
                    "The draft store is full. Try again after older drafts expire.", 503,
                )
            item = EmailDraft(
                uuid4().hex, owner.casefold(), conversation_key, content.subject, content.body,
                recipient, available, list(dict.fromkeys(selected_ids)), time.monotonic() + self.ttl,
            )
            self._drafts[item.draft_id] = item
            return self._view(item)

    async def preview(
        self, *, draft_id: str, owner: str, recipient: str, subject: str, body: str,
        attachment_ids: list[str],
    ) -> dict:
        recipient = validate_recipient(recipient)
        if not subject.strip() or len(subject) > 200 or "\r" in subject or "\n" in subject:
            raise EmailError("Enter a subject of 1–200 characters on a single line.")
        if not body.strip() or len(body) > 12000:
            raise EmailError("Enter an email body of 1–12,000 characters.")
        async with self._lock:
            item = self._get(draft_id, owner)
            if item.state in {"sending", "sent", "unknown"}:
                raise EmailError("This draft has already been submitted for sending.", 409)
            allowed = {r.report_id for r in item.available}
            ids = list(dict.fromkeys(attachment_ids))
            if len(ids) > 8 or not set(ids).issubset(allowed):
                raise EmailError("Select up to eight files from this draft's attachment list.")
            attachments = []
            for report_id in ids:
                artifact = await self.reports.get(report_id, owner)
                if artifact is None:
                    raise EmailError("An attachment expired. Generate the file again.", 410)
                attachments.append(artifact)
            total = sum(len(a.content) for a in attachments)
            retained = sum(
                len(a.content) for d in self._drafts.values() if d is not item
                for a in d.attachments
            )
            if total > 12_000_000 or retained + total > 48_000_000:
                raise EmailError(
                    "These attachments exceed available email storage. Select fewer.",
                )
            item.recipient, item.subject, item.body = recipient, subject, body
            item.selected_ids, item.attachments = ids, attachments
            item.review_token = secrets.token_urlsafe(32)
            item.state = "reviewed"
            return self._view(item)

    async def send(
        self, *, draft_id: str, owner: str, review_token: str,
        confirmed: bool, access_token: str,
    ) -> dict:
        async with self._lock:
            item = self._get(draft_id, owner)
            if not confirmed or not review_token or not secrets.compare_digest(
                item.review_token, review_token,
            ):
                raise EmailError("Preview this email and explicitly confirm Send first.", 409)
            if item.state == "sent":
                return self._view(item)  # Duplicate click returns receipt, never sends again.
            if item.state != "reviewed":
                raise EmailError("This email is already sending or needs a new review.", 409)
            item.state = "sending"
        try:
            message_id = await self.sender.send(
                owner=owner, access_token=access_token, recipient=item.recipient,
                subject=item.subject, body=item.body, attachments=item.attachments,
            )
        except BaseException as exc:
            async with self._lock:
                item.state = (
                    "draft" if isinstance(exc, EmailError) and not isinstance(exc, SendUncertain)
                    else "unknown"
                )
                item.review_token = ""
            raise
        async with self._lock:
            item.state, item.message_id = "sent", message_id
            item.attachments = []
            return self._view(item)
