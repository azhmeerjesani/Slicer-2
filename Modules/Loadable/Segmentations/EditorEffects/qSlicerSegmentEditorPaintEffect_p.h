/*==============================================================================

  Copyright (c) Laboratory for Percutaneous Surgery (PerkLab)
  Queen's University, Kingston, ON, Canada. All Rights Reserved.

  See COPYRIGHT.txt
  or http://www.slicer.org/copyright/copyright.txt for details.

  Unless required by applicable law or agreed to in writing, software
  distributed under the License is distributed on an "AS IS" BASIS,
  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
  See the License for the specific language governing permissions and
  limitations under the License.

  This file was originally developed by Csaba Pinter, PerkLab, Queen's University
  and was supported through the Applied Cancer Research Unit program of Cancer Care
  Ontario with funds provided by the Ontario Ministry of Health and Long-Term Care

==============================================================================*/

#ifndef __qSlicerSegmentEditorPaintEffect_p_h
#define __qSlicerSegmentEditorPaintEffect_p_h

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

// Segmentations Editor Effects includes
#include "qSlicerSegmentationsEditorEffectsExport.h"

#include "qSlicerSegmentEditorPaintEffect.h"

// VTK includes
#include <vtkCutter.h>
#include <vtkCylinderSource.h>
#include <vtkSmartPointer.h>
#include <vtkSphereSource.h>
#include <vtkTransform.h>
#include <vtkTransformPolyDataFilter.h>
#include <vtkWeakPointer.h>

// Qt includes
#include <QObject>
#include <QList>
#include <QMap>

class BrushPipeline;
class ctkDoubleSlider;
class QPoint;
class QIcon;
class QFrame;
class QCheckBox;
class QToolButton;
class qMRMLSliceWidget;
class qMRMLSliderWidget;
class qMRMLSpinBox;
class QTimer;
class vtkActor2D;
class vtkGlyph2D;
class vtkGlyph3D;
class vtkMatrix4x4;
class vtkRegularPolygonSource;
class vtkTrivialProducer;
class vtkPoints;
class vtkPolyDataNormals;
class vtkPolyDataToImageStencil;

/// \brief Private implementation of the segment editor paint effect
class qSlicerSegmentEditorPaintEffectPrivate : public QObject
{
  Q_OBJECT
  Q_DECLARE_PUBLIC(qSlicerSegmentEditorPaintEffect);

protected:
  qSlicerSegmentEditorPaintEffect* const q_ptr;

public:
  typedef QObject Superclass;
  qSlicerSegmentEditorPaintEffectPrivate(qSlicerSegmentEditorPaintEffect& object);
  ~qSlicerSegmentEditorPaintEffectPrivate() override;

  /// Depending on the \sa DelayedPaint mode, either paint the given point or queue
  /// it up with a marker for later painting.
  /// If lastBrushPosition_World is specified then multiple points are added along a line
  /// that connects the current and last brush position, to ensure a smooth and continuous brush stroke.
  void paintAddPoint(qMRMLWidget* viewWidget, double pixelPositionWorld[3], double* lastBrushPosition_World = nullptr);

  /// Update paint circle glyph
  void updateBrush(qMRMLWidget* viewWidget, BrushPipeline* brush);

  /// Update brushes. The view that the mouse is in is always rendered, other views only if the brush
  /// or the stroke feedback may have changed what they show.
  void updateBrushes();

  /// Update brush model (shape and position)
  void updateBrushModel(qMRMLWidget* viewWidget, double brushPosition_World[3]);

  /// Updates the brush stencil that can be used to quickly paint the brush shape into
  /// modifierLabelmap at many different positions.
  void updateBrushStencil(qMRMLWidget* viewWidget);

protected:
  /// Get brush object for widget. Create if does not exist
  BrushPipeline* brushForWidget(qMRMLWidget* viewWidget);

  /// Delete all brush pipelines
  void clearBrushPipelines();

  /// Paint brushes to the modifier labelmap
  void paintBrushes(vtkOrientedImageData* modifierLabelmap, qMRMLWidget* viewWidget, vtkPoints* pixelPositions_World, int extent[6] = nullptr);

  /// Paint one pixel at coordinate
  void paintPixel(vtkOrientedImageData* modifierLabelmap, qMRMLWidget* viewWidget, double pixelPosition_World[3]);

  /// Paint pixels at the coordinates
  void paintPixels(vtkOrientedImageData* modifierLabelmap, vtkPoints* pixelPositions_World, int extent[6] = nullptr);

  /// Transform points from World to IJK
  void transformPointsFromWorldToIJK(vtkOrientedImageData* image, vtkMRMLSegmentationNode* segmentationNode, vtkPoints* inputPoints, vtkPoints* outputPoints);

  /// Scale brush diameter and save it in parameter node
  void scaleDiameter(double scaleFactor);

  // Compute absolute size (in mm) from relative size (percentage of screen height)
  void updateAbsoluteBrushDiameter();

  bool brushPositionInWorld(qMRMLWidget* viewWidget, int brushPositionInView[2], double brushPosition_World[3]);

  /// Radius of a sphere around the brush position that contains the brush model
  double brushBoundingRadius();

  /// Add the stroke points that have no feedback glyph yet to the feedback (glyphs only the new points)
  void addFeedbackPoints();

  /// Remove all points from the stroke feedback
  void resetFeedback();

  /// Show the brush size next to the brush for a short time (or while Shift is held)
  void showBrushSizeHud();

