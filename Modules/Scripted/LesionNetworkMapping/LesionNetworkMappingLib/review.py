"""Review of lesions (L-43; SPEC_LNM_ADDENDUM 3.10, 7.2-7.3): states, transitions, audit, assisted provenance, blinding.

Papers say that lesions "were checked by an expert" and rarely show it. This module records that check for every lesion
of a study, keeps automatic and unreviewed lesions out of analyses, and supports tracing blind to the groups and scores.
Pure stdlib (no Slicer, no Qt): Studio (``StudioLib.review``, ``StudioLib.libcard_review``) and the command-line tools use
the same rules.

**States** (``participants.tsv`` column ``review_status``; ``none`` when missing)::

    none ──Done──> pending ──> approved
                     │  ├────> changes ──Done──> pending
                     │  └────> rejected (include = 0, with a reason)
    approved / rejected ──(the lesion is traced again)──> pending

``approved → pending`` and ``rejected → pending`` reopen a review when the lesion changes after it (an approval is for
the voxels that were reviewed). A reopened rejection stays excluded (``include`` = 0) while it waits: only a reviewer's
approval restores the ``include`` value that the rejection replaced, so a tracer's Done never undoes a rejection.

**Transitions** (:func:`apply_transition`) set ``review_status``, ``reviewer`` and ``review_comment`` and append one record
to ``<study>/.studio/audit.jsonl``::

    {"event": "review", "sid", "from", "to", "who", "when", "comment", ["reason", "include_before", ...]}

Requesting changes needs a comment; rejecting needs a reason of :data:`REJECT_REASONS` (and a comment for "other").

**When a lesion becomes pending** (:func:`status_after_done`): on Done, when the study requires review
(``Study.review_required``), when the lesion was under review before, or always for an automatic segmentation
(``auto:<tool>`` in ``trace_method``), whatever the study setting.

**Assisted provenance** (:func:`assisted_summary`): the voxels that assisted tools added (``StudioLib.draw.notifyAssist``:
Smart fill) become ``{"smartfill_voxels_pct", "tools"}`` in the lesion sidecar (``SlicerStudio.assist``) and the tokens of
``trace_method`` (``smartfill``; ``auto:<tool>`` for an automatic tool). Smart fill is assisted, never automatic, and
never approves anything.

**Gate**: ``Study.analysis_rows`` excludes rows that review has not approved when the study requires review;
:func:`gate_rows` also excludes automatic lesions that no human approved, whatever the setting, and
:func:`gate_summary` / :func:`gate_text` describe the exclusions and any recorded override for preflight and methods.

**Blinding** (:func:`blinded_columns`): with ``study.json`` ``blind_tracing``, the group, score and symptom columns are hidden
from tracers and reviewers; network maps are never shown while tracing or reviewing (Studio). Changes of the study
settings are audited (:func:`set_study_setting`) and :func:`provenance` reports them.

Paths: the audit log holds participant ids and study-relative paths only (never an absolute local path).
"""

import contextlib
import datetime
import json
import os

STATES = ("none", "pending", "approved", "changes", "rejected")
#: Allowed transitions {from: (to, ...)}
TRANSITIONS = {
    "none": ("pending",),
    "pending": ("approved", "changes", "rejected"),
    "changes": ("pending",),
    "approved": ("pending",),
    "rejected": ("pending",),
}
#: States a reviewer decides (they set ``reviewer``)
DECISIONS = ("approved", "changes", "rejected")
#: Reasons of a rejection
REJECT_REASONS = ("image quality", "not a focal lesion", "duplicate", "outside criteria", "other")
#: Labels of the states (UI and methods text)
STATE_LABELS = {"none": "Not reviewed", "pending": "Pending review", "approved": "Approved", "changes": "Changes requested", "rejected": "Rejected"}
#: Audit events of this module
AUDIT_EVENT = "review"
SETTING_EVENT = "study_setting"
OVERRIDE_EVENT = "review_override"
#: Study settings that :func:`set_study_setting` changes
STUDY_SETTINGS = ("review_required", "blind_tracing")
#: Columns hidden from tracers in a blind study, besides the group and score columns (when the table has them)
SYMPTOM_COLUMNS = ("symptom", "domain", "group_original")
#: Token of ``trace_method`` written by Smart fill (A-02) and the prefix of automatic tools
SMARTFILL_TOKEN = "smartfill"
AUTO_PREFIX = "auto:"
REVIEW_EDIT_TOKEN = "review-edit"
#: Exclusion reasons of :func:`gate_rows` (the first is ``study.EXCLUDED_REVIEW``)
EXCLUDED_AUTO = "automatic segmentation not approved"

