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

#ifndef __qSlicerStyle_h
#define __qSlicerStyle_h

// Qt includes
#include <QColor>

// CTK includes
#include <ctkProxyStyle.h>

// Slicer includes
#include "qSlicerBaseQTGUIExport.h"

class Q_SLICER_BASE_QTGUI_EXPORT qSlicerStyle : public ctkProxyStyle
{
  Q_OBJECT
public:
  /// Superclass typedef
  typedef ctkProxyStyle Superclass;

  /// Spacing of the user interface elements.
  /// \sa setDensity(), density()
  enum Density
  {
    /// Historical Slicer layout spacing and margins.
    Compact = 0,
    /// Slightly larger layout spacing and margins.
    Comfortable
  };
  Q_ENUM(Density)

  /// Color theme followed by the standard palette of the "Slicer" style.
  /// The "Dark Slicer" and "Light Slicer" styles always use their own palette.
  /// \sa setTheme(), theme(), standardPalette()
  enum Theme
  {
    /// Follow the operating system setting (Windows only, light otherwise).
    SystemTheme = 0,
    DarkTheme,
    LightTheme
  };
  Q_ENUM(Theme)

  /// Constructors
  qSlicerStyle();
  ~qSlicerStyle() override;

  SubControl hitTestComplexControl(ComplexControl cc, const QStyleOptionComplex* opt, const QPoint& pt, const QWidget* widget) const override;

  int pixelMetric(PixelMetric metric, const QStyleOption* option = nullptr, const QWidget* widget = nullptr) const override;

  /// Return the dark or light palette depending on theme().
  /// When testing is enabled or the application runs without main window,
  /// the light palette is always returned (historical appearance).
  QPalette standardPalette() const override;
  QPalette standardLightPalette() const;
  QPalette standardDarkPalette() const;
  int styleHint(StyleHint hint, const QStyleOption* opt, const QWidget* widget, QStyleHintReturn* returnData) const override;

  /// Draw check box and radio button indicators with a visible frame and an accent fill
  /// on dark palettes. Other primitives (and all primitives on light palettes) are drawn
  /// by the base style.
  void drawPrimitive(PrimitiveElement element, const QStyleOption* option, QPainter* painter, const QWidget* widget = nullptr) const override;

  /// Return the color of a named style token of the dark or light theme.
  /// Supported names: canvas, card, control-hover, control-pressed, hairline, border,
  /// text2, text3, accent, accent-hover, accent-pressed, accent-fg, accent-subtle,
  /// success, warning, danger, view-bar and lesion.
  /// Accent tokens follow accentColor() if it is set (except when testing is enabled or the
  /// application runs without main window).
  /// An invalid color is returned for unknown names.
  static QColor tokenColor(const QString& name, bool dark);

  /// Return the color of a named style token for the current application palette.
  /// \sa tokenColor(), isDarkPalette()
  Q_INVOKABLE QColor token(const QString& name) const;

  /// Return true if the current application palette is dark (window color lightness < 128).
  Q_INVOKABLE bool isDarkPalette() const;

  /// Set the density used by all Slicer styles. It is read by pixelMetric(), therefore
  /// widgets that are already laid out are not updated.
  /// \sa density()
  static void setDensity(Density density);
  /// Return the density in effect. Always Compact when testing is enabled.
  /// qSlicerApplication also sets Compact when the application runs without main window.
  static Density density();

  /// Set the theme followed by standardPalette(). Call QApplication::setPalette()
  /// with the new standard palette to apply it.
  /// \sa theme()
  static void setTheme(Theme theme);
  static Theme theme();

  /// Set the accent color of the palettes (Highlight, Link) and of the accent tokens.
  /// An invalid color restores the default accent color of each theme.
  /// The accent color is ignored when testing is enabled or the application runs without main window.
  /// Call QApplication::setPalette() with the new standard palette to apply it.
  /// \sa accentColor()
  static void setAccentColor(const QColor& color);
  static QColor accentColor();

  /// Convert between a theme and its settings value: "System", "Dark" or "Light" (case insensitive).
  /// \a defaultTheme is returned for unknown names.
  static QString themeToString(Theme theme);
  static Theme themeFromString(const QString& name, Theme defaultTheme = DarkTheme);

  /// Convert between a density and its settings value: "Compact" or "Comfortable" (case insensitive).
  /// \a defaultDensity is returned for unknown names.
  static QString densityToString(Density density);
  static Density densityFromString(const QString& name, Density defaultDensity = Comfortable);

  /// Behavior of widgets can be tweaked if an event filter is installed on a
  /// widget or application.
  /// If activated, the filter:
  ///  * prevents widgets to receive wheel events when they are in a scroll area
  ///    with a visible scrollbar.
  bool eventFilter(QObject* obj, QEvent* event) override;
};

#endif
