"""Literature sources and PRISMA counts (minimal; L-51, SPEC_LNM_ADDENDUM RW-12).

A study of published case reports keeps the records it screened in ``sources/records.tsv`` so that the counts of a PRISMA
flow diagram, and the paper each case comes from, are never retyped.

- **Records** (:class:`Records`, ``sources/records.tsv``, :data:`COLUMNS`): ``record_id`` (letters and digits: the cases are
  ``sub-<record_id><nn>``), ``doi``, ``pmid``, ``title``, ``authors``, ``year``, ``journal``, ``stage`` (:data:`STAGES`),
  ``exclusion_reason``, ``n_cases``, ``origin`` (database, citation, other), ``notes`` and ``added``. The stage changes are
  logged in ``sources/stage_log.tsv`` (what "undo" restores, and at which stage a record was excluded). Attachments (figures,
  PDFs) live in ``sources/<record_id>/`` and are never exported (``share.py`` takes ``records.tsv`` only).
- **Parsers** (:func:`parse_nbib`, :func:`parse_ris`, :func:`parse_bibtex`, :func:`parse_csv`, :func:`parse_identifiers`;
  :func:`parse_file` picks one): they return draft records and never touch the network.
- **Duplicates** (:meth:`Records.add`): an exact DOI or PMID merges into the record already there (empty fields are filled); a
  title and year that match closely (normalized, ``difflib`` ratio of at least :data:`TITLE_RATIO`) are only *offered* for
  merging (:meth:`Records.merge_offers`, :meth:`Records.merge`).
- **Screening** (:meth:`Records.set_stage`, :meth:`Records.advance`, :meth:`Records.exclude`, :meth:`Records.undo`):
  identified, then screened (passed the title and abstract), fulltext (full text under assessment), included; excluded
  at any stage with a reason.
- **Metadata lookup** (:func:`lookup`, :func:`fetch_metadata`): Crossref ``works/<doi>`` or the NCBI ``esummary`` for a PMID
  through ``requests``, only when called (a click); only the identifier is sent; offline it returns an error text and changes
  nothing.
- **Cases** (:func:`add_cases`): rows ``sub-<record_id><nn>`` with the record's ``record_id``, ``doi``, ``pmid`` and
  ``citation`` (``source_type`` = case_report, ``group`` = case).
- **PRISMA** (:func:`prisma_counts`, :func:`prisma_flow_svg`, :func:`export_prisma`): ``derivatives/prisma/prisma_counts.json``
  and ``prisma_flow.svg`` (a pure-Python SVG template). The search log is ``study.json`` ``search``: a list of
  ``{database, query, date, records, duplicates}``.

No dual-screening reconciliation and no risk-of-bias assessment (out of scope). Pure standard library (``requests`` is
imported only by the lookup); shared files hold no absolute paths.
"""

import csv
import datetime
import difflib
import io
import json
import os
import re
import unicodedata
from xml.sax.saxutils import escape

from . import study as study_module

NA = "n/a"
SOURCES_DIR = "sources"
RECORDS_TSV = (SOURCES_DIR, "records.tsv")
STAGE_LOG_TSV = (SOURCES_DIR, "stage_log.tsv")
PRISMA_DIR = ("derivatives", "prisma")
PRISMA_COUNTS_JSON = "prisma_counts.json"
PRISMA_FLOW_SVG = "prisma_flow.svg"

COLUMNS = ("record_id", "doi", "pmid", "title", "authors", "year", "journal", "stage", "exclusion_reason", "n_cases", "origin", "notes", "added")
LOG_COLUMNS = ("record_id", "from", "to", "reason", "when")
#: "to" of a stage log line that records a merge of two records (not a stage)
MERGED = "merged"
IDENTIFIED, SCREENED, FULLTEXT, INCLUDED, EXCLUDED = "identified", "screened", "fulltext", "included", "excluded"
STAGES = (IDENTIFIED, SCREENED, FULLTEXT, INCLUDED, EXCLUDED)
#: The forward path of P (include / advance)
ADVANCE = (IDENTIFIED, SCREENED, FULLTEXT, INCLUDED)
ORIGINS = ("database", "citation", "other")
#: Titles whose normalized text matches at least this closely (and have the same year) are offered for merging
TITLE_RATIO = 0.95
FORMAT_VERSION = 1
LOOKUP_TIMEOUT_S = 10
CROSSREF_URL = "https://api.crossref.org/works/"
ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"

_DOI = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
_PMID = re.compile(r"^\d{1,9}$")


class SourcesError(Exception):
    """A records file that cannot be read, or a lookup that failed (the message is for the user)."""


def now_iso():
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _clean(value):
    """One line of text without tabs (a TSV cell)."""
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    return "" if text == NA else text


def _blank(value):
    return _clean(value) == ""


def normalize_doi(value):
    """The DOI of a text ("https://doi.org/10.1000/ABC.", "doi:10.1000/abc" and the like) in lower case, or ""."""
    text = _clean(value)
    match = _DOI.search(text)
    if not match:
        return ""
    return match.group(0).rstrip(".,;)]}>\"'").lower()


def normalize_pmid(value):
    text = _clean(value)
    match = re.fullmatch(r"(?:pmid:?\s*)?(\d{1,9})", text, re.IGNORECASE)
    return match.group(1).lstrip("0") if match else ""


