PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS students (
    id                  INTEGER PRIMARY KEY,
    university_id       TEXT UNIQUE NOT NULL,
    first_name          TEXT NOT NULL,
    last_name           TEXT NOT NULL,
    email               TEXT UNIQUE NOT NULL,
    program             TEXT NOT NULL DEFAULT 'PhD' CHECK (program IN ('PhD', 'MS')),
    status              TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'on_leave', 'graduated', 'withdrawn')),
    start_year          INTEGER NOT NULL,
    expected_grad_year  INTEGER,
    research_area       TEXT,
    notes               TEXT
);

CREATE TABLE IF NOT EXISTS faculty (
    id             INTEGER PRIMARY KEY,
    first_name     TEXT NOT NULL,
    last_name      TEXT NOT NULL,
    email          TEXT UNIQUE NOT NULL,
    research_area  TEXT
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
                CHECK (kind IN ('qualifying_exam', 'candidacy', 'thesis_proposal',
                                'thesis_defense', 'other')),
    date        TEXT NOT NULL,
    outcome     TEXT NOT NULL DEFAULT 'pending'
                CHECK (outcome IN ('pending', 'passed', 'conditional', 'failed')),
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS funding (
    id          INTEGER PRIMARY KEY,
    student_id  INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('TA', 'RA', 'fellowship', 'other')),
    source      TEXT,
    poetaf_coa  TEXT,                   -- POETAF/COA account number
    term_year   INTEGER CHECK (term_year BETWEEN 1950 AND 2100),
    term_season TEXT CHECK (term_season IN ('Winter', 'Spring', 'Summer', 'Autumn')),
    legacy_term TEXT,                   -- free-text term from before the fixed menu; re-enter it
    amount      REAL,
    CHECK ((term_year IS NULL) = (term_season IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_advising_student   ON advising(student_id);
CREATE INDEX IF NOT EXISTS idx_milestones_student ON milestones(student_id);
CREATE INDEX IF NOT EXISTS idx_funding_student    ON funding(student_id);

-- Web interface logins. 'viewer' can read; 'editor' can also add/change/delete records;
-- 'admin' can additionally manage accounts. must_change forces a new password at next login.
CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY,
    username       TEXT UNIQUE NOT NULL,
    password_hash  TEXT NOT NULL,
    role           TEXT NOT NULL DEFAULT 'viewer' CHECK (role IN ('viewer', 'editor', 'admin')),
    must_change    INTEGER NOT NULL DEFAULT 0
);