_PRIVATE_DIR = ".studio"
_AUDIT_JSONL = "audit.jsonl"
_NA = "n/a"


class TransitionError(ValueError):
    """A review transition that the rules do not allow (unknown state, wrong order, missing comment or reason)."""


def _now():
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _missing(value):
    return value is None or (isinstance(value, str) and value.strip() in ("", _NA))


def _study_path(study):
    """The folder of a study object or path (a study.json path gives its folder)."""
    path = getattr(study, "path", study)
    path = os.path.abspath(os.fspath(path))
    if os.path.basename(path) == "study.json":
        path = os.path.dirname(path)
    return path


#
# Rows and trace_method tokens
#


def status_of(row):
    """The review state of a row ("none" when the column is missing or "n/a")."""
    value = (row or {}).get("review_status")
    return "none" if _missing(value) else str(value)


def trace_tokens(method):
    """The tokens of a ``trace_method`` value (or a row): ``"manual+smartfill"`` → ``["manual", "smartfill"]``."""
    if isinstance(method, dict):
        method = method.get("trace_method")
    if _missing(method):
        return []
    return [token for token in str(method).split("+") if token and token != _NA]


def add_tokens(method, *tokens):
    """``trace_method`` with ``tokens`` appended once each (order kept); "manual" is the first token of an empty method."""
    current = trace_tokens(method) or ["manual"]
    for token in tokens:
        if token and token not in current:
            current.append(token)
    return "+".join(current)


def auto_tokens(method):
    """The ``auto:<tool>`` tokens of a ``trace_method`` value or a row."""
    return [token for token in trace_tokens(method) if token.startswith(AUTO_PREFIX)]


def is_auto(method):
    """True for an automatic segmentation (an ``auto:<tool>`` token in ``trace_method``)."""
    return bool(auto_tokens(method))


def is_auto_unreviewed(row):
    """An automatic segmentation that no human approved (the 'Auto-unreviewed' queue)."""
    return is_auto(row) and status_of(row) != "approved"


def needs_review(row):
    """A lesion waiting for its review (the 'Needs review' queue): pending, and included or reviewed before (a reopened
    rejection waits with include = 0 until it is approved).
    """
    row = row or {}
    return status_of(row) == "pending" and (str(row.get("include", "1")) != "0" or not _missing(row.get("reviewer")))


def changes_requested(row):
    """A lesion whose reviewer asked for changes (the 'Changes requested' queue)."""
    return status_of(row) == "changes"


#
# Transitions
#


def can_transition(source, target):
    """True if ``source → target`` is allowed (:data:`TRANSITIONS`)."""
    return target in TRANSITIONS.get(source or "none", ())


def allowed_transitions(row):
    """The states a row may go to next."""
    return TRANSITIONS.get(status_of(row), ())


def _comment_text(target, comment, reason):
    comment = (comment or "").strip()
    if target == "rejected":
        return f"{reason}: {comment}" if comment else reason
    return comment or None


def validate_transition(row, target, who, comment=None, reason=None):
    """Raise :class:`TransitionError` if ``row`` may not go to ``target`` with these values; return the current state."""
    if target not in STATES:
        raise TransitionError(f"Unknown review state {target!r} (one of {', '.join(STATES)})")
    if not isinstance(who, str) or not who.strip():
        raise TransitionError("A review transition needs the name of who made it")
    source = status_of(row)
    if not can_transition(source, target):
        allowed = ", ".join(TRANSITIONS.get(source, ())) or "nothing"
        raise TransitionError(f"{row.get('participant_id')}: a lesion in the state {source!r} cannot go to {target!r} (allowed: {allowed})")
    if target == "changes" and not (comment or "").strip():
        raise TransitionError("Requesting changes needs a comment that says what should change")
    if target == "rejected":
        if reason not in REJECT_REASONS:
            raise TransitionError(f"A rejection needs a reason: one of {', '.join(REJECT_REASONS)}")
        if reason == "other" and not (comment or "").strip():
            raise TransitionError("A rejection for another reason needs a comment")
    elif reason is not None:
        raise TransitionError("Only a rejection has a reason")
    return source


