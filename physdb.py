#!/usr/bin/env python3
"""Graduate student tracking for the physics department (SQLite + CLI)."""
import argparse
import csv
import datetime
import os
import re
import sqlite3
import sys
from pathlib import Path

DB_PATH = os.environ.get("PHYSDB_PATH", "physics_grads.db")
SCHEMA = Path(__file__).with_name("schema.sql")


# Terms in calendar order within a year (the university year runs Autumn, Winter, Spring, Summer).
CITIZENSHIP = ["citizen", "permanent_resident", "international"]
# Milestone types offered for new entries, in menu order. The retired two are no longer offered, but entries that
# already use them are kept (and still shown, with their old names) rather than deleted or silently relabeled.
MILESTONE_KINDS = ["gde_classical_mechanics", "gde_quantum_mechanics", "gde_electrodynamics",
                   "gde_statistical_mechanics", "candidacy", "ms_degree", "committee_formed", "pre_oral", "thesis_defense", "other"]
RETIRED_MILESTONE_KINDS = ["qualifying_exam", "thesis_proposal"]
SEASONS = ["Autumn", "Winter", "Spring", "Summer"]
# SQL expression giving chronological position of a season within its calendar year.
SEASON_RANK_SQL = ("CASE term_season WHEN 'Winter' THEN 1 WHEN 'Spring' THEN 2 "
                   "WHEN 'Summer' THEN 3 WHEN 'Autumn' THEN 4 END")
# ORDER BY fragment: newest term first, entries without a term last.
FUNDING_ORDER_SQL = f"term_year IS NULL, term_year DESC, {SEASON_RANK_SQL} DESC, id DESC"


# ORDER BY fragment for a student's advisors/committee (tables aliased `a` = advising, `f` = faculty):
# current entries first, then ended ones; within each: advisor, co-advisor, committee chair, members.
ADVISING_ORDER_SQL = ("a.end_date IS NOT NULL, "
                      "CASE a.role WHEN 'advisor' THEN 1 WHEN 'co-advisor' THEN 2 "
                      "WHEN 'committee_chair' THEN 3 WHEN 'committee' THEN 4 ELSE 5 END, "
                      "f.last_name, f.first_name, a.start_date")


# Approximate first day (month, day) of each term, used to work out the current term from today's
# date. Real term start dates move a little each year; edit this one table if you want them to match
# the registrar's calendar more closely. Terms are listed in calendar order within a year.
TERM_STARTS = [("Winter", (1, 1)), ("Spring", (3, 25)), ("Summer", (6, 15)), ("Autumn", (9, 25))]
_TERM_POSITION = {name: i for i, (name, _) in enumerate(TERM_STARTS)}


def current_term(today=None):
    """(season, calendar year) for today's date, e.g. ('Autumn', 2026)."""
    today = today or datetime.date.today()
    season = [name for name, start in TERM_STARTS if start <= (today.month, today.day)][-1]
    return season, today.year


def term_index(season, year):
    """Consecutive number for each term: Autumn 2025 -> n, Winter 2026 -> n+1, ..."""
    return year * len(TERM_STARTS) + _TERM_POSITION[season]


def previous_term(season, year):
    """The term just before this one: ('Winter', 2027) -> ('Autumn', 2026)."""
    n = term_index(season, year) - 1
    return TERM_STARTS[n % len(TERM_STARTS)][0], n // len(TERM_STARTS)


def next_term(season, year):
    """The term just after this one: ('Autumn', 2026) -> ('Winter', 2027)."""
    n = term_index(season, year) + 1
    return TERM_STARTS[n % len(TERM_STARTS)][0], n // len(TERM_STARTS)


def copy_funding(conn, student_id, from_year, from_season, to_year, to_season):
    """Make a student's funding for one term a copy of their funding for another term.

    Every entry of the source term (type, source, POETAF/COA, amount, notes) is copied. Any entries the student
    already has for the target term are replaced. If the source term has no entries nothing is changed at all.
    Returns (entries copied, entries replaced).
    """
    if (from_year, from_season) == (to_year, to_season):
        raise ValueError("choose a different term to copy from")
    src = conn.execute("SELECT * FROM funding WHERE student_id = ? AND term_year = ? AND term_season = ? "
                       "ORDER BY id", (student_id, from_year, from_season)).fetchall()
    if not src:
        return 0, 0
    with conn:   # all or nothing
        replaced = conn.execute("DELETE FROM funding WHERE student_id = ? AND term_year = ? AND term_season = ?",
                                (student_id, to_year, to_season)).rowcount
        for r in src:
            conn.execute("INSERT INTO funding (student_id, kind, source, poetaf_coa, term_year, term_season, "
                         "amount, notes) VALUES (?,?,?,?,?,?,?,?)",
                         (student_id, r["kind"], r["source"], r["poetaf_coa"], to_year, to_season,
                          r["amount"], r["notes"]))
    return len(src), replaced


