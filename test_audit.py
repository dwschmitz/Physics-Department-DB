import json
import os
import sqlite3
import tempfile
import threading
import unittest

import audit
import physdb


def fresh(actor="tester"):
    path = os.path.join(tempfile.mkdtemp(), "a.db")
    return path, physdb.connect(path, actor=actor)


def log(conn, **where):
    sql = "SELECT * FROM audit_log" + (" WHERE " + " AND ".join(f"{k} = ?" for k in where) if where else "") + " ORDER BY id"
    return [dict(r) for r in conn.execute(sql, tuple(where.values()))]


def student(conn, uid="12345678", **kw):
    return physdb.add_student(conn, university_id=uid, first_name=kw.get("first", "Ada"), last_name="Lovelace",
                              email=f"{uid}@u.edu", matriculation_year=2022, research_area=None, notes=kw.get("notes"))


class TriggerTests(unittest.TestCase):
    def test_insert_update_delete_are_recorded_with_before_and_after(self):
        path, conn = fresh("ed")
        sid = student(conn)
        conn.execute("UPDATE students SET research_area = 'optics', status = 'on_leave' WHERE id = ?", (sid,)); conn.commit()
        conn.execute("DELETE FROM students WHERE id = ?", (sid,)); conn.commit()
        rows = log(conn)
        self.assertEqual([(r["action"], r["entity"], r["actor"], r["row_id"], r["student_id"], r["label"]) for r in rows],
                         [("insert", "students", "ed", sid, sid, "Ada Lovelace"), ("update", "students", "ed", sid, sid, "Ada Lovelace"),
                          ("delete", "students", "ed", sid, sid, "Ada Lovelace")])
        ins, upd, dele = (json.loads(rows[0]["new_values"]), rows[1], json.loads(rows[2]["old_values"]))
        self.assertEqual((ins["university_id"], ins["status"], rows[0]["old_values"]), ("12345678", "active", None))
        self.assertEqual((json.loads(upd["old_values"])["status"], json.loads(upd["new_values"])["status"]), ("active", "on_leave"))
        self.assertEqual(dele["research_area"], "optics"); self.assertIsNone(rows[2]["new_values"])
        self.assertTrue(rows[0]["at"].startswith("20") and "T" in rows[0]["at"])

    def test_saving_without_changing_anything_logs_nothing(self):
        path, conn = fresh()
        sid = student(conn); before = len(log(conn))
        conn.execute("UPDATE students SET status = status, notes = notes WHERE id = ?", (sid,)); conn.commit()
        self.assertEqual(len(log(conn)), before)

    def test_every_record_table_is_audited_and_student_links_are_kept(self):
        path, conn = fresh()
        sid = student(conn)
        fid = physdb.add_faculty(conn, "Niels", "Bohr", "b@u.edu", cnet="nbohr")
        rid = physdb.add_fsr(conn, "Xavier", "Xx", "x@u.edu")
        physdb.assign_advisor(conn, "12345678", "b@u.edu")
        physdb.add_milestone(conn, "12345678", "candidacy", "2025-01-01", "passed")
        physdb.add_funding(conn, "12345678", "RA", 2026, "Autumn", amount=5) if hasattr(physdb, "add_funding") else \
            conn.execute("INSERT INTO funding (student_id, kind, term_year, term_season, amount) VALUES (?, 'RA', 2026, 'Autumn', 5)", (sid,))
        conn.commit()
        physdb.set_course_credits(conn, sid, ["cm", "sm"])
        entities = [(r["entity"], r["student_id"]) for r in log(conn)]
        for table, linked in (("students", sid), ("faculty", None), ("fsrs", None), ("advising", sid), ("milestones", sid),
                              ("funding", sid), ("course_credits", sid)):
            self.assertIn((table, linked), entities, table)
        self.assertEqual([r["label"] for r in log(conn, entity="advising")], ["Ada Lovelace"])      # named after the student
        self.assertEqual(sorted(json.loads(r["new_values"])["course"] for r in log(conn, entity="course_credits")), ["cm", "sm"])

    def test_course_credits_log_only_what_changed(self):
        path, conn = fresh()
        sid = student(conn)
        physdb.set_course_credits(conn, sid, ["cm", "qm1"])
        n = len(log(conn, entity="course_credits"))
        physdb.set_course_credits(conn, sid, ["cm", "qm1"])                                           # saving the same boxes again
        self.assertEqual(len(log(conn, entity="course_credits")), n)
        physdb.set_course_credits(conn, sid, ["cm", "em1"])
        added = log(conn, entity="course_credits")[n:]
        self.assertEqual([(r["action"], json.loads(r["new_values"] or r["old_values"])["course"]) for r in added],
                         [("delete", "qm1"), ("insert", "em1")])

    def test_passwords_pdfs_and_signature_images_never_enter_the_log(self):
        path, conn = fresh("admin")
        conn.execute("INSERT INTO users (username, password_hash, role, full_name, email) VALUES ('ch', 'scrypt:SECRETHASH', 'chair', 'C', 'c@u.edu')")
        conn.commit()
        conn.execute("UPDATE users SET password_hash = 'scrypt:OTHERHASH', must_change = 1 WHERE username = 'ch'"); conn.commit()
        uid = conn.execute("SELECT id FROM users WHERE username = 'ch'").fetchone()[0]
        conn.execute("INSERT INTO signatures VALUES (?, x'89504e47DEADBEEF', 'now')", (uid,))
        conn.execute("UPDATE signatures SET image = x'00112233' WHERE user_id = ?", (uid,)); conn.commit()
        sid = student(conn)
        conn.execute("INSERT INTO forms (student_id, kind, template, created_at, created_by, pdf, pdf_sha256) VALUES (?, 'candidacy', 't', 'n', 'x', x'25504446', 'hash')", (sid,))
        conn.execute("UPDATE forms SET status = 'pending_chair', pdf = x'0000' WHERE student_id = ?", (sid,)); conn.commit()
        blob = " ".join(f"{r['old_values']} {r['new_values']}" for r in log(conn))
        for secret in ("SECRETHASH", "OTHERHASH", "scrypt", "DEADBEEF", "89504e47", "25504446"):
            self.assertNotIn(secret, blob)
        pw = [r for r in log(conn, entity="users") if r["action"] == "update"][0]
        self.assertEqual(json.loads(pw["new_values"])["password_hash_changed"], 1)                     # noted, not copied
        self.assertEqual(json.loads(pw["new_values"])["must_change"], 1)
        self.assertEqual(sorted(json.loads(pw["new_values"])), ["email", "faculty_id", "fsr_id", "full_name", "id", "must_change", "password_hash_changed", "role", "username"])
        self.assertEqual(len(log(conn, entity="signatures")), 2)                                       # the image change is recorded

    def test_deleting_a_student_logs_the_records_that_went_with_them(self):
        path, conn = fresh("ad")
        sid = student(conn)
        physdb.add_milestone(conn, "12345678", "candidacy", "2025-01-01", "passed"); physdb.set_course_credits(conn, sid, ["cm"])
        n = len(log(conn))
        conn.execute("DELETE FROM students WHERE id = ?", (sid,)); conn.commit()
        gone = log(conn)[n:]
        self.assertEqual(sorted((r["entity"], r["action"]) for r in gone),
                         [("course_credits", "delete"), ("milestones", "delete"), ("students", "delete")])
        self.assertTrue(all(r["actor"] == "ad" for r in gone))


