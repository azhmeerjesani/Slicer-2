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

#ifndef __qSlicerSettingsViewsPanel_h
#define __qSlicerSettingsViewsPanel_h

// CTK includes
#include <ctkSettingsPanel.h>

// QtGUI includes
#include "qSlicerBaseQTGUIExport.h"

class QSettings;
class qSlicerSettingsViewsPanelPrivate;

class Q_SLICER_BASE_QTGUI_EXPORT qSlicerSettingsViewsPanel : public ctkSettingsPanel
{
  Q_OBJECT
  /// Holds the current MSAA's name for the settings.
  /// The current MSAA setting can be accessed using qSlicerApplication
  /// \sa currentMSAA(), setCurrentMSAA()
  Q_PROPERTY(QString currentMSAA READ currentMSAA WRITE setCurrentMSAA NOTIFY currentMSAAChanged)
  Q_PROPERTY(QString sliceOrientationMarkerType READ sliceOrientationMarkerType WRITE setSliceOrientationMarkerType NOTIFY currentSliceOrientationMarkerTypeChanged)
  Q_PROPERTY(QString sliceOrientationMarkerSize READ sliceOrientationMarkerSize WRITE setSliceOrientationMarkerSize NOTIFY currentSliceOrientationMarkerSizeChanged)
  Q_PROPERTY(QString sliceRulerType READ sliceRulerType WRITE setSliceRulerType NOTIFY currentSliceRulerTypeChanged)
  Q_PROPERTY(QString threeDOrientationMarkerType READ threeDOrientationMarkerType WRITE setThreeDOrientationMarkerType NOTIFY currentThreeDOrientationMarkerTypeChanged)
  Q_PROPERTY(QString threeDOrientationMarkerSize READ threeDOrientationMarkerSize WRITE setThreeDOrientationMarkerSize NOTIFY currentThreeDOrientationMarkerSizeChanged)
  Q_PROPERTY(QString threeDRulerType READ threeDRulerType WRITE setThreeDRulerType NOTIFY currentThreeDRulerTypeChanged)
public:
  /// Superclass typedef
  typedef ctkSettingsPanel Superclass;

  /// Constructor
  explicit qSlicerSettingsViewsPanel(QWidget* parent = nullptr);

  /// Destructor
  ~qSlicerSettingsViewsPanel() override;

  /// Get current value as string
  QString currentMSAA() const;
  QString sliceOrientationMarkerType() const;
  QString sliceOrientationMarkerSize() const;
  QString sliceRulerType() const;
  QString threeDOrientationMarkerType() const;
  QString threeDOrientationMarkerSize() const;
  QString threeDRulerType() const;

  /// Return true if the default background and box of 3D views follow the theme and the
  /// Appearance/Default3DView/* settings (BackgroundColor, BackgroundColor2, BoxVisible, BoxColor).
  /// Return false when testing is enabled or the application runs without main window: the
  /// vtkMRMLViewNode defaults are used then.
  static bool threeDViewThemeDefaultsEnabled();

  /// Default of the 3D view setting \a name (BackgroundColor, BackgroundColor2, BoxVisible or BoxColor)
  /// used when Appearance/Default3DView/<name> is not set. It follows the theme: dark if the
  /// application palette is dark. Colors are returned as QColor, BoxVisible as bool. The box is hidden
  /// if the Default3DView/BoxVisibility setting is false.
  /// Return an invalid QVariant if threeDViewThemeDefaultsEnabled() is false.
  static QVariant threeDViewThemeDefault(const QString& name);

  /// Default of the 3D view setting \a name: the Appearance/Default3DView/<name> setting if it is set,
  /// otherwise threeDViewThemeDefault().
  /// Return an invalid QVariant if threeDViewThemeDefaultsEnabled() is false.
  static QVariant threeDViewDefault(const QString& name);

public slots:
  /// Change the current value based on its name
  void setCurrentMSAA(const QString&);
  void setSliceOrientationMarkerType(const QString&);
  void setSliceOrientationMarkerSize(const QString&);
  void setSliceRulerType(const QString&);
  void setThreeDOrientationMarkerType(const QString&);
  void setThreeDOrientationMarkerSize(const QString&);
  void setThreeDRulerType(const QString&);
  void sliceViewOrientationChangedByUser();

  /// Reimplemented to also handle the "Navigation in Studio" settings (Studio/LeftDragWindowLevel,
  /// Studio/FocusFollowsMouse, Studio/PageStepSlices). They are saved only when the user changes them
  /// and are applied by the Slicer Studio interface while it is active, never by the panel.
  void applySettings() override;
  void resetSettings() override;
  void restoreDefaultSettings() override;
  void reloadSettings() override;

signals:
  /// Signal emitted when the current value is changed
  void currentMSAAChanged(const QString&);
  void currentSliceOrientationMarkerTypeChanged(const QString&);
  void currentSliceOrientationMarkerSizeChanged(const QString&);
  void currentSliceRulerTypeChanged(const QString&);
  void currentThreeDOrientationMarkerTypeChanged(const QString&);
  void currentThreeDOrientationMarkerSizeChanged(const QString&);
  void currentThreeDRulerTypeChanged(const QString&);

protected slots:
  void onMSAAChanged(const QString&);
  void onThreeDBackgroundColorChanged(const QColor&);
  void onThreeDBackgroundColor2Changed(const QColor&);
  void onThreeDBoxVisibleToggled(bool);
  void onThreeDBoxColorChanged(const QColor&);
  void resetThreeDViewDefaults();

protected:
  void changeEvent(QEvent* event) override;

  QScopedPointer<qSlicerSettingsViewsPanelPrivate> d_ptr;

private:
  Q_DECLARE_PRIVATE(qSlicerSettingsViewsPanel);
  Q_DISABLE_COPY(qSlicerSettingsViewsPanel);
};

#endif
