"""Threshold rules for network maps.

A threshold is stored as a *rule* (a small JSON object), never as a bare number: the threshold value
is re-resolved from the rule, the number of subjects N and the number of voxels V every time a map is
displayed or exported, so a T threshold derived for one N is never silently reused for another N.

Rule kinds::

    {"kind": "fwe_bonferroni", "alpha": 0.05}                 |T| >= t.isf(alpha / 2 / V, df=N-1)
    {"kind": "t_convention", "value": 7, "reference_n": 1000}  |T| >= 7, only for N >= 900
    {"kind": "fz_abs", "value": 0.05}                         |mean Fz| >= 0.05 (effect size)
    {"kind": "custom_t", "value": 5.0, "reference_n": 500}     |T| >= 5 (reference_n is optional)
    {"kind": "top_pct", "value": 10, "statistic": "Fz", "sign": "both"}
                                                              the top 10% of |map| (rank rule, any N)

A map whose N is unknown (a T map opened from a file without a sidecar) can only take the rules that do
not need N (:data:`N_FREE_KINDS`, :func:`resolve_unknown_n`): a custom |T| value is applied as typed,
without p-values, and the rules derived from N (FWE, the |T| >= 7 convention) are not resolved.

Rules that carry a ``reference_n`` get a banner when N differs from it by more than 10%. The
``t_convention`` rule is not allowed below N = 900 unless the user chose to keep it
(``"override": true``, recorded in provenance).

``fz_abs`` and ``top_pct`` are N-independent *alternatives* to the T rules, never their
"equivalents": T = mean Fz / (sd / sqrt(N)) and sd varies by voxel, so no Fz value corresponds to a T
threshold, and this module never converts one into the other. ``top_pct`` needs the map values to
resolve (the threshold is the |value| that leaves x% of the finite in-mask voxels above it).

Symmetric |T| rules on connectomes preprocessed without global signal regression get the GSR banner
(addendum §3.2, Cohen & Fox 2021); with GSR, negative peaks get the Murphy & Fox 2017 note.
"""

import json
import math
from dataclasses import asdict, dataclass

KINDS = ("fwe_bonferroni", "t_convention", "fz_abs", "custom_t", "top_pct")
DEFAULT_RULE = {"kind": "fwe_bonferroni", "alpha": 0.05}
RULE_DEFAULTS = {
    "fwe_bonferroni": {"alpha": 0.05},
    "t_convention": {"value": 7.0, "reference_n": 1000},
    "fz_abs": {"value": 0.05},
    "custom_t": {"value": 5.0, "reference_n": None},
    "top_pct": {"value": 10.0, "statistic": "Fz", "sign": "both"},
}
RULE_LABELS = {
    "fwe_bonferroni": "FWE (Bonferroni)",
    "t_convention": "|T| ≥ 7 convention (N≈1000)",
    "fz_abs": "Fz effect size",
    "custom_t": "Custom T",
    "top_pct": "Top x% (N-independent)",
}
T_CONVENTION_MIN_N = 900  # the |T| >= 7 convention is only meaningful for N close to 1000
REFERENCE_N_TOLERANCE = 0.1  # banner when |N - reference_n| / reference_n exceeds this
SYMMETRIC_T_KINDS = ("fwe_bonferroni", "t_convention", "custom_t")  # |T| >= x rules (the GSR banner applies)
SIGNS = ("both", "pos", "neg")
GSR_BANNER = "Non-GSR connectomes centre uninformative connections above 0; consider asymmetric or effect-size thresholds (Cohen & Fox 2021)."
NEGATIVE_PEAKS_NOTE = "The magnitude and extent of negative correlations are influenced by global signal regression (Murphy & Fox 2017)."
TOP_PCT_NEEDS_VALUES = "The top-x% rule needs the map values."
N_FREE_KINDS = ("custom_t", "fz_abs", "top_pct")  # rules that can be applied to a map whose N is unknown
N_UNKNOWN_NOTE = "The N of this map is unknown: no p-value."