class ActorTests(unittest.TestCase):
    def test_who_is_recorded_per_commit_and_note(self):
        path, conn = fresh("alice")
        sid = student(conn)
        conn.actor = "bob"; conn.audit_note = "student import from x.xlsx"
        conn.execute("UPDATE students SET notes = 'n' WHERE id = ?", (sid,)); conn.commit()
        conn.audit_note = None
        conn.execute("UPDATE students SET notes = 'm' WHERE id = ?", (sid,)); conn.commit()
        self.assertEqual([(r["actor"], r["note"]) for r in log(conn)], [("alice", None), ("bob", "student import from x.xlsx"), ("bob", None)])

    def test_with_block_and_rollback(self):
        path, conn = fresh("carol")
        sid = student(conn)
        with conn:
            conn.execute("UPDATE students SET notes = 'a' WHERE id = ?", (sid,))
        self.assertEqual(log(conn)[-1]["actor"], "carol")
        conn.execute("UPDATE students SET notes = 'b' WHERE id = ?", (sid,)); conn.rollback()
        try:
            with conn:
                conn.execute("UPDATE students SET notes = 'c' WHERE id = ?", (sid,))
                raise RuntimeError("stop")
        except RuntimeError:
            pass
        self.assertEqual(conn.execute("SELECT notes FROM students").fetchone()[0], "a")
        self.assertEqual([r["action"] for r in log(conn)], ["insert", "update"])                      # undone changes leave no trace

    def test_changes_from_outside_the_app_have_no_actor_and_are_not_given_to_someone_else(self):
        path, conn = fresh("alice")
        sid = student(conn)
        outside = sqlite3.connect(path)                                                               # e.g. a database browser
        outside.execute("UPDATE students SET notes = 'edited elsewhere' WHERE id = ?", (sid,)); outside.commit(); outside.close()
        conn.execute("UPDATE students SET research_area = 'optics' WHERE id = ?", (sid,)); conn.commit()
        rows = log(conn)
        self.assertEqual([r["actor"] for r in rows], ["alice", None, "alice"])
        self.assertEqual(json.loads(rows[1]["new_values"])["notes"], "edited elsewhere")

    def test_two_people_writing_at_once_each_get_their_own_changes(self):
        path, a = fresh("anna")
        s1, s2 = student(a, "11111111"), student(a, "22222222")
        a.execute("UPDATE students SET notes = 'by anna' WHERE id = ?", (s1,))              # anna's transaction is open...
        errors = []
        def ben():
            try:
                b = physdb.connect(path, actor="ben")
                b.execute("UPDATE students SET notes = 'by ben' WHERE id = ?", (s2,)); b.commit()
            except Exception as e:                                                                    # (waits for anna's lock, then goes)
                errors.append(e)
        t = threading.Thread(target=ben); t.start()
        a.commit(); t.join(10)
        self.assertEqual(errors, [])
        by = {json.loads(r["new_values"])["notes"]: r["actor"] for r in log(a) if r["action"] == "update"}
        self.assertEqual(by, {"by anna": "anna", "by ben": "ben"})

    def test_events_are_recorded_with_their_actor(self):
        path, conn = fresh("x")
        audit.event(conn, "dana", "Signed in"); conn.commit()
        r = log(conn, action="event")[0]
        self.assertEqual((r["actor"], r["entity"], r["label"]), ("dana", "session", "Signed in"))


