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

  This file was originally developed by Julien Finet, Kitware Inc.
  and was partially funded by NIH grant 3P41RR013218-12S1

==============================================================================*/

// Qt includes
#include <QApplication>
#include <QDebug>
#include <QTimer>

// Slicer includes
#include "qSlicerApplication.h"
#include "vtkSlicerApplicationLogic.h"
#include "vtkSlicerConfigure.h"

// Volumes includes
#include "qSlicerVolumesIOOptionsWidget.h"

// MRML includes
#include "vtkMRMLColorLogic.h"
#include "vtkMRMLScene.h"

// VTK includes
#include "qMRMLWidget.h"

// STD includes
#include <iostream>

//-----------------------------------------------------------------------------
int qSlicerVolumesIOOptionsWidgetTest1(int argc, char* argv[])
{
  qMRMLWidget::preInitializeApplication();
  qSlicerApplication app(argc, argv);
  qMRMLWidget::postInitializeApplication();

  // set up the color nodes for access from the widget
  vtkSlicerApplicationLogic* appLogic = qSlicerCoreApplication::application()->applicationLogic();
  QString defaultLabelColor;
  QString defaultGreyColor;
  if (appLogic && appLogic->GetColorLogic())
  {
    appLogic->GetColorLogic()->SetMRMLScene(qSlicerApplication::application()->mrmlScene());
    appLogic->GetColorLogic()->AddDefaultColorNodes();
    defaultLabelColor = QString(appLogic->GetColorLogic()->GetDefaultLabelMapColorNodeID());
    defaultGreyColor = QString(appLogic->GetColorLogic()->GetDefaultVolumeColorNodeID());
  }

  qSlicerVolumesIOOptionsWidget optionsWidget;
  optionsWidget.setMRMLScene(qSlicerApplication::application()->mrmlScene());

  optionsWidget.setFileName("mylabelmap-seg.nrrd");
  if (!optionsWidget.properties()["labelmap"].toBool())
  {
    std::cerr << "Must be a labelmap" << std::endl;
    return EXIT_FAILURE;
  }
  QString colorID = optionsWidget.properties()["colorNodeID"].toString();
  qDebug() << __LINE__ << ": Label map: color id: " << colorID;
  if (colorID != defaultLabelColor)
  {
    std::cerr << __LINE__ << ": wrong color id set for a label map, expected " << defaultLabelColor.toStdString() << std::endl;
    return EXIT_FAILURE;
  }

  optionsWidget.setFileName("./mylabelmap_seg_1.nrrd");
  if (!optionsWidget.properties()["labelmap"].toBool())
  {
    std::cerr << "Must be a labelmap" << std::endl;
    return EXIT_FAILURE;
  }
  colorID = optionsWidget.properties()["colorNodeID"].toString();
  qDebug() << __LINE__ << ": Label map: color id: " << colorID;
  if (colorID != defaultLabelColor)
  {
    std::cerr << __LINE__ << ": wrong color id set for a label map, expected " << defaultLabelColor.toStdString() << std::endl;
    return EXIT_FAILURE;
  }

  optionsWidget.setFileName("./segment.nrrd");
  if (optionsWidget.properties()["labelmap"].toBool())
  {
    std::cerr << "Not a labelmap" << std::endl;
    return EXIT_FAILURE;
  }
  colorID = optionsWidget.properties()["colorNodeID"].toString();
  qDebug() << __LINE__ << ": Greyscale: color id: " << colorID;
  if (colorID != defaultGreyColor)
  {
    std::cerr << __LINE__ << ": wrong color id set for a grey scale, expected " << defaultGreyColor.toStdString() << std::endl;
    return EXIT_FAILURE;
  }

  // Lesion, mask and region of interest images are labelmaps (whole words only)
  const char* labelMapFileNames[] = { "sub-001_lesion.nii.gz", "brain_mask.nii.gz", "roi.nrrd", "MASK.nii.gz", "LESION.nii.gz" };
  for (const char* fileName : labelMapFileNames)
  {
    optionsWidget.setFileName(fileName);
    if (!optionsWidget.properties()["labelmap"].toBool())
    {
      std::cerr << __LINE__ << ": " << fileName << " must be a labelmap" << std::endl;
      return EXIT_FAILURE;
    }
  }
  // Partial words and probability maps are not labelmaps
  const char* notLabelMapFileNames[] = { "masked_T1.nii.gz", "lesion_prob.nii.gz", "sub-01_desc-lesionProb_mask.nii.gz" };
  for (const char* fileName : notLabelMapFileNames)
  {
    optionsWidget.setFileName(fileName);
    if (optionsWidget.properties()["labelmap"].toBool())
    {
      std::cerr << __LINE__ << ": " << fileName << " is not a labelmap" << std::endl;
      return EXIT_FAILURE;
    }
  }

  optionsWidget.show();

  if (argc < 2 || QString(argv[2]) != "-I")
  {
    QTimer::singleShot(200, &app, SLOT(quit()));
  }
  return app.exec();
}
