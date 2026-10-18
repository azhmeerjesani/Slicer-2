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

  This file was originally developed by Johan Andruejol, Kitware Inc.
  and was partially funded by NIH grant 3P41RR013218-12S1

==============================================================================*/

// Qt includes
#include <QDebug>
#include <QFileDialog>
#include <QMainWindow>
#include <QSettings>
#include <QStyleFactory>

// QtGUI includes
#include "qSlicerApplication.h"
#include "qSlicerCommandOptions.h"
#include "qSlicerRelativePathMapper.h"
#include "qSlicerSettingsStylesPanel.h"
#include "qSlicerStyle.h"
#include "ui_qSlicerSettingsStylesPanel.h"

// --------------------------------------------------------------------------
// qSlicerSettingsStylesPanelPrivate

//-----------------------------------------------------------------------------
class qSlicerSettingsStylesPanelPrivate : public Ui_qSlicerSettingsStylesPanel
{
  Q_DECLARE_PUBLIC(qSlicerSettingsStylesPanel);
  typedef qSlicerSettingsStylesPanelPrivate Self;

protected:
  qSlicerSettingsStylesPanel* const q_ptr;

public:
  qSlicerSettingsStylesPanelPrivate(qSlicerSettingsStylesPanel& object);
  void init();
  int styleIndex(const QString& styleName) const;
  void populateStyles();

  /// Apply the standard palette of the current application style, if it changed.
  void applyStylePalette();
  /// Show the current accent color in the accent color widgets.
  void updateAccentColorWidgets();
  /// The theme only applies to the "Slicer" style, other styles have their own palette.
  void updateThemeWidgets();
  /// Default accent color of the current palette (the accent color used when none is set).
  QColor defaultAccentColor() const;

  /// Return true if the application palette is dark.
  static bool isDarkPalette();
  /// Return true if the theme and the accent color are not applied: the historical light
  /// appearance is kept when testing is enabled or the application runs without main window.
  static bool isClassicAppearance();

  static bool isQtStyle(const QString& styleName);
  static QStringList qtStyles();

  QStringList AdditionalPaths;
  /// Accent color name, empty for the default accent color
  QString AccentColor;
  bool UpdatingAccentColorWidgets{ false };
};

// --------------------------------------------------------------------------
// qSlicerSettingsStylesPanelPrivate methods

// --------------------------------------------------------------------------
qSlicerSettingsStylesPanelPrivate::qSlicerSettingsStylesPanelPrivate(qSlicerSettingsStylesPanel& object)
  : q_ptr(&object)
{
}

