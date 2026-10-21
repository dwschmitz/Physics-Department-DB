import datetime
import io
import os
import sqlite3
import tempfile
import unittest

import pypdf
from PIL import Image, ImageDraw

import formfill
import physdb
import workflow


def signature_png(size=(800, 300), transparent=True, fmt="PNG", ink=(10, 10, 120)):
    im = Image.new("RGBA" if transparent else "RGB", size, (255, 255, 255, 0) if transparent else (255, 255, 255))
    ImageDraw.Draw(im).line([(40, 250), (200, 40), (330, 260), (500, 60), (size[0] - 40, 200)], fill=ink, width=7)
    buf = io.BytesIO(); im.convert("RGB" if fmt == "JPEG" else im.mode).save(buf, fmt)
    return buf.getvalue()


class SignatureTests(unittest.TestCase):
    def test_a_good_image_becomes_a_trimmed_png(self):
        out = formfill.prepare_signature(signature_png(size=(1600, 900)))
        img = Image.open(io.BytesIO(out))
        self.assertEqual(img.format, "PNG")
        self.assertLessEqual(img.width, 1200)                       # scaled down
        self.assertLess(img.height, 900)                            # empty margin trimmed
        self.assertEqual(Image.open(io.BytesIO(formfill.prepare_signature(signature_png(fmt="JPEG", transparent=False)))).format, "PNG")

    def test_unusable_images_are_refused_with_a_message(self):
        blank = io.BytesIO(); Image.new("RGB", (200, 80), "white").save(blank, "PNG")
        gif = io.BytesIO(); Image.new("RGB", (20, 20), "black").save(gif, "GIF")
        for data, word in ((b"", "Choose an image"), (b"not an image", "could not be read"), (blank.getvalue(), "blank"),
                           (gif.getvalue(), "PNG or JPEG"), (b"x" * (formfill.MAX_SIGNATURE_BYTES + 1), "too large")):
            with self.assertRaises(formfill.SignatureError) as cm:
                formfill.prepare_signature(data)
            self.assertIn(word, str(cm.exception))


