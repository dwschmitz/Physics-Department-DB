#!/usr/bin/env python3
"""Web interface for the physics graduate student database."""
import argparse
import datetime
import getpass
import hmac
import math
import os
import re
import secrets
import socket
import sqlite3
import threading
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import urlencode

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import physdb

STATUSES = ["active", "on_leave", "graduated", "withdrawn"]
FACULTY_ROLE_FILTERS = {"any": "Any role", "advisor": "Advisor",
                        "committee": "Committee member only"}
ACCOUNT_ROLES = ["viewer", "editor", "admin"]      # roles an admin can hand out directly
ROLE_ORDER = ACCOUNT_ROLES + ["faculty", "fsr"]       # faculty / FSR logins are made from the person, see user_new_faculty
# The ONLY pages a faculty login may open (everything else is refused): their own page, a read-only page for each
# of their advisees, changing their password, signing in/out, and the stylesheet.
FACULTY_ENDPOINTS = {"faculty_home", "faculty_student", "account", "logout", "login", "static", "home"}
# An FSR login gets just their own page (the advisees of the faculty they are FSR for) and the same basics.
FSR_ENDPOINTS = {"fsr_home", "account", "logout", "login", "static", "home"}
LIMITED_ENDPOINTS = {"faculty": FACULTY_ENDPOINTS, "fsr": FSR_ENDPOINTS}   # login role -> the only pages it may open
HOME_ENDPOINT = {"faculty": "faculty_home", "fsr": "fsr_home"}              # where each limited role lands
MIN_PASSWORD = 10
ROLES = ["advisor", "co-advisor", "committee", "committee_chair"]
MILESTONE_KINDS = physdb.MILESTONE_KINDS
OUTCOMES = ["pending", "passed", "conditional", "failed"]
FUNDING_KINDS = ["TA", "RA", "fellowship", "supplement", "other"]
DISPLAY_NAMES = {"gde_classical_mechanics": "GDE Classical Mechanics", "gde_quantum_mechanics": "GDE Quantum Mechanics",
                 "gde_electrodynamics": "GDE Electrodynamics", "gde_statistical_mechanics": "GDE Statistical Mechanics",
                 "candidacy": "Candidacy", "ms_degree": "MS Degree", "committee_formed": "Committee Formed", "pre_oral": "Pre-Oral",
                 "thesis_defense": "Thesis Defense", "qualifying_exam": "Qualifying Exam",
                 "thesis_proposal": "Thesis Proposal", "citizen": "Citizen",
                 "permanent_resident": "Permanent Resident", "international": "International", "fsr": "FSR"}

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