// --------------------------------------------------------------------------
void qSlicerSettingsStylesPanelPrivate::init()
{
  Q_Q(qSlicerSettingsStylesPanel);

  this->setupUi(q);

  // General appearance settings
  QObject::connect(this->FontButton, SIGNAL(currentFontChanged(QFont)), q, SLOT(onFontChanged(QFont)));
  QObject::connect(this->ShowToolTipsCheckBox, SIGNAL(toggled(bool)), q, SLOT(onShowToolTipsToggled(bool)));
  QObject::connect(this->ShowToolButtonTextCheckBox, SIGNAL(toggled(bool)), q, SLOT(onShowToolButtonTextToggled(bool)));

  q->registerProperty("no-tooltip", this->ShowToolTipsCheckBox, /*no tr*/ "checked", SIGNAL(toggled(bool)));
  q->registerProperty("font", this->FontButton, "currentFont", SIGNAL(currentFontChanged(QFont)));
  q->registerProperty("MainWindow/ShowToolButtonText",
                      this->ShowToolButtonTextCheckBox,
                      /*no tr*/ "checked",
                      SIGNAL(toggled(bool)));
  q->registerProperty("MainWindow/RestoreGeometry", this->RestoreUICheckBox, /*no tr*/ "checked", SIGNAL(toggled(bool)));

  // Additional Style paths
  this->AdditionalStylePathMoreButton->setChecked(false);

  // Additional path setting
  QObject::connect(this->AdditionalStylePathsView, SIGNAL(directoryListChanged()), q, SLOT(onAdditionalStylePathsChanged()));
  qSlicerRelativePathMapper* relativePathMapper = new qSlicerRelativePathMapper(this->AdditionalStylePathsView, "directoryList", SIGNAL(directoryListChanged()));
  q->registerProperty("Styles/AdditionalPaths",
                      relativePathMapper,
                      "relativePaths",
                      SIGNAL(relativePathsChanged(QStringList)),
                      qSlicerSettingsStylesPanel::tr("Additional style paths"),
                      ctkSettingsPanel::OptionRequireRestart);

  // Style setting
  // Connect before setting the default style, so that the palette of the style is
  // applied whenever the style is set.
  this->populateStyles();
  QObject::connect(this->StyleComboBox, &QComboBox::currentTextChanged, q, &qSlicerSettingsStylesPanel::onStyleChanged);
  q->setCurrentStyle("Slicer");
  q->registerProperty("Styles/Style", q, "currentStyle", SIGNAL(currentStyleChanged(QString)), qSlicerSettingsStylesPanel::tr("Current style"));

  // Theme, accent color and density settings.
  // These are stored in Appearance/* keys and not in Styles/Style, so that other Slicer
  // installations sharing the same settings file are not affected.
  // The theme and the accent color are already applied at startup by qSlicerApplication.
  this->ThemeComboBox->addItem(qSlicerSettingsStylesPanel::tr("System"), qSlicerStyle::themeToString(qSlicerStyle::SystemTheme));
  this->ThemeComboBox->addItem(qSlicerSettingsStylesPanel::tr("Dark"), qSlicerStyle::themeToString(qSlicerStyle::DarkTheme));
  this->ThemeComboBox->addItem(qSlicerSettingsStylesPanel::tr("Light"), qSlicerStyle::themeToString(qSlicerStyle::LightTheme));
  q->setTheme(qSlicerSettingsStylesPanel::defaultTheme());
  QObject::connect(this->ThemeComboBox, SIGNAL(currentIndexChanged(int)), q, SLOT(onThemeChanged(int)));
  q->registerProperty("Appearance/Theme", q, "theme", SIGNAL(themeChanged(QString)), qSlicerSettingsStylesPanel::tr("Theme"));

  this->updateAccentColorWidgets();
  QObject::connect(this->AccentColorPickerButton, SIGNAL(colorChanged(QColor)), q, SLOT(onAccentColorPicked(QColor)));
  QObject::connect(this->AccentColorResetButton, SIGNAL(clicked()), q, SLOT(onAccentColorReset()));
  q->registerProperty("Appearance/Accent", q, "accentColor", SIGNAL(accentColorChanged(QString)), qSlicerSettingsStylesPanel::tr("Accent color"));

  this->DensityComboBox->addItem(qSlicerSettingsStylesPanel::tr("Comfortable"), qSlicerStyle::densityToString(qSlicerStyle::Comfortable));
  this->DensityComboBox->addItem(qSlicerSettingsStylesPanel::tr("Compact"), qSlicerStyle::densityToString(qSlicerStyle::Compact));
  q->setDensity(qSlicerStyle::densityToString(qSlicerStyle::Comfortable));
  QObject::connect(this->DensityComboBox, SIGNAL(currentIndexChanged(int)), q, SLOT(onDensityChanged(int)));
  q->registerProperty("Appearance/Density", q, "density", SIGNAL(densityChanged(QString)), qSlicerSettingsStylesPanel::tr("Density"), ctkSettingsPanel::OptionRequireRestart);

  this->updateThemeWidgets();

  // Connect AdditionalStylePaths buttons
  QObject::connect(this->AddAdditionalStylePathButton, SIGNAL(clicked()), q, SLOT(onAddStyleAdditionalPathClicked()));
  QObject::connect(this->RemoveAdditionalStylePathButton, SIGNAL(clicked()), q, SLOT(onRemoveStyleAdditionalPathClicked()));
}

// --------------------------------------------------------------------------
int qSlicerSettingsStylesPanelPrivate::styleIndex(const QString& styleName) const
{
  int styleIndex = this->StyleComboBox->findText(styleName, Qt::MatchFixedString);
  if (styleIndex == -1)
  {
    // if not found (call setCurrentStyle with a wrong style for example),
    // default to slicer's style
    styleIndex = this->StyleComboBox->findText("Slicer", Qt::MatchFixedString);
  }
  return styleIndex;
}

// --------------------------------------------------------------------------
void qSlicerSettingsStylesPanelPrivate::applyStylePalette()
{
  qSlicerApplication* app = qSlicerApplication::application();
  if (!app || !app->style())
  {
    return;
  }
  QPalette palette = app->style()->standardPalette();
  // Setting the application palette updates all widgets, skip it if nothing changed.
  if (palette != app->palette())
  {
    app->setPalette(palette);
  }
  this->updateAccentColorWidgets();
}

