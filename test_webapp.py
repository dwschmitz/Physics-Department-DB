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
                    program="PhD", status="active", start_year="2022", csrf_token=self.token(c))
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
                   email="ada@u.edu", program="MS", status="on_leave", start_year="2022", csrf_token=t))
        self.assertEqual(r.status_code, 302)
        self.assertIn("King", c.get("/students").get_data(as_text=True))
        # children
        c.post("/faculty/new", data=dict(first_name="Niels", last_name="Bohr", email="b@u.edu", csrf_token=t))
        for table, data in {
            "advising": dict(faculty_id="1", role="advisor", start_date="2023-01-01"),
            "milestones": dict(kind="qualifying_exam", date="2024-05-01", outcome="passed"),
            "funding": dict(kind="TA", term_year="2026", term_season="Autumn", poetaf_coa="1234567-ABC", amount="12000"),
        }.items():
            r = c.post(f"/students/1/{table}/new", data={**data, "csrf_token": t})
            self.assertEqual(r.status_code, 302, table)
        page = c.get("/students/1").get_data(as_text=True)
        for needle in ("Bohr", "Qualifying exam", "$12,000", "Autumn 2026", "1234567-ABC"):
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
        self.add_student(ed, first_name="=HYPERLINK(1)", start_year="2015")
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
