"""Test of the names of the sign buttons (bug map-sign-buttons-no-accessible-name).

The Result card's sign switch (``LNMSignSegmented``) and the sign switch of the Overlap tab have the glyph buttons "±", "+"
and the minus sign (QToolButtons without an icon). They had no accessible name and no tooltip of their own, so a screen reader said
"plus" and "minus" (USABILITY 10: every control has a tooltip; the other icon-only Studio buttons carry a name). Each
button now has an accessible name ("Positive tail"...) and a "Name · description" tooltip.

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingSignButtonNamesTest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import unittest

try:
    import qt  # noqa: F401
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")

NAMES = ["Both tails", "Positive tail", "Negative tail"]


class SignButtonNamesTest(unittest.TestCase):
    def _check(self, group):
        buttons = group.buttons()
        self.assertEqual(len(buttons), 3)
        for button, name in zip(buttons, NAMES, strict=True):
            self.assertEqual(str(button.accessibleName), name, f"accessible name of {button.text!r}")
            tip = str(button.toolTip)  # Qt may wrap it as rich text
            self.assertIn(name + " · ", tip, f"tooltip of {button.text!r}: {tip!r}")

    def test_helper_names_buttons(self):
        from LesionNetworkMappingUI import widgets

        frame, group = widgets.segmented(["a", "b"])
        widgets.nameSegmentedButtons(group, [("Alpha", "first"), ("Beta", "second")])
        self.assertEqual([str(b.accessibleName) for b in group.buttons()], ["Alpha", "Beta"])
        self.assertIn("Beta · second", str(group.button(1).toolTip))
        frame.delete()

    def test_result_card_sign_buttons(self):
        slicer.util.selectModule("LesionNetworkMapping")
        slicer.app.processEvents()
        widget = slicer.modules.LesionNetworkMappingWidget
        self._check(widget.signGroup)

    def test_overlap_sign_buttons(self):
        slicer.util.selectModule("LesionNetworkMapping")
        slicer.app.processEvents()
        widget = slicer.modules.LesionNetworkMappingWidget
        groups = [g for g in (getattr(c, "signGroup", None) for c in vars(widget).values()) if g is not None and g is not widget.signGroup]
        if not groups:
            self.skipTest("the Overlap tab is not built here")
        for group in groups:
            self._check(group)
