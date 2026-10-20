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

  This file was originally developed by Eric Larson.

==============================================================================*/

// Qt includes
#include <QDebug>
#include <QEvent>
#include <QSettings>

// QtGUI includes
#include "qSlicerApplication.h"
#include "qSlicerCommandOptions.h"
#include "qSlicerSettingsViewsPanel.h"
#include "ui_qSlicerSettingsViewsPanel.h"

// CTK includes
#include <ctkVTKAbstractView.h>
#include <ctkComboBox.h>

// MRMLWidgets includes
#include <qMRMLViewControllerBar.h>

// MRML includes
#include <vtkMRMLScene.h>
#include <vtkMRMLViewNode.h>

// VTK includes
#include <vtkCollection.h>
#include <vtkSmartPointer.h>

namespace
{
const char* const ThreeDViewDefaultsGroup = "Appearance/Default3DView/";

/// Default 3D view background and box, see qSlicerSettingsViewsPanel::threeDViewDefault()
struct ThreeDViewDefaults
{
  QColor BackgroundColor;
  QColor BackgroundColor2;
  bool BoxVisible{ true };
  QColor BoxColor;

  bool isValid() const { return this->BackgroundColor.isValid(); }
  bool operator==(const ThreeDViewDefaults& other) const
  {
    return this->BackgroundColor == other.BackgroundColor && this->BackgroundColor2 == other.BackgroundColor2 && this->BoxVisible == other.BoxVisible
           && this->BoxColor == other.BoxColor;
  }
  bool operator!=(const ThreeDViewDefaults& other) const { return !(*this == other); }

  static ThreeDViewDefaults current()
  {
    ThreeDViewDefaults defaults;
    if (!qSlicerSettingsViewsPanel::threeDViewThemeDefaultsEnabled())
    {
      return defaults;
    }
    defaults.BackgroundColor = qSlicerSettingsViewsPanel::threeDViewDefault("BackgroundColor").value<QColor>();
    defaults.BackgroundColor2 = qSlicerSettingsViewsPanel::threeDViewDefault("BackgroundColor2").value<QColor>();
    defaults.BoxVisible = qSlicerSettingsViewsPanel::threeDViewDefault("BoxVisible").toBool();
    defaults.BoxColor = qSlicerSettingsViewsPanel::threeDViewDefault("BoxColor").value<QColor>();
    return defaults;
  }
};

bool sameColor(const double rgb[3], const QColor& color)
{
  const double tolerance = 0.5 / 255.0;
  return qAbs(rgb[0] - color.redF()) < tolerance && qAbs(rgb[1] - color.greenF()) < tolerance && qAbs(rgb[2] - color.blueF()) < tolerance;
}

/// Apply new defaults to the default 3D view node of the scene and to the 3D views
/// that still show the previous defaults (views changed by the user are kept).
void applyThreeDViewDefaults(const ThreeDViewDefaults& previous, const ThreeDViewDefaults& defaults)
{
  qSlicerApplication* app = qSlicerApplication::application();
  vtkMRMLScene* scene = app ? app->mrmlScene() : nullptr;
  if (!scene || !defaults.isValid())
  {
    return;
  }
  vtkMRMLViewNode* defaultViewNode = vtkMRMLViewNode::SafeDownCast(scene->GetDefaultNodeByClass("vtkMRMLViewNode"));
  if (defaultViewNode)
  {
    defaultViewNode->SetBackgroundColor(defaults.BackgroundColor.redF(), defaults.BackgroundColor.greenF(), defaults.BackgroundColor.blueF());
    defaultViewNode->SetBackgroundColor2(defaults.BackgroundColor2.redF(), defaults.BackgroundColor2.greenF(), defaults.BackgroundColor2.blueF());
    defaultViewNode->SetBoxVisible(defaults.BoxVisible);
    defaultViewNode->SetBoxColor(defaults.BoxColor.redF(), defaults.BoxColor.greenF(), defaults.BoxColor.blueF());
  }
  if (!previous.isValid())
  {
    return;
  }
  vtkSmartPointer<vtkCollection> viewNodes = vtkSmartPointer<vtkCollection>::Take(scene->GetNodesByClass("vtkMRMLViewNode"));
  for (int i = 0; i < viewNodes->GetNumberOfItems(); ++i)
  {
    vtkMRMLViewNode* viewNode = vtkMRMLViewNode::SafeDownCast(viewNodes->GetItemAsObject(i));
    if (!viewNode)
    {
      continue;
    }
    // Only views that still show the previous default background follow the defaults: the box
    // of a view with another background (for example a scene saved by another application) is kept.
    if (!sameColor(viewNode->GetBackgroundColor(), previous.BackgroundColor) || !sameColor(viewNode->GetBackgroundColor2(), previous.BackgroundColor2))
    {
      continue;
    }
    int wasModifying = viewNode->StartModify();
    viewNode->SetBackgroundColor(defaults.BackgroundColor.redF(), defaults.BackgroundColor.greenF(), defaults.BackgroundColor.blueF());
    viewNode->SetBackgroundColor2(defaults.BackgroundColor2.redF(), defaults.BackgroundColor2.greenF(), defaults.BackgroundColor2.blueF());
    if (static_cast<bool>(viewNode->GetBoxVisible()) == previous.BoxVisible)
    {
      viewNode->SetBoxVisible(defaults.BoxVisible);
    }
    if (sameColor(viewNode->GetBoxColor(), previous.BoxColor))
    {
      viewNode->SetBoxColor(defaults.BoxColor.redF(), defaults.BoxColor.greenF(), defaults.BoxColor.blueF());
    }
    viewNode->EndModify(wasModifying);
  }
}
} // namespace