def normalize_title(value):
    """Lower case, accents and punctuation removed, one space between words."""
    text = unicodedata.normalize("NFKD", _clean(value)).encode("ascii", "ignore").decode("ascii").lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _year(value):
    match = re.search(r"(?<!\d)(1[6-9]\d\d|20\d\d)(?!\d)", _clean(value))
    return match.group(1) if match else ""


def draft(**fields):
    """A draft record (strings only; identifiers normalized) from the fields the parsers found."""
    out = {key: _clean(fields.get(key)) for key in ("title", "authors", "journal", "notes")}
    out["doi"] = normalize_doi(fields.get("doi"))
    out["pmid"] = normalize_pmid(fields.get("pmid"))
    out["year"] = _year(fields.get("year"))
    return out


#
# Parsers
#


def _tagged(text, pattern):
    """[(tag, value)] of a tagged format (nbib: ``TI  - ``; RIS: ``TI  - ``); continuation lines are joined."""
    items = []
    for line in text.splitlines():
        match = re.match(pattern, line)
        if match:
            items.append([match.group(1).strip(), match.group(2).strip()])
        elif items and line.strip():
            items[-1][1] += " " + line.strip()
    return items


def parse_nbib(text):
    """PubMed / MEDLINE records (``PMID- ``, ``TI  - ``, ``AU  - ``, ``DP  - ``, ``TA  - ``, ``LID - 10.x [doi]``)."""
    records, current = [], None
    for tag, value in _tagged(text, r"^([A-Z][A-Z0-9 ]{1,3}?)\s*- (.*)$"):
        if tag == "PMID":
            current = {"authors": [], "pmid": value}
            records.append(current)
        elif current is None:
            continue
        elif tag == "TI":
            current["title"] = value
        elif tag in ("AU", "FAU") and (tag == "AU" or not current["authors"]):
            current["authors"].append(value)
        elif tag == "DP":
            current["year"] = value
        elif tag in ("TA", "JT") and (tag == "TA" or "journal" not in current):
            current["journal"] = value
        elif tag in ("LID", "AID") and "[doi]" in value and not current.get("doi"):
            current["doi"] = value.replace("[doi]", "")
    return [draft(**{**record, "authors": "; ".join(record["authors"])}) for record in records]


def parse_ris(text):
    """RIS records (``TY  - `` ... ``ER  - ``)."""
    records, current = [], None
    for tag, value in _tagged(text.lstrip("﻿"), r"^([A-Z][A-Z0-9])\s{2}- ?(.*)$"):
        if tag == "TY":
            current = {"authors": []}
            records.append(current)
        elif current is None or tag == "ER":
            continue
        elif tag in ("TI", "T1") and "title" not in current:
            current["title"] = value
        elif tag in ("AU", "A1", "A2"):
            current["authors"].append(value)
        elif tag in ("PY", "Y1", "DA") and "year" not in current:
            current["year"] = value
        elif tag in ("JO", "JF", "JA", "T2") and "journal" not in current:
            current["journal"] = value
        elif tag == "DO" and not current.get("doi"):
            current["doi"] = value
        elif tag == "UR" and _DOI.search(value) and not current.get("doi"):
            current["doi"] = value
        elif tag in ("AN", "ID") and re.fullmatch(r"\d{1,9}", value) and not current.get("pmid"):
            current["pmid"] = value
        elif tag in ("N1", "M3") and "PMID" in value.upper() and not current.get("pmid"):
            current["pmid"] = value
    return [draft(**{**record, "authors": "; ".join(record["authors"])}) for record in records if record.get("title") or record.get("doi") or record.get("pmid")]


def _bibtex_value(text, index):
    """(value, next index) of a BibTeX field value starting at ``index`` ({...}, "..." or a bare word)."""
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text):
        return "", index
    if text[index] == "{":
        depth, start = 0, index + 1
        for position in range(index, len(text)):
            if text[position] == "{":
                depth += 1
            elif text[position] == "}":
                depth -= 1
                if depth == 0:
                    return text[start:position], position + 1
        return text[start:], len(text)
    if text[index] == '"':
        position = index + 1
        while position < len(text) and not (text[position] == '"' and text[position - 1] != "\\"):
            position += 1
        return text[index + 1 : position], position + 1
    match = re.compile(r"[^,}\s]*").match(text, index)
    return match.group(0), match.end()


def parse_bibtex(text):
    """BibTeX entries (``@article{key, title = {...}, author = {A and B}, ...}``)."""
    records = []
    for match in re.finditer(r"@(\w+)\s*[{(]", text):
        if match.group(1).lower() in ("comment", "string", "preamble"):
            continue
        index = text.find(",", match.end())
        if index < 0:
            continue
        fields = {}
        index += 1
        while index < len(text):
            field = re.compile(r"\s*([A-Za-z][\w-]*)\s*=").match(text, index)
            if not field:
                break
            value, index = _bibtex_value(text, field.end())
            fields[field.group(1).lower()] = re.sub(r"[{}]", "", value)
            comma = re.compile(r"\s*,").match(text, index)
            index = comma.end() if comma else index
        authors = "; ".join(part.strip() for part in re.split(r"\s+and\s+", fields.get("author", "")) if part.strip())
        record = draft(title=fields.get("title"), authors=authors, year=fields.get("year") or fields.get("date"),
                       journal=fields.get("journal") or fields.get("journaltitle") or fields.get("booktitle"),
                       doi=fields.get("doi") or fields.get("url"), pmid=fields.get("pmid") or fields.get("pubmed"))
        if record["title"] or record["doi"] or record["pmid"]:
            records.append(record)
    return records