@dataclass(frozen=True)
class Resolved:
    """A threshold rule resolved for a map with ``n`` subjects and ``v`` voxels.

    ``statistic`` is "T" or "Fz" (the map the threshold applies to), ``value`` the absolute
    threshold, ``df`` = N - 1. ``p_unc`` is the two-sided uncorrected p-value at the threshold and
    ``p_fwe`` its Bonferroni family-wise bound over V voxels (both None for effect-size rules).
    ``banner`` is a warning to show next to the map (None when there is nothing to say) and
    ``allowed`` tells whether the rule may be applied without an explicit user choice.

    The trailing fields are optional (older dictionaries lack them): ``kind`` is the rule kind,
    ``fraction`` the fraction (0-1) of finite in-mask voxels that pass, when the map values were given
    to :func:`resolve`, and ``notes`` a tuple of annotations that are not warnings (e.g. the
    negative-peaks note for GSR connectomes).
    """

    statistic: str
    value: float
    df: int
    p_unc: float | None
    p_fwe: float | None
    banner: str | None
    allowed: bool
    n: int
    v: int
    rule: dict
    kind: str | None = None
    fraction: float | None = None
    notes: tuple = ()

    def to_dict(self):
        """JSON-compatible dictionary (non-finite numbers become None)."""
        out = asdict(self)
        out["notes"] = list(self.notes)
        for key in ("value", "p_unc", "p_fwe", "fraction"):
            if out[key] is not None and not math.isfinite(out[key]):
                out[key] = None
        return out

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data):
        """Inverse of :meth:`to_dict` (a missing threshold value becomes NaN).

        Dictionaries written before ``kind``, ``fraction`` and ``notes`` existed are accepted.

        :raises ValueError: missing or invalid fields.
        """
        if not isinstance(data, dict):
            raise ValueError(f"A resolved threshold must be a JSON object, got {type(data).__name__}")
        missing = [field for field in ("statistic", "df", "allowed", "n", "v", "rule") if data.get(field) is None]
        if missing:
            raise ValueError(f"Resolved threshold without {', '.join(missing)}")
        if data["statistic"] not in ("T", "Fz"):
            raise ValueError(f"Unknown threshold statistic {data['statistic']!r}")
        values = {field: data.get(field) for field in cls.__dataclass_fields__}
        values["value"] = float("nan") if values["value"] is None else _number(values, "value")
        for key in ("p_unc", "p_fwe", "fraction"):
            values[key] = None if values[key] is None else _number(values, key)
        values["df"], values["n"], values["v"] = (_number(values, key, integer=True) for key in ("df", "n", "v"))
        values["allowed"] = _flag(values, "allowed")
        values["banner"] = None if values["banner"] is None else str(values["banner"])
        values["rule"] = normalize_rule(values["rule"])
        values["kind"] = None if values["kind"] is None else str(values["kind"])
        notes = values["notes"]
        if notes is None:
            notes = ()
        elif isinstance(notes, str) or not isinstance(notes, (list, tuple)):
            raise ValueError(f"notes must be a list of strings, got {notes!r}")
        values["notes"] = tuple(str(note) for note in notes)
        return cls(**values)

    @classmethod
    def from_json(cls, text):
        return cls.from_dict(json.loads(text))


def _number(values, key, integer=False):
    """``values[key]`` as a float (or an int), raising ValueError for anything else (None, booleans, lists, ...)."""
    raw = values[key]
    try:
        if isinstance(raw, bool):
            raise TypeError
        number = float(raw)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{key} must be a number, got {raw!r}") from None
    if integer:
        if not number.is_integer():
            raise ValueError(f"{key} must be a whole number, got {raw!r}")
        return int(number)
    return number


def _flag(values, key):
    """``values[key]`` as a boolean: true/false or 1/0 (None counts as false)."""
    raw = values[key]
    if raw is None or isinstance(raw, bool):
        return bool(raw)
    if isinstance(raw, int) and raw in (0, 1):
        return bool(raw)
    raise ValueError(f"{key} must be true or false, got {raw!r}")


