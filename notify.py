"""Emails about the candidacy-form queues: told to approvers when a form arrives for them, to the chair when one is
waiting for their signature, and (from the command line, e.g. a daily scheduled job) as a reminder while items wait.

Notices carry no student names, only a count and a link, because email is less private than the app itself.
"""
import datetime
import sqlite3
import sys
import threading

import mailer

ROLE_FOR_STATUS = {"pending_approval": "approver", "pending_chair": "chair"}
WHAT = {"approver": ("approval", "Please review and approve"), "chair": ("signature", "Please review and sign")}
ASYNC = True                  # sent from a background thread so a slow mail server never holds up a page (tests turn this off)
KEEP_LOG = 200


def recipients(conn, role):
    """Email addresses of the accounts with this role that have one."""
    return [r[0] for r in conn.execute("SELECT email FROM users WHERE role = ? AND email IS NOT NULL AND email != '' "
                                       "ORDER BY id", (role,))]


def _message(role, count):
    noun, ask = WHAT[role]
    forms = "1 candidacy form is" if count == 1 else f"{count} candidacy forms are"
    base = mailer.base_url()
    link = f"\n\nOpen the approvals page: {base}/approvals" if base else "\n\nLog in to the department's Physics Graduate Students page to see it."
    subject = f"Candidacy {'form' if count == 1 else 'forms'} waiting for your {noun}"
    return subject, f"{forms} waiting for your {noun} in the Physics Graduate Students database. {ask}.{link}\n\nThis is an automatic message."


def _db_path(conn):
    return next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), "") or None


def _log(path, to_addr, subject, error):
    """Record the outcome (own connection: this may run in the background thread)."""
    if not path:
        return
    try:
        c = sqlite3.connect(path, timeout=10)
        c.execute("INSERT INTO email_log (at, to_addr, subject, ok, error) VALUES (?, ?, ?, ?, ?)",
                  (datetime.datetime.now().isoformat(timespec="seconds"), to_addr, subject, 0 if error else 1, error))
        c.execute("DELETE FROM email_log WHERE id NOT IN (SELECT id FROM email_log ORDER BY id DESC LIMIT ?)", (KEEP_LOG,))
        c.commit(); c.close()
    except sqlite3.Error as e:
        print(f"email log not written: {e}", file=sys.stderr)


def _deliver(path, messages):
    for to_addr, subject, body in messages:
        error = None
        try:
            mailer.send(to_addr, subject, body)
        except mailer.MailError as e:
            error = str(e)
            print(f"email to {to_addr} failed: {e}", file=sys.stderr)
        _log(path, to_addr, subject, error)


def send_all(conn, messages):
    """Send (to, subject, body) messages in the background; failures go to the log, never to the caller."""
    if not messages or not mailer.enabled():
        return
    path = _db_path(conn)
    if ASYNC:
        threading.Thread(target=_deliver, args=(path, messages), daemon=True).start()
    else:
        _deliver(path, messages)


def queue_changed(conn, status):
    """A form has just entered the queue for `status`: tell the people who handle that queue."""
    role = ROLE_FOR_STATUS.get(status)
    if role is None or not mailer.enabled():
        return
    count = conn.execute("SELECT count(*) FROM forms WHERE status = ?", (status,)).fetchone()[0]
    subject, body = _message(role, count)
    send_all(conn, [(to, subject, body) for to in recipients(conn, role)])


def send_reminders(conn):
    """Remind everyone with something waiting. Returns how many messages were sent (or tried)."""
    messages = []
    for status, role in ROLE_FOR_STATUS.items():
        count = conn.execute("SELECT count(*) FROM forms WHERE status = ?", (status,)).fetchone()[0]
        if count:
            subject, body = _message(role, count)
            messages += [(to, "Reminder: " + subject, body) for to in recipients(conn, role)]
    send_all(conn, messages)
    return len(messages)
