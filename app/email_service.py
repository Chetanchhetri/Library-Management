import asyncio
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .config import settings

logger = logging.getLogger("email")


class EmailError(Exception):
    pass


def _send(to_email: str, name: str, otp: str) -> None:
    sender = settings.SMTP_FROM or settings.SMTP_USER
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Your Library Seat Finder verification code"
    msg["From"] = sender
    msg["To"] = to_email
    text = (f"Hi {name},\n\nYour verification code is {otp}.\n"
            f"It is valid for {settings.OTP_VALID_MINUTES} minutes.\n")
    html = (f"<p>Hi {name},</p><p>Your verification code is "
            f"<b style='font-size:22px;letter-spacing:4px'>{otp}</b></p>"
            f"<p>It is valid for {settings.OTP_VALID_MINUTES} minutes.</p>")
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        server.sendmail(sender, [to_email], msg.as_string())


async def send_otp_email(to_email: str, name: str, otp: str) -> None:
    if settings.DEV_ECHO_OTP:
        logger.warning("DEV_ECHO_OTP: OTP for %s is %s", to_email, otp)
        if not settings.SMTP_USER:
            return
    if not settings.SMTP_USER:
        raise EmailError("SMTP is not configured")
    try:
        await asyncio.to_thread(_send, to_email, name, otp)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to send OTP email")
        if not settings.DEV_ECHO_OTP:
            raise EmailError(str(exc)) from exc
