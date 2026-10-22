import datetime
import io
import sqlite3
import unittest

import audit
import physdb


class PhysDBTests(unittest.TestCase):
    def setUp(self):
        self.conn = physdb.connect(":memory:")
        self.sid = physdb.add_student(
            self.conn, university_id="U1001", first_name="Ada", last_name="Lovelace",
            email="ada@uni.edu", matriculation_year=2020,
            research_area="Quantum optics", notes=None)
        self.fid = physdb.add_faculty(self.conn, "Niels", "Bohr", "bohr@uni.edu", "Atomic")

    def test_lookup_by_id_email_university_id(self):
        for key in (self.sid, "ada@uni.edu", "U1001"):
            self.assertEqual(physdb._find(self.conn, "students", key)["id"], self.sid)

    def test_duplicate_email_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            physdb.add_student(
                self.conn, university_id="U2", first_name="X", last_name="Y",
                email="ada@uni.edu", matriculation_year=2021,
                research_area=None, notes=None)

    def test_advisor_filter_and_show(self):
        physdb.assign_advisor(self.conn, "U1001", "bohr@uni.edu", start_date="2021-01-01")
        self.assertEqual(len(physdb.list_students(self.conn, advisor="bohr@uni.edu")), 1)
        self.assertIn("Niels Bohr", physdb.show_student(self.conn, "U1001"))

    def test_advisor_and_committee_filters_are_distinct(self):
        def add(uid):
            return physdb.add_student(self.conn, university_id=uid, first_name=uid, last_name=uid,
                                      email=f"{uid}@u.edu", matriculation_year=2022,
                                      research_area=None, notes=None)
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
        self.assertEqual(len(physdb.missing_milestone(self.conn, "pre_oral")), 1)
        physdb.add_milestone(self.conn, "U1001", "pre_oral", "2022-05-01", "passed")
        self.assertEqual(physdb.missing_milestone(self.conn, "pre_oral"), [])

    def test_status_and_check_constraints(self):
        physdb.set_status(self.conn, "U1001", "graduated", 2026, "Spring")
        self.assertEqual(physdb.list_students(self.conn, status="active"), [])
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE students SET status='bogus'")

    def test_matriculation_default_and_migration(self):
        self.assertEqual(physdb.matriculation_label(physdb.list_students(self.conn)[0]), "Autumn 2020")
        self.assertIn("matriculated Autumn 2020", physdb.show_student(self.conn, "U1001"))
        with self.assertRaises(ValueError):
            physdb.add_student(self.conn, university_id="U11", first_name="N", last_name="M",
                               email="y@m.o", matriculation_year=2023, matriculation_term="Fall",
                               research_area=None, notes=None)

    def test_current_term_boundaries(self):
        d = datetime.date
        for day, want in [(d(2026, 1, 1), "Winter"), (d(2026, 3, 24), "Winter"), (d(2026, 3, 25), "Spring"),
                          (d(2026, 6, 14), "Spring"), (d(2026, 6, 15), "Summer"), (d(2026, 9, 24), "Summer"),
                          (d(2026, 9, 25), "Autumn"), (d(2026, 10, 17), "Autumn"), (d(2026, 12, 31), "Autumn")]:
            self.assertEqual(physdb.current_term(day), (want, 2026), day)
        self.assertEqual(physdb.current_term(), physdb.current_term(datetime.date.today()))

    def test_clock_at_a_date(self):
        stu = {"matriculation_term": "Autumn", "matriculation_year": 2022, "status": "active"}
        at = physdb.clock_label_at
        self.assertEqual(at(stu, "2022-10-17"), "0.0")                 # during the matriculation term
        self.assertEqual(at(stu, "2022-09-25"), "0.0")                 # first day of Autumn
        self.assertEqual(at(stu, "2022-12-31"), "0.0")
        self.assertEqual(at(stu, "2023-01-10"), "0.25")                # Winter 2023
        self.assertEqual(at(stu, "2023-03-25"), "0.5")                 # Spring starts Mar 25
        self.assertEqual(at(stu, "2024-05-01"), "1.5")                 # Spring 2024: 6 terms
        self.assertEqual(at(stu, "2023-10-01"), "1.0")
        self.assertEqual(at(stu, datetime.date(2024, 5, 1)), "1.5")    # a date object works too
        self.assertEqual(at(stu, "2030-01-01"), "7.25")                # future (planned) dates work: Winter 2030, 29 terms in
        self.assertEqual(at(stu, "2022-09-24"), "—")                   # Summer 2022: before they matriculated
        self.assertEqual(at(stu, "2020-01-01"), "—")
        for bad in ("", "not a date", "2024-13-45", None):
            self.assertEqual(at(stu, bad), "—", bad)
        # the student's status today doesn't matter for how far along they were then
        for status in ("graduated", "withdrawn", "on_leave"):
            self.assertEqual(at({**stu, "status": status}, "2024-05-01"), "1.5", status)
            self.assertEqual(physdb.clock_label({**stu, "status": status}, ("Autumn", 2026)),
                             "4.0" if status == "on_leave" else "—")

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

    def test_stipend_supplement_entries_are_renamed_supplement(self):
        import os, tempfile
        path = os.path.join(tempfile.mkdtemp(), "stipend.db")
        c = sqlite3.connect(path)
        c.executescript("""
            CREATE TABLE students (id INTEGER PRIMARY KEY, university_id TEXT UNIQUE NOT NULL,
              first_name TEXT NOT NULL, last_name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
              status TEXT NOT NULL DEFAULT 'active', matriculation_year INTEGER NOT NULL,
              expected_grad_year INTEGER, research_area TEXT, notes TEXT);
            CREATE TABLE funding (id INTEGER PRIMARY KEY,
              student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
              kind TEXT NOT NULL CHECK (kind IN ('TA', 'RA', 'fellowship', 'stipend_supplement', 'other')),
              source TEXT, poetaf_coa TEXT,
              term_year INTEGER CHECK (term_year BETWEEN 1950 AND 2100),
              term_season TEXT CHECK (term_season IN ('Winter', 'Spring', 'Summer', 'Autumn')),
              legacy_term TEXT, amount REAL, notes TEXT,
              CHECK ((term_year IS NULL) = (term_season IS NULL)));
            CREATE INDEX idx_funding_student ON funding(student_id);
            INSERT INTO students VALUES (1,'U1','A','B','a@b.c','active',2020,NULL,NULL,NULL);
            INSERT INTO funding VALUES (1,1,'stipend_supplement','Dept','ACCT',2026,'Autumn',NULL,500,'keep');
            INSERT INTO funding VALUES (2,1,'RA','NSF',NULL,2026,'Autumn',NULL,3000,NULL);
        """)
        c.commit(); c.close()
        conn = physdb.connect(path)
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM funding")}
        self.assertEqual((rows[1]["kind"], rows[1]["amount"], rows[1]["poetaf_coa"], rows[1]["notes"]),
                         ("supplement", 500.0, "ACCT", "keep"))
        self.assertEqual(rows[2]["kind"], "RA")
        self.assertEqual(conn.execute("SELECT count(*) FROM funding WHERE kind = 'stipend_supplement'").fetchone()[0], 0)
        with self.assertRaises(sqlite3.IntegrityError):     # the old name is no longer accepted
            physdb.add_funding(conn, "U1", "stipend_supplement", 2027, "Spring")
        physdb.connect(path)                                # second start changes nothing
        self.assertEqual(conn.execute("SELECT count(*) FROM funding WHERE kind = 'supplement'").fetchone()[0], 1)

    def test_previous_term(self):
        pt = physdb.previous_term
        self.assertEqual(pt("Winter", 2027), ("Autumn", 2026))      # across the year boundary
        self.assertEqual(pt("Autumn", 2026), ("Summer", 2026))
        self.assertEqual(pt("Summer", 2026), ("Spring", 2026))
        self.assertEqual(pt("Spring", 2026), ("Winter", 2026))
        self.assertEqual(pt("Winter", 2000), ("Autumn", 1999))

    def test_next_term(self):
        nt, pt = physdb.next_term, physdb.previous_term
        self.assertEqual(nt("Autumn", 2026), ("Winter", 2027))      # across the year boundary
        self.assertEqual(nt("Winter", 2026), ("Spring", 2026))
        self.assertEqual(nt("Spring", 2026), ("Summer", 2026))
        self.assertEqual(nt("Summer", 2026), ("Autumn", 2026))
        for season, year in (("Winter", 2000), ("Summer", 2026), ("Autumn", 2031)):    # they undo each other
            self.assertEqual(pt(*nt(season, year)), (season, year))
            self.assertEqual(nt(*pt(season, year)), (season, year))

    def test_copy_funding_from_any_term(self):
        physdb.add_student(self.conn, university_id="C1", first_name="F", last_name="C1", email="c1@m.o",
                           matriculation_year=2022, research_area=None, notes=None)
        sid = physdb._find(self.conn, "students", "C1")["id"]
        physdb.add_funding(self.conn, "C1", "RA", 2025, "Spring", "Old", 111, "OLD-1", "two terms back")
        physdb.add_funding(self.conn, "C1", "TA", 2026, "Summer", "Recent", 222)
        physdb.add_funding(self.conn, "C1", "fellowship", 2026, "Autumn", "Existing", 9)
        got = lambda y, s: [tuple(r) for r in self.conn.execute(
            "SELECT kind, source, poetaf_coa, amount, notes FROM funding WHERE student_id = ? AND term_year = ? "
            "AND term_season = ?", (sid, y, s))]
        # copy from a term that is NOT the previous one; the existing Autumn entry is replaced
        self.assertEqual(physdb.copy_funding(self.conn, sid, 2025, "Spring", 2026, "Autumn"), (1, 1))
        self.assertEqual(got(2026, "Autumn"), [("RA", "Old", "OLD-1", 111.0, "two terms back")])
        self.assertEqual(got(2025, "Spring"), [("RA", "Old", "OLD-1", 111.0, "two terms back")])   # source untouched
        self.assertEqual(got(2026, "Summer"), [("TA", "Recent", None, 222.0, None)])             # others untouched
        # a source with nothing in it changes nothing
        self.assertEqual(physdb.copy_funding(self.conn, sid, 2024, "Winter", 2026, "Autumn"), (0, 0))
        self.assertEqual(len(got(2026, "Autumn")), 1)
        # copying a term onto itself is refused
        with self.assertRaises(ValueError):
            physdb.copy_funding(self.conn, sid, 2026, "Autumn", 2026, "Autumn")
        self.assertEqual(len(got(2026, "Autumn")), 1)
        # the "previous term" helper is just the common case
        self.assertEqual(physdb.copy_previous_funding(self.conn, sid, 2026, "Autumn"), (1, ("Summer", 2026), 1))
        self.assertEqual(got(2026, "Autumn"), [("TA", "Recent", None, 222.0, None)])

    def test_copy_previous_funding(self):
        def add(uid):
            physdb.add_student(self.conn, university_id=uid, first_name="F", last_name=uid, email=f"{uid}@m.o",
                               matriculation_year=2022, research_area=None, notes=None)
        add("S1"); add("S2")
        sid = lambda u: physdb._find(self.conn, "students", u)["id"]
        rows = lambda u, y, s: [tuple(r) for r in self.conn.execute(
            "SELECT kind, source, poetaf_coa, amount, notes FROM funding WHERE student_id = ? AND term_year = ? "
            "AND term_season = ? ORDER BY id", (sid(u), y, s))]
        # nothing in the previous term: nothing changes, even if the displayed term already has entries
        physdb.add_funding(self.conn, "S1", "TA", 2026, "Autumn", "Keep", 1, "KEEP", "keep me")
        self.assertEqual(physdb.copy_previous_funding(self.conn, sid("S1"), 2026, "Autumn"), (0, ("Summer", 2026), 0))
        self.assertEqual(rows("S1", 2026, "Autumn"), [("TA", "Keep", "KEEP", 1.0, "keep me")])
        # previous term has two entries; the displayed term is empty -> both copied, every field
        physdb.add_funding(self.conn, "S2", "RA", 2026, "Summer", "NSF", 3000, "COA-1", "note a")
        physdb.add_funding(self.conn, "S2", "supplement", 2026, "Summer", None, 500, None, None)
        physdb.add_funding(self.conn, "S2", "TA", 2026, "Winter", "Dept", 7)       # a different term: untouched
        n, prev, replaced = physdb.copy_previous_funding(self.conn, sid("S2"), 2026, "Autumn")
        self.assertEqual((n, prev, replaced), (2, ("Summer", 2026), 0))
        want = [("RA", "NSF", "COA-1", 3000.0, "note a"), ("supplement", None, None, 500.0, None)]
        self.assertEqual(rows("S2", 2026, "Autumn"), want)
        self.assertEqual(rows("S2", 2026, "Summer"), want)                         # the original is left alone
        self.assertEqual(len(rows("S2", 2026, "Winter")), 1)
        # existing entries in the displayed term are REPLACED, not added to
        physdb.add_funding(self.conn, "S2", "fellowship", 2027, "Winter", "Old", 1)
        physdb.add_funding(self.conn, "S2", "other", 2027, "Winter", "Old2", 2)
        self.assertEqual(rows("S2", 2026, "Autumn"), want)
        physdb.add_funding(self.conn, "S2", "TA", 2026, "Autumn", "Extra", 9)       # now 3 entries in Autumn 2026
        n, prev, replaced = physdb.copy_previous_funding(self.conn, sid("S2"), 2026, "Autumn")
        self.assertEqual((n, replaced), (2, 3))
        self.assertEqual(rows("S2", 2026, "Autumn"), want)
        # other students are never affected
        self.assertEqual(rows("S1", 2026, "Autumn"), [("TA", "Keep", "KEEP", 1.0, "keep me")])
        # across a year boundary
        n, prev, _ = physdb.copy_previous_funding(self.conn, sid("S2"), 2027, "Winter")
        self.assertEqual((n, prev), (2, ("Autumn", 2026)))
        self.assertEqual(rows("S2", 2027, "Winter"), want)                          # replaced Old / Old2

    def _legacy_milestones_db(self, ddl_kinds, name):
        import os, tempfile
        path = os.path.join(tempfile.mkdtemp(), name)
        c = sqlite3.connect(path)
        c.executescript(f"""
            CREATE TABLE students (id INTEGER PRIMARY KEY, university_id TEXT UNIQUE NOT NULL,
              first_name TEXT NOT NULL, last_name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
              status TEXT NOT NULL DEFAULT 'active', matriculation_year INTEGER NOT NULL,
              expected_grad_year INTEGER, research_area TEXT, notes TEXT);
            INSERT INTO students VALUES (1,'U1','A','B','a@b.c','active',2020,NULL,NULL,NULL);
            CREATE TABLE milestones (id INTEGER PRIMARY KEY,
              student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
              kind TEXT NOT NULL CHECK (kind IN ({ddl_kinds})),
              date TEXT NOT NULL,
              outcome TEXT NOT NULL DEFAULT 'pending' CHECK (outcome IN ('pending', 'passed', 'conditional', 'failed')),
              notes TEXT);
            CREATE INDEX idx_milestones_student ON milestones(student_id);
            INSERT INTO milestones VALUES (5, 1, 'qualifying_exam', '2022-05-01', 'passed', 'kept note');
            INSERT INTO milestones VALUES (6, 1, 'thesis_defense', '2026-05-01', 'pending', NULL);
        """)
        c.commit(); c.close()
        return path

    def test_milestone_types_table_is_widened_keeping_rows(self):
        path = self._legacy_milestones_db("'qualifying_exam','candidacy','thesis_proposal','thesis_defense','other'", "ms.db")
        conn = physdb.connect(path)
        rows = [tuple(r) for r in conn.execute("SELECT id, student_id, kind, date, outcome, notes FROM milestones ORDER BY id")]
        self.assertEqual(rows, [(5, 1, "qualifying_exam", "2022-05-01", "passed", "kept note"),
                                (6, 1, "thesis_defense", "2026-05-01", "pending", None)])
        for kind in ("candidacy", "pre_oral", "ms_degree"):                                       # the new types are accepted
            physdb.add_milestone(conn, "U1", kind, "2023-03-01", "passed")
        for kind in ("bogus", "qualifying_exam", "thesis_proposal"):                            # others (and retired ones) aren't
            with self.assertRaises(ValueError):
                physdb.add_milestone(conn, "U1", kind, "2023-03-01")
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        self.assertNotIn("milestones_old", names); self.assertIn("idx_milestones_student", names)
        physdb.connect(path)                                                                    # a second start changes nothing
        self.assertEqual(conn.execute("SELECT count(*) FROM milestones").fetchone()[0], 5)
        # deleting the student still removes their milestones (the foreign key survived the rebuild)
        conn.execute("DELETE FROM students WHERE id = 1"); conn.commit()
        self.assertEqual(conn.execute("SELECT count(*) FROM milestones").fetchone()[0], 0)

    def test_an_interrupted_table_rebuild_is_finished_on_the_next_start(self):
        kinds = "'qualifying_exam','candidacy','thesis_proposal','thesis_defense','other'"
        # (1) killed right after the old table was set aside: the new, empty one is created and the rows come back
        path = self._legacy_milestones_db(kinds, "crash.db")
        c = sqlite3.connect(path)
        c.executescript("DROP INDEX idx_milestones_student; ALTER TABLE milestones RENAME TO milestones_old;")
        c.commit(); c.close()
        conn = physdb.connect(path)
        self.assertEqual([r[0] for r in conn.execute("SELECT id FROM milestones ORDER BY id")], [5, 6])
        self.assertEqual(conn.execute("SELECT count(*) FROM sqlite_master WHERE name = 'milestones_old'").fetchone()[0], 0)
        # (2) killed part-way through copying the rows back: the missing ones are copied, nothing is doubled or lost
        path = self._legacy_milestones_db(kinds, "partial.db")
        c = sqlite3.connect(path)
        c.executescript("DROP INDEX idx_milestones_student; ALTER TABLE milestones RENAME TO milestones_old;")
        c.executescript(physdb.SCHEMA.read_text())
        c.execute("INSERT INTO milestones SELECT * FROM milestones_old WHERE id = 5")        # only one row had been copied
        c.commit(); c.close()
        conn = physdb.connect(path)
        self.assertEqual([tuple(r) for r in conn.execute("SELECT id, kind, notes FROM milestones ORDER BY id")],
                         [(5, "qualifying_exam", "kept note"), (6, "thesis_defense", None)])
        self.assertEqual(conn.execute("SELECT count(*) FROM sqlite_master WHERE name = 'milestones_old'").fetchone()[0], 0)

    def _milestone_check_students(self):
        """At Autumn 2026: A clock 1.0, B 0.5, C 0.25, D 0.0 -- plus people who must never appear."""
        def add(uid, term, year, status="active"):
            sid = physdb.add_student(self.conn, university_id=uid, first_name="F", last_name=uid, email=f"{uid}@m.o",
                                     matriculation_year=year, matriculation_term=term, research_area=None, notes=None)
            self.conn.execute("UPDATE students SET status = ? WHERE id = ?", (status, sid)); self.conn.commit()
        add("A", "Autumn", 2025); add("B", "Spring", 2026); add("C", "Summer", 2026); add("D", "Autumn", 2026)
        add("E", "Winter", 2027)                       # hasn't matriculated yet: no clock
        add("F", "Autumn", 2020, "graduated"); add("G", "Autumn", 2020, "on_leave"); add("H", "Autumn", 2020)
        physdb.add_milestone(self.conn, "H", "pre_oral", "2022-05-01", "passed")     # already has it
        physdb.add_milestone(self.conn, "A", "pre_oral", "2025-12-01", "failed")     # a failure doesn't count as done
        physdb.add_milestone(self.conn, "B", "candidacy", "2026-05-01", "passed")           # a different milestone

    def test_missing_milestone_uses_the_clock_and_accepts_fractions(self):
        self._milestone_check_students()
        now = ("Autumn", 2026)
        # (the fixture's own student U1001, matriculated Autumn 2020, has clock 6.0 and no qualifying exam)
        everyone = lambda years, kind="pre_oral": [r["university_id"] for r in
                                                           physdb.missing_milestone(self.conn, kind, years, now)]
        who = lambda years, kind="pre_oral": [u for u in everyone(years, kind) if u != "U1001"]
        self.assertIn("U1001", everyone(6)); self.assertNotIn("U1001", everyone(6.25))      # 6.0 qualifies for 6, not 6.25
        # H (passed), F (graduated), G (on leave) and E (not matriculated yet) are never listed
        self.assertEqual(who(0), ["A", "B", "C", "D"])                # oldest matriculation first
        self.assertEqual(sorted(who(0)), ["A", "B", "C", "D"])
        self.assertEqual(who(0.25), ["A", "B", "C"])                  # clock >= 0.25
        self.assertEqual(who(0.3), ["A", "B"])                        # 0.25 is not enough for 0.3
        self.assertEqual(who(0.5), ["A", "B"])
        self.assertEqual(who(0.75), ["A"])
        self.assertEqual(who(1), ["A"])                               # exactly 1.0 qualifies
        self.assertEqual(who(1.25), [])
        self.assertEqual(who(2), [])
        # integers still behave (the old inputs keep working), and other milestone kinds are independent
        self.assertEqual(who(1, "candidacy"), ["H", "A"])             # B has a passed candidacy; H passed a different milestone
        self.assertEqual(sorted(who(0, "candidacy")), ["A", "C", "D", "H"])        # B is the only one who is done
        # each row names the current advisor(s) by last name
        fa = physdb.add_faculty(self.conn, "Niels", "Bohr", "bohr@u.edu")
        fb = physdb.add_faculty(self.conn, "Marie", "Curie", "curie@u.edu")
        for sid, f, role in (("A", fa, "advisor"), ("A", fb, "co-advisor"), ("B", fb, "committee"), ("C", fa, "advisor")):
            physdb.assign_advisor(self.conn, sid, f, role, "2025-01-01")
        self.conn.execute("UPDATE advising SET end_date = '2025-06-01' WHERE student_id = (SELECT id FROM students WHERE university_id = 'C')")
        self.conn.commit()
        adv = {r["university_id"]: r["advisors"] for r in physdb.missing_milestone(self.conn, "pre_oral", 0, now)}
        self.assertEqual(set(adv["A"].split(", ")), {"Bohr", "Curie"})
        self.assertIsNone(adv["B"])                                   # committee only: not an advisor
        self.assertIsNone(adv["C"])                                   # ended
        self.assertIsNone(adv["D"])

    def test_funding_report(self):
        def add(uid, last, status="active"):
            sid = physdb.add_student(self.conn, university_id=uid, first_name="F", last_name=last,
                                     email=f"{uid}@m.o", matriculation_year=2022, research_area=None, notes=None)
            self.conn.execute("UPDATE students SET status = ? WHERE id = ?", (status, sid))
            return uid
        add("A1", "Zed"); add("B1", "Yu"); add("C1", "Xi", "on_leave"); add("D1", "Wu", "graduated")
        physdb.add_funding(self.conn, "A1", "RA", 2026, "Autumn", "NSF", 3000, "COA-1", "note a")
        physdb.add_funding(self.conn, "A1", "supplement", 2026, "Autumn", None, 500)
        physdb.add_funding(self.conn, "A1", "TA", 2026, "Winter", "Dept", 2000)      # other term
        physdb.add_funding(self.conn, "C1", "RA", 2026, "Autumn", "NSF", 1)          # on leave: excluded
        rows = physdb.funding_report(self.conn, 2026, "Autumn")
        who = [(r["university_id"], r["kind"], r["amount"]) for r in rows]
        self.assertIn(("U1001", None, None), who)                  # Ada (setUp): active, nothing recorded
        self.assertIn(("B1", None, None), who)                     # still listed, with empty fields
        self.assertEqual(sorted(w for w in who if w[0] == "A1"),
                         [("A1", "RA", 3000.0), ("A1", "supplement", 500.0)])   # one row per entry
        self.assertEqual({w[0] for w in who}, {"U1001", "A1", "B1"})       # on leave / graduated excluded
        a1 = [r for r in rows if r["kind"] == "RA"][0]
        self.assertEqual((a1["source"], a1["poetaf_coa"], a1["notes"]), ("NSF", "COA-1", "note a"))
        self.assertEqual([w for w in physdb.funding_report(self.conn, 2026, "Winter") if w["kind"]][0]["kind"], "TA")
        self.assertEqual(len(physdb.funding_report(self.conn, 1999, "Spring")), 3)       # all blank
        self.assertEqual(physdb.undated_funding_count(self.conn), 0)

    # ---------------- faculty CNET and faculty logins ----------------
    def _login(self, username, role="viewer", faculty_id=None):
        self.conn.execute("INSERT INTO users (username, password_hash, role, must_change, faculty_id) VALUES (?,?,?,1,?)",
                          (username, "x", role, faculty_id))
        self.conn.commit()

    def test_faculty_cnet(self):
        fid = physdb.add_faculty(self.conn, "Marie", "Curie", "curie@u.edu", cnet="  MCurie ")
        self.assertEqual(self.conn.execute("SELECT cnet FROM faculty WHERE id = ?", (fid,)).fetchone()[0], "mcurie")
        self.assertEqual(physdb._find(self.conn, "faculty", "MCURIE"), physdb._find(self.conn, "faculty", fid))
        self.assertEqual(physdb._find(self.conn, "faculty", "mcurie")["id"], fid)          # look up by CNET
        with self.assertRaises(sqlite3.IntegrityError):                                    # unique, case-insensitively
            physdb.add_faculty(self.conn, "X", "Y", "xy@u.edu", cnet="mcurie")
        with self.assertRaises(ValueError):
            physdb.add_faculty(self.conn, "X", "Y", "xy@u.edu", cnet="bad name")
        for i in (1, 2):                                                                   # blank CNETs may repeat
            physdb.add_faculty(self.conn, "No", f"Cnet{i}", f"nc{i}@u.edu")

    def test_faculty_login_rules(self):
        a = physdb.add_faculty(self.conn, "Niels", "Bohr", "bohr@u.edu", cnet="nbohr")
        b = physdb.add_faculty(self.conn, "Marie", "Curie", "curie@u.edu", cnet="mcurie")
        c = physdb.add_faculty(self.conn, "Max", "Planck", "planck@u.edu")                 # no CNET
        # a faculty login must point at a faculty member, and others must not
        with self.assertRaises(sqlite3.IntegrityError):
            self._login("nobody", role="faculty")
        with self.assertRaises(sqlite3.IntegrityError):
            self._login("viewer1", role="viewer", faculty_id=a)
        # its username has to be the faculty member's CNET (nothing else will do)
        for bad in ("someone-else", "NBOHR", "mcurie"):
            with self.assertRaises(sqlite3.IntegrityError, msg=bad):
                self._login(bad, role="faculty", faculty_id=a)
        with self.assertRaises(sqlite3.IntegrityError):                                    # no CNET -> no login
            self._login("planck", role="faculty", faculty_id=c)
        self._login("nbohr", role="faculty", faculty_id=a)
        with self.assertRaises(sqlite3.IntegrityError):                                    # one login per person
            self._login("nbohr2", role="faculty", faculty_id=a)
        self._login("mcurie", role="faculty", faculty_id=b)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM users WHERE role = 'faculty'").fetchone()[0], 2)

    def test_faculty_cnet_changes_follow_the_login(self):
        a = physdb.add_faculty(self.conn, "Niels", "Bohr", "bohr@u.edu", cnet="nbohr")
        physdb.add_faculty(self.conn, "Marie", "Curie", "curie@u.edu", cnet="mcurie")
        self._login("nbohr", role="faculty", faculty_id=a)
        self._login("taken", role="viewer")
        user = lambda: self.conn.execute("SELECT username FROM users WHERE faculty_id = ?", (a,)).fetchone()[0]
        # a new CNET renames the login
        self.conn.execute("UPDATE faculty SET cnet = 'niels2' WHERE id = ?", (a,)); self.conn.commit()
        self.assertEqual(user(), "niels2")
        # removing the CNET is refused while a login exists, and nothing changes
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE faculty SET cnet = NULL WHERE id = ?", (a,))
        self.conn.rollback()
        self.assertEqual(self.conn.execute("SELECT cnet FROM faculty WHERE id = ?", (a,)).fetchone()[0], "niels2")
        # a CNET that another login already uses can't be taken, and the faculty record is left untouched
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE faculty SET cnet = 'taken' WHERE id = ?", (a,))
        self.conn.rollback()
        self.assertEqual((user(), self.conn.execute("SELECT cnet FROM faculty WHERE id = ?", (a,)).fetchone()[0]),
                         ("niels2", "niels2"))
        # faculty without a login can change or clear their CNET freely
        self.conn.execute("UPDATE faculty SET cnet = NULL WHERE email = 'curie@u.edu'"); self.conn.commit()
        # deleting the faculty member removes the login with it
        self.conn.execute("DELETE FROM faculty WHERE id = ?", (a,)); self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM users WHERE role = 'faculty'").fetchone()[0], 0)

    def test_users_and_faculty_tables_from_older_versions_are_migrated(self):
        import os, tempfile
        for with_must_change in (True, False):
            path = os.path.join(tempfile.mkdtemp(), "old.db")
            c = sqlite3.connect(path)
            c.executescript(f"""
                CREATE TABLE faculty (id INTEGER PRIMARY KEY, first_name TEXT NOT NULL, last_name TEXT NOT NULL,
                  email TEXT UNIQUE NOT NULL, research_area TEXT);
                INSERT INTO faculty VALUES (1, 'Niels', 'Bohr', 'bohr@u.edu', 'Atomic');
                CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
                  role TEXT NOT NULL DEFAULT 'viewer' CHECK (role IN ('viewer', 'editor', 'admin'))
                  {', must_change INTEGER NOT NULL DEFAULT 0' if with_must_change else ''});
                INSERT INTO users (id, username, password_hash, role) VALUES (1, 'boss', 'h1', 'admin'), (2, 'ed', 'h2', 'editor');
            """)
            c.commit(); c.close()
            conn = physdb.connect(path)
            rows = [tuple(r) for r in conn.execute("SELECT id, username, password_hash, role, faculty_id FROM users ORDER BY id")]
            self.assertEqual(rows, [(1, "boss", "h1", "admin", None), (2, "ed", "h2", "editor", None)])
            self.assertIn("cnet", {r[1] for r in conn.execute("PRAGMA table_info(faculty)")})
            self.assertEqual(conn.execute("SELECT research_area FROM faculty").fetchone()[0], "Atomic")
            conn.execute("UPDATE faculty SET cnet = 'nbohr' WHERE id = 1")
            conn.execute("INSERT INTO users (username, password_hash, role, faculty_id) VALUES ('nbohr', 'h3', 'faculty', 1)")
            conn.commit()
            physdb.connect(path)                                                  # a second start changes nothing
            self.assertEqual(conn.execute("SELECT count(*) FROM users").fetchone()[0], 3)

    def test_users_table_from_the_faculty_era_gains_fsr_logins(self):
        import os, tempfile
        path = os.path.join(tempfile.mkdtemp(), "faculty_era.db")
        conn = physdb.connect(path)
        physdb.add_faculty(conn, "Niels", "Bohr", "bohr@u.edu", cnet="nbohr")
        conn.executescript("""   -- put the users table back the way the faculty release made it (no 'fsr' role, no fsr_id)
            DROP TRIGGER fsr_cnet_needed_for_login; DROP TRIGGER fsr_cnet_renames_login;
            DROP TABLE users;
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
              role TEXT NOT NULL DEFAULT 'viewer' CHECK (role IN ('viewer', 'editor', 'admin', 'faculty')),
              must_change INTEGER NOT NULL DEFAULT 0,
              faculty_id INTEGER REFERENCES faculty(id) ON DELETE CASCADE,
              CHECK ((role = 'faculty') = (faculty_id IS NOT NULL)));
            INSERT INTO users VALUES (1, 'boss', 'h1', 'admin', 0, NULL), (2, 'nbohr', 'h2', 'faculty', 1, 1);""")
        conn.commit(); conn.close()
        conn = physdb.connect(path)
        self.assertEqual([tuple(r) for r in conn.execute("SELECT id, username, role, must_change, faculty_id, fsr_id FROM users ORDER BY id")],
                         [(1, "boss", "admin", 0, None, None), (2, "nbohr", "faculty", 1, 1, None)])
        self.assertIn("fsr_id", {r[1] for r in conn.execute("PRAGMA table_info(faculty)")})
        # the faculty login rules still work after the rebuild (their triggers were re-pointed at the new table)
        conn.execute("UPDATE faculty SET cnet = 'niels' WHERE id = 1"); conn.commit()
        self.assertEqual(conn.execute("SELECT username FROM users WHERE id = 2").fetchone()[0], "niels")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE faculty SET cnet = NULL WHERE id = 1")
        # and an FSR can be added, linked to faculty, and given a login named by their CNET
        fsr = physdb.add_fsr(conn, "Xavier", "Xx", "xx@u.edu", cnet="FSRX")
        conn.execute("UPDATE faculty SET fsr_id = ?", (fsr,))
        conn.execute("INSERT INTO users (username, password_hash, role, fsr_id) VALUES ('fsrx', 'h3', 'fsr', ?)", (fsr,))
        for bad in ("INSERT INTO users (username, password_hash, role, fsr_id) VALUES ('other', 'h', 'fsr', %d)" % fsr,   # one login each
                    "INSERT INTO users (username, password_hash, role) VALUES ('x', 'h', 'fsr')",                          # needs an FSR
                    "INSERT INTO users (username, password_hash, role, fsr_id) VALUES ('x', 'h', 'viewer', %d)" % fsr):  # only for fsr
            with self.assertRaises(sqlite3.IntegrityError, msg=bad):
                conn.execute(bad)
        conn.execute("UPDATE fsrs SET cnet = 'newx' WHERE id = ?", (fsr,)); conn.commit()
        self.assertEqual(conn.execute("SELECT username FROM users WHERE fsr_id = ?", (fsr,)).fetchone()[0], "newx")
        conn.execute("DELETE FROM fsrs WHERE id = ?", (fsr,)); conn.commit()
        self.assertIsNone(conn.execute("SELECT fsr_id FROM faculty").fetchone()[0])           # faculty keep going
        self.assertEqual(conn.execute("SELECT count(*) FROM users WHERE role = 'fsr'").fetchone()[0], 0)   # the login goes
        physdb.connect(path)                                                                    # a second start changes nothing

    def test_graduation_term(self):
        conn = self.conn
        physdb.set_status(conn, "U1001", "graduated", 2026, "Spring")
        row = conn.execute("SELECT * FROM students").fetchone()
        self.assertEqual((row["status"], physdb.graduation_label(row)), ("graduated", "Spring 2026"))
        for bad in ((None, None), (2026, None), (None, "Spring"), (2026, "Fall")):
            with self.assertRaises(ValueError, msg=bad):
                physdb.set_status(conn, "U1001", "graduated", *bad)
        self.assertEqual(conn.execute("SELECT graduation_year FROM students").fetchone()[0], 2026)     # refused: unchanged
        physdb.set_status(conn, "U1001", "active", 2026, "Spring")                                    # not graduated: cleared
        row = conn.execute("SELECT * FROM students").fetchone()
        self.assertEqual((row["graduation_year"], row["graduation_term"], physdb.graduation_label(row)), (None, None, ""))
        physdb.set_status(conn, "U1001", "graduated", 2027, "Winter")
        self.assertIn("graduated Winter 2027", physdb.show_student(conn, "U1001"))

    def test_expected_graduation_year_column_is_replaced(self):
        import os, tempfile
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        conn = physdb.connect(path)
        audit.drop_triggers(conn)                       # (a database from before the audit log has none of these)
        conn.executescript("""ALTER TABLE students DROP COLUMN graduation_year; ALTER TABLE students DROP COLUMN graduation_term;
                              ALTER TABLE students ADD COLUMN expected_grad_year INTEGER;""")
        conn.execute("INSERT INTO students (university_id, first_name, last_name, email, status, matriculation_year, expected_grad_year)"
                     " VALUES ('U9', 'Old', 'Timer', 'o@u.edu', 'graduated', 2015, 2020)")
        conn.commit(); conn.close()
        conn = physdb.connect(path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(students)")}
        self.assertTrue({"graduation_year", "graduation_term"} <= cols)
        if physdb.sqlite3.sqlite_version_info >= (3, 35):
            self.assertNotIn("expected_grad_year", cols)
        row = conn.execute("SELECT * FROM students WHERE university_id = 'U9'").fetchone()
        self.assertEqual((row["status"], row["graduation_year"], row["graduation_term"]), ("graduated", None, None))   # kept, no term yet
        physdb.connect(path)                                                                                          # idempotent

    def test_graduates_drop_off_fsr_tables_two_terms_after_graduation(self):
        fsr = physdb.add_fsr(self.conn, "Xavier", "Xx", "xx@u.edu")
        self.conn.execute("UPDATE faculty SET fsr_id = ? WHERE id = ?", (fsr, self.fid))
        physdb.assign_advisor(self.conn, "U1001", self.fid)
        physdb.set_status(self.conn, "U1001", "graduated", 2026, "Summer")
        listed = lambda now: [r["last_name"] for r in physdb.fsr_advisees(self.conn, fsr, now)]
        for now in (("Spring", 2026), ("Summer", 2026), ("Autumn", 2026)):        # the term itself and the one after
            self.assertEqual(listed(now), ["Lovelace"], now)
        for now in (("Winter", 2027), ("Spring", 2027), ("Autumn", 2030)):        # from the start of Winter 2027 on
            self.assertEqual(listed(now), [], now)
        # the boundary follows the calendar: today's date decides, via current_term
        self.assertEqual(physdb.current_term(datetime.date(2026, 12, 31)), ("Autumn", 2026))
        self.assertEqual(physdb.current_term(datetime.date(2027, 1, 1)), ("Winter", 2027))
        # other graduation terms, including across a year boundary
        physdb.set_status(self.conn, "U1001", "graduated", 2026, "Autumn")
        self.assertEqual(listed(("Autumn", 2026)), ["Lovelace"]); self.assertEqual(listed(("Winter", 2027)), ["Lovelace"])
        self.assertEqual(listed(("Spring", 2027)), []); self.assertEqual(listed(("Summer", 2027)), [])
        # only graduates are affected; a graduate with no recorded term stays (nothing to count from)
        physdb.set_status(self.conn, "U1001", "graduated", 2020, "Spring")
        self.conn.execute("UPDATE students SET graduation_year = NULL, graduation_term = NULL"); self.conn.commit()
        self.assertEqual(listed(("Autumn", 2040)), ["Lovelace"])
        # faculty are not affected: their own list keeps graduates forever
        physdb.set_status(self.conn, "U1001", "graduated", 2020, "Spring")
        self.assertEqual([r["last_name"] for r in physdb.faculty_advisees(self.conn, self.fid)], ["Lovelace"])

    def test_core_course_credits(self):
        self.assertEqual([c for c, _, _ in physdb.CORE_COURSES], ["cm", "qm1", "qm2", "em1", "em2", "sm"])
        self.assertEqual([c for _, c, _ in physdb.CORE_COURSES], ["CM", "QM-1", "QM-2", "EM-1", "EM-2", "SM"])
        self.assertEqual(physdb.course_credits(self.conn, self.sid), set())
        physdb.set_course_credits(self.conn, self.sid, ["sm", "cm"])
        self.assertEqual(physdb.course_credits(self.conn, self.sid), {"cm", "sm"})
        self.assertEqual(physdb.courses_label({"sm", "qm2", "cm"}), "CM, QM-2, SM")                 # always in course order
        physdb.set_course_credits(self.conn, "U1001", ["em1"])                                       # by University ID; replaces
        self.assertEqual(physdb.course_credits(self.conn, self.sid), {"em1"})
        with self.assertRaises(ValueError):
            physdb.set_course_credits(self.conn, self.sid, ["cm", "gde"])
        self.assertEqual(physdb.course_credits(self.conn, self.sid), {"em1"})                        # refused: unchanged
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO course_credits VALUES (?, 'xx')", (self.sid,))
        # the Milestones Table data carries them
        row = [r for r in physdb.milestone_snapshot(self.conn) if r["id"] == self.sid][0]
        self.assertEqual(row["courses"], {"em1"})
        for kind in ("gde_classical_mechanics", "gde_quantum_mechanics", "gde_electrodynamics", "gde_statistical_mechanics"):
            self.assertIn(kind, physdb.RETIRED_MILESTONE_KINDS); self.assertNotIn(kind, physdb.MILESTONE_KINDS)
            with self.assertRaises(ValueError):
                physdb.add_milestone(self.conn, "U1001", kind, "2024-01-01")
            self.conn.execute("INSERT INTO milestones (student_id, kind, date) VALUES (?, ?, '2024-01-01')", (self.sid, kind))   # old rows are fine
        self.conn.execute("DELETE FROM students WHERE id = ?", (self.sid,)); self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM course_credits").fetchone()[0], 0)

    def test_transfer_term(self):
        sid = physdb.add_student(self.conn, university_id="U7", first_name="T", last_name="Rans", email="t@u.edu",
                                 matriculation_year=2023, research_area=None, notes=None,
                                 transfer_year=2024, transfer_term="Winter")
        row = self.conn.execute("SELECT * FROM students WHERE id = ?", (sid,)).fetchone()
        self.assertEqual((physdb.transfer_label(row), physdb.matriculation_label(row)), ("Winter 2024", "Autumn 2023"))
        self.assertIn("(transfer, Winter 2024)", physdb.show_student(self.conn, "U7"))
        plain = self.conn.execute("SELECT * FROM students WHERE university_id = 'U1001'").fetchone()
        self.assertEqual(physdb.transfer_label(plain), "")
        self.assertNotIn("transfer", physdb.show_student(self.conn, "U1001"))
        for bad in (dict(transfer_year=2024), dict(transfer_term="Winter"), dict(transfer_year=2024, transfer_term="Fall")):
            with self.assertRaises(ValueError, msg=bad):
                physdb.add_student(self.conn, university_id="U8", first_name="B", last_name="B", email="b@u.edu",
                                   matriculation_year=2023, research_area=None, notes=None, **bad)

    def test_withdrawn_students_drop_off_fsr_tables_like_graduates(self):
        fsr = physdb.add_fsr(self.conn, "Xavier", "Xx", "xx@u.edu")
        self.conn.execute("UPDATE faculty SET fsr_id = ? WHERE id = ?", (fsr, self.fid))
        physdb.assign_advisor(self.conn, "U1001", self.fid)
        listed = lambda now: [r["last_name"] for r in physdb.fsr_advisees(self.conn, fsr, now)]
        with self.assertRaises(ValueError):                                       # needs the term they left
            physdb.set_status(self.conn, "U1001", "withdrawn")
        physdb.set_status(self.conn, "U1001", "withdrawn", 2026, "Winter")
        row = self.conn.execute("SELECT * FROM students").fetchone()
        self.assertEqual((row["status"], physdb.withdrawal_label(row), physdb.graduation_label(row)), ("withdrawn", "Winter 2026", ""))
        self.assertEqual(listed(("Winter", 2026)), ["Lovelace"]); self.assertEqual(listed(("Spring", 2026)), ["Lovelace"])
        self.assertEqual(listed(("Summer", 2026)), []); self.assertEqual(listed(("Autumn", 2030)), [])
        self.assertIn("withdrew Winter 2026", physdb.show_student(self.conn, "U1001"))
        # switching to graduated moves the term to the graduation columns; back to active clears everything
        physdb.set_status(self.conn, "U1001", "graduated", 2026, "Autumn")
        row = self.conn.execute("SELECT * FROM students").fetchone()
        self.assertEqual((physdb.graduation_label(row), physdb.withdrawal_label(row)), ("Autumn 2026", ""))
        physdb.set_status(self.conn, "U1001", "active")
        row = self.conn.execute("SELECT * FROM students").fetchone()
        self.assertEqual((physdb.graduation_label(row), physdb.withdrawal_label(row)), ("", ""))
        # a withdrawn student with no recorded term (entered before terms were asked for) stays listed
        self.conn.execute("UPDATE students SET status = 'withdrawn'"); self.conn.commit()
        self.assertEqual(listed(("Autumn", 2040)), ["Lovelace"])
        # faculty keep withdrawn students too
        self.assertEqual([r["last_name"] for r in physdb.faculty_advisees(self.conn, self.fid)], ["Lovelace"])

    def test_fsr_advisees(self):
        fsr = physdb.add_fsr(self.conn, "Xavier", "Xx", "xx@u.edu")
        other = physdb.add_faculty(self.conn, "Marie", "Curie", "curie@uni.edu")
        self.conn.execute("UPDATE faculty SET fsr_id = ? WHERE id = ?", (fsr, self.fid))
        s2 = physdb.add_student(self.conn, university_id="U2", first_name="B", last_name="B", email="b@u.edu", matriculation_year=2021,
                                 research_area=None, notes=None)
        physdb.assign_advisor(self.conn, "U1001", self.fid, "co-advisor")
        physdb.assign_advisor(self.conn, "U2", other, "advisor")
        self.assertEqual([r["last_name"] for r in physdb.fsr_advisees(self.conn, fsr)], ["Lovelace"])
        self.assertNotIn("university_id", physdb.fsr_advisees(self.conn, fsr)[0])
        self.assertEqual(physdb.fsr_advisees(self.conn, fsr)[0]["advisors"], "Bohr")
        with self.assertRaises(ValueError):
            physdb.add_fsr(self.conn, "A", "B", "ab@u.edu", cnet="bad cnet")

    def test_faculty_views_only_return_their_own_students_and_safe_columns(self):
        def student(uid, last):
            return physdb.add_student(self.conn, university_id=uid, first_name="S", last_name=last, email=f"{uid}@m.o",
                                      matriculation_year=2022, research_area="optics",
                                      notes="PRIVATE ADMIN NOTE", cnet=f"c{uid}", citizenship="international")
        bohr = physdb.add_faculty(self.conn, "Niels", "Bohr", "bohr@u.edu", cnet="nbohr")
        curie = physdb.add_faculty(self.conn, "Marie", "Curie", "curie@u.edu", cnet="mcurie")
        s = {n: student(f"U{n}", f"Stu{n}") for n in range(1, 8)}
        a = physdb.assign_advisor
        a(self.conn, s[1], bohr, "advisor"); a(self.conn, s[2], bohr, "co-advisor")        # Bohr advises 1, 2
        a(self.conn, s[3], curie, "advisor"); a(self.conn, s[3], bohr, "committee")        # 3: Bohr on committee only
        a(self.conn, s[4], curie, "advisor"); a(self.conn, s[4], bohr, "committee_chair")  # 4: Bohr chairs
        a(self.conn, s[5], bohr, "advisor"); a(self.conn, s[5], bohr, "committee")         # 5: advisor AND committee rows
        a(self.conn, s[6], curie, "advisor")                                                # 6: Curie's, unrelated to Bohr
        a(self.conn, s[7], bohr, "advisor")
        self.conn.execute("UPDATE advising SET end_date = '2025-01-01' WHERE student_id = ? AND faculty_id = ?", (s[7], bohr))
        self.conn.commit()
        ids = lambda rows: sorted(r["id"] for r in rows)
        self.assertEqual(ids(physdb.faculty_advisees(self.conn, bohr)), sorted([s[1], s[2], s[5]]))     # not ended, not committee-only
        self.assertEqual(ids(physdb.faculty_committee_students(self.conn, bohr)), sorted([s[3], s[4]]))  # 5 is an advisee, not "committee only"
        roles = {r["id"]: r["committee_role"] for r in physdb.faculty_committee_students(self.conn, bohr)}
        self.assertEqual(roles, {s[3]: "committee", s[4]: "committee_chair"})
        self.assertEqual(ids(physdb.faculty_advisees(self.conn, curie)), sorted([s[3], s[4], s[6]]))
        self.assertTrue(physdb.is_advisee(self.conn, bohr, s[1]) and physdb.is_advisee(self.conn, bohr, s[2]))
        for n in (3, 4, 6, 7):                                                              # committee / other's / ended: no
            self.assertFalse(physdb.is_advisee(self.conn, bohr, s[n]), n)
        self.assertFalse(physdb.is_advisee(self.conn, bohr, 9999))
        # the committee list names the advisor(s)
        self.assertEqual({r["id"]: r["advisors"] for r in physdb.faculty_committee_students(self.conn, bohr)},
                         {s[3]: "Marie Curie", s[4]: "Marie Curie"})
        # only the safe columns come back: never University ID or the internal notes
        for row in physdb.faculty_advisees(self.conn, bohr) + physdb.faculty_committee_students(self.conn, bohr):
            self.assertNotIn("university_id", row); self.assertNotIn("notes", row)
            self.assertNotIn("PRIVATE ADMIN NOTE", str(row))
            self.assertTrue(set(physdb.FACULTY_STUDENT_COLUMNS) <= set(row))
        # funding for one term, only for the students asked about
        physdb.add_funding(self.conn, "U1", "RA", 2026, "Autumn", "NSF", 100)
        physdb.add_funding(self.conn, "U1", "TA", 2026, "Winter", "Dept", 50)
        physdb.add_funding(self.conn, "U6", "RA", 2026, "Autumn", "Other", 999)
        f = physdb.funding_for_term(self.conn, [s[1], s[2]], 2026, "Autumn")
        self.assertEqual(sorted(f), [s[1], s[2]])
        self.assertEqual([e["source"] for e in f[s[1]]], ["NSF"]); self.assertEqual(f[s[2]], [])
        self.assertEqual(physdb.funding_for_term(self.conn, [], 2026, "Autumn"), {})

    def test_cnet(self):
        sid = physdb.add_student(self.conn, university_id="U20", first_name="N", last_name="M", email="n@m.o",
                                 matriculation_year=2023, research_area=None,
                                 notes=None, cnet="  NMarie ")
        self.assertEqual(physdb._find(self.conn, "students", "nmarie")["id"], sid)     # look up by CNET
        self.assertIn("CNET nmarie", physdb.show_student(self.conn, sid))
        self.assertIn("CNET -", physdb.show_student(self.conn, "U1001"))
        for kwargs, exc in (({"cnet": "NMARIE", "email": "x@m.o", "university_id": "U21"}, sqlite3.IntegrityError),
                            ({"cnet": "bad name", "email": "y@m.o", "university_id": "U22"}, ValueError)):
            with self.assertRaises(exc):
                physdb.add_student(self.conn, first_name="N", last_name="M", matriculation_year=2023,
                                   research_area=None, notes=None, **kwargs)
        for i in (30, 31):   # blank CNETs may repeat
            physdb.add_student(self.conn, university_id=f"U{i}", first_name="N", last_name="M", email=f"{i}@m.o",
                               matriculation_year=2023, research_area=None, notes=None)

    def test_citizenship(self):
        sid = physdb.add_student(self.conn, university_id="U9", first_name="N", last_name="M",
                                 email="n@m.o", matriculation_year=2023, research_area=None, notes=None, citizenship="permanent_resident")
        self.assertIn("Citizenship: permanent resident", physdb.show_student(self.conn, sid))
        self.assertIn("Citizenship: -", physdb.show_student(self.conn, "U1001"))
        with self.assertRaises(ValueError):
            physdb.add_student(self.conn, university_id="U10", first_name="N", last_name="M",
                               email="x@m.o", matriculation_year=2023, research_area=None, notes=None, citizenship="alien")

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
                           matriculation_year=2021, research_area=None, notes=None)
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
        physdb.add_funding(conn, "U1", "supplement", 2027, "Spring")
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
