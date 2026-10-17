#!/usr/bin/env python3
"""Web interface for the physics graduate student database."""
import argparse
import datetime
import getpass
import hmac
import os
import re
import secrets
import socket
import sqlite3
import threading
import urllib.request
import webbrowser
from pathlib import Path

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import physdb

STATUSES = ["active", "on_leave", "graduated", "withdrawn"]
FACULTY_ROLE_FILTERS = {"any": "Any role", "advisor": "Advisor",
                        "committee": "Committee member only"}
ACCOUNT_ROLES = ["viewer", "editor", "admin"]
MIN_PASSWORD = 10
ROLES = ["advisor", "co-advisor", "committee", "committee_chair"]
MILESTONE_KINDS = ["qualifying_exam", "candidacy", "thesis_proposal", "thesis_defense", "other"]
OUTCOMES = ["pending", "passed", "conditional", "failed"]
FUNDING_KINDS = ["TA", "RA", "fellowship", "stipend_supplement", "other"]
DISPLAY_NAMES = {"stipend_supplement": "Stipend Supplement", "citizen": "Citizen",
                 "permanent_resident": "Permanent Resident", "international": "International"}

CHILD_TABLES = {
    "advising": "Advisor / committee member",
    "milestones": "Milestone",
    "funding": "Funding",
}
_DUMMY_HASH = generate_password_hash("not-a-real-password")


def can_edit(user):
    return bool(user and user["role"] in ("editor", "admin"))


def pw_fingerprint(password_hash):
    """Stored in the session so that resetting a password signs out old sessions."""
    return password_hash[-16:]


def label(value):
    if value in DISPLAY_NAMES:
        return DISPLAY_NAMES[value]
    return str(value).replace("_", " ").capitalize() if value not in ("TA", "RA") else value


def F(name, text, kind="text", required=False, choices=None, help=None, placeholder=False, maxlen=None):
    """placeholder: show a "choose" entry so a required dropdown can't be left on its first option."""
    return dict(name=name, label=text, type=kind, required=required, choices=choices, help=help,
                placeholder=placeholder, maxlen=maxlen)


def term_years():
    """Years offered in the funding menu: newest first, from six years ahead back to 2000."""
    this_year = datetime.date.today().year
    return [(y, y) for y in range(this_year + 6, 1999, -1)]


def fields_for(table, conn):
    """Form definition for a table. Choices are (value, label) pairs."""
    enum = lambda vals: [(v, label(v)) for v in vals]
    if table == "students":
        return [
            F("first_name", "First name", required=True),
            F("last_name", "Last name", required=True),
            F("university_id", "University ID", required=True),
            F("email", "Email", "email", True),
            F("citizenship", "Citizenship", "select", True, enum(physdb.CITIZENSHIP), placeholder=True),
            F("status", "Status", "select", True, enum(STATUSES)),
            F("start_year", "Start year", "year", True),
            F("expected_grad_year", "Expected graduation year", "year"),
            F("research_area", "Research area"),
            F("notes", "Notes", "textarea"),
        ]
    if table == "faculty":
        return [F("first_name", "First name", required=True),
                F("last_name", "Last name", required=True),
                F("email", "Email", "email", True),
                F("research_area", "Research area")]
    if table == "advising":
        people = conn.execute("SELECT id, last_name, first_name FROM faculty "
                              "ORDER BY last_name, first_name").fetchall()
        return [
            F("faculty_id", "Faculty member", "select", True,
              [(r["id"], f"{r['last_name']}, {r['first_name']}") for r in people]),
            F("role", "Role", "select", True, enum(ROLES)),
            F("start_date", "Start date", "date", True),
            F("end_date", "End date", "date", help="Leave blank if ongoing."),
        ]
    if table == "milestones":
        return [F("kind", "Milestone", "select", True, enum(MILESTONE_KINDS)),
                F("date", "Date", "date", True),
                F("outcome", "Outcome", "select", True, enum(OUTCOMES)),
                F("notes", "Notes", "textarea")]
    if table == "funding":
        return [F("kind", "Type", "select", True, enum(FUNDING_KINDS)),
                F("term_year", "Year", "select", True, term_years(), placeholder=True),
                F("term_season", "Term", "select", True, [(s, s) for s in physdb.SEASONS], placeholder=True),
                F("source", "Source"),
                F("poetaf_coa", "POETAF/COA", maxlen=60, help="Account number for this funding."),
                F("amount", "Amount ($)", "number"),
                F("notes", "Notes", "textarea")]
    raise KeyError(table)