def fields_for(table, conn, row=None):
    """Form definition for a table. Choices are (value, label) pairs."""
    enum = lambda vals: [(v, label(v)) for v in vals]
    if table == "students":
        return [
            F("first_name", "First name", required=True),
            F("last_name", "Last name", required=True),
            F("university_id", "University ID", required=True),
            F("cnet", "CNET", "cnet"),
            F("email", "Email", "email", True),
            F("citizenship", "Citizenship", "select", True, enum(physdb.CITIZENSHIP), placeholder=True),
            F("status", "Status", "select", True, enum(STATUSES)),
            F("matriculation_year", "Matriculation year", "year", True),
            F("matriculation_term", "Matriculation term", "select", True, [(s, s) for s in physdb.SEASONS]),
            F("expected_grad_year", "Expected graduation year", "year"),
            F("research_area", "Research area"),
            F("notes", "Notes", "textarea"),
        ]
    if table == "faculty":
        reps = conn.execute("SELECT id, last_name, first_name FROM fsrs ORDER BY last_name, first_name").fetchall()
        return [F("first_name", "First name", required=True),
                F("last_name", "Last name", required=True),
                F("email", "Email", "email", True),
                F("cnet", "CNET", "cnet", help="Also their login name if they are given an account."),
                F("research_area", "Research area"),
                F("fsr_id", "FSR", "select", False, [(r["id"], f"{r['last_name']}, {r['first_name']}") for r in reps],
                  help="The financial services rep who helps manage this person's funding accounts.")]
    if table == "fsrs":
        return [F("first_name", "First name", required=True),
                F("last_name", "Last name", required=True),
                F("email", "Email", "email", True),
                F("cnet", "CNET", "cnet", help="Also their login name if they are given an account.")]
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
        kinds = enum(MILESTONE_KINDS)
        if row is not None and row["kind"] in physdb.RETIRED_MILESTONE_KINDS:
            # an entry saved under a retired type keeps it (selected) so editing it can't change it by accident
            kinds.insert(0, (row["kind"], f"{label(row['kind'])} (retired)"))
        return [F("kind", "Milestone", "select", True, kinds),
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
            elif kind == "cnet":
                if not re.fullmatch(physdb.CNET_PATTERN, raw):
                    raise ValueError("Enter 2-40 letters, digits, or . _ - (no spaces).")
                raw = raw.lower()
            elif kind == "password" and len(raw) < MIN_PASSWORD:
                raise ValueError(f"Enter at least {MIN_PASSWORD} characters.")
            elif kind == "email" and ("@" not in raw or " " in raw):
                raise ValueError("Enter a valid email address.")
        except ValueError as e:
            msg = str(e)
            errors[name] = msg if "Enter" in msg or "choose" in msg else "Invalid value."
        values[name] = raw
    return values, errors


def _safe_path(value):
    """A same-site path ('/students?x=1') that is safe to redirect to, else None (no open redirects)."""
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value \
            and len(value) < 500 and "\n" not in value and "\r" not in value:
        return value
    return None


def _text(value):
    """Case-insensitive sort key; blank values sort last."""
    value = (value or "").strip().lower()
    return value or None


def sort_rows(rows, keys, sort, sdir, default, tiebreak=None):
    """Sort dict-like rows by one of `keys` (name -> function). Unknown names fall back to `default`.

    Rows whose key is None (blank / not applicable) always go last, whichever way you sort.
    Returns (rows, sort, sdir) so the page can show which column is active.
    """
    sort = sort if sort in keys else default
    reverse = sdir == "desc"
    rows = sorted(rows, key=tiebreak or keys[default])               # stable base order: by name
    present = [r for r in rows if keys[sort](r) is not None]
    blank = [r for r in rows if keys[sort](r) is None]
    present.sort(key=keys[sort], reverse=reverse)
    return present + blank, sort, "desc" if reverse else "asc"


def create_app(db_path=None):
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path or physdb.DB_PATH
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=8))
    app.config["SECRET_KEY"] = os.environ.get("PHYSDB_SECRET") or _persistent_secret()
    app.jinja_env.filters["label"] = label
    app.jinja_env.filters["term"] = physdb.term_label
    app.jinja_env.filters["matric"] = physdb.matriculation_label

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
        limited = LIMITED_ENDPOINTS.get(g.user["role"])
        if limited is not None and request.endpoint not in limited:
            abort(403, f"This page isn't available for {g.user['role']} accounts.")    # default deny
        if g.user["must_change"] and request.endpoint not in ("account", "logout"):
            return redirect(url_for("account"))

    @app.context_processor
    def sort_helpers():
        def sort_url(key, cur_sort, cur_dir, prefix=""):
            """Link for a column header: keeps the current filters, toggles direction on the active column.
            A page with two sortable tables gives the second a prefix (csort / cdir)."""
            args = request.args.to_dict(flat=True)
            args.update({prefix + "sort": key, prefix + "dir": "desc" if (cur_sort == key and cur_dir == "asc") else "asc"})
            return request.path + "?" + urlencode(args)
        return dict(sort_url=sort_url)

    @app.context_processor
    def inject():
        session.setdefault("csrf", secrets.token_urlsafe(32))
        return dict(csrf_token=session["csrf"], user=g.get("user"),
                    can_edit=can_edit(g.get("user")), is_admin=bool(g.get("user") and g.user["role"] == "admin"),
                    is_faculty=bool(g.get("user") and g.user["role"] == "faculty"),
                    is_fsr=bool(g.get("user") and g.user["role"] == "fsr"))

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
                target = _safe_path(request.args.get("next"))
                if row["role"] in HOME_ENDPOINT and not (target or "").startswith(url_for(HOME_ENDPOINT[row["role"]])):
                    target = None                                   # limited logins only ever land on their own page
                return redirect(target or home_url_for(row["role"]))
            flash("Incorrect username or password.", "error")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    def home_url_for(role):
        return url_for(HOME_ENDPOINT.get(role, "students"))

    def home_url():
        return home_url_for(g.user["role"])

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    def error_page(e):
        titles = {400: "That didn't work", 403: "Not available", 404: "Page not found"}
        user = g.get("user")
        return render_template("error.html", code=e.code, title=titles[e.code], message=e.description,
                               home=(home_url() if user else url_for("login"))), e.code

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
                return redirect(home_url())
        if g.user["must_change"]:
            flash("Please choose a new password before continuing.", "error")
        return render_template("form.html", title="Change your password", fields=fields,
                               values={}, errors=errors, back=home_url())

    def user_fields(creating):
        return [F("username", "Username", "username", True)] if creating else []

    def role_field():
        return F("role", "Role", "select", True, [(r, label(r)) for r in ACCOUNT_ROLES],
                 help="Viewer: look only. Editor: can also change records. "
                      "Administrator: can also manage accounts.")

    @app.route("/users")
    def users():
        require_admin()
        rows = [dict(r) for r in db().execute(
            """SELECT u.*, COALESCE(f.first_name, r.first_name) AS f_first, COALESCE(f.last_name, r.last_name) AS f_last
               FROM users u LEFT JOIN faculty f ON f.id = u.faculty_id LEFT JOIN fsrs r ON r.id = u.fsr_id""").fetchall()]
        rows, sort, sdir = sort_rows(rows, {
            "username": lambda r: _text(r["username"]),
            "role": lambda r: ROLE_ORDER.index(r["role"]),
            "status": lambda r: r["must_change"],
        }, request.args.get("sort"), request.args.get("dir"), "username")
        return render_template("users.html", rows=rows, sort=sort, sdir=sdir)

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

    @app.route("/users/new-faculty", methods=["GET", "POST"])
    def user_new_faculty():
        """A login for a faculty member. The username is their CNET, which is also what ties the login to them."""
        require_admin()
        candidates = db().execute(
            """SELECT f.id, f.last_name, f.first_name, f.cnet FROM faculty f
               WHERE f.cnet IS NOT NULL AND NOT EXISTS (SELECT 1 FROM users u WHERE u.faculty_id = f.id)
               ORDER BY f.last_name, f.first_name""").fetchall()
        no_cnet = db().execute("SELECT last_name, first_name FROM faculty WHERE cnet IS NULL "
                               "ORDER BY last_name, first_name").fetchall()
        fields = [F("faculty_id", "Faculty member", "select", True,
                    [(r["id"], f"{r['last_name']}, {r['first_name']} ({r['cnet']})") for r in candidates],
                    placeholder=True, help="Only faculty with a CNET and no account yet are listed."),
                  F("password", "Temporary password", "password", True,
                    help=f"At least {MIN_PASSWORD} characters. They must choose their own at first login.")]
        intro = "Their username will be their CNET."
        if no_cnet:
            intro += (" Not listed because they have no CNET yet: "
                      + ", ".join(f"{r['first_name']} {r['last_name']}" for r in no_cnet) + ".")
        errors = {}
        if request.method == "POST":
            values, errors = parse_form(fields, request.form)
            if not errors:
                cnet = next(r["cnet"] for r in candidates if r["id"] == values["faculty_id"])
                try:
                    db().execute("INSERT INTO users (username, password_hash, role, must_change, faculty_id) "
                                 "VALUES (?,?,?,1,?)", (cnet, generate_password_hash(values["password"]),
                                                        "faculty", values["faculty_id"]))
                    db().commit()
                    flash(f"Faculty account '{cnet}' created.", "ok")
                    return redirect(url_for("users"))
                except sqlite3.IntegrityError:
                    db().rollback()
                    errors["faculty_id"] = f"A login named '{cnet}' already exists."
        return render_template("form.html", title="Add faculty account", fields=fields, intro=intro,
                               values=request.form if request.method == "POST" else {}, errors=errors,
                               back=url_for("users"))

    @app.route("/users/new-fsr", methods=["GET", "POST"])
    def user_new_fsr():
        """A login for an FSR. The username is their CNET, which is also what ties the login to them."""
        require_admin()
        candidates = db().execute(
            """SELECT r.id, r.last_name, r.first_name, r.cnet FROM fsrs r
               WHERE r.cnet IS NOT NULL AND NOT EXISTS (SELECT 1 FROM users u WHERE u.fsr_id = r.id)
               ORDER BY r.last_name, r.first_name""").fetchall()
        no_cnet = db().execute("SELECT last_name, first_name FROM fsrs WHERE cnet IS NULL "
                               "ORDER BY last_name, first_name").fetchall()
        fields = [F("fsr_id", "FSR", "select", True,
                    [(r["id"], f"{r['last_name']}, {r['first_name']} ({r['cnet']})") for r in candidates],
                    placeholder=True, help="Only FSRs with a CNET and no account yet are listed."),
                  F("password", "Temporary password", "password", True,
                    help=f"At least {MIN_PASSWORD} characters. They must choose their own at first login.")]
        intro = "Their username will be their CNET."
        if no_cnet:
            intro += (" Not listed because they have no CNET yet: "
                      + ", ".join(f"{r['first_name']} {r['last_name']}" for r in no_cnet) + ".")
        errors = {}
        if request.method == "POST":
            values, errors = parse_form(fields, request.form)
            if not errors:
                cnet = next(r["cnet"] for r in candidates if r["id"] == values["fsr_id"])
                try:
                    db().execute("INSERT INTO users (username, password_hash, role, must_change, fsr_id) "
                                 "VALUES (?,?,?,1,?)", (cnet, generate_password_hash(values["password"]),
                                                        "fsr", values["fsr_id"]))
                    db().commit()
                    flash(f"FSR account '{cnet}' created.", "ok")
                    return redirect(url_for("users"))
                except sqlite3.IntegrityError:
                    db().rollback()
                    errors["fsr_id"] = f"A login named '{cnet}' already exists."
        return render_template("form.html", title="Add FSR account", fields=fields, intro=intro,
                               values=request.form if request.method == "POST" else {}, errors=errors,
                               back=url_for("users"))

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
        if u["role"] in ("faculty", "fsr"):       # these logins are tied to a person: only the password can change
            fields = [F("password", "New temporary password", "password", True,
                        help="Signs them out everywhere and makes them choose a new password at next login.")]
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

    # ---- pages for faculty logins ----------------------------------------------
    def require_faculty():
        if g.user["role"] != "faculty":
            abort(403, "This page is for faculty accounts.")

    def term_stepper(endpoint, **url_args):
        """The funding term shown on an overview page (from ?year=&season=, else the current term) and the links for
        its Prev / Next buttons, which point back at `endpoint`."""
        now = physdb.current_term()
        offered = {y for y, _ in term_years()}
        try:
            year = int(request.args.get("year", now[1]))
        except ValueError:
            year = now[1]
        year = year if year in offered else now[1]
        season = request.args.get("season", now[0])
        season = season if season in physdb.SEASONS else now[0]
        step = lambda s, y: url_for(endpoint, year=y, season=s, **url_args) if y in offered else None
        prev_season, prev_year = physdb.previous_term(season, year)
        next_season, next_year = physdb.next_term(season, year)
        return dict(now=now, year=year, season=season, here=request.full_path.rstrip("?"),
                    prev_url=step(prev_season, prev_year), next_url=step(next_season, next_year),
                    prev_label=f"{prev_season} {prev_year}", next_label=f"{next_season} {next_year}")

    def sort_advisees(students):
        """Sort a student table by the clicked heading (?sort=&dir=); used by the faculty and FSR overviews. Needs each
        student's clock_terms and funding (this term's entries). Returns (students, sort, dir)."""
        for a in students:
            amounts = [e["amount"] for e in a["funding"] if e["amount"] is not None]
            a["funded"] = sum(amounts) if amounts else None                      # blank sorts last
        name = lambda r: (_text(r["last_name"]), _text(r["first_name"]))
        return sort_rows(students, {
            "name": name, "advisor": lambda r: _text(r.get("advisors")), "status": lambda r: STATUSES.index(r["status"]),
            "clock": lambda r: r["clock_terms"], "expected_grad_year": lambda r: r["expected_grad_year"],
            "research_area": lambda r: _text(r["research_area"]), "funding": lambda r: r["funded"],
        }, request.args.get("sort"), request.args.get("dir"), "name")

    def faculty_overview(faculty_id, endpoint, **url_args):
        """Everything the two-table faculty overview needs, for one faculty member: their advisees (with funding for a
        term and the clock) and the students whose committee they are on. Used for a faculty member's own page and for
        the staff view of a faculty member, so the two always look alike. `endpoint` is the page that the term
        Prev / Next links point back to."""
        keep = {k: request.args[k] for k in ("sort", "dir", "csort", "cdir") if k in request.args}     # Prev / Next keep the sorting
        term = term_stepper(endpoint, **url_args, **keep)
        year, season, now = term["year"], term["season"], term["now"]
        advisees = physdb.faculty_advisees(db(), faculty_id)
        funding = physdb.funding_for_term(db(), [a["id"] for a in advisees], year, season)
        for a in advisees:
            a["clock"] = physdb.clock_label(a, now)
            a["clock_terms"] = physdb.clock_terms(a, now)
            a["funding"] = funding[a["id"]]
        advisees, sort, sdir = sort_advisees(advisees)
        committee = physdb.faculty_committee_students(db(), faculty_id)
        for c in committee:
            c["clock"] = physdb.clock_label(c, now)
            c["clock_terms"] = physdb.clock_terms(c, now)
        name = lambda r: (_text(r["last_name"]), _text(r["first_name"]))
        committee, csort, csdir = sort_rows(committee, {
            "name": name, "role": lambda r: 0 if r["committee_role"] == "committee_chair" else 1,
            "status": lambda r: STATUSES.index(r["status"]), "clock": lambda r: r["clock_terms"],
            "research_area": lambda r: _text(r["research_area"]), "advisor": lambda r: _text(r["advisors"]),
        }, request.args.get("csort"), request.args.get("cdir"), "name")
        return dict(
            advisees=advisees, committee=committee, sort=sort, sdir=sdir, csort=csort, csdir=csdir, **term,
            total=sum(e["amount"] or 0 for a in advisees for e in a["funding"]),
            n_active_advisees=sum(a["status"] == "active" for a in advisees),        # the headings count active students
            n_active_committee=sum(c["status"] == "active" for c in committee))

    @app.route("/my")
    def faculty_home():
        """A faculty member's own page: their advisees (with funding for a term) and their committee students."""
        require_faculty()
        fac = get_or_404("faculty", g.user["faculty_id"])
        return render_template("faculty_home.html", fac=fac, **faculty_overview(fac["id"], "faculty_home"))

    @app.route("/my/students/<int:sid>")
    def faculty_student(sid):
        """Read-only detail for one of the signed-in faculty member's advisees (404 for anyone else's student)."""
        require_faculty()
        fid = g.user["faculty_id"]
        if not physdb.is_advisee(db(), fid, sid):
            abort(404)
        s = next(r for r in physdb.faculty_advisees(db(), fid) if r["id"] == sid)        # safe columns only
        s["clock"] = physdb.clock_label(s)
        advising = db().execute(
            f"""SELECT a.role, a.start_date, a.end_date, f.first_name, f.last_name FROM advising a
                JOIN faculty f ON f.id = a.faculty_id WHERE a.student_id = ? ORDER BY {physdb.ADVISING_ORDER_SQL}""",
            (sid,)).fetchall()
        milestones = [{**dict(m), "clock": physdb.clock_label_at(s, m["date"])} for m in db().execute(
            "SELECT kind, date, outcome FROM milestones WHERE student_id = ? ORDER BY date", (sid,))]
        funding = db().execute(f"SELECT * FROM funding WHERE student_id = ? ORDER BY {physdb.FUNDING_ORDER_SQL}",
                               (sid,)).fetchall()
        return render_template("faculty_student.html", s=s, advising=advising, milestones=milestones, funding=funding)

    # ---- FSRs --------------------------------------------------------------------
    def fsr_overview(fsr_id, endpoint, **url_args):
        """The students of every faculty member this FSR is the FSR for, with funding for a term and the clock.
        Used for an FSR's own page and for the staff view of an FSR."""
        keep = {k: request.args[k] for k in ("sort", "dir", "csort", "cdir") if k in request.args}     # Prev / Next keep the sorting
        term = term_stepper(endpoint, **url_args, **keep)
        students = physdb.fsr_advisees(db(), fsr_id)
        funding = physdb.funding_for_term(db(), [a["id"] for a in students], term["year"], term["season"])
        for a in students:
            a["clock"] = physdb.clock_label(a, term["now"])
            a["clock_terms"] = physdb.clock_terms(a, term["now"])
            a["funding"] = funding[a["id"]]
        students, sort, sdir = sort_advisees(students)
        return dict(
            advisees=students, sort=sort, sdir=sdir, **term,
            total=sum(e["amount"] or 0 for a in students for e in a["funding"]),
            n_active_advisees=sum(a["status"] == "active" for a in students))

    def fsr_faculty(fsr_id):
        return db().execute("SELECT id, first_name, last_name FROM faculty WHERE fsr_id = ? "
                            "ORDER BY last_name, first_name", (fsr_id,)).fetchall()

    @app.route("/fsr")
    def fsr_home():
        """An FSR's own page: the students of the faculty they work with."""
        if g.user["role"] != "fsr":
            abort(403, "This page is for FSR accounts.")
        fsr = get_or_404("fsrs", g.user["fsr_id"])
        return render_template("fsr_home.html", fsr=fsr, faculty=fsr_faculty(fsr["id"]),
                               **fsr_overview(fsr["id"], "fsr_home"))

    @app.route("/fsrs")
    def fsr_list():
        rows = [dict(r) for r in db().execute("SELECT * FROM fsrs").fetchall()]
        for r in rows:
            r["faculty"] = fsr_faculty(r["id"])
        name = lambda r: (_text(r["last_name"]), _text(r["first_name"]))
        rows, sort, sdir = sort_rows(rows, {
            "name": name, "email": lambda r: _text(r["email"]), "cnet": lambda r: _text(r["cnet"]),
            "faculty": lambda r: _text(r["faculty"][0]["last_name"]) if r["faculty"] else None,
        }, request.args.get("sort"), request.args.get("dir"), "name")
        return render_template("fsrs.html", rows=rows, sort=sort, sdir=sdir)

    @app.route("/fsrs/new", methods=["GET", "POST"])
    def fsr_new():
        require_editor()
        return form_page("fsrs", "Add FSR", None, url_for("fsr_list"))

    @app.route("/fsrs/<int:rid>")
    def fsr_detail(rid):
        fsr = get_or_404("fsrs", rid)
        return render_template("fsr_detail.html", fsr=fsr, faculty=fsr_faculty(rid),
                               **fsr_overview(rid, "fsr_detail", rid=rid), staff_view=True)

    @app.route("/fsrs/<int:rid>/edit", methods=["GET", "POST"])
    def fsr_edit(rid):
        require_editor()
        fsr = get_or_404("fsrs", rid)
        return form_page("fsrs", f"Edit {fsr['first_name']} {fsr['last_name']}", fsr,
                         url_for("fsr_detail", rid=rid), rid=rid)

    @app.post("/fsrs/<int:rid>/delete")
    def fsr_delete(rid):
        get_or_404("fsrs", rid)
        db().execute("DELETE FROM fsrs WHERE id = ?", (rid,)); db().commit()     # faculty keep going without an FSR
        flash("Deleted.", "ok")
        return redirect(url_for("fsr_list"))

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
            if "login account that uses their CNET" in msg:
                return ("This person has a login account that uses their CNET, so it can't be removed. Delete "
                        "their account on the Accounts page first.")
            if "UNIQUE" in msg:
                col = msg.rsplit(".", 1)[-1]
                return {("students", "email"): "Someone else already has this email address.",
                        ("faculty", "email"): "Someone else already has this email address.",
                        ("students", "university_id"): "Another student already has this university ID.",
                        ("students", "cnet"): "Another student already has this CNET.",
                        ("faculty", "cnet"): "Another faculty member already has this CNET.",
                        ("fsrs", "email"): "Someone else already has this email address.",
                        ("fsrs", "cnet"): "Another FSR already has this CNET.",
                        ("faculty", "username"): "That CNET is already the login name of a different account.",
                        }.get((table, col), "That entry already exists.")
            return "Could not save: the data conflicts with existing records."
        return None

    def form_page(table, title, row, back, post_extra=None, extra=None, rid=None):
        back = _safe_path(request.args.get("next")) or back     # ?next=... returns the user to where they came from
        fields = fields_for(table, db(), row)
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
                                   "matriculation_year": datetime.date.today().year,
                                   "matriculation_term": "Autumn",
                                   "start_date": datetime.date.today().isoformat(),
                                   "outcome": "pending", "role": "advisor",
                                   "term_year": datetime.date.today().year})
            if row is None:   # links can pre-fill a new form, e.g. ?term_year=2026&term_season=Autumn
                current = {**current, **{f["name"]: request.args[f["name"]]
                                         for f in fields if f["name"] in request.args}}
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
        return redirect(home_url())

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
                       OR s.university_id LIKE ? ESCAPE '\\' OR s.cnet LIKE ? ESCAPE '\\'
                       OR s.research_area LIKE ? ESCAPE '\\'
                       OR EXISTS (SELECT 1 FROM advising a JOIN faculty f ON f.id = a.faculty_id
                                  WHERE a.student_id = s.id AND a.end_date IS NULL
                                  AND a.role IN ('advisor', 'co-advisor')
                                  AND f.first_name || ' ' || f.last_name LIKE ? ESCAPE '\\'))"""
            pat = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params += [pat] * 6
        if status in STATUSES:
            sql += " AND s.status = ?"; params.append(status)
        if faculty_id.isdigit() or role != "any":
            clause, p = physdb.faculty_role_clause(role, int(faculty_id) if faculty_id.isdigit() else None)
            sql += f" AND {clause}"; params += p
        now = physdb.current_term()
        rows = [{**dict(r), "clock": physdb.clock_label(r, now), "clock_terms": physdb.clock_terms(r, now)}
                for r in db().execute(sql, params).fetchall()]
        name = lambda r: (_text(r["last_name"]), _text(r["first_name"]))
        rows, sort, sdir = sort_rows(rows, {
            "name": name,
            "university_id": lambda r: _text(r["university_id"]),
            "status": lambda r: STATUSES.index(r["status"]),
            "matriculation": lambda r: physdb.term_index(r["matriculation_term"], r["matriculation_year"]),
            "clock": lambda r: r["clock_terms"],
            "advisor": lambda r: _text(r["advisors"]),
            "research_area": lambda r: _text(r["research_area"]),
        }, request.args.get("sort"), request.args.get("dir"), "name")
        faculty = db().execute("SELECT id, last_name, first_name FROM faculty ORDER BY last_name").fetchall()
        return render_template("students.html", rows=rows, faculty=faculty, q=q, status=status,
                               faculty_id=faculty_id, role=role, role_filters=FACULTY_ROLE_FILTERS,
                               statuses=STATUSES, now=f"{now[0]} {now[1]}", sort=sort, sdir=sdir)

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
        milestones = [{**dict(m), "clock": physdb.clock_label_at(s, m["date"])}
                      for m in d.execute("SELECT * FROM milestones WHERE student_id = ? ORDER BY date", (sid,))]
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
        s, row = get_or_404("students", sid), get_or_404(table, rid, student_id=sid)
        return form_page(table, f"Edit: {CHILD_TABLES[table].lower()} for {s['first_name']} {s['last_name']}", row,
                         url_for("student", sid=sid), rid=rid, extra={"student_id": sid})

    @app.post("/students/<int:sid>/<table>/<int:rid>/delete")
    def child_delete(sid, table, rid):
        table = child_table(table)
        get_or_404(table, rid, student_id=sid)
        db().execute(f"DELETE FROM {table} WHERE id = ?", (rid,)); db().commit()
        flash("Deleted.", "ok")
        return redirect(_safe_path(request.args.get("next")) or url_for("student", sid=sid))

    # ---- faculty ---------------------------------------------------------------
    @app.route("/faculty")
    def faculty_list():
        rows = db().execute("""SELECT f.*, (SELECT r.last_name || ', ' || r.first_name FROM fsrs r WHERE r.id = f.fsr_id) AS fsr,
                  (SELECT count(DISTINCT a.student_id) FROM advising a JOIN students s ON s.id = a.student_id
                  WHERE a.faculty_id = f.id AND s.status = 'active'
                  AND a.end_date IS NULL AND a.role IN ('advisor','co-advisor')) AS n_advisees,
                  (SELECT count(DISTINCT a.student_id) FROM advising a JOIN students s ON s.id = a.student_id
                  WHERE a.faculty_id = f.id AND s.status = 'active' AND a.end_date IS NULL) AS n_committees
                  FROM faculty f""").fetchall()
        name = lambda r: (_text(r["last_name"]), _text(r["first_name"]))
        rows, sort, sdir = sort_rows([dict(r) for r in rows], {
            "name": name, "email": lambda r: _text(r["email"]), "cnet": lambda r: _text(r["cnet"]),
            "research_area": lambda r: _text(r["research_area"]), "fsr": lambda r: _text(r["fsr"]),
            "advisees": lambda r: r["n_advisees"], "committees": lambda r: r["n_committees"],
        }, request.args.get("sort"), request.args.get("dir"), "name")
        return render_template("faculty.html", rows=rows, sort=sort, sdir=sdir)

    @app.route("/faculty/new", methods=["GET", "POST"])
    def faculty_new():
        require_editor()
        return form_page("faculty", "Add faculty member", None, url_for("faculty_list"))

    @app.route("/faculty/<int:fid>")
    def faculty_detail(fid):
        f = get_or_404("faculty", fid)
        fsr = db().execute("SELECT * FROM fsrs WHERE id = ?", (f["fsr_id"],)).fetchone() if f["fsr_id"] else None
        return render_template("faculty_detail.html", f=f, fsr=fsr, **faculty_overview(fid, "faculty_detail", fid=fid))

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
    # The four GDE exams share one column of the Milestones Table, listed by these abbreviations; the others get
    # a column each as (milestone kind, short heading).
    GDE_EXAMS = [("gde_classical_mechanics", "CM"), ("gde_quantum_mechanics", "QM"),
                 ("gde_electrodynamics", "EM"), ("gde_statistical_mechanics", "SM")]
    MILESTONE_COLUMNS = [("candidacy", "Candidacy"), ("ms_degree", "MS"), ("committee_formed", "Committee"),
                         ("pre_oral", "Pre-Oral"), ("thesis_defense", "Defense")]

    @app.route("/reports/milestones")
    def milestones_table():
        """Snapshot: one row per active student, a checkmark in each milestone column they have passed."""
        now = physdb.current_term()
        rows = [{**r, "clock": physdb.clock_label(r, now), "clock_terms": physdb.clock_terms(r, now)}
                for r in physdb.milestone_snapshot(db())]
        for r in rows:
            r["gde"] = [abbr for kind, abbr in GDE_EXAMS if kind in r["done"]]
        keys = {"name": lambda r: (_text(r["last_name"]), _text(r["first_name"])),
                "clock": lambda r: r["clock_terms"], "advisor": lambda r: _text(r["advisors"]),
                "gde": lambda r: len(GDE_EXAMS) - len(r["gde"])}      # most exams passed first when ascending
        for kind, _ in MILESTONE_COLUMNS:       # checkmarks first when ascending, blanks first when descending
            keys[kind] = lambda r, kind=kind: 0 if kind in r["done"] else 1
        rows, sort, sdir = sort_rows(rows, keys, request.args.get("sort"), request.args.get("dir"), "name",
                                     tiebreak=keys["name"])
        return render_template("milestones_table.html", rows=rows, sort=sort, sdir=sdir,
                               columns=[(k, h, label(k)) for k, h in MILESTONE_COLUMNS],
                               gde_title=", ".join(f"{abbr} = {label(kind)[4:]}" for kind, abbr in GDE_EXAMS))

    @app.route("/reports")
    def reports():
        kind = request.args.get("kind", MILESTONE_KINDS[0])
        if kind not in MILESTONE_KINDS:
            kind = MILESTONE_KINDS[0]
        try:
            years = float(request.args.get("years", 2))                  # fractions like 0.25 are fine
            if not math.isfinite(years):
                raise ValueError
        except ValueError:
            years = 2.0
        years = max(0.0, min(15.0, years))
        now = physdb.current_term()
        name = lambda r: (_text(r["last_name"]), _text(r["first_name"]))
        rows, sort, sdir = sort_rows(
            [{**dict(r), "clock": physdb.clock_label(r, now), "clock_terms": physdb.clock_terms(r, now)}
             for r in physdb.missing_milestone(db(), kind, years, now)], {
            "name": name,
            "matriculation": lambda r: physdb.term_index(r["matriculation_term"], r["matriculation_year"]),
            "clock": lambda r: r["clock_terms"],
            "research_area": lambda r: _text(r["research_area"]),
            "advisor": lambda r: _text(r["advisors"]),
        }, request.args.get("sort"), request.args.get("dir"), "matriculation", tiebreak=name)
        return render_template("reports.html", rows=rows, kind=kind, years=years, kinds=MILESTONE_KINDS,
                               sort=sort, sdir=sdir)

    def funding_report_data():
        """Parse ?year=&season=&sort=&dir= and build the sorted report rows."""
        now_season, now_year = physdb.current_term()[0], physdb.current_term()[1]
        try:
            year = int(request.args.get("year", now_year))
        except ValueError:
            year = now_year
        if year not in [y for y, _ in term_years()]:
            year = now_year
        season = request.args.get("season", now_season)
        if season not in physdb.SEASONS:
            season = now_season
        now = physdb.current_term()
        rows = [{**r, "clock": physdb.clock_label(r, now), "clock_terms": physdb.clock_terms(r, now)}
                for r in physdb.funding_report(db(), year, season)]
        name = lambda r: (_text(r["last_name"]), _text(r["first_name"]), r["student_id"])
        rows, sort, sdir = sort_rows(rows, {
            "name": name,
            "clock": lambda r: r["clock_terms"],
            "advisor": lambda r: _text(r["advisors"]),
            "type": lambda r: _text(label(r["kind"]) if r["kind"] else None),
            "source": lambda r: _text(r["source"]),
            "poetaf_coa": lambda r: _text(r["poetaf_coa"]),
            "amount": lambda r: r["amount"],
        }, request.args.get("sort"), request.args.get("dir"), "name")
        return rows, year, season, sort, sdir

    @app.route("/reports/funding")
    def funding_report():
        rows, year, season, sort, sdir = funding_report_data()
        students = {r["student_id"] for r in rows}
        funded = {r["student_id"] for r in rows if r["funding_id"] is not None}
        prev_season, prev_year = physdb.previous_term(season, year)
        next_season, next_year = physdb.next_term(season, year)
        offered = {y for y, _ in term_years()}                 # the Year menu's range; stepping stops at its ends
        step_url = lambda s, y: (url_for("funding_report", year=y, season=s, sort=sort, dir=sdir)
                                 if y in offered else None)
        return render_template(
            "funding_report.html", rows=rows, year=year, season=season, sort=sort, sdir=sdir,
            years=term_years(), seasons=physdb.SEASONS, n_students=len(students), n_funded=len(funded),
            total=sum(r["amount"] or 0 for r in rows), undated=physdb.undated_funding_count(db()),
            here=request.full_path.rstrip("?"), prev_label=f"{prev_season} {prev_year}",
            prev_url=step_url(prev_season, prev_year), next_url=step_url(next_season, next_year),
            next_label=f"{next_season} {next_year}")

    @app.route("/students/<int:sid>/funding/copy", methods=["GET", "POST"])
    def funding_copy(sid):
        """Copy another term's funding into a term. GET shows what would be copied (as a page, or just the
        dialog body with ?fragment=1); POST does it. Params: year/season = the term copied INTO,
        from_year/from_season = the term copied FROM (default: the term before), next = where to return."""
        require_editor()
        s = get_or_404("students", sid)
        values = request.values
        offered = {y for y, _ in term_years()}
        source_years = set(range(min(offered) - 1, max(offered) + 1))    # one year further back than the menus

        def parse(prefix, years):
            try:
                year = int(values.get(prefix + "year", ""))
            except ValueError:
                abort(400)
            season = values.get(prefix + "season", "")
            if season not in physdb.SEASONS or year not in years:
                abort(400)
            return season, year

        to_season, to_year = parse("", offered)
        if values.get("from_year") or values.get("from_season"):
            from_season, from_year = parse("from_", source_years)
        else:
            from_season, from_year = physdb.previous_term(to_season, to_year)
        back = _safe_path(values.get("next")) or url_for("student", sid=sid)
        who = f"{s['last_name']}, {s['first_name']}"
        target, source = f"{to_season} {to_year}", f"{from_season} {from_year}"
        same = (from_season, from_year) == (to_season, to_year)

        if request.method == "POST":
            if same:
                flash("Choose a different term to copy from.", "error")
            else:
                n, replaced = physdb.copy_funding(db(), sid, from_year, from_season, to_year, to_season)
                if n == 0:
                    flash(f"{who} has no funding recorded for {source}, so nothing was copied.", "error")
                else:
                    flash(f"Copied {n} funding entr{'y' if n == 1 else 'ies'} from {source} to {target} for {who}"
                          + (f" (replacing {replaced})." if replaced else "."), "ok")
            return redirect(back)

        def entries(year, season):
            return [dict(r) for r in db().execute(
                "SELECT * FROM funding WHERE student_id = ? AND term_year = ? AND term_season = ? ORDER BY id",
                (sid, year, season))]

        def step(season, year):
            return (url_for("funding_copy", sid=sid, year=to_year, season=to_season, from_year=year,
                            from_season=season, next=back) if year in source_years else None)

        prev_season, prev_year = physdb.previous_term(from_season, from_year)
        next_season, next_year = physdb.next_term(from_season, from_year)
        page = render_template(
            "_funding_copy_body.html" if request.args.get("fragment") else "funding_copy.html",
            s=s, who=who, target=target, source=source, same=same, back=back,
            to_year=to_year, to_season=to_season, from_year=from_year, from_season=from_season,
            source_entries=entries(from_year, from_season), target_entries=entries(to_year, to_season),
            prev_url=step(prev_season, prev_year), next_url=step(next_season, next_year))
        return page

    @app.route("/reports/funding.csv")
    def funding_report_csv():
        import csv, io
        rows, year, season, _, _ = funding_report_data()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Last name", "First name", "University ID", "CNET", "Email", "Citizenship", "Status",
                    "Matriculation term", "Matriculation year", "Clock (years)", "Expected graduation year",
                    "Research area", "Advisor", "Student notes",
                    "Funding term", "Funding type", "Source", "POETAF/COA", "Amount", "Funding notes"])
        now = physdb.current_term()
        for r in rows:
            w.writerow([physdb._csv_safe(v) if v is not None else "" for v in (
                r["last_name"], r["first_name"], r["university_id"], r["cnet"], r["email"],
                label(r["citizenship"]) if r["citizenship"] else None, label(r["status"]),
                r["matriculation_term"], r["matriculation_year"],
                None if physdb.clock_label(r, now) == "\u2014" else physdb.clock_label(r, now),
                r["expected_grad_year"], r["research_area"], r["advisors_full"], r["student_notes"],
                f"{season} {year}", label(r["kind"]) if r["kind"] else None, r["source"], r["poetaf_coa"],
                r["amount"], r["notes"])])
        return Response(buf.getvalue(), mimetype="text/csv", headers={
            "Content-Disposition": f"attachment; filename=funding_{season}_{year}.csv"})

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
    existing = conn.execute("SELECT role FROM users WHERE username = ?", (username,)).fetchone()
    if existing and existing["role"] == "faculty":
        raise SystemExit(f"'{username}' is a faculty login. Manage it on the Accounts page of the web interface.")
    conn.execute("""INSERT INTO users (username, password_hash, role) VALUES (?,?,?)
                    ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash,
                    role=excluded.role, must_change=0""", (username, generate_password_hash(pw), role))
    conn.commit()
    print(f"User '{username}' saved as {role}.")


if __name__ == "__main__":
    main()