class InstallTests(unittest.TestCase):
    def test_triggers_are_installed_once_and_left_alone_afterwards(self):
        path, conn = fresh()
        names = lambda c: sorted(r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'audit_%'"))
        self.assertEqual(len(names(conn)), 3 * len(audit.TABLES))
        first = conn.execute("SELECT value FROM audit_meta WHERE key = 'triggers'").fetchone()[0]
        before = conn.total_changes
        conn2 = physdb.connect(path)
        self.assertEqual(conn2.execute("SELECT value FROM audit_meta WHERE key = 'triggers'").fetchone()[0], first)
        self.assertEqual(names(conn2), names(conn)); self.assertEqual(conn2.total_changes, 0)          # connecting again writes nothing

    def test_triggers_follow_a_changed_table_and_survive_rebuilds(self):
        path, conn = fresh("x")
        sid = student(conn)
        conn.execute("ALTER TABLE students ADD COLUMN spare TEXT"); conn.commit()
        conn = physdb.connect(path, actor="x")                                                        # notices the new column
        conn.execute("UPDATE students SET spare = 'hello' WHERE id = ?", (sid,)); conn.commit()
        self.assertEqual(json.loads(log(conn)[-1]["new_values"])["spare"], "hello")
        # an old milestones table gets rebuilt: no log noise, and the triggers are back afterwards
        physdb.add_milestone(conn, "12345678", "candidacy", "2025-01-01", "passed")
        n = len(log(conn))
        audit.drop_triggers(conn)
        conn.executescript("""PRAGMA foreign_keys = OFF; CREATE TABLE ms_old AS SELECT * FROM milestones; DROP TABLE milestones;
            CREATE TABLE milestones (id INTEGER PRIMARY KEY, student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
              kind TEXT NOT NULL CHECK (kind IN ('candidacy', 'thesis_defense', 'other')), date TEXT NOT NULL,
              outcome TEXT NOT NULL DEFAULT 'pending', notes TEXT);
            INSERT INTO milestones SELECT * FROM ms_old; DROP TABLE ms_old; PRAGMA foreign_keys = ON;""")
        conn.close()
        conn = physdb.connect(path, actor="y")
        self.assertEqual(len(log(conn)), n)                                                           # the rebuild itself wrote nothing
        conn.execute("UPDATE milestones SET notes = 'fixed' WHERE kind = 'candidacy'"); conn.commit()
        self.assertEqual((log(conn)[-1]["entity"], log(conn)[-1]["actor"]), ("milestones", "y"))

    def test_describe_gives_plain_lines(self):
        d = audit.describe
        self.assertEqual(d("students", "update", json.dumps({"status": "active", "notes": None}), json.dumps({"status": "graduated", "notes": "hi"}),
                           lambda k, v: str(v).capitalize() if k == "status" else v),
                         ["Status: Active → Graduated", "Notes: (empty) → hi"])
        self.assertEqual(d("students", "insert", None, json.dumps({"id": 1, "first_name": "Ada", "notes": None, "cnet": "ada"})),
                         ["First name: Ada", "CNET: ada"])
        self.assertEqual(d("users", "update", "{}", json.dumps({"password_hash_changed": 1})), ["Password changed"])
        self.assertEqual(d("students", "delete", json.dumps({"first_name": "Ada"}), None), ["First name: Ada"])
        self.assertIn("…", d("students", "insert", None, json.dumps({"notes": "x" * 500}))[0])


if __name__ == "__main__":
    unittest.main()
