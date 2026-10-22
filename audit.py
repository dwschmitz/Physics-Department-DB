"""The audit log: a permanent record of who changed what, and when.

How it works
  * Database triggers on every table that holds records write a row to `audit_log` for each insert, change and
    delete, with the values before and after. Because the triggers live in the database, nothing that changes the data
    can skip them (the web pages, the command line, an import, a future feature).
  * A trigger cannot know who is signed in, so it leaves `actor` empty, and `AuditedConnection` (below) fills it in
    just before the changes are committed. A change made by some other program (say a database browser) has no actor
    and is shown as "(outside the app)".
  * Passwords (hashes), PDF files and the signature image are never copied into the log; the log only notes that a
    password or signature changed.
The triggers are rebuilt automatically when the tables change (see `install`). A migration that drops or renames a
column, or rebuilds a table, must call `drop_triggers` first.
"""
import hashlib
import json
import re
import sqlite3

VERSION = 1          # raise when the trigger definitions change, so existing databases get the new ones

# table -> how to describe it. skip: columns whose values are not copied into the log (but a change to them is noted);
# student: SQL for the student the row belongs to; row: the row's id; label: who/what the row is, in words.
_STUDENT_NAME = "(SELECT first_name || ' ' || last_name FROM students WHERE id = {r}.student_id)"
TABLES = {
    "students":       dict(skip=(), student="{r}.id", row="{r}.id", label="{r}.first_name || ' ' || {r}.last_name"),
    "faculty":        dict(skip=(), student=None, row="{r}.id", label="{r}.first_name || ' ' || {r}.last_name"),
    "fsrs":           dict(skip=(), student=None, row="{r}.id", label="{r}.first_name || ' ' || {r}.last_name"),
    "advising":       dict(skip=(), student="{r}.student_id", row="{r}.id", label=_STUDENT_NAME),
    "milestones":     dict(skip=(), student="{r}.student_id", row="{r}.id", label=_STUDENT_NAME),
    "funding":        dict(skip=(), student="{r}.student_id", row="{r}.id", label=_STUDENT_NAME),
    "course_credits": dict(skip=(), student="{r}.student_id", row="NULL", label=_STUDENT_NAME),
    "forms":          dict(skip=("pdf", "pdf_faculty", "pdf_sha256"), student="{r}.student_id", row="{r}.id", label=_STUDENT_NAME),
    "users":          dict(skip=("password_hash",), student=None, row="{r}.id", label="{r}.username"),
    "signatures":     dict(skip=("image",), student=None, row="{r}.user_id",
                           label="(SELECT username FROM users WHERE id = {r}.user_id)"),
}
NOW = "strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')"


def available(conn):
    """Whether this SQLite has the JSON functions the triggers use (every normal Python does)."""
    try:
        conn.execute("SELECT json_object('a', 1)").fetchone()
        return True
    except sqlite3.OperationalError:
        return False


def _columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _signature(conn):
    shape = {t: _columns(conn, t) for t in TABLES}
    return hashlib.sha1(json.dumps([VERSION, shape], sort_keys=True).encode()).hexdigest()


def drop_triggers(conn):
    """Remove the audit triggers (and forget they were installed). Called before a table is rebuilt; `install`, at
    the end of connecting, puts them back."""
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'audit\\_%' ESCAPE '\\'").fetchall():
        conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
    try:
        conn.execute("DELETE FROM audit_meta WHERE key = 'triggers'")
    except sqlite3.OperationalError:               # a database from before the audit log: nothing recorded yet
        pass
    conn.commit()


def _json(row, cols):
    return "json_object(" + ", ".join(f"'{c}', {row}.{c}" for c in cols) + ")"


def install(conn):
    """Make sure the audit triggers exist and match the current tables. Cheap when nothing has changed."""
    if not available(conn):
        return False
    sig = _signature(conn)
    have = conn.execute("SELECT value FROM audit_meta WHERE key = 'triggers'").fetchone()
    if have and have[0] == sig:
        return True
    drop_triggers(conn)
    for table, spec in TABLES.items():
        all_cols = _columns(conn, table)
        shown = [c for c in all_cols if c not in spec["skip"]]
        noted = [c for c in spec["skip"] if c in all_cols]                     # changes to these are noted, not copied
        for action, row in (("insert", "NEW"), ("update", "NEW"), ("delete", "OLD")):
            ref = lambda text: text.format(r=row)
            old = _json("OLD", shown) if action != "insert" else "NULL"
            new = _json("NEW", shown) if action != "delete" else "NULL"
            if action == "update" and noted:                                   # e.g. password_changed: 1, without the hash
                changed = ", ".join(f"'{c}_changed', CASE WHEN OLD.{c} IS NOT NEW.{c} THEN 1 END" for c in noted)
                new = f"json_patch({new}, json_object({changed}))"
            when = ""
            if action == "update":
                when = "WHEN " + " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in all_cols)    # a change, not a no-op save
            conn.execute(f"""
                CREATE TRIGGER audit_{table}_{action[0]} AFTER {action.upper()} ON {table} {when}
                BEGIN
                  INSERT INTO audit_log (at, action, entity, row_id, student_id, label, old_values, new_values)
                  VALUES ({NOW}, '{action}', '{table}', {ref(spec["row"])}, {ref(spec["student"] or "NULL")},
                          {ref(spec["label"])}, {old}, {new});
                END""")
    conn.execute("INSERT OR REPLACE INTO audit_meta (key, value) VALUES ('triggers', ?)", (sig,))
    conn.commit()
    return True


