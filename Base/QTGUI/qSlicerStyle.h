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
    /// Follow the operating system setting (see systemTheme()).
    SystemTheme = 0,
    DarkTheme,
    LightTheme
  };
  Q_ENUM(Theme)

  /// Constructors
  /// The style object has the dynamic property "supportsSlicerRole" set to true: it paints
  /// the "slicerRole" widget property (see polish()).
  qSlicerStyle();
  ~qSlicerStyle() override;

  using Superclass::polish;
  using Superclass::unpolish;

  SubControl hitTestComplexControl(ComplexControl cc, const QStyleOptionComplex* opt, const QPoint& pt, const QWidget* widget) const override;

  int pixelMetric(PixelMetric metric, const QStyleOption* option = nullptr, const QWidget* widget = nullptr) const override;

  /// Return the dark or light palette depending on theme().
  /// When testing is enabled or the application runs without main window,
  /// the light palette is always returned (historical appearance).
  QPalette standardPalette() const override;
  QPalette standardLightPalette() const;
  QPalette standardDarkPalette() const;
  int styleHint(StyleHint hint, const QStyleOption* opt, const QWidget* widget, QStyleHintReturn* returnData) const override;

  /// Draw flat controls with the style tokens, on dark and light palettes:
  /// - push buttons and tool buttons (PE_PanelButtonCommand, PE_PanelButtonTool, PE_FrameButtonTool):
  ///   flat, rounded, with hover and pressed fills; checked buttons have the "accent-subtle" fill
  ///   and a 1 px "accent-fg" border, so that the active tool or mode is obvious. Combo boxes keep
  ///   the base style;
  /// - keyboard focus (PE_FrameFocusRect): a 2 px "accent-fg" ring, only for keyboard focus changes;
  /// - input fields (PE_FrameLineEdit): a 1 px "control-border" frame, "accent-fg" when focused;
  /// - arrows (PE_IndicatorArrow*): thin chevrons;
  /// - check boxes and radio buttons: a 1.5 px "control-border" outline and an accent fill when checked;
  /// - tool tips (PE_PanelTipLabel) and menus (PE_PanelMenu, PE_FrameMenu): rounded surfaces with a border;
  /// - tool bar handles and separators: two dots and a hairline;
  /// - frames with the "card" slicerRole (PE_Frame, PE_Widget): rounded cards.
  void drawPrimitive(PrimitiveElement element, const QStyleOption* option, QPainter* painter, const QWidget* widget = nullptr) const override;

  /// Draw flat collapsible button headers (CE_PushButtonBevel of ctkCollapsibleButton), menu items
  /// (CE_MenuItem: rounded selection, check marks, right-aligned shortcuts in "text3"), the frames of
  /// labels and frames with a slicerRole (CE_ShapedFrame: card, chip, kbd, segmented) and tool bars
  /// with the "topBar" slicerRole (CE_ToolBar). Other controls are drawn by the base style.
  void drawControl(ControlElement element, const QStyleOption* option, QPainter* painter, const QWidget* widget = nullptr) const override;

  /// Draw complex controls with the base style. The frames of spin boxes and editable combo boxes
  /// are then drawn like line edit frames, so that input fields have a visible boundary.
  /// Tool buttons with the "ghost", "toolTile" or segmented slicerRole have no menu indicator.
  void drawComplexControl(ComplexControl control, const QStyleOptionComplex* option, QPainter* painter, const QWidget* widget = nullptr) const override;

  /// Draw text with the base style, in SemiBold in ctkCollapsibleButton headers and with the
  /// text color of the slicerRole of the painted widget (e.g. white on "primary" buttons,
  /// "text2" for "secondaryText", "text3" for "caption").
  void drawItemText(QPainter* painter, const QRect& rect, int flags, const QPalette& pal, bool enabled, const QString& text, QPalette::ColorRole textRole = QPalette::NoRole)
    const override;

  /// Return the text rectangle of the base style. Disabled text is not etched
  /// (SH_EtchDisabledText is 0), but the Compact density keeps the text sizes of the base style.
  QRect itemTextRect(const QFontMetrics& metrics, const QRect& rect, int alignment, bool enabled, const QString& text) const override;

  /// Return the size of the base style, with the minimum heights of the Comfortable density
  /// (26 px for push buttons, combo boxes, line edits, spin boxes and menu items, 28 px for
  /// ctkCollapsibleButton headers) and the sizes of the slicerRole widgets ("compact" tool
  /// buttons have no padding). The Compact density keeps the sizes of the base style.
  QSize sizeFromContents(ContentsType type, const QStyleOption* option, const QSize& size, const QWidget* widget = nullptr) const override;

  /// Prepare widgets for the style:
  /// - buttons get hover events (WA_Hover);
  /// - in the Comfortable density, ctkCollapsibleButton widgets whose collapsedHeight is still
  ///   the CTK default (14) get a collapsedHeight of 2;
  /// - widgets with the "slicerRole" property get the font of their role (unless a font was set
  ///   explicitly): "sectionHeader" (SemiBold, uppercase), "secondaryText", "caption", "chip",
  ///   "kbd" and "toolTile" are smaller than the application font. "chip" and "kbd" labels get
  ///   a horizontal padding;
  /// - menus and tool tips get rounded corners (widget mask).
  ///
  /// Roles (set with widget->setProperty("slicerRole", role), then unpolish() and polish()) change
  /// only paint and size: "card", "primary", "secondary", "ghost", "segmented" (a frame whose
  /// child tool buttons are segments), "toolTile", "sectionHeader", "secondaryText", "caption",
  /// "chip" (its "chipLevel" property is one of neutral, busy, ok, warn, danger), "kbd", "topBar"
  /// and "compact" (tool button without padding).
  void polish(QWidget* widget) override;
  void unpolish(QWidget* widget) override;
  /// Follow the color scheme of the operating system while the "System" theme is selected
  /// (Qt 6.5 or later).
  void polish(QApplication* application) override;

  /// Return the color of a named style token of the dark or light theme.
  /// Supported names: canvas, card, control-hover, control-pressed, hairline, border,
  /// control-border (non-text outlines with a 3:1 contrast against the window),
  /// text2, text3, text-disabled, accent, accent-hover, accent-pressed, accent-fg,
  /// accent-subtle, success, warning, danger, view-bar and lesion.
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

  /// Set the palette of the platform theme, captured before the application palette is set.
  /// Its window color is used by systemTheme() when the platform does not report a color scheme
  /// (Qt older than 6.5).
  static void setPlatformPalette(const QPalette& palette);

  /// Return DarkTheme or LightTheme: the appearance of the operating system followed by SystemTheme.
  /// \sa resolveSystemTheme()
  static Theme systemTheme();

  /// Decide the theme followed by SystemTheme from what the platform reports, in this order:
  /// \a colorScheme (Qt::ColorScheme values: 0 unknown, 1 light, 2 dark), the Windows setting
  /// \a appsUseLightTheme (-1 unknown, 0 dark, 1 light), then the window lightness of the platform
  /// palette \a platformWindowLightness (-1 unknown, dark below 128). Light if nothing is known.
  /// Returns a Theme value (DarkTheme or LightTheme).
  Q_INVOKABLE int resolveSystemTheme(int colorScheme, int appsUseLightTheme, int platformWindowLightness) const;

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