// --------------------------------------------------------------------------
// qSlicerSettingsViewsPanelPrivate

//-----------------------------------------------------------------------------
class qSlicerSettingsViewsPanelPrivate : public Ui_qSlicerSettingsViewsPanel
{
  Q_DECLARE_PUBLIC(qSlicerSettingsViewsPanel);

protected:
  qSlicerSettingsViewsPanel* const q_ptr;

public:
  qSlicerSettingsViewsPanelPrivate(qSlicerSettingsViewsPanel& object);
  void init();

  /// Show the current 3D view defaults in the "3D view background" widgets.
  void updateThreeDViewDefaultsWidgets();
  /// Save a 3D view default chosen by the user and apply it to the 3D views.
  void setThreeDViewDefault(const QString& name, const QVariant& value);
  /// Apply the current 3D view defaults to the 3D views if they changed.
  void updateThreeDViewDefaults();

  /// Last 3D view defaults applied to the views
  ThreeDViewDefaults AppliedThreeDViewDefaults;
};

// --------------------------------------------------------------------------
// qSlicerSettingsViewsPanelPrivate methods

// --------------------------------------------------------------------------
qSlicerSettingsViewsPanelPrivate::qSlicerSettingsViewsPanelPrivate(qSlicerSettingsViewsPanel& object)
  : q_ptr(&object)
{
}

