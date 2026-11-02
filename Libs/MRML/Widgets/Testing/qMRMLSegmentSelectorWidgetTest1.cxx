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

  This file was originally developed by Kyle Sunderland, PerkLab, Queen's University
  and was supported through CANARIE's Research Software Program, Cancer
  Care Ontario, OpenAnatomy, and Brigham and Women's Hospital through NIH grant R01MH112748.

==============================================================================*/

// CTK includes
#include <ctkTest.h>

// Qt includes
#include <QApplication>
#include <QTimer>

// qMRML includes
#include "qMRMLSegmentSelectorWidget.h"

// CTK includes
#include <ctkCoreTestingMacros.h>

// MRML includes
#include "vtkMRMLScene.h"
#include "vtkMRMLSegmentationNode.h"

// Segmentation includes
#include <vtkSegmentationConverterFactory.h>
#include <vtkClosedSurfaceToBinaryLabelmapConversionRule.h>
#include <vtkBinaryLabelmapToClosedSurfaceConversionRule.h>

// ----------------------------------------------------------------------------
int testSingleSelection(qMRMLSegmentSelectorWidget* segmentSelectorWidget)
{
  vtkNew<vtkMRMLScene> scene;

  segmentSelectorWidget->setNoneEnabled(true);
  segmentSelectorWidget->setMultiSelection(false);
  segmentSelectorWidget->setMRMLScene(scene.GetPointer());

  vtkNew<vtkMRMLSegmentationNode> segmentationNode;
  scene->AddNode(segmentationNode);

  QSignalSpy nodeChangedSpy(segmentSelectorWidget, SIGNAL(currentNodeChanged(vtkMRMLNode*)));
  QSignalSpy currentSegmentChangedSpy(segmentSelectorWidget, SIGNAL(currentSegmentChanged(QString)));
  QSignalSpy segmentSelectionChangedSpy(segmentSelectorWidget, SIGNAL(segmentSelectionChanged(QStringList)));

  segmentSelectorWidget->setCurrentNode(segmentationNode);

  CHECK_INT(nodeChangedSpy.count(), 1);
  CHECK_BOOL(segmentSelectorWidget->currentSegmentID().isEmpty(), true);

  // With none enabled, adding a new segment should not cause a segment to be selected.
  QString segment1ID = QString::fromStdString(segmentationNode->GetSegmentation()->AddEmptySegment());
  CHECK_INT(currentSegmentChangedSpy.count(), 0);
  CHECK_INT(segmentSelectionChangedSpy.count(), 0);

  segmentSelectorWidget->setNoneEnabled(false);
  CHECK_INT(currentSegmentChangedSpy.count(), 1);
  CHECK_QSTRING(segment1ID, segmentSelectorWidget->currentSegmentID());

  // Adding additional segments should not change selection
  QString segment2ID = QString::fromStdString(segmentationNode->GetSegmentation()->AddEmptySegment());
  QString segment3ID = QString::fromStdString(segmentationNode->GetSegmentation()->AddEmptySegment());
  CHECK_INT(currentSegmentChangedSpy.count(), 1);
  CHECK_QSTRING(segment1ID, segmentSelectorWidget->currentSegmentID());

  // Change selection to last segment
  segmentSelectorWidget->setCurrentSegmentID(segment3ID);
  CHECK_INT(currentSegmentChangedSpy.count(), 2);
  CHECK_QSTRING(segment3ID, segmentSelectorWidget->currentSegmentID());

  // Remove non-selected segment
  segmentationNode->RemoveSegment(segment2ID.toStdString());
  CHECK_INT(currentSegmentChangedSpy.count(), 2);
  CHECK_QSTRING(segment3ID, segmentSelectorWidget->currentSegmentID());

  // Remove selected segment
  segmentationNode->RemoveSegment(segment3ID.toStdString());
  CHECK_INT(currentSegmentChangedSpy.count(), 3);
  CHECK_QSTRING_DIFFERENT(segment3ID, segmentSelectorWidget->currentSegmentID());

  // Multi-selection signal should not have been sent
  CHECK_INT(segmentSelectionChangedSpy.count(), 0);

  segmentationNode->GetSegmentation()->AddEmptySegment(segment2ID.toStdString());
  segmentationNode->GetSegmentation()->AddEmptySegment(segment3ID.toStdString());

  return EXIT_SUCCESS;
}

