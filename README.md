# Physics Department Graduate Student DB

SQLite database plus a command-line tool (Python 3, standard library only).

## Usage
```
python3 physdb.py add-faculty Niels Bohr bohr@uni.edu --research-area Atomic
python3 physdb.py add-student U1001 Ada Lovelace ada@uni.edu --matriculation-year 2022 --matriculation-term Autumn --research-area "Quantum optics"
python3 physdb.py assign-advisor U1001 bohr@uni.edu            # --role co-advisor|committee|committee_chair
python3 physdb.py add-milestone U1001 gde_classical_mechanics 2024-05-01 --outcome passed
python3 physdb.py add-funding U1001 TA 2026 Autumn --source Dept --amount 12000 --poetaf-coa 1234567
python3 physdb.py set-status U1001 on_leave
python3 physdb.py list --status active --advisor bohr@uni.edu --year 2022   # students Bohr advises
python3 physdb.py list --committee bohr@uni.edu                              # students where Bohr is only on the committee
python3 physdb.py show U1001
python3 physdb.py missing candidacy --min-years 1.5            # milestone check: active students with clock >= 1.5 and no passed candidacy
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
- **Milestone Check** (Reports tab): pick a milestone and a minimum clock in years (fractions such as 0.25 are fine) to
  list the active students at or past that clock who have no passed or conditional entry for it, with matriculation,
  clock, research area and advisor; every heading sorts.
- **Importing students from a file:** on the Students tab, **Import from file** (editors and admins) takes an Excel
  (.xlsx, first sheet) or CSV file with one student per row; `examples/Incoming_Student_Data_Test.xlsx` is a sample
  and the page links a blank CSV template. Required columns: First Name, Last Name, UCID (exactly 8 digits), Email,
  Citizenship, Matriculation Year, Matriculation Quarter; optional: CNET, Transfer / Transfer Year / Transfer Quarter,
  Research Area, Notes. Headings are matched loosely and other columns are ignored. You get a **preview** first, and
  nothing is saved until you press Import: **new students** that will be added; students **already in the database**
  (same UCID), which are not imported and whose records are not changed; and **problems** (a bad UCID, a missing
  field, an email or CNET used by someone else, a repeat within the file), which are skipped with the reason and row
  number. The import is all-or-nothing and re-checks the database when you confirm. Imported students are Active.
  (Excel support uses the `openpyxl` library, which `run.sh` installs.)
- **Transfer students:** on the Add/Edit student form, tick **Transfer** and the **Transfer year** and **Transfer term**
  boxes appear (both required); unticking clears them. The transfer term is separate from the matriculation term, shows
  on the student's page, and is included in the CSV download.
- **Graduation / withdrawal term:** when a student's Status is set to **Graduated** or **Withdrawn**, the edit form
  asks for the **year and term** they graduated or left (both required); any other status clears them. (This replaced
  "Expected graduation year", which was removed.) From the command line:
  `set-status STUDENT graduated --year 2026 --term spring` (also for `withdrawn`). The Students tab has a
  **Graduated** column (withdrawal terms show on the student's own page). Students already marked graduated or withdrawn have no term until you
  edit them and enter it.
- **Milestones Table** (Reports, first tab): a snapshot with one row per active or on-leave student: name, clock,
  advisor, a **GDE** column listing the exams passed (CM, QM, EM, SM), and a ✓ for each of Candidacy, MS, Committee,
  Pre-Oral and Defense passed (a conditional pass counts). Every column sorts. Graduated and withdrawn students
  are not listed; the **Students** tab has a **Graduated** column with each graduate's term.
- **Funding report** (Reports → *Funding by term*): pick a year and term (or step with **‹ Prev** / **Next ›**) to list every *active* student with
  their funding for that term (type, source, POETAF/COA, amount, notes). Active students with nothing
  recorded still appear, with empty fields; a student with several entries gets one row per entry.
  Students on leave, graduated or withdrawn are not included. Download it as CSV for a spreadsheet.
  Editors get these buttons on each row: **Edit** (the entry), **Clear** (deletes that row's entry; it asks
  first), **Copy**, and **+** (adds an entry; term pre-filled). Saving or cancelling returns to the same report.
  **Copy** opens a window for copying another term's funding into the displayed term for that student: it starts
  on the previous term, **Prev / Next** step through other terms, and each term's entries (type, source,
  POETAF/COA, amount, notes) are shown so you can pick the one you want. The window also shows what is
  currently recorded and warns that copying replaces it. If the chosen term has nothing, nothing changes.
  The **+** button, last on every row, adds an entry for that student and term, so a second entry in the same term (e.g. an RA plus a supplement) is one click away from any of that student's rows.
- **Faculty accounts:** give each faculty member a **CNET** (Faculty → add/edit; it is also a column on the
  list). Then, on the **Accounts** page, **+ Add faculty account** creates a login whose username is their CNET
  (pick the person and set a temporary password; they choose their own at first sign-in). After signing in a
  faculty member lands on **My students**: the students they advise (name, status, clock, graduation
  term, research area, and their funding for a term, with Prev / Next to change term; the heading counts the
  *active* ones), a read-only
  page for each advisee (milestones, advisors and committee, funding for every term), and the list of students
  whose committee they are on. They cannot see anything else: every other page is refused, and they cannot change
  anything. They never see University IDs or the internal student/milestone notes. Changing a faculty member's
  CNET renames their login; it can't be removed while they have a login, and deleting the faculty member deletes
  the login. Only current advising counts (an advising entry with an end date no longer gives access).
  Staff see the same two tables, titled **Advisees** and **Thesis Committees**, on each faculty member's page
  (Faculty → click a name), with the same funding-term stepper; names there link to the full student pages.
- **FSRs** (financial services reps, who help manage funding accounts): the **FSRs** tab lists them (name, email,
  CNET, and the faculty they work with, each a link to that faculty member's page). Editors add an FSR with first
  name, last name, email and CNET. A faculty member has at most one FSR, chosen from a pull-down on the faculty
  edit page; the FSR also shows on the faculty list and page. Deleting an FSR just leaves their faculty without one.
  **FSR accounts** work like faculty accounts: on the **Accounts** page, **+ Add FSR account** makes a login whose
  username is the FSR's CNET. An FSR who signs in lands on **My students**: every student currently advised
  (advisor or co-advisor) by any faculty member they are the FSR for, with the advisor's name, status, clock,
  graduation term, research area and funding for a term (Prev / Next to change term). That is all they can see
  (no University IDs or notes, no student pages), and they cannot change anything. A graduated or withdrawn student stays on
  this table for the term they left and the term after it, and drops off from the start of the term after that
  (graduate in Summer 2026 and you're listed through Autumn 2026, gone from Winter 2027); one with no recorded term
  stays. Faculty pages keep graduated and withdrawn students indefinitely. Staff see the same table on
  an FSR's page (FSRs → click a name).
- **Sorting:** click any column heading on the Students, Faculty, Reports and Accounts lists to sort by
  it; click again to reverse. Filters stay in place, and blank values always sort to the bottom.
- Login is required for every page; forms are CSRF-protected; deletes ask for confirmation.
- `webapp.py run` is Flask's development server. For real use serve it to other machines with a
  production server behind HTTPS, e.g. `gunicorn 'webapp:create_app()'` behind your department's
  reverse proxy. Passwords travel in plain text over plain HTTP, so don't expose it without HTTPS.
- Set `PHYSDB_SECRET` (or protect the generated `.secret_key` file) so sessions can't be forged.

## Data model (`schema.sql`)
`students`, `faculty` (each with an optional FSR), `fsrs`, `advising` (advisor/committee history with dates), `milestones`
(qualifier, candidacy, committee formed, proposal, defense; the student's page shows the clock on each milestone's date), `funding` (TA/RA/fellowship/supplement by term, with a POETAF/COA account number).
Terms are a year plus Autumn, Winter, Spring or Summer, chosen from menus and sorted chronologically
(newest first); `year` is the calendar year in which the term takes place.

## Tests
`.venv/bin/python -m unittest`

## Privacy
This holds student education records (FERPA). Don't store SSNs, and keep the `.db` file
(git-ignored) on access-controlled storage.