def parse_form(fields, form):
    """Validate submitted values. Returns (values, errors)."""
    values, errors = {}, {}
    for f in fields:
        name, kind = f["name"], f["type"]
        raw = form.get(name) or ""
        if kind not in ("password", "secret"):
            raw = raw.strip()
        if not raw:
            if f["required"]:
                errors[name] = "This field is required."
            values[name] = None
            continue
        try:
            if kind == "select":
                allowed = {str(v): v for v, _ in f["choices"]}
                if raw not in allowed:
                    raise ValueError("Please choose one of the listed options.")
                raw = allowed[raw]
            elif f.get("maxlen") and len(raw) > f["maxlen"]:
                raise ValueError(f"Enter at most {f['maxlen']} characters.")
            elif kind == "year":
                raw = int(raw)
                if not 1950 <= raw <= 2100:
                    raise ValueError("Enter a four-digit year.")
            elif kind == "date":
                datetime.date.fromisoformat(raw)
            elif kind == "number":
                raw = float(raw)
                if raw < 0 or raw != raw:
                    raise ValueError("Enter a non-negative number.")
            elif kind == "username" and not re.fullmatch(r"[A-Za-z0-9._@-]{3,60}", raw):
                raise ValueError("Enter 3-60 letters, digits, or . _ @ - (no spaces).")
            elif kind == "password" and len(raw) < MIN_PASSWORD:
                raise ValueError(f"Enter at least {MIN_PASSWORD} characters.")
            elif kind == "email" and ("@" not in raw or " " in raw):
                raise ValueError("Enter a valid email address.")
        except ValueError as e:
            msg = str(e)
            errors[name] = msg if "Enter" in msg or "choose" in msg else "Invalid value."
        values[name] = raw
    return values, errors


