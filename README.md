# Physics Department Graduate Student DB

SQLite database plus a command-line tool (Python 3, standard library only).

## Usage
```
python3 physdb.py add-faculty Niels Bohr bohr@uni.edu --research-area Atomic
python3 physdb.py add-student U1001 Ada Lovelace ada@uni.edu --matriculation-year 2022 --matriculation-term Autumn --research-area "Quantum optics"
python3 physdb.py assign-advisor U1001 bohr@uni.edu            # --role co-advisor|committee|committee_chair
python3 physdb.py add-milestone U1001 qualifying_exam 2024-05-01 --outcome passed
python3 physdb.py add-funding U1001 TA 2026 Autumn --source Dept --amount 12000 --poetaf-coa 1234567
python3 physdb.py set-status U1001 on_leave
python3 physdb.py list --status active --advisor bohr@uni.edu --year 2022   # students Bohr advises
python3 physdb.py list --committee bohr@uni.edu                              # students where Bohr is only on the committee
python3 physdb.py show U1001
python3 physdb.py missing qualifying_exam --min-years 2        # students behind schedule
python3 physdb.py export > students.csv
```
Students and faculty can be referenced by row id, university id, or email.
The database file is `physics_grads.db` (override with `PHYSDB_PATH`) and is created on first use.

## Web interface
Staff use the database through a browser; no command-line knowledge needed.
Mac / Linux: `./run.sh`  |  Windows: double-click `run.bat`  (or run it from a terminal)

The launcher creates the virtual environment and installs dependencies on first use, asks you to
create the first administrator if no accounts exist yet, then starts the server at
http://127.0.0.1:8123 and opens it in your browser. Press Ctrl+C in its window to stop it. It works from any folder, and it
only reinstalls when `requirements.txt` changes.

Without the launcher, the equivalent manual steps are:
```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python webapp.py create-user alice --role admin   # first administrator; prompts for a password
.venv/bin/python webapp.py run
```
After that, administrators manage everyone else from the **Accounts** page in the browser.
`create-user` is still available as a recovery tool: re-running it for an existing name resets that
person's password and role (for example, to regain admin access if no admin can log in).

- **Viewers** can search, filter, view and download CSV; **editors** can also add, edit and delete
  students, faculty, advisors, milestones and funding; **administrators** can do all of that and
  manage accounts. Fixed-choice fields are dropdowns.
- **Accounts page** (administrators only): add people, change roles, reset passwords, delete accounts.
  Passwords set by an admin are temporary: the person must choose their own at next login. Resetting
  a password or deleting an account signs that person out immediately, and role changes apply at
  once. You can't delete your own account, so at least one administrator always remains. Anyone can
  change their own password by clicking their username in the top bar.
- **If the page won't load:** the app can only run once at a time. If it is already running (for
  example in another terminal window), the launcher says so and opens it instead of starting a second
  copy. If something else is using port 8123 it picks the next free port and prints the address.
  To stop a stray copy: close the window it runs in, or on Mac/Linux use `pkill -f "webapp.py run"`,
  on Windows end "python" in Task Manager.
- **Funding report** (Reports → *Funding by term*): pick a year and term (or step with **‹ Prev** / **Next ›**) to list every *active* student with
  their funding for that term (type, source, POETAF/COA, amount, notes). Active students with nothing
  recorded still appear, with empty fields; a student with several entries gets one row per entry.
  Students on leave, graduated or withdrawn are not included. Download it as CSV for a spreadsheet.
  Editors get an **Edit** button on each funding entry and a **+** button (term pre-filled) for adding an entry; saving or cancelling returns to the same report. **Copy Prev** copies all of the
  student's funding from the term just before (type, source, POETAF/COA, amount, notes), replacing what the
  student already has for the displayed term (it asks first); if the earlier term has nothing, nothing changes.
  **Clear** deletes that row's entry (it asks first).
  The **+** button, always last in the row, adds an entry for that student and term; it appears on a student's last row, so a second entry in the same term (e.g. an RA plus a supplement) is one click away.
- **Sorting:** click any column heading on the Students, Faculty, Reports and Accounts lists to sort by
  it; click again to reverse. Filters stay in place, and blank values always sort to the bottom.
- Login is required for every page; forms are CSRF-protected; deletes ask for confirmation.
- `webapp.py run` is Flask's development server. For real use serve it to other machines with a
  production server behind HTTPS, e.g. `gunicorn 'webapp:create_app()'` behind your department's
  reverse proxy. Passwords travel in plain text over plain HTTP, so don't expose it without HTTPS.
- Set `PHYSDB_SECRET` (or protect the generated `.secret_key` file) so sessions can't be forged.

## Data model (`schema.sql`)
`students`, `faculty`, `advising` (advisor/committee history with dates), `milestones`
(qualifier, candidacy, proposal, defense), `funding` (TA/RA/fellowship/supplement by term, with a POETAF/COA account number).
Terms are a year plus Autumn, Winter, Spring or Summer, chosen from menus and sorted chronologically
(newest first); `year` is the calendar year in which the term takes place.

## Tests
`.venv/bin/python -m unittest`

## Privacy
This holds student education records (FERPA). Don't store SSNs, and keep the `.db` file
(git-ignored) on access-controlled storage.
