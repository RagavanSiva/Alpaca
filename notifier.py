"""Email notifications over SMTP (defaults to Gmail).

Environment variables (.env locally, secrets on GitHub Actions):
    NOTIFY_EMAIL   recipient address
    SMTP_USER      sender account, e.g. your Gmail address
    SMTP_PASSWORD  for Gmail, an App Password (Google Account > Security > App passwords)
    SMTP_HOST      optional, default smtp.gmail.com
    SMTP_PORT      optional, default 587 (STARTTLS)
"""

import os
import smtplib
import ssl
from email.message import EmailMessage


def email_configured() -> bool:
    return all(os.getenv(k) for k in ("NOTIFY_EMAIL", "SMTP_USER", "SMTP_PASSWORD"))


def send_email(subject: str, body: str) -> str:
    """Send a plain-text email and return the recipient. Raises on failure."""
    to, user = os.environ["NOTIFY_EMAIL"], os.environ["SMTP_USER"]
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(body)
    host, port = os.getenv("SMTP_HOST", "smtp.gmail.com"), int(os.getenv("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(user, os.environ["SMTP_PASSWORD"])
        smtp.send_message(msg)
    return to
