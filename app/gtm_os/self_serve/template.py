"""The manual-upload path for the self-serve landing page (2026-11-05, Majji's explicit ask:
"along with website we also have to give one more option, that is download template...
fill the fields, that is ICP values, keys... and upload").

Same field names as icp_from_website.to_search_icp()'s output, and the same names
decompose_icp() (app/gtm_os/sourcing/atoms.py) reads off a real partner's stored ICP -- a visitor
who fills this in is filling in exactly what a real partner's ICP record already looks like, not
a separate schema this module would have to keep translating.
"""
from __future__ import annotations

import csv
import io

FIELDS = ["industries", "employee_min", "employee_max", "revenue_min_usd", "revenue_max_usd", "geographies", "notes"]

_EXAMPLE_ROW = {
    "industries": "credit unions, banks",
    "employee_min": "50",
    "employee_max": "500",
    "revenue_min_usd": "",
    "revenue_max_usd": "",
    "geographies": "United States",
    "notes": "We sell website optimization tools to financial institutions.",
}

_HELP_ROW = {
    "industries": "comma-separated, e.g. credit unions, banks",
    "employee_min": "number, or leave blank",
    "employee_max": "number, or leave blank",
    "revenue_min_usd": "number in USD, or leave blank",
    "revenue_max_usd": "number in USD, or leave blank",
    "geographies": "comma-separated, e.g. United States, Canada",
    "notes": "a sentence describing who you sell to, for context",
}


def template_csv_bytes() -> bytes:
    """The downloadable template: a header row, one row of instructions, one filled example --
    so a visitor filling this in by hand sees the expected shape rather than guessing from
    headers alone. Row 2 (the help row) is a comment-like row the visitor is meant to delete or
    overwrite; parse_template_csv() below only ever reads the LAST row, so leaving it in place by
    mistake does not corrupt what gets parsed."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FIELDS)
    writer.writeheader()
    writer.writerow(_HELP_ROW)
    writer.writerow(_EXAMPLE_ROW)
    return buf.getvalue().encode("utf-8")


class TemplateParseError(Exception):
    pass


def _split_list(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _parse_int(value: str, field: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        raise TemplateParseError(f"{field!r} must be a whole number, got {value!r}") from None


def parse_template_csv(raw: bytes) -> dict:
    """The uploaded file -> the same ICP dict shape to_search_icp() produces. Reads only the LAST
    data row (a visitor's real answer), so an unedited help/example row above it is ignored
    rather than parsed as the actual ICP. Raises TemplateParseError on anything that can't be
    read as this shape -- never silently returns a half-filled ICP."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise TemplateParseError(f"could not read file as UTF-8 text: {e}") from e

    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise TemplateParseError("the file has a header but no filled-in row")
    row = rows[-1]

    missing_columns = [f for f in FIELDS if f not in row]
    if missing_columns:
        raise TemplateParseError(f"missing expected column(s): {', '.join(missing_columns)} -- "
                                 f"use the downloaded template's own header row")

    industries = _split_list(row.get("industries"))
    geographies = _split_list(row.get("geographies"))
    employee_min = _parse_int(row.get("employee_min"), "employee_min")
    employee_max = _parse_int(row.get("employee_max"), "employee_max")
    revenue_min_usd = _parse_int(row.get("revenue_min_usd"), "revenue_min_usd")
    revenue_max_usd = _parse_int(row.get("revenue_max_usd"), "revenue_max_usd")
    notes = (row.get("notes") or "").strip() or None

    if not industries and employee_min is None and employee_max is None and not geographies:
        raise TemplateParseError("the row is empty -- fill in at least an industry, headcount band, or geography")

    return {
        "industries": industries,
        "employee_min": employee_min,
        "employee_max": employee_max,
        "revenue_min_usd": revenue_min_usd,
        "revenue_max_usd": revenue_max_usd,
        "geographies": geographies,
        "notes": notes,
    }
