"""Threshold rules for network maps.

A threshold is stored as a *rule* (a small JSON object), never as a bare number: the threshold value
is re-resolved from the rule, the number of subjects N and the number of voxels V every time a map is
displayed or exported, so a T threshold derived for one N is never silently reused for another N.

Rule kinds::

    {"kind": "fwe_bonferroni", "alpha": 0.05}                 |T| >= t.isf(alpha / 2 / V, df=N-1)
    {"kind": "t_convention", "value": 7, "reference_n": 1000}  |T| >= 7, only for N >= 900
    {"kind": "fz_abs", "value": 0.05}                         |mean Fz| >= 0.05 (effect size)
    {"kind": "custom_t", "value": 5.0, "reference_n": 500}     |T| >= 5 (reference_n is optional)

Rules that carry a ``reference_n`` get a banner when N differs from it by more than 10%. The
``t_convention`` rule is not allowed below N = 900 unless the user chose to keep it
(``"override": true``, recorded in provenance).
"""

import json
import math
from dataclasses import asdict, dataclass

KINDS = ("fwe_bonferroni", "t_convention", "fz_abs", "custom_t")
DEFAULT_RULE = {"kind": "fwe_bonferroni", "alpha": 0.05}
RULE_DEFAULTS = {
    "fwe_bonferroni": {"alpha": 0.05},
    "t_convention": {"value": 7.0, "reference_n": 1000},
    "fz_abs": {"value": 0.05},
    "custom_t": {"value": 5.0, "reference_n": None},
}
RULE_LABELS = {
    "fwe_bonferroni": "FWE (Bonferroni)",
    "t_convention": "|T| ≥ 7 convention (N≈1000)",
    "fz_abs": "Fz effect size",
    "custom_t": "Custom T",
}
T_CONVENTION_MIN_N = 900  # the |T| >= 7 convention is only meaningful for N close to 1000
REFERENCE_N_TOLERANCE = 0.1  # banner when |N - reference_n| / reference_n exceeds this


@dataclass(frozen=True)
class Resolved:
    """A threshold rule resolved for a map with ``n`` subjects and ``v`` voxels.

    ``statistic`` is "T" or "Fz" (the map the threshold applies to), ``value`` the absolute
    threshold, ``df`` = N - 1. ``p_unc`` is the two-sided uncorrected p-value at the threshold and
    ``p_fwe`` its Bonferroni family-wise bound over V voxels (both None for effect-size rules).
    ``banner`` is a warning to show next to the map (None when there is nothing to say) and
    ``allowed`` tells whether the rule may be applied without an explicit user choice.
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

    def to_dict(self):
        """JSON-compatible dictionary (non-finite numbers become None)."""
        out = asdict(self)
        for key in ("value", "p_unc", "p_fwe"):
            if out[key] is not None and not math.isfinite(out[key]):
                out[key] = None
        return out

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data):
        """Inverse of :meth:`to_dict` (a missing threshold value becomes NaN)."""
        values = {field: data.get(field) for field in cls.__dataclass_fields__}
        values["value"] = float("nan") if values["value"] is None else float(values["value"])
        values["df"], values["n"], values["v"] = int(values["df"]), int(values["n"]), int(values["v"])
        values["allowed"] = bool(values["allowed"])
        values["rule"] = normalize_rule(values["rule"])
        return cls(**values)

    @classmethod
    def from_json(cls, text):
        return cls.from_dict(json.loads(text))


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
    if kind == "fwe_bonferroni":
        out["alpha"] = float(out["alpha"])
        if not 0.0 < out["alpha"] < 1.0:
            raise ValueError(f"alpha must be between 0 and 1, got {out['alpha']}")
    else:
        out["value"] = float(out["value"])
        if not (math.isfinite(out["value"]) and out["value"] > 0.0):
            raise ValueError(f"The threshold value must be positive, got {out['value']}")
    if out.get("reference_n") is not None:
        out["reference_n"] = int(out["reference_n"])
        if out["reference_n"] < 2:
            raise ValueError(f"reference_n must be at least 2, got {out['reference_n']}")
    if "override" in out:
        out["override"] = bool(out["override"])
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


def resolve(rule, n, v, note=None):
    """Resolve a threshold rule for a map with ``n`` subjects and ``v`` voxels.

    :param rule: rule dict or JSON text (see :func:`normalize_rule`).
    :param n: number of subjects of the map (df = n - 1).
    :param v: number of voxels tested (the connectome mask size).
    :param note: optional word shown after N in the banner, e.g. "partial" or "preview".
    :return: :class:`Resolved`.
    """
    rule = normalize_rule(rule)
    n, v = int(n), int(v)
    if v < 1:
        raise ValueError(f"The number of voxels must be at least 1, got {v}")
    kind = rule["kind"]
    df = max(n - 1, 0)
    if kind == "fz_abs":
        # An effect-size threshold on the mean Fz map: no p-value, valid for any N >= 1
        return Resolved("Fz", rule["value"], df, None, None, None if n >= 1 else "The map has no subjects.", n >= 1, n, v, rule)
    if n < 2:
        return Resolved("T", math.inf, df, None, None, "A T map needs at least 2 subjects.", False, n, v, rule)
    if kind == "fwe_bonferroni":
        from scipy import stats as scipy_stats

        p_unc = rule["alpha"] / v
        value = float(scipy_stats.t.isf(p_unc / 2.0, df))
        return Resolved("T", value, df, p_unc, rule["alpha"], None, True, n, v, rule)
    value = rule["value"]
    p_unc = _two_sided_p(value, df)
    p_fwe = min(1.0, p_unc * v)
    banner = _reference_banner(rule.get("reference_n"), n, note)
    allowed = True
    if kind == "t_convention" and n < T_CONVENTION_MIN_N:
        allowed = bool(rule.get("override", False))
        if banner is None:
            banner = _reference_banner(rule["reference_n"] or 1000, n, note) or f"The |T| ≥ {value:g} convention needs N ≥ {T_CONVENTION_MIN_N}; this map has N={n}. Use FWE or effect size."
    return Resolved("T", value, df, p_unc, p_fwe, banner, allowed, n, v, rule)


def format_p(p):
    """Format a p-value compactly: 1.7e-7, 4.7e-12 or 0.012."""
    if p is None or not math.isfinite(p):
        return "n/a"
    if p >= 1e-3:
        return f"{p:.2g}"
    mantissa, exponent = f"{p:.1e}".split("e")
    return f"{mantissa}e{int(exponent)}"


def describe(resolved):
    """The derived line shown under the threshold controls, e.g.
    "⇒ |T| ≥ 5.27 at N=1000 (df=999), V=292,019 · p_unc ≤ 1.7e-7".
    """
    if resolved.statistic == "Fz":
        return f"⇒ |Fz| ≥ {resolved.value:g} at N={resolved.n}, V={resolved.v:,} · effect size (no p-value)"
    if not math.isfinite(resolved.value):
        return f"⇒ no T threshold at N={resolved.n} (at least 2 subjects are needed)"
    return f"⇒ |T| ≥ {resolved.value:.2f} at N={resolved.n} (df={resolved.df}), V={resolved.v:,} · p_unc ≤ {format_p(resolved.p_unc)}"
