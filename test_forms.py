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


class MailerTests(unittest.TestCase):
    ENV = {"PHYSDB_SMTP_HOST": "mail.example.edu", "PHYSDB_SMTP_USER": "bot@example.edu", "PHYSDB_SMTP_PASSWORD": "pw"}

    def test_settings_and_defaults(self):
        import mailer
        self.assertIsNone(mailer.settings({})); self.assertIsNone(mailer.settings({"PHYSDB_SMTP_HOST": "  "}))
        cfg = mailer.settings(self.ENV)
        self.assertEqual((cfg["host"], cfg["port"], cfg["security"], cfg["sender"]), ("mail.example.edu", 587, "starttls", "bot@example.edu"))
        cfg = mailer.settings({**self.ENV, "PHYSDB_SMTP_PORT": "465", "PHYSDB_SMTP_SECURITY": "SSL", "PHYSDB_MAIL_FROM": "grads@example.edu",
                               "PHYSDB_BASE_URL": "https://grads.example.edu/"})
        self.assertEqual((cfg["port"], cfg["security"], cfg["sender"], cfg["base_url"]), (465, "ssl", "grads@example.edu", "https://grads.example.edu"))

    def test_sending(self):
        import mailer
        from unittest import mock
        with mock.patch.dict(os.environ, self.ENV, clear=False), mock.patch("smtplib.SMTP") as smtp:
            mailer.send("a@example.edu", "Hello", "Body text")
            server = smtp.return_value.__enter__.return_value
            smtp.assert_called_once_with("mail.example.edu", 587, timeout=mailer.TIMEOUT)
            server.starttls.assert_called_once(); server.login.assert_called_once_with("bot@example.edu", "pw")
            msg = server.send_message.call_args[0][0]
            self.assertEqual((msg["To"], msg["From"], msg["Subject"]), ("a@example.edu", "bot@example.edu", "Hello"))
            self.assertIn("Body text", msg.get_content())
        with mock.patch.dict(os.environ, {**self.ENV, "PHYSDB_SMTP_SECURITY": "ssl"}), mock.patch("smtplib.SMTP_SSL") as ssl_smtp:
            mailer.send("a@example.edu", "Hello", "Body")
            ssl_smtp.return_value.__enter__.return_value.send_message.assert_called_once()
        with mock.patch.dict(os.environ, {**self.ENV, "PHYSDB_SMTP_SECURITY": "none", "PHYSDB_SMTP_USER": "", "PHYSDB_MAIL_FROM": "x@example.edu"}), \
                mock.patch("smtplib.SMTP") as smtp:
            mailer.send("a@example.edu", "Hello", "Body")
            server = smtp.return_value.__enter__.return_value
            server.starttls.assert_not_called(); server.login.assert_not_called()

    def test_problems_become_mail_errors(self):
        import smtplib
        import mailer
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(mailer.MailError):
                mailer.send("a@example.edu", "s", "b")
            self.assertFalse(mailer.enabled())
        for env, word in (({"PHYSDB_SMTP_SECURITY": "carrier-pigeon"}, "SECURITY"), ({"PHYSDB_SMTP_USER": "", "PHYSDB_MAIL_FROM": ""}, "From"),
                          ({"PHYSDB_SMTP_PORT": "abc"}, "PORT")):
            with mock.patch.dict(os.environ, {**self.ENV, **env}, clear=True):
                with self.assertRaises(mailer.MailError) as cm:
                    mailer.send("a@example.edu", "s", "b")
                self.assertIn(word, str(cm.exception))
        for exc in (smtplib.SMTPAuthenticationError(535, b"bad login"), ConnectionRefusedError("down"), TimeoutError("slow")):
            with mock.patch.dict(os.environ, self.ENV, clear=True), mock.patch("smtplib.SMTP") as smtp:
                smtp.return_value.__enter__.return_value.send_message.side_effect = exc
                with self.assertRaises(mailer.MailError):
                    mailer.send("a@example.edu", "s", "b")


