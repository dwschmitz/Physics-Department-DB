"""Reading a spreadsheet of new students (.xlsx or .csv) and checking it against the database.

Nothing here writes to the database: `analyse` sorts every row into new / already in the database / problem, and
`physdb.insert_student` is what the web page calls (in one transaction) for the new ones once a person confirms.
"""
import csv
import io
import re
import zipfile

import physdb

MAX_BYTES = 5 * 1024 * 1024      # uploads larger than this are refused (see webapp: MAX_CONTENT_LENGTH)
MAX_ROWS = 5000                  # a bigger file is more likely a mistake than a class of new students
UCID_DIGITS = 8                  # University IDs are exactly this many digits

# Column headings we understand (compared loosely: case, spacing, punctuation and "(notes in brackets)" are ignored).
HEADINGS = {
    "first_name": ("first name", "firstname", "first", "given name"),
    "last_name": ("last name", "lastname", "last", "surname", "family name"),
    "university_id": ("ucid", "university id", "universityid", "student id"),
    "cnet": ("cnet", "cnet id", "cnetid"),
    "email": ("email", "email address", "e mail"),
    "citizenship": ("citizenship",),
    "matriculation_year": ("matriculation year",),
    "matriculation_term": ("matriculation quarter", "matriculation term"),
    "transfer": ("transfer",),
    "transfer_year": ("transfer year",),
    "transfer_term": ("transfer quarter", "transfer term"),
    "research_area": ("research area",),
    "notes": ("notes",),
}
REQUIRED = ("first_name", "last_name", "university_id", "email", "citizenship", "matriculation_year", "matriculation_term")
NICE = {"first_name": "First Name", "last_name": "Last Name", "university_id": "UCID", "email": "Email",
        "citizenship": "Citizenship", "matriculation_year": "Matriculation Year", "matriculation_term": "Matriculation Quarter"}
TEMPLATE_HEADER = ["First Name", "Last Name", "UCID", "CNET", "Email", "Citizenship", "Matriculation Year",
                   "Matriculation Quarter", "Transfer", "Transfer Year", "Transfer Quarter"]

_YES = {"1", "yes", "y", "true", "x", "t"}
_NO = {"", "0", "no", "n", "false", "f", "-", "none", "n/a", "na"}
_BLANKS = {"", "-", "n/a", "na", "none"}


class ImportFileError(Exception):
    """The file as a whole can't be used (wrong type, unreadable, missing columns, ...). The message is for the user."""


def _norm_heading(text):
    text = re.sub(r"\(.*?\)", " ", str(text or "").lower())
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _clean(value):
    """A cell as trimmed text ('' for empty). Whole-number floats (Excel's 12345678.0) lose the '.0'."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def read_table(data, filename):
    """(header cells, [(row number, cells), ...]) from the first sheet of an .xlsx or from a .csv. Fully blank rows are
    dropped. Raises ImportFileError with a plain-English reason."""
    name = (filename or "").lower()
    if name.endswith(".xlsx") or name.endswith(".xlsm"):
        grid = _read_xlsx(data)
    elif name.endswith(".csv") or name.endswith(".txt"):
        grid = _read_csv(data)
    elif name.endswith(".xls"):
        raise ImportFileError("This is an old-style Excel file (.xls). Open it in Excel and use File > Save As > "
                              "Excel Workbook (.xlsx), or save it as CSV, then upload that.")
    else:
        raise ImportFileError("Please upload an Excel file (.xlsx) or a CSV file (.csv).")
    rows = [(n, [_clean(c) for c in cells]) for n, cells in grid if any(_clean(c) for c in cells)]
    if not rows:
        raise ImportFileError("The file has no data in it.")
    if len(rows) - 1 > MAX_ROWS:
        raise ImportFileError(f"The file has more than {MAX_ROWS} rows, which is more than one import allows.")
    return rows[0][1], rows[1:], rows[0][0]


def _read_xlsx(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:     # an .xlsx is a zip: refuse ones that unpack to something huge
            if len(z.infolist()) > 2000 or sum(i.file_size for i in z.infolist()) > 200 * 1024 * 1024:
                raise ImportFileError("This Excel file is too large to import.")
    except zipfile.BadZipFile:
        raise ImportFileError("This doesn't look like a valid Excel (.xlsx) file.")
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        grid = []
        for n, row in enumerate(ws.iter_rows(values_only=True), 1):
            grid.append((n, list(row)))
            if n > MAX_ROWS * 4 + 1000:                  # a sheet formatted down to a million blank rows
                break
        wb.close()
    except ImportFileError:
        raise
    except Exception:
        raise ImportFileError("This Excel file could not be read. Try saving it again as .xlsx or as CSV.")
    return grid


def _read_csv(data):
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")   # older Excel "CSV (Comma delimited)" files
    try:
        return [(n, row) for n, row in enumerate(csv.reader(io.StringIO(text)), 1)]
    except csv.Error:
        raise ImportFileError("This CSV file could not be read.")


def map_columns(header):
    """({field: column index}, [unrecognised headings]). Raises ImportFileError when a required column is missing."""
    lookup = {alias: field for field, aliases in HEADINGS.items() for alias in aliases}
    found, ignored = {}, []
    for i, h in enumerate(header):
        field = lookup.get(_norm_heading(h))
        if field and field not in found:
            found[field] = i
        elif h:
            ignored.append(h)
    missing = [NICE[f] for f in REQUIRED if f not in found]
    if missing:
        raise ImportFileError("These columns were not found in the first row of the file: " + ", ".join(missing)
                              + ". (Headings are matched loosely, so capitalisation and spacing don't matter.)")
    return found, ignored


def _parse_row(cells, cols):
    """(values for physdb.insert_student, [problems]) from one row's cells."""
    cell = lambda f: cells[cols[f]] if f in cols and cols[f] < len(cells) else ""
    problems, v = [], {}
    for f in ("first_name", "last_name"):
        v[f] = cell(f)
        if not v[f]:
            problems.append(f"{NICE[f]} is empty.")
    ucid = cell("university_id")
    if not ucid:
        problems.append("UCID is empty.")
    elif not re.fullmatch(r"\d{%d}" % UCID_DIGITS, ucid):
        problems.append(f"UCID must be exactly {UCID_DIGITS} digits (found '{ucid}').")
    v["university_id"] = ucid
    email = cell("email")
    if not email:
        problems.append("Email is empty.")
    elif "@" not in email or " " in email:
        problems.append(f"Email '{email}' is not a valid address.")
    v["email"] = email
    cnet = cell("cnet").lower()
    if cnet in _BLANKS:
        cnet = ""
    if cnet and not re.fullmatch(physdb.CNET_PATTERN, cnet):
        problems.append(f"CNET '{cnet}' may only use letters, digits, . _ - (2-40 characters).")
    v["cnet"] = cnet or None
    cit = re.sub(r"[\s_]+", " ", cell("citizenship").lower())
    by_name = {c.replace("_", " "): c for c in physdb.CITIZENSHIP}
    if not cit:
        problems.append("Citizenship is empty.")
    elif cit not in by_name:
        problems.append(f"Citizenship '{cell('citizenship')}' must be Citizen, Permanent Resident or International.")
    v["citizenship"] = by_name.get(cit)

    def year(field, label):
        raw = cell(field)
        if not re.fullmatch(r"\d{4}", raw) or not 1950 <= int(raw) <= 2100:
            problems.append(f"{label} must be a four-digit year (found '{raw}')." if raw else f"{label} is empty.")
            return None
        return int(raw)

    def term(field, label):
        raw = cell(field).title()
        if raw not in physdb.SEASONS:
            problems.append(f"{label} must be Autumn, Winter, Spring or Summer (found '{cell(field)}')."
                            if raw else f"{label} is empty.")
            return None
        return raw

    v["matriculation_year"] = year("matriculation_year", "Matriculation Year")
    v["matriculation_term"] = term("matriculation_term", "Matriculation Quarter")
    flag = cell("transfer").lower()
    if flag in _YES:
        v["transfer_year"] = year("transfer_year", "Transfer Year")
        v["transfer_term"] = term("transfer_term", "Transfer Quarter")
    elif flag in _NO:
        v["transfer_year"] = v["transfer_term"] = None       # "-" placeholders etc. in the transfer columns are ignored
    else:
        problems.append(f"Transfer must be 1 or 0 (found '{cell('transfer')}').")
        v["transfer_year"] = v["transfer_term"] = None
    v["research_area"] = cell("research_area") or None
    v["notes"] = cell("notes") or None
    return v, problems


