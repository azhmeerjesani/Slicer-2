"""Test of the size texts of the connectome manager, the "Get a connectome" dialog and the Map module (bug
byte-sizes-exponent-notation).

A size between 999.5 and 1000 of a unit (for example 999.7 GB free on a 1 TB disk) printed as "1e+03 GB": three
significant digits switch to exponent notation there. It is now stated in the next larger unit ("1 TB"). The three
formatters (``manager.format_bytes``, ``catalog_dialog.formatBytes`` and ``LesionNetworkMapping._formatBytes``) agree.

The library function needs no application; the other two are checked when the Slicer application is present::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingByteSizesTest
"""

import unittest

CASES = {
    999_700_000_000: "1 TB",
    999_600: "1 MB",
    999_999_999: "1 GB",
    999_499_999_999: "999 GB",
    999_500_000_000: "1 TB",
    1_000_000_000_000: "1 TB",
    1_500: "1.5 kB",
    236_000_000_000: "236 GB",
    1_234_000_000: "1.23 GB",
    9_000: "9 kB",
    999: "999 B",
    0: "0 B",
    999_700_000_000_000: "1e+03 TB",
}


def _expected(count):
    # beyond the largest unit no larger one exists: still no exponent notation
    return "1000 TB" if count == 999_700_000_000_000 else CASES[count]


class LesionNetworkMappingByteSizesTest(unittest.TestCase):
    def checkFormatter(self, function):
        for count in CASES:
            text = function(count)
            self.assertEqual(text, _expected(count), count)
            self.assertNotIn("e+", text, count)

    def test_library_formatter(self):
        from LesionNetworkMappingLib import manager

        self.checkFormatter(manager.format_bytes)
        self.checkFormatter(manager._format_bytes)

    def test_catalog_dialog_formatter(self):
        try:
            import slicer
        except ImportError as exc:
            raise unittest.SkipTest("needs the Slicer application") from exc
        if getattr(slicer, "app", None) is None:
            raise unittest.SkipTest("needs the Slicer application")
        from LesionNetworkMappingUI import catalog_dialog

        self.checkFormatter(catalog_dialog.formatBytes)
        self.assertEqual(catalog_dialog.formatBytes(None), "—")

    def test_module_formatter(self):
        try:
            import slicer
        except ImportError as exc:
            raise unittest.SkipTest("needs the Slicer application") from exc
        if getattr(slicer, "app", None) is None:
            raise unittest.SkipTest("needs the Slicer application")
        slicer.util.selectModule("LesionNetworkMapping")
        import LesionNetworkMapping

        self.checkFormatter(LesionNetworkMapping._formatBytes)


if __name__ == "__main__":
    unittest.main()
