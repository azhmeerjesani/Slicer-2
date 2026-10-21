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

// Segmentations includes
#include "qSlicerSegmentEditorPaintEffect.h"
#include "qSlicerSegmentEditorPaintEffect_p.h"
#include "vtkMRMLSegmentationNode.h"
#include "vtkMRMLSegmentationDisplayNode.h"
#include "vtkMRMLSegmentationsDisplayableManager2D.h"
#include "vtkMRMLSegmentEditorNode.h"
#include "vtkOrientedImageData.h"

// Qt includes
#include <QDebug>
#include <QCheckBox>
#include <QFrame>
#include <QHBoxLayout>
#include <QLabel>
#include <QSettings>
#include <QTimer>
#include <QToolButton>
#include <QVBoxLayout>
#include <QFormLayout>

// VTK includes
#include <vtkActor.h>
#include <vtkActor2D.h>
#include <vtkBoundingBox.h>
#include <vtkCamera.h>
#include <vtkCellArray.h>
#include <vtkCollection.h>
#include <vtkCommand.h>
#include <vtkGlyph2D.h>
#include <vtkGlyph3D.h>
#include <vtkIdList.h>
#include <vtkImageChangeInformation.h>
#include <vtkImageStencil.h>
#include <vtkImageStencilData.h>
#include <vtkImageStencilToImage.h>
#include <vtkMath.h>
#include <vtkMatrix4x4.h>
#include <vtkNew.h>
#include <vtkObjectFactory.h>
#include <vtkPlane.h>
#include <vtkPointData.h>
#include <vtkPoints.h>
#include <vtkPolyData.h>
#include <vtkPolyDataMapper.h>
#include <vtkPolyDataMapper2D.h>
#include <vtkPolyDataNormals.h>
#include <vtkPolyDataToImageStencil.h>
#include <vtkProperty2D.h>
#include <vtkProperty.h>
#include <vtkPropPicker.h>
#include <vtkRegularPolygonSource.h>
#include <vtkRenderer.h>
#include <vtkRenderWindow.h>
#include <vtkRenderWindowInteractor.h>
#include <vtkSmartPointer.h>
#include <vtkStringArray.h>
#include <vtkTextActor.h>
#include <vtkTextProperty.h>
#include <vtkTrivialProducer.h>
#include <vtkWorldPointPicker.h>
// CTK includes
#include "ctkDoubleSlider.h"

// MRML includes
#include <vtkEventBroker.h>
#include <vtkMRMLAccuratePicker.h>
#include <vtkMRMLScalarVolumeNode.h>
#include <vtkMRMLScene.h>
#include <vtkMRMLSliceNode.h>
#include <vtkMRMLTransformNode.h>
#include <vtkMRMLViewNode.h>

// Slicer includes
#include "qMRMLSliceView.h"
#include "qMRMLSliceWidget.h"
#include "qMRMLSliderWidget.h"
#include "qMRMLThreeDView.h"
#include "qMRMLThreeDWidget.h"
#include "qSlicerLayoutManager.h"
#include "qSlicerApplication.h"
#include "vtkSlicerApplicationLogic.h"
#include "vtkMRMLSliceLogic.h"
#include "vtkMRMLSliceLayerLogic.h"
#include "vtkOrientedImageDataResample.h"

//-----------------------------------------------------------------------------
/// Visualization objects and pipeline for each slice view for the paint brush
class BrushPipeline
{
public:
  BrushPipeline()
  {
    this->WorldToSliceTransform = vtkSmartPointer<vtkTransform>::New();
    this->SlicePlane = vtkSmartPointer<vtkPlane>::New();
  };
  virtual ~BrushPipeline() = default;
  virtual void SetBrushVisibility(bool visibility) = 0;
  virtual bool GetBrushVisibility() = 0;
  virtual void SetFeedbackVisibility(bool visibility) = 0;

  vtkSmartPointer<vtkTransform> WorldToSliceTransform;
  vtkSmartPointer<vtkPlane> SlicePlane;
  /// The last render requested for the view may show the brush or new stroke feedback,
  /// so the view needs to be rendered again when the brush moves away.
  bool ContentShownInLastRender = false;
};

class BrushPipeline2D : public BrushPipeline
{
public:
  BrushPipeline2D()
  {
    this->BrushCutter = vtkSmartPointer<vtkCutter>::New();
    this->BrushCutter->SetCutFunction(this->SlicePlane);
    this->BrushCutter->SetGenerateCutScalars(0);

    this->BrushWorldToSliceTransformer = vtkSmartPointer<vtkTransformPolyDataFilter>::New();
    this->BrushWorldToSliceTransformer->SetTransform(this->WorldToSliceTransform);
    this->BrushWorldToSliceTransformer->SetInputConnection(this->BrushCutter->GetOutputPort());

    this->BrushMapper = vtkSmartPointer<vtkPolyDataMapper2D>::New();
    this->BrushMapper->SetInputConnection(this->BrushWorldToSliceTransformer->GetOutputPort());

    this->BrushActor = vtkSmartPointer<vtkActor2D>::New();
    this->BrushActor->SetMapper(this->BrushMapper);
    vtkProperty2D* brushActorProperty = this->BrushActor->GetProperty();
    brushActorProperty->SetColor(1.0, 1.0, 0.2);
    this->BrushActor->VisibilityOff();

    this->FeedbackCutter = vtkSmartPointer<vtkCutter>::New();
    this->FeedbackCutter->SetCutFunction(this->SlicePlane);
    this->FeedbackCutter->SetGenerateCutScalars(0);

    this->FeedbackWorldToSliceTransformer = vtkSmartPointer<vtkTransformPolyDataFilter>::New();
    this->FeedbackWorldToSliceTransformer->SetTransform(this->WorldToSliceTransform);
    this->FeedbackWorldToSliceTransformer->SetInputConnection(this->FeedbackCutter->GetOutputPort());

    this->FeedbackMapper = vtkSmartPointer<vtkPolyDataMapper2D>::New();
    this->FeedbackMapper->SetInputConnection(this->FeedbackWorldToSliceTransformer->GetOutputPort());
    this->FeedbackActor = vtkSmartPointer<vtkActor2D>::New();
    vtkProperty2D* feedbackActorProperty = this->FeedbackActor->GetProperty();
    feedbackActorProperty->SetColor(0.7, 0.7, 0.0);
    feedbackActorProperty->SetOpacity(0.5);
    this->FeedbackActor->SetMapper(this->FeedbackMapper);
    this->FeedbackActor->VisibilityOff();

    // Feedback of the slice view that the stroke is painted in: circles in slice coordinates, no cutting needed
    this->SliceToXYTransform = vtkSmartPointer<vtkTransform>::New();
    this->FeedbackSliceToXYTransformer = vtkSmartPointer<vtkTransformPolyDataFilter>::New();
    this->FeedbackSliceToXYTransformer->SetTransform(this->SliceToXYTransform);

    // Brush size display
    this->HudActor = vtkSmartPointer<vtkTextActor>::New();
    vtkTextProperty* hudTextProperty = this->HudActor->GetTextProperty();
    hudTextProperty->SetFontFamily(VTK_ARIAL);
    hudTextProperty->SetFontSize(12);
    hudTextProperty->SetColor(1.0, 1.0, 1.0);
    hudTextProperty->ShadowOn();
    hudTextProperty->SetVerticalJustificationToCentered();
    this->HudActor->PickableOff();
    this->HudActor->VisibilityOff();
  };
  ~BrushPipeline2D() override = default;

  void SetBrushVisibility(bool visibility) override { this->BrushActor->SetVisibility(visibility); };
  bool GetBrushVisibility() override { return this->BrushActor->GetVisibility(); };
  void SetFeedbackVisibility(bool visibility) override { this->FeedbackActor->SetVisibility(visibility); };
  void SetFeedbackFromPaintView(bool paintView)
  {
    if (paintView)
    {
      this->FeedbackMapper->SetInputConnection(this->FeedbackSliceToXYTransformer->GetOutputPort());
    }
    else
    {
      this->FeedbackMapper->SetInputConnection(this->FeedbackWorldToSliceTransformer->GetOutputPort());
    }
  };

  vtkSmartPointer<vtkActor2D> BrushActor;
  vtkSmartPointer<vtkPolyDataMapper2D> BrushMapper;
  vtkSmartPointer<vtkActor2D> FeedbackActor;
  vtkSmartPointer<vtkPolyDataMapper2D> FeedbackMapper;
  vtkSmartPointer<vtkCutter> BrushCutter;
  vtkSmartPointer<vtkTransformPolyDataFilter> BrushWorldToSliceTransformer;
  vtkSmartPointer<vtkCutter> FeedbackCutter;
  vtkSmartPointer<vtkTransformPolyDataFilter> FeedbackWorldToSliceTransformer;
  vtkSmartPointer<vtkTransform> SliceToXYTransform;
  vtkSmartPointer<vtkTransformPolyDataFilter> FeedbackSliceToXYTransformer;
  vtkSmartPointer<vtkTextActor> HudActor;
};

class BrushPipeline3D : public BrushPipeline
{
public:
  BrushPipeline3D()
  {
    this->BrushMapper = vtkSmartPointer<vtkPolyDataMapper>::New();
    this->BrushActor = vtkSmartPointer<vtkActor>::New();
    this->BrushActor->SetMapper(this->BrushMapper);
    this->BrushActor->VisibilityOff();
    this->BrushActor->PickableOff(); // otherwise picking in 3D view would not work

    this->FeedbackMapper = vtkSmartPointer<vtkPolyDataMapper>::New();
    this->FeedbackActor = vtkSmartPointer<vtkActor>::New();
    this->FeedbackActor->VisibilityOff();
    this->FeedbackActor->PickableOff(); // otherwise picking in 3D view would not work
    vtkProperty* feedbackActorProperty = this->FeedbackActor->GetProperty();
    feedbackActorProperty->SetColor(0.7, 0.7, 0.0);
    this->FeedbackActor->SetMapper(this->FeedbackMapper);
    this->FeedbackActor->VisibilityOff();
  };
  ~BrushPipeline3D() override = default;
  void SetBrushVisibility(bool visibility) override { this->BrushActor->SetVisibility(visibility); };
  bool GetBrushVisibility() override { return this->BrushActor->GetVisibility(); };
  void SetFeedbackVisibility(bool visibility) override { this->FeedbackActor->SetVisibility(visibility); };

  vtkSmartPointer<vtkPolyDataMapper> BrushMapper;
  vtkSmartPointer<vtkActor> BrushActor;
  vtkSmartPointer<vtkActor> FeedbackActor;
  vtkSmartPointer<vtkPolyDataMapper> FeedbackMapper;
};

//-----------------------------------------------------------------------------
namespace
{
/// Signed distance of a point from a plane (the plane normal does not need to be normalized)
double signedDistanceToPlane(vtkPlane* plane, const double point[3])
{
  double normal[3] = { 0.0, 0.0, 1.0 };
  plane->GetNormal(normal);
  if (vtkMath::Normalize(normal) == 0.0)
  {
    return 0.0;
  }
  double origin[3] = { 0.0, 0.0, 0.0 };
  plane->GetOrigin(origin);
  return normal[0] * (point[0] - origin[0]) + normal[1] * (point[1] - origin[1]) + normal[2] * (point[2] - origin[2]);
}
} // namespace

//-----------------------------------------------------------------------------
// qSlicerSegmentEditorPaintEffectPrivate methods

