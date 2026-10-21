"""The candidacy-form approval process.

When a student has credit for all six core courses, a candidacy form is filled in and put in a queue:

    pending_approval --(approver approves)--> pending_chair --(chair signs)--> complete

Signing adds the chair's signature image and the date, stores the signed PDF, and records the Candidacy milestone
(linked to that PDF). Either person can instead reject the form, with a reason. Everything here works on an open
database connection, commits its own changes, and refuses a step that is not valid for the form's current status
(so two people acting at once can't both succeed).
"""
import datetime

import formfill
import physdb

PENDING = ("pending_approval", "pending_chair")
STATUS_NAMES = {"pending_approval": "Waiting for an approver", "pending_chair": "Waiting for the chair's signature",
                "complete": "Signed", "rejected": "Rejected", "withdrawn": "Withdrawn"}


class WorkflowError(Exception):
    """A step that can't be done (wrong status, missing signature, ...). The message is for the user."""


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def active_form(conn, student_id):
    """The student's candidacy form that is still in the queue, if any."""
    return conn.execute("SELECT * FROM forms WHERE student_id = ? AND kind = 'candidacy' AND status IN ('pending_approval', 'pending_chair')",
                        (student_id,)).fetchone()


def has_passed_candidacy(conn, student_id):
    return conn.execute("SELECT id FROM milestones WHERE student_id = ? AND kind = 'candidacy' AND outcome IN ('passed', 'conditional') "
                        "ORDER BY date LIMIT 1", (student_id,)).fetchone()


def start_blocker(conn, student_id):
    """Why a candidacy form can't be started for this student right now, or None if it can."""
    if len(physdb.course_credits(conn, student_id)) < len(physdb.CORE_COURSES):
        return "The student does not have credit for all six core courses yet."
    if active_form(conn, student_id):
        return "A candidacy form is already waiting for approval."
    if has_passed_candidacy(conn, student_id):
        return "Candidacy is already recorded for this student."
    return None


def start_candidacy_form(conn, student_id, actor):
    """Fill in the candidacy form and put it in the approvers' queue. Returns the new form id."""
    reason = start_blocker(conn, student_id)
    if reason:
        raise WorkflowError(reason)
    student = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
    pdf = formfill.candidacy_pdf(student)
    cur = conn.execute(
        """INSERT INTO forms (student_id, kind, status, template, created_at, created_by, pdf, pdf_sha256)
           VALUES (?, 'candidacy', 'pending_approval', ?, ?, ?, ?, ?)""",
        (student_id, formfill.CANDIDACY_TEMPLATE, _now(), actor, pdf, formfill.sha256(pdf)))
    conn.commit()
    return cur.lastrowid


def save_course_credits(conn, student_id, codes, actor):
    """Record the student's core-course credits. Returns {"created": form id or None, "withdrawn": [form ids]}:
    completing the sixth course starts the candidacy form; dropping below six withdraws a form that is still waiting."""
    before = physdb.course_credits(conn, student_id)
    physdb.set_course_credits(conn, student_id, codes)
    after = physdb.course_credits(conn, student_id)
    out = {"created": None, "withdrawn": []}
    if len(after) == len(physdb.CORE_COURSES) and len(before) < len(physdb.CORE_COURSES):
        if start_blocker(conn, student_id) is None:
            out["created"] = start_candidacy_form(conn, student_id, actor)
    elif len(after) < len(physdb.CORE_COURSES):
        form = active_form(conn, student_id)
        if form:
            _close(conn, form["id"], "withdrawn", actor, "A core course was unchecked, so the requirement is no longer met.")
            out["withdrawn"].append(form["id"])
    return out


def _close(conn, form_id, status, actor, reason):
    cur = conn.execute("UPDATE forms SET status = ?, closed_at = ?, closed_by = ?, closed_reason = ? "
                       "WHERE id = ? AND status IN ('pending_approval', 'pending_chair')",
                       (status, _now(), actor, reason, form_id))
    conn.commit()
    return cur.rowcount == 1


def approve(conn, form_id, actor):
    """An approver passes the form on to the chair."""
    cur = conn.execute("UPDATE forms SET status = 'pending_chair', approved_at = ?, approved_by = ? "
                       "WHERE id = ? AND status = 'pending_approval'", (_now(), actor, form_id))
    conn.commit()
    if cur.rowcount != 1:
        raise WorkflowError("This form is no longer waiting for an approver (someone may have already dealt with it).")