// --------------------------------------------------------------------------
void qSlicerSettingsViewsPanelPrivate::init()
{
  Q_Q(qSlicerSettingsViewsPanel);

  this->setupUi(q);

  // Flat view bars. The default style of the view controller bars must be set before the views are
  // created, therefore the setting is read by the constructor and a change requires a restart.
  // Flat bars are not used by default when testing, so that tests and baselines are not affected.
  this->FlatViewBarsCheckBox->setChecked(!qSlicerApplication::testAttribute(qSlicerCoreApplication::AA_EnableTesting));
  q->registerProperty("Appearance/FlatViewBars",
                      this->FlatViewBarsCheckBox,
                      /*no tr*/ "checked",
                      SIGNAL(toggled(bool)),
                      qSlicerSettingsViewsPanel::tr("Flat view bars"),
                      ctkSettingsPanel::OptionRequireRestart);

  // 3D view background and box. These defaults follow the theme unless the user chose them.
  // They are saved only when the user changes them, in Appearance/Default3DView/* settings
  // that other Slicer installations sharing the same settings file ignore, and they are read by
  // the ViewControllers module for the default 3D view node.
  // When they are used, the box visibility set here replaces the "Show 3D cube" setting.
  bool themeDefaults = qSlicerSettingsViewsPanel::threeDViewThemeDefaultsEnabled();
  this->ThreeDBackgroundGroupBox->setVisible(themeDefaults);
  this->ThreeDBoxVisibilityLabel->setVisible(!themeDefaults);
  this->ThreeDBoxVisibilityCheckBox->setVisible(!themeDefaults);
  this->AppliedThreeDViewDefaults = ThreeDViewDefaults::current();
  this->updateThreeDViewDefaultsWidgets();
  QObject::connect(this->ThreeDBackgroundColorButton, SIGNAL(colorChanged(QColor)), q, SLOT(onThreeDBackgroundColorChanged(QColor)));
  QObject::connect(this->ThreeDBackgroundColor2Button, SIGNAL(colorChanged(QColor)), q, SLOT(onThreeDBackgroundColor2Changed(QColor)));
  QObject::connect(this->ThreeDBoxVisibleCheckBox, SIGNAL(toggled(bool)), q, SLOT(onThreeDBoxVisibleToggled(bool)));
  QObject::connect(this->ThreeDBoxColorButton, SIGNAL(colorChanged(QColor)), q, SLOT(onThreeDBoxColorChanged(QColor)));
  QObject::connect(this->ThreeDBackgroundResetButton, SIGNAL(clicked()), q, SLOT(resetThreeDViewDefaults()));

  // MSAA Setting
  // "Off" is the default, and should be the zeroth index to ensure that
  // poorly-formatted (or old) entries in the .ini default to "Off"
  // (toInt() returns zero when formatting is bad).
  this->MSAAComboBox->addItem("Off", 0);
  this->MSAAComboBox->addItem("Auto", -1);
  this->MSAAComboBox->addItem("2x", 2);
  this->MSAAComboBox->addItem("4x", 4);
  this->MSAAComboBox->addItem("8x", 8);
  this->MSAAComboBox->addItem("16x", 16);
  this->MSAAComboBox->setCurrentIndex(this->MSAAComboBox->findText("Off"));

  // Actions to propagate to the application when settings are changed
  QObject::connect(this->MSAAComboBox, &QComboBox::currentTextChanged, q, &qSlicerSettingsViewsPanel::onMSAAChanged);
  QObject::connect(this->MSAAComboBox, &QComboBox::currentTextChanged, q, &qSlicerSettingsViewsPanel::currentMSAAChanged);
  q->registerProperty(
    "Views/MSAA", q, "currentMSAA", SIGNAL(currentMSAAChanged(QString)), qSlicerSettingsViewsPanel::tr("Multisampling (MSAA)"), ctkSettingsPanel::OptionRequireRestart);

  this->SliceOrientationMarkerTypeComboBox->addItem(qSlicerSettingsViewsPanel::tr("none"), QString(/*no tr*/ "none"));
  this->SliceOrientationMarkerTypeComboBox->addItem(qSlicerSettingsViewsPanel::tr("cube"), QString(/*no tr*/ "cube"));
  this->SliceOrientationMarkerTypeComboBox->addItem(qSlicerSettingsViewsPanel::tr("human"), QString(/*no tr*/ "human"));
  this->SliceOrientationMarkerTypeComboBox->addItem(qSlicerSettingsViewsPanel::tr("axes"), QString(/*no tr*/ "axes"));

  QObject::connect(this->SliceOrientationMarkerTypeComboBox, &QComboBox::currentTextChanged, q, &qSlicerSettingsViewsPanel::currentSliceOrientationMarkerTypeChanged);
  q->registerProperty("DefaultSliceView/OrientationMarkerType",
                      q,
                      "sliceOrientationMarkerType",
                      SIGNAL(currentSliceOrientationMarkerTypeChanged(QString)),
                      qSlicerSettingsViewsPanel::tr("Slice view orientation marker type"),
                      ctkSettingsPanel::OptionRequireRestart);
  QObject::connect(this->SliceOrientationMarkerSizeComboBox, &QComboBox::currentTextChanged, q, &qSlicerSettingsViewsPanel::currentSliceOrientationMarkerSizeChanged);
  q->registerProperty("DefaultSliceView/OrientationMarkerSize",
                      q,
                      "sliceOrientationMarkerSize",
                      SIGNAL(currentSliceOrientationMarkerSizeChanged(QString)),
                      qSlicerSettingsViewsPanel::tr("Slice view orientation marker size"),
                      ctkSettingsPanel::OptionRequireRestart);
  QObject::connect(this->SliceRulerTypeComboBox, &QComboBox::currentTextChanged, q, &qSlicerSettingsViewsPanel::currentSliceRulerTypeChanged);
  q->registerProperty("DefaultSliceView/RulerType",
                      q,
                      "sliceRulerType",
                      SIGNAL(currentSliceRulerTypeChanged(QString)),
                      qSlicerSettingsViewsPanel::tr("Slice view ruler type"),
                      ctkSettingsPanel::OptionRequireRestart);

  this->SliceViewOrientationComboBox->addItem(qSlicerSettingsViewsPanel::tr("patient right is screen left (default)"), QString("PatientRightIsScreenLeft"));
  this->SliceViewOrientationComboBox->addItem(qSlicerSettingsViewsPanel::tr("patient right is screen right"), QString("PatientRightIsScreenRight"));
  q->registerProperty("DefaultSliceView/Orientation",
                      this->SliceViewOrientationComboBox,
                      "currentUserDataAsString",
                      SIGNAL(currentIndexChanged(int)),
                      qSlicerSettingsViewsPanel::tr("Default slice view orientation"),
                      ctkSettingsPanel::OptionRequireRestart);
  QObject::connect(this->SliceViewOrientationComboBox, SIGNAL(activated(int)), q, SLOT(sliceViewOrientationChangedByUser()));

  q->registerProperty("DefaultSliceView/SliceEdgeVisibility3D",
                      this->SliceEdgeVisibility3DCheckBox,
                      /*no tr*/ "checked",
                      SIGNAL(toggled(bool)),
                      qSlicerSettingsViewsPanel::tr("Slice edge visibility in 3D views"),
                      ctkSettingsPanel::OptionRequireRestart);

  q->registerProperty("Default3DView/BoxVisibility",
                      this->ThreeDBoxVisibilityCheckBox,
                      /*no tr*/ "checked",
                      SIGNAL(toggled(bool)),
                      qSlicerSettingsViewsPanel::tr("3D view cube visibility"));
  q->registerProperty("Default3DView/AxisLabelsVisibility",
                      this->ThreeDAxisLabelsVisibilityCheckBox,
                      /*no tr*/ "checked",
                      SIGNAL(toggled(bool)),
                      qSlicerSettingsViewsPanel::tr("3D view axis label visibility"));
  QObject::connect(this->ThreeDOrientationMarkerTypeComboBox, &QComboBox::currentTextChanged, q, &qSlicerSettingsViewsPanel::currentThreeDOrientationMarkerTypeChanged);
  q->registerProperty("Default3DView/OrientationMarkerType",
                      q,
                      "threeDOrientationMarkerType",
                      SIGNAL(currentThreeDOrientationMarkerTypeChanged(QString)),
                      qSlicerSettingsViewsPanel::tr("3D view orientation marker type"),
                      ctkSettingsPanel::OptionRequireRestart);
  QObject::connect(this->ThreeDOrientationMarkerSizeComboBox, &QComboBox::currentTextChanged, q, &qSlicerSettingsViewsPanel::currentThreeDOrientationMarkerSizeChanged);
  q->registerProperty("Default3DView/OrientationMarkerSize",
                      q,
                      "threeDOrientationMarkerSize",
                      SIGNAL(currentThreeDOrientationMarkerSizeChanged(QString)),
                      qSlicerSettingsViewsPanel::tr("3D view orientation marker size"),
                      ctkSettingsPanel::OptionRequireRestart);
  QObject::connect(this->ThreeDRulerTypeComboBox, &QComboBox::currentTextChanged, q, &qSlicerSettingsViewsPanel::currentThreeDRulerTypeChanged);
  q->registerProperty("Default3DView/RulerType",
                      q,
                      "threeDRulerType",
                      SIGNAL(currentThreeDRulerTypeChanged(QString)),
                      qSlicerSettingsViewsPanel::tr("3D view ruler type"),
                      ctkSettingsPanel::OptionRequireRestart);
  q->registerProperty("Default3DView/UseDepthPeeling",
                      this->ThreeDUseDepthPeelingCheckBox,
                      /*no tr*/ "checked",
                      SIGNAL(toggled(bool)),
                      qSlicerSettingsViewsPanel::tr("3D depth peeling"));
  q->registerProperty("Default3DView/UseOrthographicProjection",
                      this->ThreeDUseOrthographicProjectionCheckBox,
                      /*no tr*/ "checked",
                      SIGNAL(toggled(bool)),
                      qSlicerSettingsViewsPanel::tr("Orthographic projection"));
  q->registerProperty("Default3DView/ShadowsVisibility",
                      this->ThreeDShadowsVisibilityCheckBox,
                      /*no tr*/ "checked",
                      SIGNAL(toggled(bool)),
                      qSlicerSettingsViewsPanel::tr("Shadows visibility"));
  q->registerProperty("Default3DView/AmbientShadowsSizeScale",
                      this->ThreeDAmbientShadowsSizeScaleSlider,
                      /*no tr*/ "value",
                      SIGNAL(valueChanged(double)),
                      qSlicerSettingsViewsPanel::tr("Ambient shadows size scale"));
  q->registerProperty("Default3DView/AmbientShadowsVolumeOpacityThreshold",
                      this->ThreeDAmbientShadowsVolumeOpacityThresholdSlider,
                      /*no tr*/ "value",
                      SIGNAL(valueChanged(double)),
                      qSlicerSettingsViewsPanel::tr("Ambient shadows volume opacity threshold"));
  q->registerProperty("Default3DView/AmbientShadowsIntensityScale",
                      this->ThreeDAmbientShadowsIntensityScaleSlider,
                      /*no tr*/ "value",
                      SIGNAL(valueChanged(double)),
                      qSlicerSettingsViewsPanel::tr("Ambient shadows intensity scale"));
  q->registerProperty("Default3DView/AmbientShadowsIntensityShift",
                      this->ThreeDAmbientShadowsIntensityShiftSlider,
                      /*no tr*/ "value",
                      SIGNAL(valueChanged(double)),
                      qSlicerSettingsViewsPanel::tr("Ambient shadows intensity shift"));
}