//-----------------------------------------------------------------------------
qSlicerSegmentEditorPaintEffectPrivate::qSlicerSegmentEditorPaintEffectPrivate(qSlicerSegmentEditorPaintEffect& object)
  : q_ptr(&object)
  , FeedbackPointCount(0)
  , FeedbackViewWidget(nullptr)
  , BrushPositionValid(false)
  , BrushModelUpToDate(false)
  , BrushSizeHudTimer(nullptr)
  , ShiftKeyHeld(false)
  , BrushSizeParametersInitialized(false)
  , LastBrushDiameterIsRelative(0)
  , LastBrushDiameter(0.0)
  , MinimumPaintPointDistance2(0.0)
  , MaximumPointDistanceInStroke(0.2)
  , LastBrushPositionValid(false)
  , DelayedPaint(true)
  , IsPainting(false)
  , ActiveViewWidget(nullptr)
  , PaintOptionsFrame(nullptr)
  , BrushDiameterFrame(nullptr)
  , BrushDiameterSizeFrame(nullptr)
  , BrushDiameterSliderWidget(nullptr)
  , BrushDiameterIsAbsoluteButton(nullptr)
  , BrushSphereCheckbox(nullptr)
  , EditIn3DViewsCheckbox(nullptr)
  , ColorSmudgeCheckbox(nullptr)
  , EraseAllSegmentsCheckbox(nullptr)
  , BrushPixelModeCheckbox(nullptr)
{
  this->PaintCoordinates_World = vtkSmartPointer<vtkPoints>::New();

  this->PaintIcon = QIcon(":Icons/Paint.png");

  this->BrushCylinderSource = vtkSmartPointer<vtkCylinderSource>::New();
  this->BrushSphereSource = vtkSmartPointer<vtkSphereSource>::New();
  this->BrushToWorldOriginTransformer = vtkSmartPointer<vtkTransformPolyDataFilter>::New();
  this->BrushToWorldOriginTransform = vtkSmartPointer<vtkTransform>::New();
  this->BrushToWorldOriginTransformer->SetTransform(this->BrushToWorldOriginTransform);
  this->BrushPolyDataNormals = vtkSmartPointer<vtkPolyDataNormals>::New();
  this->BrushPolyDataNormals->SetInputConnection(this->BrushToWorldOriginTransformer->GetOutputPort());
  this->BrushPolyDataNormals->AutoOrientNormalsOn();

  this->WorldOriginToWorldTransformer = vtkSmartPointer<vtkTransformPolyDataFilter>::New();
  this->WorldOriginToWorldTransform = vtkSmartPointer<vtkTransform>::New();
  this->WorldOriginToWorldTransformer->SetTransform(this->WorldOriginToWorldTransform);
  this->WorldOriginToWorldTransformer->SetInputConnection(this->BrushPolyDataNormals->GetOutputPort());

  this->WorldOriginToModifierLabelmapIjkTransformer = vtkSmartPointer<vtkTransformPolyDataFilter>::New();
  this->WorldOriginToModifierLabelmapIjkTransform = vtkSmartPointer<vtkTransform>::New();
  this->WorldOriginToModifierLabelmapIjkTransformer->SetTransform(this->WorldOriginToModifierLabelmapIjkTransform);
  this->WorldOriginToModifierLabelmapIjkTransformer->SetInputConnection(this->BrushPolyDataNormals->GetOutputPort());
  this->BrushPolyDataToStencil = vtkSmartPointer<vtkPolyDataToImageStencil>::New();
  this->BrushPolyDataToStencil->SetOutputSpacing(1.0, 1.0, 1.0);
  this->BrushPolyDataToStencil->SetInputConnection(this->WorldOriginToModifierLabelmapIjkTransformer->GetOutputPort());

  this->FeedbackPolyData3D = vtkSmartPointer<vtkPolyData>::New();
  this->FeedbackProducer3D = vtkSmartPointer<vtkTrivialProducer>::New();
  this->FeedbackProducer3D->SetOutput(this->FeedbackPolyData3D);
  this->FeedbackPolyData2D = vtkSmartPointer<vtkPolyData>::New();
  this->FeedbackProducer2D = vtkSmartPointer<vtkTrivialProducer>::New();
  this->FeedbackProducer2D->SetOutput(this->FeedbackPolyData2D);
  this->FeedbackNewPoints3D = vtkSmartPointer<vtkPolyData>::New();
  this->FeedbackGlyph3D = vtkSmartPointer<vtkGlyph3D>::New();
  this->FeedbackGlyph3D->SetInputData(this->FeedbackNewPoints3D);
  this->FeedbackGlyph3D->SetSourceConnection(this->BrushPolyDataNormals->GetOutputPort());
  this->FeedbackNewPoints2D = vtkSmartPointer<vtkPolyData>::New();
  this->FeedbackCircleSource = vtkSmartPointer<vtkRegularPolygonSource>::New();
  this->FeedbackCircleSource->SetNumberOfSides(24);
  this->FeedbackCircleSource->GeneratePolygonOff();
  this->FeedbackCircleSource->GeneratePolylineOn();
  this->FeedbackGlyph2D = vtkSmartPointer<vtkGlyph2D>::New();
  this->FeedbackGlyph2D->SetInputData(this->FeedbackNewPoints2D);
  this->FeedbackGlyph2D->SetSourceConnection(this->FeedbackCircleSource->GetOutputPort());
  this->FeedbackRasToSlice = vtkSmartPointer<vtkMatrix4x4>::New();
  this->resetFeedback();

  this->BrushSizeHudTimer = new QTimer(this);
  this->BrushSizeHudTimer->setSingleShot(true);
  this->BrushSizeHudTimer->setInterval(1200);
  QObject::connect(this->BrushSizeHudTimer, SIGNAL(timeout()), this, SLOT(onBrushSizeHudTimeout()));

  this->ActiveViewLastInteractionPosition[0] = 0;
  this->ActiveViewLastInteractionPosition[1] = 0;
  this->ActiveViewLastPaintPosition[0] = 0;
  this->ActiveViewLastPaintPosition[1] = 0;

  this->LastBrushPosition_World[0] = 0.0;
  this->LastBrushPosition_World[1] = 0.0;
  this->LastBrushPosition_World[2] = 0.0;

  for (int i = 0; i < 3; ++i)
  {
    this->BrushPosition_World[i] = 0.0;
    this->PreviousBrushPosition_World[i] = 0.0;
  }
}

//-----------------------------------------------------------------------------
qSlicerSegmentEditorPaintEffectPrivate::~qSlicerSegmentEditorPaintEffectPrivate()
{
  this->clearBrushPipelines();
}