def apply_transition(study, sid, to, who, comment=None, reason=None, save=False, when=None, extra=None):
    """Move a lesion's review to ``to`` and append the audit record (see the module docstring).

    The row gets ``review_status``; a decision (approved, changes, rejected) also sets ``reviewer`` = ``who`` and
    ``review_comment`` ("<reason>: <comment>" for a rejection). A rejection sets ``include`` = 0 and records the value it
    replaced (``include_before``). Reopening a rejection (rejected → pending) keeps ``include`` = 0 (``include_held``);
    the approval that follows restores the value the rejection replaced (``include_restored``).

    :param study: a ``study.Study`` (changed in memory; the GUI session saves it, see ``save``).
    :param who: the reviewer, or the tracer for → pending.
    :param save: also save the study (``Study.save``, with lock and merge) before the audit record is appended.
    :param extra: more keys of the audit record (for example {"via": "done", "edited": True}).
    :return: the audit record.
    :raises KeyError: unknown subject. :raises TransitionError: see :func:`validate_transition` (nothing is changed).
    """
    row = study.subject(sid)
    source = validate_transition(row, to, who, comment, reason)
    who = who.strip()
    values = {"review_status": to}
    record = {"event": AUDIT_EVENT, "sid": sid, "from": source, "to": to, "who": who, "when": when or _now(), "comment": (comment or "").strip() or None}
    if to in DECISIONS:
        values["reviewer"] = who
        values["review_comment"] = _comment_text(to, comment, reason)
    excluded = str(row.get("include", "1")) == "0"
    held = held_include(study, sid) if excluded else None
    if to == "rejected":
        record["reason"] = reason
        # Rejected again after a reopening: the value before the first rejection is the one to restore
        record["include_before"] = held if held is not None else str(row.get("include", "1"))
        values["include"] = "0"
    elif source == "rejected" and excluded:
        record["include_held"] = "0"  # excluded until a reviewer approves it
    elif to == "approved" and held is not None:
        values["include"] = held
        record["include_restored"] = held
    if getattr(study, "blind_tracing", False):
        record["blind"] = True
    if extra:
        record.update({key: value for key, value in dict(extra).items() if key not in ("event", "sid", "from", "to")})
    study.set(sid, values)
    if save:
        study.save()
    append_audit(study, record)
    return record


def status_after_done(study, row, method=None, edited=True):
    """The review state a lesion gets when its tracer presses Done, or None to keep it.

    Pending when the lesion is automatic (always), when the study requires review, or when it was under review before
    (changes requested; approved or rejected and then ``edited``). A pending lesion stays pending. A rejected lesion that
    becomes pending stays excluded (``include`` = 0) until a reviewer approves it (:func:`apply_transition`).

    :param method: the ``trace_method`` after Done (default: the row's).
    """
    source = status_of(row)
    method = row.get("trace_method") if method is None else method
    if source == "pending":
        return None
    if source in ("approved", "rejected") and not edited:
        return None
    if is_auto(method) or bool(getattr(study, "review_required", False)) or source != "none":
        return "pending" if can_transition(source, "pending") else None
    return None


#
# Audit log
#


def audit_path(study):
    """``<study>/.studio/audit.jsonl``."""
    return os.path.join(_study_path(study), _PRIVATE_DIR, _AUDIT_JSONL)


def append_audit(study, records):
    """Append one record (a dict) or several to the study's audit log (one write call). Return the log path."""
    records = [records] if isinstance(records, dict) else list(records)
    path = audit_path(study)
    if not records:
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


def read_audit(study, sid=None, event=None):
    """The records of the audit log (damaged lines are skipped), optionally of one subject and one event."""
    records = []
    try:
        with open(audit_path(study), encoding="utf-8") as f:
            for line in f:
                with contextlib.suppress(ValueError):
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        continue
                    if sid is not None and record.get("sid") != sid:
                        continue
                    if event is not None and record.get("event") != event:
                        continue
                    records.append(record)
    except OSError:
        pass
    return records


def history(study, sid):
    """The review records of a subject, oldest first."""
    return read_audit(study, sid=sid, event=AUDIT_EVENT)


def last_rejection(study, sid):
    """The latest rejection record of a subject, or None."""
    rejections = [record for record in history(study, sid) if record.get("to") == "rejected"]
    return rejections[-1] if rejections else None


def held_include(study, sid):
    """The ``include`` value that the latest rejection of a subject replaced, while no approval has restored it, or None.

    The value a reviewer's approval of a reopened rejection gives back ("0" or "1").
    """
    records = history(study, sid)
    for index in range(len(records) - 1, -1, -1):
        if records[index].get("to") != "rejected":
            continue
        if any("include_restored" in record for record in records[index + 1:]):
            return None
        before = records[index].get("include_before")
        return before if before in ("0", "1") else None
    return None