// --------------------------------------------------------------------------
void qSlicerSettingsViewsPanelPrivate::updateThreeDViewDefaultsWidgets()
{
  ThreeDViewDefaults defaults = ThreeDViewDefaults::current();
  if (!defaults.isValid())
  {
    return;
  }
  QSignalBlocker blocker1(this->ThreeDBackgroundColorButton);
  QSignalBlocker blocker2(this->ThreeDBackgroundColor2Button);
  QSignalBlocker blocker3(this->ThreeDBoxVisibleCheckBox);
  QSignalBlocker blocker4(this->ThreeDBoxColorButton);
  this->ThreeDBackgroundColorButton->setColor(defaults.BackgroundColor);
  this->ThreeDBackgroundColor2Button->setColor(defaults.BackgroundColor2);
  this->ThreeDBoxVisibleCheckBox->setChecked(defaults.BoxVisible);
  this->ThreeDBoxColorButton->setColor(defaults.BoxColor);
  this->ThreeDBoxColorButton->setEnabled(defaults.BoxVisible);
}

// --------------------------------------------------------------------------
void qSlicerSettingsViewsPanelPrivate::setThreeDViewDefault(const QString& name, const QVariant& value)
{
  QSettings settings;
  if (value.userType() == QMetaType::QColor)
  {
    settings.setValue(ThreeDViewDefaultsGroup + name, value.value<QColor>().name());
  }
  else
  {
    settings.setValue(ThreeDViewDefaultsGroup + name, value);
  }
  this->updateThreeDViewDefaults();
}

