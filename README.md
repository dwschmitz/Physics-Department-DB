# Physics Department Graduate Student DB

SQLite database plus a command-line tool (Python 3, standard library only).

## Usage
```
python3 physdb.py add-faculty Niels Bohr bohr@uni.edu --research-area Atomic
python3 physdb.py add-student U1001 Ada Lovelace ada@uni.edu --start-year 2022 --research-area "Quantum optics"
python3 physdb.py assign-advisor U1001 bohr@uni.edu            # --role co-advisor|committee|committee_chair
python3 physdb.py add-milestone U1001 qualifying_exam 2024-05-01 --outcome passed
python3 physdb.py add-funding U1001 TA "Fall 2026" --source Dept --amount 12000
python3 physdb.py set-status U1001 on_leave
python3 physdb.py list --status active --advisor bohr@uni.edu --year 2022
python3 physdb.py show U1001
python3 physdb.py missing qualifying_exam --min-years 2        # students behind schedule
python3 physdb.py export > students.csv
```
Students and faculty can be referenced by row id, university id, or email.
The database file is `physics_grads.db` (override with `PHYSDB_PATH`) and is created on first use.

## Data model (`schema.sql`)
`students`, `faculty`, `advising` (advisor/committee history with dates), `milestones`
(qualifier, candidacy, proposal, defense), `funding` (TA/RA/fellowship by term).

## Tests
`python3 -m unittest`

## Privacy
This holds student education records (FERPA). Don't store SSNs, and keep the `.db` file
(git-ignored) on access-controlled storage.