def copy_previous_funding(conn, student_id, year, season):
    """copy_funding from the term just before (season, year). Returns (copied, previous term, replaced)."""
    prev_season, prev_year = previous_term(season, year)
    copied, replaced = copy_funding(conn, student_id, prev_year, prev_season, year, season)
    return copied, (prev_season, prev_year), replaced


def clock_terms(student, now=None):
    """Whole terms from matriculation to the current term, or None (graduated/withdrawn/not yet started)."""
    if student["status"] in ("graduated", "withdrawn"):
        return None
    now = now or current_term()
    terms = term_index(*now) - term_index(student["matriculation_term"], student["matriculation_year"])
    return terms if terms >= 0 else None


def _format_terms(terms):
    """Terms -> years as text: 0 -> '0.0', 1 -> '0.25', 4 -> '1.0', 6 -> '1.5'."""
    text = f"{terms / len(TERM_STARTS):.2f}"
    return text[:-1] if text.endswith("0") else text


def clock_label(student, now=None):
    """Years since matriculation counted in whole terms (0.25 per term), up to the current term.

    Shown as '0.0', '0.25', '0.5', '1.0', ... Blank ('—') for students who have graduated or
    withdrawn (no end term is recorded) and for students whose matriculation is still in the future.
    """
    terms = clock_terms(student, now)
    return "—" if terms is None else _format_terms(terms)


def clock_label_at(student, when):
    """The same clock as it stood on a given date (a date or 'YYYY-MM-DD'): used for milestones.

    Unlike clock_label this ignores whether the student has since graduated or withdrawn, since the
    question is how far along they were then. '—' if the date is before they matriculated (or isn't a date).
    """
    try:
        day = when if isinstance(when, datetime.date) else datetime.date.fromisoformat(str(when))
    except ValueError:
        return "—"
    terms = term_index(*current_term(day)) - term_index(student["matriculation_term"], student["matriculation_year"])
    return "—" if terms < 0 else _format_terms(terms)


def connect(path=DB_PATH):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    legacy = _stash_old_funding(conn)
    _stash_old_milestones(conn)
    conn.executescript(SCHEMA.read_text())
    if legacy:
        _restore_old_funding(conn, legacy)
    _finish_interrupted_rebuilds(conn)
    _migrate_users(conn)
    _add_faculty_accounts(conn)
    _drop_program_column(conn)
    _add_funding_notes(conn)
    _add_student_citizenship(conn)
    _migrate_matriculation(conn)
    _add_student_cnet(conn)
    return conn


def _table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def _stash_old_milestones(conn):
    """The milestone types are a CHECK rule SQLite can't change in place, so a table that predates a newly
    allowed type (the latest being 'ms_degree') is set aside, recreated from schema.sql, and its rows copied back."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'milestones'").fetchone()
    if row is None or "'ms_degree'" in row[0]:
        return False
    conn.executescript("DROP INDEX IF EXISTS idx_milestones_student;"
                       "ALTER TABLE milestones RENAME TO milestones_old;")
    return True


def _restore_old_milestones(conn):
    old = {r[1] for r in conn.execute("PRAGMA table_info(milestones_old)")}
    cols = ", ".join(r[1] for r in conn.execute("PRAGMA table_info(milestones)") if r[1] in old)
    conn.executescript(f"INSERT OR IGNORE INTO milestones ({cols}) SELECT {cols} FROM milestones_old;"
                       "DROP TABLE milestones_old;")
    conn.commit()


def _finish_interrupted_rebuilds(conn):
    """If the program was stopped in the middle of a table rebuild, the old rows are still in <table>_old:
    copy them back now (rows already restored are skipped) so nothing is ever lost."""
    if _table_exists(conn, "milestones_old"):
        _restore_old_milestones(conn)
    if _table_exists(conn, "funding_old"):
        _restore_old_funding(conn, "text_term" if "term" in {r[1] for r in conn.execute("PRAGMA table_info(funding_old)")}
                             else "check")


def _migrate_matriculation(conn):
    """'Started' became 'Matriculation' (year + term). Existing students are given Autumn."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(students)")}
    if "start_year" in cols:
        conn.execute("ALTER TABLE students RENAME COLUMN start_year TO matriculation_year")
    if "matriculation_term" not in cols:
        conn.execute("ALTER TABLE students ADD COLUMN matriculation_term TEXT NOT NULL DEFAULT 'Autumn'")
    conn.commit()