def normalize_rule(rule=None):
    """Return a validated rule dict with defaults filled in.

    :param rule: a rule dict, its JSON text, or None for :data:`DEFAULT_RULE`.
    :raises ValueError: unknown kind or invalid parameters.
    """
    if rule is None:
        rule = DEFAULT_RULE
    elif isinstance(rule, str):
        rule = json.loads(rule)
    if not isinstance(rule, dict):
        raise ValueError(f"A threshold rule must be a JSON object, got {type(rule).__name__}")
    kind = rule.get("kind")
    if kind not in KINDS:
        raise ValueError(f"Unknown threshold rule kind {kind!r}; expected one of {', '.join(KINDS)}")
    out = {"kind": kind, **RULE_DEFAULTS[kind]}
    out.update({key: value for key, value in rule.items() if key != "kind"})
    if kind == "top_pct":
        out["value"] = _number(out, "value")
        if not 0.0 < out["value"] < 100.0:
            raise ValueError(f"The top-x% value must be between 0 and 100 (exclusive), got {out['value']}")
        if out["statistic"] not in ("Fz", "T"):
            raise ValueError(f"The top-x% statistic must be Fz or T, got {out['statistic']!r}")
        if out["sign"] not in SIGNS:
            raise ValueError(f"The top-x% sign must be one of {', '.join(SIGNS)}, got {out['sign']!r}")
    elif kind == "fwe_bonferroni":
        out["alpha"] = _number(out, "alpha")
        if not 0.0 < out["alpha"] < 1.0:
            raise ValueError(f"alpha must be between 0 and 1, got {out['alpha']}")
    else:
        out["value"] = _number(out, "value")
        if not (math.isfinite(out["value"]) and out["value"] > 0.0):
            raise ValueError(f"The threshold value must be positive, got {out['value']}")
    if out.get("reference_n") is not None:
        out["reference_n"] = _number(out, "reference_n", integer=True)
        if out["reference_n"] < 2:
            raise ValueError(f"reference_n must be at least 2, got {out['reference_n']}")
    if "override" in out:
        out["override"] = _flag(out, "override")
    return out


def rule_to_json(rule):
    """Compact JSON text of a (normalized) rule, e.g. for a parameter node or provenance."""
    return json.dumps(normalize_rule(rule), sort_keys=True, separators=(",", ":"))


def rule_from_json(text):
    """Parse and normalize a rule from JSON text (empty text gives the default rule)."""
    return normalize_rule(text if text and text.strip() else None)


def rule_label(rule):
    """Short human-readable name of a rule kind (for combo boxes)."""
    return RULE_LABELS[normalize_rule(rule)["kind"]]


def _two_sided_p(t_value, df):
    from scipy import stats as scipy_stats

    return float(min(1.0, 2.0 * scipy_stats.t.sf(abs(t_value), df)))


def _reference_banner(reference_n, n, note):
    if reference_n is None or abs(n - reference_n) / reference_n <= REFERENCE_N_TOLERANCE:
        return None
    detail = f" ({note})" if note else ""
    return f"This threshold was defined for N≈{reference_n}; this map has N={n}{detail}. Use FWE or effect size."


def _scores(values, sign):
    """Finite map values as float64, oriented so that "passes" means ``score >= threshold``."""
    import numpy as np

    values = np.asarray(values, dtype=np.float64).reshape(-1)
    values = values[np.isfinite(values)]
    if sign == "pos":
        return values
    if sign == "neg":
        return -values
    return np.abs(values)


def _with_gsr(resolved, gsr):
    """Append the GSR banner (non-GSR connectome, symmetric |T| rule) or add the negative-peaks note (GSR)."""
    import dataclasses

    changes = {}
    if gsr is False and resolved.rule["kind"] in SYMMETRIC_T_KINDS:
        changes["banner"] = f"{resolved.banner} {GSR_BANNER}" if resolved.banner else GSR_BANNER
    note = negative_peaks_note(gsr)
    if note and resolved.rule.get("sign", "both") != "pos" and note not in resolved.notes:
        changes["notes"] = (*resolved.notes, note)
    return dataclasses.replace(resolved, **changes) if changes else resolved