  /// Update text, position and visibility of the brush size display of a slice view
  void updateBrushSizeHud(qMRMLWidget* viewWidget, BrushPipeline* pipeline);

  /// Hide the brush size display if it is not requested anymore
  void hideBrushSizeHudIfExpired();

  /// True if [ and ] keys resize the brush (Studio/BracketKeysResizeBrush setting,
  /// always false when testing or without main window)
  bool bracketKeysResizeBrush() const;

  /// True if the brush size display may be shown (false when testing or without main window)
  bool brushSizeHudEnabled() const;

  std::string segmentAtPosition(qMRMLWidget* viewWidget, double* ras);

public slots:
  void onDiameterUnitsClicked();
  void onDiameterValueChanged(double);
  void onBrushSizeHudTimeout();

public:
  QIcon PaintIcon;

  vtkSmartPointer<vtkCylinderSource> BrushCylinderSource;
  vtkSmartPointer<vtkSphereSource> BrushSphereSource;
  vtkSmartPointer<vtkTransformPolyDataFilter> BrushToWorldOriginTransformer;
  vtkSmartPointer<vtkTransform> BrushToWorldOriginTransform;

  vtkSmartPointer<vtkTransformPolyDataFilter> WorldOriginToWorldTransformer;
  vtkSmartPointer<vtkTransform> WorldOriginToWorldTransform;
  vtkSmartPointer<vtkPolyDataNormals> BrushPolyDataNormals;
  vtkSmartPointer<vtkTransformPolyDataFilter> WorldOriginToModifierLabelmapIjkTransformer;
  vtkSmartPointer<vtkTransform>
    WorldOriginToModifierLabelmapIjkTransform; // transforms from polydata source to modifierLabelmap's IJK coordinate system (brush origin in IJK origin)
  vtkSmartPointer<vtkPolyDataToImageStencil> BrushPolyDataToStencil;

  vtkSmartPointer<vtkPoints> PaintCoordinates_World;

  /// Stroke feedback, built incrementally: only the new stroke points are glyphed (by reusable glyph
  /// filters) and the glyphs are appended to persistent polydata, so the pipeline has a constant size.
  /// Brush glyphs in world coordinates (shown in 3D views and cut by the other slice views).
  vtkSmartPointer<vtkPolyData> FeedbackPolyData3D;
  vtkSmartPointer<vtkTrivialProducer> FeedbackProducer3D;
  /// Circle glyphs in the slice coordinate system of the slice view that the stroke is painted in.
  vtkSmartPointer<vtkPolyData> FeedbackPolyData2D;
  vtkSmartPointer<vtkTrivialProducer> FeedbackProducer2D;
  /// Reusable filters that glyph the new stroke points
  vtkSmartPointer<vtkPolyData> FeedbackNewPoints3D;
  vtkSmartPointer<vtkGlyph3D> FeedbackGlyph3D;
  vtkSmartPointer<vtkPolyData> FeedbackNewPoints2D;
  vtkSmartPointer<vtkRegularPolygonSource> FeedbackCircleSource;
  vtkSmartPointer<vtkGlyph2D> FeedbackGlyph2D;
  /// Number of stroke points that already have a feedback glyph
  vtkIdType FeedbackPointCount;
  /// Slice view that the current stroke is painted in (nullptr if painting in a 3D view)
  qMRMLWidget* FeedbackViewWidget;
  vtkSmartPointer<vtkMatrix4x4> FeedbackRasToSlice;

  /// Current brush center position (set by updateBrushModel)
  double BrushPosition_World[3];
  bool BrushPositionValid;
  /// Brush center position at the previous updateBrushes call
  double PreviousBrushPosition_World[3];
  /// Brush model and brushes are up to date for the last hover position in the active view
  bool BrushModelUpToDate;

  /// Brush size display
  QTimer* BrushSizeHudTimer;
  bool ShiftKeyHeld;
  bool BrushSizeParametersInitialized;
  int LastBrushDiameterIsRelative;
  double LastBrushDiameter;

  /// If a new point is added at less than this squared distance
  /// then the point is not added. It can be used for limiting number of
  /// points to improve performance.
  double MinimumPaintPointDistance2;

  /// Maximum distance between points of a continuous paint stroke.
  /// Defined as fraction of brush size.
  /// Smaller value creates more continuous strokes, even when mouse moves quickly.
  double MaximumPointDistanceInStroke;

  /// Previous brush position in the current stroke.
  double LastBrushPosition_World[3];
  bool LastBrushPositionValid;

  QList<vtkActor2D*> FeedbackActors;
  QMap<qMRMLWidget*, BrushPipeline*> BrushPipelines;
  bool DelayedPaint;
  bool IsPainting;

  // Observed view node
  qMRMLWidget* ActiveViewWidget;
  int ActiveViewLastInteractionPosition[2];
  int ActiveViewLastPaintPosition[2];

  QFrame* PaintOptionsFrame;
  QFrame* BrushDiameterFrame;
  QFrame* BrushDiameterSizeFrame;
  qMRMLSliderWidget* BrushDiameterSliderWidget;
  QToolButton* BrushDiameterIsAbsoluteButton;
  QCheckBox* BrushSphereCheckbox;
  QCheckBox* EditIn3DViewsCheckbox;
  QCheckBox* ColorSmudgeCheckbox;
  QCheckBox* EraseAllSegmentsCheckbox;
  QCheckBox* BrushPixelModeCheckbox;
};

#endif
