/*==============================================================================

  Program: 3D Slicer

  Copyright (c) Kitware Inc.

  See COPYRIGHT.txt
  or http://www.slicer.org/copyright/copyright.txt for details.

  Unless required by applicable law or agreed to in writing, software
  distributed under the License is distributed on an "AS IS" BASIS,
  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
  See the License for the specific language governing permissions and
  limitations under the License.

  This file was originally developed by Jean-Christophe Fillion-Robin, Kitware Inc.
  and was partially funded by NIH grant 3P41RR013218-12S1

==============================================================================*/

#ifndef __qMRMLSliceControllerWidget_p_h
#define __qMRMLSliceControllerWidget_p_h

//
//  W A R N I N G
//  -------------
//
// This file is not part of the Slicer API.  It exists purely as an
// implementation detail.  This header file may change from version to
// version without notice, or even be removed.
//
// We mean it.
//

// Qt includes
#include <QIcon>
#include <QSizePolicy>

// CTK includes
#include <ctkPimpl.h>
#include <ctkVTKObject.h>

// qMRML includes
#include "qMRMLSliceControllerWidget.h"
#include "qMRMLViewControllerBar_p.h"
#include "ui_qMRMLSliceControllerWidget.h"

// MRMLLogic includes
#include <vtkMRMLSliceLogic.h>

// VTK includes
#include <vtkAlgorithmOutput.h>
#include <vtkCollection.h>
#include <vtkImageData.h>
#include <vtkSmartPointer.h>
#include <vtkWeakPointer.h>

class ctkDynamicSpacer;
class ctkSignalMapper;
class ctkDoubleSpinBox;
class ctkVTKSliceView;
class QActionGroup;
class QLabel;
class QSpinBox;
class qMRMLSliderWidget;
class vtkMRMLSliceNode;
class vtkObject;
class vtkMRMLSegmentationDisplayNode;
class vtkMRMLSelectionNode;