// --------------------------------------------------------------------------
void qSlicerSettingsViewsPanelPrivate::updateThreeDViewDefaults()
{
  ThreeDViewDefaults defaults = ThreeDViewDefaults::current();
  if (defaults != this->AppliedThreeDViewDefaults)
  {
    applyThreeDViewDefaults(this->AppliedThreeDViewDefaults, defaults);
    this->AppliedThreeDViewDefaults = defaults;
  }
  this->updateThreeDViewDefaultsWidgets();
}

// --------------------------------------------------------------------------
// qSlicerSettingsViewsPanel methods

// --------------------------------------------------------------------------
qSlicerSettingsViewsPanel::qSlicerSettingsViewsPanel(QWidget* _parent)
  : Superclass(_parent)
  , d_ptr(new qSlicerSettingsViewsPanelPrivate(*this))
{
  Q_D(qSlicerSettingsViewsPanel);
  d->init();

  // The settings of the panel are loaded when the panel is added to the settings dialog,
  // but the default style of the view controller bars must be known before the views are created.
  // The settings dialog is created before the main window, so this is early enough.
  // Without main window (batch processing), the classic bars are kept.
  qSlicerApplication* app = qSlicerApplication::application();
  QSettings* settings = app ? app->userSettings() : nullptr;
  bool defaultFlatViewBars = !qSlicerApplication::testAttribute(qSlicerCoreApplication::AA_EnableTesting);
  bool flatViewBars = settings ? settings->value("Appearance/FlatViewBars", defaultFlatViewBars).toBool() : defaultFlatViewBars;
  if (app && app->commandOptions() && app->commandOptions()->noMainWindow())
  {
    flatViewBars = false;
  }
  qMRMLViewControllerBar::setDefaultFlatStyle(flatViewBars);
}

