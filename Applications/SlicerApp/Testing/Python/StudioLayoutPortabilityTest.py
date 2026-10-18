import os
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET

import slicer

CONVENTIONAL_LAYOUT = 2  # vtkMRMLLayoutNode.SlicerLayoutConventionalView
FOUR_UP_LAYOUT = 3  # vtkMRMLLayoutNode.SlicerLayoutFourUpView

# Canonical descriptions of the Slicer Studio layouts. They must stay byte-identical with the ones
# registered by vtkMRMLLayoutLogic and by Studio on builds that do not have them.
_RED = (
    '<view class="vtkMRMLSliceNode" singletontag="Red"><property name="orientation" action="default">Axial</property>'
    '<property name="viewlabel" action="default">R</property><property name="viewcolor" action="default">#F34A33</property></view>'
)
_GREEN = (
    '<view class="vtkMRMLSliceNode" singletontag="Green"><property name="orientation" action="default">Coronal</property>'
    '<property name="viewlabel" action="default">G</property><property name="viewcolor" action="default">#6EB04B</property></view>'
)
_YELLOW = (
    '<view class="vtkMRMLSliceNode" singletontag="Yellow"><property name="orientation" action="default">Sagittal</property>'
    '<property name="viewlabel" action="default">Y</property><property name="viewcolor" action="default">#EDD54C</property></view>'
)
_VIEW_1 = '<view class="vtkMRMLViewNode" singletontag="1"><property name="viewlabel" action="default">1</property></view>'
STUDIO_LAYOUTS = {
    7001: (
        f'<layout type="horizontal" split="true"><item splitSize="720">{_RED}</item><item splitSize="280">'
        f'<layout type="vertical" split="true"><item>{_GREEN}</item><item>{_YELLOW}</item></layout></item></layout>'
    ),
    7002: (
        f'<layout type="horizontal" split="true"><item splitSize="720">{_RED}</item><item splitSize="280">'
        f'<layout type="vertical" split="true"><item>{_GREEN}</item><item>{_YELLOW}</item><item>{_VIEW_1}</item></layout></item></layout>'
    ),
    7003: f'<layout type="horizontal" split="true"><item>{_RED}</item><item>{_GREEN}</item><item>{_YELLOW}</item></layout>',
}

LAYOUT_ELEMENT = (
    '<Layout id="vtkMRMLLayoutNodevtkMRMLLayoutNode" name="Layout" singletonTag="vtkMRMLLayoutNode" {attributes}'
    ' guiPanelVisibility="1" bottomPanelVisibility ="1" guiPanelLR="0" collapseSliceControllers="0"'
    ' numberOfCompareViewRows="1" numberOfCompareViewColumns="1" mainPanelSize="400" secondaryPanelSize="400" ></Layout>'
)