_CSV_NAMES = {
    "doi": ("doi", "digital object identifier", "di"),
    "pmid": ("pmid", "pubmed id", "pubmedid", "pm id", "pubmed"),
    "title": ("title", "article title", "ti", "document title"),
    "authors": ("authors", "author", "author full names", "au", "author(s)"),
    "year": ("year", "publication year", "py", "pubyear", "date", "publication date"),
    "journal": ("journal", "source title", "journal/book", "so", "source", "publication"),
}


def parse_csv(text):
    """A CSV (or TSV) with DOI and / or PMID columns; title, authors, year and journal columns are used when present."""
    text = text.lstrip("﻿")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        return []
    header = [cell.strip().lower() for cell in rows[0]]
    columns = {key: next((header.index(name) for name in names if name in header), None) for key, names in _CSV_NAMES.items()}
    if columns["doi"] is None and columns["pmid"] is None and columns["title"] is None:
        raise SourcesError("The table has no DOI, PMID or title column")
    records = []
    for row in rows[1:]:
        fields = {key: row[index] if index is not None and index < len(row) else "" for key, index in columns.items()}
        record = draft(**fields)
        if record["title"] or record["doi"] or record["pmid"]:
            records.append(record)
    return records


def parse_identifiers(text):
    """Pasted DOIs and PMIDs (``10.1000/xyz``, ``https://doi.org/...``, ``PMID: 123``, or a bare number on its own line)."""
    records, seen = [], set()
    bare, other_lines = [], False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        found = _DOI.findall(line)
        is_bare = False
        if found:
            candidates = [("doi", normalize_doi(item)) for item in found]
        else:
            candidates = [("pmid", number) for number in re.findall(r"(?i)pmid:?\s*(\d{1,9})", line)]
            if not candidates:
                tokens = [token for token in re.split(r"[\s,;]+", line) if token]
                if all(_PMID.match(token) for token in tokens):
                    candidates = [("pmid", token.lstrip("0")) for token in tokens]
                    is_bare = True
        if not candidates:
            other_lines = True
        for kind, value in candidates:
            if value and (kind, value) not in seen:
                seen.add((kind, value))
                records.append(draft(**{kind: value}))
                if is_bare:
                    bare.append(records[-1])
    if other_lines and bare:
        # Text with other words around bare numbers (a table, a log): the numbers are not identifiers
        records = [record for record in records if not any(record is item for item in bare)]
    return records


def detect_format(text, name=""):
    """"nbib", "ris", "bibtex", "csv" or "identifiers" for a file's text (the extension first, then the content)."""
    extension = os.path.splitext(name)[1].lower()
    head = text.lstrip("﻿").lstrip()[:2000]
    if extension in (".bib", ".bibtex") or head.startswith("@"):
        return "bibtex"
    if extension == ".ris" or re.match(r"TY\s{2}- ", head):
        return "ris"
    if re.match(r"PMID- ", head):
        return "nbib"
    if extension in (".csv", ".tsv") or ("," in head.split("\n", 1)[0] and re.search(r"(?i)\b(doi|pmid|title)\b", head.split("\n", 1)[0])):
        return "csv"
    return "identifiers"


def parse_text(text, name=""):
    """Draft records of a file's or pasted text, whichever format it is in. Return ``(format, records)``."""
    kind = detect_format(text, name)
    parser = {"nbib": parse_nbib, "ris": parse_ris, "bibtex": parse_bibtex, "csv": parse_csv, "identifiers": parse_identifiers}[kind]
    return kind, parser(text)


def parse_file(path):
    """Draft records of a file. Return ``(format, records)``. :raises SourcesError: for an unreadable file."""
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
    except OSError as e:
        raise SourcesError(f"Cannot read {os.path.basename(os.fspath(path))}: {e.strerror or e}") from e
    return parse_text(text, os.fspath(path))


#
# Records
#


def _surname(author):
    """The surname of "Smith, John", "Smith J" (PubMed) or "John Smith"."""
    author = _clean(author)
    if "," in author:
        return author.split(",")[0].strip()
    words = author.split()
    if len(words) > 1 and re.fullmatch(r"[A-Z]{1,3}", words[-1]):
        return " ".join(words[:-1])
    return words[-1] if words else ""


def _record_id_base(record):
    """Letters and digits of the first author's surname and the year ("smith2020"); "rec" without an author."""
    authors = _clean(record.get("authors"))
    first = re.split(r"[;]|\s+and\s+", authors)[0] if authors else ""
    surname = _surname(first)
    surname = re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", surname).encode("ascii", "ignore").decode().lower())
    return (surname or "rec") + _year(record.get("year"))


def _citation(row):
    """"Smith et al. 2020, Brain" from a record row."""
    authors = [part.strip() for part in re.split(r";", _clean(row.get("authors"))) if part.strip()]
    if authors:
        who = _surname(authors[0]) + (" et al." if len(authors) > 1 else "")
    else:
        who = _clean(row.get("title"))[:60]
    parts = [who + (f" {row['year']}" if not _blank(row.get("year")) else "")]
    if not _blank(row.get("journal")):
        parts.append(_clean(row["journal"]))
    return ", ".join(part for part in parts if part)


