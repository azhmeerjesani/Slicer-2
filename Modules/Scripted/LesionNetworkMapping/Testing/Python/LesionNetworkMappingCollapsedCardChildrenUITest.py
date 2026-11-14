"""Test of the children of a collapsed Map card (bug map-collapsed-qa-card-shows-cut-null-chip-before-a-map).

``ctk.ctkCollapsibleButton`` hides the children of a collapsed card only when the card is created through its own
``setVisible()``. A card created because its parent is shown never got that call, so a child shown while the card was still
hidden (the QA card's chips, the banners of Clusters and Networks) stayed visible in the collapsed card: the top of a chip
showed under the header, cut by the card edge.

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingCollapsedCardChildrenUITest
"""

import unittest

try:
    import qt
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


def _pump(count=10):
    for _ in range(count):
        slicer.app.processEvents()


class CollapsedCardChildrenUITest(unittest.TestCase):
    def test_child_shown_before_creation_is_hidden_in_collapsed_card(self):
        from LesionNetworkMappingUI import widgets

        window = qt.QWidget()
        layout = qt.QVBoxLayout(window)
        collapsedCard = widgets.card("Collapsed", "LNMTestCollapsed", collapsed=True)
        openCard = widgets.card("Open", "LNMTestOpen", collapsed=False)
        children = []
        for cardButton in (collapsedCard, openCard):
            body = qt.QVBoxLayout(cardButton)
            label = qt.QLabel("child")
            body.addWidget(label)
            label.setVisible(True)  # shown while the card is not created yet
            children.append(label)
            layout.addWidget(cardButton)
        try:
            window.show()
            _pump()
            self.assertFalse(children[0].isVisible(), "a collapsed card shows only its header")
            self.assertTrue(children[1].isVisible(), "an expanded card shows its children")
            collapsedCard.collapsed = False
            _pump()
            self.assertTrue(children[0].isVisible(), "expanding shows the child again")
            collapsedCard.collapsed = True
            _pump()
            self.assertFalse(children[0].isVisible())
        finally:
            window.hide()
            window.deleteLater()


    def test_map_cards_collapsed_before_a_map_show_only_their_header(self):
        """The Map module in a normal application start (the test application does not show the bug): child process."""
        import json
        import os
        import shutil
        import subprocess
        import tempfile

        script = (
            "import json, os, time, qt, slicer\n"
            "def pump(ms):\n"
            "    end = time.monotonic() + ms / 1000\n"
            "    while time.monotonic() < end:\n"
            "        slicer.app.processEvents(); time.sleep(0.01)\n"
            "result = {}\n"
            "try:\n"
            "    slicer.util.selectModule('LesionNetworkMapping'); pump(1000)\n"
            "    mw = slicer.util.mainWindow(); mw.resize(1920, 1080); pump(1500)\n"
            "    widget = slicer.modules.LesionNetworkMappingWidget\n"
            "    for key in ('qa', 'clusters', 'networks', 'circuits', 'compare'):\n"
            "        card = widget.ctx.card(key); card = getattr(card, 'widget', card)\n"
            "        shown = [c.objectName for c in card.findChildren(qt.QWidget) if c.parent() == card and c.isVisible()]\n"
            "        result[key] = [bool(card.collapsed), shown]\n"
            "except Exception as exc:\n"
            "    result['error'] = repr(exc)\n"
            "open(os.environ['LNM_TEST_OUT'], 'w').write(json.dumps(result))\n"
            "slicer.app.exit(0)\n"
        )
        folder = tempfile.mkdtemp(prefix="lnm-collapsed-")
        try:
            scriptPath = os.path.join(folder, "check.py")
            with open(scriptPath, "w") as handle:
                handle.write(script)
            os.makedirs(os.path.join(folder, "tmp"))
            env = dict(os.environ, HOME=folder, TMPDIR=os.path.join(folder, "tmp"), LNM_TEST_OUT=os.path.join(folder, "out.json"))
            env["SLICER_STUDIO_PROJECTS_ROOT"] = os.path.join(folder, "projects")
            launcher = slicer.app.launcherExecutableFilePath
            subprocess.run([launcher, "--no-splash", "--python-script", scriptPath], env=env, timeout=240, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            with open(env["LNM_TEST_OUT"]) as handle:
                result = json.load(handle)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        self.assertNotIn("error", result, result)
        checked = 0
        for key, (collapsed, shown) in result.items():
            if collapsed:
                checked += 1
                self.assertEqual(shown, [], f"collapsed {key} card shows only its header")
        self.assertGreater(checked, 0)


if __name__ == "__main__":
    unittest.main()
