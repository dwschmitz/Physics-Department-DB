"""Sending email through the department's mail server (SMTP).

Settings come from environment variables, so no password is ever stored in the database or the repository
(`run.sh` reads them from a `.env` file next to it if there is one):

    PHYSDB_SMTP_HOST      the mail server, e.g. smtp.example.edu. Email is switched off when this is empty.
    PHYSDB_SMTP_PORT      default 587
    PHYSDB_SMTP_SECURITY  "starttls" (default), "ssl" (port 465 style), or "none"
    PHYSDB_SMTP_USER      login name, if the server wants one
    PHYSDB_SMTP_PASSWORD  its password
    PHYSDB_MAIL_FROM      the From address (defaults to the login name)
    PHYSDB_BASE_URL       the web address people use for this app, e.g. https://physgrads.example.edu, put in
                          notices as a link (without it the notices simply say to log in)
"""
import os
import smtplib
import ssl
from email.message import EmailMessage

TIMEOUT = 20


class MailError(Exception):
    """The message could not be sent. The text says why, for the log and the administrator."""


def settings(environ=None):
    """The mail settings as a dict, or None when email is not switched on."""
    env = os.environ if environ is None else environ
    host = (env.get("PHYSDB_SMTP_HOST") or "").strip()
    if not host:
        return None
    user = (env.get("PHYSDB_SMTP_USER") or "").strip()
    sender = (env.get("PHYSDB_MAIL_FROM") or user).strip()
    return dict(host=host, port=int(env.get("PHYSDB_SMTP_PORT") or 587),
                security=(env.get("PHYSDB_SMTP_SECURITY") or "starttls").strip().lower(),
                user=user, password=env.get("PHYSDB_SMTP_PASSWORD") or "", sender=sender,
                base_url=(env.get("PHYSDB_BASE_URL") or "").strip().rstrip("/"))


def enabled():
    try:
        return settings() is not None
    except ValueError:
        return False


def base_url():
    cfg = settings() if enabled() else None
    return cfg["base_url"] if cfg else (os.environ.get("PHYSDB_BASE_URL") or "").strip().rstrip("/")


def send(to_addr, subject, body):
    """Send one plain-text message. Raises MailError if email is off or the server refuses it."""
    try:
        cfg = settings()
    except ValueError:
        raise MailError("PHYSDB_SMTP_PORT is not a number.")
    if cfg is None:
        raise MailError("Email is not set up (PHYSDB_SMTP_HOST is empty).")
    if not cfg["sender"]:
        raise MailError("No From address: set PHYSDB_MAIL_FROM (or PHYSDB_SMTP_USER).")
    if cfg["security"] not in ("starttls", "ssl", "none"):
        raise MailError("PHYSDB_SMTP_SECURITY must be starttls, ssl or none.")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = cfg["sender"], to_addr, subject
    msg.set_content(body)
    try:
        if cfg["security"] == "ssl":
            server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=TIMEOUT, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=TIMEOUT)
        with server as smtp:
            if cfg["security"] == "starttls":
                smtp.starttls(context=ssl.create_default_context())         # certificates are checked
            if cfg["user"]:
                smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
        raise MailError(f"{type(e).__name__}: {e}"[:300])