class NotifyTests(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        import mailer, notify
        self.notify, self.mailer = notify, mailer
        path = os.path.join(tempfile.mkdtemp(), "n.db")
        self.conn = physdb.connect(path)
        self.sid = physdb.add_student(self.conn, university_id="12345678", first_name="Secret", last_name="Studentname",
                                      email="s@u.edu", matriculation_year=2022, research_area=None, notes=None)
        for name, role, email in (("a1", "approver", "a1@u.edu"), ("a2", "approver", "a2@u.edu"), ("a3", "approver", None),
                                  ("c1", "chair", "c1@u.edu"), ("e1", "editor", "e1@u.edu")):
            self.conn.execute("INSERT INTO users (username, password_hash, role, full_name, email) VALUES (?, 'h', ?, ?, ?)", (name, role, name, email))
        self.conn.commit()
        self.sent = []
        env = mock.patch.dict(os.environ, {"PHYSDB_SMTP_HOST": "mail.example.edu", "PHYSDB_BASE_URL": "https://grads.example.edu"}); env.start()
        send = mock.patch.object(mailer, "send", lambda to, subject, body: self.sent.append((to, subject, body))); send.start()
        asyn = mock.patch.object(notify, "ASYNC", False); asyn.start()
        for p in (env, send, asyn):
            self.addCleanup(p.stop)
        self.all6 = [c for c, _, _ in physdb.CORE_COURSES]

    def test_a_new_form_emails_the_approvers_and_an_approval_emails_the_chair(self):
        workflow.save_course_credits(self.conn, self.sid, self.all6, "ed")
        self.assertEqual(sorted(t for t, _, _ in self.sent), ["a1@u.edu", "a2@u.edu"])            # approvers with an address; not the editor or the chair
        to, subject, body = self.sent[0]
        self.assertIn("waiting for your approval", subject); self.assertIn("1 candidacy form is waiting", body)
        self.assertIn("https://grads.example.edu/approvals", body)
        for text in (subject, body):
            self.assertNotIn("Secret", text); self.assertNotIn("Studentname", text); self.assertNotIn("12345678", text)   # no student details by email
        self.sent.clear()
        workflow.approve(self.conn, self.conn.execute("SELECT id FROM forms").fetchone()[0], "a1")
        self.assertEqual([(t, "signature" in s) for t, s, _ in self.sent], [("c1@u.edu", True)])
        self.sent.clear()
        fid = self.conn.execute("SELECT id FROM forms").fetchone()[0]
        workflow.reject(self.conn, fid, "c1", "no", "pending_chair")                              # rejecting sends nothing
        self.assertEqual(self.sent, [])

    def test_without_email_set_up_nothing_is_sent_and_nothing_breaks(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"PHYSDB_SMTP_HOST": ""}):
            workflow.save_course_credits(self.conn, self.sid, self.all6, "ed")
        self.assertEqual(self.sent, []); self.assertEqual(self.conn.execute("SELECT status FROM forms").fetchone()[0], "pending_approval")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM email_log").fetchone()[0], 0)

    def test_a_mail_failure_is_logged_not_raised(self):
        from unittest import mock
        def broken(to, subject, body):
            raise self.mailer.MailError("SMTPAuthenticationError: bad login")
        with mock.patch.object(self.mailer, "send", broken):
            workflow.save_course_credits(self.conn, self.sid, self.all6, "ed")                      # still creates the form
        self.assertEqual(self.conn.execute("SELECT status FROM forms").fetchone()[0], "pending_approval")
        rows = self.conn.execute("SELECT to_addr, ok, error FROM email_log ORDER BY id").fetchall()
        self.assertEqual([(r[0], r[1]) for r in rows], [("a1@u.edu", 0), ("a2@u.edu", 0)]); self.assertIn("bad login", rows[0][2])

    def test_successes_are_logged_and_the_log_is_trimmed(self):
        workflow.save_course_credits(self.conn, self.sid, self.all6, "ed")
        self.assertEqual([r[0] for r in self.conn.execute("SELECT ok FROM email_log")], [1, 1])
        for i in range(250):
            self.notify._log(self.conn.execute("PRAGMA database_list").fetchone()[2], f"x{i}@u.edu", "s", None)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM email_log").fetchone()[0], self.notify.KEEP_LOG)
        self.assertEqual(self.conn.execute("SELECT to_addr FROM email_log ORDER BY id DESC LIMIT 1").fetchone()[0], "x249@u.edu")

    def test_reminders(self):
        self.assertEqual(self.notify.send_reminders(self.conn), 0); self.assertEqual(self.sent, [])          # nothing waiting
        workflow.save_course_credits(self.conn, self.sid, self.all6, "ed"); self.sent.clear()
        self.assertEqual(self.notify.send_reminders(self.conn), 2)
        self.assertTrue(all(s.startswith("Reminder: ") for _, s, _ in self.sent)); self.assertEqual(sorted(t for t, _, _ in self.sent), ["a1@u.edu", "a2@u.edu"])
        workflow.approve(self.conn, self.conn.execute("SELECT id FROM forms").fetchone()[0], "a1"); self.sent.clear()
        self.assertEqual(self.notify.send_reminders(self.conn), 1); self.assertEqual(self.sent[0][0], "c1@u.edu")

    def test_background_sending_does_not_block_and_still_delivers(self):
        import threading, time
        from unittest import mock
        gate, done = threading.Event(), []
        def slow(to, subject, body):
            gate.wait(5); done.append(to)
        with mock.patch.object(self.notify, "ASYNC", True), mock.patch.object(self.mailer, "send", slow):
            start = time.time()
            workflow.save_course_credits(self.conn, self.sid, self.all6, "ed")
            self.assertLess(time.time() - start, 2); self.assertEqual(done, [])                      # returned while the mail was still "sending"
            gate.set()
            for _ in range(50):
                if len(done) == 2:
                    break
                time.sleep(0.1)
        self.assertEqual(sorted(done), ["a1@u.edu", "a2@u.edu"])


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
        # reminders need email to be set up
        with self.assertRaises(SystemExit) as cm, mock.patch.dict(os.environ, {"PHYSDB_SMTP_HOST": ""}):
            run("send-reminders")
        self.assertIn("not set up", str(cm.exception))


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
