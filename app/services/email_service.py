"""Optional SMTP email notifications."""

from __future__ import annotations

import smtplib
from datetime import datetime, timezone, timedelta
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.config import Settings

IST = timezone(timedelta(hours=5, minutes=30))


def _format_timestamp() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def build_partner_notification(
    settings: Settings,
    *,
    partner_name: str,
    partner_email: str,
    document_filename: str,
    operator_name: str,
    log_id: str,
    cc_operator: bool = True,
    attachment_bytes: bytes | None = None,
    attachment_filename: str | None = None,
) -> tuple[MIMEMultipart, list[str]]:
    body = f"""Dear {partner_name},

Your signature has been applied to a document via the YPEF Partner Document Signing Tool.
The signed PDF is attached.

Document: {document_filename}
Date/Time: {_format_timestamp()}
Operator: {operator_name}
Audit Reference: {log_id}

This is an automated notification. Please contact the operations team if you did not expect this action.

Regards,
YourNest Operations
"""

    message = MIMEMultipart()
    message["From"] = settings.smtp_from
    message["To"] = partner_email
    message["Subject"] = f"Signature Applied — {document_filename}"
    if cc_operator and settings.smtp_user:
        message["Cc"] = settings.smtp_user
    message.attach(MIMEText(body, "plain"))

    if attachment_bytes:
        filename = attachment_filename or "signed.pdf"
        part = MIMEApplication(attachment_bytes, _subtype="pdf")
        part.add_header("Content-Disposition", "attachment", filename=filename)
        message.attach(part)

    recipients = [partner_email]
    if cc_operator and settings.smtp_user:
        recipients.append(settings.smtp_user)
    return message, recipients


def send_partner_notification(
    settings: Settings,
    *,
    partner_name: str,
    partner_email: str,
    document_filename: str,
    operator_name: str,
    log_id: str,
    cc_operator: bool = True,
    attachment_bytes: bytes | None = None,
    attachment_filename: str | None = None,
) -> str:
    if not settings.email_enabled:
        return "skipped (email not configured)"

    message, recipients = build_partner_notification(
        settings,
        partner_name=partner_name,
        partner_email=partner_email,
        document_filename=document_filename,
        operator_name=operator_name,
        log_id=log_id,
        cc_operator=cc_operator,
        attachment_bytes=attachment_bytes,
        attachment_filename=attachment_filename,
    )

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
            if settings.smtp_use_tls:
                server.starttls()
            if settings.smtp_user and settings.smtp_password:
                server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(settings.smtp_from, recipients, message.as_string())
        return f"sent at {_format_timestamp()}"
    except Exception as exc:
        return f"failed: {exc}"