//-----------------------------------------------------------------------------
class QMRML_WIDGETS_EXPORT qMRMLSliceControllerWidgetPrivate
  : public qMRMLViewControllerBarPrivate
  , public Ui_qMRMLSliceControllerWidget
{
  Q_OBJECT
  QVTK_OBJECT
  Q_DECLARE_PUBLIC(qMRMLSliceControllerWidget);

public:
  typedef qMRMLSliceControllerWidgetPrivate Self;
  typedef qMRMLViewControllerBarPrivate Superclass;
  qMRMLSliceControllerWidgetPrivate(qMRMLSliceControllerWidget& object);
  ~qMRMLSliceControllerWidgetPrivate() override;

  void init() override;
  void setColor(QColor color) override;
  void updateFlatStyle() override;

  /// Create the widgets of the flat bar (orientation button, slice readout, link button).
  void setupFlatBar();
  /// Update the readout "S 96.0 mm · 97/193", the orientation button and the view label tooltip
  /// of the flat bar (no-op in the classic presentation).
  void updateFlatBarFromMRMLSliceNode();
  /// Update the flat bar when the event loop runs (with the offset slider): the slice node and the slice logic
  /// are modified several times per slice change, and each separate repaint of the bar costs a window update.
  void requestFlatBarUpdate();
  /// Hide the less important flat bar widgets when the bar is narrow.
  void updateFlatBarWidgetsVisibility();
  /// Select the Reformat module (through the layout manager of the application), or show the
  /// reformat widget when no module can be selected.
  void openReformat();

  void setupLinkedOptionsMenu();
  void setupReformatOptionsMenu();
  void setupCompositingMenu();
  void setupSliceSpacingMenu();
  void setupSliceModelMenu();
  void setupSegmentationMenu();
  void setupLabelMapMenu();
  void setupMoreOptionsMenu();
  void setupOrientationMarkerMenu();
  void setupRulerMenu();
  void setupSlabReconstructionMenu();

  vtkSmartPointer<vtkCollection> saveNodesForUndo(const QString& nodeTypes);

  void enableLayerWidgets();

  vtkMRMLSliceLogic* compositeNodeLogic(vtkMRMLSliceCompositeNode* node);
  vtkMRMLSliceLogic* sliceNodeLogic(vtkMRMLSliceNode* node);

  void setForegroundInterpolation(vtkMRMLSliceLogic* logic, bool interpolate);
  void setBackgroundInterpolation(vtkMRMLSliceLogic* logic, bool interpolate);

  /// Create a list of orientation containing the regular presets and also
  /// the "Reformat" string if sliceToRAS is different one of the preset.
  static void updateSliceOrientationSelector(vtkMRMLSliceNode* sliceNode, QComboBox* sliceOrientationSelector);

  /// Update the spin boxes of the popup menus that show slice node properties (field of view,
  /// slice model). They are only updated while the popup is visible, and when it is shown.
  void updatePopupWidgetsFromMRMLSliceNode();

  /// Update the range, step and value of the slice offset slider from the slice logic.
  void updateSliceOffsetSliderFromSliceLogic();

  bool eventFilter(QObject* object, QEvent* event) override;

public slots:
  /// Update widget state when the scene is modified
  void updateFromMRMLScene();

  /// Update widget state using the associated MRML slice node
  void updateWidgetFromMRMLSliceNode();

  /// Update widget state using the associated MRML slice composite node
  void updateWidgetFromMRMLSliceCompositeNode();

  /// Update step size when unit changes
  void updateWidgetFromUnitNode();

  /// Called after a foreground layer volume node is selected
  /// using the associated qMRMLNodeComboBox
  void onForegroundLayerNodeSelected(vtkMRMLNode* node);

  /// Called after a background layer volume node is selected
  /// using the associated qMRMLNodeComboBox
  void onBackgroundLayerNodeSelected(vtkMRMLNode* node);

  /// Called after a label layer volume node is selected
  /// using the associated qMRMLNodeComboBox
  void onLabelMapNodeSelected(vtkMRMLNode* node);

  /// Called after a segmentation node is selected in the combobox
  void onSegmentationNodeSelected(vtkMRMLNode* node);

  /// Called after the currently selected segmentation node's display
  /// option is modified
  void onSegmentationNodeDisplayModifiedEvent(vtkObject* nodeObject);
  /// Called when segment visibility is changed from the segment combobox
  void onSegmentVisibilitySelectionChanged(QStringList selectedSegmentIDs);
  /// Update segmentation outline/fill button
  void updateSegmentationOutlineFillButton();
  /// Utility function to get the display node of the current segmentation
  vtkMRMLSegmentationDisplayNode* currentSegmentationDisplayNode();

  void updateFromForegroundDisplayNode(vtkObject* displayNode);
  void updateFromBackgroundDisplayNode(vtkObject* displayNode);

  void updateFromForegroundVolumeNode(vtkObject* volumeNode);
  void updateFromBackgroundVolumeNode(vtkObject* volumeNode);

  /// Called after the SliceLogic is modified
  void onSliceLogicModifiedEvent();

  void updateSliceOffsetSliderVisibility();

protected:
  void setupPopupUi() override;
  void setMRMLSliceCompositeNodeInternal(vtkMRMLSliceCompositeNode* sliceComposite);
  void setAndObserveSelectionNode();

public:
  vtkMRMLSliceCompositeNode* MRMLSliceCompositeNode;
  vtkMRMLSelectionNode* SelectionNode{ nullptr };
  vtkSmartPointer<vtkMRMLSliceLogic> SliceLogic;
  vtkCollection* SliceLogics;
  vtkWeakPointer<vtkAlgorithmOutput> ImageDataConnection;
  QButtonGroup* ControllerButtonGroup;

  QToolButton* FitToWindowToolButton;
  qMRMLSliderWidget* SliceOffsetSlider;
  ctkDynamicSpacer* SliderSpacer;
  /// Slicer offset resolution without applying display scaling.
  double SliceOffsetResolution{ 1.0 };
  /// Slice offset range last set with setSliceOffsetRange() (valid if SliceOffsetRangeSet is true).
  /// The slider range is only set again when it changes.
  double SliceOffsetRange[2]{ 0.0, 0.0 };
  bool SliceOffsetRangeSet{ false };
  /// True while an update of the slice offset slider from the slice logic is queued: the slice logic can be
  /// modified several times per slice change, the slider is only updated once.
  bool SliceOffsetSliderUpdatePending{ false };
  /// True if the popup widgets need an update when the popup is shown
  bool PopupWidgetsUpdatePending{ false };
  double LastLabelMapOpacity;
  double LastForegroundOpacity;
  double LastBackgroundOpacity;

  QMenu* CompositingMenu;
  QMenu* SliceSpacingMenu;
  QMenu* SliceModelMenu;
  QMenu* SegmentationMenu;
  QMenu* LabelMapMenu;
  QMenu* OrientationMarkerMenu;
  QMenu* RulerMenu;
  QMenu* SlabReconstructionMenu;

  ctkDoubleSpinBox* SliceSpacingSpinBox;
  ctkDoubleSpinBox* SliceFOVSpinBox;
  ctkDoubleSpinBox* SlabReconstructionThicknessSpinBox;

  ctkDoubleSpinBox* SliceModelFOVXSpinBox;
  ctkDoubleSpinBox* SliceModelFOVYSpinBox;

  ctkDoubleSpinBox* SliceModelOriginXSpinBox;
  ctkDoubleSpinBox* SliceModelOriginYSpinBox;

  QSpinBox* SliceModelDimensionXSpinBox;
  QSpinBox* SliceModelDimensionYSpinBox;

  QSize ViewSize;

  QIcon AutomaticSliceSpacingIcon;
  QIcon ManualSliceSpacingIcon;

  ctkSignalMapper* OrientationMarkerTypesMapper;
  ctkSignalMapper* OrientationMarkerSizesMapper;

  ctkSignalMapper* RulerTypesMapper;
  ctkSignalMapper* RulerColorMapper;

  ctkSignalMapper* SlabReconstructionTypesMapper;
  QMenu* SlabReconstructionThicknessMenu{ nullptr };
  QActionGroup* SlabReconstructionTypesActions{ nullptr };

  bool ShowSliceOffsetSlider{ true };

  // Flat bar (qMRMLViewControllerBar::flatStyle)
  QToolButton* OrientationButton{ nullptr };
  QMenu* OrientationMenu{ nullptr };
  QActionGroup* OrientationActions{ nullptr };
  QAction* ReformatAction{ nullptr };
  QAction* ResetOrientationAction{ nullptr };
  QLabel* SliceIndexLabel{ nullptr };
  /// Holds the readout in the bar layout: its minimum width is 1 px, so the readout never sets the minimum width
  /// of the bar (the readout is shortened, then hidden, when the bar is narrow); the label keeps a fixed width
  /// so that a new text does not lay out the bar again
  QWidget* SliceIndexHolder{ nullptr };
  /// Readout texts: "S 96.0 mm · 97/193" and the short form "97/193" (or "S 96.0 mm" without an image)
  QString FullReadoutText;
  QString CompactReadoutText;
  /// The bar shows the short readout (updateFlatBarWidgetsVisibility)
  bool CompactReadout{ false };
  /// Width reserved for each readout form: it only grows, so the label does not change size at every slice
  int FullReadoutWidth{ 0 };
  int CompactReadoutWidth{ 0 };
  /// Show the readout text of the current form in the label, with its reserved width.
  void updateReadoutLabel();
  /// Classic size policies of the widgets whose size hint the flat bar ignores
  QSizePolicy ClassicFitSizePolicy;
  QSizePolicy ClassicSliderSizePolicy;
  bool ClassicSizePoliciesRecorded{ false };
  QToolButton* BarLinkButton{ nullptr };
  QIcon FlatFitIcon;
  QString ClassicViewLabelToolTip;
  QIcon ClassicFitIcon;
  QString ClassicFitToolTip;
  QString ClassicSpinBoxStyleSheet;
  bool ClassicSpinBoxVisible{ true };
  bool ClassicMoreButtonChecked{ false };
  bool ClassicMoreButtonHidden{ false };
  bool ClassicStateRecorded{ false };
  bool FlatBarUpdatePending{ false };
};

#endif