// --------------------------------------------------------------------------
bool qSlicerSettingsViewsPanel::threeDViewThemeDefaultsEnabled()
{
  qSlicerApplication* app = qSlicerApplication::application();
  if (!app || app->testAttribute(qSlicerCoreApplication::AA_EnableTesting))
  {
    return false;
  }
  return !(app->commandOptions() && app->commandOptions()->noMainWindow());
}

// --------------------------------------------------------------------------
QVariant qSlicerSettingsViewsPanel::threeDViewThemeDefault(const QString& name)
{
  if (!qSlicerSettingsViewsPanel::threeDViewThemeDefaultsEnabled())
  {
    return QVariant();
  }
  // Graphite backgrounds, darker at the top as the classic background
  bool dark = qApp->palette().color(QPalette::Window).lightness() < 128;
  if (name == "BackgroundColor")
  {
    return QColor(dark ? "#2b2e34" : "#f2f2f5");
  }
  else if (name == "BackgroundColor2")
  {
    return QColor(dark ? "#121316" : "#d9dae0");
  }
  else if (name == "BoxVisible")
  {
    // The box is hidden on dark backgrounds, and if the user chose to hide the cube
    // in the settings of other Slicer installations.
    QSettings settings;
    return !dark && settings.value("Default3DView/BoxVisibility", true).toBool();
  }
  else if (name == "BoxColor")
  {
    return QColor(dark ? "#6b7079" : "#9a9aa3");
  }
  qWarning() << Q_FUNC_INFO << " failed: unknown setting" << name;
  return QVariant();
}

// --------------------------------------------------------------------------
QVariant qSlicerSettingsViewsPanel::threeDViewDefault(const QString& name)
{
  QVariant themeDefault = qSlicerSettingsViewsPanel::threeDViewThemeDefault(name);
  if (!themeDefault.isValid())
  {
    return themeDefault;
  }
  QSettings settings;
  QVariant value = settings.value(ThreeDViewDefaultsGroup + name);
  if (!value.isValid())
  {
    return themeDefault;
  }
  if (themeDefault.userType() == QMetaType::QColor)
  {
    QColor color = value.userType() == QMetaType::QColor ? value.value<QColor>() : QColor(value.toString());
    return color.isValid() ? QVariant(color) : themeDefault;
  }
  return value.toBool();
}