// --------------------------------------------------------------------------
void qSlicerSettingsStylesPanelPrivate::updateAccentColorWidgets()
{
  QColor accentColor(this->AccentColor);
  bool isDefault = !accentColor.isValid();
  if (isDefault)
  {
    // Show the default accent color of the current palette
    accentColor = this->defaultAccentColor();
  }
  bool wasUpdating = this->UpdatingAccentColorWidgets;
  this->UpdatingAccentColorWidgets = true;
  this->AccentColorPickerButton->setColor(accentColor);
  this->AccentColorPickerButton->setColorName(isDefault ? qSlicerSettingsStylesPanel::tr("Default") : QString());
  this->UpdatingAccentColorWidgets = wasUpdating;
  bool classicAppearance = Self::isClassicAppearance();
  this->AccentColorPickerButton->setEnabled(!classicAppearance);
  this->AccentColorResetButton->setEnabled(!isDefault && !classicAppearance);
  this->AccentColorPickerButton->setToolTip(
    classicAppearance ? qSlicerSettingsStylesPanel::tr("The accent color is not applied when testing is enabled or the application runs without main window.")
                      : qSlicerSettingsStylesPanel::tr("Color of selected items, links and highlighted controls."));
}

// --------------------------------------------------------------------------
QColor qSlicerSettingsStylesPanelPrivate::defaultAccentColor() const
{
  // tokenColor() returns the accent color override if one is set, so clear it temporarily.
  QColor accentColor = qSlicerStyle::accentColor();
  qSlicerStyle::setAccentColor(QColor());
  QColor defaultColor = qSlicerStyle::tokenColor("accent", Self::isDarkPalette());
  qSlicerStyle::setAccentColor(accentColor);
  return defaultColor;
}

// --------------------------------------------------------------------------
bool qSlicerSettingsStylesPanelPrivate::isDarkPalette()
{
  qSlicerApplication* app = qSlicerApplication::application();
  return app && app->palette().color(QPalette::Window).lightness() < 128;
}

// --------------------------------------------------------------------------
bool qSlicerSettingsStylesPanelPrivate::isClassicAppearance()
{
  if (qSlicerCoreApplication::testAttribute(qSlicerCoreApplication::AA_EnableTesting))
  {
    return true;
  }
  qSlicerApplication* app = qSlicerApplication::application();
  qSlicerCommandOptions* options = app ? app->commandOptions() : nullptr;
  return options && options->noMainWindow();
}

// --------------------------------------------------------------------------
void qSlicerSettingsStylesPanelPrivate::updateThemeWidgets()
{
  Q_Q(qSlicerSettingsStylesPanel);
  bool slicerStyle = (q->currentStyle().compare("Slicer", Qt::CaseInsensitive) == 0);
  bool classicAppearance = Self::isClassicAppearance();
  this->ThemeComboBox->setEnabled(slicerStyle && !classicAppearance);
  if (!slicerStyle)
  {
    this->ThemeComboBox->setToolTip(qSlicerSettingsStylesPanel::tr("The colors are set by the '%1' style (see Advanced section). "
                                                                   "Select the 'Slicer' style to choose the theme here.")
                                      .arg(q->currentStyle()));
  }
  else if (classicAppearance)
  {
    this->ThemeComboBox->setToolTip(qSlicerSettingsStylesPanel::tr("The light theme is always used when testing is enabled or the application runs without main window."));
  }
  else
  {
    this->ThemeComboBox->setToolTip(qSlicerSettingsStylesPanel::tr("Color theme of the application."));
  }
}

namespace
{
QString toCamelCase(const QString& s)
{
  QString camelCase;
  if (s.size() <= 0)
  {
    return camelCase;
  }

  camelCase = s[0].toUpper();
  for (int i = 1; i < s.size(); ++i)
  {
    if (s[i - 1] == ' ')
    {
      camelCase.append(s[i].toUpper());
    }
    else
    {
      camelCase.append(s[i]);
    }
  };
  return camelCase;
}
} // namespace

// --------------------------------------------------------------------------
void qSlicerSettingsStylesPanelPrivate::populateStyles()
{
  Q_Q(qSlicerSettingsStylesPanel);
  QString currentStyle = this->StyleComboBox->currentText();

  bool wasBlocking = this->StyleComboBox->blockSignals(true);
  // Re-populate styles
  this->StyleComboBox->clear();
  for (const QString& style : q->availableSlicerStyles())
  {
    this->StyleComboBox->addItem(toCamelCase(style));
  }

  // Find the previously set style
  int currentStyleIndex = this->styleIndex(currentStyle);
  this->StyleComboBox->blockSignals(wasBlocking);

  this->StyleComboBox->setCurrentIndex(currentStyleIndex);
}

