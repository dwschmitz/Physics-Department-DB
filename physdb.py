#!/usr/bin/env python3
"""Graduate student tracking for the physics department (SQLite + CLI)."""
import argparse
import csv
import datetime
import os
import sqlite3
import sys
from pathlib import Path

DB_PATH = os.environ.get("PHYSDB_PATH", "physics_grads.db")
SCHEMA = Path(__file__).with_name("schema.sql")


def connect(path=DB_PATH):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA.read_text())
    return conn


def _find(conn, table, key):
    """Resolve a student/faculty by numeric id, university id, or email."""
    cols = ["email"] + (["university_id"] if table == "students" else [])
    clause = " OR ".join(f"{c} = ?" for c in cols)
    if str(key).isdigit():
        clause = "id = ? OR " + clause
        params = [int(key)] + [key] * len(cols)
    else:
        params = [key] * len(cols)
    row = conn.execute(f"SELECT * FROM {table} WHERE {clause}", params).fetchone()
    if row is None:
        raise SystemExit(f"No {table[:-1]} found for '{key}'")
    return row


def add_student(conn, **f):
    cur = conn.execute(
        """INSERT INTO students (university_id, first_name, last_name, email, program,
               start_year, expected_grad_year, research_area, notes)
           VALUES (:university_id, :first_name, :last_name, :email, :program,
               :start_year, :expected_grad_year, :research_area, :notes)""", f)
    conn.commit()
    return cur.lastrowid


def add_faculty(conn, first_name, last_name, email, research_area=None):
    cur = conn.execute(
        "INSERT INTO faculty (first_name, last_name, email, research_area) VALUES (?,?,?,?)",
        (first_name, last_name, email, research_area))
    conn.commit()
    return cur.lastrowid


def assign_advisor(conn, student, faculty, role="advisor", start_date=None):
    s, f = _find(conn, "students", student), _find(conn, "faculty", faculty)
    conn.execute(
        "INSERT INTO advising (student_id, faculty_id, role, start_date) VALUES (?,?,?,?)",
        (s["id"], f["id"], role, start_date or datetime.date.today().isoformat()))
    conn.commit()


def add_milestone(conn, student, kind, date, outcome="pending", notes=None):
    s = _find(conn, "students", student)
    conn.execute(
        "INSERT INTO milestones (student_id, kind, date, outcome, notes) VALUES (?,?,?,?,?)",
        (s["id"], kind, date, outcome, notes))
    conn.commit()


def add_funding(conn, student, kind, term, source=None, amount=None):
    s = _find(conn, "students", student)
    conn.execute(
        "INSERT INTO funding (student_id, kind, source, term, amount) VALUES (?,?,?,?,?)",
        (s["id"], kind, source, term, amount))
    conn.commit()


def set_status(conn, student, status):
    s = _find(conn, "students", student)
    conn.execute("UPDATE students SET status = ? WHERE id = ?", (status, s["id"]))
    conn.commit()


def list_students(conn, status=None, advisor=None, year=None):
    sql = """SELECT DISTINCT s.* FROM students s
             LEFT JOIN advising a ON a.student_id = s.id AND a.end_date IS NULL
             WHERE 1=1"""
    params = []
    if status:
        sql += " AND s.status = ?"; params.append(status)
    if year:
        sql += " AND s.start_year = ?"; params.append(year)
    if advisor:
        f = _find(conn, "faculty", advisor)
        sql += " AND a.faculty_id = ?"; params.append(f["id"])
    return conn.execute(sql + " ORDER BY s.last_name, s.first_name", params).fetchall()


def missing_milestone(conn, kind, min_years=2):
    """Active students who started >= min_years ago and have no passed `kind`."""
    cutoff = datetime.date.today().year - min_years
    return conn.execute(
        """SELECT * FROM students s WHERE s.status = 'active' AND s.start_year <= ?
           AND NOT EXISTS (SELECT 1 FROM milestones m WHERE m.student_id = s.id
                           AND m.kind = ? AND m.outcome IN ('passed','conditional'))
           ORDER BY s.start_year, s.last_name""", (cutoff, kind)).fetchall()