// ----------------------------------------------------------------------------
int testMultiSelection(qMRMLSegmentSelectorWidget* segmentSelectorWidget)
{
  vtkNew<vtkMRMLScene> scene;
  segmentSelectorWidget->setMultiSelection(true);
  segmentSelectorWidget->setMRMLScene(scene.GetPointer());

  vtkNew<vtkMRMLSegmentationNode> segmentationNode;
  scene->AddNode(segmentationNode);

  QSignalSpy nodeChangedSpy(segmentSelectorWidget, SIGNAL(currentNodeChanged(vtkMRMLNode*)));
  QSignalSpy currentSegmentChangedSpy(segmentSelectorWidget, SIGNAL(currentSegmentChanged(QString)));
  QSignalSpy segmentSelectionChangedSpy(segmentSelectorWidget, SIGNAL(segmentSelectionChanged(QStringList)));

  segmentSelectorWidget->setCurrentNode(segmentationNode);

  CHECK_INT(nodeChangedSpy.count(), 1);

  // Adding a new segment should not cause a segment to be selected.
  QString segment1ID = QString::fromStdString(segmentationNode->GetSegmentation()->AddEmptySegment());
  CHECK_INT(segmentSelectionChangedSpy.count(), 0);
  CHECK_INT(segmentSelectorWidget->selectedSegmentIDs().count(), 0);

  // Select segment
  segmentSelectorWidget->setSelectedSegmentIDs(QStringList() << segment1ID);
  CHECK_INT(segmentSelectionChangedSpy.count(), 1);
  CHECK_QSTRINGLIST(QStringList() << segment1ID, segmentSelectorWidget->selectedSegmentIDs());

  // Adding additional segments should not change selection
  QString segment2ID = QString::fromStdString(segmentationNode->GetSegmentation()->AddEmptySegment());
  QString segment3ID = QString::fromStdString(segmentationNode->GetSegmentation()->AddEmptySegment());
  CHECK_INT(segmentSelectionChangedSpy.count(), 1);
  CHECK_QSTRINGLIST(QStringList() << segment1ID, segmentSelectorWidget->selectedSegmentIDs());

  //// Select all segments
  segmentSelectorWidget->setSelectedSegmentIDs(QStringList() << segment1ID << segment3ID);
  CHECK_INT(segmentSelectionChangedSpy.count(), 2);
  CHECK_QSTRINGLIST(QStringList() << segment1ID << segment3ID, segmentSelectorWidget->selectedSegmentIDs());

  //// Remove non-selected segment
  segmentationNode->RemoveSegment(segment2ID.toStdString());
  CHECK_INT(segmentSelectionChangedSpy.count(), 2);
  CHECK_QSTRINGLIST(QStringList() << segment1ID << segment3ID, segmentSelectorWidget->selectedSegmentIDs());

  //// Remove selected segment
  segmentationNode->RemoveSegment(segment3ID.toStdString());
  CHECK_INT(segmentSelectionChangedSpy.count(), 3);
  CHECK_QSTRINGLIST(QStringList() << segment1ID, segmentSelectorWidget->selectedSegmentIDs());

  // Single-selection signal should not have been sent
  CHECK_INT(currentSegmentChangedSpy.count(), 0);

  segmentationNode->GetSegmentation()->AddEmptySegment(segment2ID.toStdString());
  segmentationNode->GetSegmentation()->AddEmptySegment(segment3ID.toStdString());

  return EXIT_SUCCESS;
}

