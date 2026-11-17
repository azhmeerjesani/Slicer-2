"""A study with one lesion is refused by the Nulls card before anything is mapped (BB-1).

The null models need at least 2 lesions. The card only checked the mapped count, so with unmapped lesions Run stayed enabled
("Run maps the lesions first"), mapped the single lesion and only then refused (bug
map-nulls-card-numerics:nulls-one-lesion-study-maps-first-then-refused). The study's lesion count is known from the cohort
info of the Map module: Run is refused at once. The status line also says "1 lesion", not "1 lesions".

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingNullsOneLesionTest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class _Widget:
    def __init__(self, info):
        self.cohortInfo = info
        self.cohortJob = None

    def cohortComputeState(self):
        return True, ""


class _Ctx:
    study = object()
    bundle = object()

    def computeEnabled(self):
        return True


class LesionNetworkMappingNullsOneLesionTest(unittest.TestCase):
    def card(self, info, mapped=None):
        from LesionNetworkMappingUI import card_nulls

        card = card_nulls.NullsCard.__new__(card_nulls.NullsCard)
        card.ctx = _Ctx()
        card.job = card.mapsJob = None
        card.kind, card.mode = "rl_cohort_shift", "exact"
        card.controlSet = None
        card.fast = type("Fast", (), {"enabled": False, "reason": ""})()
        card.kValue = lambda: 1000
        card.hasReference = lambda: True
        card.currentMapsTsv = lambda: None
        self.widget = _Widget(info)
        self._saved = card_nulls._mapWidget
        card_nulls._mapWidget = lambda: self.widget
        self.addCleanup(setattr, card_nulls, "_mapWidget", self._saved)
        return card

    def test_one_lesion_is_refused_before_mapping(self):
        card = self.card({"n": 1, "groups": {}})
        enabled, reason = card.runState()
        self.assertFalse(enabled)
        self.assertEqual(reason, "The null models need at least 2 lesions.")

    def test_two_lesions_still_map_first(self):
        card = self.card({"n": 2, "groups": {}})
        enabled, reason = card.runState()
        self.assertTrue(enabled)
        self.assertIn("maps the lesions first", reason)

    def test_one_case_among_controls_is_refused_before_mapping(self):
        # nulls.load_cohort keeps only the cases, so 1 case + 4 controls is 1 lesion for the nulls
        card = self.card({"n": 5, "groups": {"case": 1, "control": 4}})
        self.assertEqual(card.studyLesionCount(), 1)
        enabled, reason = card.runState()
        self.assertFalse(enabled)
        self.assertEqual(reason, "The null models need at least 2 lesions.")

    def test_two_cases_among_controls_count_the_cases(self):
        card = self.card({"n": 6, "groups": {"case": 2, "control": 4}})
        self.assertEqual(card.studyLesionCount(), 2)
        self.assertTrue(card.runState()[0])


if __name__ == "__main__":
    unittest.main()
