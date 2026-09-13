"""Gmail SMTP sender via app password.

send() returns True on successful transmit, False if email is disabled or
the SMTP call raised (error is logged, not re-thrown — we never want the
monitor to crash because email is down).
"""
import logging
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage

from . import config

log = logging.getLogger(__name__)



def send(subject: str, body: str, recipients: list[str] | None = None, *,
         sender: str | None = None, password: str | None = None,
         host: str | None = None, port: int | None = None) -> bool:
    """Send via SMTP/STARTTLS. Keyword overrides let the Settings page test
    credentials that are saved on disk but not yet loaded by this process."""
    rcpts = recipients or config.EMAIL_RECIPIENTS
    sender = sender if sender is not None else config.EMAIL_SENDER
    password = password if password is not None else config.EMAIL_PASSWORD
    host = host or config.EMAIL_SMTP_HOST
    port = port or config.EMAIL_SMTP_PORT
    if not (sender and password and rcpts):
        log.info(f"[MAIL DISABLED] would send to {rcpts}: {subject}")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(rcpts)
    msg["Date"] = datetime.now(config.TZ).strftime("%a, %d %b %Y %H:%M:%S %z")
    msg.set_content(body)

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(sender, password)
            s.send_message(msg)
        log.info(f"sent email to {rcpts}: {subject}")
        return True
    except Exception as e:
        log.error(f"SMTP send failed: {e}")
        return False


if __name__ == "__main__":
    # Hand-test: `python mailer.py` sends a test message with the configured
    # credentials. Exits 0 on success, 1 on failure.
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ok = send(
        f"[{config.SITE_NAME}] Mailer self-test",
        f"If you're reading this, SMTP works.\n\nSender: {config.EMAIL_SENDER}\n",
    )
    sys.exit(0 if ok else 1)
