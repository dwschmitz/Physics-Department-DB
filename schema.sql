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
    graduation_year     INTEGER,  -- set when status is 'graduated': the term they graduated (year + season)
    graduation_term     TEXT,     -- Autumn/Winter/Spring/Summer (checked by the app)
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
                -- 'qualifying_exam' and 'thesis_proposal' are retired: no longer offered for new entries, but still
                -- allowed here so entries saved earlier keep their meaning (see physdb.RETIRED_MILESTONE_KINDS).
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

CREATE INDEX IF NOT EXISTS idx_advising_student   ON advising(student_id);
CREATE INDEX IF NOT EXISTS idx_milestones_student ON milestones(student_id);
CREATE INDEX IF NOT EXISTS idx_funding_student    ON funding(student_id);

-- Web interface logins. 'viewer' can read; 'editor' can also add/change/delete records;
-- 'admin' can additionally manage accounts; 'faculty' is a faculty member who can see only their own
-- advisees and committee students (faculty_id says which faculty member; their username is their CNET);
-- 'fsr' is a financial services rep who can see the advisees of the faculty they are the FSR for (fsr_id).
-- must_change forces a new password at next login.
CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY,
    username       TEXT UNIQUE NOT NULL,
    password_hash  TEXT NOT NULL,
    role           TEXT NOT NULL DEFAULT 'viewer' CHECK (role IN ('viewer', 'editor', 'admin', 'faculty', 'fsr')),
    must_change    INTEGER NOT NULL DEFAULT 0,
    faculty_id     INTEGER REFERENCES faculty(id) ON DELETE CASCADE,
    fsr_id         INTEGER REFERENCES fsrs(id) ON DELETE CASCADE,
    CHECK ((role = 'faculty') = (faculty_id IS NOT NULL) AND (role = 'fsr') = (fsr_id IS NOT NULL))
);