// --------------------------------------------------------------------------
void qSlicerSettingsViewsPanel::onThreeDBackgroundColorChanged(const QColor& color)
{
  Q_D(qSlicerSettingsViewsPanel);
  d->setThreeDViewDefault("BackgroundColor", color);
}

// --------------------------------------------------------------------------
void qSlicerSettingsViewsPanel::onThreeDBackgroundColor2Changed(const QColor& color)
{
  Q_D(qSlicerSettingsViewsPanel);
  d->setThreeDViewDefault("BackgroundColor2", color);
}

// --------------------------------------------------------------------------
void qSlicerSettingsViewsPanel::onThreeDBoxVisibleToggled(bool visible)
{
  Q_D(qSlicerSettingsViewsPanel);
  d->setThreeDViewDefault("BoxVisible", visible);
}

// --------------------------------------------------------------------------
void qSlicerSettingsViewsPanel::onThreeDBoxColorChanged(const QColor& color)
{
  Q_D(qSlicerSettingsViewsPanel);
  d->setThreeDViewDefault("BoxColor", color);
}

// --------------------------------------------------------------------------
void qSlicerSettingsViewsPanel::resetThreeDViewDefaults()
{
  Q_D(qSlicerSettingsViewsPanel);
  QSettings settings;
  for (const char* name : { "BackgroundColor", "BackgroundColor2", "BoxVisible", "BoxColor" })
  {
    settings.remove(ThreeDViewDefaultsGroup + QString(name));
  }
  d->updateThreeDViewDefaults();
}

// --------------------------------------------------------------------------
void qSlicerSettingsViewsPanel::changeEvent(QEvent* event)
{
  Q_D(qSlicerSettingsViewsPanel);
  this->Superclass::changeEvent(event);
  if (event->type() == QEvent::ApplicationPaletteChange || event->type() == QEvent::PaletteChange)
  {
    // The defaults follow the theme (dark or light)
    d->updateThreeDViewDefaults();
  }
}

// --------------------------------------------------------------------------
qSlicerSettingsViewsPanel::~qSlicerSettingsViewsPanel() = default;

// --------------------------------------------------------------------------
void qSlicerSettingsViewsPanel::onMSAAChanged(const QString& text)
{
  /// For "ctkVTKAbstractView"s (the main data views for the program),
  /// the multisampling properties should be set *before* creating any
  /// OpenGL contexts, otherwise the setting may have no effect. This
  /// means that we must read in the user's MSAA settings from QSettings
  /// before setting up the MainWindow UI, since setting up the UI
  /// creates all the view panels (and their associated OpenGL contexts).
  ///
  /// Before the MainWindow is set up, the ViewsPanel is added to the
  /// main settings dialog, . If the saved value is true (the default is
  /// false), this triggers this method to be called, allowing it to be
  /// set prior to creation of the OpenGL contexts.

  Q_D(const qSlicerSettingsViewsPanel);
  const int index = d->MSAAComboBox->findText(text);
  const int nSamples = d->MSAAComboBox->itemData(index).toInt();
  ctkVTKAbstractView::setMultiSamples(nSamples);
}

// --------------------------------------------------------------------------
QString qSlicerSettingsViewsPanel::currentMSAA() const
{
  Q_D(const qSlicerSettingsViewsPanel);
  return d->MSAAComboBox->currentText();
}

// --------------------------------------------------------------------------
void qSlicerSettingsViewsPanel::setCurrentMSAA(const QString& text)
{
  Q_D(qSlicerSettingsViewsPanel);
  // default to "Off" (0) if conversion fails
  d->MSAAComboBox->setCurrentIndex(qMax(d->MSAAComboBox->findText(text), 0));
}

// --------------------------------------------------------------------------
QString qSlicerSettingsViewsPanel::sliceOrientationMarkerType() const
{
  Q_D(const qSlicerSettingsViewsPanel);
  return d->SliceOrientationMarkerTypeComboBox->currentText();
}

// --------------------------------------------------------------------------
void qSlicerSettingsViewsPanel::setSliceOrientationMarkerType(const QString& text)
{
  Q_D(qSlicerSettingsViewsPanel);
  // default to first item if conversion fails
  d->SliceOrientationMarkerTypeComboBox->setCurrentIndex(qMax(d->SliceOrientationMarkerTypeComboBox->findText(text), 0));
}