def citation(row):
    return _citation(row)


class Records:
    """The records of a study (``sources/records.tsv``): load, change, :meth:`save`.

    Rows are dicts of text keyed by :data:`COLUMNS` ("n/a" for empty cells on disk, "" in memory). Columns that other
    programs added to the file are kept.
    """

    def __init__(self, study_path, rows=None, extra_columns=(), log=None):
        self.study_path = os.path.abspath(os.fspath(study_path))
        self.rows = list(rows or [])
        self.extra_columns = list(extra_columns)
        self.log = list(log or [])

    # Storage

    @property
    def path(self):
        return os.path.join(self.study_path, *RECORDS_TSV)

    @property
    def log_path(self):
        return os.path.join(self.study_path, *STAGE_LOG_TSV)

    @classmethod
    def open(cls, study_or_path):
        """The records of a study (an empty table when there is no file yet)."""
        base = getattr(study_or_path, "path", study_or_path)
        records = cls(base)
        if os.path.isfile(records.path):
            try:
                columns, rows = study_module.read_tsv(records.path)
            except (OSError, ValueError) as e:
                raise SourcesError(f"sources/records.tsv cannot be read: {e}") from e
            records.extra_columns = [column for column in columns if column not in COLUMNS]
            for row in rows:
                full = {column: ("" if row.get(column, NA) == NA else row.get(column, "")) for column in (*COLUMNS, *records.extra_columns)}
                if full["record_id"]:
                    full["stage"] = full["stage"] if full["stage"] in STAGES else IDENTIFIED
                    records.rows.append(full)
        if os.path.isfile(records.log_path):
            try:
                _columns, rows = study_module.read_tsv(records.log_path)
                records.log = [{column: ("" if row.get(column, NA) == NA else row[column]) for column in LOG_COLUMNS} for row in rows]
            except (OSError, ValueError):
                records.log = []
        return records

    def save(self):
        """Write ``sources/records.tsv`` and ``stage_log.tsv`` (atomic)."""
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        study_module.write_tsv(self.path, [*COLUMNS, *self.extra_columns], self.rows)
        if self.log:
            study_module.write_tsv(self.log_path, LOG_COLUMNS, self.log)
        elif os.path.isfile(self.log_path):
            os.remove(self.log_path)

    def attachments_folder(self, record_id):
        """``sources/<record_id>/`` (not created)."""
        return os.path.join(self.study_path, SOURCES_DIR, record_id)

    # Access

    def __len__(self):
        return len(self.rows)

    def get(self, record_id):
        for row in self.rows:
            if row["record_id"] == record_id:
                return row
        raise KeyError(f"Unknown record {record_id!r}")

    def ids(self):
        return [row["record_id"] for row in self.rows]

    def find(self, doi="", pmid=""):
        """The record with this DOI or PMID, else None."""
        doi, pmid = normalize_doi(doi), normalize_pmid(pmid)
        for row in self.rows:
            if (doi and normalize_doi(row["doi"]) == doi) or (pmid and normalize_pmid(row["pmid"]) == pmid):
                return row
        return None

    def new_id(self, record):
        """A free record id: "smith2020", then "smith2020b", "smith2020c"... (letters and digits only)."""
        base = _record_id_base(record)
        taken = {existing.lower() for existing in self.ids()}
        if base.lower() not in taken:
            return base
        for suffix in "bcdefghijklmnopqrstuvwxyz":
            if (base + suffix).lower() not in taken:
                return base + suffix
        number = 2
        while f"{base}{number}x".lower() in taken:
            number += 1
        return f"{base}{number}x"

    # Adding and merging

    @staticmethod
    def _index(by_doi, by_pmid, row):
        doi, pmid = normalize_doi(row["doi"]), normalize_pmid(row["pmid"])
        if doi:
            by_doi.setdefault(doi, row)
        if pmid:
            by_pmid.setdefault(pmid, row)

    def _fill(self, row, record):
        """Fill the empty fields of ``row`` from a draft. Return the fields that were filled."""
        filled = []
        for key in ("doi", "pmid", "title", "authors", "year", "journal"):
            if _blank(row.get(key)) and record.get(key):
                row[key] = record[key]
                filled.append(key)
        return filled

    def add(self, drafts, origin="database", n_cases=None, when=None):
        """Add draft records. Exact DOI / PMID duplicates (of the table or of the drafts) are merged, not added.

        :return: ``{"added": [record_id], "merged": [(record_id, duplicate title or identifier)], "offers": [(record_id,
          other record_id, ratio)]}``; ``offers`` are near-identical titles with the same year (see :meth:`merge`).
        """
        if origin not in ORIGINS:
            raise ValueError(f"Unknown origin {origin!r}; expected one of {', '.join(ORIGINS)}")
        added, merged, offers = [], [], []
        stamp = when or now_iso()
        by_doi, by_pmid = {}, {}
        for row in self.rows:
            self._index(by_doi, by_pmid, row)
        for item in drafts:
            item = draft(**item)
            doi, pmid = normalize_doi(item["doi"]), normalize_pmid(item["pmid"])
            existing = (by_doi.get(doi) if doi else None) or (by_pmid.get(pmid) if pmid else None)
            if existing is not None:
                self._fill(existing, item)
                self._index(by_doi, by_pmid, existing)
                merged.append((existing["record_id"], item["title"] or item["doi"] or item["pmid"]))
                continue
            row = dict.fromkeys((*COLUMNS, *self.extra_columns), "")
            row.update(item)
            row["record_id"] = self.new_id(item)
            row["stage"] = IDENTIFIED
            row["origin"] = origin
            row["n_cases"] = "" if n_cases is None else str(int(n_cases))
            row["added"] = stamp
            self.rows.append(row)
            self._index(by_doi, by_pmid, row)
            added.append(row["record_id"])
        if added:
            offers = self.merge_offers(added)
        return {"added": added, "merged": merged, "offers": offers}

    def merge_offers(self, record_id=None):
        """Pairs of records whose normalized titles match (ratio of at least :data:`TITLE_RATIO`) and have the same year.

        :param record_id: only the pairs that involve this record, or one of these records (default: every pair).
        :return: ``[(record_id, other_record_id, ratio)]``, the older record first.
        """
        if record_id is None:
            wanted = None
        elif isinstance(record_id, str):
            wanted = {record_id}
        else:
            wanted = set(record_id)
        keys = [(row["record_id"], normalize_title(row["title"]), row["year"]) for row in self.rows]
        by_year = {}
        for index, (_rid, title, year) in enumerate(keys):
            if title:
                by_year.setdefault(year, []).append(index)
        seen = set()
        out = []
        matcher = difflib.SequenceMatcher(None)
        for indexes in by_year.values():
            for position, index in enumerate(indexes):
                rid, title, _year = keys[index]
                matcher.set_seq2(title)  # the statistics of seq2 are cached for the inner loop
                size = len(title)
                for other_index in indexes:
                    if other_index == index or (wanted is None and other_index < index):
                        continue
                    other, other_title, _y = keys[other_index]
                    if wanted is not None and rid not in wanted and other not in wanted:
                        continue
                    # The ratio is at most 2 * min / (len + other len): skip pairs that cannot reach TITLE_RATIO
                    other_size = len(other_title)
                    if 2.0 * min(size, other_size) / (size + other_size) < TITLE_RATIO:
                        continue
                    matcher.set_seq1(other_title)
                    if matcher.real_quick_ratio() < TITLE_RATIO or matcher.quick_ratio() < TITLE_RATIO:
                        continue
                    ratio = matcher.ratio()
                    if ratio >= TITLE_RATIO:
                        pair = (other, rid, ratio) if other_index < index else (rid, other, ratio)
                        if pair[:2] not in seen:
                            seen.add(pair[:2])
                            out.append(pair)
        return out

    def merge(self, keep, drop):
        """Merge record ``drop`` into ``keep`` (empty fields filled, ``n_cases`` and notes combined). Return the kept row.

        The attachments folder of ``drop`` is left where it is. Cases that cite ``drop`` are not changed here; the
        caller relinks them with :func:`relink_cases`.
        """
        row, other = self.get(keep), self.get(drop)
        self._fill(row, other)
        if _blank(row["n_cases"]) and not _blank(other["n_cases"]):
            row["n_cases"] = other["n_cases"]
        if other["notes"] and other["notes"] not in row["notes"]:
            row["notes"] = (row["notes"] + " " + other["notes"]).strip()
        rank = {stage: index for index, stage in enumerate(ADVANCE)}
        if row["stage"] != EXCLUDED and other["stage"] in rank and rank.get(row["stage"], 0) < rank[other["stage"]]:
            row["stage"] = other["stage"]
        self.rows = [item for item in self.rows if item is not other]
        self.log = [entry for entry in self.log if entry["record_id"] != drop]
        # Kept in the log: a manual merge counts as a removed duplicate in the PRISMA counts
        self.log.append({"record_id": keep, "from": drop, "to": MERGED, "reason": "", "when": now_iso()})
        return row

    def merged_count(self):
        """Duplicates merged by hand (:meth:`merge`)."""
        return sum(1 for entry in self.log if entry["to"] == MERGED)

    # Stages

    def _stamp(self, row, new, reason, when=None):
        self.log.append({"record_id": row["record_id"], "from": row["stage"], "to": new, "reason": _clean(reason), "when": when or now_iso()})
        row["stage"] = new
        row["exclusion_reason"] = _clean(reason) if new == EXCLUDED else ""

    def set_stage(self, record_id, stage, reason=""):
        """Set the stage of a record (the change is logged). ``reason`` is kept for "excluded" and cleared otherwise.

        :raises ValueError: for an unknown stage, or an exclusion without a reason.
        """
        if stage not in STAGES:
            raise ValueError(f"Unknown stage {stage!r}; expected one of {', '.join(STAGES)}")
        row = self.get(record_id)
        if stage == EXCLUDED and not _clean(reason):
            raise ValueError("An excluded record needs a reason")
        if row["stage"] == stage and (stage != EXCLUDED or row["exclusion_reason"] == _clean(reason)):
            return row
        self._stamp(row, stage, reason)
        return row

    def advance(self, record_id):
        """P: the next stage (identified, screened, fulltext, included). Return the new stage (unchanged at "included")."""
        row = self.get(record_id)
        if row["stage"] == EXCLUDED:
            return self.set_stage(record_id, self._stage_before_exclusion(record_id) or IDENTIFIED)["stage"]
        index = ADVANCE.index(row["stage"])
        if index + 1 < len(ADVANCE):
            self.set_stage(record_id, ADVANCE[index + 1])
        return row["stage"]

    def exclude(self, record_id, reason):
        return self.set_stage(record_id, EXCLUDED, reason)

    def _stage_before_exclusion(self, record_id):
        for entry in reversed(self.log):
            if entry["record_id"] == record_id and entry["to"] == EXCLUDED:
                return entry["from"]
        return None

    def undo(self, record_id):
        """U: restore the stage before the last change of the record (removes its log line). Return the stage, or None."""
        for index in range(len(self.log) - 1, -1, -1):
            entry = self.log[index]
            if entry["record_id"] == record_id and entry["to"] != MERGED:
                row = self.get(record_id)
                row["stage"] = entry["from"] if entry["from"] in STAGES else IDENTIFIED
                row["exclusion_reason"] = ""
                if row["stage"] == EXCLUDED:  # the earlier exclusion's reason
                    for earlier in reversed(self.log[:index]):
                        if earlier["record_id"] == record_id and earlier["to"] == EXCLUDED:
                            row["exclusion_reason"] = earlier["reason"]
                            break
                del self.log[index]
                return row["stage"]
        return None

    def excluded_at(self, row):
        """The stage a record was excluded from (identified when the log does not say)."""
        return self._stage_before_exclusion(row["record_id"]) or IDENTIFIED

    def stage_counts(self):
        counts = dict.fromkeys(STAGES, 0)
        for row in self.rows:
            counts[row["stage"]] += 1
        return counts


