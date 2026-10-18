import os
import re
import unittest

from werkzeug.security import generate_password_hash

import physdb
import webapp


class WebTests(unittest.TestCase):
    def setUp(self):
        import tempfile, os
        self.tmp = tempfile.mkdtemp()
        path = os.path.join(self.tmp, "t.db")
        os.environ["PHYSDB_SECRET"] = "test"
        # CSRF tokens are random URL-safe text, and a page can contain two-letter strings by chance. Tests that check
        # "this short name is NOT on the page" would then fail about 1 run in 100, so tokens are constant in tests.
        from unittest import mock
        patcher = mock.patch.object(webapp.secrets, "token_urlsafe", lambda nbytes=32: "x" * 43)
        patcher.start(); self.addCleanup(patcher.stop)
        self.app = webapp.create_app(path)
        conn = physdb.connect(path)
        for name, role in (("ed", "editor"), ("vw", "viewer"), ("ad", "admin")):
            conn.execute("INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
                         (name, generate_password_hash("pw-" + name), role))
        conn.commit(); conn.close()

    def client(self, user=None):
        c = self.app.test_client()
        if user:
            r = c.post("/login", data={"username": user, "password": "pw-" + user})
            self.assertEqual(r.status_code, 302)
        return c

    def token(self, c, url="/students"):
        html = c.get(url).get_data(as_text=True)
        return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)

    def add_student(self, c, **over):
        data = dict(first_name="Ada", last_name="Lovelace", university_id="U1", email="ada@u.edu",
                    status="active", matriculation_year="2022", matriculation_term="Autumn", citizenship="citizen",
                    csrf_token=self.token(c))
        data.update(over)
        return c.post("/students/new", data=data)

    def user_row(self, name):
        conn = physdb.connect(self.app.config["DB_PATH"])
        try:
            return conn.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()
        finally:
            conn.close()

    def test_only_admin_can_manage_accounts(self):
        for who in ("ed", "vw"):
            c = self.client(who)
            t = self.token(c)
            self.assertEqual(c.get("/users").status_code, 403)
            self.assertEqual(c.get("/users/new").status_code, 403)
            r = c.post("/users/new", data=dict(username="x" * 5, role="admin",
                       password="long-enough-pw", csrf_token=t))
            self.assertEqual(r.status_code, 403)
            self.assertEqual(c.post("/users/1/delete", data={"csrf_token": t}).status_code, 403)
            self.assertNotIn("Accounts", c.get("/students").get_data(as_text=True))
        self.assertIn("Accounts", self.client("ad").get("/students").get_data(as_text=True))

    def test_admin_creates_user_who_must_change_password(self):
        ad = self.client("ad"); t = self.token(ad)
        r = ad.post("/users/new", data=dict(username="newbie", role="editor",
                    password="temp-password-1", csrf_token=t))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.user_row("newbie")["must_change"], 1)
        self.assertEqual(self.user_row("newbie")["role"], "editor")
        dup = ad.post("/users/new", data=dict(username="newbie", role="viewer",
                      password="temp-password-1", csrf_token=t))
        self.assertIn("already taken", dup.get_data(as_text=True))
        short = ad.post("/users/new", data=dict(username="other", role="viewer",
                        password="short", csrf_token=t))
        self.assertIn("at least 10", short.get_data(as_text=True))
        # new user is funneled to the password page until they change it
        c = self.app.test_client()
        c.post("/login", data={"username": "newbie", "password": "temp-password-1"})
        r = c.get("/students")
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/account"))
        t2 = self.token(c, "/account")
        bad = c.post("/account", data=dict(current_password="wrong", new_password="brand-new-pass-9",
                     confirm="brand-new-pass-9", csrf_token=t2))
        self.assertIn("not your current password", bad.get_data(as_text=True))
        mismatch = c.post("/account", data=dict(current_password="temp-password-1",
                          new_password="brand-new-pass-9", confirm="different-pass-9", csrf_token=t2))
        self.assertIn("t match", mismatch.get_data(as_text=True))
        ok = c.post("/account", data=dict(current_password="temp-password-1",
                    new_password="brand-new-pass-9", confirm="brand-new-pass-9", csrf_token=t2))
        self.assertEqual(ok.status_code, 302)
        self.assertEqual(c.get("/students").status_code, 200)   # still logged in
        self.assertEqual(self.user_row("newbie")["must_change"], 0)

    def test_viewer_can_change_own_password(self):
        c = self.client("vw"); t = self.token(c)
        r = c.post("/account", data=dict(current_password="pw-vw", new_password="viewer-new-pass",
                   confirm="viewer-new-pass", csrf_token=t))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.app.test_client().post("/login", data={
            "username": "vw", "password": "viewer-new-pass"}).status_code, 302)

    def test_reset_signs_out_and_role_change_is_immediate(self):
        victim = self.client("ed")
        ad = self.client("ad"); t = self.token(ad)
        uid = self.user_row("ed")["id"]
        ad.post(f"/users/{uid}/edit", data=dict(role="viewer", password="", csrf_token=t))
        self.assertEqual(victim.get("/students/new").status_code, 403)   # demoted at once
        ad.post(f"/users/{uid}/edit", data=dict(role="viewer", password="reset-password-1", csrf_token=t))
        self.assertEqual(victim.get("/students").status_code, 302)       # old session is dead
        self.assertEqual(self.user_row("ed")["must_change"], 1)

    def test_admin_cannot_delete_or_demote_self(self):
        ad = self.client("ad"); t = self.token(ad)
        me = self.user_row("ad")["id"]
        ad.post(f"/users/{me}/delete", data={"csrf_token": t})
        self.assertIsNotNone(self.user_row("ad"))
        self.assertEqual(ad.get(f"/users/{me}/edit").headers["Location"], "/account")
        r = ad.post(f"/users/{me}/edit", data=dict(role="viewer", csrf_token=t))
        self.assertEqual(self.user_row("ad")["role"], "admin")

    def test_delete_user_ends_their_session(self):
        victim = self.client("vw")
        ad = self.client("ad"); t = self.token(ad)
        ad.post(f"/users/{self.user_row('vw')['id']}/delete", data={"csrf_token": t})
        self.assertIsNone(self.user_row("vw"))
        self.assertEqual(victim.get("/students").status_code, 302)

    def test_old_users_table_is_migrated(self):
        import sqlite3, tempfile, os
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, "
                  "password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'viewer' "
                  "CHECK (role IN ('viewer','editor')))")
        c.execute("INSERT INTO users (username, password_hash, role) VALUES ('old','h','editor')")
        c.commit(); c.close()
        conn = physdb.connect(path)
        row = conn.execute("SELECT * FROM users").fetchone()
        self.assertEqual((row["username"], row["role"], row["must_change"]), ("old", "editor", 0))
        conn.execute("UPDATE users SET role = 'admin'")   # new constraint accepts admin

    def test_login_required_and_bad_login(self):
        c = self.app.test_client()
        self.assertEqual(c.get("/students").status_code, 302)
        self.assertEqual(c.get("/export.csv").status_code, 302)
        r = c.post("/login", data={"username": "ed", "password": "wrong"})
        self.assertIn("Incorrect username", r.get_data(as_text=True))

    def test_open_redirect_blocked(self):
        c = self.app.test_client()
        r = c.post("/login?next=//evil.com", data={"username": "ed", "password": "pw-ed"})
        self.assertEqual(r.headers["Location"], "/students")

    def test_viewer_cannot_modify(self):
        c = self.client("vw")
        self.assertEqual(c.get("/students/new").status_code, 403)
        self.assertEqual(self.add_student(c).status_code, 403)
        self.assertNotIn("Add student", c.get("/students").get_data(as_text=True))

    def test_csrf_required(self):
        c = self.client("ed")
        r = c.post("/students/new", data=dict(first_name="A"))
        self.assertEqual(r.status_code, 400)

    def test_student_lifecycle(self):
        c = self.client("ed")
        self.assertEqual(self.add_student(c).status_code, 302)
        page = c.get("/students/1").get_data(as_text=True)
        self.assertIn("Ada Lovelace", page)
        # validation + duplicate handling
        r = self.add_student(c, email="not-an-email", university_id="U2")
        self.assertIn("valid email", r.get_data(as_text=True))
        r = self.add_student(c, university_id="U9")
        self.assertIn("already has this email", r.get_data(as_text=True))
        r = self.add_student(c, email="b@u.edu")
        self.assertIn("already has this university ID", r.get_data(as_text=True))
        # edit
        t = self.token(c, "/students/1/edit")
        r = c.post("/students/1/edit", data=dict(first_name="Ada", last_name="King", university_id="U1",
                   email="ada@u.edu", status="on_leave", matriculation_year="2022", matriculation_term="Winter", citizenship="international", csrf_token=t))
        self.assertEqual(r.status_code, 302)
        self.assertIn("King", c.get("/students").get_data(as_text=True))
        # children
        c.post("/faculty/new", data=dict(first_name="Niels", last_name="Bohr", email="b@u.edu", csrf_token=t))
        for table, data in {
            "advising": dict(faculty_id="1", role="advisor", start_date="2023-01-01"),
            "milestones": dict(kind="gde_classical_mechanics", date="2024-05-01", outcome="passed"),
            "funding": dict(kind="TA", term_year="2026", term_season="Autumn", poetaf_coa="1234567-ABC", amount="12000",
                           notes="Covers <i>half</i> of the term\nrenewal pending"),
        }.items():
            r = c.post(f"/students/1/{table}/new", data={**data, "csrf_token": t})
            self.assertEqual(r.status_code, 302, table)
        page = c.get("/students/1").get_data(as_text=True)
        for needle in ("Bohr", "GDE Classical Mechanics", "$12,000", "Autumn 2026", "1234567-ABC",
                       "Covers &lt;i&gt;half&lt;/i&gt; of the term", "renewal pending"):
            self.assertIn(needle, page)
        r = c.post("/students/1/advising/1/edit", data=dict(faculty_id="1", role="advisor",
                   start_date="2023-01-01", end_date="2022-01-01", csrf_token=t))
        self.assertIn("before the start date", r.get_data(as_text=True))
        self.assertEqual(c.post("/students/1/funding/1/delete", data={"csrf_token": t}).status_code, 302)
        self.assertNotIn("$12,000", c.get("/students/1").get_data(as_text=True))
        # cannot delete advisor in use; deleting the student cascades
        c.post("/faculty/1/delete", data={"csrf_token": t})
        self.assertEqual(c.get("/faculty/1").status_code, 200)
        c.post("/students/1/delete", data={"csrf_token": t})
        self.assertEqual(c.get("/students/1").status_code, 404)

    def test_funding_term_menu_sorting_and_account_field(self):
        c = self.client("ed"); self.add_student(c); t = self.token(c)
        form = c.get("/students/1/funding/new").get_data(as_text=True)
        self.assertIn("POETAF/COA", form)
        self.assertIn("&mdash; choose &mdash;", form)
        for season in ("Autumn", "Winter", "Spring", "Summer"):
            self.assertIn(f'<option value="{season}"', form)
        base = dict(kind="RA", csrf_token=t)
        # must pick both a year and a term; values outside the menu are rejected
        for bad in (dict(term_year="", term_season="Autumn"), dict(term_year="2026", term_season=""),
                    dict(term_year="2026", term_season="Fall"), dict(term_year="1850", term_season="Spring"),
                    dict(term_year="2026", term_season="Spring", poetaf_coa="x" * 61)):
            r = c.post("/students/1/funding/new", data={**base, **bad})
            self.assertEqual(r.status_code, 200, bad)
            self.assertIn("Please fix", r.get_data(as_text=True))
        for year, season in [(2025, "Summer"), (2026, "Autumn"), (2026, "Winter"), (2025, "Autumn"), (2026, "Summer")]:
            r = c.post("/students/1/funding/new", data={**base, "term_year": year, "term_season": season,
                       "poetaf_coa": "<b>9</b>" if year == 2026 and season == "Winter" else ""})
            self.assertEqual(r.status_code, 302)
        page = c.get("/students/1").get_data(as_text=True)
        order = [page.index(x) for x in ("Autumn 2026", "Summer 2026", "Winter 2026", "Autumn 2025", "Summer 2025")]
        self.assertEqual(order, sorted(order))   # newest first, chronological within a year
        self.assertIn("&lt;b&gt;9", page)        # account number is escaped

    def test_student_filter_advisor_vs_committee(self):
        c = self.client("ed"); t = self.token(c)
        for i in range(1, 6):
            self.add_student(c, university_id=f"U{i}", email=f"s{i}@u.edu", last_name=f"Stu{i}")
        for name in ("Bohr", "Curie"):
            c.post("/faculty/new", data=dict(first_name="X", last_name=name, email=f"{name}@u.edu", csrf_token=t))
        def assign(sid, fid, role, end=""):
            c.post(f"/students/{sid}/advising/new", data=dict(faculty_id=fid, role=role,
                   start_date="2024-01-01", end_date=end, csrf_token=t))
        assign(1, 1, "advisor"); assign(2, 1, "committee"); assign(3, 2, "advisor")
        assign(3, 1, "committee_chair"); assign(4, 1, "advisor"); assign(4, 1, "committee")
        assign(5, 1, "advisor", end="2025-01-01")
        def who(qs):
            html = c.get("/students?" + qs).get_data(as_text=True)
            return sorted(set(__import__("re").findall(r"Stu\d", html)))
        self.assertEqual(who("faculty=1&role=advisor"), ["Stu1", "Stu4"])
        self.assertEqual(who("faculty=1&role=committee"), ["Stu2", "Stu3"])
        self.assertEqual(who("faculty=1&role=any"), ["Stu1", "Stu2", "Stu3", "Stu4"])
        self.assertEqual(who("faculty=1"), ["Stu1", "Stu2", "Stu3", "Stu4"])
        self.assertEqual(who("faculty=2&role=committee"), [])
        self.assertEqual(who("role=advisor"), ["Stu1", "Stu3", "Stu4"])        # anyone with a current advisor
        self.assertEqual(who("faculty=1&role=bogus"), ["Stu1", "Stu2", "Stu3", "Stu4"])
        self.assertEqual(who("faculty=abc"), ["Stu1", "Stu2", "Stu3", "Stu4", "Stu5"])

    def test_students_list_clock_column(self):
        from unittest import mock
        c = self.client("ed")
        for uid, term, year, status in [("U1", "Autumn", "2026", "active"), ("U2", "Autumn", "2025", "active"),
                                        ("U3", "Spring", "2024", "active"), ("U4", "Autumn", "2020", "graduated")]:
            self.add_student(c, university_id=uid, email=f"{uid}@u.edu", last_name=f"Stu{uid}",
                             matriculation_term=term, matriculation_year=year, status=status)
        with mock.patch.object(physdb, "current_term", return_value=("Autumn", 2026)):
            html = c.get("/students").get_data(as_text=True)
        self.assertLess(html.index("sort=matriculation"), html.index("sort=clock"))
        self.assertLess(html.index("sort=clock"), html.index("sort=advisor"))
        self.assertIn("current term: Autumn 2026", html)
        import re
        cells = {m.group(1): m.group(2) for m in re.finditer(
            r"<tr><td><a [^>]*>(Stu\w+),[^<]*</a></td>.*?<td>([^<]*)</td><td>[^<]*</td><td>[^<]*</td></tr>", html, re.S)}
        self.assertEqual(cells, {"StuU1": "0.0", "StuU2": "1.0", "StuU3": "2.5", "StuU4": "—"})

    def test_funding_report_page(self):
        from unittest import mock
        c = self.client("ed"); t = self.token(c)
        for i, (last, status) in enumerate((("Aa", "active"), ("Bb", "active"), ("Cc", "active"),
                                            ("Dd", "on_leave"), ("Ee", "graduated")), 1):
            self.add_student(c, university_id=f"U{i}", email=f"s{i}@u.edu", first_name="Stu", last_name=last,
                             status=status, cnet=f"cnet{i}")
        def fund(sid, kind, year, season, **extra):
            r = c.post(f"/students/{sid}/funding/new", data=dict(kind=kind, term_year=year, term_season=season,
                       csrf_token=t, **extra))
            self.assertEqual(r.status_code, 302)
        fund(1, "RA", 2026, "Autumn", source="NSF", poetaf_coa="COA-9", amount="3000", notes="<b>hi</b>")
        fund(1, "supplement", 2026, "Autumn", amount="500")
        fund(2, "TA", 2026, "Winter", amount="100")                 # a different term
        fund(4, "RA", 2026, "Autumn", amount="999")                 # on leave -> not included
        def page(qs=""):
            with mock.patch.object(physdb, "current_term", return_value=("Autumn", 2026)):
                return c.get("/reports/funding?" + qs).get_data(as_text=True)
        html = page()                                               # defaults to the current term
        self.assertIn("Funding for Autumn 2026", html)
        self.assertIn("<option selected>Autumn</option>", html); self.assertIn("<option selected>2026</option>", html)
        who = lambda h: re.findall(r"/students/(\d+)\">(\w+),", h)
        self.assertEqual(who(html), [("1", "Aa"), ("1", "Aa"), ("2", "Bb"), ("3", "Cc")])   # 2 rows for Aa; unfunded kept
        self.assertIn("3 active students", html); self.assertIn("1 with funding recorded", html)
        self.assertIn("2 without", html); self.assertIn("total $3,500", html)
        for needle in ("NSF", "COA-9", "$3,000", "Supplement", "$500", "&lt;b&gt;hi&lt;/b&gt;"):
            self.assertIn(needle, html)
        self.assertNotIn("$999", html); self.assertNotIn("Dd, Stu", html)                     # on leave excluded
        self.assertNotIn("cnet1", html)                                                       # CNET is not on screen
        self.assertNotIn("<b>hi</b>", html)
        row_cc = re.search(r"/students/3\">Cc.*?</tr>", html, re.S).group(0)
        cells = re.findall(r"<td[^>]*>([^<]*)</td>", row_cc)      # the name cell holds a link, so: CNET, advisor,
        self.assertEqual(len(cells), 7)                           # then type, source, POETAF/COA, amount, notes
        self.assertEqual(cells[0], "4.0")                         # Clock (matriculated Autumn 2022, now Autumn 2026)
        self.assertEqual([x for x in cells[2:] if x], [])         # every funding field is empty
        # other terms, sorting, and bad input
        self.assertEqual(who(page("season=Winter&year=2026")), [("1", "Aa"), ("2", "Bb"), ("3", "Cc")])
        self.assertIn("total $100", page("season=Winter&year=2026"))
        self.assertEqual(who(page("sort=amount&dir=desc")), [("1", "Aa"), ("1", "Aa"), ("2", "Bb"), ("3", "Cc")])
        self.assertEqual(who(page("sort=name&dir=desc")), [("3", "Cc"), ("2", "Bb"), ("1", "Aa"), ("1", "Aa")])
        self.assertIn("Funding for Autumn 2026", page("year=abc&season=Fall"))
        self.assertIn("Funding for Autumn 2026", page("year=1800"))
        # tabs and a visible link from the Reports page
        self.assertIn("Funding by term", c.get("/reports").get_data(as_text=True))
        self.assertIn("Milestone Check", html)
        # CSV: the screen drops University ID, but the file carries the full student record
        c.post("/faculty/new", data=dict(first_name="Niels", last_name="Bohr", email="b@u.edu", csrf_token=t))
        c.post("/students/1/advising/new", data=dict(faculty_id=1, role="advisor", start_date="2024-01-01", csrf_token=t))
        with mock.patch.object(physdb, "current_term", return_value=("Autumn", 2026)):
            resp = c.get("/reports/funding.csv?season=Autumn&year=2026")
        self.assertEqual(resp.mimetype, "text/csv")
        self.assertIn("funding_Autumn_2026.csv", resp.headers["Content-Disposition"])
        import csv, io
        parsed = list(csv.reader(io.StringIO(resp.get_data(as_text=True))))
        header, body = parsed[0], parsed[1:]
        self.assertEqual(header, [
            "Last name", "First name", "University ID", "CNET", "Email", "Citizenship", "Status",
            "Matriculation term", "Matriculation year", "Clock (years)", "Expected graduation year",
            "Research area", "Advisor", "Student notes",
            "Funding term", "Funding type", "Source", "POETAF/COA", "Amount", "Funding notes"])
        self.assertEqual(len(body), 4)                                   # same rows as the screen
        rows = [dict(zip(header, r)) for r in body]
        aa = [r for r in rows if r["Last name"] == "Aa" and r["Funding type"] == "RA"][0]
        self.assertEqual((aa["First name"], aa["University ID"], aa["CNET"], aa["Email"]),
                         ("Stu", "U1", "cnet1", "s1@u.edu"))
        self.assertEqual((aa["Citizenship"], aa["Status"], aa["Matriculation term"], aa["Matriculation year"]),
                         ("Citizen", "Active", "Autumn", "2022"))
        self.assertEqual(aa["Clock (years)"], "4.0")
        self.assertEqual(aa["Advisor"], "Niels Bohr")                    # full name in the file
        self.assertEqual((aa["Funding term"], aa["Source"], aa["POETAF/COA"], aa["Amount"], aa["Funding notes"]),
                         ("Autumn 2026", "NSF", "COA-9", "3000.0", "<b>hi</b>"))
        cc = [r for r in rows if r["Last name"] == "Cc"][0]              # unfunded: student info, blank funding
        self.assertEqual((cc["University ID"], cc["CNET"], cc["Email"], cc["Citizenship"]),
                         ("U3", "cnet3", "s3@u.edu", "Citizen"))
        self.assertEqual([cc[k] for k in ("Funding type", "Source", "POETAF/COA", "Amount", "Funding notes")],
                         ["", "", "", "", ""])
        self.assertEqual(cc["Funding term"], "Autumn 2026")
        self.assertNotIn("Dd", [r["Last name"] for r in rows])           # on leave: still excluded

    def test_funding_report_edit_and_add_buttons(self):
        from urllib.parse import urlparse, parse_qs, unquote
        from unittest import mock
        c = self.client("ed"); t = self.token(c)
        for i, last in enumerate(("Aa", "Bb"), 1):
            self.add_student(c, university_id=f"U{i}", email=f"s{i}@u.edu", first_name="Stu", last_name=last)
        c.post("/students/1/funding/new", data=dict(kind="RA", term_year=2026, term_season="Autumn",
               amount="100", csrf_token=t))                       # funding id 1 (Aa, Autumn 2026)
        c.post("/students/1/funding/new", data=dict(kind="TA", term_year=2026, term_season="Winter",
               amount="50", csrf_token=t))                        # id 2: another term, must not be edited
        report = "/reports/funding?season=Autumn&year=2026&sort=amount&dir=desc"
        html = c.get(report).get_data(as_text=True)
        buttons = re.findall(r'class="btn[^"]*" href="([^"]+)"[^>]*>(Edit|\+)</a>', html)
        # Aa has one entry (Edit, plus an Add for a second one); Bb has none (Add)
        self.assertEqual([b for _, b in buttons], ["Edit", "+", "+"])
        edit_href, aa_add_href, add_href = (h.replace("&amp;", "&") for h, _ in buttons)   # what a browser would follow
        self.assertTrue(aa_add_href.startswith("/students/1/funding/new?"))
        self.assertEqual(parse_qs(urlparse(aa_add_href).query)["term_season"], ["Autumn"])
        self.assertTrue(edit_href.startswith("/students/1/funding/1/edit?"))      # the Autumn entry, not Winter's
        self.assertTrue(add_href.startswith("/students/2/funding/new?"))
        self.assertEqual(parse_qs(urlparse(add_href).query)["term_year"], ["2026"])
        self.assertEqual(parse_qs(urlparse(add_href).query)["term_season"], ["Autumn"])
        for href in (edit_href, add_href):                                       # both remember the report
            self.assertEqual(parse_qs(urlparse(href).query)["next"], [report])
        # Add: the form opens with the report's term filled in, and Cancel goes back to the report
        form = c.get(add_href).get_data(as_text=True)
        self.assertIn('<option value="2026" selected>2026</option>', form)
        self.assertIn('<option value="Autumn" selected>Autumn</option>', form)
        self.assertIn(f'href="{report.replace("&", "&amp;")}">Cancel', form)
        # saving returns to the same report (same term and sort) and the entry is now there
        r = c.post(add_href, data=dict(kind="supplement", term_year=2026, term_season="Autumn",
                   amount="25", csrf_token=t))
        self.assertEqual((r.status_code, r.headers["Location"]), (302, report))
        page = c.get(report).get_data(as_text=True)
        self.assertEqual([b for _, b in re.findall(r'class="btn[^"]*" href="([^"]+)"[^>]*>(Edit|\+)</a>', page)],
                         ["Edit", "+", "Edit", "+"])                              # both students: Edit + a "+" for another
        self.assertIn("$25", page)
        # Edit: opens the entry, saves, and returns to the report
        self.assertIn('value="100.0"', c.get(edit_href).get_data(as_text=True))
        r = c.post(edit_href, data=dict(kind="RA", term_year=2026, term_season="Autumn", amount="150",
                   notes="raised", csrf_token=t))
        self.assertEqual((r.status_code, r.headers["Location"]), (302, report))
        self.assertIn("$150", c.get(report).get_data(as_text=True))
        # a validation error keeps you on the form, still pointing back at the report
        bad = c.post(edit_href, data=dict(kind="RA", term_year=2026, term_season="", csrf_token=t))
        self.assertEqual(bad.status_code, 200)
        self.assertIn("Cancel", bad.get_data(as_text=True))
        self.assertEqual(c.post(edit_href, data=dict(kind="RA", term_year=2026, term_season="Autumn",
                         csrf_token=t)).headers["Location"], report)
        # no ?next= -> behaves as before (back to the student page)
        r = c.post("/students/2/funding/new", data=dict(kind="TA", term_year=2026, term_season="Spring", csrf_token=t))
        self.assertEqual(r.headers["Location"], "/students/2")

    def test_next_parameter_cannot_redirect_off_site(self):
        c = self.client("ed"); t = self.token(c); self.add_student(c)
        for evil in ("//evil.com/x", "http://evil.com", "https://evil.com/x", "\\\\evil.com", "evil.com", "/ok\\evil"):
            r = c.post("/students/1/funding/new", data=dict(kind="TA", term_year=2026, term_season="Autumn",
                       csrf_token=t), query_string={"next": evil})
            self.assertEqual((r.status_code, r.headers["Location"]), (302, "/students/1"), evil)
        r = c.post("/students/1/funding/new", data=dict(kind="TA", term_year=2026, term_season="Winter", csrf_token=t),
                   query_string={"next": "/reports/funding?year=2026&season=Winter"})
        self.assertEqual(r.headers["Location"], "/reports/funding?year=2026&season=Winter")

    def test_funding_report_source_column_is_wide_enough(self):
        c = self.client("ed"); t = self.token(c); self.add_student(c)
        html = c.get("/reports/funding").get_data(as_text=True)
        self.assertIn(".source{min-width:20ch}", html)                       # room for ~20 characters
        self.assertIn(".wide td.actions{white-space:nowrap", html)           # Edit / Clear / Copy / + never wrap
        self.assertRegex(html, r'<th class="source"[^>]*>\s*<a [^>]*>Source')   # header and cells share the class
        c.post("/students/1/funding/new", data=dict(kind="RA", term_year=2026, term_season="Autumn",
               source="Department of Energy", csrf_token=t))
        from unittest import mock
        with mock.patch.object(physdb, "current_term", return_value=("Autumn", 2026)):
            html = c.get("/reports/funding").get_data(as_text=True)
        self.assertIn('<td class="source">Department of Energy</td>', html)

    def test_funding_report_shows_full_poetaf_and_no_university_id(self):
        import csv, io
        from unittest import mock
        c = self.client("ed"); t = self.token(c)
        self.add_student(c, university_id="U777", research_area="=SUM(1)", notes="@cmd", cnet="abc")
        longest = "12-3456789-ABCDEF-0001-2026-XYZ-9999-0000-000-11111-22222"       # 56 characters
        c.post("/students/1/funding/new", data=dict(kind="RA", term_year=2026, term_season="Autumn",
               poetaf_coa=longest, csrf_token=t))
        with mock.patch.object(physdb, "current_term", return_value=("Autumn", 2026)):
            html = c.get("/reports/funding").get_data(as_text=True)
            data = c.get("/reports/funding.csv").get_data(as_text=True)
        self.assertIn(f'<td style="white-space:nowrap">{longest}</td>', html)           # whole value, never wrapped
        self.assertIn(f'<td style="white-space:nowrap">{longest}</td>', c.get("/students/1").get_data(as_text=True))
        self.assertNotIn("University ID", html); self.assertNotIn("U777", html)        # not on screen...
        self.assertIn("U777", data); self.assertIn(longest, data)                       # ...but in the CSV
        row = list(csv.DictReader(io.StringIO(data)))[0]
        self.assertEqual(row["POETAF/COA"], longest)
        # spreadsheet formulas in any column are neutralized
        self.assertEqual(row["Research area"], "'=SUM(1)"); self.assertEqual(row["Student notes"], "'@cmd")
        self.assertEqual(c.get("/reports/funding?sort=university_id").status_code, 200)  # old bookmark still works

    def test_funding_report_shows_clock_instead_of_cnet(self):
        from unittest import mock
        c = self.client("ed")
        for uid, last, term, year in (("U1", "Aa", "Autumn", "2022"), ("U2", "Bb", "Spring", "2025"),
                                      ("U3", "Cc", "Winter", "2027"), ("U4", "Dd", "Autumn", "2026")):
            self.add_student(c, university_id=uid, email=f"{uid}@u.edu", last_name=f"Rep_{last}", cnet=f"net{last}",
                             matriculation_term=term, matriculation_year=year)
        def get(qs=""):
            with mock.patch.object(physdb, "current_term", return_value=("Autumn", 2026)):
                return c.get("/reports/funding?" + qs).get_data(as_text=True)
        html = get()
        self.assertIn("sort=clock", html); self.assertNotIn("sort=cnet", html)
        self.assertNotIn(">CNET", html); self.assertNotIn("netaa", html)         # CNET is off the screen
        names = lambda h: re.findall(r">(Rep_\w+), \w+</a>", h)
        clocks = lambda h: re.findall(r"Rep_\w+, Ada</a></td>\s*<td>([^<]*)</td>", h)
        self.assertEqual(names(html), ["Rep_Aa", "Rep_Bb", "Rep_Cc", "Rep_Dd"])
        self.assertEqual(clocks(html), ["4.0", "1.5", "—", "0.0"])                  # Cc hasn't matriculated yet
        self.assertEqual(names(get("sort=clock")), ["Rep_Dd", "Rep_Bb", "Rep_Aa", "Rep_Cc"])             # blank last
        self.assertEqual(names(get("sort=clock&dir=desc")), ["Rep_Aa", "Rep_Bb", "Rep_Dd", "Rep_Cc"])
        self.assertEqual(c.get("/reports/funding?sort=cnet").status_code, 200)    # an old bookmark still works
        # ...but the CSV keeps the CNET (and the Clock) alongside everything else
        with mock.patch.object(physdb, "current_term", return_value=("Autumn", 2026)):
            data = c.get("/reports/funding.csv").get_data(as_text=True)
        self.assertIn("CNET", data.splitlines()[0]); self.assertIn("netaa", data); self.assertIn("Clock (years)", data)

    def _funding_setup(self):
        """Students Aa (RA in Autumn 2026, supplement too), Bb (nothing), Cc (nothing now; RA in Summer 2026)."""
        c = self.client("ed"); t = self.token(c)
        for i, last in enumerate(("Aa", "Bb", "Cc"), 1):
            self.add_student(c, university_id=f"U{i}", email=f"s{i}@u.edu", first_name="Stu", last_name=last)
        def fund(sid, kind, year, season, **extra):
            self.assertEqual(c.post(f"/students/{sid}/funding/new", data=dict(
                kind=kind, term_year=year, term_season=season, csrf_token=t, **extra)).status_code, 302)
        fund(1, "RA", 2026, "Autumn", source="Now", amount="100")                        # funding id 1
        fund(1, "supplement", 2026, "Autumn", amount="5")                                # funding id 2
        fund(1, "TA", 2026, "Summer", source="Before", poetaf_coa="OLD-1", amount="300", notes="from summer")
        fund(3, "RA", 2026, "Summer", source="NSF", poetaf_coa="COA-77", amount="2500", notes="renewed")
        return c, t

    def _report_forms(self, c, report):
        from unittest import mock
        with mock.patch.object(physdb, "current_term", return_value=("Autumn", 2026)):
            html = c.get(report).get_data(as_text=True)
        out = {}
        for m in re.finditer(r"<tr><td[^>]*><a href=\"/students/(\d+)\">.*?</tr>", html, re.S):
            sid, row = m.group(1), m.group(0)
            out.setdefault(sid, []).append(row)
        return html, out

    @staticmethod
    def confirm_prompts(html):
        """Every confirm() message a browser would actually run, decoded from the page's onsubmit attributes."""
        import json
        from html.parser import HTMLParser
        found = []
        class P(HTMLParser):
            def handle_starttag(self, tag, attrs):
                js = dict(attrs).get("onsubmit")
                if tag == "form" and js is not None:
                    assert js.startswith("return confirm(") and js.endswith(")"), f"broken onsubmit: {js!r}"
                    found.append(json.loads(js[len("return confirm("):-1]))      # must be one complete JSON string
        P().feed(html)
        return found

    def test_confirmation_prompts_are_intact_and_complete(self):
        """Regression: the message used to be cut off by its own quotes, so no confirmation ever appeared."""
        c = self.client("ad"); t = self.token(c)
        self.add_student(c, first_name='Bob "Rob"', last_name="O'Brien <b>&</b>")
        c.post("/faculty/new", data=dict(first_name="N", last_name="Bohr", email="b@u.edu", csrf_token=t))
        c.post("/students/1/advising/new", data=dict(faculty_id=1, role="advisor", start_date="2024-01-01", csrf_token=t))
        c.post("/students/1/milestones/new", data=dict(kind="other", date="2024-01-01", outcome="pending", csrf_token=t))
        c.post("/students/1/funding/new", data=dict(kind="RA", term_year=2026, term_season="Autumn", amount="5", csrf_token=t))
        student = self.confirm_prompts(c.get("/students/1").get_data(as_text=True))
        self.assertEqual(len(student), 4)                                  # student, advisor, milestone, funding
        self.assertIn('Permanently delete Bob "Rob" O\'Brien <b>&</b> and all of their records?', student)
        self.assertEqual(student.count("Delete this entry?"), 3)
        self.assertEqual(self.confirm_prompts(c.get("/faculty/1").get_data(as_text=True)), ["Delete this faculty member?"])
        users = self.confirm_prompts(c.get("/users").get_data(as_text=True))
        self.assertTrue(users and all(u.startswith("Delete the account ") for u in users))
        from unittest import mock
        with mock.patch.object(physdb, "current_term", return_value=("Autumn", 2026)):
            report = self.confirm_prompts(c.get("/reports/funding").get_data(as_text=True))
        self.assertEqual(report, ['Delete this funding entry (RA, $5) for O\'Brien <b>&</b>, Bob "Rob" in Autumn 2026? '
                                  'This cannot be undone.'])

    def test_funding_report_row_buttons(self):
        c, t = self._funding_setup()
        html, rows = self._report_forms(c, "/reports/funding?season=Autumn&year=2026")
        labels = lambda row: re.findall(r">(Edit|\+|Copy|Clear)</(?:a|button)>", row)
        self.assertEqual([labels(r) for r in rows["1"]],                                         # funded rows; the student's
                         [["Edit", "Clear", "Copy", "+"]] * 2)                                     # every row ends with +
        self.assertEqual(labels(rows["2"][0]), ["Copy", "+"])                                    # empty rows: the "+" is
        self.assertEqual(labels(rows["3"][0]), ["Copy", "+"])                                    # last, as on funded rows
        # every form button (Clear) carries a real CSRF token; Copy and + are links, not forms
        for rws in rows.values():
            for row in rws:
                tokens = re.findall(r'name="csrf_token" value="([^"]*)"', row)
                self.assertEqual(len(tokens), row.count("<form"), row)
                self.assertTrue(all(tokens), row)
        # Clear warns before deleting; Copy opens the chooser window instead of asking up front
        text = html.replace("&#34;", '"').replace("&#39;", "'")
        self.assertIn("Delete this funding entry (RA, $100) for Aa, Stu in Autumn 2026? This cannot be undone.", text)
        self.assertNotIn("Replace the", text)
        for rws in rows.values():
            for row in rws:
                self.assertRegex(row, r'<a class="btn secondary small" data-copy href="/students/\d+/funding/copy\?[^"]*"')
        self.assertIn('<dialog id="copy-dialog"', html)

    def test_copy_prev_on_an_empty_row_creates_matching_entries(self):
        c, t = self._funding_setup()
        report = "/reports/funding?season=Autumn&year=2026&sort=amount&dir=desc"
        r = c.post("/students/3/funding/copy", data=dict(year=2026, season="Autumn", next=report, csrf_token=t))
        self.assertEqual((r.status_code, r.headers["Location"]), (302, report))                 # back to the same view
        conn = physdb.connect(self.app.config["DB_PATH"])
        got = [tuple(x) for x in conn.execute("SELECT kind, source, poetaf_coa, amount, notes, term_year, term_season "
                                              "FROM funding WHERE student_id = 3 ORDER BY id")]
        conn.close()
        self.assertEqual(got, [("RA", "NSF", "COA-77", 2500.0, "renewed", 2026, "Summer"),
                               ("RA", "NSF", "COA-77", 2500.0, "renewed", 2026, "Autumn")])
        html, rows = self._report_forms(c, report)
        self.assertIn("Copied 1 funding entry from Summer 2026 to Autumn 2026 for Cc, Stu.", html.replace("&#39;", "'"))
        self.assertIn("COA-77", rows["3"][0]); self.assertIn(">Edit<", rows["3"][0])            # row now has an entry

    def test_copy_prev_on_a_funded_row_replaces_this_terms_entries(self):
        c, t = self._funding_setup()
        r = c.post("/students/1/funding/copy", data=dict(year=2026, season="Autumn", csrf_token=t))
        self.assertEqual(r.headers["Location"], "/students/1")                                  # no next -> student page
        conn = physdb.connect(self.app.config["DB_PATH"])
        got = [tuple(x) for x in conn.execute("SELECT kind, source, poetaf_coa, amount, notes FROM funding "
                                              "WHERE student_id = 1 AND term_year = 2026 AND term_season = 'Autumn'")]
        summer = conn.execute("SELECT count(*) FROM funding WHERE student_id = 1 AND term_season = 'Summer'").fetchone()[0]
        conn.close()
        self.assertEqual(got, [("TA", "Before", "OLD-1", 300.0, "from summer")])               # RA + supplement are gone
        self.assertEqual(summer, 1)
        # a student whose previous term is empty: friendly message, and nothing is lost
        c.post("/students/2/funding/new", data=dict(kind="TA", term_year=2026, term_season="Autumn", amount="1", csrf_token=t))
        r = c.post("/students/2/funding/copy", data=dict(year=2026, season="Autumn", next="/reports/funding", csrf_token=t),
                   follow_redirects=True)
        self.assertIn("no funding recorded for Summer 2026, so nothing was copied", r.get_data(as_text=True))
        conn = physdb.connect(self.app.config["DB_PATH"])
        self.assertEqual(conn.execute("SELECT count(*) FROM funding WHERE student_id = 2 AND term_season = 'Autumn'").fetchone()[0], 1)
        conn.close()

    def test_clear_deletes_only_that_entry_and_returns_to_the_report(self):
        c, t = self._funding_setup()
        report = "/reports/funding?season=Autumn&year=2026"
        html, rows = self._report_forms(c, report)
        clear = [re.search(r'action="([^"]+)"[^>]*>\s*<input type="hidden" name="csrf_token" value="([^"]+)"\s*>'
                           r'\s*<button class="btn danger small">Clear', row, re.S) for row in rows["1"]]
        self.assertTrue(all(clear))
        first = clear[0].group(1).replace("&amp;", "&")
        self.assertTrue(first.startswith("/students/1/funding/1/delete?next="))
        r = c.post(first, data={"csrf_token": clear[0].group(2)})
        self.assertEqual((r.status_code, r.headers["Location"]), (302, report))
        html, rows = self._report_forms(c, report)
        self.assertEqual(len(rows["1"]), 1)                                                     # the supplement is still there
        self.assertIn("Supplement", rows["1"][0]); self.assertNotIn(">Now<", html)
        # clearing the last one leaves an empty row offering Copy / + again
        second = re.search(r'action="([^"]+/delete[^"]*)"', rows["1"][0]).group(1).replace("&amp;", "&")
        c.post(second, data={"csrf_token": t})
        html, rows = self._report_forms(c, report)
        self.assertEqual(re.findall(r">(Edit|\+|Copy|Clear)</(?:a|button)>", rows["1"][0]), ["Copy", "+"])
        # other terms are untouched
        self.assertIn("OLD-1", c.get("/reports/funding?season=Summer&year=2026").get_data(as_text=True))

    def _copy_dialog(self, c, sid=1, to="year=2026&season=Autumn", frm="", nxt="/reports/funding", fragment=True):
        from urllib.parse import quote
        qs = f"/students/{sid}/funding/copy?{to}" + (f"&{frm}" if frm else "") \
             + (f"&next={quote(nxt, safe='')}" if nxt else "") + ("&fragment=1" if fragment else "")
        return c.get(qs)

    @staticmethod
    def _steps(html):
        """(label, href) for the dialog's Prev / Next links, hrefs exactly as a browser would follow them."""
        return {m.group(2).strip(): m.group(1).replace("&amp;", "&") for m in re.finditer(
            r'<a class="btn secondary small" data-step href="([^"]+)"[^>]*>(&lsaquo; Prev|Next &rsaquo;)</a>', html)}

    def test_copy_dialog_starts_on_the_previous_term_and_shows_its_funding(self):
        c, t = self._funding_setup()
        r = self._copy_dialog(c)
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertNotIn("<html", html)                                      # just the dialog body, for the pop-up
        self.assertIn("Copy funding into Autumn 2026", html); self.assertIn("Aa, Stu", html)
        self.assertIn('<strong class="copy-source">Summer 2026</strong>', html)       # default: the previous term
        for needle in ("Before", "OLD-1", "$300", "from summer"):                       # the source term's parameters
            self.assertIn(needle, html)
        self.assertIn("Now recorded for Autumn 2026", html)                             # and what is there now
        for needle in ("Now", "$100", "Supplement", "$5"):
            self.assertIn(needle, html)
        self.assertIn("will <strong>replace</strong> the 2 entries above with the 1 from Summer 2026", " ".join(html.split()))
        self.assertIn("Copy Summer 2026 to Autumn 2026</button>", html)
        self.assertNotRegex(html, r"<button class=\"btn\" disabled")
        # the same address without ?fragment=1 is a normal page (works without JavaScript)
        page = self._copy_dialog(c, fragment=False).get_data(as_text=True)
        self.assertIn("<html", page); self.assertIn("Copy funding into Autumn 2026", page)
        # a student with nothing in the target term: no replace warning
        html3 = self._copy_dialog(c, sid=3).get_data(as_text=True)
        self.assertIn("RA", html3); self.assertIn("COA-77", html3); self.assertIn("renewed", html3)
        self.assertIn("Nothing is recorded yet.", html3); self.assertNotIn("will <strong>replace</strong>", html3)
        # a student whose previous term is empty: copy is disabled and says why
        html2 = self._copy_dialog(c, sid=2).get_data(as_text=True)
        self.assertIn("No funding is recorded for Summer 2026.", html2)
        self.assertRegex(html2, r'<button class="btn" disabled>Copy Summer 2026 to Autumn 2026')

    def test_copy_dialog_steps_through_terms(self):
        from urllib.parse import urlparse, parse_qs
        c, t = self._funding_setup()
        c.post("/students/3/funding/new", data=dict(kind="TA", term_year=2026, term_season="Spring", source="Older",
               notes="<b>older</b>", amount="111", csrf_token=t))
        base = "year=2026&season=Autumn"
        html = self._copy_dialog(c, sid=3, nxt="/reports/funding?season=Autumn&year=2026").get_data(as_text=True)
        steps = self._steps(html)
        self.assertEqual(set(steps), {"&lsaquo; Prev", "Next &rsaquo;"})
        q = parse_qs(urlparse(steps["&lsaquo; Prev"]).query)
        self.assertEqual((q["from_season"], q["from_year"]), (["Spring"], ["2026"]))                 # Summer -> Spring
        self.assertEqual((q["season"], q["year"]), (["Autumn"], ["2026"]))                           # target unchanged
        self.assertEqual(q["next"], ["/reports/funding?season=Autumn&year=2026"])                    # return address kept
        q = parse_qs(urlparse(steps["Next &rsaquo;"]).query)
        self.assertEqual((q["from_season"], q["from_year"]), (["Autumn"], ["2026"]))                 # Summer -> Autumn
        # follow Prev: the dialog now shows Spring 2026's parameters (escaped), still copying into Autumn 2026
        html = c.get(steps["&lsaquo; Prev"] + "&fragment=1").get_data(as_text=True)
        self.assertIn('<strong class="copy-source">Spring 2026</strong>', html)
        self.assertIn("Older", html); self.assertIn("$111", html); self.assertIn("&lt;b&gt;older&lt;/b&gt;", html)
        self.assertNotIn("<b>older</b>", html); self.assertNotIn("COA-77", html)                    # not Summer's entry
        self.assertIn("Copy Spring 2026 to Autumn 2026</button>", html)
        self.assertIn("Now recorded for Autumn 2026", html)
        # stepping again, to a term with nothing: says so, and the copy button is disabled
        winter = self._steps(html)["&lsaquo; Prev"]
        html = c.get(winter + "&fragment=1").get_data(as_text=True)
        self.assertIn("Winter 2026", html); self.assertIn("No funding is recorded for Winter 2026.", html)
        self.assertRegex(html, r'<button class="btn" disabled>Copy Winter 2026')
        # stepping onto the target term itself: copying is disabled with an explanation
        onto = self._copy_dialog(c, sid=3, frm="from_year=2026&from_season=Autumn").get_data(as_text=True)
        self.assertIn("That is the term you are copying <em>into</em>", onto)
        self.assertRegex(onto, r'<button class="btn" disabled>Copy Autumn 2026')
        # the ends of the range: no Prev before the earliest term offered, no Next after the latest
        import datetime
        early = self._copy_dialog(c, sid=3, to="year=2000&season=Winter", frm="from_year=1999&from_season=Winter")   # nothing earlier is offered
        self.assertEqual(early.status_code, 200)
        self.assertNotIn("&lsaquo; Prev</a>", early.get_data(as_text=True))
        self.assertIn('<span class="btn secondary small disabled">&lsaquo; Prev</span>', early.get_data(as_text=True))
        last = datetime.date.today().year + 6
        late = self._copy_dialog(c, sid=3, to=f"year={last}&season=Summer", frm=f"from_year={last}&from_season=Autumn")
        self.assertIn('<span class="btn secondary small disabled">Next &rsaquo;</span>', late.get_data(as_text=True))

    def test_copy_from_the_chosen_term(self):
        c, t = self._funding_setup()
        c.post("/students/3/funding/new", data=dict(kind="TA", term_year=2026, term_season="Spring", source="Older",
               poetaf_coa="OLD-9", amount="111", notes="two terms back", csrf_token=t))
        c.post("/students/3/funding/new", data=dict(kind="fellowship", term_year=2026, term_season="Autumn",
               source="Existing", amount="9", csrf_token=t))
        report = "/reports/funding?season=Autumn&year=2026"
        r = c.post("/students/3/funding/copy", data=dict(year=2026, season="Autumn", from_year=2026, from_season="Spring",
                   next=report, csrf_token=t))
        self.assertEqual((r.status_code, r.headers["Location"]), (302, report))
        conn = physdb.connect(self.app.config["DB_PATH"])
        rows = lambda y, s: [tuple(x) for x in conn.execute(
            "SELECT kind, source, poetaf_coa, amount, notes FROM funding WHERE student_id = 3 AND term_year = ? "
            "AND term_season = ?", (y, s))]
        self.assertEqual(rows(2026, "Autumn"), [("TA", "Older", "OLD-9", 111.0, "two terms back")])   # Spring, not Summer
        self.assertEqual(len(rows(2026, "Spring")), 1); self.assertEqual(len(rows(2026, "Summer")), 1)  # sources kept
        conn.close()
        html = c.get(report).get_data(as_text=True).replace("&#39;", "'")
        self.assertIn("Copied 1 funding entry from Spring 2026 to Autumn 2026 for Cc, Stu (replacing 1).", html)
        # copying from a term with nothing: friendly message, nothing lost
        r = c.post("/students/3/funding/copy", data=dict(year=2026, season="Autumn", from_year=2024, from_season="Winter",
                   next=report, csrf_token=t), follow_redirects=True)
        self.assertIn("has no funding recorded for Winter 2024, so nothing was copied", r.get_data(as_text=True))
        conn = physdb.connect(self.app.config["DB_PATH"])
        self.assertEqual(len(rows(2026, "Autumn")), 1); conn.close()
        # copying a term onto itself is refused
        r = c.post("/students/3/funding/copy", data=dict(year=2026, season="Autumn", from_year=2026, from_season="Autumn",
                   next=report, csrf_token=t), follow_redirects=True)
        self.assertIn("Choose a different term to copy from.", r.get_data(as_text=True))
        # omitting the source still means "the previous term" (what the old Copy Prev did)
        r = c.post("/students/3/funding/copy", data=dict(year=2026, season="Autumn", next=report, csrf_token=t))
        conn = physdb.connect(self.app.config["DB_PATH"])
        self.assertEqual(rows(2026, "Autumn"), [("RA", "NSF", "COA-77", 2500.0, "renewed")]); conn.close()

    def test_copy_dialog_access_and_bad_input(self):
        c, t = self._funding_setup()
        good = "year=2026&season=Autumn"
        for bad in ("year=abc&season=Autumn", "year=1800&season=Autumn", "year=2026&season=Fall", "season=Autumn", "",
                    good + "&from_year=1900&from_season=Autumn", good + "&from_year=2026&from_season=Fall",
                    good + "&from_year=x&from_season=Autumn"):
            self.assertEqual(c.get(f"/students/1/funding/copy?{bad}").status_code, 400, bad)
            self.assertEqual(c.post(f"/students/1/funding/copy?{bad}", data={"csrf_token": t}).status_code, 400, bad)
        self.assertEqual(c.get(f"/students/99/funding/copy?{good}").status_code, 404)
        # not logged in -> login; view-only -> forbidden (both for the dialog and for doing the copy)
        self.assertEqual(self.app.test_client().get(f"/students/1/funding/copy?{good}").status_code, 302)
        vw = self.client("vw"); tv = self.token(vw)
        self.assertEqual(vw.get(f"/students/1/funding/copy?{good}&fragment=1").status_code, 403)
        self.assertEqual(vw.post("/students/3/funding/copy", data=dict(year=2026, season="Autumn", csrf_token=tv)).status_code, 403)
        # a hostile ?next= never leaves the site, in the dialog's Cancel link or after copying
        html = c.get(f"/students/1/funding/copy?{good}&next=//evil.com&fragment=1").get_data(as_text=True)
        self.assertNotIn("evil.com", html)
        r = c.post("/students/3/funding/copy", data=dict(year=2026, season="Autumn", next="https://evil.com", csrf_token=t))
        self.assertEqual(r.headers["Location"], "/students/3")

    def test_copy_prev_and_clear_are_protected(self):
        c, t = self._funding_setup()
        good = dict(year=2026, season="Autumn", csrf_token=t)
        self.assertEqual(c.post("/students/1/funding/copy", data={**good, "csrf_token": "bad"}).status_code, 400)
        for bad in (dict(year="abc"), dict(year=1800), dict(season="Fall"), dict(season="")):
            self.assertEqual(c.post("/students/1/funding/copy", data={**good, **bad}).status_code, 400, bad)
        self.assertEqual(c.post("/students/99/funding/copy", data=good).status_code, 404)
        for evil in ("//evil.com", "https://evil.com", "\\\\evil.com"):
            r = c.post("/students/3/funding/copy", data={**good, "next": evil})
            self.assertEqual(r.headers["Location"], "/students/3", evil)
        # view-only accounts can do neither
        vw = self.client("vw"); tv = self.token(vw)
        self.assertEqual(vw.post("/students/3/funding/copy", data=dict(year=2026, season="Autumn", csrf_token=tv)).status_code, 403)
        self.assertEqual(vw.post("/students/1/funding/1/delete", data={"csrf_token": tv}).status_code, 403)
        # a funding entry can't be deleted through another student's URL
        self.assertEqual(c.post("/students/2/funding/1/delete", data={"csrf_token": t}).status_code, 404)

    def test_funding_report_prev_and_next_term_buttons(self):
        import datetime
        from urllib.parse import urlparse, parse_qs
        from unittest import mock
        c = self.client("ed")
        def page(qs):
            with mock.patch.object(physdb, "current_term", return_value=("Autumn", 2026)):
                return c.get("/reports/funding?" + qs).get_data(as_text=True)
        def target(html, label):
            m = re.search(rf'<a class="btn secondary" href="([^"]+)" title="[^"]*">{label}</a>', html)
            if not m:
                return None
            q = parse_qs(urlparse(m.group(1).replace("&amp;", "&")).query)
            return q["season"][0], int(q["year"][0]), q.get("sort", [None])[0], q.get("dir", [None])[0]
        html = page("season=Autumn&year=2026&sort=amount&dir=desc")
        self.assertEqual(target(html, "&lsaquo; Prev"), ("Summer", 2026, "amount", "desc"))   # sort is kept
        self.assertEqual(target(html, "Next &rsaquo;"), ("Winter", 2027, "amount", "desc"))   # crosses the year
        self.assertIn('title="Summer 2026"', html); self.assertIn('title="Winter 2027"', html)
        self.assertEqual(target(page("season=Winter&year=2027"), "&lsaquo; Prev")[:2], ("Autumn", 2026))
        self.assertEqual(target(page("season=Spring&year=2026"), "Next &rsaquo;")[:2], ("Summer", 2026))
        # following a link shows that term
        self.assertIn("Funding for Winter 2027", page("season=Winter&year=2027"))
        # the menus and the buttons agree, and stepping stops at the ends of the Year menu
        self.assertNotIn("disabled", page("season=Autumn&year=2026").split("<table")[0].split('class="filters"')[1])
        first = page("season=Winter&year=2000")
        self.assertIn('<span class="btn secondary disabled" title="No earlier term is available">&lsaquo; Prev</span>', first)
        self.assertIsNone(target(first, "&lsaquo; Prev")); self.assertEqual(target(first, "Next &rsaquo;")[:2], ("Spring", 2000))
        last_year = datetime.date.today().year + 6
        last = page(f"season=Autumn&year={last_year}")
        self.assertIn('disabled" title="No later term is available">Next &rsaquo;</span>', last)
        self.assertIsNone(target(last, "Next &rsaquo;")); self.assertIsNotNone(target(last, "&lsaquo; Prev"))
        # viewers get them too, and the CSV link follows the displayed term
        vw = self.client("vw")
        with mock.patch.object(physdb, "current_term", return_value=("Autumn", 2026)):
            h = vw.get("/reports/funding").get_data(as_text=True)
        self.assertIn("&lsaquo; Prev", h); self.assertIn("Next &rsaquo;", h)

    def test_buttons_and_links_styled_as_buttons_are_the_same_height(self):
        """Regression: <button> doesn't inherit line-height/font like <a> does, so Clear / Filter / Save came out
        shorter than the link-buttons beside them. The shared .btn rule must normalise all of that."""
        from pathlib import Path
        css = (Path(webapp.__file__).parent / "static" / "style.css").read_text()
        rule = re.search(r"\.btn,button\.btn\{(.*?)\}", css, re.S).group(1)
        for needed in ("font-family:inherit", "line-height:1.5", "border:1px solid", "box-sizing:border-box"):
            self.assertIn(needed, rule)
        self.assertNotIn("border:0", rule)                         # filled buttons carry a border like the outlined ones

    def test_column_headings_never_wrap(self):
        """House rule: a column heading is always on one line, so columns widen rather than wrap a heading."""
        from pathlib import Path
        root = Path(webapp.__file__).parent
        css = (root / "static" / "style.css").read_text()
        self.assertRegex(css, r"(?m)^th\{[^}]*white-space:nowrap")
        for path in [root / "static" / "style.css", *sorted((root / "templates").glob("*.html"))]:
            text = path.read_text()
            # no rule aimed at a heading may switch wrapping back on
            self.assertNotRegex(text, r"th[^{};]*\{[^}]*white-space:\s*(normal|pre-wrap|pre-line)", path.name)
        self.assertNotRegex(css, r"th a\.sortlink\{[^}]*white-space:\s*normal")

    def test_add_another_entry_for_a_student_who_already_has_funding(self):
        from urllib.parse import urlparse, parse_qs
        c, t = self._funding_setup()          # Aa has two entries in Autumn 2026; Cc and Bb have none
        def rows_of(qs, sid="1"):
            return self._report_forms(c, "/reports/funding?season=Autumn&year=2026" + qs)[1][sid]
        add_links = lambda row: re.findall(r'<a class="btn small plus" href="([^"]+)"[^>]*>\+</a>', row)
        # every row ends with exactly one "+", whatever the sort order (so sorting by amount etc. keeps it handy)
        for qs in ("", "&sort=amount&dir=asc", "&sort=amount&dir=desc", "&sort=type&dir=desc", "&sort=clock"):
            rows = rows_of(qs)
            self.assertEqual(len(rows), 2, qs)                                            # Aa has two entries
            self.assertEqual([len(add_links(r)) for r in rows], [1, 1], qs)
            for r in rows:
                self.assertLess(r.index(">Clear<"), r.index(">Copy<")); self.assertLess(r.index(">Copy<"), r.index(">+<"))
        rows = rows_of("")
        first, second = (add_links(r)[0] for r in rows)
        self.assertEqual(first, second)                                                   # same target: this student + term
        # students with no funding still have the single "+" (plus Copy); no stray extras
        self.assertEqual([len(add_links(r)) for r in rows_of("", "2")], [1])
        # either row's "+" opens a new form for that term; saving adds a THIRD entry and returns to the report
        for which, source in ((0, "Third"), (1, "Fourth")):
            href = add_links(rows_of("")[which])[0].replace("&amp;", "&")
            q = parse_qs(urlparse(href).query)
            self.assertEqual((q["term_year"], q["term_season"]), (["2026"], ["Autumn"]))
            report = "/reports/funding?season=Autumn&year=2026"
            self.assertEqual(q["next"], [report])
            self.assertIn('<option value="Autumn" selected>Autumn</option>', c.get(href).get_data(as_text=True))
            r = c.post(href, data=dict(kind="TA", term_year=2026, term_season="Autumn", source=source, amount="7", csrf_token=t))
            self.assertEqual((r.status_code, r.headers["Location"]), (302, report))
            rows = rows_of("")
            self.assertEqual(len(rows), 3 + which)                                        # nothing was replaced
            self.assertEqual([len(add_links(r)) for r in rows], [1] * len(rows))          # and every row has its "+"
        self.assertIn("Now", rows[0]); self.assertIn("Third", rows[2]); self.assertIn("Fourth", rows[3])
        # view-only accounts see none of this
        from unittest import mock
        with mock.patch.object(physdb, "current_term", return_value=("Autumn", 2026)):
            html = self.client("vw").get("/reports/funding").get_data(as_text=True)
        self.assertNotIn(">+<", html)

    def test_funding_report_has_no_buttons_for_viewers(self):
        ed = self.client("ed"); t = self.token(ed); self.add_student(ed)
        ed.post("/students/1/funding/new", data=dict(kind="RA", term_year=2026, term_season="Autumn", csrf_token=t))
        vw = self.client("vw")
        from unittest import mock
        with mock.patch.object(physdb, "current_term", return_value=("Autumn", 2026)):
            html = vw.get("/reports/funding").get_data(as_text=True)
        self.assertIn("Lovelace", html)
        self.assertNotIn(">Edit<", html); self.assertNotIn(">+<", html)
        self.assertNotIn(">Copy<", html); self.assertNotIn(">Clear<", html); self.assertNotIn("copy-dialog", html)
        self.assertEqual(vw.get("/students/1/funding/1/edit?next=/reports/funding").status_code, 403)   # and no way in

    def test_funding_report_access_and_undated_warning(self):
        anon = self.app.test_client()
        self.assertEqual(anon.get("/reports/funding").status_code, 302)
        self.assertEqual(anon.get("/reports/funding.csv").status_code, 302)
        ed = self.client("ed"); self.add_student(ed)
        self.assertEqual(self.client("vw").get("/reports/funding").status_code, 200)          # viewers may read
        self.assertNotIn("no term yet", ed.get("/reports/funding").get_data(as_text=True))
        conn = physdb.connect(self.app.config["DB_PATH"])
        conn.execute("INSERT INTO funding (student_id, kind, legacy_term) VALUES (1, 'TA', 'AY 25-26')")
        conn.commit(); conn.close()
        text = " ".join(ed.get("/reports/funding").get_data(as_text=True).split())     # ignore line breaks
        self.assertIn("1 funding entry of active students has no term yet", text)

    def test_search_matches_advisor(self):
        c = self.client("ed"); t = self.token(c)
        for i, last in enumerate(("Aa", "Bb", "Cc", "Dd", "Ee"), 1):
            self.add_student(c, university_id=f"U{i}", email=f"s{i}@u.edu", first_name="Stu", last_name=last)
        for first, last in (("Niels", "Bohr"), ("Marie", "Curie")):
            c.post("/faculty/new", data=dict(first_name=first, last_name=last, email=f"{last}@u.edu", csrf_token=t))
        def assign(sid, fid, role, end=""):
            c.post(f"/students/{sid}/advising/new", data=dict(faculty_id=fid, role=role, start_date="2024-01-01",
                   end_date=end, csrf_token=t))
        assign(1, 1, "advisor")                       # Aa: advised by Bohr
        assign(2, 1, "committee")                     # Bb: Bohr only on committee -> not an advisor match
        assign(3, 2, "co-advisor")                    # Cc: co-advised by Curie
        assign(4, 1, "advisor", end="2025-01-01")     # Dd: Bohr was advisor, ended -> not shown, not matched
        assign(5, 2, "advisor"); assign(5, 1, "committee_chair")   # Ee: Curie advises, Bohr chairs
        found = lambda q: sorted(re.findall(r"<td>(U\d)</td>", c.get("/students?" + q).get_data(as_text=True)))
        self.assertEqual(found("q=bohr"), ["U1"])                 # last name, case-insensitive
        self.assertEqual(found("q=NIELS"), ["U1"])                # first name
        self.assertEqual(found("q=niels+bohr"), ["U1"])           # full name
        self.assertEqual(found("q=curie"), ["U3", "U5"])          # co-advisors count as advisors
        self.assertEqual(found("q=ohr"), ["U1"])                  # partial
        self.assertEqual(found("q=stu"), ["U1", "U2", "U3", "U4", "U5"])      # student names still match
        self.assertEqual(found("q=bohr&faculty=2"), [])           # combines with the other filters
        self.assertEqual(found("q=curie&status=active&sort=name&dir=desc"), ["U3", "U5"])
        self.assertEqual(found("q=%25"), [])                      # wildcard characters are literal
        self.assertIn("advisor", c.get("/students").get_data(as_text=True).split('name="q"')[1][:200])

    # ======================= faculty CNET and faculty logins =======================
    def _faculty_world(self):
        """Admin session + faculty Bohr (nbohr), Curie (mcurie), Planck (no CNET) and six students:
        Aa  advised by Bohr                 Bb  co-advised by Bohr
        Cc  advised by Curie, Bohr = committee   Dd  advised by Curie, Bohr = chair
        Ee  Curie's only                     Ff  Bohr WAS the advisor (ended)"""
        c = self.client("ad"); t = self.token(c)
        for first, last, cnet in (("Niels", "Bohr", "NBohr"), ("Marie", "Curie", "mcurie"), ("Max", "Planck", "")):
            c.post("/faculty/new", data=dict(first_name=first, last_name=last, email=f"{last}@u.edu", cnet=cnet, csrf_token=t))
        for i, last in enumerate(("Aa", "Bb", "Cc", "Dd", "Ee", "Ff"), 1):
            self.add_student(c, university_id=f"UID{i}", email=f"s{i}@u.edu", first_name="Stu", last_name=last,
                             cnet=f"stu{last.lower()}", notes=f"SECRET admin note {last}", research_area=f"area-{last}")
        def adv(sid, fid, role, end=""):
            c.post(f"/students/{sid}/advising/new", data=dict(faculty_id=fid, role=role, start_date="2024-01-01",
                   end_date=end, csrf_token=t))
        adv(1, 1, "advisor"); adv(2, 1, "co-advisor"); adv(3, 2, "advisor"); adv(3, 1, "committee")
        adv(4, 2, "advisor"); adv(4, 1, "committee_chair"); adv(5, 2, "advisor"); adv(6, 1, "advisor", end="2025-01-01")
        c.post("/students/1/funding/new", data=dict(kind="RA", term_year=2026, term_season="Autumn", source="NSF grant",
               poetaf_coa="1234567-ABC", amount="3000", notes="funding note <b>x</b>", csrf_token=t))
        c.post("/students/1/funding/new", data=dict(kind="TA", term_year=2026, term_season="Winter", source="Dept",
               amount="500", csrf_token=t))
        c.post("/students/2/funding/new", data=dict(kind="supplement", term_year=2026, term_season="Autumn", amount="250", csrf_token=t))
        c.post("/students/5/funding/new", data=dict(kind="RA", term_year=2026, term_season="Autumn", source="NOT-BOHRS", amount="999", csrf_token=t))
        c.post("/students/1/milestones/new", data=dict(kind="gde_classical_mechanics", date="2024-05-01", outcome="passed",
               notes="SECRET milestone note", csrf_token=t))
        return c, t

    def _make_faculty_login(self, admin, t, fid=1, password="temp-password-1"):
        r = admin.post("/users/new-faculty", data=dict(faculty_id=fid, password=password, csrf_token=t))
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:300])

    def _faculty_client(self, username="nbohr", password="temp-password-1", new_password="faculty-new-pass-1"):
        """Log in as a faculty member, choosing a real password at the forced change; returns a signed-in client."""
        c = self.app.test_client()
        r = c.post("/login", data={"username": username, "password": password})
        self.assertEqual(r.status_code, 302)
        t = self.token(c, "/account")
        r = c.post("/account", data=dict(current_password=password, new_password=new_password, confirm=new_password, csrf_token=t))
        self.assertEqual(r.status_code, 302)
        return c

    def test_faculty_cnet_field(self):
        c = self.client("ed"); t = self.token(c)
        form = c.get("/faculty/new").get_data(as_text=True)
        self.assertLess(form.index(">Email"), form.index(">CNET<")); self.assertNotIn(">CNET <span", form)   # optional
        post = lambda **kw: c.post("/faculty/new", data=dict(first_name="N", last_name="Bohr", email="b@u.edu", csrf_token=t, **kw))
        self.assertEqual(post(cnet="  NBohr ").status_code, 302)
        page = c.get("/faculty/1").get_data(as_text=True)
        self.assertIn("<dt>CNET</dt><dd>nbohr</dd>", page)                                        # saved lowercase, trimmed
        lst = c.get("/faculty").get_data(as_text=True)
        self.assertIn("sort=cnet", lst); self.assertIn("<td>nbohr</td>", lst)                     # a column on the faculty list
        r = c.post("/faculty/new", data=dict(first_name="M", last_name="Curie", email="c@u.edu", cnet="NBOHR", csrf_token=t))
        self.assertIn("Another faculty member already has this CNET", r.get_data(as_text=True))
        for bad in ("a", "has space", "<b>", "x" * 41):
            r = c.post("/faculty/new", data=dict(first_name="M", last_name="Curie", email="c@u.edu", cnet=bad, csrf_token=t))
            self.assertEqual(r.status_code, 200, bad); self.assertIn("Please fix", r.get_data(as_text=True))
        for i in (1, 2):                                                                          # blank CNETs may repeat
            self.assertEqual(c.post("/faculty/new", data=dict(first_name="B", last_name=f"Blank{i}", email=f"b{i}@u.edu",
                                    cnet="", csrf_token=t)).status_code, 302)
        self.assertEqual([x for x in re.findall(r"<td>(nbohr)</td>", c.get("/faculty?sort=cnet").get_data(as_text=True))], ["nbohr"])

    def test_admin_creates_faculty_accounts(self):
        c, t = self._faculty_world()
        form = c.get("/users/new-faculty").get_data(as_text=True)
        self.assertIn("Their username will be their CNET.", form)
        self.assertIn("Bohr, Niels (nbohr)", form); self.assertIn("Curie, Marie (mcurie)", form)
        self.assertNotIn("Planck, Max (", form)                                                   # no CNET -> not offered...
        self.assertIn("Max Planck", " ".join(form.split()).split("Not listed because")[1])         # ...but named in the note
        self._make_faculty_login(c, t, fid=1)
        conn = physdb.connect(self.app.config["DB_PATH"])
        row = conn.execute("SELECT username, role, must_change, faculty_id FROM users WHERE faculty_id = 1").fetchone()
        conn.close()
        self.assertEqual(tuple(row), ("nbohr", "faculty", 1, 1))                                  # username = CNET
        self.assertNotIn("Bohr, Niels (nbohr)", c.get("/users/new-faculty").get_data(as_text=True))   # one login each
        users = c.get("/users").get_data(as_text=True)
        self.assertIn("Faculty", users); self.assertIn("Niels Bohr", users)
        self.assertIn("+ Add faculty account", users)
        # the form refuses a person who already has one, and a made-up id
        for fid in (1, 3, 999, ""):
            r = c.post("/users/new-faculty", data=dict(faculty_id=fid, password="temp-password-2", csrf_token=t))
            self.assertEqual(r.status_code, 200, fid); self.assertIn("Please fix", r.get_data(as_text=True))
        r = c.post("/users/new-faculty", data=dict(faculty_id=2, password="short", csrf_token=t))
        self.assertIn("at least 10", r.get_data(as_text=True))
        # a login that already uses that name blocks it, with a clear message
        c.post("/users/new", data=dict(username="mcurie", role="viewer", password="viewer-pass-12", csrf_token=t))
        r = c.post("/users/new-faculty", data=dict(faculty_id=2, password="temp-password-2", csrf_token=t))
        self.assertIn("A login named &#39;mcurie&#39; already exists", r.get_data(as_text=True))
        # only administrators can do any of this
        for who in ("ed", "vw"):
            other = self.client(who); tt = self.token(other)
            self.assertEqual(other.get("/users/new-faculty").status_code, 403)
            self.assertEqual(other.post("/users/new-faculty", data=dict(faculty_id=2, password="temp-password-3", csrf_token=tt)).status_code, 403)

    def test_faculty_login_flow_and_landing_page(self):
        c, t = self._faculty_world(); self._make_faculty_login(c, t)
        f = self.app.test_client()
        r = f.post("/login", data={"username": "nbohr", "password": "wrong"})
        self.assertIn("Incorrect username", r.get_data(as_text=True))
        r = f.post("/login?next=/students", data={"username": "nbohr", "password": "temp-password-1"})
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/my"))                    # not /students
        self.assertEqual(f.get("/my").headers["Location"], "/account")                            # must pick a password first
        self.assertEqual(f.get("/").headers["Location"], "/account")
        tok = self.token(f, "/account")
        r = f.post("/account", data=dict(current_password="temp-password-1", new_password="faculty-new-pass-1",
                   confirm="faculty-new-pass-1", csrf_token=tok))
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/my"))                    # lands on their own page
        self.assertEqual(f.get("/").headers["Location"], "/my")
        page = f.get("/my").get_data(as_text=True)
        self.assertIn("My students", page); self.assertIn("Niels Bohr (nbohr)", page)
        nav = page.split("<main>")[0]
        self.assertIn('href="/my"', nav)
        for forbidden in ("/students", "/faculty", "/reports", "/users", "export"):
            self.assertNotIn(f'href="{forbidden}', nav)                                           # no links to other pages
        self.assertIn("(faculty)", nav)

    def test_faculty_see_exactly_their_own_students(self):
        c, t = self._faculty_world(); self._make_faculty_login(c, t)
        f = self._faculty_client()
        from unittest import mock
        with mock.patch.object(physdb, "current_term", return_value=("Autumn", 2026)):
            page = f.get("/my").get_data(as_text=True)
        adv, com = page.split("Students whose committee I am on")
        self.assertEqual(re.findall(r'/my/students/(\d+)">', adv), ["1", "2"])                    # Aa, Bb: advisor + co-advisor
        self.assertNotIn("Ff, Stu", adv); self.assertNotIn("Ee, Stu", adv + com)                  # ended / someone else's
        self.assertEqual(re.findall(r">(\w+), Stu</td>", com), ["Cc", "Dd"])
        self.assertIn("Committee member", com); self.assertIn("Committee chair", com)
        self.assertIn("Marie Curie", com)                                                         # the advisor, named
        self.assertNotIn("/my/students/3", page); self.assertNotIn("/my/students/4", page)        # committee: listed, not linked
        # funding for the shown term, with its parameters; other terms and other students' funding are absent
        for needle in ("Funding for Autumn 2026", "RA · NSF grant · 1234567-ABC · $3,000", "Supplement · $250", "total $3,250"):
            self.assertIn(needle, page)
        self.assertNotIn("NOT-BOHRS", page); self.assertNotIn("$999", page); self.assertNotIn("$500", page)
        # never the University ID or the internal notes
        for secret in ("UID1", "UID2", "UID3", "SECRET"):
            self.assertNotIn(secret, page)
        self.assertIn("area-Aa", page)
        self.assertNotIn("stuaa", page)                                                           # no CNET on the summary page...
        self.assertIn("stuaa", f.get("/my/students/1").get_data(as_text=True))                    # ...it's on each student's page
        # stepping to another term
        winter = f.get("/my?year=2026&season=Winter").get_data(as_text=True)
        self.assertIn("Funding for Winter 2026", winter); self.assertIn("TA · Dept · $500", winter)
        self.assertNotIn("NSF grant", winter)
        self.assertIn("year=2027&amp;season=Winter", page)                                         # Next from Autumn 2026
        fallback = f.get("/my?year=abc&season=Fall")                                              # bad input falls back
        self.assertEqual(fallback.status_code, 200); self.assertIn("Funding for", fallback.get_data(as_text=True))

    # ======================= milestone clock, and the Committee Formed milestone =======================
    def _milestone_world(self):
        c = self.client("ad"); t = self.token(c)
        self.add_student(c, matriculation_year="2022", matriculation_term="Autumn")          # student 1, matriculated Autumn 2022
        c.post("/faculty/new", data=dict(first_name="N", last_name="Bohr", email="b@u.edu", cnet="nbohr", csrf_token=t))
        c.post("/students/1/advising/new", data=dict(faculty_id=1, role="advisor", start_date="2022-09-01", csrf_token=t))
        for kind, date, outcome in (("committee_formed", "2023-03-01", "passed"), ("gde_classical_mechanics", "2024-05-01", "passed"),
                                    ("candidacy", "2022-06-01", "passed"), ("thesis_defense", "2022-10-17", "pending")):
            r = c.post("/students/1/milestones/new", data=dict(kind=kind, date=date, outcome=outcome, csrf_token=t))
            self.assertEqual(r.status_code, 302, kind)
        return c, t

    def _milestone_rows(self, html):
        sect = html.split("<h2>Milestones")[1].split("<h2>Funding")[0]
        return [re.findall(r"<td[^>]*>([^<]*)</td>", row)[:4] for row in re.findall(r"<tr><td>.*?</tr>", sect, re.S)]

    def test_milestone_tables_show_the_clock_at_each_date(self):
        c, t = self._milestone_world()
        html = c.get("/students/1").get_data(as_text=True)
        sect = html.split("<h2>Milestones")[1].split("<h2>Funding")[0]
        heads = re.findall(r"<th[^>]*>([^<]*)</th>", sect.split("</tr>")[0])
        self.assertEqual(heads[:5], ["Milestone", "Date", "Clock", "Outcome", "Notes"])      # Clock right after Date
        self.assertEqual(self._milestone_rows(html), [
            ["Candidacy", "2022-06-01", "—", "Passed"],                # before Autumn 2022: not matriculated yet
            ["Thesis Defense", "2022-10-17", "0.0", "Pending"],        # in the matriculation term
            ["Committee Formed", "2023-03-01", "0.25", "Passed"],      # Winter 2023
            ["GDE Classical Mechanics", "2024-05-01", "1.5", "Passed"]])       # Spring 2024: six terms in
        # a student who has since graduated keeps the clock the milestone had at the time
        c.post("/students/1/edit", data=dict(first_name="Ada", last_name="Lovelace", university_id="U1", cnet="stuaa",
               email="ada@u.edu", citizenship="citizen", status="graduated", matriculation_year="2022",
               matriculation_term="Autumn", csrf_token=t))
        self.assertEqual(self._milestone_rows(c.get("/students/1").get_data(as_text=True))[3],
                         ["GDE Classical Mechanics", "2024-05-01", "1.5", "Passed"])
        # the same column on a faculty member's read-only page
        self._make_faculty_login(c, t, fid=1)
        f = self._faculty_client()
        fhtml = f.get("/my/students/1").get_data(as_text=True)
        fsect = fhtml.split("<h2>Milestones")[1].split("<h2>Funding")[0]
        self.assertEqual(re.findall(r"<th[^>]*>([^<]*)</th>", fsect.split("</tr>")[0]), ["Milestone", "Date", "Clock", "Outcome"])
        self.assertEqual(self._milestone_rows(fhtml), [
            ["Candidacy", "2022-06-01", "—", "Passed"], ["Thesis Defense", "2022-10-17", "0.0", "Pending"],
            ["Committee Formed", "2023-03-01", "0.25", "Passed"], ["GDE Classical Mechanics", "2024-05-01", "1.5", "Passed"]])
        # a student with no milestones still renders (empty row spans the new column)
        c2 = self.client("ad"); t2 = self.token(c2)
        self.add_student(c2, university_id="U2", email="u2@u.edu", last_name="Empty")
        self.assertRegex(c2.get("/students/2").get_data(as_text=True), r'<td colspan="6" class="muted">None recorded')

    def test_retired_milestone_kinds_are_kept_but_not_offered(self):
        c, t = self._milestone_world()
        conn = physdb.connect(os.path.join(self.tmp, "t.db"))
        conn.execute("INSERT INTO milestones (student_id, kind, date, outcome) VALUES (1, 'qualifying_exam', '2022-05-01', 'passed')")
        mid = conn.execute("SELECT max(id) FROM milestones").fetchone()[0]; conn.commit(); conn.close()
        self.assertIn("Qualifying Exam", c.get("/students/1").get_data(as_text=True))
        new = c.get("/students/1/milestones/new").get_data(as_text=True)
        self.assertNotIn("qualifying_exam", new)
        edit = c.get(f"/students/1/milestones/{mid}/edit").get_data(as_text=True)
        self.assertIn("Qualifying Exam (retired)", edit)
        r = c.post("/students/1/milestones/new", data=dict(kind="thesis_proposal", date="2023-03-01", outcome="passed", csrf_token=t))
        self.assertNotEqual(r.status_code, 302)

    def test_committee_formed_milestone(self):
        c, t = self._milestone_world()
        form = c.get("/students/1/milestones/new").get_data(as_text=True)
        options = re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', form.split('name="kind"')[1].split("</select>")[0])
        self.assertEqual([v for v, _ in options], ["gde_classical_mechanics", "gde_quantum_mechanics", "gde_electrodynamics",
                                                   "gde_statistical_mechanics", "candidacy", "ms_degree", "committee_formed", "pre_oral",
                                                   "thesis_defense", "other"])
        self.assertEqual([l for _, l in options], ["GDE Classical Mechanics", "GDE Quantum Mechanics", "GDE Electrodynamics",
                                                   "GDE Statistical Mechanics", "Candidacy", "MS Degree", "Committee Formed", "Pre-Oral",
                                                   "Thesis Defense", "Other"])
        self.assertIn(("committee_formed", "Committee Formed"), options)
        self.assertIn("Committee Formed", c.get("/students/1").get_data(as_text=True))
        bad = c.post("/students/1/milestones/new", data=dict(kind="committee_formd", date="2023-03-01", outcome="passed", csrf_token=t))
        self.assertEqual(bad.status_code, 200); self.assertIn("Please fix", bad.get_data(as_text=True))
        # it can be edited and chosen on the "behind schedule" report like any other milestone
        r = c.get("/reports?kind=committee_formed&years=0")
        self.assertEqual(r.status_code, 200); self.assertIn('<option value="committee_formed" selected>Committee Formed</option>', r.get_data(as_text=True))
        self.assertNotIn("Lovelace", re.sub(r"\s+", " ", r.get_data(as_text=True)).split("<table")[1])   # they have one (passed)

    def test_staff_view_of_a_faculty_member_has_advisee_and_committee_tables(self):
        from unittest import mock
        c, t = self._faculty_world()
        def page(path="/faculty/1", client=None):
            with mock.patch.object(physdb, "current_term", return_value=("Autumn", 2026)):
                return (client or c).get(path).get_data(as_text=True)
        html = page()
        self.assertIn('<h2>Advisees <span class="muted">(2 active)</span></h2>', html)
        self.assertIn('<h2>Thesis Committees <span class="muted">(2 active)</span></h2>', html)
        self.assertNotIn("Current students", html)                                         # replaced by the two tables
        adv, com = html.split("<h2>Thesis Committees")
        headers = lambda h: re.findall(r"<th>([^<]*)</th>", h.split("</tr>")[0].split("<table>")[-1])
        self.assertEqual(headers(adv), ["Name", "Status", "Clock", "Expected graduation", "Research area", "Funding, Autumn 2026"])
        self.assertEqual(headers(com), ["Name", "Role", "Status", "Clock", "Research area", "Advisor"])
        # the right people, linked to the staff student pages (committee names too: staff can open any student)
        self.assertEqual(re.findall(r'/students/(\d+)">(\w+), Stu', adv), [("1", "Aa"), ("2", "Bb")])
        self.assertEqual(re.findall(r'/students/(\d+)">(\w+), Stu', com), [("3", "Cc"), ("4", "Dd")])
        self.assertNotIn("/my/", html)
        for gone in ("Ee, Stu", "Ff, Stu"):                                                # not Bohr's / ended
            self.assertNotIn(gone, html)
        self.assertIn("Committee member", com); self.assertIn("Committee chair", com); self.assertIn("Marie Curie", com)
        self.assertRegex(adv, r"<td>4\.0</td>")                                            # clock (matriculated Autumn 2022)
        # funding for the term, and a term stepper that stays on THIS page
        for needle in ("Funding for Autumn 2026", "RA · NSF grant · 1234567-ABC · $3,000", "Supplement · $250", "total $3,250"):
            self.assertIn(needle, adv)
        self.assertNotIn("NOT-BOHRS", html); self.assertNotIn("$500", html)
        self.assertIn('href="/faculty/1?year=2026&amp;season=Summer"', adv)
        self.assertIn('href="/faculty/1?year=2027&amp;season=Winter"', adv)
        winter = page("/faculty/1?year=2026&season=Winter")
        self.assertIn("Funding for Winter 2026", winter); self.assertIn("TA · Dept · $500", winter); self.assertNotIn("NSF grant", winter)
        self.assertEqual(c.get("/faculty/1?year=abc&season=Fall").status_code, 200)         # bad input falls back
        # still the faculty page it was: details and the staff buttons
        for needle in ("<dt>CNET</dt><dd>nbohr</dd>", "Bohr@u.edu", "All faculty", ">Edit<", "Delete this faculty member?"):
            self.assertIn(needle, html)
        # the same two lists a faculty member sees on their own page
        self._make_faculty_login(c, t); f = self._faculty_client()
        mine = page("/my", client=f)
        self.assertEqual(re.findall(r">(\w+), Stu", mine.split("Students whose committee")[0]), ["Aa", "Bb"])
        self.assertEqual(re.findall(r">(\w+), Stu", mine.split("Students whose committee")[1]), ["Cc", "Dd"])
        # graduated students stay listed but are not counted
        for sid, last in ((2, "Bb"), (4, "Dd")):
            c.post(f"/students/{sid}/edit", data=dict(first_name="Stu", last_name=last, university_id=f"UID{sid}", cnet=f"stu{last.lower()}",
                   email=f"s{sid}@u.edu", citizenship="citizen", status="graduated", matriculation_year="2022",
                   matriculation_term="Autumn", csrf_token=t))
        html = page()
        adv, com = html.split("<h2>Thesis Committees")
        self.assertIn("(1 active)", adv); self.assertIn("(1 active)", com)
        self.assertIn("Bb, Stu", adv); self.assertIn("Dd, Stu", com)
        # other faculty: their own students; empty tables say so
        curie = page("/faculty/2")
        self.assertEqual(re.findall(r'/students/(\d+)">(\w+), Stu', curie.split("<h2>Thesis Committees")[0]), [("3", "Cc"), ("4", "Dd"), ("5", "Ee")])
        self.assertIn("Not on any thesis committees.", curie)
        planck = page("/faculty/3")
        self.assertIn("No current advisees.", planck); self.assertIn("Not on any thesis committees.", planck)
        self.assertEqual(planck.count("(0 active)"), 2)
        # editors and viewers see the same page; faculty logins cannot open it
        for who in ("ed", "vw"):
            self.assertIn("<h2>Advisees", page(client=self.client(who)))
        self.assertEqual(f.get("/faculty/1").status_code, 403)

    def test_milestone_check_tab(self):
        from unittest import mock
        c = self.client("ad"); t = self.token(c)
        # at Autumn 2026: Mc_A clock 1.0, Mc_B 0.5, Mc_C 0.25, Mc_D 0.0
        for uid, last, term, year in (("U1", "Mc_A", "Autumn", "2025"), ("U2", "Mc_B", "Spring", "2026"),
                                      ("U3", "Mc_C", "Summer", "2026"), ("U4", "Mc_D", "Autumn", "2026")):
            self.add_student(c, university_id=uid, email=f"{uid}@u.edu", last_name=last, matriculation_term=term,
                             matriculation_year=year, research_area=f"area-{last}")
        for first, last in (("Niels", "Bohr"), ("Marie", "Curie")):
            c.post("/faculty/new", data=dict(first_name=first, last_name=last, email=f"{last}@u.edu", csrf_token=t))
        for sid, fid, role in ((1, 1, "advisor"), (1, 2, "co-advisor"), (2, 2, "advisor"), (3, 1, "committee")):
            c.post(f"/students/{sid}/advising/new", data=dict(faculty_id=fid, role=role, start_date="2025-01-01", csrf_token=t))
        def page(qs=""):
            with mock.patch.object(physdb, "current_term", return_value=("Autumn", 2026)):
                return c.get("/reports?" + qs).get_data(as_text=True)
        names = lambda h: re.findall(r">(Mc_\w), Ada</a>", h)
        html = page("kind=gde_classical_mechanics&years=0.25")
        # 1) the tab and page are called "Milestone Check"
        self.assertIn(">Milestone Check</a>", html); self.assertIn("<h1>Milestone Check</h1>", html)
        self.assertNotIn("Behind schedule", html); self.assertNotIn("behind schedule", html)
        # 2) the time box takes fractions, and the filter works on them (clock >= 0.25: A, B, C but not D)
        self.assertRegex(html, r'<input type="number" name="years" min="0" max="15" step="0.25" value="0.25">')
        self.assertEqual(names(html), ["Mc_A", "Mc_B", "Mc_C"])
        self.assertEqual(names(page("kind=gde_classical_mechanics&years=0.3")), ["Mc_A", "Mc_B"])
        self.assertEqual(names(page("kind=gde_classical_mechanics&years=1")), ["Mc_A"])
        self.assertEqual(names(page("kind=gde_classical_mechanics&years=1.25")), [])
        self.assertEqual(sorted(names(page("kind=gde_classical_mechanics&years=0"))), ["Mc_A", "Mc_B", "Mc_C", "Mc_D"])
        for typed, shown in (("2", "2"), ("1.5", "1.5"), ("0.75", "0.75"), ("", "2"), ("abc", "2"), ("nan", "2"), ("inf", "2"),
                             ("-3", "0"), ("99", "15")):                                       # sensible handling of any input
            self.assertIn(f'step="0.25" value="{shown}">', page(f"years={typed}"), typed)
        self.assertIn('step="0.25" value="2">', page())                                       # default stays 2 years
        # 3) a Clock column right of Matriculation, 4) an Advisor column at the far right
        sect = html.split("<table>")[1]
        self.assertEqual(re.findall(r"<th[^>]*>\s*<a [^>]*>([^<]*?)\s*<span", sect.split("</tr>")[0]),
                         ["Name", "Matriculation", "Clock", "Research area", "Advisor"])
        rows = [re.findall(r"<td[^>]*>(.*?)</td>", r, re.S) for r in re.findall(r"<tr><td>.*?</tr>", sect, re.S)]
        cells = [[re.sub(r"<[^>]+>", "", x).strip() for x in row] for row in rows]
        self.assertEqual(cells, [["Mc_A, Ada", "Autumn 2025", "1.0", "area-Mc_A", "Bohr, Curie"],
                                 ["Mc_B, Ada", "Spring 2026", "0.5", "area-Mc_B", "Curie"],
                                 ["Mc_C, Ada", "Summer 2026", "0.25", "area-Mc_C", ""]])   # committee-only: no advisor listed
        # both new columns sort (blanks last either way), and the links keep the milestone and the years
        self.assertEqual(names(page("kind=gde_classical_mechanics&years=0&sort=clock")), ["Mc_D", "Mc_C", "Mc_B", "Mc_A"])
        self.assertEqual(names(page("kind=gde_classical_mechanics&years=0&sort=clock&dir=desc")), ["Mc_A", "Mc_B", "Mc_C", "Mc_D"])
        self.assertEqual(names(page("kind=gde_classical_mechanics&years=0&sort=advisor"))[:2], ["Mc_A", "Mc_B"])
        self.assertEqual(names(page("kind=gde_classical_mechanics&years=0&sort=advisor&dir=desc"))[:2], ["Mc_B", "Mc_A"])
        self.assertEqual(sorted(names(page("kind=gde_classical_mechanics&years=0&sort=advisor"))[2:]), ["Mc_C", "Mc_D"])
        link_page = page("kind=candidacy&years=0.25&sort=clock&dir=asc")
        self.assertIn("kind=candidacy&amp;years=0.25&amp;sort=clock&amp;dir=desc", link_page)
        self.assertIn("kind=candidacy&amp;years=0.25&amp;sort=advisor&amp;dir=asc", link_page)
        self.assertIn('name="sort" value="clock"', link_page)
        # the page still exists for everyone who could open it before (view-only too), and the other tab is intact
        with mock.patch.object(physdb, "current_term", return_value=("Autumn", 2026)):
            self.assertEqual(self.client("vw").get("/reports").status_code, 200)
        self.assertIn("Funding by term", html)

    def test_faculty_summary_columns_and_active_counts(self):
        c, t = self._faculty_world(); self._make_faculty_login(c, t)
        f = self._faculty_client()
        from unittest import mock
        def page():
            with mock.patch.object(physdb, "current_term", return_value=("Autumn", 2026)):
                return f.get("/my").get_data(as_text=True)
        html = page()
        adv, com = html.split("Students whose committee I am on")
        headers = lambda h: re.findall(r"<th>([^<]*)</th>", h.split("</tr>")[0].split("<table>")[-1])
        self.assertEqual(headers(adv), ["Name", "Status", "Clock", "Expected graduation", "Research area",
                                        "Funding, Autumn 2026"])                                  # no CNET, no Matriculation
        self.assertEqual(headers(com), ["Name", "My role", "Status", "Clock", "Research area", "Advisor"])
        for gone in ("CNET", "Matriculation"):
            self.assertNotIn(gone, html)
        self.assertNotIn("Autumn 2022", html)                                                     # their matriculation term
        self.assertRegex(adv, r"<td>4\.0</td>")                                                   # the clock is still there
        # counts say how many are ACTIVE
        self.assertIn("Students I advise <span class=\"muted\">(2 active)</span>", html)
        self.assertIn("Students whose committee I am on <span class=\"muted\">(2 active)</span>", html)
        # a graduated advisee and a graduated committee student are still listed, but not counted
        for sid, last in ((2, "Bb"), (4, "Dd")):
            r = c.post(f"/students/{sid}/edit", data=dict(first_name="Stu", last_name=last, university_id=f"UID{sid}",
                       cnet=f"stu{last.lower()}", email=f"s{sid}@u.edu", citizenship="citizen", status="graduated",
                       matriculation_year="2022", matriculation_term="Autumn", csrf_token=t))
            self.assertEqual(r.status_code, 302)
        html = page()
        adv, com = html.split("Students whose committee I am on")
        self.assertIn("(1 active)", adv); self.assertIn("(1 active)", com)
        self.assertEqual(re.findall(r'/my/students/(\d+)">', adv), ["1", "2"])                    # Bb is still listed
        self.assertIn("Dd, Stu", com); self.assertIn("Graduated", adv); self.assertIn("Graduated", com)
        for sid, last in ((1, "Aa"), (3, "Cc")):                                                  # nobody active left
            c.post(f"/students/{sid}/edit", data=dict(first_name="Stu", last_name=last, university_id=f"UID{sid}",
                   cnet=f"stu{last.lower()}", email=f"s{sid}@u.edu", citizenship="citizen", status="withdrawn",
                   matriculation_year="2022", matriculation_term="Autumn", csrf_token=t))
        html = page()
        self.assertEqual(html.count("(0 active)"), 2)
        self.assertEqual(f.get("/my/students/1").status_code, 200)                                # still reachable

    def test_faculty_student_detail_is_limited_to_their_own_advisees(self):
        c, t = self._faculty_world(); self._make_faculty_login(c, t)
        f = self._faculty_client()
        page = f.get("/my/students/1").get_data(as_text=True)
        self.assertEqual(f.get("/my/students/2").status_code, 200)
        for needle in ("Stu Aa", "stuaa", "area-Aa", "GDE Classical Mechanics", "Passed", "Niels Bohr", "NSF grant", "1234567-ABC",
                       "$3,000", "Autumn 2026", "Winter 2026", "funding note &lt;b&gt;x&lt;/b&gt;"):   # funding notes shown, escaped
            self.assertIn(needle, page)
        self.assertNotIn("<b>x</b>", page)
        for secret in ("UID1", "SECRET admin note", "SECRET milestone note"):                    # never these
            self.assertNotIn(secret, page)
        self.assertNotIn(">Edit<", page); self.assertNotIn(">Clear<", page); self.assertNotIn("<form class=\"inline\"", page)
        # every other student is a plain 404 -- committee, someone else's, ended advisor, nonexistent
        for sid in (3, 4, 5, 6, 99):
            self.assertEqual(f.get(f"/my/students/{sid}").status_code, 404, sid)

    def test_faculty_logins_are_refused_everywhere_else(self):
        """Default deny: try EVERY address in the app as a faculty member; only their own pages may open."""
        c, t = self._faculty_world(); self._make_faculty_login(c, t)
        f = self._faculty_client(); tok = self.token(f, "/my")
        allowed = webapp.FACULTY_ENDPOINTS
        checked, tested = 0, set()
        for rule in self.app.url_map.iter_rules():
            if rule.endpoint in allowed:
                continue
            path = re.sub(r"<(?:int:)?(\w+)>", lambda m: {"table": "funding"}.get(m.group(1), "1"), rule.rule)
            for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
                r = f.open(path, method=method, data={"csrf_token": tok} if method == "POST" else None)
                self.assertEqual(r.status_code, 403, f"{method} {path} ({rule.endpoint}) -> {r.status_code}")
                checked += 1; tested.add(rule.endpoint)
        # the sweep really covered the sensitive parts of the app
        for endpoint in ("students", "student", "student_edit", "student_delete", "export", "funding_report",
                         "funding_report_csv", "funding_copy", "child_new", "child_edit", "child_delete", "faculty_list",
                         "faculty_detail", "faculty_edit", "reports", "users", "user_new", "user_new_faculty",
                         "user_edit", "user_delete"):
            self.assertIn(endpoint, tested)
        self.assertEqual(allowed, {"faculty_home", "faculty_student", "account", "logout", "login", "static", "home"})
        for path in ("/export.csv", "/students", "/students/1", "/faculty", "/reports", "/reports/funding", "/users",
                     "/students/1/funding/copy?year=2026&season=Autumn", "/nonexistent", "/static/../webapp.py"):
            self.assertIn(f.get(path).status_code, (403, 404), path)
        # ... including with a forged "next" at login
        r = self.app.test_client().post("/login?next=/users", data={"username": "nbohr", "password": "faculty-new-pass-1"})
        self.assertEqual(r.headers["Location"], "/my")
        # their own pages and password change still work
        self.assertEqual(f.get("/my").status_code, 200); self.assertEqual(f.get("/account").status_code, 200)
        self.assertEqual(f.post("/logout", data={"csrf_token": tok}).status_code, 302)

    def test_faculty_pages_escape_what_they_show(self):
        c, t = self._faculty_world(); self._make_faculty_login(c, t)
        c.post("/students/1/edit", data=dict(first_name="<script>alert(1)</script>", last_name="Aa", university_id="UID1",
               cnet="stuaa", email="s1@u.edu", citizenship="citizen", status="active", matriculation_year="2022",
               matriculation_term="Autumn", research_area="<img src=x onerror=alert(2)>", csrf_token=t))
        c.post("/students/3/edit", data=dict(first_name="<b>Bold</b>", last_name="Cc", university_id="UID3",
               cnet="stucc", email="s3@u.edu", citizenship="citizen", status="active", matriculation_year="2022",
               matriculation_term="Autumn", csrf_token=t))
        f = self._faculty_client()
        for path in ("/my", "/my/students/1"):
            html = f.get(path).get_data(as_text=True)
            self.assertNotIn("<script>alert", html); self.assertNotIn("<img src=x", html); self.assertNotIn("<b>Bold</b>", html)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)

    def test_generic_add_account_cannot_make_a_faculty_login(self):
        c, t = self._faculty_world()
        form = c.get("/users/new").get_data(as_text=True)
        self.assertNotIn('<option value="faculty"', form)
        r = c.post("/users/new", data=dict(username="sneaky", role="faculty", password="a-long-password-1", csrf_token=t))
        self.assertEqual(r.status_code, 200); self.assertIn("Please fix", r.get_data(as_text=True))
        conn = physdb.connect(self.app.config["DB_PATH"])
        self.assertEqual(conn.execute("SELECT count(*) FROM users WHERE username = 'sneaky'").fetchone()[0], 0); conn.close()

    def test_refusals_show_a_friendly_page(self):
        c, t = self._faculty_world(); self._make_faculty_login(c, t)
        f = self._faculty_client()
        r = f.get("/students")
        self.assertEqual(r.status_code, 403)
        html = r.get_data(as_text=True)
        self.assertIn("<h1>Not available</h1>", html); self.assertIn("isn&#39;t available for faculty accounts", html)
        self.assertIn('href="/my">Back to my students</a>', html)
        r = f.get("/my/students/999")
        self.assertEqual(r.status_code, 404); self.assertIn("<h1>Page not found</h1>", r.get_data(as_text=True))
        r = c.get("/students/999")                                                        # staff get the same style
        self.assertEqual(r.status_code, 404); self.assertIn("Back to the start", r.get_data(as_text=True))
        self.assertEqual(c.post("/students/new", data={"csrf_token": "bad"}).status_code, 400)
        self.assertIn("<h1>That didn&#39;t work</h1>", c.post("/students/new", data={"csrf_token": "bad"}).get_data(as_text=True))

    def test_faculty_pages_are_closed_to_everyone_else(self):
        c, t = self._faculty_world(); self._make_faculty_login(c, t)
        for who in ("ad", "ed", "vw"):
            other = self.client(who)
            for path in ("/my", "/my/students/1"):
                self.assertEqual(other.get(path).status_code, 403, (who, path))
        self.assertEqual(self.app.test_client().get("/my").status_code, 302)                      # not logged in -> login

    def test_faculty_account_follows_the_faculty_record(self):
        c, t = self._faculty_world(); self._make_faculty_login(c, t)
        f = self._faculty_client()
        self.assertEqual(f.get("/my").status_code, 200)
        # changing their CNET renames the login; the old name stops working, the new one works
        r = c.post("/faculty/1/edit", data=dict(first_name="Niels", last_name="Bohr", email="Bohr@u.edu", cnet="nbohr2", csrf_token=t))
        self.assertEqual(r.status_code, 302)
        self.assertIn("nbohr2", c.get("/users").get_data(as_text=True))
        old = self.app.test_client().post("/login", data={"username": "nbohr", "password": "faculty-new-pass-1"})
        self.assertIn("Incorrect username", old.get_data(as_text=True))
        new = self.app.test_client().post("/login", data={"username": "nbohr2", "password": "faculty-new-pass-1"})
        self.assertEqual(new.status_code, 302)
        # removing it is refused while the login exists (with an explanation); another person's CNET can't be taken
        r = c.post("/faculty/1/edit", data=dict(first_name="Niels", last_name="Bohr", email="Bohr@u.edu", cnet="", csrf_token=t))
        self.assertEqual(r.status_code, 200); self.assertIn("login account that uses their CNET", r.get_data(as_text=True))
        r = c.post("/faculty/1/edit", data=dict(first_name="Niels", last_name="Bohr", email="Bohr@u.edu", cnet="mcurie", csrf_token=t))
        self.assertIn("Another faculty member already has this CNET", r.get_data(as_text=True))
        c.post("/users/new", data=dict(username="taken-name", role="viewer", password="viewer-pass-12", csrf_token=t))
        r = c.post("/faculty/1/edit", data=dict(first_name="Niels", last_name="Bohr", email="Bohr@u.edu", cnet="taken-name", csrf_token=t))
        self.assertIn("already the login name of a different account", r.get_data(as_text=True))
        # resetting the password signs them out and forces a new one; the faculty edit page offers only that
        uid = physdb.connect(self.app.config["DB_PATH"]).execute("SELECT id FROM users WHERE faculty_id = 1").fetchone()[0]
        edit = c.get(f"/users/{uid}/edit").get_data(as_text=True)
        self.assertIn("New temporary password", edit); self.assertNotIn('name="role"', edit)
        r = c.post(f"/users/{uid}/edit", data=dict(role="admin", password="reset-password-77", csrf_token=t))   # role is ignored
        self.assertEqual(r.status_code, 302)
        conn = physdb.connect(self.app.config["DB_PATH"])
        self.assertEqual(tuple(conn.execute("SELECT role, must_change FROM users WHERE id = ?", (uid,)).fetchone()), ("faculty", 1)); conn.close()
        self.assertEqual(f.get("/my").status_code, 302)                                           # old session is dead
        # a faculty member who still advises students can't be deleted (so their login stays too)
        c.post("/faculty/1/delete", data={"csrf_token": t})
        conn = physdb.connect(self.app.config["DB_PATH"])
        self.assertEqual(conn.execute("SELECT count(*) FROM users WHERE faculty_id = 1").fetchone()[0], 1); conn.close()
        # but deleting one with no students removes their login with them, and ends any open session
        c.post("/faculty/3/edit", data=dict(first_name="Max", last_name="Planck", email="Planck@u.edu", cnet="mplanck", csrf_token=t))
        self._make_faculty_login(c, t, fid=3, password="planck-temp-pass")
        pf = self._faculty_client("mplanck", "planck-temp-pass", "planck-new-pass-9")
        self.assertEqual(pf.get("/my").status_code, 200)
        self.assertEqual(c.post("/faculty/3/delete", data={"csrf_token": t}).status_code, 302)
        conn = physdb.connect(self.app.config["DB_PATH"])
        self.assertEqual(conn.execute("SELECT count(*) FROM users WHERE username = 'mplanck'").fetchone()[0], 0); conn.close()
        self.assertEqual(pf.get("/my").status_code, 302)                                          # signed out at once

    def test_create_user_command_never_touches_a_faculty_login(self):
        c, t = self._faculty_world(); self._make_faculty_login(c, t)
        from unittest import mock
        with mock.patch("getpass.getpass", return_value="a-long-password-1"), \
             mock.patch.object(physdb, "connect", lambda *a, **k: self._conn_for_cli()):
            with self.assertRaises(SystemExit) as cm:
                webapp.save_user("nbohr", "viewer")
        self.assertIn("faculty login", str(cm.exception))
        conn = physdb.connect(self.app.config["DB_PATH"])
        self.assertEqual(conn.execute("SELECT role FROM users WHERE username = 'nbohr'").fetchone()[0], "faculty"); conn.close()

    def _conn_for_cli(self):
        import sqlite3 as _s
        conn = _s.connect(self.app.config["DB_PATH"]); conn.row_factory = _s.Row; conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def test_cnet_field(self):
        c = self.client("ed")
        form = c.get("/students/new").get_data(as_text=True)
        self.assertLess(form.index("University ID"), form.index(">CNET<"))        # right after University ID
        self.assertLess(form.index(">CNET<"), form.index("Email"))
        self.assertNotIn(">CNET <span", form)                                      # optional
        # optional: students without one are fine, and several may be blank
        self.assertEqual(self.add_student(c, university_id="U1", email="a1@u.edu").status_code, 302)
        self.assertEqual(self.add_student(c, university_id="U2", email="a2@u.edu", cnet="").status_code, 302)
        # saved lowercase and trimmed, shown on the student's page
        self.assertEqual(self.add_student(c, university_id="U3", email="a3@u.edu", cnet="  AdaL ").status_code, 302)
        self.assertIn("<dt>CNET</dt><dd>adal</dd>", c.get("/students/3").get_data(as_text=True))
        self.assertIn("<dt>CNET</dt><dd>—</dd>", c.get("/students/1").get_data(as_text=True))
        # unique, case-insensitively, with a friendly message
        r = self.add_student(c, university_id="U4", email="a4@u.edu", cnet="ADAL")
        self.assertIn("Another student already has this CNET", r.get_data(as_text=True))
        # bad characters / length rejected
        for bad in ("a", "has space", "semi;colon", "x" * 41, "<b>"):
            r = self.add_student(c, university_id="U5", email="a5@u.edu", cnet=bad)
            self.assertEqual(r.status_code, 200, bad)
            self.assertIn("Please fix", r.get_data(as_text=True))
        # searchable, and editing a student keeps its own CNET without a false duplicate error
        self.assertEqual(self.names3(c.get("/students?q=adal").get_data(as_text=True)), ["U3"])
        t = self.token(c, "/students/3/edit")
        r = c.post("/students/3/edit", data=dict(first_name="Ada", last_name="Lovelace", university_id="U3",
                   cnet="adal", email="a3@u.edu", citizenship="citizen", status="active",
                   matriculation_year="2022", matriculation_term="Autumn", csrf_token=t))
        self.assertEqual(r.status_code, 302)
        self.assertIn("adal", c.get("/export.csv").get_data(as_text=True))

    def names3(self, html):
        return re.findall(r"<td>(U\d+)</td>", html)

    def names(self, html):
        return re.findall(r"Stu_\w+(?=,)", html)

    def test_student_columns_sort_both_ways(self):
        from unittest import mock
        c = self.client("ed")
        for last, uid, status, term, year, area in [   # clock at Autumn 2026: C=2.5, B=1.0, A and D blank
                ("Stu_C", "U3", "active", "Spring", "2024", "optics"),
                ("Stu_A", "U1", "graduated", "Autumn", "2020", ""),
                ("Stu_B", "U2", "on_leave", "Autumn", "2025", "Atomic"),
                ("Stu_D", "U4", "active", "Winter", "2027", "cosmology")]:   # not yet matriculated
            self.add_student(c, last_name=last, university_id=uid, email=f"{uid}@u.edu", status=status,
                             matriculation_term=term, matriculation_year=year, research_area=area)
        def page(qs=""):
            with mock.patch.object(physdb, "current_term", return_value=("Autumn", 2026)):
                return c.get("/students?" + qs).get_data(as_text=True)
        order = lambda qs="": self.names(page(qs))
        A, B, C, D = "Stu_A", "Stu_B", "Stu_C", "Stu_D"
        self.assertEqual(order(), [A, B, C, D])                                   # default: by name
        self.assertEqual(order("sort=name&dir=desc"), [D, C, B, A])
        self.assertEqual(order("sort=university_id&dir=desc"), [D, C, B, A])
        self.assertEqual(order("sort=status"), [C, D, B, A])                       # active, on leave, graduated
        self.assertEqual(order("sort=status&dir=desc"), [A, B, C, D])
        self.assertEqual(order("sort=matriculation"), [A, C, B, D])                # oldest first
        self.assertEqual(order("sort=matriculation&dir=desc"), [D, B, C, A])
        self.assertEqual(order("sort=clock"), [B, C, A, D])                        # blanks last either way
        self.assertEqual(order("sort=clock&dir=desc"), [C, B, A, D])
        self.assertEqual(order("sort=research_area"), [B, D, C, A])                # case-insensitive, blank last
        self.assertEqual(order("sort=research_area&dir=desc"), [C, D, B, A])
        self.assertEqual(order("sort=bogus&dir=sideways"), [A, B, C, D])           # bad input falls back to default
        # links: clicking the active column flips direction; filters are kept; the arrow marks the column
        html = page("status=active&sort=clock&dir=asc")
        self.assertEqual(self.names(html), [C, D])
        self.assertIn('href="/students?status=active&amp;sort=clock&amp;dir=desc"', html)
        self.assertIn('href="/students?status=active&amp;sort=name&amp;dir=asc"', html)
        self.assertIn('aria-sort="ascending"', html)
        self.assertIn('name="sort" value="clock"', html)                           # a new search keeps the sort
        self.assertIn("▲", page("sort=name&dir=asc")); self.assertIn("▼", page("sort=name&dir=desc"))

    def test_faculty_reports_and_accounts_sort(self):
        c = self.client("ad"); t = self.token(c)
        for last, area in (("Cc", "optics"), ("Aa", ""), ("Bb", "atomic")):
            c.post("/faculty/new", data=dict(first_name="P", last_name=last, email=f"{last}@u.edu",
                                             research_area=area, csrf_token=t))
        for i in (1, 2, 3):
            self.add_student(c, last_name=f"Stu_{i}", university_id=f"U{i}", email=f"s{i}@u.edu",
                             matriculation_year=str(2014 + i))
        def assign(sid, fid, role):
            c.post(f"/students/{sid}/advising/new", data=dict(faculty_id=fid, role=role,
                   start_date="2024-01-01", csrf_token=t))
        # Cc (id 1): advises student 1 and sits on student 2's committee -> 1 advisee, 2 committees
        # Bb (id 3): advises student 3 -> 1 advisee, 1 committee; Aa (id 2): none
        assign(1, 1, "advisor"); assign(2, 1, "committee"); assign(3, 3, "advisor")
        fac = lambda qs="": re.findall(r"/faculty/\d+\">(\w+),", c.get("/faculty?" + qs).get_data(as_text=True))
        self.assertEqual(fac(), ["Aa", "Bb", "Cc"])
        self.assertEqual(fac("sort=name&dir=desc"), ["Cc", "Bb", "Aa"])
        self.assertEqual(fac("sort=research_area"), ["Bb", "Cc", "Aa"])               # blank last
        self.assertEqual(fac("sort=committees&dir=desc"), ["Cc", "Bb", "Aa"])
        self.assertEqual(fac("sort=advisees"), ["Aa", "Bb", "Cc"])
        self.assertEqual(fac("sort=advisees&dir=desc"), ["Bb", "Cc", "Aa"])           # tie keeps name order
        rep = lambda qs="": self.names2(c.get("/reports?kind=gde_classical_mechanics&years=0&" + qs).get_data(as_text=True))
        self.assertEqual(rep(), ["Stu_1", "Stu_2", "Stu_3"])                           # default: oldest matriculation first
        self.assertEqual(rep("sort=matriculation&dir=desc"), ["Stu_3", "Stu_2", "Stu_1"])
        self.assertEqual(rep("sort=name&dir=desc"), ["Stu_3", "Stu_2", "Stu_1"])
        html = c.get("/reports?kind=candidacy&years=1&sort=name&dir=asc").get_data(as_text=True)
        self.assertIn("kind=candidacy&amp;years=1&amp;sort=name&amp;dir=desc", html)      # report filters kept
        users = lambda qs="": re.findall(r"<tr><td>(\w+)", c.get("/users?" + qs).get_data(as_text=True))
        self.assertEqual(users(), ["ad", "ed", "vw"])
        self.assertEqual(users("sort=role&dir=desc"), ["ad", "ed", "vw"])             # admin, editor, viewer
        self.assertEqual(users("sort=role"), ["vw", "ed", "ad"])
        self.assertEqual(users("sort=username&dir=desc"), ["vw", "ed", "ad"])

    def names2(self, html):
        return re.findall(r"Stu_\d(?=,)", html)

    def test_delete_buttons_carry_a_working_csrf_token(self):
        """Regression: the buttons used to render an empty token, so deleting from the browser failed."""
        c = self.client("ad"); t = self.token(c)
        self.add_student(c)
        c.post("/faculty/new", data=dict(first_name="N", last_name="Bohr", email="b@u.edu", csrf_token=t))
        c.post("/students/1/milestones/new", data=dict(kind="other", date="2024-01-01", outcome="pending", csrf_token=t))
        c.post("/users/new", data=dict(username="tempuser", role="viewer", password="temp-password-1", csrf_token=t))
        def delete_forms(url):
            html = c.get(url).get_data(as_text=True)
            return re.findall(r'<form class="inline" method="post" action="([^"]+/delete)"[^>]*>\s*'
                              r'<input type="hidden" name="csrf_token" value="([^"]*)"', html)
        for url in ("/students/1", "/faculty/1", "/users"):
            forms = delete_forms(url)
            self.assertTrue(forms, url)
            for action, token in forms:
                self.assertTrue(token, f"empty CSRF token on a Delete button at {url}")
        (m_action, m_token), = [f for f in delete_forms("/students/1") if "/milestones/" in f[0]]
        self.assertEqual(c.post(m_action, data={"csrf_token": m_token}).status_code, 302)
        self.assertNotIn("2024-01-01", c.get("/students/1").get_data(as_text=True))     # the milestone is gone
        (u_action, u_token), = [f for f in delete_forms("/users") if f[0].endswith(f"/users/{self.user_row('tempuser')['id']}/delete")]
        self.assertEqual(c.post(u_action, data={"csrf_token": u_token}).status_code, 302)
        self.assertIsNone(self.user_row("tempuser"))
        (f_action, f_token), = delete_forms("/faculty/1")
        self.assertEqual(c.post(f_action, data={"csrf_token": f_token}).status_code, 302)
        self.assertEqual(c.get("/faculty/1").status_code, 404)
        (s_action, s_token), = [f for f in delete_forms("/students/1") if f[0] == "/students/1/delete"]
        self.assertEqual(c.post(s_action, data={"csrf_token": s_token}).status_code, 302)
        self.assertEqual(c.get("/students/1").status_code, 404)

    def test_matriculation_year_and_term(self):
        c = self.client("ed")
        form = c.get("/students/new").get_data(as_text=True)
        self.assertIn("Matriculation year", form); self.assertIn("Matriculation term", form)
        self.assertNotIn("Start year", form)
        # Autumn is preselected; the other terms are offered
        self.assertIn('<option value="Autumn" selected>Autumn</option>', form)
        for season in ("Winter", "Spring", "Summer"):
            self.assertIn(f'<option value="{season}" >{season}</option>', form)
        for bad in ("", "Fall", "autumn"):
            r = self.add_student(c, matriculation_term=bad)
            self.assertEqual(r.status_code, 200, bad)
        self.assertEqual(self.add_student(c, matriculation_term="Spring", matriculation_year="2021").status_code, 302)
        page = c.get("/students/1").get_data(as_text=True)
        self.assertIn("<dt>Matriculation</dt><dd>Spring 2021</dd>", page)
        self.assertIn("Spring 2021", c.get("/students").get_data(as_text=True))
        self.assertIn("matriculation_term", c.get("/export.csv").get_data(as_text=True))
        self.assertNotIn("Started", page)

    def test_citizenship_field(self):
        c = self.client("ed")
        form = c.get("/students/new").get_data(as_text=True)
        self.assertIn("Citizenship", form)
        self.assertIn("&mdash; choose &mdash;", form)
        for value, text in (("citizen", "Citizen"), ("permanent_resident", "Permanent Resident"),
                            ("international", "International")):
            self.assertIn(f'<option value="{value}" >{text}</option>', form)
        for bad in ("", "alien", "Citizen"):   # required, and only the listed values
            r = self.add_student(c, citizenship=bad)
            self.assertEqual(r.status_code, 200, bad)
            self.assertIn("Please fix", r.get_data(as_text=True))
        self.assertEqual(self.add_student(c, citizenship="permanent_resident").status_code, 302)
        self.assertIn("Permanent Resident", c.get("/students/1").get_data(as_text=True))
        self.assertIn("permanent_resident", c.get("/export.csv").get_data(as_text=True))
        # an existing student with no value is shown as blank and must be filled in on edit
        conn = physdb.connect(self.app.config["DB_PATH"])
        conn.execute("UPDATE students SET citizenship = NULL"); conn.commit(); conn.close()
        self.assertIn("<dd>—</dd>", c.get("/students/1").get_data(as_text=True))
        self.assertIn("&mdash; choose &mdash;", c.get("/students/1/edit").get_data(as_text=True))

    def test_advisors_listed_in_role_order(self):
        c = self.client("ed"); t = self.token(c); self.add_student(c)
        names = ["Aaron", "Bravo", "Carter", "Dunn", "Evans", "Fox"]
        for n in names:
            c.post("/faculty/new", data=dict(first_name="P", last_name=n, email=f"{n}@u.edu", csrf_token=t))
        def assign(fid, role, end=""):
            c.post("/students/1/advising/new", data=dict(faculty_id=fid, role=role, start_date="2024-01-01",
                   end_date=end, csrf_token=t))
        # entered in scrambled order, with alphabetically-first people on the lowest-ranked roles
        assign(1, "committee"); assign(2, "committee_chair"); assign(6, "co-advisor")
        assign(5, "committee"); assign(4, "advisor"); assign(3, "advisor", end="2025-01-01")
        page = c.get("/students/1").get_data(as_text=True)
        section = page[page.index("Advisors"):page.index("Milestones")]
        order = [n for n in sorted(names, key=section.index) if n in section]
        self.assertEqual(order, ["Dunn", "Fox", "Bravo", "Aaron", "Evans", "Carter"])
        #   advisor, co-advisor, chair, members (alphabetical), then the ended advisor last

    def test_faculty_committee_count(self):
        c = self.client("ed"); t = self.token(c)
        for i in (1, 2, 3):
            self.add_student(c, university_id=f"U{i}", email=f"s{i}@u.edu")
        for name in ("Bohr", "Curie"):
            c.post("/faculty/new", data=dict(first_name="X", last_name=name, email=f"{name}@u.edu", csrf_token=t))
        def assign(sid, fid, role, end=""):
            c.post(f"/students/{sid}/advising/new", data=dict(faculty_id=fid, role=role,
                   start_date="2024-01-01", end_date=end, csrf_token=t))
        assign(1, 1, "advisor"); assign(1, 1, "committee")            # same student twice: counts once
        assign(2, 1, "committee_chair"); assign(3, 1, "committee", end="2025-01-01")  # ended: not counted
        assign(2, 2, "committee")
        html = c.get("/faculty").get_data(as_text=True)
        import re
        rows = {m.group(1): re.findall(r"<td>(\d+)</td>", m.group(2)) for m in
                re.finditer(r"<tr><td><a [^>]*>([^<,]+),[^<]*</a></td>(.*?)</tr>", html, re.S)}
        self.assertEqual(rows["Bohr"], ["1", "2"])    # 1 advisee; 2 distinct students on committees
        self.assertEqual(rows["Curie"], ["0", "1"])
        self.assertIn("Committees", html)
        # only active students are tallied: once student 2 graduates, their committee seats stop counting
        conn = physdb.connect(os.path.join(self.tmp, "t.db"))
        conn.execute("UPDATE students SET status = 'graduated' WHERE id IN (1, 2)"); conn.commit(); conn.close()
        html = c.get("/faculty").get_data(as_text=True)
        rows = {m.group(1): re.findall(r"<td>(\d+)</td>", m.group(2)) for m in
                re.finditer(r"<tr><td><a [^>]*>([^<,]+),[^<]*</a></td>(.*?)</tr>", html, re.S)}
        self.assertEqual(rows["Bohr"], ["0", "0"])
        self.assertEqual(rows["Curie"], ["0", "0"])

    def test_supplement_funding_type(self):
        c = self.client("ed"); self.add_student(c); t = self.token(c)
        form = c.get("/students/1/funding/new").get_data(as_text=True)
        self.assertIn('<option value="supplement" >Supplement</option>', form)
        self.assertLess(form.index(">Fellowship<"), form.index(">Supplement<"))
        self.assertLess(form.index(">Supplement<"), form.index(">Other<"))
        r = c.post("/students/1/funding/new", data=dict(kind="supplement", term_year="2026",
                   term_season="Winter", csrf_token=t))
        self.assertEqual(r.status_code, 302)
        self.assertIn("Supplement", c.get("/students/1").get_data(as_text=True))
        bad = c.post("/students/1/funding/new", data=dict(kind="bonus", term_year="2026",
                     term_season="Winter", csrf_token=t))
        self.assertEqual(bad.status_code, 200)

    def test_other_students_records_not_reachable(self):
        c = self.client("ed")
        self.add_student(c)
        self.add_student(c, university_id="U2", email="b@u.edu")
        t = self.token(c)
        c.post("/students/1/milestones/new", data=dict(kind="other", date="2024-01-01", outcome="pending", csrf_token=t))
        self.assertEqual(c.get("/students/2/milestones/1/edit").status_code, 404)
        self.assertEqual(c.post("/students/2/milestones/1/delete", data={"csrf_token": t}).status_code, 404)
        self.assertEqual(c.get("/students/1/users/1/edit").status_code, 404)

    def test_xss_escaped_and_search_wildcards(self):
        c = self.client("ed")
        self.add_student(c, first_name="<script>alert(1)</script>", last_name="X")
        html = c.get("/students").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("1 student", c.get("/students?q=script").get_data(as_text=True))
        self.assertIn("0 students", c.get("/students?q=%25").get_data(as_text=True))

    def test_csv_formula_injection_and_reports(self):
        c = self.client("vw")
        ed = self.client("ed")
        self.add_student(ed, first_name="=HYPERLINK(1)", matriculation_year="2015")
        self.assertIn("'=HYPERLINK", c.get("/export.csv").get_data(as_text=True))
        self.assertIn("1 student", c.get("/reports").get_data(as_text=True))
        self.assertEqual(c.get("/reports?kind=zzz&years=abc").status_code, 200)


class ServeTests(unittest.TestCase):
    def test_port_helpers(self):
        import socket
        s = socket.socket(); s.bind(("127.0.0.1", 0)); busy = s.getsockname()[1]
        try:
            self.assertFalse(webapp._port_free("127.0.0.1", busy))
            self.assertFalse(webapp._our_app_running("127.0.0.1", busy))  # not our app
        finally:
            s.close()
        self.assertTrue(webapp._port_free("127.0.0.1", busy))


if __name__ == "__main__":
    unittest.main()