// --------------------------------------------------------------------------
QString qSlicerSettingsViewsPanel::sliceOrientationMarkerSize() const
{
  Q_D(const qSlicerSettingsViewsPanel);
  return d->SliceOrientationMarkerSizeComboBox->currentText();
}

// --------------------------------------------------------------------------
void qSlicerSettingsViewsPanel::setSliceOrientationMarkerSize(const QString& text)
{
  Q_D(qSlicerSettingsViewsPanel);
  // default to first item if conversion fails
  d->SliceOrientationMarkerSizeComboBox->setCurrentIndex(qMax(d->SliceOrientationMarkerSizeComboBox->findText(text), 0));
}

// --------------------------------------------------------------------------
QString qSlicerSettingsViewsPanel::sliceRulerType() const
{
  Q_D(const qSlicerSettingsViewsPanel);
  return d->SliceRulerTypeComboBox->currentText();
}

// --------------------------------------------------------------------------
void qSlicerSettingsViewsPanel::setSliceRulerType(const QString& text)
{
  Q_D(qSlicerSettingsViewsPanel);
  // default to first item if conversion fails
  d->SliceRulerTypeComboBox->setCurrentIndex(qMax(d->SliceRulerTypeComboBox->findText(text), 0));
}

// --------------------------------------------------------------------------
QString qSlicerSettingsViewsPanel::threeDOrientationMarkerType() const
{
  Q_D(const qSlicerSettingsViewsPanel);
  return d->ThreeDOrientationMarkerTypeComboBox->currentText();
}

// --------------------------------------------------------------------------
void qSlicerSettingsViewsPanel::setThreeDOrientationMarkerType(const QString& text)
{
  Q_D(qSlicerSettingsViewsPanel);
  // default to first item if conversion fails
  d->ThreeDOrientationMarkerTypeComboBox->setCurrentIndex(qMax(d->ThreeDOrientationMarkerTypeComboBox->findText(text), 0));
}

// --------------------------------------------------------------------------
QString qSlicerSettingsViewsPanel::threeDOrientationMarkerSize() const
{
  Q_D(const qSlicerSettingsViewsPanel);
  return d->ThreeDOrientationMarkerSizeComboBox->currentText();
}

// --------------------------------------------------------------------------
void qSlicerSettingsViewsPanel::setThreeDOrientationMarkerSize(const QString& text)
{
  Q_D(qSlicerSettingsViewsPanel);
  // default to first item if conversion fails
  d->ThreeDOrientationMarkerSizeComboBox->setCurrentIndex(qMax(d->ThreeDOrientationMarkerSizeComboBox->findText(text), 0));
}

// --------------------------------------------------------------------------
QString qSlicerSettingsViewsPanel::threeDRulerType() const
{
  Q_D(const qSlicerSettingsViewsPanel);
  return d->ThreeDRulerTypeComboBox->currentText();
}

// --------------------------------------------------------------------------
void qSlicerSettingsViewsPanel::setThreeDRulerType(const QString& text)
{
  Q_D(qSlicerSettingsViewsPanel);
  // default to first item if conversion fails
  d->ThreeDRulerTypeComboBox->setCurrentIndex(qMax(d->ThreeDRulerTypeComboBox->findText(text), 0));
}

// --------------------------------------------------------------------------
void qSlicerSettingsViewsPanel::sliceViewOrientationChangedByUser()
{
  Q_D(qSlicerSettingsViewsPanel);
  if (d->SliceViewOrientationComboBox->currentUserDataAsString() == "PatientRightIsScreenRight")
  {
    if (d->SliceOrientationMarkerTypeComboBox->currentData() == /*no tr*/ "none")
    {
      // Non-default orientation is chosen and no orientation marker is displayed.
      // To ensure that there is no accidental mixup of orientations, show the orientation marker.
      int index = d->SliceOrientationMarkerTypeComboBox->findData(/*no tr*/ "axes");
      d->SliceOrientationMarkerTypeComboBox->setCurrentIndex(index);
    }
  }
}