def resolve(rule, n, v, note=None, gsr=None, values=None):
    """Resolve a threshold rule for a map with ``n`` subjects and ``v`` voxels.

    :param rule: rule dict or JSON text (see :func:`normalize_rule`).
    :param n: number of subjects of the map (df = n - 1).
    :param v: number of voxels tested (the connectome mask size).
    :param note: optional word shown after N in the banner, e.g. "partial" or "preview".
    :param gsr: the connectome's ``preprocessing.gsr`` (None when unknown). False adds the GSR banner to
      symmetric |T| rules; True adds the negative-peaks note (:func:`negative_peaks_note`) to ``notes``.
    :param values: optional in-mask values of the map the rule applies to (non-finite values are
      ignored). Required by ``top_pct``; when given, ``fraction`` is the fraction of voxels that pass.
    :return: :class:`Resolved`.
    """
    rule = normalize_rule(rule)
    n, v = int(n), int(v)
    if v < 1:
        raise ValueError(f"The number of voxels must be at least 1, got {v}")
    resolved = _with_gsr(_resolve(rule, n, v, note, values), gsr)
    if values is not None and math.isfinite(resolved.value):
        import dataclasses

        resolved = dataclasses.replace(resolved, fraction=fraction_connected(values, resolved))
    return resolved


def resolve_unknown_n(rule, v, values=None):
    """Resolve a rule for a map whose number of subjects is unknown (a map file without a sidecar).

    Only the rules of :data:`N_FREE_KINDS` can be resolved: ``custom_t`` is applied as typed (no
    p-value, and a banner when the rule records a ``reference_n``), ``fz_abs`` and ``top_pct`` as for
    any N. The result has ``n`` = ``df`` = 0 and :data:`N_UNKNOWN_NOTE` in ``notes``, which :func:`describe`
    shows as "N unknown".

    :param v: number of voxels tested (e.g. the non-zero voxels of the map).
    :param values: optional map values (required by ``top_pct``; gives ``fraction``).
    :raises ValueError: a rule that needs N (FWE, the |T| >= 7 convention); the message says so.
    """
    import dataclasses

    rule = normalize_rule(rule)
    kind = rule["kind"]
    if kind not in N_FREE_KINDS:
        raise ValueError(f"The {RULE_LABELS[kind]} rule needs the number of subjects (N) of the map, which is unknown.")
    v = int(v)
    if v < 1:
        raise ValueError(f"The number of voxels must be at least 1, got {v}")
    if kind == "custom_t":
        banner = None
        if rule.get("reference_n") is not None:
            banner = f"This threshold was defined for N≈{rule['reference_n']}; the N of this map is unknown. Use effect size where you can."
        resolved = Resolved("T", rule["value"], 0, None, None, banner, True, 0, v, rule, kind, notes=(N_UNKNOWN_NOTE,))
    else:
        resolved = _resolve(rule, 1, v, None, values)
        resolved = dataclasses.replace(resolved, n=0, df=0, notes=(*resolved.notes, N_UNKNOWN_NOTE))
    if values is not None and math.isfinite(resolved.value):
        resolved = dataclasses.replace(resolved, fraction=fraction_connected(values, resolved))
    return resolved


def _resolve(rule, n, v, note, values):
    kind = rule["kind"]
    df = max(n - 1, 0)
    if kind == "top_pct":
        # A rank rule: N-independent and without p-values (the cut is a percentile of this map)
        if values is None:
            return Resolved(rule["statistic"], math.nan, df, None, None, TOP_PCT_NEEDS_VALUES, False, n, v, rule, kind)
        scores = _scores(values, rule["sign"])
        if scores.size == 0:
            return Resolved(rule["statistic"], math.nan, df, None, None, "The map has no finite values.", False, n, v, rule, kind)
        import numpy as np

        value = float(np.percentile(scores, 100.0 - rule["value"]))
        return Resolved(rule["statistic"], value, df, None, None, None, True, n, v, rule, kind)
    if kind == "fz_abs":
        # An effect-size threshold on the mean Fz map: no p-value, valid for any N >= 1
        return Resolved("Fz", rule["value"], df, None, None, None if n >= 1 else "The map has no subjects.", n >= 1, n, v, rule, kind)
    if n < 2:
        return Resolved("T", math.inf, df, None, None, "A T map needs at least 2 subjects.", False, n, v, rule, kind)
    if kind == "fwe_bonferroni":
        from scipy import stats as scipy_stats

        p_unc = rule["alpha"] / v
        value = float(scipy_stats.t.isf(p_unc / 2.0, df))
        return Resolved("T", value, df, p_unc, rule["alpha"], None, True, n, v, rule, kind)
    value = rule["value"]
    p_unc = _two_sided_p(value, df)
    p_fwe = min(1.0, p_unc * v)
    banner = _reference_banner(rule.get("reference_n"), n, note)
    allowed = True
    if kind == "t_convention" and n < T_CONVENTION_MIN_N:
        allowed = bool(rule.get("override", False))
        if banner is None:
            banner = _reference_banner(rule["reference_n"] or 1000, n, note) or f"The |T| ≥ {value:g} convention needs N ≥ {T_CONVENTION_MIN_N}; this map has N={n}. Use FWE or effect size."
    return Resolved("T", value, df, p_unc, p_fwe, banner, allowed, n, v, rule, kind)