#
# Lookup (only when called; only the identifier is sent)
#


def _http_json(url, params=None, timeout=LOOKUP_TIMEOUT_S):
    """GET ``url`` and parse the JSON (``requests`` is imported here, so nothing else needs it)."""
    import requests

    response = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": "SlicerStudio-sources"})
    response.raise_for_status()
    return response.json()


def _crossref_record(message):
    title = (message.get("title") or [""])[0]
    authors = "; ".join(f"{a.get('family', '')}, {a.get('given', '')}".strip(", ") for a in message.get("author") or [] if a.get("family") or a.get("name"))
    year = ""
    for key in ("issued", "published-print", "published-online", "created"):
        parts = (message.get(key) or {}).get("date-parts") or [[]]
        if parts and parts[0]:
            year = str(parts[0][0])
            break
    journal = (message.get("container-title") or [""])[0]
    return draft(title=title, authors=authors, year=year, journal=journal, doi=message.get("DOI"))


def _esummary_record(entry):
    doi = ""
    for item in entry.get("articleids") or []:
        if item.get("idtype") == "doi":
            doi = item.get("value", "")
    authors = "; ".join(a.get("name", "") for a in entry.get("authors") or [] if a.get("name"))
    return draft(title=entry.get("title"), authors=authors, year=entry.get("pubdate"), journal=entry.get("source"), doi=doi, pmid=entry.get("uid"))