def matriculation_label(row):
    """e.g. 'Autumn 2022'."""
    return f"{row['matriculation_term']} {row['matriculation_year']}"


CNET_PATTERN = r"[A-Za-z0-9._-]{2,40}"


def _add_student_cnet(conn):
    """CNET column plus a unique index (blank values may repeat). Done here rather than in schema.sql
    because the index can only be created once the column exists in older databases."""
    if "cnet" not in {r[1] for r in conn.execute("PRAGMA table_info(students)")}:
        conn.execute("ALTER TABLE students ADD COLUMN cnet TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_students_cnet ON students(cnet) WHERE cnet IS NOT NULL")
    conn.commit()


def _add_student_citizenship(conn):
    if "citizenship" not in {r[1] for r in conn.execute("PRAGMA table_info(students)")}:
        conn.execute("ALTER TABLE students ADD COLUMN citizenship TEXT")
        conn.commit()


def _add_funding_notes(conn):
    if "notes" not in {r[1] for r in conn.execute("PRAGMA table_info(funding)")}:
        conn.execute("ALTER TABLE funding ADD COLUMN notes TEXT")
        conn.commit()


def _drop_program_column(conn):
    """The department has no master's students, so the old PhD/MS `program` column is gone."""
    if "program" in {r[1] for r in conn.execute("PRAGMA table_info(students)")}:
        try:
            conn.execute("ALTER TABLE students DROP COLUMN program")
            conn.commit()
        except sqlite3.OperationalError:
            pass   # very old SQLite: leave the unused column (it has a default, so inserts still work)


def parse_term(text):
    """Best-effort parse of old free-text terms like 'Fall 2026' or '2026 winter'."""
    m = (re.search(r"(?i)\b(autumn|fall|winter|spring|summer)\b\D*?\b(\d{4})\b", text or "")
         or re.search(r"(?i)\b(\d{4})\b\D*?\b(autumn|fall|winter|spring|summer)\b", text or ""))
    if not m:
        return None, None
    a, b = m.groups()
    season, year = (a, b) if a.isalpha() else (b, a)
    season = "Autumn" if season.lower() == "fall" else season.capitalize()
    return int(year), season


def term_label(row):
    """Display text for a funding row's term."""
    if row["term_year"] is not None:
        return f"{row['term_season']} {row['term_year']}"
    return row["legacy_term"] or ""


def _stash_old_funding(conn):
    """Set the funding table aside if it must be rebuilt; returns why ('text_term', 'check') or None.

    text_term: it still has the old free-text `term` column.
    check:     its CHECK rule predates a newly allowed funding type (SQLite can't alter a CHECK
               in place), so it is recreated from schema.sql and the rows copied across.
    """
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'funding'").fetchone()
    if row is None:
        return None
    cols = {r[1] for r in conn.execute("PRAGMA table_info(funding)")}
    if "term_year" not in cols:
        why = "text_term"
    elif "'supplement'" not in row[0]:
        why = "check"
    else:
        return None
    conn.executescript("DROP INDEX IF EXISTS idx_funding_student;"
                       "ALTER TABLE funding RENAME TO funding_old;")
    return why


def _restore_old_funding(conn, why="text_term"):
    if why == "check":
        old = {r[1] for r in conn.execute("PRAGMA table_info(funding_old)")}
        new = {r[1] for r in conn.execute("PRAGMA table_info(funding)")}
        cols = [r[1] for r in conn.execute("PRAGMA table_info(funding)") if r[1] in old and r[1] in new]
        # the 'Stipend Supplement' type was renamed 'Supplement': convert saved entries while copying
        picks = ", ".join("CASE kind WHEN 'stipend_supplement' THEN 'supplement' ELSE kind END"
                          if c == "kind" else c for c in cols)
        conn.executescript(f"INSERT OR IGNORE INTO funding ({', '.join(cols)}) SELECT {picks} FROM funding_old;"
                           "DROP TABLE funding_old;")
        conn.commit()
        return
    for r in conn.execute("SELECT * FROM funding_old").fetchall():
        year, season = parse_term(r["term"])
        conn.execute(
            "INSERT OR IGNORE INTO funding (id, student_id, kind, source, term_year, term_season, legacy_term, amount) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (r["id"], r["student_id"], r["kind"], r["source"], year, season,
             None if year else r["term"], r["amount"]))
    conn.executescript("DROP TABLE funding_old;")
    conn.commit()


