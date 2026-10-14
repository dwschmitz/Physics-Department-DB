import io
import sqlite3
import unittest

import physdb


class PhysDBTests(unittest.TestCase):
    def setUp(self):
        self.conn = physdb.connect(":memory:")
        self.sid = physdb.add_student(
            self.conn, university_id="U1001", first_name="Ada", last_name="Lovelace",
            email="ada@uni.edu", program="PhD", start_year=2020,
            expected_grad_year=2026, research_area="Quantum optics", notes=None)
        self.fid = physdb.add_faculty(self.conn, "Niels", "Bohr", "bohr@uni.edu", "Atomic")

    def test_lookup_by_id_email_university_id(self):
        for key in (self.sid, "ada@uni.edu", "U1001"):
            self.assertEqual(physdb._find(self.conn, "students", key)["id"], self.sid)

    def test_duplicate_email_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            physdb.add_student(
                self.conn, university_id="U2", first_name="X", last_name="Y",
                email="ada@uni.edu", program="PhD", start_year=2021,
                expected_grad_year=None, research_area=None, notes=None)

    def test_advisor_filter_and_show(self):
        physdb.assign_advisor(self.conn, "U1001", "bohr@uni.edu", start_date="2021-01-01")
        self.assertEqual(len(physdb.list_students(self.conn, advisor="bohr@uni.edu")), 1)
        self.assertIn("Niels Bohr", physdb.show_student(self.conn, "U1001"))

    def test_missing_milestone(self):
        self.assertEqual(len(physdb.missing_milestone(self.conn, "qualifying_exam")), 1)
        physdb.add_milestone(self.conn, "U1001", "qualifying_exam", "2022-05-01", "passed")
        self.assertEqual(physdb.missing_milestone(self.conn, "qualifying_exam"), [])

    def test_status_and_check_constraints(self):
        physdb.set_status(self.conn, "U1001", "graduated")
        self.assertEqual(physdb.list_students(self.conn, status="active"), [])
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE students SET status='bogus'")

    def test_export(self):
        buf = io.StringIO()
        physdb.export_csv(self.conn, buf)
        self.assertIn("Lovelace", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
