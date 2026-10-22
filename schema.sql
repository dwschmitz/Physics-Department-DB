PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS students (
    id                  INTEGER PRIMARY KEY,
    university_id       TEXT UNIQUE NOT NULL,
    cnet                TEXT,   -- CNET ID, lowercase; unique when present (index made in physdb.connect)
    first_name          TEXT NOT NULL,
    last_name           TEXT NOT NULL,
    email               TEXT UNIQUE NOT NULL,
    -- citizen / permanent_resident / international. Deliberately no CHECK here: the students table
    -- is referenced by other tables, so SQLite can't rebuild it later to change a CHECK. The
    -- web form and CLI only offer the allowed values.
    citizenship         TEXT,
    status              TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'on_leave', 'graduated', 'withdrawn')),
    matriculation_year  INTEGER NOT NULL,
    matriculation_term  TEXT NOT NULL DEFAULT 'Autumn',  -- Autumn/Winter/Spring/Summer (checked by the app)
    transfer_year       INTEGER,  -- set for a transfer student: the term they transferred in (year + season),
    transfer_term       TEXT,     -- which can differ from the matriculation term; both empty otherwise
    graduation_year     INTEGER,  -- set when status is 'graduated': the term they graduated (year + season)
    graduation_term     TEXT,     -- Autumn/Winter/Spring/Summer (checked by the app)
    withdrawal_year     INTEGER,  -- the same for status 'withdrawn': the term they left
    withdrawal_term     TEXT,
    research_area       TEXT,
    notes               TEXT
);

-- Financial services representatives: they help manage funding accounts for faculty and the department.
CREATE TABLE IF NOT EXISTS fsrs (
    id          INTEGER PRIMARY KEY,
    first_name  TEXT NOT NULL,
    last_name   TEXT NOT NULL,
    email       TEXT UNIQUE NOT NULL,
    cnet        TEXT    -- CNET ID, lowercase; unique when present (index made in physdb.connect)
);

CREATE TABLE IF NOT EXISTS faculty (
    id             INTEGER PRIMARY KEY,
    first_name     TEXT NOT NULL,
    last_name      TEXT NOT NULL,
    email          TEXT UNIQUE NOT NULL,
    cnet           TEXT,   -- CNET ID, lowercase; unique when present (index made in physdb.connect)
    research_area  TEXT,
    fsr_id         INTEGER REFERENCES fsrs(id) ON DELETE SET NULL   -- this faculty member's FSR, if any
);

-- A student may have several advisors / committee members over time.
CREATE TABLE IF NOT EXISTS advising (
    id          INTEGER PRIMARY KEY,
    student_id  INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    faculty_id  INTEGER NOT NULL REFERENCES faculty(id),
    role        TEXT NOT NULL DEFAULT 'advisor'
                CHECK (role IN ('advisor', 'co-advisor', 'committee', 'committee_chair')),
    start_date  TEXT NOT NULL,
    end_date    TEXT
);

CREATE TABLE IF NOT EXISTS milestones (
    id          INTEGER PRIMARY KEY,
    student_id  INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL
                -- The two old exam types and the four GDE types are retired: no longer offered for new entries, but still
                -- allowed here so entries saved earlier keep their meaning (see physdb.RETIRED_MILESTONE_KINDS).
                -- (GDEs are now recorded as core course credits, see course_credits below.)
                CHECK (kind IN ('gde_classical_mechanics', 'gde_quantum_mechanics', 'gde_electrodynamics',
                                'gde_statistical_mechanics', 'candidacy', 'ms_degree', 'committee_formed', 'pre_oral',
                                'thesis_defense', 'other', 'qualifying_exam', 'thesis_proposal')),
    date        TEXT NOT NULL,
    outcome     TEXT NOT NULL DEFAULT 'pending'
                CHECK (outcome IN ('pending', 'passed', 'conditional', 'failed')),
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS funding (
    id          INTEGER PRIMARY KEY,
    student_id  INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('TA', 'RA', 'fellowship', 'supplement', 'other')),
    source      TEXT,
    poetaf_coa  TEXT,                   -- POETAF/COA account number
    term_year   INTEGER CHECK (term_year BETWEEN 1950 AND 2100),
    term_season TEXT CHECK (term_season IN ('Winter', 'Spring', 'Summer', 'Autumn')),
    legacy_term TEXT,                   -- free-text term from before the fixed menu; re-enter it
    amount      REAL,
    notes       TEXT,
    CHECK ((term_year IS NULL) = (term_season IS NULL))
);

-- The six core courses a student needs credit for before candidacy (by passing the course or testing out of it with
-- a GDE). One row per course credited; the codes are listed in physdb.CORE_COURSES.
CREATE TABLE IF NOT EXISTS course_credits (
    student_id  INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    course      TEXT NOT NULL CHECK (course IN ('cm', 'qm1', 'qm2', 'em1', 'em2', 'sm')),
    PRIMARY KEY (student_id, course)
);