class StudioLayoutPortabilityTest(unittest.TestCase):
    """Test that the Slicer Studio layouts 7001-7003 exist and that scenes are saved with portable layouts.

    A layout in the range 7001-7099 that the fork reserves is saved as currentViewArrangement="2"
    (the conventional layout, which every Slicer version knows) plus studioViewArrangement="<id>",
    which other Slicer versions ignore. The fork restores the layout from studioViewArrangement.
    """

    def setUp(self):
        self.tempDir = tempfile.mkdtemp(prefix="StudioLayoutPortabilityTest")
        slicer.mrmlScene.Clear(0)
        layoutManager = slicer.app.layoutManager()
        self.originalLayout = layoutManager.layout if layoutManager else None

    def tearDown(self):
        slicer.mrmlScene.Clear(0)
        if self.originalLayout is not None:
            slicer.app.layoutManager().setLayout(self.originalLayout)
        shutil.rmtree(self.tempDir, ignore_errors=True)

    def _standaloneLayout(self):
        """Return a new scene and its layout logic and layout node, independent of the application."""
        scene = slicer.vtkMRMLScene()
        layoutLogic = slicer.vtkMRMLLayoutLogic()
        layoutLogic.SetMRMLScene(scene)
        return scene, layoutLogic, layoutLogic.GetLayoutNode()

    @staticmethod
    def _sceneLayoutAttributes(scene):
        scene.SetSaveToXMLString(1)
        scene.Commit()
        scene.SetSaveToXMLString(0)
        return ET.fromstring(scene.GetSceneXMLString()).find("Layout").attrib

    @staticmethod
    def _fileLayoutAttributes(mrmlPath):
        return ET.parse(mrmlPath).getroot().find("Layout").attrib

    @staticmethod
    def _importLayout(attributes):
        """Import a scene whose layout node has the given XML attributes and return its view arrangement."""
        scene = slicer.vtkMRMLScene()
        scene.SetLoadFromXMLString(1)
        scene.SetSceneXMLString(f'<MRML version="Slicer 5.13.0" userTags="">{LAYOUT_ELEMENT.format(attributes=attributes)}</MRML>')
        scene.Import()
        return scene.GetFirstNodeByClass("vtkMRMLLayoutNode").GetViewArrangement()

    def _requireLayoutManager(self):
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            self.skipTest("requires the main window")
        return layoutManager

    def _assertSliceViewsShown(self, layoutManager, names):
        slicer.app.processEvents()
        viewport = layoutManager.viewport()
        for name in names:
            sliceWidget = layoutManager.sliceWidget(name)
            self.assertIsNotNone(sliceWidget, f"slice view {name} does not exist")
            self.assertTrue(sliceWidget.isVisibleTo(viewport), f"slice view {name} is not shown")

    def test_StudioLayoutsRegisteredInCpp(self):
        _, _, layoutNode = self._standaloneLayout()
        for layoutId, description in STUDIO_LAYOUTS.items():
            self.assertTrue(layoutNode.IsLayoutDescription(layoutId), f"layout {layoutId} is not registered")
            self.assertEqual(layoutNode.GetLayoutDescription(layoutId), description, f"layout {layoutId} is not canonical")
        # Built-in layouts are unchanged and the rest of the reserved range is free for user layouts
        self.assertTrue(layoutNode.IsLayoutDescription(CONVENTIONAL_LAYOUT))
        self.assertFalse(layoutNode.IsLayoutDescription(7004))
        layoutManager = slicer.app.layoutManager()
        if layoutManager is not None:
            applicationLayoutNode = layoutManager.layoutLogic().GetLayoutNode()
            for layoutId, description in STUDIO_LAYOUTS.items():
                self.assertEqual(applicationLayoutNode.GetLayoutDescription(layoutId), description)

    def test_WriteXMLPortableFallback(self):
        scene, _, layoutNode = self._standaloneLayout()
        for layoutId in (7000, 7099, 7100):  # descriptions for the IDs that are not registered by default
            layoutNode.AddLayoutDescription(layoutId, STUDIO_LAYOUTS[7003])
        for layoutId in (7001, 7002, 7003, 7099):
            layoutNode.SetViewArrangement(layoutId)
            attributes = self._sceneLayoutAttributes(scene)
            self.assertEqual(attributes.get("currentViewArrangement"), str(CONVENTIONAL_LAYOUT), f"layout {layoutId}")
            self.assertEqual(attributes.get("studioViewArrangement"), str(layoutId))
            # Saving does not change the layout of the scene
            self.assertEqual(layoutNode.GetViewArrangement(), layoutId)
        for layoutId in (CONVENTIONAL_LAYOUT, FOUR_UP_LAYOUT, 7000, 7100):
            layoutNode.SetViewArrangement(layoutId)
            attributes = self._sceneLayoutAttributes(scene)
            self.assertEqual(attributes.get("currentViewArrangement"), str(layoutId))
            self.assertNotIn("studioViewArrangement", attributes)

    def test_ReadXMLAttributes(self):
        self.assertEqual(self._importLayout('currentViewArrangement="2" studioViewArrangement="7002"'), 7002)
        # The attribute order does not matter
        self.assertEqual(self._importLayout('studioViewArrangement="7003" currentViewArrangement="2"'), 7003)
        # Scenes written by other Slicer versions
        self.assertEqual(self._importLayout('currentViewArrangement="2"'), CONVENTIONAL_LAYOUT)
        self.assertEqual(self._importLayout('currentViewArrangement="3"'), FOUR_UP_LAYOUT)
        # An invalid studioViewArrangement is ignored
        self.assertEqual(self._importLayout('currentViewArrangement="2" studioViewArrangement="invalid"'), CONVENTIONAL_LAYOUT)
        self.assertEqual(self._importLayout('currentViewArrangement="2" studioViewArrangement="-5"'), CONVENTIONAL_LAYOUT)

    def test_CopyKeepsLayout(self):
        _, _, layoutNode = self._standaloneLayout()
        layoutNode.SetViewArrangement(7001)
        copiedNode = slicer.vtkMRMLLayoutNode()
        copiedNode.Copy(layoutNode)
        self.assertEqual(copiedNode.GetViewArrangement(), 7001)

    def test_SaveAndReloadStudioLayout(self):
        layoutManager = self._requireLayoutManager()
        for extension in (".mrml", ".mrb"):
            with self.subTest(extension=extension):
                slicer.mrmlScene.Clear(0)
                layoutManager.setLayout(7001)
                slicer.app.processEvents()
                self.assertEqual(layoutManager.layout, 7001)
                scenePath = os.path.join(self.tempDir, f"StudioLayout{extension}")
                self.assertTrue(slicer.util.saveScene(scenePath))
                if extension == ".mrml":
                    attributes = self._fileLayoutAttributes(scenePath)
                    self.assertEqual(attributes.get("currentViewArrangement"), str(CONVENTIONAL_LAYOUT))
                    self.assertEqual(attributes.get("studioViewArrangement"), "7001")

                slicer.mrmlScene.Clear(0)
                layoutManager.setLayout(FOUR_UP_LAYOUT)
                slicer.app.processEvents()
                slicer.util.loadScene(scenePath)
                slicer.app.processEvents()
                self.assertEqual(layoutManager.layout, 7001)
                self._assertSliceViewsShown(layoutManager, ("Red", "Green", "Yellow"))

    def test_SaveAndReloadOtherLayouts(self):
        layoutManager = self._requireLayoutManager()
        for layoutId in (CONVENTIONAL_LAYOUT, FOUR_UP_LAYOUT):
            with self.subTest(layout=layoutId):
                slicer.mrmlScene.Clear(0)
                layoutManager.setLayout(layoutId)
                slicer.app.processEvents()
                scenePath = os.path.join(self.tempDir, f"Layout{layoutId}.mrml")
                self.assertTrue(slicer.util.saveScene(scenePath))
                attributes = self._fileLayoutAttributes(scenePath)
                self.assertEqual(attributes.get("currentViewArrangement"), str(layoutId))
                self.assertNotIn("studioViewArrangement", attributes)

                slicer.mrmlScene.Clear(0)
                layoutManager.setLayout(7003)
                slicer.app.processEvents()
                slicer.util.loadScene(scenePath)
                slicer.app.processEvents()
                self.assertEqual(layoutManager.layout, layoutId)
                self._assertSliceViewsShown(layoutManager, ("Red", "Green", "Yellow"))
