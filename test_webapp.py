import os
import re
import unittest

from werkzeug.security import generate_password_hash

import physdb
import webapp


def mailer_module():
    import mailer
    return mailer


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
        if over.get("status") == "graduated":
            data.update(graduation_year="2025", graduation_term="Spring")      # a graduated student needs the term
        if over.get("status") == "withdrawn":
            data.update(withdrawal_year="2025", withdrawal_term="Winter")      # ...and so does a withdrawn one
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
            "milestones": dict(kind="pre_oral", date="2024-05-01", outcome="passed"),
            "funding": dict(kind="TA", term_year="2026", term_season="Autumn", poetaf_coa="1234567-ABC", amount="12000",
                           notes="Covers <i>half</i> of the term\nrenewal pending"),
        }.items():
            r = c.post(f"/students/1/{table}/new", data={**data, "csrf_token": t})
            self.assertEqual(r.status_code, 302, table)
        page = c.get("/students/1").get_data(as_text=True)
        for needle in ("Bohr", "Pre-Oral", "$12,000", "Autumn 2026", "1234567-ABC",
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
            r"<tr><td><a [^>]*>(Stu\w+),[^<]*</a></td>.*?<td>([^<]*)</td><td>[^<]*</td><td>[^<]*</td><td>[^<]*</td></tr>", html, re.S)}
        self.assertEqual(cells, {"StuU1": "0.0", "StuU2": "1.0", "StuU3": "2.5", "StuU4": "—"})
        # the last column shows the graduation term (graduated students only) and sorts
        grads = lambda q="": re.findall(r"<tr><td><a [^>]*>(Stu\w+),[^<]*</a></td>.*?<td>([^<]*)</td></tr>",
                                        c.get("/students" + q).get_data(as_text=True), re.S)
        self.assertIn("Graduated", html.split("</tr>")[0])
        self.assertEqual(dict(grads()), {"StuU1": "", "StuU2": "", "StuU3": "", "StuU4": "Spring 2025"})
        self.assertEqual(grads("?sort=graduated")[0], ("StuU4", "Spring 2025"))
        self.assertEqual(grads("?sort=graduated&dir=desc")[0], ("StuU4", "Spring 2025"))               # blanks always last

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
            "Matriculation term", "Matriculation year", "Clock (years)",
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
        css = self.app.test_client().get("/static/style.css").get_data(as_text=True)
        self.assertIn(".source{min-width:20ch}", css)                         # room for ~20 characters
        self.assertIn('class="source"', html)
        self.assertIn(".wide td.actions{white-space:nowrap", css)             # Edit / Clear / Copy / + never wrap
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
        """Every confirmation message a browser would ask, read from the pages' data-confirm attributes (the small
        script in static/app.js shows them). There must be no inline onsubmit handlers left."""
        from html.parser import HTMLParser
        found = []
        class P(HTMLParser):
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                assert "onsubmit" not in attrs, "inline handlers are blocked by the Content-Security-Policy"
                if tag == "form" and attrs.get("data-confirm") is not None:
                    found.append(attrs["data-confirm"])
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
        fac = self.confirm_prompts(c.get("/faculty/1?year=2026&season=Autumn").get_data(as_text=True))
        self.assertEqual(fac, ["Delete this faculty member?",
                               'Delete this funding entry (RA, $5) for O\'Brien <b>&</b>, Bob "Rob" in Autumn 2026? This cannot be undone.'])
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
        c.post("/students/1/milestones/new", data=dict(kind="pre_oral", date="2024-05-01", outcome="passed",
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
        for kind, date, outcome in (("committee_formed", "2023-03-01", "passed"), ("pre_oral", "2024-05-01", "passed"),
                                    ("candidacy", "2022-06-01", "passed"), ("thesis_defense", "2022-10-17", "pending")):
            r = c.post("/students/1/milestones/new", data=dict(kind=kind, date=date, outcome=outcome, csrf_token=t))
            self.assertEqual(r.status_code, 302, kind)
        return c, t

    def _milestone_rows(self, html):
        sect = html.split("<h2>Milestones")[1].split("<h2>Funding")[0]
        return [re.findall(r"<td[^>]*>([^<]*)</td>", row)[:4] for row in re.findall(r"<tr><td>.*?</tr>", sect, re.S)]

    def test_milestones_table_is_a_snapshot_of_passed_milestones(self):
        c, t = self._milestone_world()                # Lovelace: passed Candidacy, Committee, Pre-Oral; Defense only pending
        for i, (last, status) in enumerate((("Bb", "active"), ("Cc", "graduated"), ("Dd", "active"), ("Ee", "withdrawn"),
                                            ("Ff", "on_leave")), 2):
            self.add_student(c, university_id=f"U{i}", email=f"s{i}@u.edu", first_name="Stu", last_name=last, status=status,
                             matriculation_year="2022", matriculation_term="Autumn",
                             **(dict(graduation_year="2025", graduation_term="Winter") if status == "graduated" else {}))
        post = lambda sid, kind, outcome: c.post(f"/students/{sid}/milestones/new",
                                                 data=dict(kind=kind, date="2024-01-01", outcome=outcome, csrf_token=t))
        courses = lambda sid, *codes: c.post(f"/students/{sid}/courses", data={"course": list(codes), "csrf_token": t})
        courses(1, "cm"); courses(2, "cm", "em1", "em2"); courses(4, "qm1", "qm2", "sm")
        post(2, "pre_oral", "conditional"); post(2, "candidacy", "failed"); post(2, "ms_degree", "pending")
        for kind in physdb.MILESTONE_KINDS[:-1]:
            post(3, kind, "passed")                   # graduated: listed, with the term they graduated
        post(5, "candidacy", "passed"); post(6, "candidacy", "passed")      # withdrawn: not listed; on leave: listed
        from unittest import mock
        def page(q=""):
            with mock.patch.object(physdb, "current_term", return_value=("Autumn", 2026)):
                return c.get("/reports/milestones" + q).get_data(as_text=True)
        html = page()
        text = lambda h: re.sub(r"\s+", " ", re.sub(r"<[^>]+>|[▲▼↕]", "", h)).strip()
        heads = [text(h) for h in re.findall(r"<th[^>]*>(.*?)</th>", html.split("</tr>")[0], re.S)]
        self.assertEqual(heads, ["Name", "Clock", "Advisor", "Courses", "Candidacy", "MS",
                                 "Committee", "Pre-Oral", "Defense"])
        def table(h):
            out = {}
            for row in re.findall(r"<tr><td><a [^>]*>(.*?)</a></td>(.*?)</tr>", h, re.S):
                cells = re.findall(r"<td[^>]*>(.*?)</td>", row[1], re.S)
                out[row[0]] = (cells[0], cells[1], cells[2],
                                [i for i, x in enumerate(cells[3:]) if "✓" in x or "&#10003;" in x])
            return out
        rows = table(html)
        self.assertNotIn("Ee, Stu", rows); self.assertNotIn("Cc, Stu", rows)                # withdrawn and graduated left out
        self.assertEqual(rows["Lovelace, Ada"], ("4.0", "Bohr", "CM", [0, 2, 3]))            # course CM; Candidacy, Committee, Pre-Oral
        self.assertEqual(rows["Bb, Stu"], ("4.0", "", "CM, EM-1, EM-2", [3]))               # conditional counts; failed/pending don't
        self.assertEqual(rows["Dd, Stu"], ("4.0", "", "QM-1, QM-2, SM", []))                # abbreviations in course order; blanks
        self.assertEqual(rows["Ff, Stu"], ("4.0", "", "", [0]))                              # on leave: listed
        self.assertEqual(re.findall(r"<tr><td><a [^>]*>(.*?)</a>", html), ["Bb, Stu", "Dd, Stu", "Ff, Stu", "Lovelace, Ada"])
        self.assertIn("3 active students, 1 on leave", html)
        # sorting: the checkmark columns put checks first, or last when reversed
        order = lambda q: re.findall(r"<tr><td><a [^>]*>(\w+)", page(q))
        self.assertEqual(order("?sort=courses"), ["Bb", "Dd", "Lovelace", "Ff"])            # most courses with credit first
        self.assertEqual(order("?sort=courses&dir=desc"), ["Ff", "Lovelace", "Bb", "Dd"])
        self.assertEqual(order("?sort=candidacy"), ["Ff", "Lovelace", "Bb", "Dd"])
        self.assertEqual(sorted(order("?sort=candidacy&dir=desc")[:2]), ["Bb", "Dd"])           # blanks first when reversed
        self.assertEqual(order("?sort=advisor")[0], "Lovelace"); self.assertEqual(order("?sort=name&dir=desc")[0], "Lovelace")
        self.assertEqual(sorted(order("?sort=clock")), ["Bb", "Dd", "Ff", "Lovelace"]); self.assertEqual(order("?sort=bogus"), order(""))
        # it is the first report tab, and the Reports link opens it
        tabs = re.findall(r'<p class="tabs">(.*?)</p>', html, re.S)[0]
        self.assertEqual(re.findall(r">([^<]+)</a>", tabs), ["Milestones Table", "Milestone Check", "Funding by term"])
        self.assertIn('class="on" href="/reports/milestones"', tabs)
        self.assertIn('<a href="/reports/milestones">Reports</a>', html)
        for path in ("/reports", "/reports/funding"):
            self.assertIn("Milestones Table", c.get(path).get_data(as_text=True))
        # viewers can look; faculty and FSR logins cannot
        self.assertEqual(self.client("vw").get("/reports/milestones").status_code, 200)

    def test_graduation_term_replaces_expected_graduation_year(self):
        c = self.client("ed"); t = self.token(c)
        form = c.get("/students/new").get_data(as_text=True)
        self.assertNotIn("Expected graduation", form)
        self.assertIn('data-show-when="status=graduated"', form)                           # revealed only for Graduated
        labels = re.findall(r"<label for=\"(\w+)\">", form)
        self.assertEqual(labels[labels.index("transfer_term") + 1:][:2], ["graduation_year", "graduation_term"])
        self.assertIn("Graduation term", form); self.assertIn("Graduation year", form)
        self.assertEqual(self.add_student(c).status_code, 302)
        base = dict(first_name="Ada", last_name="Lovelace", university_id="U1", email="ada@u.edu", citizenship="citizen",
                    matriculation_year="2022", matriculation_term="Autumn", csrf_token=t)
        post = lambda **kw: c.post("/students/1/edit", data={**base, **kw})
        # graduated needs both the year and the term
        r = post(status="graduated"); html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Choose the term they graduated.", html); self.assertIn("Enter the year they graduated.", html)
        self.assertIn("Choose the term they graduated.", post(status="graduated", graduation_year="2026").get_data(as_text=True))
        self.assertIn("Enter the year they graduated.", post(status="graduated", graduation_term="Spring").get_data(as_text=True))
        self.assertIn("Enter a four-digit year", post(status="graduated", graduation_year="26", graduation_term="Spring").get_data(as_text=True))
        self.assertEqual(post(status="graduated", graduation_year="2026", graduation_term="Spring").status_code, 302)
        page = c.get("/students/1").get_data(as_text=True)
        self.assertIn("<dt>Graduated</dt><dd>Spring 2026</dd>", page); self.assertNotIn("Expected graduation", page)
        edit = c.get("/students/1/edit").get_data(as_text=True)
        self.assertIn('<option value="Spring" selected>', edit.split('name="graduation_term"')[1]); self.assertIn('value="2026"', edit)
        # any other status clears the term
        self.assertEqual(post(status="active", graduation_year="2026", graduation_term="Spring").status_code, 302)
        conn = physdb.connect(self.app.config["DB_PATH"])
        self.assertEqual(tuple(conn.execute("SELECT graduation_year, graduation_term FROM students").fetchone()), (None, None)); conn.close()
        self.assertNotIn("<dt>Graduated</dt>", c.get("/students/1").get_data(as_text=True))

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
            ["Pre-Oral", "2024-05-01", "1.5", "Passed"]])       # Spring 2024: six terms in
        # a student who has since graduated keeps the clock the milestone had at the time
        c.post("/students/1/edit", data=dict(first_name="Ada", last_name="Lovelace", university_id="U1", cnet="stuaa",
               email="ada@u.edu", citizenship="citizen", status="graduated", matriculation_year="2022",
               matriculation_term="Autumn", csrf_token=t))
        self.assertEqual(self._milestone_rows(c.get("/students/1").get_data(as_text=True))[3],
                         ["Pre-Oral", "2024-05-01", "1.5", "Passed"])
        # the same column on a faculty member's read-only page
        self._make_faculty_login(c, t, fid=1)
        f = self._faculty_client()
        fhtml = f.get("/my/students/1").get_data(as_text=True)
        fsect = fhtml.split("<h2>Milestones")[1].split("<h2>Funding")[0]
        self.assertEqual(re.findall(r"<th[^>]*>([^<]*)</th>", fsect.split("</tr>")[0]), ["Milestone", "Date", "Clock", "Outcome", "Form"])
        self.assertEqual(self._milestone_rows(fhtml), [
            ["Candidacy", "2022-06-01", "—", "Passed"], ["Thesis Defense", "2022-10-17", "0.0", "Pending"],
            ["Committee Formed", "2023-03-01", "0.25", "Passed"], ["Pre-Oral", "2024-05-01", "1.5", "Passed"]])
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

    def test_core_courses_checkboxes_on_the_student_page(self):
        c, t = self._milestone_world()
        page = lambda client=c, path="/students/1": client.get(path).get_data(as_text=True)
        html = page()
        box = html.split("<h2>Milestones")[1].split("<table>")[0]                         # at the top of the Milestones section
        self.assertEqual(re.findall(r'name="course" value="(\w+)"', box), ["cm", "qm1", "qm2", "em1", "em2", "sm"])
        self.assertEqual([x for x in re.findall(r'<label class="course" title="([^"]+)">', box)],
                         ["Classical Mechanics", "Quantum Mechanics I", "Quantum Mechanics II", "Electrodynamics I",
                          "Electrodynamics II", "Statistical Mechanics"])
        self.assertEqual(re.findall(r"> (CM|QM-1|QM-2|EM-1|EM-2|SM)</label>", box), ["CM", "QM-1", "QM-2", "EM-1", "EM-2", "SM"])
        self.assertNotIn(" checked", box); self.assertIn("0 of 6", box)
        r = c.post("/students/1/courses", data={"course": ["cm", "qm2", "sm", "bogus"], "csrf_token": t})
        self.assertEqual(r.status_code, 302)
        box = page().split("<h2>Milestones")[1].split("<table>")[0]
        self.assertEqual(re.findall(r'value="(\w+)" checked', box), ["cm", "qm2", "sm"]); self.assertIn("3 of 6", box)
        conn = physdb.connect(self.app.config["DB_PATH"])
        self.assertEqual(sorted(x[0] for x in conn.execute("SELECT course FROM course_credits")), ["cm", "qm2", "sm"]); conn.close()
        # saving again replaces the set (unticking removes credit); an empty form clears it
        c.post("/students/1/courses", data={"course": ["em1"], "csrf_token": t})
        self.assertEqual(re.findall(r'value="(\w+)" checked', page().split("<h2>Milestones")[1].split("<table>")[0]), ["em1"])
        c.post("/students/1/courses", data={"csrf_token": t})
        self.assertIn("0 of 6", page())
        c.post("/students/1/courses", data={"course": ["cm", "qm1", "qm2", "em1", "em2", "sm"], "csrf_token": t})
        self.assertIn("6 of 6", page())
        # viewers see a read-only list; they cannot save
        v = self.client("vw"); vt = self.token(v)
        ro = page(v).split("<h2>Milestones")[1].split("<table>")[0]
        self.assertNotIn("<input", ro); self.assertIn("6 of 6", ro); self.assertIn("CM", ro)
        self.assertEqual(v.post("/students/1/courses", data={"course": ["cm"], "csrf_token": vt}).status_code, 403)
        self.assertEqual(c.post("/students/1/courses", data={"course": ["cm"]}).status_code, 400)       # needs the CSRF token
        self.assertEqual(c.post("/students/99/courses", data={"csrf_token": t}).status_code, 404)
        # the credits go with the student if they are deleted
        c.post("/students/1/delete", data={"csrf_token": t})
        conn = physdb.connect(self.app.config["DB_PATH"])
        self.assertEqual(conn.execute("SELECT count(*) FROM course_credits").fetchone()[0], 0); conn.close()

    def test_core_courses_on_faculty_and_fsr_pages_are_read_only(self):
        c, t = self._fsr_world()
        c.post("/students/1/courses", data={"course": ["cm", "sm"], "csrf_token": t})
        self._make_faculty_login(c, t); fac = self._faculty_client()
        html = fac.get("/my/students/1").get_data(as_text=True)
        box = html.split("<h2>Milestones")[1].split("<table>")[0]
        self.assertNotIn("<input", box); self.assertIn("2 of 6", box)
        self.assertEqual(len(re.findall(r'class="course done"', box)), 2)
        self.assertEqual(fac.post("/students/1/courses", data={"course": ["cm"], "csrf_token": self.token(fac, "/account")}).status_code, 403)
        self._make_fsr_login(c, t); fsr = self._fsr_client()
        self.assertEqual(fsr.post("/students/1/courses", data={"course": ["cm"], "csrf_token": self.token(fsr, "/account")}).status_code, 403)

    def test_gde_milestones_are_retired_but_kept(self):
        c, t = self._milestone_world()
        conn = physdb.connect(self.app.config["DB_PATH"])
        conn.execute("INSERT INTO milestones (student_id, kind, date, outcome) VALUES (1, 'gde_quantum_mechanics', '2023-01-05', 'passed')")
        mid = conn.execute("SELECT max(id) FROM milestones").fetchone()[0]; conn.commit(); conn.close()
        self.assertIn("GDE Quantum Mechanics", c.get("/students/1").get_data(as_text=True))               # still shown
        self.assertNotIn("gde_", c.get("/students/1/milestones/new").get_data(as_text=True))              # no longer offered
        self.assertIn("GDE Quantum Mechanics (retired)", c.get(f"/students/1/milestones/{mid}/edit").get_data(as_text=True))
        r = c.post("/students/1/milestones/new", data=dict(kind="gde_classical_mechanics", date="2023-03-01", outcome="passed", csrf_token=t))
        self.assertNotEqual(r.status_code, 302)
        self.assertNotIn("gde_", c.get("/reports").get_data(as_text=True))                                # not on Milestone Check either

    def test_committee_formed_milestone(self):
        c, t = self._milestone_world()
        form = c.get("/students/1/milestones/new").get_data(as_text=True)
        options = re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', form.split('name="kind"')[1].split("</select>")[0])
        self.assertEqual([v for v, _ in options], ["candidacy", "ms_degree", "committee_formed", "pre_oral", "thesis_defense", "other"])
        self.assertEqual([l for _, l in options], ["Candidacy", "MS Degree", "Committee Formed", "Pre-Oral", "Thesis Defense", "Other"])
        self.assertNotIn("GDE", form)                                                   # the GDE entries are gone from the menu
        self.assertIn(("committee_formed", "Committee Formed"), options)
        self.assertIn("Committee Formed", c.get("/students/1").get_data(as_text=True))
        bad = c.post("/students/1/milestones/new", data=dict(kind="committee_formd", date="2023-03-01", outcome="passed", csrf_token=t))
        self.assertEqual(bad.status_code, 200); self.assertIn("Please fix", bad.get_data(as_text=True))
        # it can be edited and chosen on the "behind schedule" report like any other milestone
        r = c.get("/reports?kind=committee_formed&years=0")
        self.assertEqual(r.status_code, 200); self.assertIn('<option value="committee_formed" selected>Committee Formed</option>', r.get_data(as_text=True))
        self.assertNotIn("Lovelace", re.sub(r"\s+", " ", r.get_data(as_text=True)).split("<table")[1])   # they have one (passed)

    # ---- FSRs ----------------------------------------------------------------------------------------------
    def _fsr_world(self):
        """_faculty_world plus FSRs Xx (cnet fsrx) for Bohr, and Yy (no CNET) for Curie. Returns (admin client, token)."""
        c, t = self._faculty_world()
        for first, last, cnet in (("Xavier", "Xx", "FSRX"), ("Yara", "Yy", "")):
            r = c.post("/fsrs/new", data=dict(first_name=first, last_name=last, email=f"{last}@u.edu", cnet=cnet, csrf_token=t))
            self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:300])
        for fid, first, last, email, cnet, fsr in ((1, "Niels", "Bohr", "Bohr@u.edu", "nbohr", 1), (2, "Marie", "Curie", "Curie@u.edu", "mcurie", 2)):
            r = c.post(f"/faculty/{fid}/edit", data=dict(first_name=first, last_name=last, email=email, cnet=cnet,
                       fsr_id=fsr, csrf_token=t))
            self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:300])
        return c, t

    def test_fsr_records_and_faculty_link(self):
        c, t = self._fsr_world()
        form = c.get("/fsrs/new").get_data(as_text=True)
        for label in ("First name", "Last name", "Email", "CNET"):
            self.assertIn(f">{label}", form)
        self.assertEqual(re.findall(r'name="(\w+)"', form.split('<form method="post" class="form"')[1].split("</form>")[0])[1:5],
                         ["first_name", "last_name", "email", "cnet"])
        # CNET is stored lowercase; a duplicate email or CNET is refused with a message, not an error page
        dup = c.post("/fsrs/new", data=dict(first_name="A", last_name="B", email="Xx@u.edu", cnet="", csrf_token=t))
        self.assertEqual(dup.status_code, 200); self.assertIn("already has this email", dup.get_data(as_text=True))
        dup = c.post("/fsrs/new", data=dict(first_name="A", last_name="B", email="new@u.edu", cnet="fsrx", csrf_token=t))
        self.assertIn("already has this CNET", dup.get_data(as_text=True))
        conn = physdb.connect(self.app.config["DB_PATH"])
        self.assertEqual(conn.execute("SELECT cnet FROM fsrs WHERE id = 1").fetchone()[0], "fsrx"); conn.close()
        # the faculty form has a pull-down of FSRs (blank allowed), with the current one selected
        edit = c.get("/faculty/1/edit").get_data(as_text=True)
        sel = edit.split('name="fsr_id"')[1].split("</select>")[0]
        self.assertEqual(re.findall(r'<option value="(\d*)"[^>]*>([^<]*)</option>', sel), [("", ""), ("1", "Xx, Xavier"), ("2", "Yy, Yara")])
        self.assertIn('<option value="1" selected>', sel)
        r = c.post("/faculty/1/edit", data=dict(first_name="Niels", last_name="Bohr", email="Bohr@u.edu", cnet="nbohr", fsr_id="99", csrf_token=t))
        self.assertEqual(r.status_code, 200)                                       # not a real FSR
        # list page: faculty names are links to their summary pages, and the column sorts
        c.post("/faculty/3/edit", data=dict(first_name="Max", last_name="Planck", email="Planck@u.edu", cnet="", fsr_id="1", csrf_token=t))
        page = c.get("/fsrs").get_data(as_text=True)
        row = re.search(r"<tr><td><a [^>]*>Xx, Xavier</a></td>(.*?)</tr>", page, re.S).group(1)
        self.assertEqual(re.findall(r'<a href="(/faculty/\d+)">([^<]*)</a>', row), [("/faculty/1", "Bohr"), ("/faculty/3", "Planck")])
        self.assertIn("fsrx", row)
        names = lambda q: re.findall(r"<tr><td><a [^>]*>(\w+), \w+</a></td>", c.get("/fsrs" + q).get_data(as_text=True))
        self.assertEqual(names(""), ["Xx", "Yy"]); self.assertEqual(names("?sort=name&dir=desc"), ["Yy", "Xx"])
        self.assertEqual(names("?sort=faculty"), ["Xx", "Yy"])                     # Bohr < Curie
        # faculty list and summary page show the FSR
        self.assertIn("Xx, Xavier", c.get("/faculty").get_data(as_text=True))
        detail = c.get("/faculty/1").get_data(as_text=True)
        self.assertIn('<a href="/fsrs/1">Xavier Xx</a>', detail)
        # viewers can look but not change
        v = self.client("vw")
        self.assertEqual(v.get("/fsrs").status_code, 200); self.assertEqual(v.get("/fsrs/1").status_code, 200)
        self.assertNotIn("+ Add FSR", v.get("/fsrs").get_data(as_text=True))
        self.assertEqual(v.get("/fsrs/new").status_code, 403); self.assertEqual(v.get("/fsrs/1/edit").status_code, 403)
        self.assertEqual(v.post("/fsrs/1/delete", data={"csrf_token": self.token(v)}).status_code, 403)
        # deleting an FSR leaves their faculty without one
        r = c.post("/fsrs/1/delete", data={"csrf_token": t}); self.assertEqual(r.status_code, 302)
        conn = physdb.connect(self.app.config["DB_PATH"])
        self.assertEqual([x[0] for x in conn.execute("SELECT fsr_id FROM faculty ORDER BY id")], [None, 2, None]); conn.close()

    def test_graduates_leave_fsr_pages_but_stay_on_faculty_pages(self):
        from unittest import mock
        c, t = self._fsr_world()                       # Aa, Bb are Bohr's advisees; Bohr's FSR is Xx
        def grad(sid, last, year, term):
            r = c.post(f"/students/{sid}/edit", data=dict(first_name="Stu", last_name=last, university_id=f"UID{sid}",
                       cnet=f"stu{last.lower()}", email=f"s{sid}@u.edu", citizenship="citizen", status="graduated",
                       matriculation_year="2022", matriculation_term="Autumn", graduation_year=year, graduation_term=term, csrf_token=t))
            self.assertEqual(r.status_code, 302)
        grad(1, "Aa", "2026", "Summer")
        names = lambda path, now: re.findall(r'/students/\d+">(\w+), Stu', self._at(now, c, path).split("<h2>Thesis Committees")[0]) \
            if path.startswith("/fsrs") or path.startswith("/faculty") else None
        for now in (("Summer", 2026), ("Autumn", 2026)):
            self.assertEqual(names("/fsrs/1", now), ["Aa", "Bb"], now)
        self.assertEqual(names("/fsrs/1", ("Winter", 2027)), ["Bb"])               # gone at the start of Winter 2027
        self.assertEqual(names("/faculty/1", ("Winter", 2027)), ["Aa", "Bb"])      # faculty pages keep graduates
        self.assertEqual(names("/faculty/1", ("Autumn", 2040)), ["Aa", "Bb"])
        # the FSR's own page behaves the same, and the heading and total ignore the one who left
        self._make_fsr_login(c, t); f = self._fsr_client()
        def mine(now):
            with mock.patch.object(physdb, "current_term", return_value=now):
                return f.get("/fsr").get_data(as_text=True)
        self.assertIn("Aa, Stu", mine(("Autumn", 2026))); self.assertNotIn("Aa, Stu", mine(("Winter", 2027)))
        self.assertIn('(1 active)', mine(("Winter", 2027)))

    # ---- importing students from a spreadsheet ---------------------------------------------------------
    EXAMPLE = "examples/Incoming_Student_Data_Test.xlsx"

    def _import_setup(self):
        import tempfile
        from unittest import mock
        p = mock.patch.object(tempfile, "tempdir", self.tmp); p.start(); self.addCleanup(p.stop)   # uploads land in the test's folder
        c = self.client("ed")
        return c, self.token(c, "/students/import")

    def _upload(self, c, t, data, name="f.xlsx"):
        import io
        return c.post("/students/import", data=dict(csrf_token=t, file=(io.BytesIO(data), name)), content_type="multipart/form-data")

    def _count(self, sql="SELECT count(*) FROM students"):
        conn = physdb.connect(self.app.config["DB_PATH"])
        try:
            return conn.execute(sql).fetchone()[0]
        finally:
            conn.close()

    def _example(self):
        return open(self.EXAMPLE, "rb").read()

    def test_import_students_preview_then_confirm(self):
        import os
        c, t = self._import_setup()
        page = c.get("/students/import").get_data(as_text=True)
        self.assertIn('type="file"', page); self.assertIn("exactly 8 digits", page)
        self.assertIn("Import from file", c.get("/students").get_data(as_text=True))
        # the preview saves nothing
        r = self._upload(c, t, self._example()); html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Nothing has been saved yet.", html); self.assertIn("7 new", html)
        self.assertIn("Feynman, Richard", html); self.assertIn("Autumn 2026", html)
        self.assertIn("Import 7 new students", html)
        self.assertEqual(self._count(), 0)
        # confirming adds them, all active, with the transfer terms
        r = c.post("/students/import/confirm", data={"csrf_token": t}); html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200); self.assertIn("Imported 7 new students.", html)
        self.assertEqual(self._count(), 7)
        self.assertEqual(self._count("SELECT count(*) FROM students WHERE status = 'active'"), 7)
        self.assertEqual(self._count("SELECT count(*) FROM students WHERE transfer_term = 'Autumn' AND transfer_year = 2026"), 3)
        self.assertEqual(self._count("SELECT count(*) FROM students WHERE citizenship = 'international'"), 4)
        self.assertEqual(self._count("SELECT count(*) FROM students WHERE cnet = 'wolf' AND matriculation_year = 2024"), 1)
        self.assertEqual(os.listdir(os.path.join(self.tmp, "physdb_imports")), [])             # the uploaded copy is deleted
        self.assertEqual(c.post("/students/import/confirm", data={"csrf_token": t}).status_code, 302)   # no double import
        self.assertEqual(self._count(), 7)
        self.assertIn("Dirac, Paul", c.get("/students").get_data(as_text=True))
        # the same file again: everyone is already there, so nothing is offered and nothing changes
        r = self._upload(c, t, self._example()); html = r.get_data(as_text=True)
        self.assertIn("0 new", html); self.assertIn("7 already in the database", html)
        self.assertIn("already in the database and so will not be imported", html)
        self.assertIn("left exactly as they are", html); self.assertNotIn("Import 0", html); self.assertIn("nothing new to import", html)
        self.assertNotIn("Import 7", html)
        c.post("/students/import/cancel", data={"csrf_token": t})

    def test_import_mixed_file_skips_duplicates_and_problems(self):
        from test_import import HEADER, good, xlsx
        c, t = self._import_setup()
        self.add_student(c, university_id="10000001", first_name="Existing", last_name="Student", email="exist@u.edu", research_area="keep me")
        rows = [HEADER, good(1, **{"First Name": "Changed"}), good(2), good(3, UCID=123), good(4, Email="exist@u.edu"),
                good(5, Email="<b>x</b>@u.edu", **{"Last Name": "<script>alert(1)</script>"}), good(6)]
        html = self._upload(c, t, xlsx(rows)).get_data(as_text=True)
        self.assertIn("3 new", html); self.assertIn("1 already in the database", html); self.assertIn("2 with problems", html)
        dup = html.split("Already in the database <")[1].split("<h2>")[0]
        self.assertIn("Lovelace1, Changed", dup); self.assertIn("Student, Existing", dup)
        self.assertIn("will be left exactly as they are", dup)
        probs = html.split("Problems <")[1]
        self.assertIn("UCID must be exactly 8 digits (found &#39;123&#39;)", probs)
        self.assertIn("already belongs to a different student", probs); self.assertIn("Student, Existing", probs)
        self.assertNotIn("<script>alert", html); self.assertIn("&lt;script&gt;", html)           # file contents are escaped
        r = c.post("/students/import/confirm", data={"csrf_token": t}); html = r.get_data(as_text=True)
        self.assertIn("Imported 3 new students.", html)
        self.assertEqual(self._count(), 4)
        self.assertEqual(self._count("SELECT count(*) FROM students WHERE first_name = 'Existing' AND research_area = 'keep me'"), 1)
        self.assertEqual(self._count("SELECT count(*) FROM students WHERE first_name = 'Changed'"), 0)       # duplicates are not updated

    def test_import_is_rechecked_at_confirm_and_all_or_nothing(self):
        import sqlite3
        from unittest import mock
        from test_import import HEADER, good, xlsx
        c, t = self._import_setup()
        self._upload(c, t, xlsx([HEADER, good(1), good(2), good(3)]))
        self.add_student(c, university_id="10000002", first_name="Late", last_name="Arrival", email="late@u.edu")   # appears meanwhile
        html = c.post("/students/import/confirm", data={"csrf_token": t}).get_data(as_text=True)
        self.assertIn("Imported 2 new students.", html); self.assertIn("1 was already in the database", html)
        self.assertEqual(self._count(), 3)
        # a failure part-way through leaves nothing behind
        self._upload(c, t, xlsx([HEADER, good(4), good(5), good(6)]))
        real = physdb.insert_student; calls = []
        def flaky(conn, **f):
            calls.append(1)
            if len(calls) == 3:
                raise sqlite3.IntegrityError("boom")
            return real(conn, **f)
        with mock.patch.object(physdb, "insert_student", flaky):
            r = c.post("/students/import/confirm", data={"csrf_token": t})
        self.assertEqual(r.status_code, 302)
        self.assertIn("Nothing was imported", c.get("/students/import").get_data(as_text=True))
        self.assertEqual(self._count(), 3)

    def test_import_cancel_expiry_and_bad_files(self):
        import os
        c, t = self._import_setup()
        self.assertEqual(c.post("/students/import/confirm", data={"csrf_token": t}).status_code, 302)       # nothing uploaded
        self.assertIn("expired", c.get("/students/import").get_data(as_text=True))
        self._upload(c, t, self._example())
        self.assertEqual(len(os.listdir(os.path.join(self.tmp, "physdb_imports"))), 1)
        r = c.post("/students/import/cancel", data={"csrf_token": t}); self.assertEqual(r.status_code, 302)
        self.assertEqual(os.listdir(os.path.join(self.tmp, "physdb_imports")), []); self.assertEqual(self._count(), 0)
        self.assertEqual(c.post("/students/import/confirm", data={"csrf_token": t}).status_code, 302)       # cancelled: gone
        # wrong kinds of file give a message, not an error page
        for data, name, word in ((b"hello", "notes.txt.pdf", "upload an Excel"), (b"junk", "f.xlsx", "valid Excel"),
                                 (b"a,b\n1,2\n", "f.csv", "were not found")):
            r = self._upload(c, t, data, name)
            self.assertEqual(r.status_code, 302, name)
            self.assertIn(word, c.get("/students/import").get_data(as_text=True))
        r = c.post("/students/import", data=dict(csrf_token=t)); self.assertEqual(r.status_code, 302)       # no file chosen
        self.assertIn("Choose an Excel", c.get("/students/import").get_data(as_text=True))
        big = self._upload(c, t, b"x" * (6 * 1024 * 1024), "big.csv")
        self.assertEqual(big.status_code, 413); self.assertIn("too large", big.get_data(as_text=True))
        template = c.get("/students/import/template.csv")
        self.assertEqual(template.status_code, 200); self.assertIn("UCID", template.get_data(as_text=True))

    def test_import_is_for_editors_and_needs_the_csrf_token(self):
        import io
        c, t = self._import_setup()
        files = lambda: dict(file=(io.BytesIO(self._example()), "f.xlsx"))
        r = c.post("/students/import", data=files(), content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)                                                                 # no CSRF token
        v = self.client("vw"); vt = self.token(v)
        for method, path in (("get", "/students/import"), ("get", "/students/import/template.csv")):
            self.assertEqual(getattr(v, method)(path).status_code, 403, path)
        for path in ("/students/import", "/students/import/confirm", "/students/import/cancel"):
            self.assertEqual(v.post(path, data={"csrf_token": vt}).status_code, 403, path)
        self.assertNotIn("Import from file", v.get("/students").get_data(as_text=True))
        # faculty and FSR logins cannot reach it either
        a, at = self._fsr_world()
        self._make_faculty_login(a, at); fac = self._faculty_client()
        self.assertEqual(fac.get("/students/import").status_code, 403)
        self._make_fsr_login(a, at); fsr = self._fsr_client()
        self.assertEqual(fsr.get("/students/import").status_code, 403)
        self.assertEqual(self.app.test_client().get("/students/import").status_code, 302)                    # signed out: login page

    def test_transfer_checkbox_with_year_and_term(self):
        c = self.client("ed"); t = self.token(c)
        form = c.get("/students/new").get_data(as_text=True)
        self.assertRegex(form, r'<input type="checkbox" id="is_transfer" name="is_transfer" value="1" >? ?')
        self.assertNotIn(" checked", form.split('id="is_transfer"')[1].split(">")[0])
        self.assertEqual(form.count('data-show-when="is_transfer=checked"'), 2)         # year and term, shown when ticked
        labels = re.findall(r"<label for=\"(\w+)\">|<label class=\"check\">.*?(Transfer)", form, re.S)
        self.assertLess(form.index('id="is_transfer"'), form.index('id="transfer_year"'))
        self.assertLess(form.index('id="matriculation_term"'), form.index('id="is_transfer"'))
        base = dict(first_name="Ada", last_name="Lovelace", university_id="U1", email="ada@u.edu", citizenship="citizen",
                    status="active", matriculation_year="2022", matriculation_term="Autumn", csrf_token=t)
        # ticked without a term: refused, and the box stays ticked
        r = c.post("/students/new", data={**base, "is_transfer": "1"}); html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Choose the term they transferred.", html); self.assertIn("Enter the year they transferred.", html)
        self.assertIn("checked", html.split('id="is_transfer"')[1].split(">")[0])
        r = c.post("/students/new", data={**base, "is_transfer": "1", "transfer_year": "2023", "transfer_term": "Winter"})
        self.assertEqual(r.status_code, 302)
        page = c.get("/students/1").get_data(as_text=True)
        self.assertIn("<dt>Transfer</dt><dd>Winter 2023</dd>", page)
        self.assertIn("Autumn 2022", page)                                                    # matriculation is separate
        edit = c.get("/students/1/edit").get_data(as_text=True)
        self.assertIn("checked", edit.split('id="is_transfer"')[1].split(">")[0])
        self.assertIn('<option value="Winter" selected>', edit.split('name="transfer_term"')[1]); self.assertIn('value="2023"', edit)
        # unticked clears the term, even if the boxes still had values
        post = lambda **kw: c.post("/students/1/edit", data={**base, **kw})
        self.assertEqual(post(transfer_year="2023", transfer_term="Winter").status_code, 302)
        conn = physdb.connect(self.app.config["DB_PATH"])
        self.assertEqual(tuple(conn.execute("SELECT transfer_year, transfer_term FROM students").fetchone()), (None, None)); conn.close()
        self.assertNotIn("<dt>Transfer</dt>", c.get("/students/1").get_data(as_text=True))
        csv = c.get("/export.csv").get_data(as_text=True).splitlines()[0]
        self.assertIn("transfer_year", csv); self.assertIn("transfer_term", csv)

    def test_withdrawal_term_is_asked_for_shown_and_hides_students_on_fsr_pages(self):
        c, t = self._fsr_world()
        form = c.get("/students/1/edit").get_data(as_text=True)
        self.assertIn('data-show-when="status=withdrawn"', form)
        self.assertIn("Withdrawal term", form); self.assertIn("Withdrawal year", form)
        base = dict(first_name="Stu", last_name="Aa", university_id="UID1", cnet="stuaa", email="s1@u.edu", citizenship="citizen",
                    matriculation_year="2022", matriculation_term="Autumn", csrf_token=t)
        post = lambda **kw: c.post("/students/1/edit", data={**base, **kw})
        r = post(status="withdrawn"); html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Choose the term they left.", html); self.assertIn("Enter the year they left.", html)
        self.assertIn("Enter the year they left.", post(status="withdrawn", withdrawal_term="Winter").get_data(as_text=True))
        # a graduation term does not satisfy a withdrawal, and is cleared
        self.assertEqual(post(status="withdrawn", graduation_year="2026", graduation_term="Spring").status_code, 200)
        self.assertEqual(post(status="withdrawn", withdrawal_year="2026", withdrawal_term="Winter",
                              graduation_year="2020", graduation_term="Spring").status_code, 302)
        conn = physdb.connect(self.app.config["DB_PATH"])
        self.assertEqual(tuple(conn.execute("SELECT graduation_year, graduation_term, withdrawal_year, withdrawal_term FROM students WHERE id = 1").fetchone()),
                         (None, None, 2026, "Winter")); conn.close()
        self.assertIn("<dt>Withdrew</dt><dd>Winter 2026</dd>", c.get("/students/1").get_data(as_text=True))
        # the Students tab has no Withdrawn column (rare), though the status itself shows
        page = c.get("/students").get_data(as_text=True)
        self.assertNotIn("Withdrawn</a>", page.split("</tr>")[0]); self.assertNotIn("sort=withdrawn", page)
        self.assertIn("Withdrawn", re.search(r"<tr><td><a [^>]*>Aa, Stu</a>.*?</tr>", page, re.S).group(0))      # the status badge
        # FSR pages: listed through Spring 2026 (two terms after Winter 2026 is Summer), faculty pages keep them
        names = lambda path, now: re.findall(r'/students/\d+">(\w+), Stu', self._at(now, c, path).split("<h2>Thesis Committees")[0])
        self.assertEqual(names("/fsrs/1", ("Spring", 2026)), ["Aa", "Bb"])
        self.assertEqual(names("/fsrs/1", ("Summer", 2026)), ["Bb"])
        self.assertEqual(names("/faculty/1", ("Autumn", 2040)), ["Aa", "Bb"])
        # status back to active clears the term
        self.assertEqual(post(status="active", withdrawal_year="2026", withdrawal_term="Winter").status_code, 302)
        self.assertNotIn("<dt>Withdrew</dt>", c.get("/students/1").get_data(as_text=True))

    def _at(self, now, client, path):
        from unittest import mock
        with mock.patch.object(physdb, "current_term", return_value=now):
            return client.get(path).get_data(as_text=True)

    def _make_fsr_login(self, admin, t):
        r = admin.post("/users/new-fsr", data=dict(fsr_id=1, password="temp-password-1", csrf_token=t))
        self.assertEqual(r.status_code, 302)

    def _fsr_client(self):
        f = self.app.test_client()
        f.post("/login", data={"username": "fsrx", "password": "temp-password-1"})
        nt = self.token(f, "/account")
        f.post("/account", data=dict(current_password="temp-password-1", new_password="fsr-new-pass-1", confirm="fsr-new-pass-1", csrf_token=nt))
        return f

    def test_fsr_login_sees_only_their_faculty_advisees(self):
        from unittest import mock
        c, t = self._fsr_world()
        form = c.get("/users/new-fsr").get_data(as_text=True)
        self.assertIn("Xx, Xavier (fsrx)", form); self.assertNotIn("Yy, Yara", form)    # only FSRs with a CNET
        self.assertIn("Yara Yy", form)                                                   # ...and who is missing is explained
        r = c.post("/users/new-fsr", data=dict(fsr_id=1, password="temp-password-1", csrf_token=t)); self.assertEqual(r.status_code, 302)
        r = c.post("/users/new-fsr", data=dict(fsr_id=1, password="temp-password-2", csrf_token=t)); self.assertEqual(r.status_code, 200)
        users = c.get("/users").get_data(as_text=True)
        self.assertIn("FSR", users); self.assertIn("Xavier Xx", users); self.assertIn("+ Add FSR account", users)
        conn = physdb.connect(self.app.config["DB_PATH"])
        self.assertEqual(tuple(conn.execute("SELECT username, role, faculty_id, fsr_id FROM users WHERE fsr_id = 1").fetchone()),
                         ("fsrx", "fsr", None, 1)); conn.close()
        f = self.app.test_client()
        r = f.post("/login?next=/students", data={"username": "fsrx", "password": "temp-password-1"})
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/fsr"))
        self.assertEqual(f.get("/fsr").headers["Location"], "/account")                  # must choose a password first
        nt = self.token(f, "/account")
        f.post("/account", data=dict(current_password="temp-password-1", new_password="fsr-new-pass-1", confirm="fsr-new-pass-1", csrf_token=nt))
        def page(q="", client=f):
            with mock.patch.object(physdb, "current_term", return_value=("Autumn", 2026)):
                return client.get("/fsr" + q).get_data(as_text=True)
        html = page()
        self.assertIn("<h1>My students</h1>", html); self.assertIn("faculty: Bohr</p>", html)
        head = html.split("</tr>")[0].split('<table class="nowrap">')[-1]
        headers = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>|[▲▼↕]", "", h)).strip() for h in re.findall(r"<th[^>]*>(.*?)</th>", head, re.S)]
        self.assertEqual(headers, ["Name", "Advisor", "Status", "Clock", "Graduated", "Research area", "Funding, Autumn 2026"])
        self.assertEqual(head.count('class="sortlink'), 7)                                 # every heading sorts
        who = lambda h: re.findall(r"<tr><td[^>]*>(\w+), Stu</td>", h)
        self.assertEqual(who(html), ["Aa", "Bb"])        # Bohr's advisor/co-advisor students; not committee-only, not the ended one
        self.assertIn("RA · NSF grant · 1234567-ABC · $3,000", html); self.assertIn("Supplement · $250", html)
        self.assertNotIn("NOT-BOHRS", html)
        for hidden in ("UID1", "SECRET", "s1@u.edu"):
            self.assertNotIn(hidden, html)
        self.assertNotIn("/students/", html); self.assertNotIn(">Clear<", html); self.assertNotIn("data-copy", html)
        self.assertIn('<h2>Students I support <span class="muted">(2 active)</span></h2>', html)
        self.assertIn("Winter 2027", html)                                                # term stepper
        self.assertNotIn("Winter 2026</strong>", html)
        # a second faculty member joins this FSR: their advisees appear too (advisor column names who advises)
        c.post("/faculty/2/edit", data=dict(first_name="Marie", last_name="Curie", email="Curie@u.edu", cnet="mcurie", fsr_id="1", csrf_token=t))
        html = page()
        self.assertEqual(who(html), ["Aa", "Bb", "Cc", "Dd", "Ee"])
        self.assertIn("faculty: Bohr, Curie</p>", html); self.assertIn("NOT-BOHRS", html)
        self.assertIn("Winter 2026</strong>", page("?year=2026&season=Winter"))
        # every column sorts (blank values last, whichever way), and Prev / Next keep the sorting
        self.assertEqual(who(page("?sort=name&dir=desc")), ["Ee", "Dd", "Cc", "Bb", "Aa"])
        adv = lambda q: [re.sub(r"<[^>]+>", "", m) for m in re.findall(r"</td>\n<td>([^<]*)</td>\n<td><span", page(q))]
        self.assertEqual(adv("?sort=advisor"), ["Bohr"] * 2 + ["Curie"] * 3)
        self.assertEqual(adv("?sort=advisor&dir=desc"), ["Curie"] * 3 + ["Bohr"] * 2)
        total = lambda q: who(page(q))
        # funding for Autumn 2026: Aa $3,000, Bb $250, Ee $999, Cc/Dd none -> blanks last both ways
        self.assertEqual(total("?sort=funding")[:3], ["Bb", "Ee", "Aa"]); self.assertEqual(sorted(total("?sort=funding")[3:]), ["Cc", "Dd"])
        self.assertEqual(total("?sort=funding&dir=desc")[:3], ["Aa", "Ee", "Bb"]); self.assertEqual(sorted(total("?sort=funding&dir=desc")[3:]), ["Cc", "Dd"])
        self.assertEqual(sorted(who(page("?sort=clock"))), ["Aa", "Bb", "Cc", "Dd", "Ee"])
        for key in ("status", "clock", "graduated", "research_area"):
            self.assertEqual(sorted(who(page(f"?sort={key}&dir=desc"))), ["Aa", "Bb", "Cc", "Dd", "Ee"], key)
        self.assertEqual(who(page("?sort=research_area&dir=desc")), ["Ee", "Dd", "Cc", "Bb", "Aa"])   # area-Ee ... area-Aa
        self.assertEqual(who(page("?sort=bogus")), ["Aa", "Bb", "Cc", "Dd", "Ee"])               # unknown column: by name
        stepped = page("?sort=funding&dir=desc")
        self.assertRegex(stepped, r'href="/fsr\?year=2027&amp;season=Winter&amp;sort=funding&amp;dir=desc"')
        self.assertIn('href="/fsr?sort=funding&amp;dir=asc"', stepped)                           # clicking the active heading reverses
        self.assertIn('href="/fsr?year=2026&amp;season=Winter&amp;sort=advisor&amp;dir=asc"', page("?year=2026&season=Winter"))   # sorting keeps the term
        # one funding entry per line, none wrapping
        self.assertIn('<table class="nowrap">', stepped)
        self.assertIn("table.nowrap td", self.app.test_client().get("/static/style.css").get_data(as_text=True))
        # nothing else is reachable, and nothing can be changed
        for path in ("/students", "/students/1", "/faculty", "/faculty/1", "/fsrs", "/fsrs/1", "/users", "/reports", "/my",
                     "/my/students/1", "/students/1/funding/new", "/students/1/funding/copy", "/reports/funding", "/fsrs/new"):
            self.assertEqual(f.get(path).status_code, 403, path)
        nt = self.token(f, "/account")
        for path in ("/students/1/funding/1/delete", "/fsrs/1/delete", "/faculty/1/delete", "/students/new"):
            self.assertEqual(f.post(path, data={"csrf_token": nt}).status_code, 403, path)
        # staff pages for faculty logins/staff stay closed to the FSR, and a faculty login can't open /fsr
        self._make_faculty_login(c, t)
        fac = self._faculty_client()
        self.assertEqual(fac.get("/fsr").status_code, 403); self.assertEqual(fac.get("/fsrs").status_code, 403)
        self.assertEqual(self.client("ed").get("/fsr").status_code, 403)
        # changing the CNET renames the login; removing it is refused while the login exists
        r = c.post("/fsrs/1/edit", data=dict(first_name="Xavier", last_name="Xx", email="Xx@u.edu", cnet="xnew", csrf_token=t))
        self.assertEqual(r.status_code, 302)
        conn = physdb.connect(self.app.config["DB_PATH"])
        self.assertEqual(conn.execute("SELECT username FROM users WHERE fsr_id = 1").fetchone()[0], "xnew"); conn.close()
        r = c.post("/fsrs/1/edit", data=dict(first_name="Xavier", last_name="Xx", email="Xx@u.edu", cnet="", csrf_token=t))
        self.assertEqual(r.status_code, 200); self.assertIn("login account", r.get_data(as_text=True))
        # the staff page of an FSR shows the same table, with links to students and the funding buttons
        with mock.patch.object(physdb, "current_term", return_value=("Autumn", 2026)):
            sp = c.get("/fsrs/1").get_data(as_text=True)
        self.assertEqual(re.findall(r'<a href="/students/(\d+)">(\w+), Stu</a>', sp), [("1", "Aa"), ("2", "Bb"), ("3", "Cc"), ("4", "Dd"), ("5", "Ee")])
        self.assertIn(">Clear</button>", sp)
        # deleting the FSR removes their login too
        c.post("/fsrs/1/delete", data={"csrf_token": t})
        conn = physdb.connect(self.app.config["DB_PATH"])
        self.assertEqual(conn.execute("SELECT count(*) FROM users WHERE role = 'fsr'").fetchone()[0], 0); conn.close()

    # ---- candidacy form approvals ------------------------------------------------------------------------
    ALL6 = ["cm", "qm1", "qm2", "em1", "em2", "sm"]

    def _approval_world(self):
        """Admin client + a student (id 1) + an approver 'appr' and a chair 'chr' who have chosen their own passwords."""
        c = self.client("ad"); t = self.token(c)
        self.add_student(c)
        out = [c, t]
        for username, role, name in (("appr", "approver", "Ann Approver"), ("chr", "chair", "Carl Chair")):
            r = c.post("/users/new", data=dict(username=username, role=role, full_name=name, email=f"{username}@u.edu", password="temp-password-1", csrf_token=t))
            self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:300])
            k = self.app.test_client()
            self.assertEqual(k.post("/login", data={"username": username, "password": "temp-password-1"}).status_code, 302)
            nt = self.token(k, "/account")
            k.post("/account", data=dict(current_password="temp-password-1", new_password="my-own-pass-1", confirm="my-own-pass-1", csrf_token=nt))
            out.append(k)
        return out

    def _sig_upload(self, chair, name="sig.png", data=None):
        import io
        from test_forms import signature_png
        return chair.post("/signatures", data=dict(csrf_token=self.token(chair, "/signatures"),
                          image=(io.BytesIO(data if data is not None else signature_png()), name)), content_type="multipart/form-data")

    def _forms(self):
        conn = physdb.connect(self.app.config["DB_PATH"])
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM forms ORDER BY id")]
        finally:
            conn.close()

    def test_accounts_for_approvers_and_the_chair(self):
        c, t, appr, chair = self._approval_world()
        page = c.get("/users/new").get_data(as_text=True)
        self.assertIn('<option value="approver"', page); self.assertIn('<option value="chair"', page); self.assertIn("Full name", page)
        r = c.post("/users/new", data=dict(username="noname", role="chair", full_name="", email="", password="temp-password-1", csrf_token=t))
        self.assertEqual(r.status_code, 200); self.assertIn("Enter their full name", r.get_data(as_text=True))      # a name is required
        self.assertIn("Enter their email address", r.get_data(as_text=True))                                         # and an email
        r = c.post("/users/new", data=dict(username="noname", role="approver", full_name="N", email="not-an-email", password="temp-password-1", csrf_token=t))
        self.assertEqual(r.status_code, 200); self.assertIn("valid email", r.get_data(as_text=True))
        r = c.post("/users/new", data=dict(username="vw2", role="viewer", full_name="", password="temp-password-1", csrf_token=t))
        self.assertEqual(r.status_code, 302)                                                                         # but not for others
        users = c.get("/users").get_data(as_text=True)
        self.assertIn("Approver", users); self.assertIn("Chair", users); self.assertIn("Ann Approver", users); self.assertIn("Carl Chair", users)
        self.assertIn("appr@u.edu", users); self.assertIn("chr@u.edu", users)
        # they land on the approvals page, and only see that
        k = self.app.test_client()
        r = k.post("/login?next=/students", data={"username": "appr", "password": "my-own-pass-1"})
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/students"))                # they land where editors do
        self.assertEqual(k.get("/").headers["Location"], "/students")
        home = appr.get("/students").get_data(as_text=True)
        self.assertIn("(approver)", home); self.assertIn(">Students</a>", home); self.assertNotIn(">Signatures", home); self.assertNotIn(">Accounts<", home)
        chome = chair.get("/students").get_data(as_text=True)
        self.assertIn(">Signatures", chome); self.assertIn("(chair)", chome); self.assertIn(">Accounts<", chome)
        # changing a chair to another role removes their stored signature
        self._sig_upload(chair)
        conn = physdb.connect(self.app.config["DB_PATH"]); uid = conn.execute("SELECT id FROM users WHERE username = 'chr'").fetchone()[0]
        self.assertEqual(conn.execute("SELECT count(*) FROM signatures").fetchone()[0], 1); conn.close()
        r = c.post(f"/users/{uid}/edit", data=dict(role="editor", full_name="Carl Chair", email="chr@u.edu", password="", csrf_token=t)); self.assertEqual(r.status_code, 302)
        conn = physdb.connect(self.app.config["DB_PATH"])
        self.assertEqual(conn.execute("SELECT count(*) FROM signatures").fetchone()[0], 0); conn.close()

    def test_sixth_course_starts_the_form_and_the_whole_approval_path(self):
        import datetime
        c, t, appr, chair = self._approval_world()
        save = lambda codes: " | ".join(re.findall(r'<div class="flash [^"]*">(.*?)</div>', c.post("/students/1/courses", data={"course": codes, "csrf_token": t}, follow_redirects=True).get_data(as_text=True), re.S))
        self.assertNotIn("candidacy form", save(self.ALL6[:5]))
        self.assertEqual(self._forms(), [])
        page = save(self.ALL6)
        self.assertIn("All six core courses are complete, so the candidacy form was filled in and is waiting for an approver.", page)
        f = self._forms()[0]
        self.assertEqual((f["status"], f["created_by"], f["kind"]), ("pending_approval", "ad", "candidacy"))
        student = c.get("/students/1").get_data(as_text=True)
        forms = student.split("<h2>Forms")[1].split("<h2>Funding")[0]
        self.assertIn("Waiting for an approver", forms); self.assertIn("View PDF", forms)
        self.assertNotIn("Start candidacy form", forms)                                          # one is already waiting
        self.assertEqual(save(self.ALL6).count("candidacy form"), 0); self.assertEqual(len(self._forms()), 1)
        # staff can read the PDF; it is flat, with the student's details
        pdf = c.get(f"/forms/{f['id']}.pdf")
        self.assertEqual((pdf.status_code, pdf.mimetype), (200, "application/pdf")); self.assertTrue(pdf.data.startswith(b"%PDF"))
        self.assertIn("no-store", pdf.headers["Cache-Control"]); self.assertEqual(pdf.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.client("vw").get(f"/forms/{f['id']}.pdf").status_code, 200)
        self.assertEqual(c.get("/forms/999.pdf").status_code, 404)
        # the approver's queue (and the count in the top bar); the chair can approve at this stage too
        q = appr.get("/approvals").get_data(as_text=True)
        self.assertIn("Waiting for approval", q); self.assertIn("Lovelace, Ada", q); self.assertIn("U1", q)
        self.assertRegex(q, r'<span class="count">1</span>')
        self.assertIn(">Approve</button>", chair.get("/approvals").get_data(as_text=True))
        self.assertIn("Nothing is waiting for your signature", chair.get("/signatures").get_data(as_text=True))
        self.assertEqual(appr.get(f"/forms/{f['id']}.pdf").status_code, 200)
        # nobody can sign before the approval, and the approver can never sign or open the Signatures page
        self.assertEqual(appr.post(f"/forms/{f['id']}/sign", data={"csrf_token": self.token(appr, "/approvals")}).status_code, 403)
        self.assertEqual(appr.get("/signatures").status_code, 403)
        r = chair.post(f"/forms/{f['id']}/sign", data={"csrf_token": self.token(chair, "/signatures")}, follow_redirects=True)
        self.assertIn("no longer waiting for the chair", r.get_data(as_text=True)); self.assertEqual(self._forms()[0]["status"], "pending_approval")
        r = appr.post(f"/forms/{f['id']}/approve", data={"csrf_token": self.token(appr, "/approvals")}, follow_redirects=True)
        self.assertIn("now waiting for the chair", r.get_data(as_text=True))
        f = self._forms()[0]; self.assertEqual((f["status"], f["approved_by"]), ("pending_chair", "appr"))
        aq = appr.get("/approvals").get_data(as_text=True)
        self.assertIn("Nothing is waiting for approval", aq); self.assertIn("Waiting for the chair", aq)
        # the chair's Signatures page: a signature image is needed first
        sq = chair.get("/signatures").get_data(as_text=True)
        self.assertIn("Lovelace, Ada", sq); self.assertIn("You have not uploaded a signature image", sq)
        self.assertIn("disabled", sq.split("Approve and sign")[0].rsplit("<button", 1)[1])
        r = chair.post(f"/forms/{f['id']}/sign", data={"csrf_token": self.token(chair, "/signatures")}, follow_redirects=True)
        self.assertIn("Upload your signature image first", r.get_data(as_text=True)); self.assertEqual(self._forms()[0]["status"], "pending_chair")
        self.assertEqual(self._sig_upload(chair).status_code, 302)
        self.assertEqual(chair.get("/signature.png").mimetype, "image/png")
        self.assertIn('/signature.png', chair.get("/signatures").get_data(as_text=True))
        # signing: milestone recorded, form complete and linked from the student's page
        before = self._forms()[0]["pdf"]
        r = chair.post(f"/forms/{f['id']}/sign", data={"csrf_token": self.token(chair, "/signatures")}, follow_redirects=True)
        self.assertIn("Candidacy milestone was recorded", r.get_data(as_text=True))
        f = self._forms()[0]
        self.assertEqual((f["status"], f["signed_by"]), ("complete", "chr")); self.assertNotEqual(f["pdf"], before)
        conn = physdb.connect(self.app.config["DB_PATH"])
        m = conn.execute("SELECT * FROM milestones").fetchall(); conn.close()
        self.assertEqual(len(m), 1)
        self.assertEqual((m[0]["kind"], m[0]["outcome"], m[0]["date"], f["milestone_id"]), ("candidacy", "passed", datetime.date.today().isoformat(), m[0]["id"]))
        student = c.get("/students/1").get_data(as_text=True)
        self.assertIn(f'href="/forms/{f["id"]}.pdf" target="_blank">Candidacy form (PDF)</a>', student.split("<h2>Milestones")[1].split("<h2>Forms")[0])
        forms = student.split("<h2>Forms")[1].split("<h2>Funding")[0]
        self.assertIn("Signed", forms); self.assertIn("appr", forms); self.assertIn("chr", forms)
        self.assertIn("Signed", c.get("/approvals").get_data(as_text=True))
        # nothing can be done twice
        self.assertIn("no longer waiting", chair.post(f"/forms/{f['id']}/sign", data={"csrf_token": self.token(chair, "/signatures")}, follow_redirects=True).get_data(as_text=True))
        self.assertEqual(len(self._forms()), 1)

    def test_rejection_withdrawal_and_restarting_a_form(self):
        c, t, appr, chair = self._approval_world()
        save = lambda codes: " | ".join(re.findall(r'<div class="flash [^"]*">(.*?)</div>', c.post("/students/1/courses", data={"course": codes, "csrf_token": t}, follow_redirects=True).get_data(as_text=True), re.S))
        save(self.ALL6); fid = self._forms()[0]["id"]
        # a reason is required, and the student's page shows it
        r = appr.post(f"/forms/{fid}/reject", data={"reason": " ", "stage": "pending_approval", "csrf_token": self.token(appr, "/approvals")}, follow_redirects=True)
        self.assertIn("give a reason", r.get_data(as_text=True)); self.assertEqual(self._forms()[0]["status"], "pending_approval")
        r = appr.post(f"/forms/{fid}/reject", data={"reason": "Wrong UCID", "stage": "pending_approval", "csrf_token": self.token(appr, "/approvals")}, follow_redirects=True)
        self.assertIn("rejected", r.get_data(as_text=True))
        forms = c.get("/students/1").get_data(as_text=True).split("<h2>Forms")[1].split("<h2>Funding")[0]
        self.assertIn("Rejected", forms); self.assertIn("appr rejected it: Wrong UCID", forms)
        self.assertIn("Wrong UCID", appr.get("/approvals").get_data(as_text=True))                   # in "recently finished"
        # saving the courses again does not start a form (nothing changed), but the button does
        self.assertNotIn("candidacy form", save(self.ALL6)); self.assertEqual(len(self._forms()), 1)
        self.assertIn("+ Start candidacy form", c.get("/students/1").get_data(as_text=True))
        r = c.post("/students/1/forms/candidacy", data={"csrf_token": t}, follow_redirects=True)
        self.assertIn("waiting for an approver", r.get_data(as_text=True)); self.assertEqual(self._forms()[1]["status"], "pending_approval")
        self.assertNotIn("+ Start candidacy form", c.get("/students/1").get_data(as_text=True))
        r = c.post("/students/1/forms/candidacy", data={"csrf_token": t}, follow_redirects=True)           # not while one is waiting
        self.assertIn("already waiting", r.get_data(as_text=True)); self.assertEqual(len(self._forms()), 2)
        # the chair can reject too
        fid = self._forms()[1]["id"]
        appr.post(f"/forms/{fid}/approve", data={"csrf_token": self.token(appr, "/approvals")})
        self.assertEqual(chair.post(f"/forms/{fid}/reject", data={"reason": "Please redo", "stage": "pending_chair", "csrf_token": self.token(chair, "/signatures")}).status_code, 302)
        self.assertEqual((self._forms()[1]["status"], self._forms()[1]["closed_by"]), ("rejected", "chr"))
        # unchecking a course withdraws a form that is waiting
        c.post("/students/1/forms/candidacy", data={"csrf_token": t})
        self.assertEqual(self._forms()[2]["status"], "pending_approval")
        page = save(self.ALL6[:5])
        self.assertIn("was withdrawn", page); self.assertEqual(self._forms()[2]["status"], "withdrawn")
        self.assertEqual(appr.get("/approvals").get_data(as_text=True).count("Nothing is waiting for approval"), 1)
        self.assertEqual(c.post("/students/1/forms/candidacy", data={"csrf_token": t}, follow_redirects=True).get_data(as_text=True).count("all six core courses"), 1)

    def test_advisors_see_the_signed_form_without_the_university_id(self):
        c, t, appr, chair = self._approval_world()
        for first, last, cnet in (("Niels", "Bohr", "nbohr"), ("Marie", "Curie", "mcurie")):
            c.post("/faculty/new", data=dict(first_name=first, last_name=last, email=f"{last}@u.edu", cnet=cnet, csrf_token=t))
        c.post("/students/1/advising/new", data=dict(faculty_id=1, role="advisor", start_date="2024-01-01", csrf_token=t))
        self._make_faculty_login(c, t, fid=1); bohr = self._faculty_client("nbohr")
        self._make_faculty_login(c, t, fid=2, password="temp-password-2"); curie = self._faculty_client("mcurie", "temp-password-2", "faculty-new-pass-2")
        c.post("/students/1/courses", data={"course": self.ALL6, "csrf_token": t})
        fid = self._forms()[0]["id"]
        milestone_line = lambda k: k.get("/my/students/1").get_data(as_text=True).split("<h2>Milestones")[1].split("<h2>Funding")[0]
        # nothing for the advisor until the chair has signed: no milestone yet, and the form is not reachable
        self.assertNotIn("Candidacy form", milestone_line(bohr))
        self.assertEqual(bohr.get(f"/my/forms/{fid}.pdf").status_code, 404)
        appr.post(f"/forms/{fid}/approve", data={"csrf_token": self.token(appr, "/approvals")})
        self.assertEqual(bohr.get(f"/my/forms/{fid}.pdf").status_code, 404)
        self._sig_upload(chair)
        chair.post(f"/forms/{fid}/sign", data={"csrf_token": self.token(chair, "/approvals")})
        # now the Candidacy line has a link, and it opens the signed PDF
        line = milestone_line(bohr)
        self.assertRegex(line, r'(?s)<td>Candidacy</td>.*<a href="/my/forms/%d.pdf" target="_blank">Candidacy form \(PDF\)</a>' % fid)
        pdf = bohr.get(f"/my/forms/{fid}.pdf")
        self.assertEqual((pdf.status_code, pdf.mimetype), (200, "application/pdf")); self.assertTrue(pdf.data.startswith(b"%PDF"))
        self.assertIn("no-store", pdf.headers["Cache-Control"])
        # it is the copy without the University ID, not the one staff keep
        f = self._forms()[0]
        self.assertEqual(pdf.data, f["pdf_faculty"]); self.assertNotEqual(pdf.data, f["pdf"])
        # a faculty member who is not the student's advisor cannot open it, and neither can anyone reach the staff copy
        self.assertNotIn("Candidacy form", curie.get("/my").get_data(as_text=True))
        self.assertEqual(curie.get(f"/my/forms/{fid}.pdf").status_code, 404)
        self.assertEqual(bohr.get(f"/forms/{fid}.pdf").status_code, 403)
        self.assertEqual(bohr.get("/my/forms/999.pdf").status_code, 404)
        self.assertEqual(self.app.test_client().get(f"/my/forms/{fid}.pdf").status_code, 302)           # signed out: login page
        self.assertEqual(appr.get(f"/my/forms/{fid}.pdf").status_code, 403)                              # only faculty logins use this address
        # an advisor whose advising has ended loses access
        conn = physdb.connect(self.app.config["DB_PATH"])
        conn.execute("UPDATE advising SET end_date = '2025-01-01'"); conn.commit(); conn.close()
        self.assertEqual(bohr.get(f"/my/forms/{fid}.pdf").status_code, 404)

    def test_account_emails_and_the_email_notices_panel(self):
        import os
        from unittest import mock
        c, t, appr, chair = self._approval_world()
        form = c.get("/users/new").get_data(as_text=True)
        self.assertIn("Email", form); self.assertIn("Required for an Approver or Chair", form)
        # optional for ordinary accounts (and checked when given)
        ok = lambda **kw: c.post("/users/new", data=dict(username=kw.pop("username"), role="viewer", full_name="", email=kw.pop("email", ""),
                                                          password="temp-password-1", csrf_token=t))
        self.assertEqual(ok(username="viewer1").status_code, 302); self.assertEqual(ok(username="viewer2", email="v2@u.edu").status_code, 302)
        self.assertEqual(ok(username="viewer3", email="nonsense").status_code, 200)
        # editing keeps or changes it
        conn = physdb.connect(self.app.config["DB_PATH"]); uid = conn.execute("SELECT id FROM users WHERE username = 'appr'").fetchone()[0]; conn.close()
        self.assertIn('value="appr@u.edu"', c.get(f"/users/{uid}/edit").get_data(as_text=True))
        r = c.post(f"/users/{uid}/edit", data=dict(role="approver", full_name="Ann Approver", email="", password="", csrf_token=t))
        self.assertEqual(r.status_code, 200); self.assertIn("Enter their email address", r.get_data(as_text=True))
        r = c.post(f"/users/{uid}/edit", data=dict(role="approver", full_name="Ann Approver", email="ann@new.edu", password="", csrf_token=t))
        self.assertEqual(r.status_code, 302); self.assertIn("ann@new.edu", c.get("/users").get_data(as_text=True))
        # faculty and FSR accounts show the email from their own record
        a2, at = self._fsr_world()
        self._make_faculty_login(a2, at)
        self.assertIn("Bohr@u.edu", a2.get("/users").get_data(as_text=True))
        # the panel: off by default, with a way to test once on
        with mock.patch.dict(os.environ, {"PHYSDB_SMTP_HOST": ""}):
            page = c.get("/users").get_data(as_text=True)
            self.assertIn("Email is not set up", page); self.assertNotIn("Send test email", page)
        with mock.patch.dict(os.environ, {"PHYSDB_SMTP_HOST": "mail.example.edu"}):
            page = c.get("/users").get_data(as_text=True)
            self.assertIn("Email is switched on", page); self.assertIn("Send test email", page)
            sent = []
            with mock.patch.object(mailer_module(), "send", lambda to, subject, body: sent.append((to, subject))):
                r = c.post("/users/test-email", data=dict(to="me@u.edu", csrf_token=t), follow_redirects=True)
            self.assertEqual(sent[0][0], "me@u.edu"); self.assertIn("Test email sent to me@u.edu", r.get_data(as_text=True))
            self.assertIn("me@u.edu", c.get("/users").get_data(as_text=True).split("Email notices")[1]); self.assertIn("Sent", c.get("/users").get_data(as_text=True))
            def broken(to, subject, body):
                raise mailer_module().MailError("SMTPAuthenticationError: bad login")
            with mock.patch.object(mailer_module(), "send", broken):
                r = c.post("/users/test-email", data=dict(to="me@u.edu", csrf_token=t), follow_redirects=True)
            self.assertIn("could not be sent: SMTPAuthenticationError: bad login", r.get_data(as_text=True))
            self.assertIn("Failed", c.get("/users").get_data(as_text=True)); self.assertIn("bad login", c.get("/users").get_data(as_text=True))
            r = c.post("/users/test-email", data=dict(to="nonsense", csrf_token=t), follow_redirects=True)
            self.assertIn("Enter an email address", r.get_data(as_text=True))
        # only administrators
        for k in (self.client("ed"), appr):
            self.assertEqual(k.post("/users/test-email", data=dict(to="me@u.edu", csrf_token=self.token(k, "/account"))).status_code, 403)

    # ---- sign-in limits and browser protections -------------------------------------------------------------
    def _try_login(self, client, user, password, **environ):
        return client.post("/login", data={"username": user, "password": password}, environ_base=environ)

    def test_repeated_wrong_passwords_lock_the_username_for_a_while(self):
        from unittest import mock
        c = self.app.test_client()
        clock = [10_000.0]
        with mock.patch.object(webapp.time, "time", lambda: clock[0]):
            for n in range(webapp.LOGIN_MAX_PER_USERNAME):
                r = self._try_login(c, "ed", "wrong-%d" % n)
                self.assertEqual(r.status_code, 200); self.assertIn("Incorrect username or password", r.get_data(as_text=True))
                clock[0] += 10
            # now blocked, even with the right password, and told how long
            r = self._try_login(c, "ed", "pw-ed")
            self.assertEqual(r.status_code, 429); html = r.get_data(as_text=True)
            self.assertIn("Too many failed sign-in attempts. Please wait 15 minutes", html)
            self.assertEqual(c.get("/students").status_code, 302)                                  # not signed in
            clock[0] += 14 * 60                                                                    # still inside the window
            self.assertEqual(self._try_login(c, "ed", "pw-ed").status_code, 429)
            clock[0] += 2 * 60                                                                     # the first failure is now 15+ minutes old
            self.assertEqual(self._try_login(c, "ed", "pw-ed").status_code, 302)                   # allowed again
            self.assertEqual(c.get("/students").status_code, 200)
            # a good sign-in wipes the count: five more failures are needed to lock it again
            c2 = self.app.test_client()
            for n in range(webapp.LOGIN_MAX_PER_USERNAME - 1):
                self._try_login(c2, "ed", "nope")
            self.assertEqual(self._try_login(c2, "ed", "pw-ed").status_code, 302)
            self.assertEqual(self._count("SELECT count(*) FROM login_failures WHERE username = 'ed'"), 0)

    def test_the_lockout_is_per_username_case_insensitive_and_does_not_reveal_which_accounts_exist(self):
        from unittest import mock
        c = self.app.test_client()
        with mock.patch.object(webapp.time, "time", lambda: 50_000.0):
            for n in range(webapp.LOGIN_MAX_PER_USERNAME):
                self._try_login(c, "ED" if n % 2 else "Ed", "bad")                                  # any capitalisation counts as the same name
            self.assertEqual(self._try_login(c, "ed", "pw-ed").status_code, 429)
            self.assertEqual(self._try_login(c, "vw", "pw-vw").status_code, 302)                     # other people are not affected
            for n in range(webapp.LOGIN_MAX_PER_USERNAME):                                           # a name that does not exist locks the same way
                r = self._try_login(c, "nobody", "bad")
                self.assertIn("Incorrect username or password", r.get_data(as_text=True))
            r = self._try_login(c, "nobody", "bad")
            real = self._try_login(c, "ed", "pw-ed")
            self.assertEqual((r.status_code, real.status_code), (429, 429))
            self.assertEqual(re.sub(r"\s+", " ", re.findall(r'<div class="flash error">(.*?)</div>', r.get_data(as_text=True), re.S)[0]),
                             re.sub(r"\s+", " ", re.findall(r'<div class="flash error">(.*?)</div>', real.get_data(as_text=True), re.S)[0]))
            long_name = "x" * 5000
            self.assertEqual(self._try_login(c, long_name, "bad").status_code, 200)                  # absurd input is handled
            self.assertEqual(self._count("SELECT max(length(username)) FROM login_failures"), 80)

    def test_one_address_trying_many_usernames_is_slowed_down_too(self):
        from unittest import mock
        c = self.app.test_client()
        with mock.patch.object(webapp.time, "time", lambda: 70_000.0):
            for n in range(webapp.LOGIN_MAX_PER_ADDRESS):
                self._try_login(c, "guess%d" % n, "bad", REMOTE_ADDR="203.0.113.9")
            r = self._try_login(c, "ed", "pw-ed", REMOTE_ADDR="203.0.113.9")
            self.assertEqual(r.status_code, 429)                                                      # even for a different, real username
            self.assertEqual(self._try_login(c, "ed", "pw-ed", REMOTE_ADDR="203.0.113.10").status_code, 302)   # another address is fine
        self.assertEqual(self._count("SELECT count(*) FROM login_failures WHERE ip = '203.0.113.9'"), webapp.LOGIN_MAX_PER_ADDRESS)

    def test_old_failures_are_cleaned_up_and_proxy_addresses_are_used_when_trusted(self):
        import os
        from unittest import mock
        conn = physdb.connect(self.app.config["DB_PATH"])
        conn.execute("INSERT INTO login_failures VALUES (1.0, 'old', '1.2.3.4')"); conn.commit(); conn.close()
        self.app.test_client().post("/login", data={"username": "x", "password": "y"})
        self.assertEqual(self._count("SELECT count(*) FROM login_failures WHERE username = 'old'"), 0)
        with mock.patch.dict(os.environ, {"PHYSDB_TRUST_PROXY": "1"}):
            app = webapp.create_app(self.app.config["DB_PATH"])
        app.test_client().post("/login", data={"username": "viaproxy", "password": "y"}, headers={"X-Forwarded-For": "198.51.100.7"})
        conn = physdb.connect(self.app.config["DB_PATH"])
        self.assertEqual(conn.execute("SELECT ip FROM login_failures WHERE username = 'viaproxy'").fetchone()[0], "198.51.100.7"); conn.close()
        self.app.test_client().post("/login", data={"username": "direct", "password": "y"}, headers={"X-Forwarded-For": "198.51.100.8"})
        conn = physdb.connect(self.app.config["DB_PATH"])                                           # not trusted by default: the header is ignored
        self.assertEqual(conn.execute("SELECT ip FROM login_failures WHERE username = 'direct'").fetchone()[0], "127.0.0.1"); conn.close()

    def test_browser_protection_headers(self):
        c = self.client("ad"); t = self.token(c)
        self.add_student(c)
        for path in ("/login", "/students", "/students/1", "/reports/milestones", "/users", "/nonexistent"):
            r = (self.app.test_client() if path == "/login" else c).get(path)
            h = r.headers
            self.assertEqual(h["X-Content-Type-Options"], "nosniff", path)
            self.assertEqual(h["X-Frame-Options"], "DENY", path)
            self.assertEqual(h["Referrer-Policy"], "same-origin", path)
            self.assertIn("camera=()", h["Permissions-Policy"]); self.assertEqual(h["Cross-Origin-Opener-Policy"], "same-origin")
            self.assertEqual(h["Cache-Control"], "no-store", path)
            csp = h["Content-Security-Policy"]
            for part in ("default-src 'self'", "script-src 'self'", "style-src 'self'", "object-src 'none'", "frame-ancestors 'none'", "form-action 'self'", "base-uri 'self'"):
                self.assertIn(part, csp, path)
            self.assertNotRegex(csp, r"(script-src|style-src) [^;]*unsafe")                                      # no inline scripts or style blocks, ever
            self.assertIn("style-src-attr 'unsafe-inline'", csp)                                                # (only style="..." attributes)
            self.assertNotIn("Strict-Transport-Security", h)                                                    # only when served over HTTPS
        css = c.get("/static/style.css")                                                             # static files: cacheable, but still nosniff
        self.assertEqual(css.headers["X-Content-Type-Options"], "nosniff"); self.assertNotIn("no-store", css.headers.get("Cache-Control", ""))
        self.assertNotIn("Content-Security-Policy", css.headers)
        c.post("/students/1/courses", data={"course": self.ALL6, "csrf_token": t})
        pdf = c.get("/forms/1.pdf")                                                                  # no page policy on PDFs (it can upset PDF viewers)
        self.assertEqual(pdf.mimetype, "application/pdf"); self.assertNotIn("Content-Security-Policy", pdf.headers)
        self.assertEqual(pdf.headers["X-Content-Type-Options"], "nosniff"); self.assertIn("no-store", pdf.headers["Cache-Control"])

    def test_https_mode_marks_cookies_secure_and_sends_hsts(self):
        import os
        from unittest import mock
        def sign_in(env):
            with mock.patch.dict(os.environ, env):
                app = webapp.create_app(self.app.config["DB_PATH"])
            c = app.test_client()
            r = c.post("/login", data={"username": "ed", "password": "pw-ed"})
            return r, c.get("/students")
        r, page = sign_in({"PHYSDB_HTTPS": "1"})
        cookie = r.headers["Set-Cookie"]
        self.assertIn("Secure", cookie); self.assertIn("HttpOnly", cookie); self.assertIn("SameSite=Lax", cookie)
        self.assertIn("max-age=31536000", r.headers["Strict-Transport-Security"]); self.assertIn("Strict-Transport-Security", page.headers)
        r, _ = sign_in({"PHYSDB_BASE_URL": "https://physgrads.example.edu"})                          # the public address implies HTTPS
        self.assertIn("Secure", r.headers["Set-Cookie"])
        r, _ = sign_in({"PHYSDB_HTTPS": "", "PHYSDB_BASE_URL": "http://localhost:8123"})
        self.assertNotIn("Secure", r.headers["Set-Cookie"]); self.assertNotIn("Strict-Transport-Security", r.headers)   # plain local use still works

    def test_pages_have_no_inline_scripts_styles_or_event_handlers(self):
        """The Content-Security-Policy forbids them, so any that crept back in would silently stop working."""
        c, t, appr, chair = self._approval_world()
        c.post("/faculty/new", data=dict(first_name="Niels", last_name="Bohr", email="b@u.edu", cnet="nbohr", csrf_token=t))
        c.post("/fsrs/new", data=dict(first_name="X", last_name="Xx", email="x@u.edu", cnet="fsrx", csrf_token=t))
        c.post("/faculty/1/edit", data=dict(first_name="Niels", last_name="Bohr", email="b@u.edu", cnet="nbohr", fsr_id="1", csrf_token=t))
        c.post("/students/1/advising/new", data=dict(faculty_id=1, role="advisor", start_date="2024-01-01", csrf_token=t))
        c.post("/students/1/funding/new", data=dict(kind="RA", term_year=2026, term_season="Autumn", amount="5", csrf_token=t))
        c.post("/students/1/courses", data={"course": self.ALL6, "csrf_token": t})
        self._make_faculty_login(c, t, fid=1); fac = self._faculty_client()
        pages = {c: ["/students", "/students/1", "/students/new", "/students/1/edit", "/students/1/milestones/new", "/students/1/funding/new",
                     "/students/1/funding/copy?year=2026&season=Autumn", "/students/import", "/faculty", "/faculty/1", "/faculty/1/edit", "/fsrs",
                     "/fsrs/1", "/reports", "/reports/milestones", "/reports/funding", "/approvals", "/users", "/users/new", "/users/1/edit", "/account"],
                 chair: ["/signatures", "/approvals"], fac: ["/my", "/my/students/1"]}
        checked = 0
        for client, paths in pages.items():
            for path in paths:
                r = client.get(path)
                self.assertEqual(r.status_code, 200, path)
                html = r.get_data(as_text=True)
                self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)", path)                           # only <script src="..."> is allowed
                self.assertNotIn("<style", html, path)
                self.assertNotRegex(html, r"<[^>]+\son[a-z]+\s*=", path)                             # no onclick= / onsubmit= ...
                self.assertNotRegex(html, r"""(?:href|src|action)\s*=\s*["']\s*javascript:""", path)
                checked += 1
        self.assertGreater(checked, 20)
        js = c.get("/static/app.js")
        self.assertEqual(js.status_code, 200); self.assertIn("data-confirm", js.get_data(as_text=True))
        self.assertIn('<script src="/static/app.js" defer></script>', c.get("/students").get_data(as_text=True))
        self.assertIn('class="wide-page"', c.get("/reports/milestones").get_data(as_text=True))

    # ---- the audit log -----------------------------------------------------------------------------------
    def _audit(self, **where):
        conn = physdb.connect(self.app.config["DB_PATH"])
        try:
            sql = "SELECT * FROM audit_log" + (" WHERE " + " AND ".join(f"{k} = ?" for k in where) if where else "") + " ORDER BY id"
            return [dict(r) for r in conn.execute(sql, tuple(where.values()))]
        finally:
            conn.close()

    def test_web_changes_are_credited_to_whoever_made_them(self):
        import json
        c, t, appr, chair = self._approval_world()                                 # admin "ad", approver "appr", chair "chr"
        ed = self.client("ed"); et = self.token(ed)
        ed.post("/students/1/edit", data=dict(first_name="Ada", last_name="Lovelace", university_id="U1", email="ada@u.edu", citizenship="citizen",
                status="on_leave", matriculation_year="2022", matriculation_term="Autumn", research_area="optics", csrf_token=et))
        ed.post("/students/1/milestones/new", data=dict(kind="pre_oral", date="2024-05-01", outcome="passed", csrf_token=et))
        ed.post("/students/1/funding/new", data=dict(kind="RA", term_year=2026, term_season="Autumn", amount="5", csrf_token=et))
        ed.post("/students/1/courses", data={"course": ["cm", "sm"], "csrf_token": et})
        who = {(r["entity"], r["action"]): r["actor"] for r in self._audit() if r["actor"] != "ad"}
        for key in (("students", "update"), ("milestones", "insert"), ("funding", "insert"), ("course_credits", "insert")):
            self.assertEqual(who.get(key), "ed", key)
        upd = [r for r in self._audit(entity="students") if r["action"] == "update"][0]
        old, new = json.loads(upd["old_values"]), json.loads(upd["new_values"])
        self.assertEqual((old["status"], new["status"], old["research_area"], new["research_area"]), ("active", "on_leave", None, "optics"))
        # account actions by the administrator, and the user's own password change, are logged without any hash
        self.assertTrue(any(r["entity"] == "users" and r["action"] == "insert" and r["actor"] == "ad" and r["label"] == "appr" for r in self._audit()))
        appr.post("/account", data=dict(current_password="my-own-pass-1", new_password="another-pass-2", confirm="another-pass-2", csrf_token=self.token(appr, "/account")))
        pw = [r for r in self._audit(entity="users", actor="appr") if r["action"] == "update"]
        self.assertEqual(len(pw), 2)                                                                  # the forced first change, and this one
        self.assertTrue(all("password_hash_changed" in r["new_values"] for r in pw))
        self.assertNotIn("scrypt", " ".join(f"{r['old_values']}{r['new_values']}" for r in self._audit()))
        # the approval path is attributed too
        c.post("/students/1/courses", data={"course": self.ALL6, "csrf_token": t})
        fid = self._forms()[0]["id"]
        appr.post(f"/forms/{fid}/approve", data={"csrf_token": self.token(appr, "/approvals")})
        self._sig_upload(chair); chair.post(f"/forms/{fid}/sign", data={"csrf_token": self.token(chair, "/signatures")})
        steps = [(r["entity"], r["action"], r["actor"]) for r in self._audit() if r["entity"] in ("forms", "signatures") or
                 (r["entity"] == "milestones" and r["actor"] == "chr")]
        self.assertIn(("forms", "insert", "ad"), steps); self.assertIn(("forms", "update", "appr"), steps)
        self.assertIn(("signatures", "insert", "chr"), steps); self.assertIn(("forms", "update", "chr"), steps)
        self.assertIn(("milestones", "insert", "chr"), steps)                                       # the milestone the signing recorded
        # nobody in the log is "(outside the app)" and nothing is unattributed
        self.assertEqual([r for r in self._audit() if r["actor"] is None], [])

    def test_sign_ins_lockouts_and_imports_are_logged(self):
        from test_import import HEADER, good, xlsx
        c = self.app.test_client()
        self._try_login(c, "ed", "pw-ed")
        ev = [r for r in self._audit(action="event")]
        self.assertEqual([(r["actor"], r["label"]) for r in ev], [("ed", "Signed in")])
        for n in range(webapp.LOGIN_MAX_PER_USERNAME):
            self._try_login(self.app.test_client(), "mallory", "bad")
        locked = [r for r in self._audit(action="event") if "locked" in r["label"]]
        self.assertEqual(len(locked), 1); self.assertIn("'mallory'", locked[0]["label"]); self.assertEqual(locked[0]["actor"], "(not signed in)")
        self._try_login(self.app.test_client(), "mallory", "bad")                                    # blocked attempts don't add more
        self.assertEqual(len([r for r in self._audit(action="event") if "locked" in r["label"]]), 1)
        # an import is marked as such, one row per student
        import io
        ed = self.client("ed"); et = self.token(ed, "/students/import")
        ed.post("/students/import", data=dict(csrf_token=et, file=(io.BytesIO(xlsx([HEADER, good(1), good(2)])), "new class.xlsx")), content_type="multipart/form-data")
        ed.post("/students/import/confirm", data={"csrf_token": et})
        imported = [r for r in self._audit(entity="students") if r["action"] == "insert"]
        self.assertEqual([(r["actor"], r["note"]) for r in imported], [("ed", "student import from new class.xlsx")] * 2)

    def test_the_audit_page_is_for_administrators_and_the_chair_only(self):
        c, t, appr, chair = self._approval_world()
        self.assertEqual(c.get("/audit").status_code, 200); self.assertEqual(chair.get("/audit").status_code, 200)
        for who in (self.client("ed"), self.client("vw"), appr):
            self.assertEqual(who.get("/audit").status_code, 403)
        self.assertEqual(self.app.test_client().get("/audit").status_code, 302)
        c.post("/faculty/new", data=dict(first_name="Niels", last_name="Bohr", email="b@u.edu", cnet="nbohr", csrf_token=t))
        self._make_faculty_login(c, t, fid=1); fac = self._faculty_client()
        self.assertEqual(fac.get("/audit").status_code, 403)
        self.assertNotIn(">Audit<", self.client("ed").get("/students").get_data(as_text=True))
        self.assertIn(">Audit<", c.get("/students").get_data(as_text=True))
        self.assertIn('href="/audit?student=1"', c.get("/students/1").get_data(as_text=True))          # History link
        self.assertNotIn("/audit?student=", self.client("ed").get("/students/1").get_data(as_text=True))
        # there is no way to change or remove an entry from the site
        for path in ("/audit/1/delete", "/audit/1/edit", "/audit/clear"):
            self.assertIn(c.post(path, data={"csrf_token": t}).status_code, (404, 405))

    def test_the_audit_page_shows_readable_entries_and_filters_them(self):
        c, t, appr, chair = self._approval_world()
        c.post("/faculty/new", data=dict(first_name="Niels", last_name="Bohr", email="b@u.edu", cnet="nbohr", csrf_token=t))
        c.post("/students/1/advising/new", data=dict(faculty_id=1, role="advisor", start_date="2024-01-01", csrf_token=t))
        ed = self.client("ed"); et = self.token(ed)
        ed.post("/students/1/edit", data=dict(first_name="Ada", last_name="Lovelace", university_id="U1", email="ada@u.edu", citizenship="citizen",
                status="graduated", graduation_year="2026", graduation_term="Spring", matriculation_year="2022", matriculation_term="Autumn",
                notes="<script>alert(1)</script>", csrf_token=et))
        page = c.get("/audit").get_data(as_text=True)
        self.assertIn("Status: Active → Graduated", page); self.assertIn("Graduation year: (empty) → 2026", page)
        self.assertIn("Changed student", page); self.assertIn("Added advisor / committee", page); self.assertIn("Faculty: Niels Bohr", page)
        self.assertIn("Role: Advisor", page)
        c.post("/students/1/courses", data={"course": ["qm2"], "csrf_token": t})
        self.assertIn("Course: QM-2 (Quantum Mechanics II)", c.get("/audit").get_data(as_text=True))
        self.assertNotIn("Student: 1", c.get("/audit").get_data(as_text=True))
        self.assertIn(">ed<", page); self.assertIn(">ad<", page)
        self.assertNotIn("<script>alert", page); self.assertIn("&lt;script&gt;alert(1)", page)            # entries are escaped
        rows = lambda q: re.findall(r"<tr>\s*<td style=\"white-space:nowrap\">(\d{4}-\d{2}-\d{2} [\d:]+)</td>", c.get("/audit" + q).get_data(as_text=True))
        everything = len(rows(""))
        self.assertGreater(everything, 5)
        by_ed = c.get("/audit?who=ed").get_data(as_text=True)
        self.assertIn("Status: Active → Graduated", by_ed); self.assertNotIn("Role: Advisor", by_ed)
        self.assertIn("Role: Advisor", c.get("/audit?what=advising").get_data(as_text=True)); self.assertNotIn("Status:", c.get("/audit?what=advising").get_data(as_text=True))
        self.assertNotIn("Role: Advisor", c.get("/audit?action=update").get_data(as_text=True))
        self.assertIn("Role: Advisor", c.get("/audit?action=insert").get_data(as_text=True))
        stu = c.get("/audit?student=1").get_data(as_text=True)
        self.assertIn("Ada Lovelace", stu); self.assertIn("only changes concerning", stu); self.assertNotIn("Bohr, Niels", stu)
        self.assertIn("Ada Lovelace", c.get("/audit?q=lovelace").get_data(as_text=True))
        self.assertEqual(len(rows("?q=zzzznomatch")), 0); self.assertIn("Nothing matches", c.get("/audit?q=zzzznomatch").get_data(as_text=True))
        self.assertEqual(len(rows("?since=2999-01-01")), 0); self.assertEqual(len(rows("?until=2000-01-01")), 0)
        import datetime
        today = datetime.date.today().isoformat()
        self.assertEqual(len(rows(f"?since={today}&until={today}")), everything)                           # "until" includes the whole day
        for junk in ("?since=not-a-date", "?until=2026-13-45", "?page=abc", "?student=x", "?who=%27%3B--", "?what=nonsense", "?q=%25_%5C"):
            self.assertEqual(c.get("/audit" + junk).status_code, 200, junk)
        # a change made straight to the database file shows up as outside the app
        import sqlite3
        raw = sqlite3.connect(self.app.config["DB_PATH"]); raw.execute("UPDATE students SET notes = 'sneaky' WHERE id = 1"); raw.commit(); raw.close()
        outside = c.get("/audit?who=_outside").get_data(as_text=True)
        self.assertIn("(outside the app)", outside); self.assertIn("sneaky", outside)

    def test_the_audit_page_pages_through_a_long_log(self):
        c, t, appr, chair = self._approval_world()
        conn = physdb.connect(self.app.config["DB_PATH"])
        conn.executemany("INSERT INTO audit_log (at, actor, action, entity, label, new_values) VALUES ('2026-01-01T00:00:00', 'bulk', 'event', 'session', ?, NULL)",
                         [(f"event {n}",) for n in range(250)]); conn.commit(); conn.close()
        first = c.get("/audit?who=bulk").get_data(as_text=True)
        self.assertEqual(first.count("event "), 100)
        self.assertIn("event 249", first); self.assertNotIn("event 149", first); self.assertIn("Older", first); self.assertNotIn("Newer", first)
        older = c.get("/audit?who=bulk&page=2").get_data(as_text=True)
        self.assertIn("event 149", older); self.assertIn("Newer", older); self.assertIn("Older", older)
        last = c.get("/audit?who=bulk&page=3").get_data(as_text=True)
        self.assertEqual(last.count("event "), 50); self.assertNotIn("Older", last)
        self.assertIn("who=bulk", re.search(r'href="([^"]*)">Older', first).group(1).replace("&amp;", "&"))        # filters are kept

    def test_signature_upload_rules(self):
        import io
        c, t, appr, chair = self._approval_world()
        self.assertEqual(appr.get("/signatures").status_code, 403); self.assertEqual(appr.get("/signature.png").status_code, 403)
        self.assertEqual(c.get("/signatures").status_code, 403)                                        # even an administrator
        self.assertEqual(chair.get("/signature.png").status_code, 404)                                  # none stored yet
        for data, name, word in ((b"hello", "sig.png", "could not be read"), (b"", "sig.png", "Choose an image")):
            r = self._sig_upload(chair, name, data)
            self.assertEqual(r.status_code, 302)
            self.assertIn(word, chair.get("/signatures").get_data(as_text=True))
        self.assertEqual(chair.post("/signatures", data=dict(csrf_token=self.token(chair, "/signatures"))).status_code, 302)        # no file at all
        self.assertEqual(chair.post("/signatures", data=dict(image=(io.BytesIO(b"x"), "s.png")), content_type="multipart/form-data").status_code, 400)  # CSRF
        self._sig_upload(chair)
        self.assertEqual(chair.get("/signature.png").status_code, 200)
        self.assertNotIn("signature.png", appr.get("/approvals").get_data(as_text=True))
        r = chair.post("/signatures", data=dict(remove="1", csrf_token=self.token(chair, "/signatures")), follow_redirects=True)
        self.assertIn("was removed", r.get_data(as_text=True)); self.assertEqual(chair.get("/signature.png").status_code, 404)

    def test_an_approver_is_an_editor_and_the_chair_is_an_administrator(self):
        c, t, appr, chair = self._approval_world()
        c.post("/students/1/courses", data={"course": self.ALL6, "csrf_token": t})
        fid = self._forms()[0]["id"]
        # the approver sees every editor page and can change records, but cannot manage accounts or sign
        for path in ("/students", "/students/1", "/students/new", "/students/import", "/faculty", "/faculty/new", "/fsrs", "/fsrs/new",
                     "/reports", "/reports/milestones", "/reports/funding", "/export.csv", "/students/1/edit", "/students/1/funding/new",
                     "/approvals", "/account"):
            self.assertEqual(appr.get(path).status_code, 200, path)
        for path in ("/users", "/users/new", "/users/new-faculty", "/signatures", "/signature.png", "/my", "/fsr"):
            self.assertEqual(appr.get(path).status_code, 403, path)
        at = self.token(appr, "/students")
        self.assertEqual(appr.post("/students/1/courses", data={"course": self.ALL6, "csrf_token": at}).status_code, 302)    # edits records
        self.assertEqual(appr.post("/faculty/new", data=dict(first_name="N", last_name="Bohr", email="b@u.edu", csrf_token=at)).status_code, 302)
        self.assertEqual(appr.post("/users/new", data={"csrf_token": at}).status_code, 403)
        self.assertEqual(appr.post("/users/1/delete", data={"csrf_token": at}).status_code, 403)
        # the chair sees everything an administrator does, plus Approvals and Signatures
        for path in ("/students", "/students/1/edit", "/faculty", "/fsrs", "/reports", "/reports/funding", "/export.csv", "/users", "/users/new",
                     "/users/new-faculty", "/users/new-fsr", "/students/import", "/approvals", "/signatures", "/account"):
            self.assertEqual(chair.get(path).status_code, 200, path)
        self.assertEqual(chair.get("/my").status_code, 403); self.assertEqual(chair.get("/fsr").status_code, 403)    # not faculty / FSR pages
        ct = self.token(chair, "/students")
        self.assertEqual(chair.post("/students/new", data=dict(first_name="Z", last_name="Zed", university_id="U9", email="z@u.edu",
                                    citizenship="citizen", status="active", matriculation_year="2022", matriculation_term="Autumn", csrf_token=ct)).status_code, 302)
        self.assertEqual(chair.post("/users/new", data=dict(username="newviewer", role="viewer", full_name="", email="", password="temp-password-1", csrf_token=ct)).status_code, 302)
        # the tabs: approver = editor tabs + Approvals; chair = admin tabs + Approvals + Signatures; others see "Forms"
        header = lambda k: re.findall(r"<header>(.*?)<span class=\"spacer\">", k.get("/students").get_data(as_text=True), re.S)[0]
        tabs = lambda k: [re.sub(r"<[^>]+>|\s+", " ", x).strip().split(" ")[0] for x in re.findall(r"<a href=\"/[^\"]*\">(.*?)</a>", header(k), re.S)]
        self.assertEqual(tabs(appr), ["Students", "Faculty", "FSRs", "Reports", "Approvals"])
        self.assertEqual(tabs(chair), ["Students", "Faculty", "FSRs", "Reports", "Approvals", "Signatures", "Accounts", "Audit"])
        self.assertEqual(tabs(c), ["Students", "Faculty", "FSRs", "Reports", "Forms", "Accounts", "Audit"])
        self.assertRegex(header(appr), r'Approvals <span class="count">1</span>'); self.assertNotIn("Signatures", header(appr))
        # first round: the approver or the chair; final round: the chair only; rejecting follows the same split
        tok = lambda k: self.token(k, "/students")
        ed = self.client("ed")
        self.assertEqual(c.post(f"/forms/{fid}/approve", data={"csrf_token": t}).status_code, 403)         # plain administrators don't approve
        self.assertEqual(ed.post(f"/forms/{fid}/approve", data={"csrf_token": tok(ed)}).status_code, 403)
        self.assertEqual(appr.post(f"/forms/{fid}/reject", data={"stage": "pending_chair", "reason": "x", "csrf_token": tok(appr)}).status_code, 403)
        self.assertEqual(appr.post(f"/forms/{fid}/reject", data={"stage": "bogus", "reason": "x", "csrf_token": tok(appr)}).status_code, 400)
        self.assertEqual(chair.post(f"/forms/{fid}/approve", data={"csrf_token": ct}).status_code, 302)    # the chair may approve the first round
        self.assertEqual(self._forms()[0]["approved_by"], "chr")
        self.assertEqual(appr.post(f"/forms/{fid}/sign", data={"csrf_token": tok(appr)}).status_code, 403)
        self.assertEqual(c.post(f"/forms/{fid}/sign", data={"csrf_token": t}).status_code, 403)
        self.assertEqual(self._forms()[0]["status"], "pending_chair")
        v = self.client("vw"); self.assertEqual(v.get("/approvals").status_code, 200)
        self.assertNotIn(">Approve</button>", v.get("/approvals").get_data(as_text=True))                  # viewers just look
        for path in (f"/forms/{fid}/approve", f"/forms/{fid}/sign", f"/forms/{fid}/reject"):
            self.assertEqual(v.post(path, data={"csrf_token": self.token(v), "reason": "x", "stage": "pending_chair"}).status_code, 403, path)
        # faculty and FSR logins cannot see the forms or the queue
        c.post("/faculty/new", data=dict(first_name="Marie", last_name="Curie", email="curie@u.edu", cnet="mcurie", csrf_token=t))
        self._make_faculty_login(c, t, fid=2); fac = self._faculty_client("mcurie")
        self.assertEqual(fac.get("/approvals").status_code, 403)
        self.assertEqual(fac.get(f"/forms/{fid}.pdf").status_code, 403)

    def test_child_edit_heading_names_the_student(self):
        c, t = self._faculty_world()
        self.assertIn("<h1>Edit: funding for Stu Aa</h1>", c.get("/students/1/funding/1/edit").get_data(as_text=True))
        self.assertIn("<h1>Add: funding for Stu Aa</h1>", c.get("/students/1/funding/new").get_data(as_text=True))

    def test_faculty_advisee_table_sorts_and_does_not_wrap(self):
        from unittest import mock
        c, t = self._faculty_world()
        def who(path, client=c):
            with mock.patch.object(physdb, "current_term", return_value=("Autumn", 2026)):
                html = client.get(path).get_data(as_text=True)
            return re.findall(r'/students/\d+">(\w+), Stu', html.split("<h2>Thesis Committees")[0]), html
        # Aa: $3,000 (RA)   Bb: $250 (supplement)   both advisees of Bohr
        self.assertEqual(who("/faculty/1")[0], ["Aa", "Bb"])
        self.assertEqual(who("/faculty/1?sort=name&dir=desc")[0], ["Bb", "Aa"])
        self.assertEqual(who("/faculty/1?sort=funding")[0], ["Bb", "Aa"])
        self.assertEqual(who("/faculty/1?sort=funding&dir=desc")[0], ["Aa", "Bb"])
        self.assertEqual(who("/faculty/1?sort=research_area&dir=desc")[0], ["Bb", "Aa"])
        self.assertEqual(who("/faculty/1?sort=bogus")[0], ["Aa", "Bb"])
        for key in ("status", "clock", "graduated"):
            self.assertEqual(sorted(who(f"/faculty/1?sort={key}")[0]), ["Aa", "Bb"], key)
        # a blank sorts last whichever way: with Aa's funding cleared for the term, Aa is last in both directions
        c.post("/students/1/funding/1/delete", data={"csrf_token": t})
        self.assertEqual(who("/faculty/1?sort=funding")[0], ["Bb", "Aa"]); self.assertEqual(who("/faculty/1?sort=funding&dir=desc")[0], ["Bb", "Aa"])
        _, html = who("/faculty/1?sort=funding&dir=desc")
        head = html.split('<table class="nowrap">')[1].split("</tr>")[0]
        self.assertEqual(head.count('class="sortlink'), 6)                                    # every heading sorts
        self.assertIn("table.nowrap td", self.app.test_client().get("/static/style.css").get_data(as_text=True))      # the rule is in the stylesheet
        self.assertIn("sort=funding&amp;dir=desc", html.split("Prev")[0].rsplit("<a ", 1)[1])      # Prev / Next keep the sort
        self.assertIn("sort=funding&amp;dir=desc", html.split("Next")[0].rsplit("<a ", 1)[1])
        # the faculty member's own page sorts the same way
        self._make_faculty_login(c, t); fc = self._faculty_client()
        with mock.patch.object(physdb, "current_term", return_value=("Autumn", 2026)):
            own = fc.get("/my?sort=name&dir=desc").get_data(as_text=True)
        self.assertEqual(re.findall(r"<tr><td[^>]*><a [^>]*>(\w+), Stu</a>", own.split("Students whose committee")[0]), ["Bb", "Aa"])

    def test_faculty_committee_table_sorts_independently(self):
        from unittest import mock
        c, t = self._faculty_world()
        def page(q=""):
            with mock.patch.object(physdb, "current_term", return_value=("Autumn", 2026)):
                html = c.get("/faculty/1" + q).get_data(as_text=True)
            adv, com = html.split("<h2>Thesis Committees")
            names = lambda h: re.findall(r'/students/\d+">(\w+), Stu', h)
            return names(adv), names(com), html, com
        self.assertEqual(page()[:2], (["Aa", "Bb"], ["Cc", "Dd"]))
        self.assertEqual(page("?csort=name&cdir=desc")[:2], (["Aa", "Bb"], ["Dd", "Cc"]))           # advisees untouched
        self.assertEqual(page("?csort=role")[1], ["Dd", "Cc"])                                      # chair first
        self.assertEqual(page("?csort=role&cdir=desc")[1], ["Cc", "Dd"])
        self.assertEqual(page("?sort=name&dir=desc&csort=role")[:2], (["Bb", "Aa"], ["Dd", "Cc"]))  # both at once
        for key in ("status", "clock", "research_area", "advisor", "bogus"):
            self.assertEqual(sorted(page(f"?csort={key}")[1]), ["Cc", "Dd"], key)
        self.assertEqual(page("?csort=research_area&cdir=desc")[1], ["Dd", "Cc"])
        _, _, html, com = page("?sort=funding&csort=role")
        head = com.split("</tr>")[0]
        self.assertEqual(head.count('class="sortlink'), 6)
        self.assertIn("csort=name", head); self.assertIn("sort=funding", head)                      # keeps the other table's sort
        self.assertNotIn("sort=name&amp;dir", head.replace("csort=name&amp;cdir", ""))              # ...but only changes its own
        self.assertIn("csort=role", html.split("<h2>Thesis Committees")[0].split("Next")[0].rsplit("<a ", 1)[1])   # Prev/Next keep it
        self.assertIn('<table class="nowrap"><tr>', com)

    def test_staff_view_of_a_faculty_member_has_funding_buttons(self):
        from unittest import mock
        c, t = self._faculty_world()
        def page(client, path="/faculty/1"):
            with mock.patch.object(physdb, "current_term", return_value=("Autumn", 2026)):
                return client.get(path).get_data(as_text=True)
        html = page(c)
        # Aa and Bb each have one entry (Edit, Clear, Copy, +); Ff-less table: no advisee lacks funding here
        self.assertEqual(html.count('class="btn secondary small" href="/students/1/funding/1/edit'), 1)
        self.assertEqual(len(re.findall(r'>Clear</button>', html)), 2)
        self.assertEqual(html.count("data-copy href"), 2)
        self.assertEqual(html.count('class="btn small plus"'), 2)
        self.assertIn("next=/faculty/1", html)
        # an editor sees them, a viewer does not
        self.assertIn(">Clear</button>", page(self.client("ed")))
        v = page(self.client("vw"))
        self.assertNotIn(">Clear</button>", v); self.assertNotIn("data-copy", v); self.assertNotIn("copy-dialog", v)
        # Clear works and returns to the faculty page
        r = c.post("/students/1/funding/1/delete?next=/faculty/1%3Fseason%3DAutumn%26year%3D2026", data={"csrf_token": t})
        self.assertEqual(r.status_code, 302); self.assertIn("/faculty/1", r.headers["Location"])
        # a faculty login's own page has none of it
        self._make_faculty_login(c, t)
        fc = self._faculty_client()
        mine = page(fc, "/my")
        self.assertNotIn(">Clear</button>", mine); self.assertNotIn("data-copy", mine); self.assertNotIn("/funding/", mine)

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
        headers = lambda h: [re.sub(r"\s+", " ", re.sub(r"<[^>]+>|[▲▼↕]", "", x)).strip() for x in
                             re.findall(r"<th[^>]*>(.*?)</th>", h.split("</tr>")[0].split("<table")[-1], re.S)]
        self.assertEqual(headers(adv), ["Name", "Status", "Clock", "Graduated", "Research area", "Funding, Autumn 2026"])
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
                   matriculation_term="Autumn", graduation_year="2026", graduation_term="Spring", csrf_token=t))
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
        html = page("kind=pre_oral&years=0.25")
        # 1) the tab and page are called "Milestone Check"
        self.assertIn(">Milestone Check</a>", html); self.assertIn("<h1>Milestone Check</h1>", html)
        self.assertNotIn("Behind schedule", html); self.assertNotIn("behind schedule", html)
        # 2) the time box takes fractions, and the filter works on them (clock >= 0.25: A, B, C but not D)
        self.assertRegex(html, r'<input type="number" name="years" min="0" max="15" step="0.25" value="0.25">')
        self.assertEqual(names(html), ["Mc_A", "Mc_B", "Mc_C"])
        self.assertEqual(names(page("kind=pre_oral&years=0.3")), ["Mc_A", "Mc_B"])
        self.assertEqual(names(page("kind=pre_oral&years=1")), ["Mc_A"])
        self.assertEqual(names(page("kind=pre_oral&years=1.25")), [])
        self.assertEqual(sorted(names(page("kind=pre_oral&years=0"))), ["Mc_A", "Mc_B", "Mc_C", "Mc_D"])
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
        self.assertEqual(names(page("kind=pre_oral&years=0&sort=clock")), ["Mc_D", "Mc_C", "Mc_B", "Mc_A"])
        self.assertEqual(names(page("kind=pre_oral&years=0&sort=clock&dir=desc")), ["Mc_A", "Mc_B", "Mc_C", "Mc_D"])
        self.assertEqual(names(page("kind=pre_oral&years=0&sort=advisor"))[:2], ["Mc_A", "Mc_B"])
        self.assertEqual(names(page("kind=pre_oral&years=0&sort=advisor&dir=desc"))[:2], ["Mc_B", "Mc_A"])
        self.assertEqual(sorted(names(page("kind=pre_oral&years=0&sort=advisor"))[2:]), ["Mc_C", "Mc_D"])
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
        headers = lambda h: [re.sub(r"\s+", " ", re.sub(r"<[^>]+>|[▲▼↕]", "", x)).strip() for x in
                             re.findall(r"<th[^>]*>(.*?)</th>", h.split("</tr>")[0].split("<table")[-1], re.S)]
        self.assertEqual(headers(adv), ["Name", "Status", "Clock", "Graduated", "Research area",
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
                       matriculation_year="2022", matriculation_term="Autumn", graduation_year="2026",
                       graduation_term="Spring", csrf_token=t))
            self.assertEqual(r.status_code, 302)
        html = page()
        adv, com = html.split("Students whose committee I am on")
        self.assertIn("(1 active)", adv); self.assertIn("(1 active)", com)
        self.assertEqual(re.findall(r'/my/students/(\d+)">', adv), ["1", "2"])                    # Bb is still listed
        self.assertIn("Dd, Stu", com); self.assertIn("Graduated", adv); self.assertIn("Graduated", com)
        for sid, last in ((1, "Aa"), (3, "Cc")):                                                  # nobody active left
            c.post(f"/students/{sid}/edit", data=dict(first_name="Stu", last_name=last, university_id=f"UID{sid}",
                   cnet=f"stu{last.lower()}", email=f"s{sid}@u.edu", citizenship="citizen", status="withdrawn",
                   matriculation_year="2022", matriculation_term="Autumn", withdrawal_year="2026", withdrawal_term="Winter", csrf_token=t))
        html = page()
        self.assertEqual(html.count("(0 active)"), 2)
        self.assertEqual(f.get("/my/students/1").status_code, 200)                                # still reachable

    def test_faculty_student_detail_is_limited_to_their_own_advisees(self):
        c, t = self._faculty_world(); self._make_faculty_login(c, t)
        f = self._faculty_client()
        page = f.get("/my/students/1").get_data(as_text=True)
        self.assertEqual(f.get("/my/students/2").status_code, 200)
        for needle in ("Stu Aa", "stuaa", "area-Aa", "Pre-Oral", "Passed", "Niels Bohr", "NSF grant", "1234567-ABC",
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
        self.assertEqual(allowed, {"faculty_home", "faculty_student", "faculty_form_pdf", "account", "logout", "login", "static", "home"})
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

    def test_prev_and_next_follow_the_students_list_order(self):
        import re as _re
        c = self.client("ed")
        ids = {}
        for last, uid, status in [("Nb", "U1", "active"), ("Na", "U2", "graduated"), ("Nc", "U3", "active")]:
            self.add_student(c, last_name=last, university_id=uid, email=f"{uid}@u.edu", status=status)
            ids[last] = int(_re.search(r"/students/(\d+)", c.get("/students?q=" + last).get_data(as_text=True)).group(1))
        def page(name):
            return c.get(f"/students/{ids[name]}").get_data(as_text=True)
        def links(html):
            return [int(m) for m in _re.findall(r'href="/students/(\d+)"[^>]*>(?:&lsaquo; Prev|Next &rsaquo;)', html)]
        # no list visited yet: everyone, by name
        h = page("Nb")
        self.assertEqual(links(h), [ids["Na"], ids["Nc"]])
        self.assertIn("2 of 3", h)
        self.assertIn('class="btn secondary small disabled">&lsaquo; Prev', page("Na"))   # first: no Prev
        self.assertIn('class="btn secondary small disabled">Next &rsaquo;', page("Nc"))   # last: no Next
        # sorted by name descending
        c.get("/students?sort=name&dir=desc")
        self.assertEqual(links(page("Nb")), [ids["Nc"], ids["Na"]])
        self.assertIn('href="/students?sort=name&amp;dir=desc"', page("Nb"))              # All students returns to it
        # filtered: the graduated student is out of the list, so Prev / Next skip them
        c.get("/students?status=active")
        h = page("Nb")
        self.assertEqual(links(h), [ids["Nc"]])
        self.assertIn("1 of 2", h)
        # a student outside the remembered list falls back to everyone
        h = page("Na")
        self.assertIn("1 of 3", h)
        self.assertIn('href="/students"', h)
        self.assertNotIn("status=active", h)
        # junk or oversized remembered values are harmless
        with c.session_transaction() as sess:
            sess["student_list"] = "sort=%00&dir=&&q=" + "z" * 300 + "&role=x&faculty=abc"
        self.assertEqual(c.get(f"/students/{ids['Nb']}").status_code, 200)

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
        rep = lambda qs="": self.names2(c.get("/reports?kind=pre_oral&years=0&" + qs).get_data(as_text=True))
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
