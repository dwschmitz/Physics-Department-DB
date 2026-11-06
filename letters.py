"""Term assignment letters: fills the Word templates in letter_templates/ for one student and one funding entry.

A template is an ordinary .docx whose fill-in spots are Word mail-merge fields, shown as «NAME». We replace each field
with its value as plain text (keeping the formatting of the field), so the letter opens in Word looking like the
template with nothing left to merge. Which field a spot is comes from the name shown between the « » marks, not from
Word's hidden field code.

Settings that are the same for every letter have the defaults below; an environment variable replaces one:
  PHYSDB_LETTER_DEPARTMENT   the employing unit and the signature block (DEPT_OR_PROGRAM)
  PHYSDB_LETTER_REPLY_EMAIL  where the signed letter is returned
  PHYSDB_LETTER_INSTRUCTOR, PHYSDB_LETTER_INSTRUCTOR_EMAIL  the TA letters' instructor of record
  PHYSDB_LETTER_ENROLLMENT_LIMIT  the TA letters' expected class size (a placeholder until it is recorded per course)
"""
import datetime
import io
import os
import re
import zipfile
from html import escape
from pathlib import Path

import physdb

TEMPLATES_DIR = Path(__file__).with_name("letter_templates")
TEMPLATES = {"RA": "RA_Primary_Template.docx", "TA": "TA_Primary_Template.docx"}          # funding kind -> template


class LetterError(Exception):
    """The letter can't be made yet. The message tells the user what is missing."""


def long_date(d):
    return f"{d:%B} {d.day}, {d.year}"


def settings(environ=None):
    env = os.environ if environ is None else environ
    get = lambda name, default="": (env.get(name) or default).strip()
    return dict(department=get("PHYSDB_LETTER_DEPARTMENT", "Physics"),
                reply_email=get("PHYSDB_LETTER_REPLY_EMAIL", "putrikusumo@uchicago.edu"),
                instructor=get("PHYSDB_LETTER_INSTRUCTOR", "Stuart Gazes"),
                instructor_email=get("PHYSDB_LETTER_INSTRUCTOR_EMAIL", "gazes@uchicago.edu"),
                enrollment_limit=get("PHYSDB_LETTER_ENROLLMENT_LIMIT", "100"))


def supervisors(conn, student_id, term_end, today):
    """The student's current advisors and co-advisors: those whose advising has begun by the end of the term and has
    not ended before `today` (ISO dates). One whose "to" date is in the past is not listed."""
    return conn.execute(
        """SELECT DISTINCT f.first_name, f.last_name, f.email FROM advising a JOIN faculty f ON f.id = a.faculty_id
           WHERE a.student_id = ? AND a.role IN ('advisor', 'co-advisor') AND a.start_date <= ?
             AND (a.end_date IS NULL OR a.end_date >= ?)
           ORDER BY a.role, f.last_name, f.first_name""", (student_id, term_end, today)).fetchall()


def values_for(conn, student, funding, today=None, environ=None):
    """The value of every merge field for this student and funding entry, or LetterError saying what is missing."""
    cfg = settings(environ)
    if funding["term_year"] is None:
        raise LetterError("This funding entry has no term, so there are no dates for the letter. Edit it and choose the term.")
    dates = physdb.term_dates(conn, funding["term_season"], funding["term_year"])
    if dates is None:
        raise LetterError(f"{funding['term_season']} {funding['term_year']} is not in the term calendar yet. "
                          "An administrator can add its dates on the Terms page.")
    today = today or datetime.date.today()
    start = datetime.date.fromisoformat(dates[0])
    end = datetime.date.fromisoformat(dates[1])
    name = f"{student['first_name']} {student['last_name']}"
    values = {
        "FULL_ADDRESEE_NAME": name,                        # the RA template's spelling
        "FULL_ADDRESSEE_NAME": name,                       # the TA template's
        "DATE": long_date(today),
        "STUDENT_FIRST": student["first_name"],
        "START_DATE": long_date(start),
        "END_DATE": long_date(end),
        "DEPT_OR_PROGRAM": cfg["department"],
        "PREFERRED_EMAIL": cfg["reply_email"],
    }
    if funding["kind"] == "TA":
        course = (funding["course_code"] or "").strip()
        if not course:
            raise LetterError("This TA entry has no course code. Edit the funding entry and fill it in.")
        values.update({"INSTRUCTOR_OF_RECORD": cfg["instructor"], "INSTRUCTOR_EMAIL": cfg["instructor_email"],
                       "COURSE_CODE": course, "ENROLLMENT_LIMIT": cfg["enrollment_limit"]})
    else:
        advisors = supervisors(conn, student["id"], dates[1], today.isoformat())
        if not advisors:
            raise LetterError(f"{name} has no current advisor recorded. Add one on the student's page first "
                              "(an advisor whose \"to\" date has passed does not count).")
        join = lambda parts: " and ".join(parts)
        values.update({"SUPERVISOR_NAME": join([f"{a['first_name']} {a['last_name']}" for a in advisors]),
                       "SUPERVISOR_EMAIL": join([a["email"] for a in advisors if a["email"]])})
    return values