def reject(conn, form_id, actor, reason, stage):
    """An approver (stage 'pending_approval') or the chair (stage 'pending_chair') sends the form back, with a reason."""
    reason = (reason or "").strip()
    if not reason:
        raise WorkflowError("Please give a reason for rejecting the form.")
    cur = conn.execute("UPDATE forms SET status = 'rejected', closed_at = ?, closed_by = ?, closed_reason = ? "
                       "WHERE id = ? AND status = ?", (_now(), actor, reason[:500], form_id, stage))
    conn.commit()
    if cur.rowcount != 1:
        raise WorkflowError("This form is no longer waiting for you (someone may have already dealt with it).")


def chair_sign(conn, form_id, actor, signature_png, today=None):
    """The chair signs: the signature image and the date are added to a fresh copy of the form (from the student's
    current record), the PDF is stored, and the Candidacy milestone is recorded and linked to it. Returns the milestone id."""
    today = today or datetime.date.today()
    form = conn.execute("SELECT * FROM forms WHERE id = ?", (form_id,)).fetchone()
    if form is None or form["status"] != "pending_chair":
        raise WorkflowError("This form is no longer waiting for the chair's signature.")
    if not signature_png:
        raise WorkflowError("Upload your signature image first (the Signature link at the top).")
    student = conn.execute("SELECT * FROM students WHERE id = ?", (form["student_id"],)).fetchone()
    pdf = formfill.candidacy_pdf(student, signature_png, today)
    advisor_copy = formfill.candidacy_pdf(student, signature_png, today, hide_id=True)     # what advisors may see
    try:
        existing = has_passed_candidacy(conn, form["student_id"])
        if existing:
            milestone_id = existing["id"]                      # already recorded by hand: link to it instead of adding another
        else:
            milestone_id = conn.execute(
                "INSERT INTO milestones (student_id, kind, date, outcome, notes) VALUES (?, 'candidacy', ?, 'passed', ?)",
                (form["student_id"], today.isoformat(), "Admission to candidacy form signed.")).lastrowid
        cur = conn.execute(
            "UPDATE forms SET status = 'complete', signed_at = ?, signed_by = ?, pdf = ?, pdf_sha256 = ?, pdf_faculty = ?, "
            "milestone_id = ? WHERE id = ? AND status = 'pending_chair'",
            (_now(), actor, pdf, formfill.sha256(pdf), advisor_copy, milestone_id, form_id))
        if cur.rowcount != 1:
            raise WorkflowError("This form is no longer waiting for the chair's signature.")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return milestone_id


def forms_for_student(conn, student_id):
    """Every candidacy form for the student, newest first (without the PDF bytes)."""
    return conn.execute("SELECT id, student_id, kind, status, created_at, created_by, approved_at, approved_by, signed_at, "
                        "signed_by, closed_at, closed_by, closed_reason, milestone_id FROM forms WHERE student_id = ? "
                        "ORDER BY id DESC", (student_id,)).fetchall()


def queue(conn, status):
    """Forms in a given status with the student's name, oldest first (for the approvers' and chair's pages)."""
    return conn.execute("SELECT f.id, f.status, f.created_at, f.approved_at, f.approved_by, s.id AS student_id, s.first_name, "
                        "s.last_name, s.university_id FROM forms f JOIN students s ON s.id = f.student_id "
                        "WHERE f.status = ? ORDER BY f.id", (status,)).fetchall()


def recent(conn, limit=20):
    """The most recently finished forms (signed, rejected or withdrawn)."""
    return conn.execute("SELECT f.id, f.status, f.created_at, f.signed_at, f.signed_by, f.closed_at, f.closed_by, f.closed_reason, "
                        "s.id AS student_id, s.first_name, s.last_name FROM forms f JOIN students s ON s.id = f.student_id "
                        "WHERE f.status IN ('complete', 'rejected', 'withdrawn') "
                        "ORDER BY COALESCE(f.signed_at, f.closed_at) DESC, f.id DESC LIMIT ?", (limit,)).fetchall()
