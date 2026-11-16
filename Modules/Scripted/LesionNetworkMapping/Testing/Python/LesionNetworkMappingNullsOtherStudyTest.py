"""Opening another study takes the previous study's nulls map out of the slice views (BB-1).

The Nulls card dropped its result when another study (or connectome) was opened, but left the "Nulls Excess" map of the
previous study as the foreground of every slice view, where it read as a result of the study open now. A run that
finished after another study was opened also said "the maps are in the Nulls card", which then showed the other study
(bug map-nulls-card-numerics:nulls-map-of-other-study-stays-in-views). The Cohort card does the same for its group map
(``hideGroupMapOfOtherScope``).

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingNullsOtherStudyTest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import os
import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class _Ctx:
    bundle = None


class _Job:
    state = "done"

    @staticmethod
    def succeeded():
        return True


class LesionNetworkMappingNullsOtherStudyTest(unittest.TestCase):
    def setUp(self):
        from LesionNetworkMappingUI import card_nulls

        slicer.mrmlScene.Clear(0)
        self.addCleanup(slicer.mrmlScene.Clear, 0)
        saved = card_nulls._updateProgress
        card_nulls._updateProgress = lambda *args, **kwargs: None
        self.addCleanup(setattr, card_nulls, "_updateProgress", saved)

    def card(self, mapsFolder):
        from LesionNetworkMappingUI import card_nulls

        card = card_nulls.NullsCard.__new__(card_nulls.NullsCard)
        card.ctx = _Ctx()
        card.job = card.mapsJob = None
        card.kind, card.mode = "rl_independent", "exact"
        card.result = None
        card.notice = ""
        card.volumeId = None
        card.mapScope = None
        card._scope = None
        card._cleanedUp = False
        card._updating = False
        card._timer = type("Timer", (), {"stop": lambda self: None})()
        card.progress = None
        card.estimateLabel = type("Label", (), {"hide": lambda self: None})()
        card.folder = mapsFolder
        card.currentMapsTsv = lambda: os.path.join(card.folder, "maps.tsv")
        card.mapsFolder = lambda: card.folder
        card.running = lambda: True  # keeps refresh away from the progress row
        for name in ("showKinds", "showMode", "showSettings", "showState", "showResult", "showPlanPending", "pushGroupQa"):
            setattr(card, name, lambda *args, **kwargs: None)
        card.toasts = []
        card.message = lambda text, ms=3000: card.toasts.append(text)
        card.refresh()
        return card

    def shownMap(self, card):
        """A nulls map volume in the foreground of every slice view, as NullsCard.showMap leaves it."""
        from LesionNetworkMappingUI import card_nulls

        volume = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "Nulls Excess (rl_independent, exact pooled, K=19)")
        volume.SetAttribute(card_nulls.VOLUME_ATTRIBUTE, "1")
        card.volumeId = volume.GetID()
        card.result = ("unused", {})
        slicer.util.setSliceViewerLayers(foreground=volume, foregroundOpacity=0.8)
        card.mapScope = card._scope  # what showMap records
        return volume

    @staticmethod
    def foregrounds():
        return [node.GetForegroundVolumeID() for node in slicer.util.getNodesByClass("vtkMRMLSliceCompositeNode")]

    def test_other_study_takes_the_map_out_of_the_views(self):
        card = self.card("/projects/Study2/conn-x/cohort")
        volume = self.shownMap(card)
        self.assertIn(volume.GetID(), self.foregrounds())
        card.refresh()  # the same study: the map stays
        self.assertIn(volume.GetID(), self.foregrounds())
        card.folder = "/projects/Study1L/conn-x/cohort"
        card.refresh()
        self.assertIsNone(card.result)
        self.assertNotIn(volume.GetID(), self.foregrounds())
        self.assertIsNotNone(slicer.mrmlScene.GetNodeByID(volume.GetID()))  # only out of the views, not deleted

    def test_map_of_another_card_session_is_left_alone(self):
        # a map that this card did not put in the views (a restored scene) is not taken out by the first refresh
        card = self.card("/projects/Study2/conn-x/cohort")
        volume = self.shownMap(card)
        card.mapScope = None
        card.folder = "/projects/Study1L/conn-x/cohort"
        card.refresh()
        self.assertIn(volume.GetID(), self.foregrounds())

    def test_run_of_the_open_study_says_the_maps_are_in_the_card(self):
        card = self.card("/projects/Study2/conn-x/cohort")
        card.showMap = lambda desc: None
        card.onDone(_Job(), os.path.join(card.folder, "nulls-rl_independent-exact"))
        self.assertEqual(card.toasts, ["Null models done: the maps are in the Nulls card"])

    def test_run_of_another_study_does_not_point_to_the_card(self):
        card = self.card("/projects/Study2/conn-x/cohort")
        card.showMap = lambda desc: self.fail("the map of another study is shown")
        folder = os.path.join(card.folder, "nulls-rl_independent-exact")
        card.folder = "/projects/Study1L/conn-x/cohort"
        card.onDone(_Job(), folder)
        self.assertEqual(len(card.toasts), 1)
        self.assertNotIn("the maps are in the Nulls card", card.toasts[0])
        self.assertIn("another study", card.toasts[0])


if __name__ == "__main__":
    unittest.main()