# One Word field: the run holding the "begin" mark, through the run holding the "end" mark.
_RUN = r'<w:r\b[^>]*>(?:(?!</w:r>).)*?'
_FIELD = re.compile(_RUN + r'<w:fldChar w:fldCharType="begin"/></w:r>.*?' + _RUN + r'<w:fldChar w:fldCharType="end"/></w:r>', re.S)
_MARKED = re.compile(r"«([A-Z_]+)»")


def fill(template_bytes, values):
    """The template with every merge field replaced by its value. A field the values don't cover is an error."""
    src = zipfile.ZipFile(io.BytesIO(template_bytes))
    xml = src.read("word/document.xml").decode("utf-8")

    def replace(m):
        field = m.group(0)
        shown = re.findall(r"<w:t[^>]*>([^<]*)</w:t>", field.split('fldCharType="separate"', 1)[-1])
        name = _MARKED.search("".join(shown))
        if not name:
            return field                                   # a field that isn't one of ours: leave it alone
        if name.group(1) not in values:
            raise LetterError(f"The template has a field «{name.group(1)}» that the app does not know how to fill.")
        result = re.search(r'<w:r\b[^>]*>(?:(?!</w:r>).)*?<w:t[^>]*>', field.split('fldCharType="separate"', 1)[-1], re.S)
        props = re.search(r"<w:rPr>.*?</w:rPr>", result.group(0), re.S) if result else None
        props = props.group(0).replace("<w:noProof/>", "") if props else ""
        return f'<w:r>{props}<w:t xml:space="preserve">{escape(values[name.group(1)], quote=False)}</w:t></w:r>'

    xml = _FIELD.sub(replace, xml)
    if _MARKED.search(xml):
        raise LetterError("The template still has an unfilled field: " + _MARKED.search(xml).group(0))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            dst.writestr(item, xml.encode("utf-8") if item.filename == "word/document.xml" else src.read(item.filename))
    return out.getvalue()


def docx_to_pdfs(files):
    """Convert Word files to PDF with LibreOffice (if the computer has it). `files` maps a file name to its bytes;
    returns the same names with .pdf, mapped to the PDF bytes. Raises LetterError, with a message for the user, if
    LibreOffice is missing or fails; a file it could not convert is simply absent from the result."""
    import shutil
    import subprocess
    import tempfile
    mac_app = "/Applications/LibreOffice.app/Contents/MacOS/soffice"          # where the Mac download puts it (not on the PATH)
    office = shutil.which("soffice") or shutil.which("libreoffice") or (mac_app if Path(mac_app).exists() else None)
    if office is None:
        raise LetterError("PDFs could not be made because LibreOffice is not installed on the computer running this app.")
    with tempfile.TemporaryDirectory(prefix="physdb-letters-") as tmp:
        tmp = Path(tmp)
        (tmp / "in").mkdir(); (tmp / "out").mkdir()
        for name, data in files.items():
            (tmp / "in" / Path(name).name).write_bytes(data)
        try:
            subprocess.run([office, "--headless", "--norestore", "--nolockcheck",
                            f"-env:UserInstallation={(tmp / 'profile').as_uri()}",       # its own settings: no clash with another run
                            "--convert-to", "pdf", "--outdir", str(tmp / "out"), *sorted(str(p) for p in (tmp / "in").iterdir())],
                           capture_output=True, timeout=60 + 5 * len(files), check=True, cwd=tmp)
        except (subprocess.SubprocessError, OSError):
            raise LetterError("PDFs could not be made: LibreOffice did not finish converting the letters.") from None
        return {p.name: p.read_bytes() for p in (tmp / "out").glob("*.pdf")}


def make_letter(conn, student, funding, today=None, environ=None):
    """(file bytes, suggested file name) for the letter for this funding entry."""
    template = TEMPLATES.get(funding["kind"])
    if template is None:
        raise LetterError(f"There is no letter template for {funding['kind']} funding yet.")
    values = values_for(conn, student, funding, today, environ)
    data = fill((TEMPLATES_DIR / template).read_bytes(), values)
    last = re.sub(r"[^A-Za-z0-9_-]+", "_", student["last_name"]) or "student"
    return data, f"{funding['kind']}_letter_{last}_{funding['term_season']}_{funding['term_year']}.docx"
