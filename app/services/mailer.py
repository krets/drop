"""Plain-text email over SMTP (stdlib, run in a thread). Independent of Google on purpose:
the alert that Drive needs re-authorizing can't depend on Drive being authorized."""
import asyncio
import re
import smtplib
import ssl
from email.message import EmailMessage

import config


def configured() -> bool:
    return bool(config.SMTP_SERVER and config.SMTP_FROM)


def oneline(text: str, limit: int = 150) -> str:
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _send_sync(subject: str, body: str, to: list[str]) -> None:
    msg = EmailMessage()
    msg["Subject"] = oneline(subject, 200)
    msg["From"] = config.SMTP_FROM
    msg["To"] = ", ".join(to)
    msg.set_content(body)
    ctx = ssl.create_default_context()
    if config.SMTP_SECURITY == "ssl":
        smtp = smtplib.SMTP_SSL(config.SMTP_SERVER, config.SMTP_PORT, timeout=30, context=ctx)
    else:
        smtp = smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT, timeout=30)
    with smtp:
        if config.SMTP_SECURITY == "starttls":
            smtp.starttls(context=ctx)
        if config.SMTP_USER:
            smtp.login(config.SMTP_USER, config.SMTP_PASS)
        smtp.send_message(msg)


async def send(subject: str, body: str, to: list[str]) -> None:
    """Raises on failure (smtplib.SMTPException / OSError); callers decide whether to retry."""
    if not configured():
        raise RuntimeError("SMTP is not configured")
    if not to:
        raise RuntimeError("No recipient")
    await asyncio.to_thread(_send_sync, subject, body, to)