def fraction_connected(values, resolved, sign=None):
    """Fraction (0-1) of the finite values that pass a resolved threshold ("% of in-mask voxels").

    :param values: in-mask values of the map the threshold applies to.
    :param resolved: :class:`Resolved`.
    :param sign: "both" (|value| >= threshold), "pos" or "neg"; default: the rule's sign, else "both".
    :return: the fraction, or NaN when the threshold is undefined or there are no finite values.
    """
    sign = sign or resolved.rule.get("sign", "both")
    if sign not in SIGNS:
        raise ValueError(f"sign must be one of {', '.join(SIGNS)}, got {sign!r}")
    scores = _scores(values, sign)
    if scores.size == 0 or math.isnan(resolved.value):
        return math.nan
    return float((scores >= resolved.value).sum() / scores.size)


def negative_peaks_note(gsr):
    """The note shown next to negative peaks of maps from a GSR connectome (None otherwise)."""
    return NEGATIVE_PEAKS_NOTE if gsr is True else None


def format_p(p):
    """Format a p-value compactly: 1.7e-7, 4.7e-12 or 0.012."""
    if p is None or not math.isfinite(p):
        return "n/a"
    if p >= 1e-3:
        return f"{p:.2g}"
    mantissa, exponent = f"{p:.1e}".split("e")
    return f"{mantissa}e{int(exponent)}"


def _format_fraction(fraction):
    percent = 100.0 * fraction
    if percent == 0.0 or percent >= 10.0:
        return f"{percent:.0f}%"
    if percent >= 1.0:
        return f"{percent:.1f}%"
    return f"{percent:.2g}%"


def describe(resolved, fraction=None):
    """The derived line shown under the threshold controls, e.g.
    "⇒ |T| ≥ 5.27 at N=1000 (df=999), V=292,019 · p_unc ≤ 1.7e-7".

    :param fraction: optional fraction (0-1) of in-mask voxels that pass (:func:`fraction_connected`),
      appended as " · 63% of in-mask voxels".
    """
    suffix = "" if fraction is None or not math.isfinite(fraction) else f" · {_format_fraction(fraction)} of in-mask voxels"
    unknown_n = N_UNKNOWN_NOTE in resolved.notes
    at_n = "N unknown" if unknown_n else f"N={resolved.n}"
    if unknown_n and resolved.rule.get("kind") == "custom_t" and math.isfinite(resolved.value):
        return f"⇒ |T| ≥ {resolved.value:.2f} at {at_n}, V={resolved.v:,} · as typed (no p-value)" + suffix
    if resolved.rule.get("kind") == "top_pct":
        sign = resolved.rule.get("sign", "both")
        quantity = {"both": f"|{resolved.statistic}|", "pos": resolved.statistic, "neg": f"-{resolved.statistic}"}[sign]
        cut = f" (≥ {resolved.value:.3g})" if math.isfinite(resolved.value) else ""
        line = f"⇒ top {resolved.rule['value']:g}% of {quantity}{cut} at {at_n}, V={resolved.v:,} · rank rule (no p-value)"
        return line + suffix
    if resolved.statistic == "Fz":
        return f"⇒ |Fz| ≥ {resolved.value:g} at {at_n}, V={resolved.v:,} · effect size (no p-value)" + suffix
    if not math.isfinite(resolved.value):
        return f"⇒ no T threshold at N={resolved.n} (at least 2 subjects are needed)"
    return f"⇒ |T| ≥ {resolved.value:.2f} at N={resolved.n} (df={resolved.df}), V={resolved.v:,} · p_unc ≤ {format_p(resolved.p_unc)}" + suffix