// --------------------------------------------------------------------------
QStringList qSlicerSettingsStylesPanelPrivate::qtStyles()
{
  return QStringList() //
         << "Windows"
         << "WindowsCE"
         << "WindowsXP"
         << "WindowsVista"
         << "Motif"
         << "CDE"
         << "Plastique"
         << "Cleanlooks"
         << "Macintosh"
         << "Macintosh (aqua)"
         << "GTK+"
         << "Fusion";
}

// --------------------------------------------------------------------------
bool qSlicerSettingsStylesPanelPrivate::isQtStyle(const QString& styleName)
{
  // Styles are case insensitive
  return Self::qtStyles().contains(styleName, Qt::CaseInsensitive);
}

// --------------------------------------------------------------------------
// qSlicerSettingsStylesPanel methods

// --------------------------------------------------------------------------
qSlicerSettingsStylesPanel::qSlicerSettingsStylesPanel(QWidget* _parent)
  : Superclass(_parent)
  , d_ptr(new qSlicerSettingsStylesPanelPrivate(*this))
{
  Q_D(qSlicerSettingsStylesPanel);
  d->init();
}

// --------------------------------------------------------------------------
qSlicerSettingsStylesPanel::~qSlicerSettingsStylesPanel() = default;

// --------------------------------------------------------------------------
void qSlicerSettingsStylesPanel::onAdditionalStylePathsChanged()
{
  Q_D(qSlicerSettingsStylesPanel);

  // Remove old paths
  for (const QString& path : d->AdditionalPaths)
  {
    QCoreApplication::removeLibraryPath(path);
  }

  // Add new ones
  d->AdditionalPaths = d->AdditionalStylePathsView->directoryList(true);
  for (const QString& path : d->AdditionalPaths)
  {
    QCoreApplication::addLibraryPath(path);
  }

  d->populateStyles();
}

// --------------------------------------------------------------------------
void qSlicerSettingsStylesPanel::onAddStyleAdditionalPathClicked()
{
  Q_D(qSlicerSettingsStylesPanel);
  qSlicerCoreApplication* coreApp = qSlicerCoreApplication::application();
  QString extensionInstallPath = coreApp->toSlicerHomeAbsolutePath(coreApp->revisionUserSettings()->value("Extensions/InstallPath").toString());
  QString path = QFileDialog::getExistingDirectory(this, tr("Select a path containing a \"styles\" plugin directory"), extensionInstallPath);
  // An empty directory means that the user cancelled the dialog.
  if (path.isEmpty())
  {
    return;
  }
  d->AdditionalStylePathsView->addDirectory(path);
}

// --------------------------------------------------------------------------
void qSlicerSettingsStylesPanel::onRemoveStyleAdditionalPathClicked()
{
  Q_D(qSlicerSettingsStylesPanel);
  // Remove all selected
  d->AdditionalStylePathsView->removeSelectedDirectories();
}

// --------------------------------------------------------------------------
QStringList qSlicerSettingsStylesPanel::availableSlicerStyles()
{
  QStringList styles;
  for (const QString& style : QStyleFactory::keys())
  {
    if (qSlicerSettingsStylesPanelPrivate::isQtStyle(style))
    {
      continue;
    }
    styles << style;
  }
  return styles;
}

// --------------------------------------------------------------------------
QString qSlicerSettingsStylesPanel::currentStyle() const
{
  Q_D(const qSlicerSettingsStylesPanel);
  return d->StyleComboBox->currentText();
}

// --------------------------------------------------------------------------
void qSlicerSettingsStylesPanel::setCurrentStyle(const QString& newStyleName)
{
  Q_D(qSlicerSettingsStylesPanel);
  d->StyleComboBox->setCurrentIndex(d->styleIndex(newStyleName));
}

// --------------------------------------------------------------------------
void qSlicerSettingsStylesPanel::onStyleChanged(const QString& newStyleName)
{
  Q_D(qSlicerSettingsStylesPanel);
  qSlicerApplication* app = qSlicerApplication::application();
  app->removeEventFilter(app->style());

  QStyle* newStyle = QStyleFactory::create(newStyleName);
  if (!newStyle)
  {
    qWarning() << "Style named " << newStyleName << " not found !"
               << " Defaulting to Slicer's style.";
    this->setCurrentStyle("Slicer");
    return;
  }

  app->setStyle(newStyle);
  app->installEventFilter(app->style());
  app->setPalette(newStyle->standardPalette());

  d->updateThemeWidgets();
  d->updateAccentColorWidgets();

  emit this->currentStyleChanged(newStyleName);
}