#
# Assisted provenance
#


def assisted_summary(counts, total_voxels=None):
    """``{"smartfill_voxels_pct", "tools", "auto"}`` of the assisted counts of one lesion.

    :param counts: ``StudioLib.draw.assistCounts(sid)``: {tool: {"voxels", "applies", "params"}} (the segment tag of the
      lesion is the key Draw counts them under); a stored summary (with "tools") is accepted too.
    :param total_voxels: voxels of the lesion as written (None: the percentage is not known).
    :return: ``smartfill_voxels_pct`` is the percentage of the lesion's voxels added by Smart fill (capped at 100; None
      without Smart fill or without the total), ``tools`` {tool: {"voxels", "applies"}}, ``auto`` the ``auto:<tool>``
      tokens of automatic tools.
    """
    if isinstance(counts, dict) and isinstance(counts.get("tools"), dict):
        counts = counts["tools"]
    tools = {}
    for tool, entry in dict(counts or {}).items():
        entry = entry if isinstance(entry, dict) else {}
        voxels = int(entry.get("voxels", 0) or 0)
        applies = int(entry.get("applies", 0) or 0)
        if voxels or applies:
            tools[str(tool)] = {"voxels": voxels, "applies": applies}
    smartfill = tools.get(SMARTFILL_TOKEN, {}).get("voxels", 0)
    pct = None
    if smartfill and total_voxels:
        pct = round(min(100.0, 100.0 * smartfill / float(total_voxels)), 2)
    auto = sorted(tool for tool in tools if tool.startswith(AUTO_PREFIX))
    return {"smartfill_voxels_pct": pct, "tools": tools, "auto": auto}


def method_after_assist(method, summary):
    """``trace_method`` with the tokens of an :func:`assisted_summary`: ``smartfill`` and ``auto:<tool>``."""
    tokens = []
    if (summary or {}).get("tools", {}).get(SMARTFILL_TOKEN, {}).get("voxels"):
        tokens.append(SMARTFILL_TOKEN)
    tokens.extend((summary or {}).get("auto") or ())
    return add_tokens(method, *tokens)


def sidecar_values(study, row, summary=None):
    """Values for the ``SlicerStudio`` object of the lesion sidecar: review state, assisted summary and blinding."""
    values = {"review": {"status": status_of(row), "reviewer": None if _missing(row.get("reviewer")) else row.get("reviewer")}}
    if not _missing(row.get("trace_method")):
        values["trace_method"] = row.get("trace_method")
    if summary is not None:
        values["assist"] = {"smartfill_voxels_pct": summary.get("smartfill_voxels_pct"), "tools": summary.get("tools") or {}}
    if getattr(study, "blind_tracing", False):
        values["blind_tracing"] = True
    return values


#
# Blinding and study settings
#


def blinded_columns(study):
    """Columns hidden from tracers and reviewers when ``study.json`` sets ``blind_tracing`` (else []), in table order.

    The group columns, the score columns and the symptom columns (symptom, domain, group_original) of the table; "group"
    is always included.
    """
    if not getattr(study, "blind_tracing", False):
        return []
    hidden = {"group"}
    with contextlib.suppress(Exception):
        hidden.update(study.group_columns())
    with contextlib.suppress(Exception):
        hidden.update(name for name, _confirmed in study.score_columns())
    hidden.update(SYMPTOM_COLUMNS)
    columns = list(getattr(study, "columns", ()) or ())
    ordered = [column for column in columns if column in hidden]
    if "group" not in ordered:
        ordered.insert(0, "group")
    return ordered


def blinded_row(study, row):
    """A copy of a row without its blinded columns."""
    hidden = set(blinded_columns(study))
    return {key: value for key, value in dict(row).items() if key not in hidden}


def set_study_setting(study, key, value, who, save=False):
    """Change ``review_required`` or ``blind_tracing`` and audit it. Return the record, or None when nothing changed.

    ``value`` None for review_required goes back to its default (required when a row is a case report).
    """
    if key not in STUDY_SETTINGS:
        raise ValueError(f"Unknown review setting {key!r} (one of {', '.join(STUDY_SETTINGS)})")
    stored = getattr(study, "metadata", {}).get(key)
    before = getattr(study, key)
    setattr(study, key, value)
    after = getattr(study, key)
    if before == after and stored == getattr(study, "metadata", {}).get(key):
        return None
    record = {"event": SETTING_EVENT, "key": key, "from": before, "to": after, "who": (who or "").strip() or None, "when": _now()}
    if save:
        study.save()
    append_audit(study, record)
    return record