def lookup(doi="", pmid="", get=None):
    """Metadata of a publication from Crossref (DOI) or the NCBI esummary (PMID). Only the identifier is sent.

    Call it only for a user's click. ``get(url, params)`` returns the parsed JSON (default: ``requests``; tests pass a stub).

    :return: a draft record (see :func:`draft`).
    :raises SourcesError: offline, not found or an unreadable answer (the message says what happened).
    """
    get = get or _http_json
    doi, pmid = normalize_doi(doi), normalize_pmid(pmid)
    try:
        if doi:
            data = get(CROSSREF_URL + doi, None)
            message = data.get("message") if isinstance(data, dict) else None
            if not isinstance(message, dict):
                raise SourcesError("Crossref sent no record for this DOI")
            return {**_crossref_record(message), "doi": doi}
        if pmid:
            data = get(ESUMMARY_URL, {"db": "pubmed", "id": pmid, "retmode": "json"})
            entry = ((data or {}).get("result") or {}).get(pmid)
            if not isinstance(entry, dict) or entry.get("error"):
                raise SourcesError("PubMed has no record for this PMID")
            return {**_esummary_record(entry), "pmid": pmid}
    except SourcesError:
        raise
    except ImportError as e:
        raise SourcesError("The lookup needs the 'requests' package") from e
    except Exception as e:  # offline, timeout, HTTP error, bad JSON
        raise SourcesError(f"The lookup failed ({type(e).__name__}); check the connection and try again") from e
    raise SourcesError("The record has no DOI or PMID to look up")


def fetch_metadata(records, record_id, get=None):
    """Fill the empty fields of a record from :func:`lookup` (never overwrites). Return the fields filled.

    :raises SourcesError: when the lookup failed (the record is unchanged).
    """
    row = records.get(record_id)
    found = lookup(row["doi"], row["pmid"], get=get)
    return records._fill(row, found)


#
# Cases
#


def linked_subjects(study, record_id):
    """Participant ids whose ``record_id`` is this record."""
    if "record_id" not in study.columns:
        return []
    return [row["participant_id"] for row in study.subjects if row.get("record_id") == record_id]


