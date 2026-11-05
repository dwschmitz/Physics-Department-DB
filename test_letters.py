import datetime
import io
import os
import re
import unittest
import zipfile
from unittest import mock

import letters
import physdb

ENV = {"PHYSDB_LETTER_REPLY_EMAIL": "grad@u.edu"}
TODAY = datetime.date(2026, 11, 5)


def text_of(docx):
    xml = zipfile.ZipFile(io.BytesIO(docx)).read("word/document.xml").decode()
    return re.sub(r"<[^>]+>", "", xml), xml


class LetterTests(unittest.TestCase):
    def setUp(self):
        self.conn = physdb.connect(":memory:")
        self.sid = physdb.add_student(self.conn, university_id="U1", first_name="Ada", last_name="Lovelace",
                                      email="a@u.edu", matriculation_year=2022, research_area=None, notes=None)
        self.fid = physdb.add_faculty(self.conn, "Niels", "Bohr", "bohr@u.edu", "Atomic")
        self.conn.execute("INSERT INTO advising (student_id, faculty_id, role, start_date) VALUES (?, ?, 'advisor', '2024-01-01')",
                          (self.sid, self.fid))
        self.conn.execute("INSERT INTO terms (term_year, term_season, start_date, end_date) VALUES (2026, 'Autumn', '2026-09-28', '2026-12-11')")
        self.conn.execute("INSERT INTO funding (student_id, kind, term_year, term_season) VALUES (?, 'RA', 2026, 'Autumn')", (self.sid,))
        self.conn.commit()

    def make(self, env=ENV):
        s = self.conn.execute("SELECT * FROM students").fetchone()
        f = self.conn.execute("SELECT * FROM funding ORDER BY id DESC").fetchone()
        return letters.make_letter(self.conn, s, f, TODAY, env)

    def test_every_field_is_filled_from_the_records_and_the_calendar(self):
        data, name = self.make()
        self.assertEqual(name, "RA_letter_Lovelace_Autumn_2026.docx")
        text, xml = text_of(data)
        for want in ("To: Ada Lovelace", "November 5, 2026", "Dear Ada,", "begin on September 28, 2026.",
                     "Employment unit: Physics.", "Supervisor) is Niels Bohr.", "email is bohr@u.edu.",
                     "returning via email to grad@u.edu."):
            self.assertIn(want, text)
        self.assertNotIn("«", xml)
        self.assertNotIn("MERGEFIELD", xml)                # flattened: nothing left to merge in Word
        self.assertIn("Graduate Student Employee", text)   # the rest of the document is intact

    def test_start_date_comes_from_the_calendar_not_from_the_stale_field_code(self):
        # the template's START_DATE spot carries a DEPT_OR_PROGRAM field code; it must still get the date
        text, _ = text_of(self.make({**ENV, "PHYSDB_LETTER_DEPARTMENT": "Dept of Physics"})[0])
        self.assertIn("begin on September 28, 2026.", text)
        self.assertIn("Employment unit: Dept of Physics.", text)

    def test_values_are_escaped(self):
        self.conn.execute("UPDATE students SET first_name = 'A<b>&Z'")
        self.conn.commit()
        text, xml = text_of(self.make()[0])
        self.assertIn("A<b>&Z Lovelace", text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))
        self.assertNotIn("<b>&Z", xml)

    def test_joint_supervisors_and_only_those_advising_during_the_term(self):
        f2 = physdb.add_faculty(self.conn, "Marie", "Curie", "curie@u.edu", "Radio")
        f3 = physdb.add_faculty(self.conn, "Max", "Planck", "planck@u.edu", "Quanta")
        self.conn.execute("INSERT INTO advising (student_id, faculty_id, role, start_date) VALUES (?, ?, 'co-advisor', '2026-10-15')", (self.sid, f2))
        self.conn.execute("INSERT INTO advising (student_id, faculty_id, role, start_date, end_date) VALUES (?, ?, 'advisor', '2020-01-01', '2026-09-01')", (self.sid, f3))
        self.conn.execute("INSERT INTO advising (student_id, faculty_id, role, start_date) VALUES (?, ?, 'committee', '2024-01-01')", (self.sid, f3))
        self.conn.commit()
        text, _ = text_of(self.make()[0])
        self.assertIn("Supervisor) is Niels Bohr and Marie Curie.", text)
        self.assertIn("email is bohr@u.edu and curie@u.edu.", text)
        self.assertNotIn("Planck", text)

    def test_what_is_missing_is_explained(self):
        with self.assertRaisesRegex(letters.LetterError, "PHYSDB_LETTER_REPLY_EMAIL"):
            self.make({})
        self.conn.execute("INSERT INTO funding (student_id, kind, term_year, term_season) VALUES (?, 'RA', 2027, 'Winter')", (self.sid,))
        self.conn.commit()
        with self.assertRaisesRegex(letters.LetterError, "Winter 2027 is not in the term calendar"):
            self.make()
        self.conn.execute("INSERT INTO terms (term_year, term_season, start_date, end_date) VALUES (2027, 'Winter', '2027-01-04', '2027-03-19')")
        self.conn.execute("UPDATE advising SET end_date = '2026-12-31'")
        self.conn.commit()
        with self.assertRaisesRegex(letters.LetterError, "no advisor recorded for Winter 2027"):
            self.make()
        self.conn.execute("INSERT INTO funding (student_id, kind) VALUES (?, 'RA')", (self.sid,))
        self.conn.commit()
        with self.assertRaisesRegex(letters.LetterError, "no term"):
            self.make()
        self.conn.execute("INSERT INTO funding (student_id, kind, term_year, term_season) VALUES (?, 'TA', 2026, 'Autumn')", (self.sid,))
        self.conn.commit()
        with self.assertRaisesRegex(letters.LetterError, "no letter template for TA"):
            self.make()

    def test_a_field_the_app_cannot_fill_is_refused(self):
        template = (letters.TEMPLATES_DIR / letters.TEMPLATES["RA"]).read_bytes()
        with self.assertRaisesRegex(letters.LetterError, "do not know|does not know"):
            letters.fill(template, {"DATE": "x"})


if __name__ == "__main__":
    unittest.main()