//-----------------------------------------------------------------------------
BrushPipeline* qSlicerSegmentEditorPaintEffectPrivate::brushForWidget(qMRMLWidget* viewWidget)
{
  Q_Q(qSlicerSegmentEditorPaintEffect);

  if (this->BrushPipelines.contains(viewWidget))
  {
    return this->BrushPipelines[viewWidget];
  }

  // Create brushPipeline if does not yet exist
  qMRMLSliceWidget* sliceWidget = qobject_cast<qMRMLSliceWidget*>(viewWidget);
  qMRMLThreeDWidget* threeDWidget = qobject_cast<qMRMLThreeDWidget*>(viewWidget);
  if (sliceWidget)
  {
    BrushPipeline2D* pipeline = new BrushPipeline2D();
    pipeline->BrushCutter->SetInputConnection(this->WorldOriginToWorldTransformer->GetOutputPort());
    pipeline->FeedbackCutter->SetInputConnection(this->FeedbackProducer3D->GetOutputPort());
    pipeline->FeedbackSliceToXYTransformer->SetInputConnection(this->FeedbackProducer2D->GetOutputPort());
    pipeline->SetFeedbackFromPaintView(viewWidget == this->FeedbackViewWidget);
    // Use the same font as other view annotations (custom font file of the application, if any)
    vtkSlicerApplicationLogic* appLogic = qSlicerApplication::application() ? qSlicerApplication::application()->applicationLogic() : nullptr;
    if (appLogic)
    {
      appLogic->UseCustomFontFile(pipeline->HudActor->GetTextProperty());
    }
    this->updateBrush(viewWidget, pipeline);

    // add brush actor later to make it appear above the feedback actor (feedback actor
    // shows previous brush positions)
    q->addActor2D(viewWidget, pipeline->FeedbackActor);
    q->addActor2D(viewWidget, pipeline->BrushActor);
    q->addActor2D(viewWidget, pipeline->HudActor);

    this->BrushPipelines[viewWidget] = pipeline;
    return pipeline;
  }
  else if (threeDWidget)
  {
    BrushPipeline3D* pipeline = new BrushPipeline3D();
    pipeline->BrushMapper->SetInputConnection(this->WorldOriginToWorldTransformer->GetOutputPort());
    pipeline->FeedbackMapper->SetInputConnection(this->FeedbackProducer3D->GetOutputPort());
    this->updateBrush(viewWidget, pipeline);
    q->addActor3D(viewWidget, pipeline->BrushActor);
    q->addActor3D(viewWidget, pipeline->FeedbackActor);
    this->BrushPipelines[viewWidget] = pipeline;
    return pipeline;
  }

  return nullptr;
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffectPrivate::paintAddPoint(qMRMLWidget* viewWidget, double brushPosition_World[3], double* lastBrushPosition_World /*=nullptr*/)
{
  Q_Q(qSlicerSegmentEditorPaintEffect);

  if (lastBrushPosition_World)
  {
    double strokeLength = sqrt(vtkMath::Distance2BetweenPoints(brushPosition_World, lastBrushPosition_World));
    double maximumDistanceBetweenPoints = this->MaximumPointDistanceInStroke * q->doubleParameter("BrushAbsoluteDiameter");
    if (maximumDistanceBetweenPoints > 0.0)
    {
      int numberOfPointsToAdd = static_cast<int>(strokeLength / maximumDistanceBetweenPoints) - 1;
      for (int pointIndex = 0; pointIndex < numberOfPointsToAdd; pointIndex++)
      {
        double lastPointWeight = static_cast<double>(pointIndex + 1) / static_cast<double>(numberOfPointsToAdd + 1);
        this->PaintCoordinates_World->InsertNextPoint(lastPointWeight * lastBrushPosition_World[0] + (1.0 - lastPointWeight) * brushPosition_World[0],
                                                      lastPointWeight * lastBrushPosition_World[1] + (1.0 - lastPointWeight) * brushPosition_World[1],
                                                      lastPointWeight * lastBrushPosition_World[2] + (1.0 - lastPointWeight) * brushPosition_World[2]);
      }
    }
  }
  this->PaintCoordinates_World->InsertNextPoint(brushPosition_World);
  this->PaintCoordinates_World->Modified();

  if (q->integerParameter("BrushPixelMode") || !this->DelayedPaint)
  {
    // the points are painted (and removed) right away, no feedback is needed
    q->paintApply(viewWidget);
    qSlicerSegmentEditorAbstractEffect::forceRender(viewWidget); // TODO: repaint all?
  }
  else
  {
    this->addFeedbackPoints();
  }
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffectPrivate::updateBrushStencil(qMRMLWidget* viewWidget)
{
  Q_Q(qSlicerSegmentEditorPaintEffect);
  Q_UNUSED(viewWidget);

  if (!q->parameterSetNode())
  {
    qCritical() << Q_FUNC_INFO << ": Invalid segment editor parameter set node!";
    return;
  }
  vtkMRMLSegmentationNode* segmentationNode = q->parameterSetNode()->GetSegmentationNode();
  if (!segmentationNode)
  {
    qCritical() << Q_FUNC_INFO << ": Invalid segmentationNode";
    return;
  }
  vtkOrientedImageData* modifierLabelmap = q->modifierLabelmap();
  if (!modifierLabelmap)
  {
    qCritical() << Q_FUNC_INFO << ": Invalid modifierLabelmap";
    return;
  }

  // Brush stencil transform

  this->WorldOriginToModifierLabelmapIjkTransform->Identity();

  vtkNew<vtkMatrix4x4> segmentationToSegmentationIjkTransformMatrix;
  modifierLabelmap->GetImageToWorldMatrix(segmentationToSegmentationIjkTransformMatrix.GetPointer());
  segmentationToSegmentationIjkTransformMatrix->Invert();
  segmentationToSegmentationIjkTransformMatrix->SetElement(0, 3, 0);
  segmentationToSegmentationIjkTransformMatrix->SetElement(1, 3, 0);
  segmentationToSegmentationIjkTransformMatrix->SetElement(2, 3, 0);
  this->WorldOriginToModifierLabelmapIjkTransform->Concatenate(segmentationToSegmentationIjkTransformMatrix.GetPointer());

  vtkNew<vtkMatrix4x4> worldToSegmentationTransformMatrix;
  // We don't support painting in non-linearly transformed node (it could be implemented, but would probably slow down things too much)
  // TODO: show a meaningful error message to the user if attempted
  vtkMRMLTransformNode::GetMatrixTransformBetweenNodes(nullptr, segmentationNode->GetParentTransformNode(), worldToSegmentationTransformMatrix.GetPointer());
  worldToSegmentationTransformMatrix->SetElement(0, 3, 0);
  worldToSegmentationTransformMatrix->SetElement(1, 3, 0);
  worldToSegmentationTransformMatrix->SetElement(2, 3, 0);
  this->WorldOriginToModifierLabelmapIjkTransform->Concatenate(worldToSegmentationTransformMatrix.GetPointer());

  this->WorldOriginToModifierLabelmapIjkTransformer->Update();
  vtkPolyData* brushModel_ModifierLabelmapIjk = this->WorldOriginToModifierLabelmapIjkTransformer->GetOutput();
  double* boundsIjk = brushModel_ModifierLabelmapIjk->GetBounds();
  this->BrushPolyDataToStencil->SetOutputWholeExtent(
    floor(boundsIjk[0]) - 1, ceil(boundsIjk[1]) + 1, floor(boundsIjk[2]) - 1, ceil(boundsIjk[3]) + 1, floor(boundsIjk[4]) - 1, ceil(boundsIjk[5]) + 1);
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffectPrivate::transformPointsFromWorldToIJK(vtkOrientedImageData* image,
                                                                           vtkMRMLSegmentationNode* segmentationNode,
                                                                           vtkPoints* rasPoints,
                                                                           vtkPoints* ijkPoints)
{
  vtkNew<vtkTransform> worldToModifierLabelmapIjkTransform;

  vtkNew<vtkMatrix4x4> segmentationToSegmentationIjkTransformMatrix;
  image->GetWorldToImageMatrix(segmentationToSegmentationIjkTransformMatrix.GetPointer());
  worldToModifierLabelmapIjkTransform->Concatenate(segmentationToSegmentationIjkTransformMatrix.GetPointer());

  vtkNew<vtkMatrix4x4> worldToSegmentationTransformMatrix;
  // We don't support painting in non-linearly transformed node (it could be implemented, but would probably slow down things too much)
  // TODO: show a meaningful error message to the user if attempted
  vtkMRMLTransformNode::GetMatrixTransformBetweenNodes(nullptr, segmentationNode->GetParentTransformNode(), worldToSegmentationTransformMatrix.GetPointer());
  worldToModifierLabelmapIjkTransform->Concatenate(worldToSegmentationTransformMatrix.GetPointer());
  worldToModifierLabelmapIjkTransform->TransformPoints(rasPoints, ijkPoints);
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffectPrivate::paintPixel(vtkOrientedImageData* modifierLabelmap, qMRMLWidget* viewWidget, double pixelPosition_World[3])
{
  Q_UNUSED(viewWidget);
  vtkSmartPointer<vtkPoints> points = vtkSmartPointer<vtkPoints>::New();
  points->InsertNextPoint(pixelPosition_World);
  this->paintPixels(modifierLabelmap, points);
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffectPrivate::paintPixels(vtkOrientedImageData* modifierLabelmap, vtkPoints* pixelPositions_World, int updateExtent[6])
{
  Q_Q(qSlicerSegmentEditorPaintEffect);

  if (!pixelPositions_World)
  {
    qCritical() << Q_FUNC_INFO << ": Invalid pixelPositions";
    return;
  }

  if (!modifierLabelmap)
  {
    return;
  }

  int dims[3] = { 0, 0, 0 };
  modifierLabelmap->GetDimensions(dims);

  double valueToSet = (q->m_Erase ? q->m_EraseValue : q->m_FillValue);

  vtkMRMLSegmentationNode* segmentationNode = q->parameterSetNode()->GetSegmentationNode();
  if (!segmentationNode)
  {
    qCritical() << Q_FUNC_INFO << ": Invalid segmentationNode";
    return;
  }

  vtkIdType numberOfPoints = pixelPositions_World->GetNumberOfPoints();

  int modifierExtent[6] = { 0, -1, 0, -1, 0, -1 };
  modifierLabelmap->GetExtent(modifierExtent);

  vtkNew<vtkPoints> paintCoordinates_Ijk;
  this->transformPointsFromWorldToIJK(modifierLabelmap, segmentationNode, this->PaintCoordinates_World, paintCoordinates_Ijk);

  for (int pointIndex = 0; pointIndex < numberOfPoints; pointIndex++)
  {
    double ijkCoordinates[3] = { 0 };

    paintCoordinates_Ijk->GetPoint(pointIndex, ijkCoordinates);

    int ijk[3] = { static_cast<int>(std::round(ijkCoordinates[0])), static_cast<int>(std::round(ijkCoordinates[1])), static_cast<int>(std::round(ijkCoordinates[2])) };

    // Clamp to image extent
    if (ijk[0] < modifierExtent[0] || ijk[0] > modifierExtent[1] || //
        ijk[1] < modifierExtent[2] || ijk[1] > modifierExtent[3] || //
        ijk[2] < modifierExtent[4] || ijk[2] > modifierExtent[5])
    {
      continue;
    }

    for (int i = 0; i < 3; ++i)
    {
      updateExtent[2 * i] = std::max(updateExtent[2 * i], ijk[i]);
      updateExtent[2 * i + 1] = std::min(updateExtent[2 * i + 1], ijk[i]);
    }
    modifierLabelmap->SetScalarComponentFromDouble(ijk[0], ijk[1], ijk[2], 0, valueToSet);
  }
  modifierLabelmap->Modified();
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffectPrivate::paintBrushes(vtkOrientedImageData* modifierLabelmap, qMRMLWidget* viewWidget, vtkPoints* pixelPositions_World, int updateExtent[6])
{
  Q_UNUSED(pixelPositions_World);
  Q_Q(qSlicerSegmentEditorPaintEffect);

  this->updateBrushStencil(viewWidget);

  if (!modifierLabelmap)
  {
    return;
  }

  vtkMRMLSegmentationNode* segmentationNode = q->parameterSetNode()->GetSegmentationNode();
  if (!segmentationNode)
  {
    qCritical() << Q_FUNC_INFO << ": Invalid segmentationNode";
    return;
  }

  this->BrushPolyDataToStencil->Update();
  vtkImageStencilData* stencilData = this->BrushPolyDataToStencil->GetOutput();
  int stencilExtent[6] = { 0, -1, 0, -1, 0, -1 };
  stencilData->GetExtent(stencilExtent);

  vtkNew<vtkPoints> paintCoordinates_Ijk;
  this->transformPointsFromWorldToIJK(modifierLabelmap, segmentationNode, this->PaintCoordinates_World, paintCoordinates_Ijk);

  vtkNew<vtkImageStencilToImage> stencilToImage;
  stencilToImage->SetInputConnection(this->BrushPolyDataToStencil->GetOutputPort());
  stencilToImage->SetInsideValue(q->m_FillValue);
  stencilToImage->SetOutsideValue(q->m_EraseValue);
  stencilToImage->SetOutputScalarType(modifierLabelmap->GetScalarType());

  vtkNew<vtkImageChangeInformation> brushPositioner;
  brushPositioner->SetInputConnection(stencilToImage->GetOutputPort());
  brushPositioner->SetOutputSpacing(modifierLabelmap->GetSpacing());
  brushPositioner->SetOutputOrigin(modifierLabelmap->GetOrigin());

  vtkIdType numberOfPoints = this->PaintCoordinates_World->GetNumberOfPoints();
  for (int pointIndex = 0; pointIndex < numberOfPoints; pointIndex++)
  {
    double* shiftDouble = paintCoordinates_Ijk->GetPoint(pointIndex);
    int shift[3] = { vtkMath::Round(shiftDouble[0]), vtkMath::Round(shiftDouble[1]), vtkMath::Round(shiftDouble[2]) };
    brushPositioner->SetExtentTranslation(shift);
    brushPositioner->Update();
    vtkNew<vtkOrientedImageData> orientedBrushPositionerOutput;
    orientedBrushPositionerOutput->ShallowCopy(brushPositioner->GetOutput());
    orientedBrushPositionerOutput->CopyDirections(modifierLabelmap);
    if (pointIndex == 0)
    {
      orientedBrushPositionerOutput->GetExtent(updateExtent);
    }
    else
    {
      int* brushExtent = orientedBrushPositionerOutput->GetExtent();
      for (int i = 0; i < 3; i++)
      {
        if (brushExtent[i * 2] < updateExtent[i * 2])
        {
          updateExtent[i * 2] = brushExtent[i * 2];
        }
        if (brushExtent[i * 2 + 1] > updateExtent[i * 2 + 1])
        {
          updateExtent[i * 2 + 1] = brushExtent[i * 2 + 1];
        }
      }
    }
    vtkOrientedImageDataResample::ModifyImage(modifierLabelmap, orientedBrushPositionerOutput.GetPointer(), vtkOrientedImageDataResample::OPERATION_MAXIMUM);
  }
  modifierLabelmap->Modified();
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffectPrivate::scaleDiameter(double scaleFactor)
{
  Q_Q(qSlicerSegmentEditorPaintEffect);
  if (q->integerParameter("BrushDiameterIsRelative"))
  {
    q->setCommonParameter("BrushRelativeDiameter", q->doubleParameter("BrushRelativeDiameter") * scaleFactor);
  }
  else
  {
    // Stay within the limits of the slider (they follow the reference geometry)
    double minimumDiameter = q->doubleParameter("BrushMinimumAbsoluteDiameter");
    double maximumDiameter = q->doubleParameter("BrushMaximumAbsoluteDiameter");
    double diameter = q->doubleParameter("BrushAbsoluteDiameter") * scaleFactor;
    if (minimumDiameter > 0.0 && maximumDiameter > minimumDiameter)
    {
      diameter = qBound(minimumDiameter, diameter, maximumDiameter);
    }
    q->setCommonParameter("BrushAbsoluteDiameter", diameter);
  }
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffectPrivate::onDiameterUnitsClicked()
{
  Q_Q(qSlicerSegmentEditorPaintEffect);
  if (q->integerParameter("BrushDiameterIsRelative") == 0)
  {
    q->setCommonParameter("BrushDiameterIsRelative", 1);
  }
  else
  {
    q->setCommonParameter("BrushDiameterIsRelative", 0);
  }
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffectPrivate::onDiameterValueChanged(double value)
{
  Q_Q(qSlicerSegmentEditorPaintEffect);
  if (q->integerParameter("BrushDiameterIsRelative") == 0)
  {
    q->setCommonParameter("BrushAbsoluteDiameter", value);
  }
  else
  {
    q->setCommonParameter("BrushRelativeDiameter", value);
  }
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffectPrivate::updateAbsoluteBrushDiameter()
{
  Q_Q(qSlicerSegmentEditorPaintEffect);
  if (!q->integerParameter("BrushDiameterIsRelative"))
  {
    // user specified absolute brush diameter
    return;
  }
  if (this->ActiveViewWidget == nullptr)
  {
    return;
  }

  double mmPerPixel = 1.0;
  int screenSizePixel = 1000;
  qMRMLSliceWidget* sliceWidget = qobject_cast<qMRMLSliceWidget*>(this->ActiveViewWidget);
  qMRMLThreeDWidget* threeDWidget = qobject_cast<qMRMLThreeDWidget*>(this->ActiveViewWidget);
  if (sliceWidget)
  {
    vtkMatrix4x4* xyToSlice = sliceWidget->sliceLogic()->GetSliceNode()->GetXYToSlice();
    mmPerPixel = sqrt(xyToSlice->GetElement(0, 1) * xyToSlice->GetElement(0, 1) + xyToSlice->GetElement(1, 1) * xyToSlice->GetElement(1, 1)
                      + xyToSlice->GetElement(2, 1) * xyToSlice->GetElement(2, 1));
    screenSizePixel = sliceWidget->sliceView()->renderWindow()->GetScreenSize()[1];
  }
  else if (threeDWidget && threeDWidget->threeDView() && threeDWidget->threeDView()->renderer() //
           && threeDWidget->threeDView()->renderer()->GetActiveCamera())
  {
    screenSizePixel = threeDWidget->threeDView()->renderWindow()->GetScreenSize()[1];
    vtkRenderer* renderer = threeDWidget->threeDView()->renderer();
    // Viewport: xmin, ymin, xmax, ymax; range: 0.0-1.0; origin is bottom left
    // Determine the available renderer size in pixels
    double minX = 0;
    double minY = 0;
    renderer->NormalizedDisplayToDisplay(minX, minY);
    double maxX = 1;
    double maxY = 1;
    renderer->NormalizedDisplayToDisplay(maxX, maxY);
    int rendererSizeInPixels[2] = { static_cast<int>(maxX - minX), static_cast<int>(maxY - minY) };
    vtkCamera* cam = renderer->GetActiveCamera();
    if (cam->GetParallelProjection())
    {
      // Parallel scale: height of the viewport in world-coordinate distances.
      // Larger numbers produce smaller images.
      mmPerPixel = (cam->GetParallelScale() * 2.0) / double(rendererSizeInPixels[1]);
    }
    else
    {
      double cameraFP[4] = { 0 };
      cam->GetFocalPoint(cameraFP);
      cameraFP[3] = 1.0;

      double cameraViewUp[3] = { 0 };
      cam->GetViewUp(cameraViewUp);
      vtkMath::Normalize(cameraViewUp);

      // Get distance in pixels between two points at unit distance above and below the focal point
      renderer->SetWorldPoint(cameraFP[0] + cameraViewUp[0], cameraFP[1] + cameraViewUp[1], cameraFP[2] + cameraViewUp[2], cameraFP[3]);
      renderer->WorldToDisplay();
      double topCenter[3] = { 0 };
      renderer->GetDisplayPoint(topCenter);
      renderer->SetWorldPoint(cameraFP[0] - cameraViewUp[0], cameraFP[1] - cameraViewUp[1], cameraFP[2] - cameraViewUp[2], cameraFP[3]);
      renderer->WorldToDisplay();
      double bottomCenter[3] = { 0 };
      renderer->GetDisplayPoint(bottomCenter);
      double distInPixels = sqrt(vtkMath::Distance2BetweenPoints(topCenter, bottomCenter));

      // 2.0 = 2x length of viewUp vector in mm (because viewUp is unit vector)
      mmPerPixel = 2.0 / distInPixels;
    }
  }
  double brushRelativeDiameter = q->doubleParameter("BrushRelativeDiameter");
  double newBrushAbsoluteDiameter = screenSizePixel * (brushRelativeDiameter / 100.0) * mmPerPixel;

  double brushAbsoluteDiameter = q->doubleParameter("BrushAbsoluteDiameter");
  if (brushAbsoluteDiameter > 0 && fabs(newBrushAbsoluteDiameter - brushAbsoluteDiameter) / brushAbsoluteDiameter < 0.01)
  {
    // no brush size change
    return;
  }
  q->setCommonParameter("BrushAbsoluteDiameter", newBrushAbsoluteDiameter);

  if (this->ActiveViewWidget)
  {
    qSlicerSegmentEditorAbstractEffect::scheduleRender(this->ActiveViewWidget);
  }
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffectPrivate::updateBrushModel(qMRMLWidget* viewWidget, double brushPosition_World[3])
{
  Q_Q(qSlicerSegmentEditorPaintEffect);
  this->updateAbsoluteBrushDiameter();

  double diameterMm = q->doubleParameter("BrushAbsoluteDiameter");

  qMRMLSliceWidget* sliceWidget = qobject_cast<qMRMLSliceWidget*>(viewWidget);
  if (!sliceWidget || q->integerParameter("BrushSphere"))
  {
    this->BrushSphereSource->SetRadius(diameterMm / 2.0);
    this->BrushSphereSource->SetPhiResolution(32);
    this->BrushSphereSource->SetThetaResolution(32);
    this->BrushToWorldOriginTransformer->SetInputConnection(this->BrushSphereSource->GetOutputPort());
  }
  else
  {
    this->BrushCylinderSource->SetRadius(diameterMm / 2.0);
    this->BrushCylinderSource->SetResolution(32);
    double sliceSpacingMm = qSlicerSegmentEditorAbstractEffect::sliceSpacing(sliceWidget);
    this->BrushCylinderSource->SetHeight(sliceSpacingMm);
    this->BrushToWorldOriginTransformer->SetInputConnection(this->BrushCylinderSource->GetOutputPort());
  }

  vtkNew<vtkMatrix4x4> brushToWorldOriginTransformMatrix;
  if (sliceWidget)
  {
    // brush is rotated to the slice widget plane
    brushToWorldOriginTransformMatrix->DeepCopy(sliceWidget->sliceLogic()->GetSliceNode()->GetSliceToRAS());
    brushToWorldOriginTransformMatrix->SetElement(0, 3, 0);
    brushToWorldOriginTransformMatrix->SetElement(1, 3, 0);
    brushToWorldOriginTransformMatrix->SetElement(2, 3, 0);
  }
  this->BrushToWorldOriginTransform->Identity();
  this->BrushToWorldOriginTransform->Concatenate(brushToWorldOriginTransformMatrix.GetPointer());
  this->BrushToWorldOriginTransform->RotateX(90); // cylinder's long axis is the Y axis, we need to rotate it to Z axis

  if (brushPosition_World)
  {
    this->WorldOriginToWorldTransform->Identity();
    this->WorldOriginToWorldTransform->Translate(brushPosition_World);
    this->BrushPosition_World[0] = brushPosition_World[0];
    this->BrushPosition_World[1] = brushPosition_World[1];
    this->BrushPosition_World[2] = brushPosition_World[2];
    this->BrushPositionValid = true;
  }
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffectPrivate::updateBrush(qMRMLWidget* viewWidget, BrushPipeline* pipeline)
{
  Q_Q(qSlicerSegmentEditorPaintEffect);
  qMRMLSliceWidget* sliceWidget = qobject_cast<qMRMLSliceWidget*>(viewWidget);
  if (this->BrushToWorldOriginTransformer->GetNumberOfInputConnections(0) == 0 //
      || q->integerParameter("BrushPixelMode"))
  {
    pipeline->SetBrushVisibility(false);
    if (sliceWidget)
    {
      this->updateBrushSizeHud(viewWidget, pipeline);
    }
    return;
  }
  if (sliceWidget)
  {
    pipeline->SetBrushVisibility(this->ActiveViewWidget != nullptr);
  }
  else
  {
    // The brush is shown in 3D views only when 3D views can be edited: rendering 3D views
    // at every mouse move while painting in slice views would make hovering slow.
    pipeline->SetBrushVisibility(this->ActiveViewWidget != nullptr && q->integerParameter("EditIn3DViews"));
  }

  if (sliceWidget)
  {
    // Update slice cutting plane position and orientation
    vtkMRMLSliceNode* sliceNode = sliceWidget->sliceLogic()->GetSliceNode();
    vtkMatrix4x4* sliceXyToRas = sliceNode->GetXYToRAS();
    pipeline->SlicePlane->SetNormal(sliceXyToRas->GetElement(0, 2), sliceXyToRas->GetElement(1, 2), sliceXyToRas->GetElement(2, 2));
    pipeline->SlicePlane->SetOrigin(sliceXyToRas->GetElement(0, 3), sliceXyToRas->GetElement(1, 3), sliceXyToRas->GetElement(2, 3));

    vtkNew<vtkMatrix4x4> rasToSliceXy;
    vtkMatrix4x4::Invert(sliceXyToRas, rasToSliceXy.GetPointer());
    pipeline->WorldToSliceTransform->SetMatrix(rasToSliceXy.GetPointer());

    BrushPipeline2D* pipeline2D = dynamic_cast<BrushPipeline2D*>(pipeline);
    if (pipeline2D)
    {
      vtkNew<vtkMatrix4x4> sliceToXy;
      vtkMatrix4x4::Invert(sliceNode->GetXYToSlice(), sliceToXy.GetPointer());
      pipeline2D->SliceToXYTransform->SetMatrix(sliceToXy.GetPointer());
    }
    this->updateBrushSizeHud(viewWidget, pipeline);
  }
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffectPrivate::updateBrushes()
{
  Q_Q(qSlicerSegmentEditorPaintEffect);
  // unusedWidgetPipelines will contain those widget pointers that are not in the layout anymore
  QList<qMRMLWidget*> unusedWidgetPipelines = this->BrushPipelines.keys();
  qSlicerLayoutManager* layoutManager = qSlicerApplication::application()->layoutManager();

  // Only render the views where the brush or the stroke feedback may change what is shown:
  // other slice views show the brush (or new stroke feedback) only where it intersects their slice plane.
  double brushRadius = this->brushBoundingRadius();
  bool newFeedbackShown = this->IsPainting && this->DelayedPaint && this->BrushPositionValid;
  for (const QString& sliceViewName : layoutManager->sliceViewNames())
  {
    qMRMLSliceWidget* sliceWidget = layoutManager->sliceWidget(sliceViewName);
    if (!q->segmentationDisplayableInView(sliceWidget->mrmlSliceNode()))
    {
      continue;
    }
    unusedWidgetPipelines.removeOne(sliceWidget); // not an orphan

    BrushPipeline* brushPipeline = this->brushForWidget(sliceWidget);
    bool brushWasVisible = brushPipeline->GetBrushVisibility();
    this->updateBrush(sliceWidget, brushPipeline);
    bool contentShown = false;
    if (this->BrushPositionValid)
    {
      double distance = signedDistanceToPlane(brushPipeline->SlicePlane, this->BrushPosition_World);
      contentShown = brushPipeline->GetBrushVisibility() && fabs(distance) < brushRadius;
      if (!contentShown && newFeedbackShown)
      {
        // Feedback of the new stroke points (between the previous and current brush positions)
        double previousDistance = signedDistanceToPlane(brushPipeline->SlicePlane, this->PreviousBrushPosition_World);
        contentShown = fabs(previousDistance) < brushRadius || (previousDistance < 0) != (distance < 0);
      }
    }
    if (sliceWidget == this->ActiveViewWidget || brushWasVisible != brushPipeline->GetBrushVisibility() //
        || contentShown || brushPipeline->ContentShownInLastRender)
    {
      qSlicerSegmentEditorAbstractEffect::scheduleRender(sliceWidget);
      brushPipeline->ContentShownInLastRender = contentShown || sliceWidget == this->ActiveViewWidget;
    }
  }
  for (int threeDViewId = 0; threeDViewId < layoutManager->threeDViewCount(); ++threeDViewId)
  {
    qMRMLThreeDWidget* threeDWidget = layoutManager->threeDWidget(threeDViewId);
    if (!q->segmentationDisplayableInView(threeDWidget->mrmlViewNode()))
    {
      continue;
    }
    unusedWidgetPipelines.removeOne(threeDWidget); // not an orphan

    BrushPipeline* brushPipeline = this->brushForWidget(threeDWidget);
    bool brushWasVisible = brushPipeline->GetBrushVisibility();
    this->updateBrush(threeDWidget, brushPipeline);
    // The brush is visible in 3D views only if editing in 3D views is enabled
    if (threeDWidget == this->ActiveViewWidget || brushWasVisible != brushPipeline->GetBrushVisibility() || brushPipeline->GetBrushVisibility())
    {
      qSlicerSegmentEditorAbstractEffect::scheduleRender(threeDWidget);
    }
  }
  if (this->BrushPositionValid)
  {
    this->PreviousBrushPosition_World[0] = this->BrushPosition_World[0];
    this->PreviousBrushPosition_World[1] = this->BrushPosition_World[1];
    this->PreviousBrushPosition_World[2] = this->BrushPosition_World[2];
  }

  for (qMRMLWidget* const viewWidget : unusedWidgetPipelines)
  {
    BrushPipeline* pipeline = this->BrushPipelines[viewWidget];
    BrushPipeline2D* pipeline2D = dynamic_cast<BrushPipeline2D*>(pipeline);
    BrushPipeline3D* pipeline3D = dynamic_cast<BrushPipeline3D*>(pipeline);
    if (pipeline2D)
    {
      q->removeActor2D(viewWidget, pipeline2D->BrushActor);
      q->removeActor2D(viewWidget, pipeline2D->FeedbackActor);
      q->removeActor2D(viewWidget, pipeline2D->HudActor);
    }
    else if (pipeline3D)
    {
      q->removeActor3D(viewWidget, pipeline3D->BrushActor);
      q->removeActor3D(viewWidget, pipeline3D->FeedbackActor);
    }
    delete pipeline;
    this->BrushPipelines.remove(viewWidget);
  }
}

//-----------------------------------------------------------------------------
double qSlicerSegmentEditorPaintEffectPrivate::brushBoundingRadius()
{
  Q_Q(qSlicerSegmentEditorPaintEffect);
  double radius = q->doubleParameter("BrushAbsoluteDiameter") / 2.0;
  qMRMLSliceWidget* sliceWidget = qobject_cast<qMRMLSliceWidget*>(this->ActiveViewWidget);
  if (sliceWidget && !q->integerParameter("BrushSphere"))
  {
    // cylinder, its height is the slice spacing
    double halfHeight = qSlicerSegmentEditorAbstractEffect::sliceSpacing(sliceWidget) / 2.0;
    radius = sqrt(radius * radius + halfHeight * halfHeight);
  }
  // Small margin for the tessellation of the brush model
  return radius * 1.01 + 1e-3;
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffectPrivate::resetFeedback()
{
  this->FeedbackPolyData3D->Initialize();
  this->FeedbackPolyData3D->Modified();
  this->FeedbackPolyData2D->Initialize();
  this->FeedbackPolyData2D->Modified();
  this->FeedbackPointCount = 0;
}

namespace
{
//-----------------------------------------------------------------------------
/// Append the cells of source (with point ids shifted by pointOffset) to target, or to a new cell array if target is nullptr
vtkSmartPointer<vtkCellArray> appendCells(vtkCellArray* target, vtkCellArray* source, vtkIdType pointOffset)
{
  vtkSmartPointer<vtkCellArray> cells = target;
  if (!cells)
  {
    cells = vtkSmartPointer<vtkCellArray>::New();
  }
  cells->Append(source, pointOffset);
  return cells;
}

//-----------------------------------------------------------------------------
/// Append points, point data and cells of source to target (in place, amortized cost proportional to the size of source)
void appendPolyDataInPlace(vtkPolyData* target, vtkPolyData* source)
{
  if (!source || source->GetNumberOfPoints() == 0)
  {
    return;
  }
  if (target->GetNumberOfPoints() == 0 || !target->GetPoints())
  {
    target->DeepCopy(source);
    return;
  }
  vtkIdType pointOffset = target->GetNumberOfPoints();
  vtkIdType numberOfNewPoints = source->GetNumberOfPoints();
  target->GetPoints()->InsertPoints(pointOffset, numberOfNewPoints, 0, source->GetPoints());
  // Point data arrays that are not in the source are removed to keep the arrays consistent with the points
  vtkPointData* targetPointData = target->GetPointData();
  for (int arrayIndex = targetPointData->GetNumberOfArrays() - 1; arrayIndex >= 0; --arrayIndex)
  {
    vtkAbstractArray* targetArray = targetPointData->GetAbstractArray(arrayIndex);
    vtkAbstractArray* sourceArray = targetArray->GetName() ? source->GetPointData()->GetAbstractArray(targetArray->GetName()) : nullptr;
    if (!sourceArray || sourceArray->GetNumberOfComponents() != targetArray->GetNumberOfComponents())
    {
      targetPointData->RemoveArray(arrayIndex);
      continue;
    }
    targetArray->InsertTuples(pointOffset, numberOfNewPoints, 0, sourceArray);
  }
  // vtkPolyData returns a shared empty dummy array for missing cell types, it must not be modified
  if (source->GetNumberOfVerts() > 0)
  {
    target->SetVerts(appendCells(target->GetNumberOfVerts() > 0 ? target->GetVerts() : nullptr, source->GetVerts(), pointOffset));
  }
  if (source->GetNumberOfLines() > 0)
  {
    target->SetLines(appendCells(target->GetNumberOfLines() > 0 ? target->GetLines() : nullptr, source->GetLines(), pointOffset));
  }
  if (source->GetNumberOfPolys() > 0)
  {
    target->SetPolys(appendCells(target->GetNumberOfPolys() > 0 ? target->GetPolys() : nullptr, source->GetPolys(), pointOffset));
  }
  if (source->GetNumberOfStrips() > 0)
  {
    target->SetStrips(appendCells(target->GetNumberOfStrips() > 0 ? target->GetStrips() : nullptr, source->GetStrips(), pointOffset));
  }
  target->DeleteCells(); // the cell map is rebuilt on demand
  target->Modified();
}
} // namespace

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffectPrivate::addFeedbackPoints()
{
  Q_Q(qSlicerSegmentEditorPaintEffect);
  vtkIdType numberOfPoints = this->PaintCoordinates_World->GetNumberOfPoints();
  if (numberOfPoints <= this->FeedbackPointCount)
  {
    return;
  }
  vtkNew<vtkPoints> newPoints_World;
  for (vtkIdType pointIndex = this->FeedbackPointCount; pointIndex < numberOfPoints; ++pointIndex)
  {
    newPoints_World->InsertNextPoint(this->PaintCoordinates_World->GetPoint(pointIndex));
  }
  this->FeedbackPointCount = numberOfPoints;

  // Brush glyphs in world coordinates (shown in 3D views and cut by the other slice views).
  // Only the new points are glyphed, the result is appended to the feedback polydata.
  this->FeedbackNewPoints3D->SetPoints(newPoints_World);
  this->FeedbackNewPoints3D->Modified();
  this->FeedbackGlyph3D->Update();
  appendPolyDataInPlace(this->FeedbackPolyData3D, this->FeedbackGlyph3D->GetOutput());

  // Circles in the slice view that the stroke is painted in (the stroke points are in its slice plane)
  if (this->FeedbackViewWidget)
  {
    vtkNew<vtkTransform> rasToSlice;
    rasToSlice->SetMatrix(this->FeedbackRasToSlice);
    vtkNew<vtkPoints> newPoints_Slice;
    rasToSlice->TransformPoints(newPoints_World, newPoints_Slice);
    this->FeedbackNewPoints2D->SetPoints(newPoints_Slice);
    this->FeedbackNewPoints2D->Modified();
    this->FeedbackCircleSource->SetRadius(q->doubleParameter("BrushAbsoluteDiameter") / 2.0);
    this->FeedbackGlyph2D->Update();
    appendPolyDataInPlace(this->FeedbackPolyData2D, this->FeedbackGlyph2D->GetOutput());
  }
}

//-----------------------------------------------------------------------------
bool qSlicerSegmentEditorPaintEffectPrivate::bracketKeysResizeBrush() const
{
  qSlicerApplication* app = qSlicerApplication::application();
  if (!app || app->testAttribute(qSlicerCoreApplication::AA_EnableTesting) || !app->mainWindow())
  {
    // Under testing and without main window (classic behavior), [ and ] keep cycling the volumes
    // of the slice view, whatever the settings say
    return false;
  }
  QSettings* settings = app->userSettings();
  return settings ? settings->value("Studio/BracketKeysResizeBrush", true).toBool() : true;
}

//-----------------------------------------------------------------------------
bool qSlicerSegmentEditorPaintEffectPrivate::brushSizeHudEnabled() const
{
  // Views are unchanged under testing and without main window (classic behavior)
  qSlicerApplication* app = qSlicerApplication::application();
  return app && !app->testAttribute(qSlicerCoreApplication::AA_EnableTesting) && app->mainWindow();
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffectPrivate::showBrushSizeHud()
{
  if (!this->brushSizeHudEnabled())
  {
    return;
  }
  this->BrushSizeHudTimer->start();
  qMRMLSliceWidget* sliceWidget = qobject_cast<qMRMLSliceWidget*>(this->ActiveViewWidget);
  if (!sliceWidget || !this->BrushPipelines.contains(sliceWidget))
  {
    return;
  }
  this->updateBrushSizeHud(sliceWidget, this->BrushPipelines[sliceWidget]);
  qSlicerSegmentEditorAbstractEffect::scheduleRender(sliceWidget);
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffectPrivate::updateBrushSizeHud(qMRMLWidget* viewWidget, BrushPipeline* pipeline)
{
  Q_Q(qSlicerSegmentEditorPaintEffect);
  BrushPipeline2D* pipeline2D = dynamic_cast<BrushPipeline2D*>(pipeline);
  qMRMLSliceWidget* sliceWidget = qobject_cast<qMRMLSliceWidget*>(viewWidget);
  if (!pipeline2D || !sliceWidget)
  {
    return;
  }
  bool show = this->brushSizeHudEnabled() && (this->ShiftKeyHeld || this->BrushSizeHudTimer->isActive()) //
              && viewWidget == this->ActiveViewWidget && this->BrushPositionValid && pipeline2D->GetBrushVisibility();
  if (!show)
  {
    pipeline2D->HudActor->SetVisibility(false);
    return;
  }

  double diameterMm = q->doubleParameter("BrushAbsoluteDiameter");
  QString text = QString(QChar(0x00D8)) + " " + QString::number(diameterMm, 'f', diameterMm >= 1.0 ? 1 : 2) + " mm";
  pipeline2D->HudActor->SetInput(text.toUtf8().constData());

  // Place the text next to the brush (on its left if there is no room on the right)
  vtkMRMLSliceNode* sliceNode = sliceWidget->sliceLogic()->GetSliceNode();
  vtkMatrix4x4* xyToSlice = sliceNode->GetXYToSlice();
  double mmPerPixel = sqrt(xyToSlice->GetElement(0, 0) * xyToSlice->GetElement(0, 0) + xyToSlice->GetElement(1, 0) * xyToSlice->GetElement(1, 0)
                           + xyToSlice->GetElement(2, 0) * xyToSlice->GetElement(2, 0));
  double radiusPixel = (mmPerPixel > 0.0 ? diameterMm / 2.0 / mmPerPixel : 0.0);
  double brushPosition_XY[3] = { 0.0, 0.0, 0.0 };
  pipeline2D->WorldToSliceTransform->TransformPoint(this->BrushPosition_World, brushPosition_XY);
  double scale = sliceWidget->sliceView() ? sliceWidget->sliceView()->devicePixelRatioF() : 1.0;
  vtkTextProperty* textProperty = pipeline2D->HudActor->GetTextProperty();
  textProperty->SetFontSize(static_cast<int>(12 * scale + 0.5));
  double gap = 8.0 * scale;
  double textWidthEstimate = 80.0 * scale;
  int* viewSize = sliceNode->GetDimensions();
  if (brushPosition_XY[0] + radiusPixel + gap + textWidthEstimate <= viewSize[0])
  {
    textProperty->SetJustificationToLeft();
    pipeline2D->HudActor->SetPosition(brushPosition_XY[0] + radiusPixel + gap, brushPosition_XY[1]);
  }
  else
  {
    textProperty->SetJustificationToRight();
    pipeline2D->HudActor->SetPosition(brushPosition_XY[0] - radiusPixel - gap, brushPosition_XY[1]);
  }
  pipeline2D->HudActor->SetVisibility(true);
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffectPrivate::hideBrushSizeHudIfExpired()
{
  if (this->ShiftKeyHeld || this->BrushSizeHudTimer->isActive())
  {
    return;
  }
  for (QMap<qMRMLWidget*, BrushPipeline*>::iterator it = this->BrushPipelines.begin(); it != this->BrushPipelines.end(); ++it)
  {
    BrushPipeline2D* pipeline2D = dynamic_cast<BrushPipeline2D*>(it.value());
    if (pipeline2D && pipeline2D->HudActor->GetVisibility())
    {
      pipeline2D->HudActor->SetVisibility(false);
      qSlicerSegmentEditorAbstractEffect::scheduleRender(it.key());
    }
  }
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffectPrivate::onBrushSizeHudTimeout()
{
  this->hideBrushSizeHudIfExpired();
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffectPrivate::clearBrushPipelines()
{
  Q_Q(qSlicerSegmentEditorPaintEffect);

  // On exiting Slicer, this method (triggered by deactivation of the effect when exiting the
  // module containing the segment editor widget) may be called after the layout was destroyed
  bool removeActors = true;
  if (!qSlicerApplication::application() || !qSlicerApplication::application()->layoutManager())
  {
    removeActors = false;
  }

  QMapIterator<qMRMLWidget*, BrushPipeline*> it(this->BrushPipelines);
  while (it.hasNext())
  {
    it.next();
    BrushPipeline* brushPipeline = it.value();
    if (removeActors)
    {
      qMRMLWidget* viewWidget = it.key();
      BrushPipeline2D* pipeline2D = dynamic_cast<BrushPipeline2D*>(brushPipeline);
      BrushPipeline3D* pipeline3D = dynamic_cast<BrushPipeline3D*>(brushPipeline);
      if (pipeline2D)
      {
        q->removeActor2D(viewWidget, pipeline2D->BrushActor);
        q->removeActor2D(viewWidget, pipeline2D->FeedbackActor);
        q->removeActor2D(viewWidget, pipeline2D->HudActor);
      }
      else if (pipeline3D)
      {
        q->removeActor3D(viewWidget, pipeline3D->BrushActor);
        q->removeActor3D(viewWidget, pipeline3D->FeedbackActor);
      }
      qSlicerSegmentEditorAbstractEffect::scheduleRender(viewWidget);
    }
    delete brushPipeline;
  }
  this->BrushPipelines.clear();
  this->BrushModelUpToDate = false;
}

//-----------------------------------------------------------------------------
std::string qSlicerSegmentEditorPaintEffectPrivate::segmentAtPosition(qMRMLWidget* viewWidget, double* ras)
{
  Q_Q(qSlicerSegmentEditorPaintEffect);
  if (!q->parameterSetNode())
  {
    return "";
  }
  vtkMRMLSegmentationNode* segmentationNode = q->parameterSetNode()->GetSegmentationNode();
  if (!segmentationNode)
  {
    return "";
  }
  std::string selectedSegmentID = (q->parameterSetNode()->GetSelectedSegmentID() ? q->parameterSetNode()->GetSelectedSegmentID() : "");
  qMRMLSliceWidget* sliceWidget = qobject_cast<qMRMLSliceWidget*>(viewWidget);
  if (!sliceWidget)
  {
    // segment position can only be obtained from slice views
    return selectedSegmentID;
  }
  if (!sliceWidget->sliceView())
  {
    return selectedSegmentID;
  }

  vtkSlicerApplicationLogic* appLogic = qSlicerApplication::application()->applicationLogic();
  if (!appLogic)
  {
    return selectedSegmentID;
  }

  // Get slice displayable manager
  vtkMRMLAbstractViewNode* viewNode = sliceWidget->mrmlSliceNode();
  vtkMRMLSegmentationsDisplayableManager2D* segmentationDisplayableManager2D =
    vtkMRMLSegmentationsDisplayableManager2D::SafeDownCast(appLogic->GetViewDisplayableManagerByClassName(viewNode, "vtkMRMLSegmentationsDisplayableManager2D"));
  if (!segmentationDisplayableManager2D)
  {
    return selectedSegmentID;
  }
  std::string newSelectedSegmentID;
  for (int displayNodeIndex = 0; displayNodeIndex < segmentationNode->GetNumberOfDisplayNodes(); displayNodeIndex++)
  {
    vtkMRMLSegmentationDisplayNode* displayNode = vtkMRMLSegmentationDisplayNode::SafeDownCast(segmentationNode->GetNthDisplayNode(displayNodeIndex));
    if (!displayNode)
    {
      continue;
    }
    vtkNew<vtkStringArray> segmentIDs;
    segmentationDisplayableManager2D->GetVisibleSegmentsForPosition(ras, displayNode, segmentIDs.GetPointer());
    if (segmentIDs->GetNumberOfValues() == 0)
    {
      continue;
    }
    if (!selectedSegmentID.empty() && segmentIDs->LookupValue(selectedSegmentID) >= 0)
    {
      // current segment ID is at the current position, don't change it
      return selectedSegmentID;
    }
    // Don't return immediately if a segment ID is found, because via other display nodes
    // it is still possible that the current segment is visible at this position.
    newSelectedSegmentID = segmentIDs->GetValue(0);
  }
  return newSelectedSegmentID;
}
//-----------------------------------------------------------------------------

//-----------------------------------------------------------------------------
// qSlicerSegmentEditorPaintEffect methods

//----------------------------------------------------------------------------
qSlicerSegmentEditorPaintEffect::qSlicerSegmentEditorPaintEffect(QObject* parent)
  : Superclass(parent)
  , d_ptr(new qSlicerSegmentEditorPaintEffectPrivate(*this))
{
  this->m_Name = QString(/*no tr*/ "Paint");
  this->m_Title = tr("Paint");
  this->m_AlwaysErase = false;
  this->m_Erase = false;
  this->m_ShowEffectCursorInThreeDView = true;
}

//----------------------------------------------------------------------------
qSlicerSegmentEditorPaintEffect::~qSlicerSegmentEditorPaintEffect() = default;

//---------------------------------------------------------------------------
QIcon qSlicerSegmentEditorPaintEffect::icon()
{
  Q_D(qSlicerSegmentEditorPaintEffect);

  return d->PaintIcon;
}

//---------------------------------------------------------------------------
const QString qSlicerSegmentEditorPaintEffect::helpText() const
{
  return QString("<html>")
         + tr("Paint with a round brush<br>."
              "<p><ul style=\"margin: 0\">"
              "<li><b>Left-button drag-and-drop:</b> paint strokes."
              "<li><b>Shift + mouse wheel</b> or <b>+/- keys:</b> adjust brush size."
              "<li><b>Ctrl + mouse wheel:</b> slice view zoom in/out."
              "</ul><p>"
              "Editing is available both in slice and 3D views."
              "<p>");
}

//-----------------------------------------------------------------------------
qSlicerSegmentEditorAbstractEffect* qSlicerSegmentEditorPaintEffect::clone()
{
  return new qSlicerSegmentEditorPaintEffect();
}

//---------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffect::deactivate()
{
  Q_D(qSlicerSegmentEditorPaintEffect);
  Superclass::deactivate();
  d->IsPainting = false;
  d->PaintCoordinates_World->Reset();
  d->resetFeedback();
  d->clearBrushPipelines();
  d->FeedbackViewWidget = nullptr;
  d->ActiveViewWidget = nullptr;
  d->BrushSizeHudTimer->stop();
  d->ShiftKeyHeld = false;
  d->BrushSizeParametersInitialized = false;
}

//---------------------------------------------------------------------------
bool qSlicerSegmentEditorPaintEffectPrivate::brushPositionInWorld(qMRMLWidget* viewWidget, int brushPositionInView[2], double brushPosition_World[3])
{
  // This effect only supports interactions in the 2D slice views currently
  qMRMLSliceWidget* sliceWidget = qobject_cast<qMRMLSliceWidget*>(viewWidget);
  qMRMLThreeDWidget* threeDWidget = qobject_cast<qMRMLThreeDWidget*>(viewWidget);

  if (sliceWidget)
  {
    double eventPositionXY[4] = { static_cast<double>(brushPositionInView[0]), static_cast<double>(brushPositionInView[1]), 0.0, 1.0 };
    sliceWidget->sliceLogic()->GetSliceNode()->GetXYToRAS()->MultiplyPoint(eventPositionXY, brushPosition_World);
  }
  else if (threeDWidget)
  {
    vtkRenderer* renderer = qSlicerSegmentEditorAbstractEffect::renderer(viewWidget);
    if (!renderer)
    {
      return false;
    }
    static bool useCellPicker = true;
    if (useCellPicker)
    {
      // vtkMRMLAccuratePicker also picks surfaces that have no polygonal geometry
      // (segmentations shown as binary labelmap in 3D views)
      vtkNew<vtkMRMLAccuratePicker> picker;
      picker->SetTolerance(.005);
      if (!picker->Pick(brushPositionInView[0], brushPositionInView[1], 0, renderer))
      {
        return false;
      }

      vtkPoints* pickPositions = picker->GetPickedPositions();
      int numberOfPickedPositions = pickPositions->GetNumberOfPoints();
      if (numberOfPickedPositions < 1)
      {
        return false;
      }
      double cameraPosition[3] = { 0, 0, 0 };
      renderer->GetActiveCamera()->GetPosition(cameraPosition);
      pickPositions->GetPoint(0, brushPosition_World);
      double minDist2 = vtkMath::Distance2BetweenPoints(brushPosition_World, cameraPosition);
      for (int i = 1; i < numberOfPickedPositions; i++)
      {
        double currentMinDist2 = vtkMath::Distance2BetweenPoints(pickPositions->GetPoint(i), cameraPosition);
        if (currentMinDist2 < minDist2)
        {
          pickPositions->GetPoint(i, brushPosition_World);
          minDist2 = currentMinDist2;
        }
      }
    }
    else
    {
      vtkNew<vtkPropPicker> picker;
      // vtkNew<vtkWorldPointPicker> picker;
      if (!picker->Pick(brushPositionInView[0], brushPositionInView[1], 0, renderer))
      {
        return false;
      }
      picker->GetPickPosition(brushPosition_World);
    }
  }

  return true;
}

//---------------------------------------------------------------------------
bool qSlicerSegmentEditorPaintEffect::processInteractionEvents(vtkRenderWindowInteractor* callerInteractor, unsigned long eid, qMRMLWidget* viewWidget)
{
  Q_D(qSlicerSegmentEditorPaintEffect);

  qMRMLSliceWidget* sliceWidget = qobject_cast<qMRMLSliceWidget*>(viewWidget);
  qMRMLThreeDWidget* threeDWidget = qobject_cast<qMRMLThreeDWidget*>(viewWidget);

  if (threeDWidget && !this->integerParameter("EditIn3DViews"))
  {
    // interacting in a 3D view and interaction in 3D views is disabled
    return false;
  }

  bool shiftKeyPressed = callerInteractor->GetShiftKey();
  bool anyModifierKeyPressed = (callerInteractor->GetShiftKey() || callerInteractor->GetControlKey() || callerInteractor->GetAltKey());

  if (d->ShiftKeyHeld && eid != vtkCommand::KeyPressEvent && eid != vtkCommand::KeyReleaseEvent && (!shiftKeyPressed || eid == vtkCommand::LeaveEvent))
  {
    // The Shift key release may have been received by another widget (focus changed while Shift was held).
    // Mouse events carry the actual modifier state.
    d->ShiftKeyHeld = false;
    d->hideBrushSizeHudIfExpired();
  }

  // Process events that do not provide event position (or we don't need event position)
  double scaleDiameterRequested = -1.0; // <0 means no scale change is requested
  const double zoomFactor = 0.2;
  if (eid == vtkCommand::KeyPressEvent)
  {
    const char* key = callerInteractor->GetKeySym();
    if (!key)
    {
      return false;
    }
    if (!strcmp(key, "plus") || !strcmp(key, "equal"))
    {
      scaleDiameterRequested = (1.0 + zoomFactor);
    }
    else if (!strcmp(key, "minus") || !strcmp(key, "underscore"))
    {
      scaleDiameterRequested = (1.0 - zoomFactor);
    }
    else if ((!strcmp(key, "bracketright") || !strcmp(key, "bracketleft")) && d->bracketKeysResizeBrush())
    {
      // When not enabled, [ and ] are left to the slice view (cycle the volumes)
      scaleDiameterRequested = (!strcmp(key, "bracketright") ? (1.0 + zoomFactor) : (1.0 - zoomFactor));
    }
    else
    {
      if (!strcmp(key, "Shift_L") || !strcmp(key, "Shift_R"))
      {
        // Show the brush size while Shift is held (Shift + mouse wheel changes the brush size)
        if (!d->ShiftKeyHeld)
        {
          d->ShiftKeyHeld = true;
          d->showBrushSizeHud();
        }
      }
      return false;
    }
  }
  else if (eid == vtkCommand::KeyReleaseEvent)
  {
    const char* key = callerInteractor->GetKeySym();
    if (key && (!strcmp(key, "Shift_L") || !strcmp(key, "Shift_R")) && d->ShiftKeyHeld)
    {
      d->ShiftKeyHeld = false;
      d->hideBrushSizeHudIfExpired();
    }
    return false;
  }
  else if (eid == vtkCommand::MouseWheelForwardEvent || eid == vtkCommand::MouseWheelLeftEvent)
  {
    if (shiftKeyPressed)
    {
      scaleDiameterRequested = (1.0 + zoomFactor);
    }
    else
    {
      return false;
    }
  }
  else if (eid == vtkCommand::MouseWheelBackwardEvent || eid == vtkCommand::MouseWheelRightEvent)
  {
    if (shiftKeyPressed)
    {
      scaleDiameterRequested = (1.0 - zoomFactor);
    }
    else
    {
      return false;
    }
  }

  if (scaleDiameterRequested > 0)
  {
    d->scaleDiameter(scaleDiameterRequested);
    d->updateBrushModel(d->ActiveViewWidget, nullptr);
    if (d->IsPainting)
    {
      // The feedback of the stroke shows the new brush size at every point of the stroke, as it will be painted
      d->resetFeedback();
      d->addFeedbackPoints();
    }
    d->showBrushSizeHud();
    // Only schedule render done force immediate update.
    // When zooming slice in/out then immediate rendering would produce jitter, because
    // brush size would be updated immediately and then slice position would be updated shortly after.
    qSlicerSegmentEditorAbstractEffect::scheduleRender(viewWidget);
    return true; // abortEvent
  }

  BrushPipeline* brushPipeline = nullptr;
  if (sliceWidget)
  {
    brushPipeline = d->brushForWidget(sliceWidget);
  }
  else if (threeDWidget)
  {
    brushPipeline = d->brushForWidget(threeDWidget);
  }
  if (!brushPipeline)
  {
    qCritical() << Q_FUNC_INFO << ": Failed to create brushPipeline";
    return false;
  }

  bool sameActiveView = (d->ActiveViewWidget == viewWidget);
  if (d->ActiveViewWidget == nullptr && viewWidget != nullptr)
  {
    // Mouse pointer entered the widget
    // It is more reliable to check for viewWidget pointer change than vtkCommand::EnterEvent, because
    // the effect may be activated using keyboard shortcut while the mouse pointer is already inside the widget.
    brushPipeline->SetBrushVisibility(!this->integerParameter("BrushPixelMode"));
  }
  d->ActiveViewWidget = viewWidget;

  int eventPosition[2] = { 0, 0 };
  callerInteractor->GetEventPosition(eventPosition);
  if (eid == vtkCommand::MouseMoveEvent && !d->IsPainting && d->BrushModelUpToDate && sameActiveView //
      && eventPosition[0] == d->ActiveViewLastInteractionPosition[0] && eventPosition[1] == d->ActiveViewLastInteractionPosition[1])
  {
    // The mouse moved less than a pixel, the brush is already up to date
    return false;
  }
  d->ActiveViewLastInteractionPosition[0] = eventPosition[0];
  d->ActiveViewLastInteractionPosition[1] = eventPosition[1];

  double brushPosition_World[4] = { 0.0, 0.0, 0.0, 1.0 };
  bool worldPositionValid = d->brushPositionInWorld(viewWidget, eventPosition, brushPosition_World);

  bool abortEvent = false;

  if (worldPositionValid && eid == vtkCommand::LeftButtonPressEvent && !anyModifierKeyPressed)
  {
    if (this->m_AlwaysErase)
    {
      // Erase effect
      this->m_Erase = true;
    }
    else
    {
      // Paint effect
      std::string selectedSegmentId = (this->parameterSetNode()->GetSelectedSegmentID() ? this->parameterSetNode()->GetSelectedSegmentID() : "");
      if (this->integerParameter("ColorSmudge"))
      {
        std::string paintSegmentId = d->segmentAtPosition(viewWidget, brushPosition_World);
        if (paintSegmentId.empty())
        {
          // clicked on empty area
          this->m_Erase = true;
        }
        else
        {
          this->parameterSetNode()->SetSelectedSegmentID(paintSegmentId.c_str());
          this->m_Erase = false;
        }
      }
      else
      {
        this->m_Erase = false;
      }
    }
    // Warn the user if current segment is not visible
    int confirmedEditingAllowed = this->confirmCurrentSegmentVisible();
    if (confirmedEditingAllowed == NotConfirmed || confirmedEditingAllowed == ConfirmedWithDialog)
    {
      // If user had to move the mouse to click on the popup, so we cannot continue with painting
      // from the current mouse position. User will need to click again.
      // The dialog is not displayed again for the same segment.

      // The event has to be aborted, because otherwise there would be a LeftButtonPressEvent without a matching
      // LeftButtonReleaseEvent (as the popup window got the release button event) and so the view would be stuck
      // in view rotation mode.
      abortEvent = true;

      return abortEvent;
    }
    d->IsPainting = true;
    if (!this->integerParameter("BrushPixelMode"))
    {
      // this->cursorOff(sliceWidget);
    }
    QList<qMRMLWidget*> viewWidgets = d->BrushPipelines.keys();
    if (!this->parameterSetNode())
    {
      return false;
    }
    // Start the stroke feedback. In the slice view that the stroke is painted in, it is drawn as circles
    // in the slice plane; other views show the brush glyphs (the other slice views cut them).
    d->resetFeedback();
    d->FeedbackViewWidget = sliceWidget;
    if (sliceWidget)
    {
      vtkMatrix4x4::Invert(sliceWidget->sliceLogic()->GetSliceNode()->GetSliceToRAS(), d->FeedbackRasToSlice);
    }
    bool editIn3DViews = this->integerParameter("EditIn3DViews");
    for (qMRMLWidget* const viewWidget : viewWidgets)
    {
      BrushPipeline* pipeline = d->BrushPipelines[viewWidget];
      BrushPipeline2D* pipeline2D = dynamic_cast<BrushPipeline2D*>(pipeline);
      if (pipeline2D)
      {
        pipeline2D->SetFeedbackFromPaintView(viewWidget == d->FeedbackViewWidget);
      }
      pipeline->SetFeedbackVisibility(d->DelayedPaint && (pipeline2D || editIn3DViews));
    }
    // Start point of a paint stroke
    d->LastBrushPosition_World[0] = brushPosition_World[0];
    d->LastBrushPosition_World[1] = brushPosition_World[1];
    d->LastBrushPosition_World[2] = brushPosition_World[2];
    d->LastBrushPositionValid = true;
    d->paintAddPoint(viewWidget, brushPosition_World);
    d->ActiveViewLastPaintPosition[0] = eventPosition[0];
    d->ActiveViewLastPaintPosition[1] = eventPosition[1];
    abortEvent = true;
  }
  else if (eid == vtkCommand::LeftButtonReleaseEvent)
  {
    if (d->IsPainting)
    {
      if (worldPositionValid)
      {
        d->paintAddPoint(viewWidget, brushPosition_World, d->LastBrushPositionValid ? d->LastBrushPosition_World : nullptr);
      }

      // Schedule rendering of all views.
      // Cleaning up pipelines schedules re-rendering as well, but on some Intel video cards, and especially in debug mode,
      // this additional rendering request is necessary for showing the filled segment after paint stroke is completed.
      QList<qMRMLWidget*> viewWidgets = d->BrushPipelines.keys();
      for (qMRMLWidget* const aViewWidget : viewWidgets)
      {
        d->BrushPipelines[aViewWidget]->SetFeedbackVisibility(false);
        d->BrushPipelines[aViewWidget]->SetBrushVisibility(worldPositionValid);
        qSlicerSegmentEditorAbstractEffect::scheduleRender(aViewWidget);
      }

      this->paintApply(viewWidget);
      d->IsPainting = false;
      abortEvent = true;
    }
    // this->cursorOn(sliceWidget);
  }
  else if (eid == vtkCommand::MouseMoveEvent)
  {
    if (worldPositionValid)
    {
      d->updateBrushModel(viewWidget, brushPosition_World);
      d->updateBrushes();
      d->BrushModelUpToDate = true;
      if (d->IsPainting)
      {
        double distance2fromLastInteractionPosition = 0.0;
        if (d->MinimumPaintPointDistance2 > 0.0)
        {
          distance2fromLastInteractionPosition = (d->ActiveViewLastPaintPosition[0] - eventPosition[0]) * (d->ActiveViewLastPaintPosition[0] - eventPosition[0])
                                                 + (d->ActiveViewLastPaintPosition[1] - eventPosition[1]) * (d->ActiveViewLastPaintPosition[1] - eventPosition[1]);
        }

        if (distance2fromLastInteractionPosition >= d->MinimumPaintPointDistance2)
        {
          // add a new paint position
          d->paintAddPoint(viewWidget, brushPosition_World, d->LastBrushPositionValid ? d->LastBrushPosition_World : nullptr);
          d->ActiveViewLastPaintPosition[0] = eventPosition[0];
          d->ActiveViewLastPaintPosition[1] = eventPosition[1];
          // Save this position for connecting next stroke
          d->LastBrushPosition_World[0] = brushPosition_World[0];
          d->LastBrushPosition_World[1] = brushPosition_World[1];
          d->LastBrushPosition_World[2] = brushPosition_World[2];
          d->LastBrushPositionValid = true;
        }
        else
        {
          // too close to previous position, just update the brush position
          qSlicerSegmentEditorAbstractEffect::forceRender(viewWidget);
        }
      }
    }
    else
    {
      // no valid world position (nothing can be picked), hide the brush
      QList<qMRMLWidget*> viewWidgets = d->BrushPipelines.keys();
      for (qMRMLWidget* const aViewWidget : viewWidgets)
      {
        bool brushVisibilityChanged = (d->BrushPipelines[aViewWidget]->GetBrushVisibility() != worldPositionValid);
        if (brushVisibilityChanged)
        {
          d->BrushPipelines[aViewWidget]->SetBrushVisibility(worldPositionValid);
          qSlicerSegmentEditorAbstractEffect::scheduleRender(aViewWidget);
        }
      }
      // make sure this stroke is not connected to the next one
      d->LastBrushPositionValid = false;
      d->BrushModelUpToDate = false;
    }
  }
  else if (eid == vtkCommand::LeaveEvent)
  {
    brushPipeline->SetBrushVisibility(false);
    d->ActiveViewWidget = nullptr;
    d->BrushModelUpToDate = false;
    d->updateBrushModel(viewWidget, nullptr);
    d->updateBrushes();
    // The view is not the active view anymore, but its brush must be hidden
    qSlicerSegmentEditorAbstractEffect::scheduleRender(viewWidget);
    // qSlicerSegmentEditorAbstractEffect::forceRender(viewWidget);
  }

  return abortEvent;
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffect::processViewNodeEvents(vtkMRMLAbstractViewNode* callerViewNode, unsigned long eid, qMRMLWidget* viewWidget)
{
  Q_D(qSlicerSegmentEditorPaintEffect);
  Q_UNUSED(callerViewNode);
  Q_UNUSED(eid);

  // This effect only supports interactions in the 2D slice views currently
  qMRMLSliceWidget* sliceWidget = qobject_cast<qMRMLSliceWidget*>(viewWidget);
  qMRMLThreeDWidget* threeDWidget = qobject_cast<qMRMLThreeDWidget*>(viewWidget);
  if (!sliceWidget && !threeDWidget)
  {
    return;
  }

  BrushPipeline* brushPipeline = d->brushForWidget(viewWidget);
  if (!brushPipeline)
  {
    qCritical() << Q_FUNC_INFO << ": Failed to create brushPipeline!";
    return;
  }

  if (viewWidget == d->ActiveViewWidget)
  {
    double brushPosition_World[4] = { 0.0, 0.0, 0.0, 1.0 };
    if (d->brushPositionInWorld(viewWidget, d->ActiveViewLastInteractionPosition, brushPosition_World))
    {
      d->updateBrushModel(viewWidget, brushPosition_World);
    }
    else
    {
      d->updateBrushModel(viewWidget, nullptr);
    }
    d->updateBrushes();
    qSlicerSegmentEditorAbstractEffect::scheduleRender(d->ActiveViewWidget);
  }

  d->updateBrush(viewWidget, brushPipeline);
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffect::setupOptionsFrame()
{
  Q_D(qSlicerSegmentEditorPaintEffect);

  // Setup widgets corresponding to the parent class of this effect
  Superclass::setupOptionsFrame();

  // We put all options within a frame so that derived classes can reposition it as a whole within their GUI
  d->PaintOptionsFrame = new QFrame();
  QVBoxLayout* paintOptionsLayout = new QVBoxLayout();
  d->PaintOptionsFrame->setLayout(paintOptionsLayout);
  this->addOptionsWidget(d->PaintOptionsFrame);

  // Create options frame for this effect
  d->BrushDiameterFrame = new QFrame();
  QFormLayout* brushFormLayout = new QFormLayout();
  brushFormLayout->setContentsMargins(0, 0, 0, 0);
  d->BrushDiameterFrame->setLayout(brushFormLayout);
  paintOptionsLayout->addWidget(d->BrushDiameterFrame);

  d->BrushDiameterSizeFrame = new QFrame();
  d->BrushDiameterSizeFrame->setLayout(new QHBoxLayout());
  paintOptionsLayout->addWidget(d->BrushDiameterSizeFrame);

  QLabel* diameterLabel = new QLabel(tr("Diameter:"), d->BrushDiameterFrame);
  diameterLabel->setAlignment(Qt::AlignCenter);
  diameterLabel->setToolTip(tr("Set the paint brush size as a percentage of screen height or physical size"));

  d->BrushDiameterIsAbsoluteButton = new QToolButton(d->BrushDiameterSizeFrame);
  d->BrushDiameterIsAbsoluteButton->setCheckable(true);
  d->BrushDiameterIsAbsoluteButton->setText("absolute");
  d->BrushDiameterIsAbsoluteButton->setChecked(false);
  d->BrushDiameterIsAbsoluteButton->setToolTip(tr("Set the paint brush size as a percentage of screen height or physical size"));

  d->BrushDiameterSliderWidget = new qMRMLSliderWidget(d->BrushDiameterSizeFrame);
  d->BrushDiameterSliderWidget->setToolTip(tr("Set the paint brush size as a percentage of screen height or physical size"));

  d->BrushDiameterSizeFrame->layout()->setContentsMargins(0, 0, 0, 0);
  d->BrushDiameterSizeFrame->layout()->addWidget(d->BrushDiameterIsAbsoluteButton);
  d->BrushDiameterSizeFrame->layout()->addWidget(d->BrushDiameterSliderWidget);
  brushFormLayout->addRow(diameterLabel, d->BrushDiameterSizeFrame);

  // Create options frame for this effect
  QHBoxLayout* hbox = new QHBoxLayout();

  d->BrushSphereCheckbox = new QCheckBox(tr("Sphere brush"));
  d->BrushSphereCheckbox->setToolTip(tr("Use a 3D spherical brush rather than a 2D circular brush."));
  hbox->addWidget(d->BrushSphereCheckbox);

  d->EditIn3DViewsCheckbox = new QCheckBox(tr("Edit in 3D views"));
  d->EditIn3DViewsCheckbox->setToolTip(tr("Allow painting in 3D views. If enabled, click-and-drag in a 3D view paints in the view "
                                          "instead of rotating the view."));
  hbox->addWidget(d->EditIn3DViewsCheckbox);

  d->ColorSmudgeCheckbox = new QCheckBox(tr("Color smudge"));
  d->ColorSmudgeCheckbox->setToolTip(
    tr("Select segment by sampling the pixel location"
       "where the brush stroke starts. If brush stroke starts in an empty area then the brush erases highlighted region from the selected segment."));
  if (!this->m_AlwaysErase)
  {
    hbox->addWidget(d->ColorSmudgeCheckbox);
  }

  d->EraseAllSegmentsCheckbox = new QCheckBox(tr("Erase all segments"));
  d->EraseAllSegmentsCheckbox->setToolTip(tr("If not checked then highlighted area is erased"
                                             " from all segments. If unchecked then only area is only erased from selected segment."));
  if (this->m_AlwaysErase)
  {
    hbox->addWidget(d->EraseAllSegmentsCheckbox);
  }

  d->BrushPixelModeCheckbox = new QCheckBox(tr("Pixel mode"));
  d->BrushPixelModeCheckbox->setToolTip(tr("Paint exactly the pixel under the cursor, ignoring the diameter, threshold, and paint over."));
  // TODO: Implement this effect option
  // hbox->addWidget(d->BrushPixelModeCheckbox);

  paintOptionsLayout->addLayout(hbox);

  QObject::connect(d->BrushDiameterIsAbsoluteButton, SIGNAL(clicked()), d, SLOT(onDiameterUnitsClicked()));
  QObject::connect(d->BrushSphereCheckbox, SIGNAL(clicked()), this, SLOT(updateMRMLFromGUI()));
  QObject::connect(d->EditIn3DViewsCheckbox, SIGNAL(clicked()), this, SLOT(updateMRMLFromGUI()));
  QObject::connect(d->ColorSmudgeCheckbox, SIGNAL(clicked()), this, SLOT(updateMRMLFromGUI()));
  QObject::connect(d->EraseAllSegmentsCheckbox, SIGNAL(clicked()), this, SLOT(updateMRMLFromGUI()));
  QObject::connect(d->BrushPixelModeCheckbox, SIGNAL(clicked()), this, SLOT(updateMRMLFromGUI()));
  QObject::connect(d->BrushDiameterSliderWidget, SIGNAL(valueChanged(double)), d, SLOT(onDiameterValueChanged(double)));
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffect::setMRMLDefaults()
{
  Superclass::setMRMLDefaults();

  this->setCommonParameterDefault("BrushMinimumAbsoluteDiameter", 0.01);
  this->setCommonParameterDefault("BrushMaximumAbsoluteDiameter", 100.0);
  this->setCommonParameterDefault("BrushAbsoluteDiameter", 5.0);
  this->setCommonParameterDefault("BrushRelativeDiameter", 3.0);
  this->setCommonParameterDefault("BrushDiameterIsRelative", 1);
  this->setCommonParameterDefault("BrushSphere", 0);
  this->setCommonParameterDefault("EditIn3DViews", 0);
  this->setParameterDefault("ColorSmudge", 0);
  this->setParameterDefault("EraseAllSegments", 0);
  this->setCommonParameterDefault("BrushPixelMode", 0);
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffect::updateGUIFromMRML()
{
  Q_D(qSlicerSegmentEditorPaintEffect);
  if (!this->active())
  {
    // updateGUIFromMRML is called when the effect is activated
    return;
  }

  if (!this->scene())
  {
    return;
  }

  this->m_ShowEffectCursorInThreeDView = (this->integerParameter("EditIn3DViews") != 0);

  d->BrushSphereCheckbox->blockSignals(true);
  d->BrushSphereCheckbox->setChecked(this->integerParameter("BrushSphere"));
  d->BrushSphereCheckbox->blockSignals(false);

  d->EditIn3DViewsCheckbox->blockSignals(true);
  d->EditIn3DViewsCheckbox->setChecked(this->integerParameter("EditIn3DViews"));
  d->EditIn3DViewsCheckbox->blockSignals(false);

  d->ColorSmudgeCheckbox->blockSignals(true);
  d->ColorSmudgeCheckbox->setChecked(this->integerParameter("ColorSmudge"));
  d->ColorSmudgeCheckbox->blockSignals(false);

  d->EraseAllSegmentsCheckbox->blockSignals(true);
  d->EraseAllSegmentsCheckbox->setChecked(this->integerParameter("EraseAllSegments"));
  d->EraseAllSegmentsCheckbox->blockSignals(false);

  bool pixelMode = this->integerParameter("BrushPixelMode");
  d->BrushPixelModeCheckbox->blockSignals(true);
  d->BrushPixelModeCheckbox->setChecked(pixelMode);
  d->BrushPixelModeCheckbox->blockSignals(false);

  Superclass::updateGUIFromMRML();

  // Diameter is also disabled if pixel mode is on
  d->BrushDiameterFrame->setEnabled(!pixelMode);

  bool brushDiameterIsRelative = this->integerParameter("BrushDiameterIsRelative");

  d->BrushDiameterIsAbsoluteButton->blockSignals(true);
  d->BrushDiameterIsAbsoluteButton->setChecked(!brushDiameterIsRelative);
  d->BrushDiameterIsAbsoluteButton->blockSignals(false);
  // The segment editor widget sets the "studioPresentation" property in the Slicer Studio presentation,
  // where the button names the current unit instead of being an "absolute" toggle.
  if (this->property("studioPresentation").toBool())
  {
    d->BrushDiameterIsAbsoluteButton->setText(brushDiameterIsRelative ? tr("% of view") : tr("mm"));
    d->BrushDiameterIsAbsoluteButton->setToolTip(brushDiameterIsRelative ? tr("The brush size is a percentage of the view height. Click to set it in millimeters.")
                                                                         : tr("The brush size is in millimeters. Click to set it as a percentage of the view height."));
  }
  else
  {
    d->BrushDiameterIsAbsoluteButton->setText("absolute");
    d->BrushDiameterIsAbsoluteButton->setToolTip(tr("Set the paint brush size as a percentage of screen height or physical size"));
  }

  d->BrushDiameterSliderWidget->blockSignals(true);
  if (brushDiameterIsRelative)
  {
    d->BrushDiameterSliderWidget->setRange(1, 25);
    d->BrushDiameterSliderWidget->setValue(this->doubleParameter("BrushRelativeDiameter"));
    d->BrushDiameterSliderWidget->setSingleStep(1);
  }
  else
  {
    d->BrushDiameterSliderWidget->setRange(this->doubleParameter("BrushMinimumAbsoluteDiameter"), this->doubleParameter("BrushMaximumAbsoluteDiameter"));
    d->BrushDiameterSliderWidget->setValue(this->doubleParameter("BrushAbsoluteDiameter"));
    d->BrushDiameterSliderWidget->setSingleStep(this->doubleParameter("BrushMinimumAbsoluteDiameter"));
  }
  if (brushDiameterIsRelative)
  {
    d->BrushDiameterSliderWidget->setQuantity("");
    d->BrushDiameterSliderWidget->setSuffix("%");
    d->BrushDiameterSliderWidget->setDecimals(0);
  }
  else
  {
    d->BrushDiameterSliderWidget->setQuantity("length");
    d->BrushDiameterSliderWidget->setSuffix(tr("mm"));
    d->BrushDiameterSliderWidget->setUnitAwareProperties(qMRMLSliderWidget::Prefix | qMRMLSliderWidget::Suffix);
    // Enough decimals for the minimum diameter (e.g., 4 for 0.01 mm, 2 for 0.5 mm)
    int decimals = (int)floor(log10(d->BrushDiameterSliderWidget->minimum()));
    d->BrushDiameterSliderWidget->setDecimals(decimals < 0 ? -decimals * 2 : 1);
  }
  d->BrushDiameterSliderWidget->blockSignals(false);

  // Show the brush size for a short time when it is changed (not when the effect is activated)
  double brushDiameter = this->doubleParameter(brushDiameterIsRelative ? "BrushRelativeDiameter" : "BrushAbsoluteDiameter");
  if (d->BrushSizeParametersInitialized && (d->LastBrushDiameterIsRelative != static_cast<int>(brushDiameterIsRelative) || d->LastBrushDiameter != brushDiameter))
  {
    d->showBrushSizeHud();
  }
  d->BrushSizeParametersInitialized = true;
  d->LastBrushDiameterIsRelative = brushDiameterIsRelative;
  d->LastBrushDiameter = brushDiameter;

  // Brush shape may have changed, update it at the next mouse move
  d->BrushModelUpToDate = false;

  // Update brushes
  d->updateBrushes();
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffect::updateMRMLFromGUI()
{
  Q_D(qSlicerSegmentEditorPaintEffect);

  Superclass::updateMRMLFromGUI();

  this->setCommonParameter("BrushSphere", (int)d->BrushSphereCheckbox->isChecked());
  this->setCommonParameter("EditIn3DViews", (int)d->EditIn3DViewsCheckbox->isChecked());
  if (this->m_AlwaysErase)
  {
    // erase
    this->setParameter("EraseAllSegments", (int)d->EraseAllSegmentsCheckbox->isChecked());
  }
  else
  {
    // paint
    this->setParameter("ColorSmudge", (int)d->ColorSmudgeCheckbox->isChecked());
  }
  bool pixelMode = d->BrushPixelModeCheckbox->isChecked();
  bool pixelModeChanged = (pixelMode != (bool)this->integerParameter("BrushPixelMode"));
  this->setCommonParameter("BrushPixelMode", (int)pixelMode);

  bool isBrushDiameterRelative = !d->BrushDiameterIsAbsoluteButton->isChecked();
  this->setCommonParameter("BrushDiameterIsRelative", isBrushDiameterRelative ? 1 : 0);
  if (isBrushDiameterRelative)
  {
    this->setCommonParameter("BrushRelativeDiameter", d->BrushDiameterSliderWidget->value());
  }
  else
  {
    this->setCommonParameter("BrushAbsoluteDiameter", d->BrushDiameterSliderWidget->value());
  }

  // If pixel mode changed, then other GUI changes are due
  if (pixelModeChanged)
  {
    // Update label options based on constraints set by pixel mode
    Superclass::updateGUIFromMRML();

    d->BrushDiameterFrame->setEnabled(!pixelMode);
  }
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffect::referenceGeometryChanged()
{
  Superclass::referenceGeometryChanged();

  vtkOrientedImageData* referenceGeometryImage = this->referenceGeometryImage();
  if (referenceGeometryImage == nullptr)
  {
    return;
  }
  double spacing[3] = { 0.0, 0.0, 0.0 };
  referenceGeometryImage->GetSpacing(spacing);
  double minimumSpacing = qMin(spacing[0], qMin(spacing[1], spacing[2]));
  double minimumDiameter = 0.5 * minimumSpacing;

  int dimensions[3] = { 0, 0, 0 };
  referenceGeometryImage->GetDimensions(dimensions);
  double bounds[3] = { spacing[0] * dimensions[0], spacing[1] * dimensions[1], spacing[2] * dimensions[2] };
  double maximumBounds = qMax(bounds[0], qMax(bounds[1], bounds[2]));
  double maximumDiameter = 0.5 * maximumBounds;
  if (minimumDiameter <= 0.0 || maximumDiameter <= minimumDiameter)
  {
    // Invalid or degenerate geometry
    return;
  }

  this->setCommonParameter("BrushMinimumAbsoluteDiameter", minimumDiameter);
  this->setCommonParameter("BrushMaximumAbsoluteDiameter", maximumDiameter);
  // Keep the diameter that the user has chosen, only make sure that it is within the limits
  double diameter = this->doubleParameter("BrushAbsoluteDiameter");
  double clampedDiameter = qBound(minimumDiameter, diameter, maximumDiameter);
  if (clampedDiameter != diameter)
  {
    this->setCommonParameter("BrushAbsoluteDiameter", clampedDiameter);
  }

  this->updateGUIFromMRML();
}

//-----------------------------------------------------------------------------
double qSlicerSegmentEditorPaintEffect::minimumPaintPointDistance()
{
  Q_D(qSlicerSegmentEditorPaintEffect);
  return sqrt(d->MinimumPaintPointDistance2);
}

//-----------------------------------------------------------------------------
bool qSlicerSegmentEditorPaintEffect::delayedPaint()
{
  Q_D(qSlicerSegmentEditorPaintEffect);
  return d->DelayedPaint;
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffect::setMinimumPaintPointDistance(double dist)
{
  Q_D(qSlicerSegmentEditorPaintEffect);
  d->MinimumPaintPointDistance2 = dist * dist;
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffect::setDelayedPaint(bool delayed)
{
  Q_D(qSlicerSegmentEditorPaintEffect);
  d->DelayedPaint = delayed;
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffect::paintApply(qMRMLWidget* viewWidget)
{
  Q_D(qSlicerSegmentEditorPaintEffect);

  vtkOrientedImageData* modifierLabelmap = this->defaultModifierLabelmap();
  if (!modifierLabelmap)
  {
    qCritical() << Q_FUNC_INFO << ": Invalid modifier labelmap";
    return;
  }
  if (!this->parameterSetNode())
  {
    qCritical() << Q_FUNC_INFO << ": Invalid segment editor parameter set node!";
    return;
  }
  vtkMRMLSegmentationNode* segmentationNode = this->parameterSetNode()->GetSegmentationNode();
  if (!segmentationNode)
  {
    qCritical() << Q_FUNC_INFO << ": Invalid segmentationNode";
    return;
  }

  this->saveStateForUndo();

  QList<int> updateExtentList;
  int updateExtent[6] = { 0, -1, 0, -1, 0, -1 };

  if (this->integerParameter("BrushPixelMode"))
  {
    d->paintPixels(modifierLabelmap, d->PaintCoordinates_World, updateExtent);
  }
  else
  {
    d->paintBrushes(modifierLabelmap, viewWidget, d->PaintCoordinates_World, updateExtent);
  }

  int modifierExtent[6] = { 0, -1, 0, -1, 0, -1 };
  modifierLabelmap->GetExtent(modifierExtent);
  for (int i = 0; i < 3; i++)
  {
    updateExtent[2 * i] = std::max(updateExtent[2 * i], modifierExtent[2 * i]);
    updateExtent[2 * i + 1] = std::min(updateExtent[2 * i + 1], modifierExtent[2 * i + 1]);
  }
  for (int i = 0; i < 6; i++)
  {
    updateExtentList << updateExtent[i];
  }

  // Notify editor about changes
  qSlicerSegmentEditorAbstractEffect::ModificationMode modificationMode;
  if (this->m_AlwaysErase)
  {
    modificationMode =
      this->integerParameter("EraseAllSegments") ? qSlicerSegmentEditorAbstractEffect::ModificationModeRemoveAll : qSlicerSegmentEditorAbstractEffect::ModificationModeRemove;
  }
  else
  {
    modificationMode = this->m_Erase ? qSlicerSegmentEditorAbstractEffect::ModificationModeRemove : qSlicerSegmentEditorAbstractEffect::ModificationModeAdd;
  }

  this->modifySelectedSegmentByLabelmap(modifierLabelmap, modificationMode, updateExtentList);

  // Rendering the feedback actor with no points will result in an error message that will clutter the log.
  // "No input data"
  d->PaintCoordinates_World->Reset();
  d->resetFeedback();
  d->clearBrushPipelines();
  d->FeedbackViewWidget = nullptr;
}

//-----------------------------------------------------------------------------
QList<int> qSlicerSegmentEditorPaintEffect::paintBrushesIntoLabelmap(vtkOrientedImageData* labelmap, qMRMLWidget* viewWidget)
{
  Q_D(qSlicerSegmentEditorPaintEffect);
  int modifiedExtent[6] = { 0, -1, 0, -1, 0, -1 };
  d->paintBrushes(labelmap, viewWidget, d->PaintCoordinates_World, modifiedExtent);
  QList<int> modifiedExtentList;
  for (int i = 0; i < 6; i++)
  {
    modifiedExtentList.append(modifiedExtent[i]);
  }
  return modifiedExtentList;
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffect::clearBrushes()
{
  Q_D(qSlicerSegmentEditorPaintEffect);
  // Rendering the feedback actor with no points will result in an error message that will clutter the log.
  // "No input data"
  d->PaintCoordinates_World->Reset();
  d->resetFeedback();
  d->clearBrushPipelines();
  d->FeedbackViewWidget = nullptr;
}

//-----------------------------------------------------------------------------
void qSlicerSegmentEditorPaintEffect::setColorSmudgeCheckboxVisible(bool show)
{
  Q_D(qSlicerSegmentEditorPaintEffect);
  d->ColorSmudgeCheckbox->setVisible(show);
}

//-----------------------------------------------------------------------------
QFrame* qSlicerSegmentEditorPaintEffect::paintOptionsFrame()
{
  Q_D(qSlicerSegmentEditorPaintEffect);
  return d->PaintOptionsFrame;
}
