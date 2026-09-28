"""Send reminder emails through an ordinary email account (Gmail by default).

Uses Python's built-in smtplib: SMTP is the standard protocol mail programs use
to send email. The connection is upgraded to encryption (STARTTLS) before the
password is sent. The account and its app password come from .env.
"""
import os
import re
import smtplib
import socket
import ssl
from email.message import EmailMessage

import config

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class MailError(Exception):
    """A sending problem explained in plain English."""


def sender():
    return os.getenv("SMTP_USER") or ""


def email_configured():
    return bool(sender() and os.getenv("SMTP_PASSWORD"))


def looks_like_email(address):
    return bool(EMAIL_PATTERN.match(address or ""))


def send_email(to, subject, body, html=None):
    """Send one email. `body` is plain text; `html`, if given, is the styled version
    that most email apps show instead (the plain text is the fallback)."""
    if not email_configured():
        raise MailError("No sending account set up. Add SMTP_USER and SMTP_PASSWORD "
                        "to the .env file, then restart the app.")
    message = EmailMessage()
    message["From"] = f"{config.APP_NAME} <{sender()}>"
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    if html:
        message.add_alternative(html, subtype="html")

    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())  # encrypt before logging in
            # Google shows app passwords as "abcd efgh ijkl mnop"; the spaces aren't part of it.
            smtp.login(sender(), os.getenv("SMTP_PASSWORD").replace(" ", ""))
            smtp.send_message(message)
    except smtplib.SMTPAuthenticationError:
        raise MailError("The email account rejected the sign-in. For Gmail, SMTP_PASSWORD "
                        "must be an app password (16 letters), not your normal password.")
    except smtplib.SMTPRecipientsRefused:
        raise MailError(f"The mail server refused the address {to}.")
    except smtplib.SMTPException as err:  # must come before OSError: it's a kind of OSError
        raise MailError(f"The mail server reported a problem: {err}")
    except (socket.gaierror, socket.timeout, ConnectionError, OSError) as err:
        raise MailError(f"Couldn't connect to {config.SMTP_HOST}. Check your internet "
                        f"connection. ({err.__class__.__name__})")