def analyse(conn, data, filename):
    """Read the file and sort its rows. Returns a dict:
       new         [{row, v}]                       will be added
       duplicates  [{row, v, existing}]             University ID already in the database: not imported, not changed
       problems    [{row, v, messages}]             can't be imported (the rest still can)
       ignored     [headings that were not recognised], header_row, total
    Raises ImportFileError for a problem with the file as a whole."""
    header, rows, header_row = read_table(data, filename)
    cols, ignored = map_columns(header)
    out = dict(new=[], duplicates=[], problems=[], ignored=ignored, header_row=header_row, total=len(rows))
    seen = {"university_id": {}, "email": {}, "cnet": {}}          # value -> row number it first appeared on
    for n, cells in rows:
        v, problems = _parse_row(cells, cols)
        who = f"{v['last_name']}, {v['first_name']}".strip(", ") or "(no name)"
        # a University ID that is already in the database is simply skipped, whatever else the row says
        existing = conn.execute("SELECT id, first_name, last_name FROM students WHERE university_id = ?",
                                (v["university_id"],)).fetchone() if v["university_id"] else None
        if existing is not None:
            out["duplicates"].append(dict(row=n, v=v, who=who, existing=dict(existing)))
            seen["university_id"].setdefault(v["university_id"], n)
            continue
        keys = {"university_id": v["university_id"], "email": v["email"].lower(), "cnet": v["cnet"] or ""}
        if not problems:
            for field, label in (("university_id", "UCID"), ("email", "Email"), ("cnet", "CNET")):
                if keys[field] and keys[field] in seen[field]:
                    problems.append(f"{label} '{keys[field]}' appears more than once in the file "
                                    f"(first on row {seen[field][keys[field]]}).")
            clash = conn.execute("SELECT first_name, last_name FROM students WHERE lower(email) = ?",
                                 (keys["email"],)).fetchone() if keys["email"] else None
            if clash:
                problems.append(f"Email {v['email']} already belongs to a different student in the database "
                                f"({clash['last_name']}, {clash['first_name']}).")
            clash = conn.execute("SELECT first_name, last_name FROM students WHERE cnet = ?",
                                 (keys["cnet"],)).fetchone() if keys["cnet"] else None
            if clash:
                problems.append(f"CNET {v['cnet']} already belongs to a different student in the database "
                                f"({clash['last_name']}, {clash['first_name']}).")
        if problems:
            out["problems"].append(dict(row=n, v=v, who=who, messages=problems))
            continue
        for field in seen:
            if keys[field]:
                seen[field].setdefault(keys[field], n)
        out["new"].append(dict(row=n, v=v, who=who))
    return out


def csv_template():
    """A header-only CSV that staff can fill in (UTF-8 with a BOM so Excel opens it correctly)."""
    buf = io.StringIO()
    csv.writer(buf).writerow(TEMPLATE_HEADER)
    return "﻿" + buf.getvalue()