def add_cases(study, records, record_id, n):
    """Add ``n`` cases of a record to the study: rows ``sub-<record_id><nn>`` with the record's fields.

    The rows get ``record_id``, ``doi``, ``pmid`` and ``citation``, ``source_type`` = case_report and ``group`` = case. The
    record's ``n_cases`` is raised to the number of linked cases. The caller saves the study and the records.

    :return: the new participant ids.
    :raises ValueError: for ``n`` below 1 or a record id that is not letters and digits.
    """
    n = int(n)
    if n < 1:
        raise ValueError("Add at least one case")
    if not re.fullmatch(r"[A-Za-z0-9]+", record_id):
        raise ValueError(f"Invalid record id {record_id!r}")
    row = records.get(record_id)
    existing = set(study.subject_ids)
    values = {"record_id": record_id, "source_type": "case_report", "group": "case"}
    for column in ("doi", "pmid"):
        if row[column]:
            values[column] = row[column]
    text = _citation(row)
    if text:
        values["citation"] = text
    created, number = [], 0
    while len(created) < n:
        number += 1
        sid = f"sub-{record_id}{number:02d}"
        if sid in existing:
            continue
        study.add_subject(sid, values)
        created.append(sid)
    # Keep the PRISMA case count in step with the study rows
    digits = re.search(r"\d+", row["n_cases"])
    row["n_cases"] = str(max(int(digits.group(0)) if digits else 0, len(linked_subjects(study, record_id))))
    return created


def relink_cases(study, keep, drop):
    """Point the cases of record ``drop`` at record ``keep`` (after :meth:`Records.merge`). Return the participant ids."""
    moved = linked_subjects(study, drop)
    for sid in moved:
        study.set(sid, record_id=keep)
    return moved


#
# Search log and PRISMA
#


