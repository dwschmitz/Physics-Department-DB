"""Filling in the blank PDF forms in forms/ and preparing the chair's signature image.

The finished PDF is flattened (the typed values and the signature are drawn onto the page and the form fields are
removed), so it looks the same in every PDF viewer and cannot be edited by changing a field.
"""
import datetime
import hashlib
import io
import logging
from pathlib import Path

from PIL import Image, ImageChops, UnidentifiedImageError
import pypdf

logging.getLogger("pypdf").setLevel(logging.ERROR)     # the blank form has a few harmless quirks that pypdf would otherwise print

FORMS_DIR = Path(__file__).with_name("forms")
CANDIDACY_TEMPLATE = "Candidacy_Form_2020-2021.pdf"
ID_WITHHELD = "(withheld)"
DEPARTMENT = "Physics"                       # written on the form as the candidate's department

# Where the chair's signature goes on the candidacy form (PDF points from the bottom-left): x0, y0, x1, y1.
SIGNATURE_BOX = (156, 350, 460, 402)

MAX_SIGNATURE_BYTES = 2 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 40_000_000         # refuse decompression bombs


class SignatureError(Exception):
    """The uploaded image can't be used. The message is for the user."""


def prepare_signature(data):
    """Check an uploaded signature picture and return it as a tidy PNG: transparency flattened onto white, empty margins
    trimmed, scaled down if huge. Accepts PNG or JPEG."""
    if not data:
        raise SignatureError("Choose an image file first.")
    if len(data) > MAX_SIGNATURE_BYTES:
        raise SignatureError(f"That image is too large (the limit is {MAX_SIGNATURE_BYTES // 1024 // 1024} MB).")
    try:
        img = Image.open(io.BytesIO(data))
        if img.format not in ("PNG", "JPEG"):
            raise SignatureError("Please use a PNG or JPEG image.")
        img.load()
    except SignatureError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError):
        raise SignatureError("That file could not be read as an image. Please use a PNG or JPEG.")
    img = img.convert("RGBA")
    page = Image.new("RGB", img.size, "white")
    page.paste(img, mask=img.getchannel("A"))
    ink = ImageChops.invert(page.convert("L")).point(lambda v: 255 if v > 40 else 0)     # anything darker than near-white
    box = ink.getbbox()
    if box is None:
        raise SignatureError("That image looks blank. Use a picture of the signature on a light background.")
    pad = max(4, round(0.04 * max(box[2] - box[0], box[3] - box[1])))
    page = page.crop((max(box[0] - pad, 0), max(box[1] - pad, 0), min(box[2] + pad, page.width), min(box[3] + pad, page.height)))
    if page.width > 1200:
        page = page.resize((1200, max(1, round(page.height * 1200 / page.width))), Image.LANCZOS)
    out = io.BytesIO()
    page.save(out, "PNG", optimize=True)
    return out.getvalue()


def _flatten(writer):
    writer.remove_annotations(subtypes="/Widget")
    if "/AcroForm" in writer._root_object:
        del writer._root_object["/AcroForm"]


def candidacy_pdf(student, signature_png=None, signed_on=None, flatten=True, hide_id=False):
    """The admission-to-candidacy form for `student` (a dict-like row with first_name, last_name, university_id).
    With a signature image and a date, Part II is completed as well. Returns the PDF bytes. (flatten=False leaves the
    form fields in place, which is only useful for checking what was filled in. hide_id=True writes "(withheld)" in
    place of the University ID: that is the copy advisors see, since faculty accounts never see University IDs.)"""
    reader = pypdf.PdfReader(str(FORMS_DIR / CANDIDACY_TEMPLATE))
    writer = pypdf.PdfWriter(clone_from=reader)
    page = writer.pages[0]
    values = {"Check Box2": "/Yes",                                           # Doctor of Philosophy
              "Text3": f"{student['first_name']} {student['last_name']}",
              "Text4": DEPARTMENT, "Text5": ID_WITHHELD if hide_id else str(student["university_id"])}
    if signed_on:
        values["Date7_af_date"] = signed_on.strftime("%m/%d/%Y")
    writer.update_page_form_field_values(page, values, auto_regenerate=False, flatten=flatten)
    if flatten:
        _flatten(writer)
    if signature_png:
        sig = Image.open(io.BytesIO(signature_png)).convert("RGB")
        buf = io.BytesIO()
        sig.save(buf, "PDF", resolution=72.0)                                 # a one-page PDF the size of the picture
        overlay = pypdf.PdfReader(buf).pages[0]
        x0, y0, x1, y1 = SIGNATURE_BOX
        scale = min((x1 - x0) / float(overlay.mediabox.width), (y1 - y0) / float(overlay.mediabox.height))
        page.merge_transformed_page(overlay, pypdf.Transformation().scale(scale).translate(x0, y0))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def sha256(data):
    return hashlib.sha256(data).hexdigest()