def event(conn, actor, text, entity="session"):
    """Record something that is not a change to a record, e.g. a sign-in. Saved when the connection commits."""
    conn.execute("INSERT INTO audit_log (at, actor, action, entity, label, new_values) VALUES (" + NOW + ", ?, 'event', ?, ?, NULL)",
                 (actor, entity, text))


class AuditedConnection(sqlite3.Connection):
    """A connection that knows who is making changes. Set `.actor` (and optionally `.audit_note`, a few words about the
    change such as "student import"); the rows the triggers write get that name when the changes are committed."""
    actor = "command line"
    audit_note = None
    _mark = None
    _writes = re.compile(r"\s*(INSERT|UPDATE|DELETE|REPLACE)\b", re.I)

    def _begin_mark(self, sql):
        if not self.in_transaction and self._writes.match(sql):
            try:     # remember where the log ended, so only rows written by THIS transaction are signed
                self._mark = super().execute("SELECT COALESCE(MAX(id), 0) FROM audit_log").fetchone()[0]
            except sqlite3.OperationalError:          # before the table exists (first start of a new database)
                self._mark = None

    def execute(self, sql, *args):
        self._begin_mark(sql)
        return super().execute(sql, *args)

    def executemany(self, sql, *args):
        self._begin_mark(sql)
        return super().executemany(sql, *args)

    def commit(self):
        if self.in_transaction and self._mark is not None:
            super().execute("UPDATE audit_log SET actor = ?, note = ? WHERE actor IS NULL AND id > ?",
                            (self.actor, self.audit_note, self._mark))
        self._mark = None
        super().commit()

    def rollback(self):
        self._mark = None
        super().rollback()

    def __exit__(self, exc_type, exc, tb):             # `with conn:` must sign the changes too (the built-in one skips commit())
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


# ---- reading the log for display ------------------------------------------------------------------------------

ENTITY_NAMES = {"students": "Student", "faculty": "Faculty", "fsrs": "FSR", "advising": "Advisor / committee",
                "milestones": "Milestone", "funding": "Funding", "course_credits": "Core course", "forms": "Form",
                "users": "Account", "signatures": "Signature image", "session": "Sign-in"}
FIELD_NAMES = {"university_id": "University ID", "cnet": "CNET", "poetaf_coa": "POETAF/COA", "fsr_id": "FSR",
               "faculty_id": "Faculty", "student_id": "Student", "password_hash_changed": "Password", "image_changed": "Image",
               "pdf_changed": "PDF", "pdf_faculty_changed": "Advisor copy", "pdf_sha256_changed": "PDF checksum",
               "password_changed": "Password", "milestone_id": "Milestone", "term_year": "Year", "term_season": "Term"}


def field_name(key):
    return FIELD_NAMES.get(key) or key.replace("_changed", "").replace("_", " ").capitalize()


def _show(value, limit=90):
    if value is None or value == "":
        return "(empty)"
    text = str(value)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def describe(entity, action, old, new, nice=lambda k, v: v):
    """Plain-English lines for one log row: ["Status: active → graduated", ...]. old / new are JSON text or None.
    `nice(field, value)` turns stored values into display text (for instance 'on_leave' into 'On leave')."""
    old, new = (json.loads(old) if old else {}), (json.loads(new) if new else {})
    lines = []
    if action == "update":
        for key in new:
            if key.endswith("_changed"):
                lines.append(f"{field_name(key)} changed")
            elif old.get(key) != new[key]:
                lines.append(f"{field_name(key)}: {_show(nice(key, old.get(key)))} → {_show(nice(key, new[key]))}")
    else:
        for key, value in (new if action == "insert" else old).items():
            if value not in (None, "") and not key.endswith("_changed") and key not in ("id", "student_id", "user_id"):
                lines.append(f"{field_name(key)}: {_show(nice(key, value))}")
    return lines
