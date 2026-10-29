from email.header import decode_header

from app.config import Settings
from app.services.email_service import build_partner_notification, send_partner_notification


def _settings(**overrides) -> Settings:
    values = {
        "app_password": "secret",
        "operator_name": "Sanjay Jha",
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_user": "ops@yournest.in",
        "smtp_password": "pw",
        "smtp_from": "ops@yournest.in",
        "smtp_use_tls": True,
    }
    values.update(overrides)
    return Settings(**values)


def _filename(header_value: str) -> str:
    parts = decode_header(header_value)
    decoded = "".join(
        text.decode(enc or "utf-8") if isinstance(text, bytes) else text
        for text, enc in parts
    )
    if "filename=" in decoded:
        return decoded.split("filename=", 1)[1].strip().strip('"')
    return decoded


def test_partner_notification_attaches_signed_pdf():
    pdf = b"%PDF-1.4 signed-bytes"
    message, recipients = build_partner_notification(
        _settings(),
        partner_name="Girish Shivani",
        partner_email="girish.shivani@yournest.in",
        document_filename="consent.docx",
        operator_name="Sanjay Jha",
        log_id="evt-1",
        attachment_bytes=pdf,
        attachment_filename="consent_signed.pdf",
    )

    payloads = list(message.walk())
    pdf_parts = [
        part
        for part in payloads
        if part.get_content_type() == "application/pdf"
        or (part.get_content_disposition() == "attachment")
    ]
    assert recipients == ["girish.shivani@yournest.in", "ops@yournest.in"]
    assert pdf_parts
    attached = pdf_parts[0]
    assert attached.get_payload(decode=True) == pdf
    assert "consent_signed.pdf" in _filename(attached.get("Content-Disposition", ""))
    body = next(part.get_payload() for part in payloads if part.get_content_type() == "text/plain")
    assert "signed PDF is attached" in body


def test_send_is_skipped_when_smtp_is_not_configured():
    status = send_partner_notification(
        _settings(smtp_host="", smtp_from=""),
        partner_name="Girish Shivani",
        partner_email="girish.shivani@yournest.in",
        document_filename="consent.pdf",
        operator_name="Sanjay Jha",
        log_id="evt-2",
        attachment_bytes=b"%PDF-1.4",
        attachment_filename="consent_signed.pdf",
    )
    assert status == "skipped (email not configured)"
