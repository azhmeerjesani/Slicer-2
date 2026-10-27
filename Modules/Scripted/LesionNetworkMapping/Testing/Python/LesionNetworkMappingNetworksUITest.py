"""Tests of the Networks card's Yeo-7 / Yeo-17 control (card_networks.py).

Qt 6 has no ``QButtonGroup.buttonClicked(int)`` signal (only ``idClicked(int)``, which exists since Qt 5.15), so a card
that listens to ``buttonClicked(int)`` never learns about a click on the macOS disk image. On Qt 5 the test removes the
``buttonClicked(int)`` connections first, which is what Qt 6 amounts to, then clicks Yeo-17.

They need the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingNetworksUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import unittest

try:
    import qt
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class LesionNetworkMappingNetworksUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from LesionNetworkMappingUI import card_networks

        cls.cn = card_networks
        settings = qt.QSettings()
        cls.storedScheme = settings.value(card_networks.SCHEME_SETTING)
        slicer.util.selectModule("LesionNetworkMapping")
        slicer.app.processEvents()
        cls.card = slicer.modules.LesionNetworkMappingWidget.ctx.card("networks")

    @classmethod
    def tearDownClass(cls):
        settings = qt.QSettings()
        if cls.storedScheme is None:
            settings.remove(cls.cn.SCHEME_SETTING)
        else:
            settings.setValue(cls.cn.SCHEME_SETTING, cls.storedScheme)

    def test_scheme_click_without_buttonClicked_int(self):
        card = self.card
        self.assertIsNotNone(card)
        group = card.schemeGroup
        self.assertIsNotNone(slicer.util.findChild(card.widget, "LNMNetworksSchemeSegmented"))
        card.setScheme("yeo7")
        self.assertEqual(card.scheme, "yeo7")
        if not hasattr(group, "idClicked"):
            self.skipTest("Qt before 5.15: no idClicked(int)")
        if hasattr(group, "buttonClicked") and qt.qVersion().startswith("5."):
            group.disconnect("buttonClicked(int)")  # what Qt 6 amounts to: that signal does not exist there
        yeo17 = list(self.cn.SCHEMES).index("yeo17")
        group.button(yeo17).click()
        slicer.app.processEvents()
        self.assertEqual(group.checkedId(), yeo17)
        self.assertEqual(card.scheme, "yeo17")
        self.assertEqual(qt.QSettings().value(self.cn.SCHEME_SETTING), "yeo17")
        group.button(list(self.cn.SCHEMES).index("yeo7")).click()
        slicer.app.processEvents()
        self.assertEqual(card.scheme, "yeo7")


if __name__ == "__main__":
    unittest.main()
