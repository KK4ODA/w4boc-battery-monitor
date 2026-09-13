"""Send a test email with the configured Gmail credentials.
Run: python tools/mailer_test.py"""
import logging
import sys

import _shim  # noqa: F401
from w4boc import config, mailer

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
ok = mailer.send(
    f"[{config.SITE_NAME}] Mailer self-test",
    f"If you are reading this, SMTP works.\n\nSender: {config.EMAIL_SENDER}\n",
)
sys.exit(0 if ok else 1)