def search_log(study):
    """The search log of ``study.json``: ``[{database, query, date, records, duplicates}]`` (a copy)."""
    entries = study.metadata.get("search")
    return [dict(entry) for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def add_search(study, database, query="", date=None, records=None, duplicates=None):
    """Append a search to ``study.json`` ``search`` (the caller saves the study). Return the entry."""
    entry = {"database": _clean(database) or "Other", "query": _clean(query), "date": date or datetime.date.today().isoformat()}
    if records is not None:
        entry["records"] = int(records)
    if duplicates is not None:
        entry["duplicates"] = int(duplicates)
    entries = study.metadata.get("search")
    study.metadata["search"] = [*(entries if isinstance(entries, list) else []), entry]
    return entry


def import_text(study, records, text, name="", origin="database", database=None, query="", n_cases=None):
    """Parse a file's or pasted text, add its records and log the search. Return the :meth:`Records.add` report plus
    ``format`` and ``parsed`` (the caller saves the records and the study).

    :raises SourcesError: when the text holds no record.
    """
    kind, drafts = parse_text(text, name)
    if not drafts:
        raise SourcesError("No record found in this file")
    report = records.add(drafts, origin=origin, n_cases=n_cases)
    report["format"], report["parsed"] = kind, len(drafts)
    add_search(study, database or ("Pasted identifiers" if kind == "identifiers" else os.path.basename(name) or kind.upper()), query,
               records=len(drafts), duplicates=len(report["merged"]))
    return report


def prisma_counts(study, records=None):
    """The counts of the PRISMA 2020 flow diagram. Write them with :func:`export_prisma`.

    ``identified`` counts every record imported, duplicates included (``duplicates_removed``: the exact matches merged at
    import and the merges made by hand); ``screened`` the unique records. A record excluded while identified was excluded at
    title and abstract screening; one excluded later, at the full-text stage, with its reason counted in
    ``fulltext_excluded_reasons``. ``cases`` is the sum of ``n_cases`` of the included records.
    """
    records = records or Records.open(study)
    searches = search_log(study)
    duplicates = sum(int(entry.get("duplicates") or 0) for entry in searches) + records.merged_count()
    unique = len(records.rows)
    by_origin = dict.fromkeys(ORIGINS, 0)
    for row in records.rows:
        by_origin[row["origin"] if row["origin"] in ORIGINS else "other"] += 1
    rank = {stage: index for index, stage in enumerate(ADVANCE)}
    excluded_screening, reasons = 0, {}
    sought = included = cases = 0
    for row in records.rows:
        stage = row["stage"]
        if stage == EXCLUDED:
            if records.excluded_at(row) == IDENTIFIED:
                excluded_screening += 1
            else:
                sought += 1
                reason = row["exclusion_reason"] or "No reason given"
                reasons[reason] = reasons.get(reason, 0) + 1
        elif rank.get(stage, 0) >= rank[SCREENED]:
            sought += 1
            if stage == INCLUDED:
                included += 1
                with_digits = re.search(r"\d+", row["n_cases"])
                cases += int(with_digits.group(0)) if with_digits else 0
    return {
        "format": "slicer-lnm-prisma",
        "version": FORMAT_VERSION,
        "created": now_iso(),
        "identified": unique + duplicates,
        "identified_by_origin": by_origin,
        "duplicates_removed": duplicates,
        "screened": unique,
        "unscreened": records.stage_counts()[IDENTIFIED],
        "excluded_screening": excluded_screening,
        "fulltext_sought": sought,
        "fulltext_excluded": sum(reasons.values()),
        "fulltext_excluded_reasons": dict(sorted(reasons.items(), key=lambda item: (-item[1], item[0]))),
        "included": included,
        "cases": cases,
        "search": searches,
    }


_BOX_W, _BOX_H, _GAP = 250, 64, 44


def _box(x, y, w, h, lines, bold_first=True, fill="#f4f6f8"):
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="4" fill="{fill}" stroke="#4a5560" stroke-width="1.2"/>']
    size, line_h = 12, 16
    top = y + (h - line_h * len(lines)) / 2 + 12
    for index, text in enumerate(lines):
        weight = ' font-weight="600"' if bold_first and index == 0 else ""
        out.append(f'<text x="{x + w / 2}" y="{top + index * line_h:.0f}" text-anchor="middle" font-size="{size}"{weight}>{escape(text)}</text>')
    return out


def _arrow(x1, y1, x2, y2):
    return [f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#4a5560" stroke-width="1.2" marker-end="url(#arrow)"/>']


def prisma_flow_svg(counts):
    """A PRISMA 2020 flow diagram (identification, screening, eligibility, included) as SVG text, from :func:`prisma_counts`.

    A fixed template in pure Python: well-formed XML without scripts, external references or local paths.
    """
    n = {key: counts.get(key, 0) for key in ("identified", "duplicates_removed", "screened", "excluded_screening", "fulltext_sought",
                                              "fulltext_excluded", "included", "cases")}
    origin = counts.get("identified_by_origin") or {}
    reasons = list((counts.get("fulltext_excluded_reasons") or {}).items())
    shown = reasons[:4]
    left, right = 70, 70 + _BOX_W + _GAP
    width = right + _BOX_W + 20
    rows_y = [20, 20 + (_BOX_H + _GAP), 20 + 2 * (_BOX_H + _GAP), 20 + 3 * (_BOX_H + _GAP)]
    side_h = _BOX_H + 16 * (len(shown) + (1 if len(reasons) > len(shown) else 0))
    height = rows_y[3] + _BOX_H + 30
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" font-family="Helvetica, Arial, sans-serif" fill="#1f2933">',
        "<title>PRISMA 2020 flow diagram</title>",
        ('<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#4a5560"/></marker></defs>'),
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
    ]
    labels = ("Identification", "Screening", "Eligibility", "Included")
    for label, y in zip(labels, rows_y, strict=True):
        parts.append(f'<text transform="translate(26 {y + _BOX_H / 2:.0f}) rotate(-90)" text-anchor="middle" font-size="13" font-weight="600" fill="#4a5560">{label}</text>')
    identified = [f"Records identified (n = {n['identified']})"]
    if origin.get("citation") or origin.get("other"):
        identified.append(f"databases n = {origin.get('database', 0)}, other n = {origin.get('citation', 0) + origin.get('other', 0)}")
    parts += _box(left, rows_y[0], _BOX_W, _BOX_H, identified)
    parts += _box(right, rows_y[0], _BOX_W, _BOX_H, ["Duplicates removed", f"(n = {n['duplicates_removed']})"])
    parts += _arrow(left + _BOX_W, rows_y[0] + _BOX_H / 2, right, rows_y[0] + _BOX_H / 2)
    parts += _box(left, rows_y[1], _BOX_W, _BOX_H, [f"Records screened (n = {n['screened']})"])
    parts += _arrow(left + _BOX_W / 2, rows_y[0] + _BOX_H, left + _BOX_W / 2, rows_y[1])
    parts += _box(right, rows_y[1], _BOX_W, _BOX_H, ["Records excluded", f"(n = {n['excluded_screening']})"])
    parts += _arrow(left + _BOX_W, rows_y[1] + _BOX_H / 2, right, rows_y[1] + _BOX_H / 2)
    parts += _box(left, rows_y[2], _BOX_W, _BOX_H, [f"Full texts assessed (n = {n['fulltext_sought']})"])
    parts += _arrow(left + _BOX_W / 2, rows_y[1] + _BOX_H, left + _BOX_W / 2, rows_y[2])
    side = [f"Full texts excluded (n = {n['fulltext_excluded']})"] + [f"{reason}: {count}" for reason, count in shown]
    if len(reasons) > len(shown):
        side.append(f"{len(reasons) - len(shown)} other reasons")
    parts += _box(right, rows_y[2], _BOX_W, max(side_h, _BOX_H), side)
    parts += _arrow(left + _BOX_W, rows_y[2] + _BOX_H / 2, right, rows_y[2] + _BOX_H / 2)
    parts += _box(left, rows_y[3], _BOX_W, _BOX_H, [f"Studies included (n = {n['included']})", f"{n['cases']} cases"], fill="#e8f3ec")
    parts += _arrow(left + _BOX_W / 2, rows_y[2] + _BOX_H, left + _BOX_W / 2, rows_y[3])
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def prisma_paths(study):
    folder = os.path.join(study.path, *PRISMA_DIR)
    return os.path.join(folder, PRISMA_COUNTS_JSON), os.path.join(folder, PRISMA_FLOW_SVG)


def export_prisma(study, records=None):
    """Write ``derivatives/prisma/prisma_counts.json`` and ``prisma_flow.svg`` (atomic). Return ``(counts, [paths])``."""
    counts = prisma_counts(study, records)
    json_path, svg_path = prisma_paths(study)
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    for path, text in ((json_path, json.dumps(counts, indent=2, ensure_ascii=False) + "\n"), (svg_path, prisma_flow_svg(counts))):
        study_module._write_text_atomic(path, text)
    return counts, [json_path, svg_path]
