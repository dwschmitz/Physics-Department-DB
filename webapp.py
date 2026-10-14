#!/usr/bin/env python3
"""Web interface for the physics graduate student database."""
import argparse
import datetime
import getpass
import hmac
import os
import secrets
import sqlite3
from pathlib import Path

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import physdb

STATUSES = ["active", "on_leave", "graduated", "withdrawn"]
PROGRAMS = ["PhD", "MS"]
ROLES = ["advisor", "co-advisor", "committee", "committee_chair"]
MILESTONE_KINDS = ["qualifying_exam", "candidacy", "thesis_proposal", "thesis_defense", "other"]
OUTCOMES = ["pending", "passed", "conditional", "failed"]
FUNDING_KINDS = ["TA", "RA", "fellowship", "other"]

CHILD_TABLES = {
    "advising": "Advisor / committee member",
    "milestones": "Milestone",
    "funding": "Funding",
}
_DUMMY_HASH = generate_password_hash("not-a-real-password")


def label(value):
    return str(value).replace("_", " ").capitalize() if value not in ("PhD", "MS", "TA", "RA") else value


def F(name, text, kind="text", required=False, choices=None, help=None):
    return dict(name=name, label=text, type=kind, required=required, choices=choices, help=help)


def fields_for(table, conn):
    """Form definition for a table. Choices are (value, label) pairs."""
    enum = lambda vals: [(v, label(v)) for v in vals]
    if table == "students":
        return [
            F("first_name", "First name", required=True),
            F("last_name", "Last name", required=True),
            F("university_id", "University ID", required=True),
            F("email", "Email", "email", True),
            F("program", "Program", "select", True, enum(PROGRAMS)),
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
                F("term", "Term", required=True, help="For example: Fall 2026"),
                F("source", "Source"),
                F("amount", "Amount ($)", "number")]
    raise KeyError(table)


def parse_form(fields, form):
    """Validate submitted values. Returns (values, errors)."""
    values, errors = {}, {}
    for f in fields:
        raw = (form.get(f["name"]) or "").strip()
        name, kind = f["name"], f["type"]
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
            if g.user is None:
                session.clear()
        if request.endpoint in ("login", "static"):
            return None
        if g.user is None:
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        if request.method == "POST":
            sent = request.form.get("csrf_token", "")
            if not hmac.compare_digest(sent, session.get("csrf", "")):
                abort(400, "Your session expired. Go back, reload the page and try again.")
            if g.user["role"] != "editor" and request.endpoint != "logout":
                abort(403, "Your account is view-only.")

    @app.context_processor
    def inject():
        session.setdefault("csrf", secrets.token_urlsafe(32))
        return dict(csrf_token=session["csrf"], user=g.get("user"),
                    can_edit=bool(g.get("user") and g.user["role"] == "editor"))

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
            errors, current = {}, (row or {"status": "active", "program": "PhD",
                                   "start_year": datetime.date.today().year,
                                   "start_date": datetime.date.today().isoformat(),
                                   "outcome": "pending", "role": "advisor"})
        return render_template("form.html", title=title, fields=fields, values=current,
                               errors=errors, back=back)

    def require_editor():
        if g.user["role"] != "editor":
            abort(403, "Your account is view-only.")

    # ---- students --------------------------------------------------------------
    @app.route("/")
    def home():
        return redirect(url_for("students"))

    @app.route("/students")
    def students():
        q = request.args.get("q", "").strip()
        status = request.args.get("status", "")
        program = request.args.get("program", "")
        advisor = request.args.get("advisor", "")
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
        if program in PROGRAMS:
            sql += " AND s.program = ?"; params.append(program)
        if advisor.isdigit():
            sql += " AND EXISTS (SELECT 1 FROM advising a WHERE a.student_id = s.id " \
                   "AND a.faculty_id = ? AND a.end_date IS NULL)"
            params.append(int(advisor))
        rows = db().execute(sql + " ORDER BY s.last_name, s.first_name", params).fetchall()
        faculty = db().execute("SELECT id, last_name, first_name FROM faculty ORDER BY last_name").fetchall()
        return render_template("students.html", rows=rows, faculty=faculty, q=q, status=status,
                               program=program, advisor=advisor, statuses=STATUSES, programs=PROGRAMS)

    @app.route("/students/new", methods=["GET", "POST"])
    def student_new():
        require_editor()
        return form_page("students", "Add student", None, url_for("students"))

    @app.route("/students/<int:sid>")
    def student(sid):
        s = get_or_404("students", sid)
        d = db()
        advising = d.execute("""SELECT a.*, f.first_name, f.last_name FROM advising a
                                JOIN faculty f ON f.id = a.faculty_id WHERE a.student_id = ?
                                ORDER BY a.end_date IS NOT NULL, a.start_date DESC""", (sid,)).fetchall()
        milestones = d.execute("SELECT * FROM milestones WHERE student_id = ? ORDER BY date", (sid,)).fetchall()
        funding = d.execute("SELECT * FROM funding WHERE student_id = ? ORDER BY id DESC", (sid,)).fetchall()
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
                  AND a.end_date IS NULL AND a.role IN ('advisor','co-advisor')) AS n_advisees
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


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="start the development server")
    r.add_argument("--host", default="127.0.0.1"); r.add_argument("--port", type=int, default=5000)
    u = sub.add_parser("create-user", help="add a login, or reset an existing one's password/role")
    u.add_argument("username"); u.add_argument("--role", choices=["viewer", "editor"], default="viewer")
    args = p.parse_args()
    if args.cmd == "run":
        create_app().run(host=args.host, port=args.port)
    else:
        pw = getpass.getpass("Password: ")
        if len(pw) < 10 or pw != getpass.getpass("Repeat password: "):
            raise SystemExit("Passwords must match and be at least 10 characters.")
        conn = physdb.connect()
        conn.execute("""INSERT INTO users (username, password_hash, role) VALUES (?,?,?)
                        ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash,
                        role=excluded.role""", (args.username, generate_password_hash(pw), args.role))
        conn.commit()
        print(f"User '{args.username}' saved as {args.role}.")


if __name__ == "__main__":
    main()
