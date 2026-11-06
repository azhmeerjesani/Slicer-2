"""Test that the Robust card's Influence "Show" link opens the subject in the Library (L-37) for a study chosen in Map.

Map keeps a study of its own (Seed > Cohort > Open study..., remembered in ``Studio/LNM/CohortStudy`` and reopened after
a restart without the Library). "Show" only switched to the Library and set its selection: with no study open there, the
Library showed its Projects page and the subject was nowhere on screen, while the link reported success (bug
map-robust-card:robust-influence-show-does-nothing-map-study). The study is now opened in the Library first; a study
the Library already has open is kept (its session is not replaced). The preflight card's "Show in Library" shares the
same helper (``widgets.showSubjectsInLibrary``).

They need the Slicer application and use the synthetic study of ``lnm_pipeline_fixture.py`` (no network)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingRobustShowUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import os
import shutil
import sys
import tempfile
import time
import unittest

try:
    import qt
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
if TEST_DIR not in sys.path:
    sys.path.insert(0, TEST_DIR)


def _pump(ms):
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        slicer.app.processEvents()
        time.sleep(0.005)


def _samePath(a, b):
    return bool(a) and bool(b) and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


class LesionNetworkMappingRobustShowUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import lnm_pipeline_fixture as fixture

        from LesionNetworkMapping import COHORT_STUDY_SETTING
        from LesionNetworkMappingUI import card_pipeline
        from StudioLib import library, study_ui

        cls.library, cls.study_ui = library, study_ui
        cls.setting = COHORT_STUDY_SETTING
        cls.tmp = tempfile.mkdtemp(prefix="lnm robust show ")
        cls.fx = fixture.make_fixture(os.path.join(cls.tmp, "f1"), extra_rows=False)
        study_ui.closeStudy()
        slicer.util.selectModule("LesionNetworkMapping")
        cls.widget = slicer.modules.LesionNetworkMappingWidget
        # The study is chosen in Map's Seed card only (as "Open study..." or the remembered study after a restart)
        cls.widget.openCohortStudy(cls.fx["study"])
        card_pipeline.setCohortSeed(cls.widget)
        cls.ctx = cls.widget.ctx
        cls.robust = cls.ctx.card("robust")
        cls.preflight = cls.ctx.card("preflight")
        _pump(100)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.study_ui.closeStudy()
        except Exception:
            pass
        qt.QSettings().remove(cls.setting)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.study_ui.closeStudy()
        self.library._setStudySelection([])
        _pump(50)
        self.assertIsNone(self.study_ui.session())
        self.assertTrue(_samePath(getattr(self.ctx.study, "path", None), self.fx["study"]), "Map does not use the fixture study")

    def assertShown(self, sids):
        session = self.study_ui.session()
        self.assertIsNotNone(session, "the study was not opened in the Library")
        self.assertTrue(_samePath(session.study.path, self.fx["study"]), f"the Library opened {session.study.path}")
        self.assertEqual(list(self.library._studySelection), list(sids))
        shell = self.ctx.shell()
        if shell is not None:
            self.assertEqual(shell.currentWorkspace(), "library")
        _pump(200)
        views = [view for view in self.library._studyViews if hasattr(view, "model")]
        self.assertTrue(views, "the Library shows no subject list")
        self.assertTrue(any(view.model.rowOf(sids[0]) >= 0 for view in views), f"{sids[0]} is not in the Library's subject list")

    def test_1_show_opens_the_map_study_in_the_library(self):
        sid = self.fx["sids"][3]
        self.assertTrue(self.robust.showSubject(sid))
        self.assertShown([sid])

    def test_2_show_keeps_the_study_the_library_has_open(self):
        session = self.study_ui.openStudy(self.fx["study"], showLibrary=False)
        sid = self.fx["sids"][1]
        self.assertTrue(self.robust.showSubject(sid))
        self.assertIs(self.study_ui.session(), session, "the Library's open study session was replaced")
        self.assertShown([sid])

    def test_3_preflight_show_in_library_opens_the_map_study(self):
        sids = self.fx["sids"][2:4]
        self.assertTrue(self.preflight.openLibrary(sids, "test"))
        self.assertShown(sids)


if __name__ == "__main__":
    unittest.main()
