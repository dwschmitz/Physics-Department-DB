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
            "milestones": dict(kind="qualifying_exam", date="2024-05-01", outcome="passed"),
            "funding": dict(kind="TA", term_year="2026", term_season="Autumn", poetaf_coa="1234567-ABC", amount="12000",
                           notes="Covers <i>half</i> of the term\nrenewal pending"),
        }.items():
            r = c.post(f"/students/1/{table}/new", data={**data, "csrf_token": t})
            self.assertEqual(r.status_code, 302, table)
        page = c.get("/students/1").get_data(as_text=True)
        for needle in ("Bohr", "Qualifying exam", "$12,000", "Autumn 2026", "1234567-ABC",
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
        for needle in ("NSF", "COA-9", "$3,000", "Supplement", "$500", "cnet1", "&lt;b&gt;hi&lt;/b&gt;"):
            self.assertIn(needle, html)
        self.assertNotIn("$999", html); self.assertNotIn("Dd", html)                          # on leave excluded
        self.assertNotIn("<b>hi</b>", html)
        row_cc = re.search(r"/students/3\">Cc.*?</tr>", html, re.S).group(0)
        cells = re.findall(r"<td[^>]*>([^<]*)</td>", row_cc)      # the name cell holds a link, so: CNET, advisor,
        self.assertEqual(len(cells), 7)                           # then type, source, POETAF/COA, amount, notes
        self.assertEqual(cells[0], "cnet3")                       # (University ID is no longer shown on screen)
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
        self.assertIn("Behind schedule", html)
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
        buttons = re.findall(r'class="btn[^"]*" href="([^"]+)">(Edit|Add)</a>', html)
        self.assertEqual([b for _, b in buttons], ["Edit", "Add"])               # Aa has an entry, Bb has none
        edit_href, add_href = (h.replace("&amp;", "&") for h, _ in buttons)      # exactly what a browser would follow
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
        self.assertEqual([b for _, b in re.findall(r'class="btn[^"]*" href="([^"]+)">(Edit|Add)</a>', page)],
                         ["Edit", "Edit"])
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

    def test_funding_report_has_no_buttons_for_viewers(self):
        ed = self.client("ed"); t = self.token(ed); self.add_student(ed)
        ed.post("/students/1/funding/new", data=dict(kind="RA", term_year=2026, term_season="Autumn", csrf_token=t))
        vw = self.client("vw")
        from unittest import mock
        with mock.patch.object(physdb, "current_term", return_value=("Autumn", 2026)):
            html = vw.get("/reports/funding").get_data(as_text=True)
        self.assertIn("Lovelace", html)
        self.assertNotIn(">Edit<", html); self.assertNotIn(">Add<", html)
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
        rep = lambda qs="": self.names2(c.get("/reports?kind=qualifying_exam&years=0&" + qs).get_data(as_text=True))
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