class FormFillTests(unittest.TestCase):
    STUDENT = {"first_name": "Ada", "last_name": "Lovelace", "university_id": "12345678"}

    def test_fields_are_filled_in_and_signing_adds_the_date(self):
        fields = lambda pdf: {a.get_object()["/T"]: a.get_object().get("/V") for a in pypdf.PdfReader(io.BytesIO(pdf)).pages[0]["/Annots"]}
        unsigned = fields(formfill.candidacy_pdf(self.STUDENT, flatten=False))
        self.assertEqual((unsigned["Text3"], unsigned["Text4"], unsigned["Text5"]), ("Ada Lovelace", "Physics", "12345678"))
        self.assertEqual((unsigned["Check Box1"], unsigned["Check Box2"]), ("/Off", "/Yes"))       # the PhD box
        self.assertIn(unsigned["Date7_af_date"], ("", None))                                        # no date until signed
        signed = fields(formfill.candidacy_pdf(self.STUDENT, signature_png(), datetime.date(2026, 3, 9), flatten=False))
        self.assertEqual(signed["Date7_af_date"], "03/09/2026")

    def test_the_advisors_copy_withholds_the_university_id(self):
        ids = lambda **kw: {a.get_object()["/T"]: a.get_object().get("/V") for a in
                            pypdf.PdfReader(io.BytesIO(formfill.candidacy_pdf(self.STUDENT, flatten=False, **kw))).pages[0]["/Annots"]}["Text5"]
        self.assertEqual(ids(), "12345678"); self.assertEqual(ids(hide_id=True), formfill.ID_WITHHELD)
        self.assertNotIn("12345678", formfill.ID_WITHHELD)

    def test_the_finished_pdf_is_flat_and_the_signature_is_drawn(self):
        plain = formfill.candidacy_pdf(self.STUDENT)
        signed = formfill.candidacy_pdf(self.STUDENT, formfill.prepare_signature(signature_png()), datetime.date(2026, 3, 9))
        for pdf in (plain, signed):
            r = pypdf.PdfReader(io.BytesIO(pdf))
            self.assertEqual((len(r.pages), r.get_fields(), list(r.pages[0].get("/Annots") or [])), (1, None, []))      # nothing left to edit
            self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertIn("ADMISSION TO CANDIDACY FORM", pypdf.PdfReader(io.BytesIO(plain)).pages[0].extract_text())
        self.assertNotEqual(plain, signed)
        self.assertGreater(len(signed), len(plain))                                                  # the picture is in there
        self.assertEqual(len(formfill.sha256(signed)), 64)

    def test_unusual_names_do_not_break_the_form(self):
        for name in ("José", "Zoë Łukasz", "O'Brien <b>", "李"):
            self.assertTrue(formfill.candidacy_pdf({**self.STUDENT, "first_name": name}).startswith(b"%PDF"))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.conn = physdb.connect(":memory:")
        self.sid = physdb.add_student(self.conn, university_id="12345678", first_name="Ada", last_name="Lovelace",
                                      email="ada@u.edu", matriculation_year=2022, research_area=None, notes=None)
        self.all6 = [c for c, _, _ in physdb.CORE_COURSES]
        self.sig = formfill.prepare_signature(signature_png())

    def forms(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM forms ORDER BY id")]

    def test_the_sixth_course_creates_the_form_once(self):
        for n in range(1, 6):
            self.assertEqual(workflow.save_course_credits(self.conn, self.sid, self.all6[:n], "ed")["created"], None)
        self.assertEqual(self.forms(), [])
        res = workflow.save_course_credits(self.conn, self.sid, self.all6, "ed")
        f = self.forms()
        self.assertEqual((res["created"], len(f), f[0]["status"], f[0]["created_by"], f[0]["kind"]), (f[0]["id"], 1, "pending_approval", "ed", "candidacy"))
        self.assertTrue(f[0]["pdf"].startswith(b"%PDF")); self.assertEqual(f[0]["pdf_sha256"], formfill.sha256(f[0]["pdf"]))
        self.assertEqual(workflow.save_course_credits(self.conn, self.sid, self.all6, "ed")["created"], None)    # saving again: no second form
        self.assertEqual(len(self.forms()), 1)

    def test_unchecking_a_course_withdraws_a_waiting_form_but_not_a_signed_one(self):
        workflow.save_course_credits(self.conn, self.sid, self.all6, "ed")
        res = workflow.save_course_credits(self.conn, self.sid, self.all6[:5], "ed")
        f = self.forms()[0]
        self.assertEqual((res["withdrawn"], f["status"], f["closed_by"]), ([f["id"]], "withdrawn", "ed"))
        self.assertIn("no longer met", f["closed_reason"])
        res = workflow.save_course_credits(self.conn, self.sid, self.all6, "ed")                 # ticking it again starts a new one
        self.assertEqual(len(self.forms()), 2); self.assertEqual(self.forms()[1]["status"], "pending_approval")
        workflow.approve(self.conn, res["created"], "appr"); workflow.chair_sign(self.conn, res["created"], "chair", self.sig)
        self.assertEqual(workflow.save_course_credits(self.conn, self.sid, self.all6[:3], "ed")["withdrawn"], [])
        self.assertEqual(self.forms()[1]["status"], "complete")

    def test_full_path_records_the_milestone_and_links_the_signed_pdf(self):
        fid = workflow.save_course_credits(self.conn, self.sid, self.all6, "ed")["created"]
        before = self.forms()[0]["pdf"]
        with self.assertRaises(workflow.WorkflowError):                                         # the chair can't skip the approver
            workflow.chair_sign(self.conn, fid, "chair", self.sig)
        workflow.approve(self.conn, fid, "appr")
        f = self.forms()[0]
        self.assertEqual((f["status"], f["approved_by"]), ("pending_chair", "appr")); self.assertTrue(f["approved_at"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM milestones").fetchone()[0], 0)    # not yet
        with self.assertRaises(workflow.WorkflowError) as cm:
            workflow.chair_sign(self.conn, fid, "chair", None)
        self.assertIn("signature", str(cm.exception)); self.assertEqual(self.forms()[0]["status"], "pending_chair")
        mid = workflow.chair_sign(self.conn, fid, "chair", self.sig, today=datetime.date(2026, 5, 4))
        f = self.forms()[0]
        self.assertEqual((f["status"], f["signed_by"], f["milestone_id"]), ("complete", "chair", mid))
        m = self.conn.execute("SELECT * FROM milestones WHERE id = ?", (mid,)).fetchone()
        self.assertEqual((m["student_id"], m["kind"], m["date"], m["outcome"]), (self.sid, "candidacy", "2026-05-04", "passed"))
        self.assertNotEqual(f["pdf"], before); self.assertEqual(f["pdf_sha256"], formfill.sha256(f["pdf"]))
        self.assertEqual(workflow.forms_for_student(self.conn, self.sid)[0]["status"], "complete")
        for step in (lambda: workflow.approve(self.conn, fid, "appr"), lambda: workflow.chair_sign(self.conn, fid, "chair", self.sig)):
            with self.assertRaises(workflow.WorkflowError):                                      # nothing happens twice
                step()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM milestones").fetchone()[0], 1)
        self.assertIsNotNone(workflow.start_blocker(self.conn, self.sid))                         # candidacy is recorded: no new form
        self.conn.execute("DELETE FROM milestones WHERE id = ?", (mid,)); self.conn.commit()    # deleting it keeps the form
        self.assertEqual((self.forms()[0]["status"], self.forms()[0]["milestone_id"]), ("complete", None))

    def test_an_existing_candidacy_milestone_is_linked_not_duplicated(self):
        mid = physdb.add_milestone(self.conn, "12345678", "candidacy", "2025-01-01", "passed") or \
            self.conn.execute("SELECT id FROM milestones").fetchone()[0]
        self.assertIsNotNone(workflow.start_blocker(self.conn, self.sid))                         # nothing to start automatically
        self.assertEqual(workflow.save_course_credits(self.conn, self.sid, self.all6, "ed")["created"], None)
        self.conn.execute("DELETE FROM milestones"); self.conn.commit()
        fid = workflow.start_candidacy_form(self.conn, self.sid, "ed")
        physdb.add_milestone(self.conn, "12345678", "candidacy", "2025-01-01", "passed")          # entered by hand meanwhile
        workflow.approve(self.conn, fid, "appr")
        existing = self.conn.execute("SELECT id FROM milestones").fetchone()[0]
        self.assertEqual(workflow.chair_sign(self.conn, fid, "chair", self.sig), existing)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM milestones").fetchone()[0], 1)

    def test_rejection_needs_a_reason_and_the_right_stage(self):
        physdb.set_course_credits(self.conn, self.sid, self.all6)
        fid = workflow.start_candidacy_form(self.conn, self.sid, "ed")
        with self.assertRaises(workflow.WorkflowError):
            workflow.reject(self.conn, fid, "appr", "  ", "pending_approval")
        with self.assertRaises(workflow.WorkflowError):                                          # the chair can't reject at the approver's stage
            workflow.reject(self.conn, fid, "chair", "no", "pending_chair")
        workflow.reject(self.conn, fid, "appr", "Name is misspelled", "pending_approval")
        f = self.forms()[0]
        self.assertEqual((f["status"], f["closed_by"], f["closed_reason"]), ("rejected", "appr", "Name is misspelled"))
        with self.assertRaises(workflow.WorkflowError):
            workflow.approve(self.conn, fid, "appr")
        fid2 = workflow.start_candidacy_form(self.conn, self.sid, "ed")                          # a fresh one can be started
        workflow.approve(self.conn, fid2, "appr"); workflow.reject(self.conn, fid2, "chair", "Not yet", "pending_chair")
        self.assertEqual(self.forms()[1]["status"], "rejected")

    def test_cannot_start_without_all_six_courses(self):
        physdb.set_course_credits(self.conn, self.sid, self.all6[:5])
        with self.assertRaises(workflow.WorkflowError):
            workflow.start_candidacy_form(self.conn, self.sid, "ed")

    def test_queues(self):
        physdb.set_course_credits(self.conn, self.sid, self.all6)
        fid = workflow.start_candidacy_form(self.conn, self.sid, "ed")
        self.assertEqual([q["id"] for q in workflow.queue(self.conn, "pending_approval")], [fid])
        self.assertEqual(workflow.queue(self.conn, "pending_chair"), []); self.assertEqual(workflow.recent(self.conn), [])
        workflow.reject(self.conn, fid, "appr", "no", "pending_approval")
        self.assertEqual([r["status"] for r in workflow.recent(self.conn)], ["rejected"])


class CommandLineTests(unittest.TestCase):
    def test_course_credit_command_starts_and_withdraws_the_form(self):
        import contextlib
        from unittest import mock
        path = os.path.join(tempfile.mkdtemp(), "cli.db")
        conn = physdb.connect(path)
        physdb.add_student(conn, university_id="12345678", first_name="A", last_name="B", email="a@u.edu",
                           matriculation_year=2022, research_area=None, notes=None); conn.close()
        real = physdb.connect
        def run(*argv):
            out = io.StringIO()
            with mock.patch.object(physdb, "connect", lambda: real(path)), contextlib.redirect_stdout(out):
                physdb.main(list(argv))
            return out.getvalue()
        self.assertIn("none", run("course-credit", "12345678", "cm", "--remove"))
        self.assertIn("CM, QM-1", run("course-credit", "12345678", "cm", "qm1"))
        out = run("course-credit", "12345678", "qm2", "em1", "em2", "sm")
        self.assertIn("CM, QM-1, QM-2, EM-1, EM-2, SM", out); self.assertIn("candidacy form was created", out)
        self.assertIn("withdrawn", run("course-credit", "12345678", "sm", "--remove"))
        conn = real(path)
        self.assertEqual([r["status"] for r in conn.execute("SELECT status FROM forms")], ["withdrawn"])
        self.assertEqual(conn.execute("SELECT created_by FROM forms").fetchone()[0], "command line")


class MigrationTests(unittest.TestCase):
    def test_users_table_gains_the_new_roles_without_losing_anything(self):
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        conn = physdb.connect(path)
        fsr = physdb.add_fsr(conn, "X", "Xx", "x@u.edu", cnet="fsrx")
        physdb.add_faculty(conn, "N", "Bohr", "b@u.edu", cnet="nbohr")
        conn.executescript("""   -- the users table as the FSR release made it
            DROP TABLE users;
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
              role TEXT NOT NULL DEFAULT 'viewer' CHECK (role IN ('viewer', 'editor', 'admin', 'faculty', 'fsr')),
              must_change INTEGER NOT NULL DEFAULT 0,
              faculty_id INTEGER REFERENCES faculty(id) ON DELETE CASCADE, fsr_id INTEGER REFERENCES fsrs(id) ON DELETE CASCADE,
              CHECK ((role = 'faculty') = (faculty_id IS NOT NULL) AND (role = 'fsr') = (fsr_id IS NOT NULL)));
            INSERT INTO users VALUES (1, 'boss', 'h1', 'admin', 0, NULL, NULL), (2, 'nbohr', 'h2', 'faculty', 1, 1, NULL),
                                     (3, 'fsrx', 'h3', 'fsr', 0, NULL, 1);""")
        conn.commit(); conn.close()
        conn = physdb.connect(path)
        self.assertEqual([tuple(r) for r in conn.execute("SELECT id, username, role, must_change, faculty_id, fsr_id, full_name FROM users ORDER BY id")],
                         [(1, "boss", "admin", 0, None, None, None), (2, "nbohr", "faculty", 1, 1, None, None), (3, "fsrx", "fsr", 0, None, 1, None)])
        conn.execute("INSERT INTO users (username, password_hash, role, full_name) VALUES ('ap', 'h', 'approver', 'Ann Approver')")
        conn.execute("INSERT INTO users (username, password_hash, role, full_name) VALUES ('ch', 'h', 'chair', 'Carl Chair')")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO users (username, password_hash, role) VALUES ('bad', 'h', 'president')")
        conn.execute("UPDATE faculty SET cnet = 'niels' WHERE id = 1")                         # the login rules still work
        self.assertEqual(conn.execute("SELECT username FROM users WHERE id = 2").fetchone()[0], "niels")
        conn.commit()
        # a signature is kept across a later rebuild of users, and still points at the users table
        uid = conn.execute("SELECT id FROM users WHERE username = 'ch'").fetchone()[0]
        conn.execute("INSERT INTO signatures VALUES (?, x'89504e47', '2026-01-01')", (uid,)); conn.commit()
        conn.executescript("""ALTER TABLE users ADD COLUMN spare TEXT;""")
        self.assertEqual(conn.execute("PRAGMA foreign_key_list(signatures)").fetchone()["table"], "users")
        conn.close()
        self.assertEqual(physdb.connect(path).execute("SELECT count(*) FROM signatures").fetchone()[0], 1)


    def test_rebuilding_the_milestones_table_keeps_the_forms_pointing_at_it(self):
        path = os.path.join(tempfile.mkdtemp(), "ms.db")
        conn = physdb.connect(path)
        sid = physdb.add_student(conn, university_id="12345678", first_name="A", last_name="B", email="a@u.edu",
                                 matriculation_year=2022, research_area=None, notes=None)
        mid = physdb.add_milestone(conn, "12345678", "candidacy", "2025-01-01", "passed") or 1
        conn.execute("INSERT INTO forms (student_id, kind, status, template, created_at, created_by, pdf, pdf_sha256, milestone_id) "
                     "VALUES (?, 'candidacy', 'complete', 't', 'now', 'x', x'25', 'h', 1)", (sid,)); conn.commit()
        conn.executescript("""PRAGMA foreign_keys = OFF;   -- the milestones table as an earlier release made it (no 'ms_degree')
            CREATE TABLE ms_old AS SELECT * FROM milestones; DROP TABLE milestones;
            CREATE TABLE milestones (id INTEGER PRIMARY KEY, student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
              kind TEXT NOT NULL CHECK (kind IN ('candidacy', 'thesis_defense', 'other')), date TEXT NOT NULL,
              outcome TEXT NOT NULL DEFAULT 'pending', notes TEXT);
            INSERT INTO milestones SELECT * FROM ms_old; DROP TABLE ms_old; PRAGMA foreign_keys = ON;""")
        conn.commit(); conn.close()
        conn = physdb.connect(path)
        self.assertEqual(conn.execute("SELECT count(*) FROM milestones").fetchone()[0], 1)
        self.assertEqual(sorted(r["table"] for r in conn.execute("PRAGMA foreign_key_list(forms)")), ["milestones", "students"])   # (listed in no fixed order, but never a set-aside copy)
        conn.execute("INSERT INTO forms (student_id, kind, template, created_at, created_by, pdf, pdf_sha256) "
                     "VALUES (1, 'candidacy', 't', 'now', 'x', x'25', 'h')")                       # would fail on a dangling reference
        conn.execute("DELETE FROM milestones"); conn.commit()
        self.assertIsNone(conn.execute("SELECT milestone_id FROM forms WHERE id = 1").fetchone()[0])   # SET NULL still works


if __name__ == "__main__":
    unittest.main()