#
# Gate (analyses use approved lesions only) and provenance
#


def gate_rows(study, rows, include_unreviewed=False):
    """Split rows an analysis was about to use: ``(kept, {reason: [participant ids]})``.

    Automatic segmentations that no human approved are excluded whatever the study setting (:data:`EXCLUDED_AUTO`); with
    the study requiring review, every row not approved is excluded (``study.EXCLUDED_REVIEW``). ``include_unreviewed`` (a
    recorded override) keeps them.
    """
    from .study import EXCLUDED_REVIEW

    required = bool(getattr(study, "review_required", False))
    kept, excluded = [], {}
    for row in rows:
        reason = None
        if not include_unreviewed:
            if is_auto_unreviewed(row):
                reason = EXCLUDED_AUTO
            elif required and status_of(row) != "approved":
                reason = EXCLUDED_REVIEW
        if reason is None:
            kept.append(row)
        else:
            excluded.setdefault(reason, []).append(row.get("participant_id"))
    return kept, excluded


def gate_summary(study, include_unreviewed=False):
    """What the review gate does to the included rows of a study.

    :return: {"required", "blind_tracing", "excluded": {state: [ids]}, "auto_unreviewed": [ids], "included_anyway": [ids],
      "approved": n, "included": n}; with ``include_unreviewed`` the rows the gate would exclude are listed under
      "included_anyway" instead.
    """
    required = bool(getattr(study, "review_required", False))
    rows = [row for row in study.subjects if str(row.get("include", "1")) != "0"]
    excluded, auto, anyway = {}, [], []
    for row in rows:
        sid = row["participant_id"]
        gated = is_auto_unreviewed(row) or (required and status_of(row) != "approved")
        if not gated:
            continue
        if include_unreviewed:
            anyway.append(sid)
            continue
        if is_auto_unreviewed(row):
            auto.append(sid)
        excluded.setdefault(status_of(row), []).append(sid)
    return {"required": required, "blind_tracing": bool(getattr(study, "blind_tracing", False)), "excluded": excluded, "auto_unreviewed": auto,
            "included_anyway": anyway, "approved": sum(1 for row in rows if status_of(row) == "approved"), "included": len(rows)}


def _count(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def gate_text(summary):
    """One sentence for methods and preflight from :func:`gate_summary`."""
    parts = [f"Review: {'required' if summary['required'] else 'not required'} for analyses; {summary['approved']} of {summary['included']} included lesion(s) approved"]
    excluded = summary.get("excluded") or {}
    total = sum(len(sids) for sids in excluded.values())
    if total:
        states = ", ".join(f"{len(sids)} {STATE_LABELS.get(state, state).lower()}" for state, sids in sorted(excluded.items()))
        parts.append(f"{_count(total, 'lesion')} excluded as not approved ({states})")
    if summary.get("auto_unreviewed"):
        parts.append(f"{_count(len(summary['auto_unreviewed']), 'automatic segmentation')} excluded until approved by a human")
    if summary.get("included_anyway"):
        parts.append(f"{_count(len(summary['included_anyway']), 'lesion')} not approved by review included anyway (explicit override, recorded)")
    if summary.get("blind_tracing"):
        parts.append("tracing and review were blind to group, score and symptom columns")
    return "; ".join(parts) + "."


def record_override(study, sids, who, check="unreviewed", context=None):
    """Audit an 'Include anyway' of lesions that review has not approved. Return the record."""
    record = {"event": OVERRIDE_EVENT, "check": check, "sids": sorted(sids), "who": (who or "").strip() or None, "when": _now()}
    if context:
        record["context"] = context
    append_audit(study, record)
    return record


def provenance(study):
    """The review settings and counts of a study for run provenance and methods: settings, counts per state, reviewers."""
    rows = study.subjects
    counts = {}
    for row in rows:
        counts[status_of(row)] = counts.get(status_of(row), 0) + 1
    reviewers = sorted({str(row["reviewer"]) for row in rows if not _missing(row.get("reviewer"))})
    changes = read_audit(study, event=SETTING_EVENT)
    return {"review_required": bool(getattr(study, "review_required", False)), "blind_tracing": bool(getattr(study, "blind_tracing", False)),
            "states": counts, "reviewers": len(reviewers), "auto_unreviewed": sum(1 for row in rows if is_auto_unreviewed(row)),
            "setting_changes": len(changes)}