// --------------------------------------------------------------------------
QString qSlicerSettingsStylesPanel::defaultTheme()
{
  QString theme;
  qSlicerCoreApplication* coreApp = qSlicerCoreApplication::application();
  if (coreApp && coreApp->defaultSettings())
  {
    theme = coreApp->defaultSettings()->value("Appearance/ForkDefaultTheme").toString();
  }
  return qSlicerStyle::themeToString(qSlicerStyle::themeFromString(theme, qSlicerStyle::DarkTheme));
}

// --------------------------------------------------------------------------
QString qSlicerSettingsStylesPanel::theme() const
{
  Q_D(const qSlicerSettingsStylesPanel);
  return d->ThemeComboBox->currentData().toString();
}

// --------------------------------------------------------------------------
void qSlicerSettingsStylesPanel::setTheme(const QString& theme)
{
  Q_D(qSlicerSettingsStylesPanel);
  QString themeName = qSlicerStyle::themeToString(qSlicerStyle::themeFromString(theme, qSlicerStyle::themeFromString(qSlicerSettingsStylesPanel::defaultTheme())));
  d->ThemeComboBox->setCurrentIndex(d->ThemeComboBox->findData(themeName));
}

// --------------------------------------------------------------------------
void qSlicerSettingsStylesPanel::onThemeChanged(int index)
{
  Q_D(qSlicerSettingsStylesPanel);
  QString themeName = d->ThemeComboBox->itemData(index).toString();
  qSlicerStyle::setTheme(qSlicerStyle::themeFromString(themeName));
  d->applyStylePalette();
  emit this->themeChanged(themeName);
}

// --------------------------------------------------------------------------
QString qSlicerSettingsStylesPanel::accentColor() const
{
  Q_D(const qSlicerSettingsStylesPanel);
  return d->AccentColor;
}

// --------------------------------------------------------------------------
void qSlicerSettingsStylesPanel::setAccentColor(const QString& colorName)
{
  Q_D(qSlicerSettingsStylesPanel);
  QColor color(colorName);
  QString newColorName = color.isValid() ? color.name() : QString();
  if (newColorName == d->AccentColor)
  {
    return;
  }
  d->AccentColor = newColorName;
  qSlicerStyle::setAccentColor(color);
  d->applyStylePalette();
  emit this->accentColorChanged(newColorName);
}

// --------------------------------------------------------------------------
void qSlicerSettingsStylesPanel::onAccentColorPicked(const QColor& color)
{
  Q_D(qSlicerSettingsStylesPanel);
  if (d->UpdatingAccentColorWidgets)
  {
    return;
  }
  // Picking the default accent color of the current theme restores the default accent color,
  // so that each theme keeps its own default.
  this->setAccentColor(color == d->defaultAccentColor() ? QString() : color.name());
}

// --------------------------------------------------------------------------
void qSlicerSettingsStylesPanel::onAccentColorReset()
{
  this->setAccentColor(QString());
}

// --------------------------------------------------------------------------
QString qSlicerSettingsStylesPanel::density() const
{
  Q_D(const qSlicerSettingsStylesPanel);
  return d->DensityComboBox->currentData().toString();
}

// --------------------------------------------------------------------------
void qSlicerSettingsStylesPanel::setDensity(const QString& density)
{
  Q_D(qSlicerSettingsStylesPanel);
  QString densityName = qSlicerStyle::densityToString(qSlicerStyle::densityFromString(density));
  d->DensityComboBox->setCurrentIndex(d->DensityComboBox->findData(densityName));
}

// --------------------------------------------------------------------------
void qSlicerSettingsStylesPanel::onDensityChanged(int index)
{
  Q_D(qSlicerSettingsStylesPanel);
  // The density is applied by qSlicerApplication at startup (the setting requires a restart).
  emit this->densityChanged(d->DensityComboBox->itemData(index).toString());
}

// --------------------------------------------------------------------------
void qSlicerSettingsStylesPanel::onFontChanged(const QFont& font)
{
  qSlicerApplication::application()->setFont(font);
}

// --------------------------------------------------------------------------
void qSlicerSettingsStylesPanel::onShowToolTipsToggled(bool disable)
{
  qSlicerApplication::application()->setToolTipsEnabled(!disable);
}

// --------------------------------------------------------------------------
void qSlicerSettingsStylesPanel::onShowToolButtonTextToggled(bool enable)
{
  QMainWindow* mainWindow = qSlicerApplication::application()->mainWindow();
  if (mainWindow)
  {
    mainWindow->setToolButtonStyle(enable ? Qt::ToolButtonTextUnderIcon : Qt::ToolButtonIconOnly);
  }
}