def create_app(db_path=None):
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path or physdb.DB_PATH
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=8))
    app.config["SECRET_KEY"] = os.environ.get("PHYSDB_SECRET") or _persistent_secret()
    app.jinja_env.filters["label"] = label
    app.jinja_env.filters["term"] = physdb.term_label

    def db():
        if "db" not in g:
            g.db = physdb.connect(app.config["DB_PATH"])
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    # ---- authentication / CSRF -------------------------------------------------
    @app.before_request
    def gate():
        g.user = None
        uid = session.get("user_id")
        if uid is not None:
            g.user = db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
            if g.user is None or not hmac.compare_digest(
                    session.get("pwf", ""), pw_fingerprint(g.user["password_hash"])):
                g.user = None
                session.clear()
        if request.endpoint in ("login", "static"):
            return None
        if g.user is None:
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        if request.method == "POST":
            sent = request.form.get("csrf_token", "")
            if not hmac.compare_digest(sent, session.get("csrf", "")):
                abort(400, "Your session expired. Go back, reload the page and try again.")
            if not can_edit(g.user) and request.endpoint not in ("logout", "account"):
                abort(403, "Your account is view-only.")
        if g.user["must_change"] and request.endpoint not in ("account", "logout"):
            return redirect(url_for("account"))

    @app.context_processor
    def inject():
        session.setdefault("csrf", secrets.token_urlsafe(32))
        return dict(csrf_token=session["csrf"], user=g.get("user"),
                    can_edit=can_edit(g.get("user")), is_admin=bool(g.get("user") and g.user["role"] == "admin"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            row = db().execute("SELECT * FROM users WHERE username = ?",
                               (request.form.get("username", "").strip(),)).fetchone()
            ok = check_password_hash(row["password_hash"] if row else _DUMMY_HASH,
                                     request.form.get("password", ""))
            if row and ok:
                session.clear()
                session.permanent = True
                session["user_id"] = row["id"]
                session["pwf"] = pw_fingerprint(row["password_hash"])
                session["csrf"] = secrets.token_urlsafe(32)
                nxt = request.args.get("next", "")
                if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt:
                    nxt = url_for("students")
                return redirect(nxt)
            flash("Incorrect username or password.", "error")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ---- account management ------------------------------------------------------
    @app.route("/account", methods=["GET", "POST"])
    def account():
        fields = [F("current_password", "Current password", "secret", True),
                  F("new_password", "New password", "password", True,
                    help=f"At least {MIN_PASSWORD} characters."),
                  F("confirm", "Repeat new password", "password", True)]
        errors = {}
        if request.method == "POST":
            values, errors = parse_form(fields, request.form)
            if "current_password" not in errors and not check_password_hash(
                    g.user["password_hash"], values["current_password"]):
                errors["current_password"] = "That is not your current password."
            if "confirm" not in errors and values["new_password"] != values["confirm"]:
                errors["confirm"] = "The two passwords don't match."
            if "new_password" not in errors and values["new_password"] == values["current_password"]:
                errors["new_password"] = "Enter a password different from the current one."
            if not errors:
                new_hash = generate_password_hash(values["new_password"])
                db().execute("UPDATE users SET password_hash = ?, must_change = 0 WHERE id = ?",
                             (new_hash, g.user["id"]))
                db().commit()
                session["pwf"] = pw_fingerprint(new_hash)
                flash("Your password has been changed.", "ok")
                return redirect(url_for("students"))
        if g.user["must_change"]:
            flash("Please choose a new password before continuing.", "error")
        return render_template("form.html", title="Change your password", fields=fields,
                               values={}, errors=errors, back=url_for("students"))

    def user_fields(creating):
        return [F("username", "Username", "username", True)] if creating else []

    def role_field():
        return F("role", "Role", "select", True, [(r, label(r)) for r in ACCOUNT_ROLES],
                 help="Viewer: look only. Editor: can also change records. "
                      "Administrator: can also manage accounts.")

    @app.route("/users")
    def users():
        require_admin()
        rows = db().execute("SELECT * FROM users ORDER BY username").fetchall()
        return render_template("users.html", rows=rows)

    @app.route("/users/new", methods=["GET", "POST"])
    def user_new():
        require_admin()
        fields = user_fields(True) + [role_field(),
                 F("password", "Temporary password", "password", True,
                   help=f"At least {MIN_PASSWORD} characters. The person must choose their own at first login.")]
        errors = {}
        if request.method == "POST":
            values, errors = parse_form(fields, request.form)
            if not errors:
                try:
                    db().execute("INSERT INTO users (username, password_hash, role, must_change) "
                                 "VALUES (?,?,?,1)", (values["username"],
                                 generate_password_hash(values["password"]), values["role"]))
                    db().commit()
                    flash(f"Account '{values['username']}' created.", "ok")
                    return redirect(url_for("users"))
                except sqlite3.IntegrityError:
                    db().rollback()
                    errors["username"] = "That username is already taken."
        return render_template("form.html", title="Add account", fields=fields,
                               values=request.form if request.method == "POST" else {"role": "viewer"},
                               errors=errors, back=url_for("users"))

    @app.route("/users/<int:uid>/edit", methods=["GET", "POST"])
    def user_edit(uid):
        require_admin()
        u = get_or_404("users", uid)
        if u["id"] == g.user["id"]:
            return redirect(url_for("account"))   # own password/role: use the normal page
        fields = [role_field(),
                  F("password", "New temporary password", "password", False,
                    help="Leave blank to keep the current password. Setting one signs the person out "
                         "everywhere and makes them choose a new password at next login.")]
        errors = {}
        if request.method == "POST":
            values, errors = parse_form(fields, request.form)
            if not errors:
                if "role" in values:
                    db().execute("UPDATE users SET role = ? WHERE id = ?", (values["role"], uid))
                if values.get("password"):
                    db().execute("UPDATE users SET password_hash = ?, must_change = 1 WHERE id = ?",
                                 (generate_password_hash(values["password"]), uid))
                db().commit()
                flash("Account updated.", "ok")
                return redirect(url_for("users"))
        return render_template("form.html", title=f"Edit account: {u['username']}", fields=fields,
                               values=request.form if request.method == "POST" else u,
                               errors=errors, back=url_for("users"))

    @app.post("/users/<int:uid>/delete")
    def user_delete(uid):
        require_admin()
        u = get_or_404("users", uid)
        if u["id"] == g.user["id"]:
            flash("You can't delete your own account.", "error")
        else:
            db().execute("DELETE FROM users WHERE id = ?", (uid,)); db().commit()
            flash(f"Account '{u['username']}' deleted.", "ok")
        return redirect(url_for("users"))

    # ---- generic helpers -------------------------------------------------------
    def get_or_404(table, rid, **extra):
        where = " AND ".join(["id = ?"] + [f"{k} = ?" for k in extra])
        row = db().execute(f"SELECT * FROM {table} WHERE {where}", (rid, *extra.values())).fetchone()
        if row is None:
            abort(404)
        return row

    def save(table, fields, values, rid=None, extra=None):
        """Insert/update from validated values; returns error message or None."""
        data = {**values, **(extra or {})}
        cols = list(data)
        try:
            if rid is None:
                db().execute(f"INSERT INTO {table} ({','.join(cols)}) "
                             f"VALUES ({','.join('?' * len(cols))})", [data[c] for c in cols])
            else:
                db().execute(f"UPDATE {table} SET {','.join(c + '=?' for c in cols)} WHERE id=?",
                             [data[c] for c in cols] + [rid])
            db().commit()
        except sqlite3.IntegrityError as e:
            db().rollback()
            msg = str(e)
            if "UNIQUE" in msg:
                col = msg.rsplit(".", 1)[-1]
                return {"email": "Someone else already has this email address.",
                        "university_id": "Another student already has this university ID."
                        }.get(col, "That entry already exists.")
            return "Could not save: the data conflicts with existing records."
        return None

    def form_page(table, title, row, back, post_extra=None, extra=None, rid=None):
        fields = fields_for(table, db())
        if request.method == "POST":
            values, errors = parse_form(fields, request.form)
            if "start_date" in values and "end_date" in values and not errors and \
                    values["end_date"] and values["end_date"] < values["start_date"]:
                errors["end_date"] = "End date can't be before the start date."
            if not errors:
                problem = save(table, fields, values, rid, extra)
                if problem is None:
                    flash("Saved.", "ok")
                    return redirect(back)
                errors["_form"] = problem
            current = request.form
        else:
            errors, current = {}, (row or {"status": "active",
                                   "start_year": datetime.date.today().year,
                                   "start_date": datetime.date.today().isoformat(),
                                   "outcome": "pending", "role": "advisor",
                                   "term_year": datetime.date.today().year})
        return render_template("form.html", title=title, fields=fields, values=current,
                               errors=errors, back=back)

    def require_editor():
        if not can_edit(g.user):
            abort(403, "Your account is view-only.")

    def require_admin():
        if g.user["role"] != "admin":
            abort(403, "Only administrators can manage accounts.")

    # ---- students --------------------------------------------------------------
    @app.route("/")
    def home():
        return redirect(url_for("students"))

    @app.route("/students")
    def students():
        q = request.args.get("q", "").strip()
        status = request.args.get("status", "")
        faculty_id = request.args.get("faculty", "")
        role = request.args.get("role", "any")
        if role not in FACULTY_ROLE_FILTERS:
            role = "any"
        sql = """SELECT s.*, (SELECT group_concat(f.last_name, ', ') FROM advising a
                  JOIN faculty f ON f.id = a.faculty_id
                  WHERE a.student_id = s.id AND a.end_date IS NULL AND a.role IN ('advisor','co-advisor'))
                  AS advisors FROM students s WHERE 1=1"""
        params = []
        if q:
            sql += """ AND (s.first_name || ' ' || s.last_name LIKE ? ESCAPE '\\' OR s.email LIKE ? ESCAPE '\\'
                       OR s.university_id LIKE ? ESCAPE '\\' OR s.research_area LIKE ? ESCAPE '\\')"""
            pat = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params += [pat] * 4
        if status in STATUSES:
            sql += " AND s.status = ?"; params.append(status)
        if faculty_id.isdigit() or role != "any":
            clause, p = physdb.faculty_role_clause(role, int(faculty_id) if faculty_id.isdigit() else None)
            sql += f" AND {clause}"; params += p
        rows = db().execute(sql + " ORDER BY s.last_name, s.first_name", params).fetchall()
        faculty = db().execute("SELECT id, last_name, first_name FROM faculty ORDER BY last_name").fetchall()
        return render_template("students.html", rows=rows, faculty=faculty, q=q, status=status,
                               faculty_id=faculty_id, role=role, role_filters=FACULTY_ROLE_FILTERS,
                               statuses=STATUSES)

    @app.route("/students/new", methods=["GET", "POST"])
    def student_new():
        require_editor()
        return form_page("students", "Add student", None, url_for("students"))

    @app.route("/students/<int:sid>")
    def student(sid):
        s = get_or_404("students", sid)
        d = db()
        advising = d.execute(f"""SELECT a.*, f.first_name, f.last_name FROM advising a
                                JOIN faculty f ON f.id = a.faculty_id WHERE a.student_id = ?
                                ORDER BY {physdb.ADVISING_ORDER_SQL}""", (sid,)).fetchall()
        milestones = d.execute("SELECT * FROM milestones WHERE student_id = ? ORDER BY date", (sid,)).fetchall()
        funding = d.execute(f"SELECT * FROM funding WHERE student_id = ? ORDER BY {physdb.FUNDING_ORDER_SQL}",
                            (sid,)).fetchall()
        return render_template("student.html", s=s, advising=advising, milestones=milestones,
                               funding=funding, titles=CHILD_TABLES)

    @app.route("/students/<int:sid>/edit", methods=["GET", "POST"])
    def student_edit(sid):
        require_editor()
        s = get_or_404("students", sid)
        return form_page("students", f"Edit {s['first_name']} {s['last_name']}", s,
                         url_for("student", sid=sid), rid=sid)

    @app.post("/students/<int:sid>/delete")
    def student_delete(sid):
        s = get_or_404("students", sid)
        db().execute("DELETE FROM students WHERE id = ?", (sid,)); db().commit()
        flash(f"Deleted {s['first_name']} {s['last_name']}.", "ok")
        return redirect(url_for("students"))

    # ---- advising / milestones / funding ---------------------------------------
    def child_table(table):
        if table not in CHILD_TABLES:
            abort(404)
        return table

    @app.route("/students/<int:sid>/<table>/new", methods=["GET", "POST"])
    def child_new(sid, table):
        require_editor()
        s, table = get_or_404("students", sid), child_table(table)
        return form_page(table, f"Add: {CHILD_TABLES[table].lower()} for {s['first_name']} {s['last_name']}",
                         None, url_for("student", sid=sid), extra={"student_id": sid})

    @app.route("/students/<int:sid>/<table>/<int:rid>/edit", methods=["GET", "POST"])
    def child_edit(sid, table, rid):
        require_editor()
        table = child_table(table)
        row = get_or_404(table, rid, student_id=sid)
        return form_page(table, f"Edit: {CHILD_TABLES[table].lower()}", row,
                         url_for("student", sid=sid), rid=rid, extra={"student_id": sid})

    @app.post("/students/<int:sid>/<table>/<int:rid>/delete")
    def child_delete(sid, table, rid):
        table = child_table(table)
        get_or_404(table, rid, student_id=sid)
        db().execute(f"DELETE FROM {table} WHERE id = ?", (rid,)); db().commit()
        flash("Deleted.", "ok")
        return redirect(url_for("student", sid=sid))

    # ---- faculty ---------------------------------------------------------------
    @app.route("/faculty")
    def faculty_list():
        rows = db().execute("""SELECT f.*, (SELECT count(*) FROM advising a WHERE a.faculty_id = f.id
                  AND a.end_date IS NULL AND a.role IN ('advisor','co-advisor')) AS n_advisees,
                  (SELECT count(DISTINCT a.student_id) FROM advising a WHERE a.faculty_id = f.id
                  AND a.end_date IS NULL) AS n_committees
                  FROM faculty f ORDER BY f.last_name, f.first_name""").fetchall()
        return render_template("faculty.html", rows=rows)

    @app.route("/faculty/new", methods=["GET", "POST"])
    def faculty_new():
        require_editor()
        return form_page("faculty", "Add faculty member", None, url_for("faculty_list"))

    @app.route("/faculty/<int:fid>")
    def faculty_detail(fid):
        f = get_or_404("faculty", fid)
        rows = db().execute("""SELECT s.id, s.first_name, s.last_name, s.status, a.role, a.start_date
                  FROM advising a JOIN students s ON s.id = a.student_id
                  WHERE a.faculty_id = ? AND a.end_date IS NULL
                  ORDER BY s.last_name, s.first_name""", (fid,)).fetchall()
        return render_template("faculty_detail.html", f=f, rows=rows)

    @app.route("/faculty/<int:fid>/edit", methods=["GET", "POST"])
    def faculty_edit(fid):
        require_editor()
        f = get_or_404("faculty", fid)
        return form_page("faculty", f"Edit {f['first_name']} {f['last_name']}", f,
                         url_for("faculty_detail", fid=fid), rid=fid)

    @app.post("/faculty/<int:fid>/delete")
    def faculty_delete(fid):
        get_or_404("faculty", fid)
        try:
            db().execute("DELETE FROM faculty WHERE id = ?", (fid,)); db().commit()
        except sqlite3.IntegrityError:
            db().rollback()
            flash("This person is listed as an advisor or committee member for a student. "
                  "Remove those entries first.", "error")
            return redirect(url_for("faculty_detail", fid=fid))
        flash("Deleted.", "ok")
        return redirect(url_for("faculty_list"))

    # ---- reports / export ------------------------------------------------------
    @app.route("/reports")
    def reports():
        kind = request.args.get("kind", "qualifying_exam")
        if kind not in MILESTONE_KINDS:
            kind = "qualifying_exam"
        try:
            years = max(0, min(15, int(request.args.get("years", 2))))
        except ValueError:
            years = 2
        rows = physdb.missing_milestone(db(), kind, years)
        return render_template("reports.html", rows=rows, kind=kind, years=years, kinds=MILESTONE_KINDS)

    @app.route("/export.csv")
    def export():
        import io
        buf = io.StringIO()
        physdb.export_csv(db(), buf)
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=students.csv"})

    return app


def _persistent_secret():
    path = Path(os.environ.get("PHYSDB_SECRET_FILE", ".secret_key"))
    if not path.exists():
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600)
    return path.read_text().strip()


DEFAULT_PORT = 8123
APP_MARKER = "Physics Graduate Students"


def _port_free(host, port):
    with socket.socket() as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _our_app_running(host, port):
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/login", timeout=1.5) as r:
            return APP_MARKER in r.read(4000).decode("utf-8", "ignore")
    except Exception:
        return False


def serve(host, port, open_browser=True):
    """Start the server, coping with the usual 'it was already running' / 'port taken' cases."""
    if _our_app_running(host, port):
        url = f"http://{host}:{port}"
        print(f"The app is already running at {url} -- opening it. (To restart it, close the "
              "window it is running in first.)")
        if open_browser:
            webbrowser.open(url)
        return
    chosen = next((p for p in range(port, port + 20) if _port_free(host, p)), None)
    if chosen is None:
        raise SystemExit(f"Ports {port}-{port + 19} are all in use. Use --port to choose another.")
    if chosen != port:
        print(f"Port {port} is used by another program, so using port {chosen} instead.")
    url = f"http://{host}:{chosen}"
    print(f"Open {url} in your browser. Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    create_app().run(host=host, port=chosen)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="start the development server")
    r.add_argument("--host", default="127.0.0.1"); r.add_argument("--port", type=int, default=DEFAULT_PORT)
    r.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    u = sub.add_parser("create-user", help="add a login, or reset an existing one's password/role")
    u.add_argument("username"); u.add_argument("--role", choices=ACCOUNT_ROLES, default="viewer")
    sub.add_parser("ensure-admin", help="if there are no logins yet, prompt to create the first administrator")
    args = p.parse_args()
    if args.cmd == "run":
        serve(args.host, args.port, not args.no_browser)
    elif args.cmd == "create-user":
        save_user(args.username, args.role)
    else:
        if physdb.connect().execute("SELECT count(*) FROM users").fetchone()[0] == 0:
            print("No accounts exist yet. Let's create the first administrator.")
            name = input("Username [admin]: ").strip() or "admin"
            save_user(name, "admin")


def save_user(username, role):
    pw = getpass.getpass("Password: ")
    if len(pw) < MIN_PASSWORD or pw != getpass.getpass("Repeat password: "):
        raise SystemExit(f"Passwords must match and be at least {MIN_PASSWORD} characters.")
    conn = physdb.connect()
    conn.execute("""INSERT INTO users (username, password_hash, role) VALUES (?,?,?)
                    ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash,
                    role=excluded.role, must_change=0""", (username, generate_password_hash(pw), role))
    conn.commit()
    print(f"User '{username}' saved as {role}.")


if __name__ == "__main__":
    main()