def show_student(conn, key):
    s = _find(conn, "students", key)
    out = [f"{s['first_name']} {s['last_name']}  <{s['email']}>",
           f"  ID {s['university_id']} | {s['program']} | {s['status']} | "
           f"started {s['start_year']}, expected {s['expected_grad_year'] or '?'}",
           f"  Research: {s['research_area'] or '-'}"]
    if s["notes"]:
        out.append(f"  Notes: {s['notes']}")
    out.append("  Advisors:")
    for r in conn.execute(
            """SELECT f.first_name, f.last_name, a.role, a.start_date, a.end_date
               FROM advising a JOIN faculty f ON f.id = a.faculty_id
               WHERE a.student_id = ? ORDER BY a.start_date""", (s["id"],)):
        end = r["end_date"] or "present"
        out.append(f"    {r['role']}: {r['first_name']} {r['last_name']} ({r['start_date']} – {end})")
    out.append("  Milestones:")
    for r in conn.execute("SELECT * FROM milestones WHERE student_id = ? ORDER BY date", (s["id"],)):
        out.append(f"    {r['date']}  {r['kind']}: {r['outcome']}")
    out.append("  Funding:")
    for r in conn.execute("SELECT * FROM funding WHERE student_id = ?", (s["id"],)):
        amt = f" ${r['amount']:,.0f}" if r["amount"] is not None else ""
        out.append(f"    {r['term']}: {r['kind']} ({r['source'] or '-'}){amt}")
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
            f"{r['program']:<4} {r['status']:<9} {r['start_year']}  {r['research_area'] or ''}")


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add-student")
    a.add_argument("university_id"); a.add_argument("first_name"); a.add_argument("last_name")
    a.add_argument("email"); a.add_argument("--program", default="PhD", choices=["PhD", "MS"])
    a.add_argument("--start-year", type=int, default=datetime.date.today().year)
    a.add_argument("--expected-grad-year", type=int)
    a.add_argument("--research-area"); a.add_argument("--notes")

    a = sub.add_parser("add-faculty")
    a.add_argument("first_name"); a.add_argument("last_name"); a.add_argument("email")
    a.add_argument("--research-area")

    a = sub.add_parser("assign-advisor")
    a.add_argument("student"); a.add_argument("faculty")
    a.add_argument("--role", default="advisor",
                   choices=["advisor", "co-advisor", "committee", "committee_chair"])
    a.add_argument("--start-date")

    a = sub.add_parser("add-milestone")
    a.add_argument("student")
    a.add_argument("kind", choices=["qualifying_exam", "candidacy", "thesis_proposal",
                                    "thesis_defense", "other"])
    a.add_argument("date", help="YYYY-MM-DD")
    a.add_argument("--outcome", default="pending",
                   choices=["pending", "passed", "conditional", "failed"])
    a.add_argument("--notes")

    a = sub.add_parser("add-funding")
    a.add_argument("student"); a.add_argument("kind", choices=["TA", "RA", "fellowship", "other"])
    a.add_argument("term"); a.add_argument("--source"); a.add_argument("--amount", type=float)

    a = sub.add_parser("set-status")
    a.add_argument("student"); a.add_argument("status",
                   choices=["active", "on_leave", "graduated", "withdrawn"])

    a = sub.add_parser("list")
    a.add_argument("--status"); a.add_argument("--advisor"); a.add_argument("--year", type=int)

    a = sub.add_parser("show"); a.add_argument("student")

    a = sub.add_parser("missing", help="active students lacking a passed milestone")
    a.add_argument("kind"); a.add_argument("--min-years", type=int, default=2)

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
                                               args.email, args.research_area))
        elif c == "assign-advisor":
            assign_advisor(conn, args.student, args.faculty, args.role, args.start_date)
        elif c == "add-milestone":
            add_milestone(conn, args.student, args.kind, args.date, args.outcome, args.notes)
        elif c == "add-funding":
            add_funding(conn, args.student, args.kind, args.term, args.source, args.amount)
        elif c == "set-status":
            set_status(conn, args.student, args.status)
        elif c == "list":
            for r in list_students(conn, args.status, args.advisor, args.year):
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
