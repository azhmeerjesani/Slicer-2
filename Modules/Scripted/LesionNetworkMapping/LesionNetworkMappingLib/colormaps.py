"""Colour maps of lesion network maps (pure Python: no numpy, no Slicer).

``lnm_diverging(n)`` is the diverging map of SPEC 4.12 used for T, Fz and r maps: entry 0 is the most
negative value (-W) and entry n - 1 the most positive (+W) of a symmetric window. The negative half
runs from #2166AC next to zero to #7FE3F0 at -W, the positive half from #B2182B next to zero to
#FDDB54 at +W, so that stronger values are brighter in both tails. The Map module builds its
"LNM Diverging" colour table node from it (``display.ensureLUT``); the figure renderer reuses it.
"""

#: Colours of the diverging map (SPEC 4.12): (next to zero, at the end of the window)
NEGATIVE_COLORS = ("#2166AC", "#7FE3F0")
POSITIVE_COLORS = ("#B2182B", "#FDDB54")

#: Default number of entries (the alpha of each entry is rewritten when the threshold changes)
DEFAULT_ENTRIES = 256


def hex_to_rgb(text):
    """(r, g, b) in 0..1 of a "#RRGGBB" colour."""
    text = text.lstrip("#")
    if len(text) != 6:
        raise ValueError(f"Not a #RRGGBB colour: {text!r}")
    return tuple(int(text[index:index + 2], 16) / 255.0 for index in (0, 2, 4))


def _mix(a, b, t):
    return tuple(a[index] + (b[index] - a[index]) * t for index in range(3))


def lnm_diverging(n=DEFAULT_ENTRIES):
    """The diverging colour map as a list of ``n`` (r, g, b) tuples in 0..1 (``n`` even, at least 4).

    Entries 0 .. n/2 - 1 cover [-W, 0) and n/2 .. n - 1 cover [0, +W]; within each half the colour moves
    linearly from the end of the window towards zero (negative half) or from zero towards the end
    (positive half).
    """
    n = int(n)
    if n < 4 or n % 2:
        raise ValueError(f"The number of entries must be even and at least 4, got {n}")
    half = n // 2
    negativeNear, negativeFar = (hex_to_rgb(color) for color in NEGATIVE_COLORS)
    positiveNear, positiveFar = (hex_to_rgb(color) for color in POSITIVE_COLORS)
    colors = []
    for index in range(half):  # -W ... 0: far (bright) to near (dark)
        colors.append(_mix(negativeFar, negativeNear, index / (half - 1)))
    for index in range(half):  # 0 ... +W: near (dark) to far (bright)
        colors.append(_mix(positiveNear, positiveFar, index / (half - 1)))
    return colors


def entry_values(n, window):
    """Map value at the centre of each of the ``n`` entries of a symmetric window [-window, +window]."""
    n = int(n)
    window = float(window)
    step = 2.0 * window / n
    return [-window + (index + 0.5) * step for index in range(n)]


def entry_alphas(n, window, threshold, sign="both"):
    """Alpha (0 or 1) of each entry: 1 when the entry's value passes the threshold.

    :param sign: "both" (|value| >= threshold), "pos" (value >= threshold) or "neg" (value <= -threshold).
    A threshold of 0 or less shows every entry of the selected sign(s).
    """
    if sign not in ("both", "pos", "neg"):
        raise ValueError(f"sign must be both, pos or neg, got {sign!r}")
    threshold = float(threshold)
    alphas = []
    for value in entry_values(n, window):
        if sign == "pos":
            passes = value >= threshold and value > 0
        elif sign == "neg":
            passes = -value >= threshold and value < 0
        else:
            passes = abs(value) >= threshold
        alphas.append(1.0 if passes else 0.0)
    return alphas
