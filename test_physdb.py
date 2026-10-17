import datetime
import io
import sqlite3
import unittest

import physdb


class PhysDBTests(unittest.TestCase):
    def setUp(self):
        self.conn = physdb.connect(":memory:")
        self.sid = physdb.add_student(
            self.conn, university_id="U1001", first_name="Ada", last_name="Lovelace",
            email="ada@uni.edu", matriculation_year=2020,
            expected_grad_year=2026, research_area="Quantum optics", notes=None)
        self.fid = physdb.add_faculty(self.conn, "Niels", "Bohr", "bohr@uni.edu", "Atomic")

    def test_lookup_by_id_email_university_id(self):
        for key in (self.sid, "ada@uni.edu", "U1001"):
            self.assertEqual(physdb._find(self.conn, "students", key)["id"], self.sid)

    def test_duplicate_email_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            physdb.add_student(
                self.conn, university_id="U2", first_name="X", last_name="Y",
                email="ada@uni.edu", matriculation_year=2021,
                expected_grad_year=None, research_area=None, notes=None)

    def test_advisor_filter_and_show(self):
        physdb.assign_advisor(self.conn, "U1001", "bohr@uni.edu", start_date="2021-01-01")
        self.assertEqual(len(physdb.list_students(self.conn, advisor="bohr@uni.edu")), 1)
        self.assertIn("Niels Bohr", physdb.show_student(self.conn, "U1001"))

    def test_advisor_and_committee_filters_are_distinct(self):
        def add(uid):
            return physdb.add_student(self.conn, university_id=uid, first_name=uid, last_name=uid,
                                      email=f"{uid}@u.edu", matriculation_year=2022,
                                      expected_grad_year=None, research_area=None, notes=None)
        physdb.add_faculty(self.conn, "Marie", "Curie", "curie@uni.edu")
        for uid in ("A", "B", "C", "D", "E"):
            add(uid)
        a = physdb.assign_advisor
        a(self.conn, "A", "bohr@uni.edu", "advisor"); a(self.conn, "B", "bohr@uni.edu", "committee")
        a(self.conn, "C", "curie@uni.edu", "advisor"); a(self.conn, "C", "bohr@uni.edu", "committee_chair")
        a(self.conn, "D", "bohr@uni.edu", "advisor"); a(self.conn, "D", "bohr@uni.edu", "committee")
        a(self.conn, "E", "bohr@uni.edu", "co-advisor")
        names = lambda rows: sorted(r["university_id"] for r in rows)
        self.assertEqual(names(physdb.list_students(self.conn, advisor="bohr@uni.edu")), ["A", "D", "E"])
        self.assertEqual(names(physdb.list_students(self.conn, committee="bohr@uni.edu")), ["B", "C"])
        self.assertEqual(names(physdb.list_students(self.conn, advisor="curie@uni.edu")), ["C"])
        self.assertEqual(names(physdb.list_students(self.conn, advisor="curie@uni.edu",
                                                    committee="bohr@uni.edu")), ["C"])
        self.assertEqual(physdb.list_students(self.conn, advisor="curie@uni.edu", committee="curie@uni.edu"), [])

    def test_missing_milestone(self):
        self.assertEqual(len(physdb.missing_milestone(self.conn, "qualifying_exam")), 1)
        physdb.add_milestone(self.conn, "U1001", "qualifying_exam", "2022-05-01", "passed")
        self.assertEqual(physdb.missing_milestone(self.conn, "qualifying_exam"), [])

    def test_status_and_check_constraints(self):
        physdb.set_status(self.conn, "U1001", "graduated")
        self.assertEqual(physdb.list_students(self.conn, status="active"), [])
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE students SET status='bogus'")

    def test_matriculation_default_and_migration(self):
        self.assertEqual(physdb.matriculation_label(physdb.list_students(self.conn)[0]), "Autumn 2020")
        self.assertIn("matriculated Autumn 2020", physdb.show_student(self.conn, "U1001"))
        with self.assertRaises(ValueError):
            physdb.add_student(self.conn, university_id="U11", first_name="N", last_name="M",
                               email="y@m.o", matriculation_year=2023, matriculation_term="Fall",
                               expected_grad_year=None, research_area=None, notes=None)

    def test_current_term_boundaries(self):
        d = datetime.date
        for day, want in [(d(2026, 1, 1), "Winter"), (d(2026, 3, 24), "Winter"), (d(2026, 3, 25), "Spring"),
                          (d(2026, 6, 14), "Spring"), (d(2026, 6, 15), "Summer"), (d(2026, 9, 24), "Summer"),
                          (d(2026, 9, 25), "Autumn"), (d(2026, 10, 17), "Autumn"), (d(2026, 12, 31), "Autumn")]:
            self.assertEqual(physdb.current_term(day), (want, 2026), day)
        self.assertEqual(physdb.current_term(), physdb.current_term(datetime.date.today()))

    def test_clock(self):
        now = ("Autumn", 2026)
        def clock(term, year, status="active", at=now):
            return physdb.clock_label({"matriculation_term": term, "matriculation_year": year,
                                       "status": status}, at)
        self.assertEqual(clock("Autumn", 2026), "0.0")        # matriculated this term
        self.assertEqual(clock("Autumn", 2025), "1.0")        # one year ago
        self.assertEqual(clock("Summer", 2026), "0.25")
        self.assertEqual(clock("Spring", 2026), "0.5")
        self.assertEqual(clock("Winter", 2026), "0.75")
        self.assertEqual(clock("Summer", 2025), "1.25")
        self.assertEqual(clock("Spring", 2025), "1.5")
        self.assertEqual(clock("Autumn", 2019), "7.0")
        self.assertEqual(clock("Autumn", 2025, at=("Winter", 2027)), "1.25")   # moves with the term
        self.assertEqual(clock("Winter", 2027), "—")           # hasn't matriculated yet
        self.assertEqual(clock("Autumn", 2020, "graduated"), "—")
        self.assertEqual(clock("Autumn", 2020, "withdrawn"), "—")
        self.assertEqual(clock("Autumn", 2024, "on_leave"), "2.0")

    def test_cnet(self):
        sid = physdb.add_student(self.conn, university_id="U20", first_name="N", last_name="M", email="n@m.o",
                                 matriculation_year=2023, expected_grad_year=None, research_area=None,
                                 notes=None, cnet="  NMarie ")
        self.assertEqual(physdb._find(self.conn, "students", "nmarie")["id"], sid)     # look up by CNET
        self.assertIn("CNET nmarie", physdb.show_student(self.conn, sid))
        self.assertIn("CNET -", physdb.show_student(self.conn, "U1001"))
        for kwargs, exc in (({"cnet": "NMARIE", "email": "x@m.o", "university_id": "U21"}, sqlite3.IntegrityError),
                            ({"cnet": "bad name", "email": "y@m.o", "university_id": "U22"}, ValueError)):
            with self.assertRaises(exc):
                physdb.add_student(self.conn, first_name="N", last_name="M", matriculation_year=2023,
                                   expected_grad_year=None, research_area=None, notes=None, **kwargs)
        for i in (30, 31):   # blank CNETs may repeat
            physdb.add_student(self.conn, university_id=f"U{i}", first_name="N", last_name="M", email=f"{i}@m.o",
                               matriculation_year=2023, expected_grad_year=None, research_area=None, notes=None)

    def test_citizenship(self):
        sid = physdb.add_student(self.conn, university_id="U9", first_name="N", last_name="M",
                                 email="n@m.o", matriculation_year=2023, expected_grad_year=None,
                                 research_area=None, notes=None, citizenship="permanent_resident")
        self.assertIn("Citizenship: permanent resident", physdb.show_student(self.conn, sid))
        self.assertIn("Citizenship: -", physdb.show_student(self.conn, "U1001"))
        with self.assertRaises(ValueError):
            physdb.add_student(self.conn, university_id="U10", first_name="N", last_name="M",
                               email="x@m.o", matriculation_year=2023, expected_grad_year=None,
                               research_area=None, notes=None, citizenship="alien")

    def test_funding_and_term_parsing(self):
        physdb.add_funding(self.conn, "U1001", "TA", 2026, "Autumn", "Dept", 12000, "ACCT-1")
        physdb.add_funding(self.conn, "U1001", "RA", 2026, "Winter", notes="Pending approval")
        text = physdb.show_student(self.conn, "U1001")
        self.assertIn("Autumn 2026", text); self.assertIn("POETAF/COA ACCT-1", text)
        self.assertIn("Notes: Pending approval", text)
        self.assertLess(text.index("Autumn 2026"), text.index("Winter 2026"))
        with self.assertRaises(sqlite3.IntegrityError):
            physdb.add_funding(self.conn, "U1001", "TA", 2026, "Fall")
        for raw, want in {"Fall 2026": (2026, "Autumn"), "spring 2027": (2027, "Spring"),
                          "2025 Summer": (2025, "Summer"), "Winter, 2024": (2024, "Winter"),
                          "AY 2026": (None, None), "": (None, None), None: (None, None)}.items():
            self.assertEqual(physdb.parse_term(raw), want, raw)

    def test_old_funding_table_is_migrated(self):
        import os, tempfile
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        c = sqlite3.connect(path)
        c.executescript("""
            CREATE TABLE students (id INTEGER PRIMARY KEY, university_id TEXT UNIQUE NOT NULL,
              first_name TEXT NOT NULL, last_name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
              program TEXT NOT NULL DEFAULT 'PhD', status TEXT NOT NULL DEFAULT 'active',
              start_year INTEGER NOT NULL, expected_grad_year INTEGER, research_area TEXT, notes TEXT);
            CREATE TABLE funding (id INTEGER PRIMARY KEY,
              student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
              kind TEXT NOT NULL, source TEXT, term TEXT NOT NULL, amount REAL);
            CREATE INDEX idx_funding_student ON funding(student_id);
            INSERT INTO students VALUES (1,'U1','A','B','a@b.c','PhD','active',2020,NULL,NULL,NULL);
            INSERT INTO funding VALUES (1,1,'TA','Dept','Fall 2026',100), (2,1,'RA',NULL,'AY 25-26',200);
        """)
        c.commit(); c.close()
        conn = physdb.connect(path)
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM funding")}
        self.assertEqual((rows[1]["term_year"], rows[1]["term_season"], rows[1]["amount"]), (2026, "Autumn", 100))
        self.assertEqual((rows[2]["term_year"], rows[2]["legacy_term"]), (None, "AY 25-26"))
        self.assertEqual(physdb.term_label(rows[2]), "AY 25-26")
        self.assertIsNone(rows[1]["poetaf_coa"])
        self.assertIn("cnet", {r[1] for r in conn.execute("PRAGMA table_info(students)")})   # added to old DBs
        self.assertEqual(conn.execute("SELECT count(*) FROM sqlite_master WHERE name='idx_students_cnet'").fetchone()[0], 1)
        self.assertIn("citizenship", {r[1] for r in conn.execute("PRAGMA table_info(students)")})   # added to old DBs
        self.assertIsNone(conn.execute("SELECT citizenship FROM students").fetchone()[0])
        scols = {r[1] for r in conn.execute("PRAGMA table_info(students)")}
        self.assertTrue({"matriculation_year", "matriculation_term"} <= scols and "start_year" not in scols)
        self.assertEqual(tuple(conn.execute("SELECT matriculation_year, matriculation_term FROM students").fetchone()),
                         (2020, "Autumn"))     # old 'started' year kept; term defaults to Autumn
        self.assertEqual(conn.execute("SELECT count(*) FROM sqlite_master WHERE name='funding_old'").fetchone()[0], 0)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(students)")}
        if sqlite3.sqlite_version_info >= (3, 35):      # DROP COLUMN needs SQLite 3.35+
            self.assertNotIn("program", cols)
        self.assertEqual(conn.execute("SELECT first_name FROM students").fetchone()[0], "A")
        physdb.add_student(conn, university_id="U2", first_name="C", last_name="D", email="c@d.e",
                           matriculation_year=2021, expected_grad_year=None, research_area=None, notes=None)
        physdb.connect(path)   # re-running on a migrated DB is a no-op
        self.assertEqual(conn.execute("SELECT count(*) FROM funding").fetchone()[0], 2)

    def test_funding_table_without_notes_gets_the_column(self):
        import os, tempfile
        path = os.path.join(tempfile.mkdtemp(), "mid.db")
        c = sqlite3.connect(path)
        c.executescript("""
            CREATE TABLE students (id INTEGER PRIMARY KEY, university_id TEXT UNIQUE NOT NULL,
              first_name TEXT NOT NULL, last_name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
              status TEXT NOT NULL DEFAULT 'active', start_year INTEGER NOT NULL,
              expected_grad_year INTEGER, research_area TEXT, notes TEXT);
            CREATE TABLE funding (id INTEGER PRIMARY KEY,
              student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
              kind TEXT NOT NULL, source TEXT, poetaf_coa TEXT, term_year INTEGER, term_season TEXT,
              legacy_term TEXT, amount REAL);
            INSERT INTO students VALUES (1,'U1','A','B','a@b.c','active',2020,NULL,NULL,NULL);
            INSERT INTO funding VALUES (1,1,'TA','Dept','ACCT',2026,'Autumn',NULL,100);
        """)
        c.commit(); c.close()
        conn = physdb.connect(path)
        row = conn.execute("SELECT * FROM funding").fetchone()
        self.assertEqual((row["poetaf_coa"], row["amount"], row["notes"]), ("ACCT", 100, None))
        physdb.add_funding(conn, "U1", "RA", 2027, "Winter", notes="hello")
        self.assertEqual(conn.execute("SELECT count(*) FROM funding WHERE notes = 'hello'").fetchone()[0], 1)
        physdb.connect(path)   # idempotent

    def test_funding_type_check_is_widened_keeping_rows(self):
        import os, tempfile
        path = os.path.join(tempfile.mkdtemp(), "oldcheck.db")
        c = sqlite3.connect(path)
        c.executescript("""
            CREATE TABLE students (id INTEGER PRIMARY KEY, university_id TEXT UNIQUE NOT NULL,
              first_name TEXT NOT NULL, last_name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
              status TEXT NOT NULL DEFAULT 'active', start_year INTEGER NOT NULL,
              expected_grad_year INTEGER, research_area TEXT, notes TEXT);
            CREATE TABLE funding (id INTEGER PRIMARY KEY,
              student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
              kind TEXT NOT NULL CHECK (kind IN ('TA', 'RA', 'fellowship', 'other')),
              source TEXT, poetaf_coa TEXT,
              term_year INTEGER CHECK (term_year BETWEEN 1950 AND 2100),
              term_season TEXT CHECK (term_season IN ('Winter', 'Spring', 'Summer', 'Autumn')),
              legacy_term TEXT, amount REAL, notes TEXT,
              CHECK ((term_year IS NULL) = (term_season IS NULL)));
            CREATE INDEX idx_funding_student ON funding(student_id);
            INSERT INTO students VALUES (1,'U1','A','B','a@b.c','active',2020,NULL,NULL,NULL);
            INSERT INTO funding VALUES (7,1,'TA','Dept','ACCT',2026,'Autumn',NULL,100,'keep me');
            INSERT INTO funding VALUES (8,1,'other',NULL,NULL,NULL,NULL,'AY 25-26',NULL,NULL);
        """)
        c.commit(); c.close()
        conn = physdb.connect(path)
        rows = {r["id"]: tuple(r) for r in conn.execute("SELECT * FROM funding")}
        self.assertEqual(len(rows), 2)
        self.assertEqual(conn.execute("SELECT notes, poetaf_coa, amount FROM funding WHERE id = 7").fetchone()[:], ("keep me", "ACCT", 100.0))
        self.assertEqual(conn.execute("SELECT legacy_term FROM funding WHERE id = 8").fetchone()[0], "AY 25-26")
        physdb.add_funding(conn, "U1", "stipend_supplement", 2027, "Spring")
        self.assertEqual(conn.execute("SELECT count(*) FROM funding").fetchone()[0], 3)
        self.assertEqual(conn.execute("SELECT count(*) FROM sqlite_master WHERE name='funding_old'").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT count(*) FROM sqlite_master WHERE name='idx_funding_student'").fetchone()[0], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            physdb.add_funding(conn, "U1", "bonus", 2027, "Spring")
        physdb.connect(path)   # second start is a no-op
        self.assertEqual(conn.execute("SELECT count(*) FROM funding").fetchone()[0], 3)

    def test_show_student_lists_advisor_first(self):
        for last, role in (("Zed", "committee"), ("Yu", "committee_chair"), ("Bohr2", "advisor")):
            fid = physdb.add_faculty(self.conn, "P", last, f"{last}@u.edu")
            physdb.assign_advisor(self.conn, "U1001", fid, role, "2024-01-01")
        text = physdb.show_student(self.conn, "U1001")
        order = sorted(("Bohr2", "Yu", "Zed"), key=text.index)
        self.assertEqual(order, ["Bohr2", "Yu", "Zed"])
        self.assertLess(text.index("advisor: P Bohr2"), text.index("committee_chair: P Yu"))
        self.assertLess(text.index("committee_chair: P Yu"), text.index("committee: P Zed"))

    def test_export(self):
        buf = io.StringIO()
        physdb.export_csv(self.conn, buf)
        self.assertIn("Lovelace", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