// ----------------------------------------------------------------------------
int testMultiSelectionNodeChange(qMRMLSegmentSelectorWidget* segmentSelectorWidget)
{
  // The selection stored for one segmentation node must not be reported as a
  // selection change when another segmentation node becomes current. The slice
  // controller applies segmentSelectionChanged to the current node's segment
  // visibility, so a stale selection used to hide every segment of a newly loaded
  // segmentation (for example when a scene is loaded with clear=True).
  vtkNew<vtkMRMLScene> scene;
  segmentSelectorWidget->setMultiSelection(true);
  segmentSelectorWidget->setMRMLScene(scene.GetPointer());

  vtkNew<vtkMRMLSegmentationNode> segmentationNodeA;
  scene->AddNode(segmentationNodeA);
  QString segmentAID = QString::fromStdString(segmentationNodeA->GetSegmentation()->AddEmptySegment("segmentA"));

  segmentSelectorWidget->setCurrentNode(segmentationNodeA);
  segmentSelectorWidget->setSelectedSegmentIDs(QStringList() << segmentAID);
  CHECK_QSTRINGLIST(QStringList() << segmentAID, segmentSelectorWidget->selectedSegmentIDs());

  QSignalSpy segmentSelectionChangedSpy(segmentSelectorWidget, SIGNAL(segmentSelectionChanged(QStringList)));

  // Switch to another segmentation node whose segment IDs differ
  vtkNew<vtkMRMLSegmentationNode> segmentationNodeB;
  scene->AddNode(segmentationNodeB);
  QString segmentBID = QString::fromStdString(segmentationNodeB->GetSegmentation()->AddEmptySegment("segmentB"));
  segmentSelectorWidget->setCurrentNode(segmentationNodeB);
  CHECK_INT(segmentSelectionChangedSpy.count(), 0);
  CHECK_INT(segmentSelectorWidget->selectedSegmentIDs().count(), 0);

  // Scene replaced: the current node is removed, then a new node is selected
  segmentSelectorWidget->setSelectedSegmentIDs(QStringList() << segmentBID);
  CHECK_INT(segmentSelectionChangedSpy.count(), 1);
  scene->RemoveNode(segmentationNodeA);
  scene->RemoveNode(segmentationNodeB);
  vtkNew<vtkMRMLSegmentationNode> segmentationNodeC;
  scene->AddNode(segmentationNodeC);
  segmentationNodeC->GetSegmentation()->AddEmptySegment("segmentC");
  segmentSelectorWidget->setCurrentNode(segmentationNodeC);
  CHECK_INT(segmentSelectionChangedSpy.count(), 1);
  CHECK_INT(segmentSelectorWidget->selectedSegmentIDs().count(), 0);

  return EXIT_SUCCESS;
}

//-----------------------------------------------------------------------------
int qMRMLSegmentSelectorWidgetTest1(int argc, char* argv[])
{
  qMRMLWidget::preInitializeApplication();
  QApplication app(argc, argv);
  qMRMLWidget::postInitializeApplication();

  qRegisterMetaType<vtkMRMLNode*>("vtkMRMLNode*");

  vtkSegmentationConverterFactory* converterFactory = vtkSegmentationConverterFactory::GetInstance();
  converterFactory->RegisterConverterRule(vtkSmartPointer<vtkClosedSurfaceToBinaryLabelmapConversionRule>::New());
  converterFactory->RegisterConverterRule(vtkSmartPointer<vtkBinaryLabelmapToClosedSurfaceConversionRule>::New());

  qMRMLSegmentSelectorWidget singleSegmentSelectorWidget;
  singleSegmentSelectorWidget.show();
  if (testSingleSelection(&singleSegmentSelectorWidget) != EXIT_SUCCESS)
  {
    return EXIT_FAILURE;
  }

  qMRMLSegmentSelectorWidget multiSegmentSelectorWidget;
  multiSegmentSelectorWidget.show();
  if (testMultiSelection(&multiSegmentSelectorWidget) != EXIT_SUCCESS)
  {
    return EXIT_FAILURE;
  }

  qMRMLSegmentSelectorWidget nodeChangeSegmentSelectorWidget;
  nodeChangeSegmentSelectorWidget.show();
  if (testMultiSelectionNodeChange(&nodeChangeSegmentSelectorWidget) != EXIT_SUCCESS)
  {
    return EXIT_FAILURE;
  }

  // ------------------------
  if (argc < 2 || QString(argv[1]) != "-I")
  {
    QTimer::singleShot(200, &app, SLOT(quit()));
  }

  return app.exec();
}
