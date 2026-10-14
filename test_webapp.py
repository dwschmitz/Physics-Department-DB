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
        for name, role in (("ed", "editor"), ("vw", "viewer")):
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
            "funding": dict(kind="TA", term="Fall 2026", amount="12000"),
        }.items():
            r = c.post(f"/students/1/{table}/new", data={**data, "csrf_token": t})
            self.assertEqual(r.status_code, 302, table)
        page = c.get("/students/1").get_data(as_text=True)
        for needle in ("Bohr", "Qualifying exam", "$12,000"):
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


if __name__ == "__main__":
    unittest.main()