def _migrate_users(conn):
    """Upgrade a users table from before the 'admin' role, must_change, or the faculty role/faculty_id existed."""
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'users'").fetchone()[0]
    if "'faculty'" in ddl and "faculty_id" in ddl and "must_change" in ddl:
        return
    has_mc = "must_change" in ddl
    conn.executescript(f"""
        ALTER TABLE users RENAME TO users_old;
        CREATE TABLE users (
            id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'viewer' CHECK (role IN ('viewer', 'editor', 'admin', 'faculty')),
            must_change INTEGER NOT NULL DEFAULT 0,
            faculty_id INTEGER REFERENCES faculty(id) ON DELETE CASCADE,
            CHECK ((role = 'faculty') = (faculty_id IS NOT NULL)));
        INSERT INTO users (id, username, password_hash, role, must_change)
            SELECT id, username, password_hash, role, {'must_change' if has_mc else '0'} FROM users_old;
        DROP TABLE users_old;""")


def _add_faculty_accounts(conn):
    """Faculty CNET + the rules that tie a faculty login to it. Run after the users migration.

    Rules, enforced in the database so no code path can break them:
      * a CNET is unique among faculty (blank ones may repeat);
      * a faculty member has at most one login;
      * a faculty login's username IS the faculty member's CNET;
      * changing the CNET renames the login; removing it is refused while a login exists.
    """
    if "cnet" not in {r[1] for r in conn.execute("PRAGMA table_info(faculty)")}:
        conn.execute("ALTER TABLE faculty ADD COLUMN cnet TEXT")
    conn.executescript("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_faculty_cnet ON faculty(cnet) WHERE cnet IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_users_faculty ON users(faculty_id) WHERE faculty_id IS NOT NULL;
        CREATE TRIGGER IF NOT EXISTS users_faculty_login_is_cnet BEFORE INSERT ON users
          WHEN NEW.role = 'faculty'
               AND NEW.username IS NOT (SELECT cnet FROM faculty WHERE id = NEW.faculty_id)
        BEGIN SELECT RAISE(ABORT, 'A faculty login must use the faculty member''s CNET as its username.'); END;
        CREATE TRIGGER IF NOT EXISTS faculty_cnet_needed_for_login BEFORE UPDATE OF cnet ON faculty
          WHEN NEW.cnet IS NULL AND EXISTS (SELECT 1 FROM users WHERE faculty_id = OLD.id)
        BEGIN SELECT RAISE(ABORT, 'This person has a login account that uses their CNET.'); END;
        CREATE TRIGGER IF NOT EXISTS faculty_cnet_renames_login AFTER UPDATE OF cnet ON faculty
          WHEN NEW.cnet IS NOT NULL AND NEW.cnet IS NOT OLD.cnet
        BEGIN UPDATE users SET username = NEW.cnet WHERE faculty_id = NEW.id; END;""")
    conn.commit()


def _find(conn, table, key):
    """Resolve a student/faculty by numeric id, university id, email, or CNET (CNETs match in any case)."""
    cols = ["email", "cnet"] + (["university_id"] if table == "students" else [])
    clause = " OR ".join(f"{c} = ?" for c in cols)
    params = [str(key).strip().lower() if c == "cnet" else key for c in cols]
    if str(key).isdigit():
        clause, params = "id = ? OR " + clause, [int(key)] + params
    row = conn.execute(f"SELECT * FROM {table} WHERE {clause}", params).fetchone()
    if row is None:
        noun = "student" if table == "students" else "faculty member"
        raise SystemExit(f"No {noun} found for '{key}'")
    return row


def add_student(conn, **f):
    f.setdefault("citizenship", None)
    f["cnet"] = (f.get("cnet") or "").strip().lower() or None
    if f["cnet"] and not re.fullmatch(CNET_PATTERN, f["cnet"]):
        raise ValueError("CNET must be 2-40 letters, digits, or . _ -")
    f.setdefault("matriculation_term", "Autumn")
    if f["matriculation_term"] not in SEASONS:
        raise ValueError(f"matriculation term must be one of {', '.join(SEASONS)}")
    if f["citizenship"] is not None and f["citizenship"] not in CITIZENSHIP:
        raise ValueError(f"citizenship must be one of {', '.join(CITIZENSHIP)}")
    cur = conn.execute(
        """INSERT INTO students (university_id, cnet, first_name, last_name, email, citizenship,
               matriculation_year, matriculation_term, expected_grad_year, research_area, notes)
           VALUES (:university_id, :cnet, :first_name, :last_name, :email, :citizenship,
               :matriculation_year, :matriculation_term, :expected_grad_year, :research_area, :notes)""", f)
    conn.commit()
    return cur.lastrowid


def add_faculty(conn, first_name, last_name, email, research_area=None, cnet=None):
    cnet = (cnet or "").strip().lower() or None
    if cnet and not re.fullmatch(CNET_PATTERN, cnet):
        raise ValueError("CNET must be 2-40 letters, digits, or . _ -")
    cur = conn.execute(
        "INSERT INTO faculty (first_name, last_name, email, cnet, research_area) VALUES (?,?,?,?,?)",
        (first_name, last_name, email, cnet, research_area))
    conn.commit()
    return cur.lastrowid


def assign_advisor(conn, student, faculty, role="advisor", start_date=None):
    s, f = _find(conn, "students", student), _find(conn, "faculty", faculty)
    conn.execute(
        "INSERT INTO advising (student_id, faculty_id, role, start_date) VALUES (?,?,?,?)",
        (s["id"], f["id"], role, start_date or datetime.date.today().isoformat()))
    conn.commit()


def add_milestone(conn, student, kind, date, outcome="pending", notes=None):
    if kind not in MILESTONE_KINDS:
        retired = " (that type has been retired)" if kind in RETIRED_MILESTONE_KINDS else ""
        raise ValueError(f"unknown milestone type '{kind}'{retired}; choose one of: {', '.join(MILESTONE_KINDS)}")
    s = _find(conn, "students", student)
    conn.execute(
        "INSERT INTO milestones (student_id, kind, date, outcome, notes) VALUES (?,?,?,?,?)",
        (s["id"], kind, date, outcome, notes))
    conn.commit()


def add_funding(conn, student, kind, year, season, source=None, amount=None, poetaf_coa=None,
                notes=None):
    s = _find(conn, "students", student)
    conn.execute(
        "INSERT INTO funding (student_id, kind, source, poetaf_coa, term_year, term_season, amount, notes) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (s["id"], kind, source, poetaf_coa, year, season, amount, notes))
    conn.commit()


def set_status(conn, student, status):
    s = _find(conn, "students", student)
    conn.execute("UPDATE students SET status = ? WHERE id = ?", (status, s["id"]))
    conn.commit()


def faculty_role_clause(role, faculty_id=None):
    """SQL condition (on a students row aliased `s`) for a current advising relationship.

    role 'advisor':   advisor or co-advisor.
    role 'committee': committee member or chair, but NOT students the same person also advises.
    role 'any':       any current role.
    faculty_id restricts to one faculty member; None means any faculty member.
    """
    conds, params = ["a.student_id = s.id", "a.end_date IS NULL"], []
    if faculty_id is not None:
        conds.append("a.faculty_id = ?"); params.append(faculty_id)
    if role == "advisor":
        conds.append("a.role IN ('advisor', 'co-advisor')")
    elif role == "committee":
        conds.append("a.role IN ('committee', 'committee_chair')")
        conds.append("NOT EXISTS (SELECT 1 FROM advising b WHERE b.student_id = a.student_id "
                     "AND b.faculty_id = a.faculty_id AND b.end_date IS NULL "
                     "AND b.role IN ('advisor', 'co-advisor'))")
    return f"EXISTS (SELECT 1 FROM advising a WHERE {' AND '.join(conds)})", params


def list_students(conn, status=None, advisor=None, year=None, committee=None):
    """advisor: students this faculty member advises. committee: students for whom they are
    only on the committee (not also the advisor). Both may be combined."""
    sql, params = "SELECT s.* FROM students s WHERE 1=1", []
    if status:
        sql += " AND s.status = ?"; params.append(status)
    if year:
        sql += " AND s.matriculation_year = ?"; params.append(year)
    for who, role in ((advisor, "advisor"), (committee, "committee")):
        if who:
            clause, p = faculty_role_clause(role, _find(conn, "faculty", who)["id"])
            sql += f" AND {clause}"; params += p
    return conn.execute(sql + " ORDER BY s.last_name, s.first_name", params).fetchall()


# What a faculty member may see about a student. Deliberately NOT included: University ID and the internal
# admin notes. Every faculty-facing query below selects exactly these columns, so nothing else can leak.
FACULTY_STUDENT_COLUMNS = ("id", "first_name", "last_name", "cnet", "email", "citizenship", "status",
                           "matriculation_year", "matriculation_term", "expected_grad_year", "research_area")
_FSC = ", ".join(f"s.{c}" for c in FACULTY_STUDENT_COLUMNS)
_ADVISOR_ROLES = "('advisor', 'co-advisor')"


def faculty_advisees(conn, faculty_id):
    """Students this faculty member currently advises (advisor or co-advisor), limited to the visible columns."""
    return [dict(r) for r in conn.execute(
        f"""SELECT DISTINCT {_FSC} FROM students s JOIN advising a ON a.student_id = s.id
            WHERE a.faculty_id = ? AND a.end_date IS NULL AND a.role IN {_ADVISOR_ROLES}
            ORDER BY s.last_name, s.first_name, s.id""", (faculty_id,))]


def is_advisee(conn, faculty_id, student_id):
    return conn.execute(
        f"""SELECT 1 FROM advising WHERE faculty_id = ? AND student_id = ? AND end_date IS NULL
            AND role IN {_ADVISOR_ROLES}""", (faculty_id, student_id)).fetchone() is not None


def faculty_committee_students(conn, faculty_id):
    """Students on whose committee this faculty member sits (member or chair) but whom they do not advise."""
    return [dict(r) for r in conn.execute(
        f"""SELECT {_FSC},
                   CASE WHEN SUM(a.role = 'committee_chair') > 0 THEN 'committee_chair' ELSE 'committee' END AS committee_role,
                   (SELECT group_concat(fa.first_name || ' ' || fa.last_name, '; ') FROM advising x
                      JOIN faculty fa ON fa.id = x.faculty_id
                     WHERE x.student_id = s.id AND x.end_date IS NULL AND x.role IN {_ADVISOR_ROLES}) AS advisors
            FROM students s JOIN advising a ON a.student_id = s.id
            WHERE a.faculty_id = ? AND a.end_date IS NULL AND a.role IN ('committee', 'committee_chair')
              AND NOT EXISTS (SELECT 1 FROM advising b WHERE b.student_id = a.student_id AND b.faculty_id = a.faculty_id
                              AND b.end_date IS NULL AND b.role IN {_ADVISOR_ROLES})
            GROUP BY s.id ORDER BY s.last_name, s.first_name, s.id""", (faculty_id,))]


def funding_for_term(conn, student_ids, year, season):
    """{student_id: [funding entries]} for the given students in one term."""
    out = {sid: [] for sid in student_ids}
    if not student_ids:
        return out
    marks = ",".join("?" * len(student_ids))
    for r in conn.execute(f"SELECT * FROM funding WHERE term_year = ? AND term_season = ? AND student_id IN ({marks}) "
                          "ORDER BY id", (year, season, *student_ids)):
        out[r["student_id"]].append(dict(r))
    return out


def funding_report(conn, year, season):
    """One row per funding entry for the term, for every ACTIVE student.

    Active students with nothing recorded for that term still appear once, with empty funding fields.
    A student with several entries (say an RA plus a supplement) gets one row per entry.
    """
    return [dict(r) for r in conn.execute(
        """SELECT s.id AS student_id, s.first_name, s.last_name, s.university_id, s.cnet, s.email,
                  s.citizenship, s.status, s.matriculation_year, s.matriculation_term,
                  s.expected_grad_year, s.research_area, s.notes AS student_notes,
                  (SELECT group_concat(fa.last_name, ', ') FROM advising a
                     JOIN faculty fa ON fa.id = a.faculty_id
                    WHERE a.student_id = s.id AND a.end_date IS NULL
                      AND a.role IN ('advisor', 'co-advisor')) AS advisors,
                  (SELECT group_concat(fa.first_name || ' ' || fa.last_name, '; ') FROM advising a
                     JOIN faculty fa ON fa.id = a.faculty_id
                    WHERE a.student_id = s.id AND a.end_date IS NULL
                      AND a.role IN ('advisor', 'co-advisor')) AS advisors_full,
                  f.id AS funding_id, f.kind, f.source, f.poetaf_coa, f.amount, f.notes
           FROM students s
           LEFT JOIN funding f ON f.student_id = s.id AND f.term_year = ? AND f.term_season = ?
           WHERE s.status = 'active'
           ORDER BY s.last_name, s.first_name, s.id, f.id""", (year, season))]


def undated_funding_count(conn):
    """Funding entries of active students that have no term yet (left over from free-text terms)."""
    return conn.execute(
        """SELECT count(*) FROM funding f JOIN students s ON s.id = f.student_id
           WHERE f.term_year IS NULL AND s.status = 'active'""").fetchone()[0]


def missing_milestone(conn, kind, min_years=2, now=None):
    """Active students whose clock is at least `min_years` (fractions such as 0.25 are fine) and who have no
    passed or conditional milestone of this kind. Each row includes `advisors` (current advisors' last names).
    The clock is the same term count shown on the Students tab, so this filter and that column always agree."""
    now = now or current_term()
    rows = conn.execute(
        """SELECT s.*,
                  (SELECT group_concat(fa.last_name, ', ') FROM advising a JOIN faculty fa ON fa.id = a.faculty_id
                    WHERE a.student_id = s.id AND a.end_date IS NULL
                      AND a.role IN ('advisor', 'co-advisor')) AS advisors
           FROM students s WHERE s.status = 'active'
           AND NOT EXISTS (SELECT 1 FROM milestones m WHERE m.student_id = s.id
                           AND m.kind = ? AND m.outcome IN ('passed','conditional'))
           ORDER BY s.matriculation_year,
                    CASE s.matriculation_term WHEN 'Winter' THEN 1 WHEN 'Spring' THEN 2 WHEN 'Summer' THEN 3 ELSE 4 END,
                    s.last_name""", (kind,)).fetchall()
    return [r for r in rows
            if (terms := clock_terms(r, now)) is not None and terms / len(TERM_STARTS) >= min_years]


def show_student(conn, key):
    s = _find(conn, "students", key)
    out = [f"{s['first_name']} {s['last_name']}  <{s['email']}>",
           f"  ID {s['university_id']} | CNET {s['cnet'] or '-'} | {s['status']} | "
           f"matriculated {matriculation_label(s)}, expected {s['expected_grad_year'] or '?'}",
           f"  Citizenship: {s['citizenship'].replace('_', ' ') if s['citizenship'] else '-'}",
           f"  Research: {s['research_area'] or '-'}"]
    if s["notes"]:
        out.append(f"  Notes: {s['notes']}")
    out.append("  Advisors:")
    for r in conn.execute(
            f"""SELECT f.first_name, f.last_name, a.role, a.start_date, a.end_date
               FROM advising a JOIN faculty f ON f.id = a.faculty_id
               WHERE a.student_id = ? ORDER BY {ADVISING_ORDER_SQL}""", (s["id"],)):
        end = r["end_date"] or "present"
        out.append(f"    {r['role']}: {r['first_name']} {r['last_name']} ({r['start_date']} – {end})")
    out.append("  Milestones:")
    for r in conn.execute("SELECT * FROM milestones WHERE student_id = ? ORDER BY date", (s["id"],)):
        out.append(f"    {r['date']}  {r['kind']}: {r['outcome']}")
    out.append("  Funding:")
    for r in conn.execute(f"SELECT * FROM funding WHERE student_id = ? ORDER BY {FUNDING_ORDER_SQL}",
                          (s["id"],)):
        amt = f" ${r['amount']:,.0f}" if r["amount"] is not None else ""
        acct = f" [POETAF/COA {r['poetaf_coa']}]" if r["poetaf_coa"] else ""
        out.append(f"    {term_label(r)}: {r['kind']} ({r['source'] or '-'}){acct}{amt}")
        if r["notes"]:
            out.append(f"        Notes: {r['notes']}")
    return "\n".join(out)


def _csv_safe(v):
    """Neutralize spreadsheet formula injection (cells starting with = + - @)."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def export_csv(conn, fh):
    rows = list_students(conn)
    w = csv.writer(fh)
    w.writerow(rows[0].keys() if rows else ["id"])
    for r in rows:
        w.writerow(tuple(_csv_safe(v) for v in r))


def _row_line(r):
    return (f"{r['id']:>4}  {r['last_name']}, {r['first_name']:<14} {r['university_id']:<10} "
            f"{r['status']:<9} {matriculation_label(r)}  {r['research_area'] or ''}")


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add-student")
    a.add_argument("university_id"); a.add_argument("first_name"); a.add_argument("last_name")
    a.add_argument("email"); a.add_argument("--cnet")
    a.add_argument("--citizenship", choices=CITIZENSHIP)
    a.add_argument("--matriculation-year", type=int, default=datetime.date.today().year)
    a.add_argument("--matriculation-term", choices=SEASONS, type=str.capitalize, default="Autumn")
    a.add_argument("--expected-grad-year", type=int)
    a.add_argument("--research-area"); a.add_argument("--notes")

    a = sub.add_parser("add-faculty")
    a.add_argument("first_name"); a.add_argument("last_name"); a.add_argument("email")
    a.add_argument("--research-area"); a.add_argument("--cnet")

    a = sub.add_parser("assign-advisor")
    a.add_argument("student"); a.add_argument("faculty")
    a.add_argument("--role", default="advisor",
                   choices=["advisor", "co-advisor", "committee", "committee_chair"])
    a.add_argument("--start-date")

    a = sub.add_parser("add-milestone")
    a.add_argument("student")
    a.add_argument("kind", choices=MILESTONE_KINDS)
    a.add_argument("date", help="YYYY-MM-DD")
    a.add_argument("--outcome", default="pending",
                   choices=["pending", "passed", "conditional", "failed"])
    a.add_argument("--notes")

    a = sub.add_parser("add-funding")
    a.add_argument("student"); a.add_argument("kind", choices=["TA", "RA", "fellowship", "supplement", "other"])
    a.add_argument("year", type=int); a.add_argument("season", type=str.capitalize, choices=SEASONS)
    a.add_argument("--source"); a.add_argument("--amount", type=float)
    a.add_argument("--poetaf-coa", help="POETAF/COA account number"); a.add_argument("--notes")

    a = sub.add_parser("set-status")
    a.add_argument("student"); a.add_argument("status",
                   choices=["active", "on_leave", "graduated", "withdrawn"])

    a = sub.add_parser("list")
    a.add_argument("--status"); a.add_argument("--year", type=int)
    a.add_argument("--advisor", help="students this faculty member advises (advisor or co-advisor)")
    a.add_argument("--committee", help="students for whom this faculty member is only on the committee")

    a = sub.add_parser("show"); a.add_argument("student")

    a = sub.add_parser("missing", help="active students lacking a passed milestone")
    a.add_argument("kind"); a.add_argument("--min-years", type=float, default=2,
                   help="minimum clock in years, e.g. 1.5 (default 2)")

    sub.add_parser("export", help="write all students as CSV to stdout")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    conn = connect()
    c = args.cmd
    try:
        if c == "add-student":
            d = vars(args); d.pop("cmd")
            print("Added student", add_student(conn, **d))
        elif c == "add-faculty":
            print("Added faculty", add_faculty(conn, args.first_name, args.last_name,
                                               args.email, args.research_area, args.cnet))
        elif c == "assign-advisor":
            assign_advisor(conn, args.student, args.faculty, args.role, args.start_date)
        elif c == "add-milestone":
            add_milestone(conn, args.student, args.kind, args.date, args.outcome, args.notes)
        elif c == "add-funding":
            add_funding(conn, args.student, args.kind, args.year, args.season, args.source, args.amount,
                        args.poetaf_coa, args.notes)
        elif c == "set-status":
            set_status(conn, args.student, args.status)
        elif c == "list":
            for r in list_students(conn, args.status, args.advisor, args.year, args.committee):
                print(_row_line(r))
        elif c == "show":
            print(show_student(conn, args.student))
        elif c == "missing":
            for r in missing_milestone(conn, args.kind, args.min_years):
                print(_row_line(r))
        elif c == "export":
            export_csv(conn, sys.stdout)
    except sqlite3.IntegrityError as e:
        raise SystemExit(f"Error: {e}")


if __name__ == "__main__":
    main()