-- Forms generated for a student (currently only the candidacy form) and where each is in the approval process:
-- pending_approval (waiting for an approver) -> pending_chair (waiting for the chair's signature) -> complete
-- (signed; the milestone is recorded), or rejected / withdrawn. The PDF itself is stored here. People are recorded by
-- username as text so the history survives an account being deleted.
CREATE TABLE IF NOT EXISTS forms (
    id            INTEGER PRIMARY KEY,
    student_id    INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('candidacy')),
    status        TEXT NOT NULL DEFAULT 'pending_approval'
                  CHECK (status IN ('pending_approval', 'pending_chair', 'complete', 'rejected', 'withdrawn')),
    template      TEXT NOT NULL,        -- which version of the blank form was filled in
    created_at    TEXT NOT NULL, created_by TEXT NOT NULL,
    approved_at   TEXT, approved_by TEXT,
    signed_at     TEXT, signed_by TEXT,
    closed_at     TEXT, closed_by TEXT, closed_reason TEXT,        -- rejected or withdrawn
    milestone_id  INTEGER REFERENCES milestones(id) ON DELETE SET NULL,   -- the milestone recorded when it was signed
    pdf           BLOB NOT NULL,
    pdf_sha256    TEXT NOT NULL,
    pdf_faculty   BLOB                  -- the signed form again with the University ID left off, for advisors to see
);
CREATE INDEX IF NOT EXISTS idx_forms_student ON forms(student_id);

-- The audit log (see audit.py): who changed what, and when. Written by triggers; never edited by the app.
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY,
    at          TEXT NOT NULL,            -- local time of the change
    actor       TEXT,                     -- the username (or "command line"); empty if the change came from outside the app
    note        TEXT,                     -- a few words of context, e.g. "student import"
    action      TEXT NOT NULL,            -- insert / update / delete / event
    entity      TEXT NOT NULL,            -- the table changed (or "session" for sign-ins)
    row_id      INTEGER,
    student_id  INTEGER,                  -- the student this concerns, if any (no link: the student may be deleted later)
    label       TEXT,                     -- who or what it was, in words, at the time
    old_values  TEXT,                     -- JSON, for update and delete
    new_values  TEXT                      -- JSON, for insert and update
);
CREATE INDEX IF NOT EXISTS idx_audit_at      ON audit_log(at);
CREATE INDEX IF NOT EXISTS idx_audit_student ON audit_log(student_id, id);
CREATE INDEX IF NOT EXISTS idx_audit_actor   ON audit_log(actor, id);
CREATE TABLE IF NOT EXISTS audit_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- Failed sign-in attempts, so repeated wrong passwords can be slowed down (see webapp LOGIN_* settings).
CREATE TABLE IF NOT EXISTS login_failures (
    at        REAL NOT NULL,            -- time.time() of the failed attempt
    username  TEXT NOT NULL,            -- as typed, lowercased (whether or not such an account exists)
    ip        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_failures_user ON login_failures(username, at);
CREATE INDEX IF NOT EXISTS idx_login_failures_ip   ON login_failures(ip, at);

-- A short record of the emails the app tried to send (newest 200 are kept), so a mail problem can be seen and fixed.
CREATE TABLE IF NOT EXISTS email_log (
    id       INTEGER PRIMARY KEY,
    at       TEXT NOT NULL,
    to_addr  TEXT NOT NULL,
    subject  TEXT NOT NULL,
    ok       INTEGER NOT NULL,
    error    TEXT
);

CREATE INDEX IF NOT EXISTS idx_advising_student   ON advising(student_id);
CREATE INDEX IF NOT EXISTS idx_milestones_student ON milestones(student_id);
CREATE INDEX IF NOT EXISTS idx_funding_student    ON funding(student_id);

-- Web interface logins. 'viewer' can read; 'editor' can also add/change/delete records;
-- 'admin' can additionally manage accounts; 'faculty' is a faculty member who can see only their own
-- advisees and committee students (faculty_id says which faculty member; their username is their CNET);
-- 'fsr' is a financial services rep who can see the advisees of the faculty they are the FSR for (fsr_id);
-- 'approver' and 'chair' take part in the form approval process (see forms) and can see nothing else.
-- must_change forces a new password at next login.
CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY,
    username       TEXT UNIQUE NOT NULL,
    password_hash  TEXT NOT NULL,
    role           TEXT NOT NULL DEFAULT 'viewer' CHECK (role IN ('viewer', 'editor', 'admin', 'faculty', 'fsr', 'approver', 'chair')),
    must_change    INTEGER NOT NULL DEFAULT 0,
    faculty_id     INTEGER REFERENCES faculty(id) ON DELETE CASCADE,
    fsr_id         INTEGER REFERENCES fsrs(id) ON DELETE CASCADE,
    full_name      TEXT,                 -- shown on approvals (needed for approver and chair)
    email          TEXT,                 -- where approval notices go (needed for approver and chair; faculty and FSR
                                         -- logins use the email on their faculty / FSR record instead)
    CHECK ((role = 'faculty') = (faculty_id IS NOT NULL) AND (role = 'fsr') = (fsr_id IS NOT NULL))
);
