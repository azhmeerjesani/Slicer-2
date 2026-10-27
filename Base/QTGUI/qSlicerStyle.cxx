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
#include <QAbstractButton>
#include <QAbstractScrollArea>
#include <QAbstractSpinBox>
#include <QApplication>
#include <QBitmap>
#include <QCheckBox>
#include <QComboBox>
#include <QDateTime>
#include <QStyleFactory>
#include <QDebug>
#include <QEvent>
#include <QFontInfo>
#include <QGroupBox>
#include <QLabel>
#include <QMenu>
#include <QMouseEvent>
#include <QPainter>
#include <QPainterPath>
#include <QPointer>
#include <qdrawutil.h>
#include <QScrollBar>
#include <QSettings>
#include <QSlider>
#include <QStyleHints>
#include <QStyleOption>
#include <QStyleOptionComboBox>
#include <QStyleOptionGroupBox>
#include <QStyleOptionSpinBox>
#include <QTabBar>
#include <QToolButton>
#include <QtMath>

// STD includes
#include <climits>
#include <cmath>

// Slicer includes
#include "qSlicerApplication.h"
#include "qSlicerCommandOptions.h"

// qMRML includes
#include "qSlicerStyle.h"

namespace
{
// Settings shared by all Slicer styles (the style object is replaced when the style is changed).
qSlicerStyle::Density StyleDensity = qSlicerStyle::Compact;
qSlicerStyle::Theme StyleTheme = qSlicerStyle::SystemTheme;
QColor StyleAccentColor;
// Window lightness of the platform palette (-1 if unknown), see qSlicerStyle::setPlatformPalette()
int PlatformWindowLightness = -1;
// CTK default of ctkCollapsibleButton::collapsedHeight, and the value of the Comfortable density
const int CTKDefaultCollapsedHeight = 14;
const int ComfortableCollapsedHeight = 2;

struct StyleToken
{
  const char* Name;
  const char* Dark;
  // nullptr if the token is derived from the accent color
  const char* Light;
};

// Graphite color tokens of the dark and light themes.
// Tokens that are not palette roles, used by the style and by widgets that need them
// (e.g. Python scripted modules through qSlicerStyle::token()).
const StyleToken StyleTokens[] = {
  { "canvas", "#0f0f11", "#f1f1f4" },          // Library page, field backgrounds, segmented tracks
  { "card", "#1d1d20", "#f6f6f8" },            // inspector cards, secondary buttons
  { "card-low", "#18181b", "#ffffff" },        // cards on the canvas (Library project and study cards)
  { "selected", "#2a2a2f", "#ffffff" },        // selected segment, list row and palette row
  { "control-hover", "#26262a", "#eeeef2" },   //
  { "control-pressed", "#202024", "#e4e4e9" }, //
  { "hairline", "#26262a", "#e4e4e8" },        // separators, card borders
  { "border", "#34343a", "#d4d4da" },          // button and dialog borders
  { "control-border", "#6b6b74", "#8a8a94" },  // non-text outlines (fields, check boxes): 3:1 on the window
  { "text2", "#a1a1aa", "#52525b" },           //
  { "text3", "#8b8b94", "#6b6b74" },           // informative secondary text: 4.5:1 on the window and on cards
  { "text-disabled", "#71717a", "#8e8e98" },   // disabled glyphs
  { "accent", "#2f6fe0", "#2f6fe0" },          // primary buttons (white text 4.7:1), progress, selected tiles
  { "accent-hover", nullptr, nullptr },        // derived from the accent color (+8% lightness)
  { "accent-pressed", nullptr, nullptr },      // derived from the accent color (-8% lightness)
  { "accent-fg", "#7aa7ff", "#2158bd" },       // icons, slider fill, focus ring, links
  { "accent-subtle", "#22324f", "#e3ecfc" },   // checked tiles, icon tiles
  { "success", "#3fb950", "#1a7f37" },         //
  { "warning", "#e3b341", "#9a6700" },         //
  { "danger", "#f85149", "#cf222e" },          //
  { "view-bar", "#161618", "#ffffff" },        // view bars and the top bar (the window color)
  { "lesion", "#ff5a5f", "#ff5a5f" },          //
};

//------------------------------------------------------------------------------
bool isTestingEnabled()
{
  return qSlicerCoreApplication::testAttribute(qSlicerCoreApplication::AA_EnableTesting);
}

//------------------------------------------------------------------------------
/// Return true if the historical appearance must be kept (light palette, default accent color):
/// when testing is enabled or when the application runs without main window.
bool useClassicAppearance()
{
  if (isTestingEnabled())
  {
    return true;
  }
  qSlicerApplication* app = qSlicerApplication::application();
  qSlicerCommandOptions* options = app ? app->commandOptions() : nullptr;
  return options && options->noMainWindow();
}

//------------------------------------------------------------------------------
/// Accent color override in effect (invalid if the default accent colors must be used)
QColor effectiveAccentColor()
{
  if (!StyleAccentColor.isValid() || useClassicAppearance())
  {
    return QColor();
  }
  return StyleAccentColor;
}

//------------------------------------------------------------------------------
/// Relative luminance of a color, as defined by WCAG 2
qreal relativeLuminance(const QColor& color)
{
  auto channel = [](qreal value) { return value <= 0.03928 ? value / 12.92 : qPow((value + 0.055) / 1.055, 2.4); };
  return 0.2126 * channel(color.redF()) + 0.7152 * channel(color.greenF()) + 0.0722 * channel(color.blueF());
}

//------------------------------------------------------------------------------
/// Contrast ratio between two colors, as defined by WCAG 2 (from 1 to 21)
qreal contrastRatio(const QColor& color1, const QColor& color2)
{
  const qreal luminance1 = relativeLuminance(color1);
  const qreal luminance2 = relativeLuminance(color2);
  return (qMax(luminance1, luminance2) + 0.05) / (qMin(luminance1, luminance2) + 0.05);
}

//------------------------------------------------------------------------------
/// Return \a color with its HSL lightness changed by \a delta (in the [-1, 1] range).
QColor shiftLightness(const QColor& color, qreal delta)
{
  QColor hsl = color.toHsl();
  qreal lightness = qBound<qreal>(0., hsl.lightnessF() + delta, 1.);
  return QColor::fromHslF(hsl.hslHueF(), hsl.hslSaturationF(), lightness, hsl.alphaF()).toRgb();
}

//------------------------------------------------------------------------------
/// Return \a color made lighter (on a dark background) or darker (on a light background)
/// until its contrast ratio with \a background is at least \a minimumContrast.
QColor readableColor(const QColor& color, const QColor& background, qreal minimumContrast)
{
  const qreal delta = background.lightness() < 128 ? 0.02 : -0.02;
  QColor readable = color;
  // At most 50 steps: lightness reaches 0 or 1, which gives the maximum contrast
  for (int step = 0; step < 50 && contrastRatio(readable, background) < minimumContrast; ++step)
  {
    readable = shiftLightness(readable, delta);
  }
  return readable;
}

//------------------------------------------------------------------------------
/// Return the color between \a from (ratio = 0) and \a to (ratio = 1).
QColor mixColors(const QColor& from, const QColor& to, qreal ratio)
{
  return QColor::fromRgbF(from.redF() + (to.redF() - from.redF()) * ratio, //
                          from.greenF() + (to.greenF() - from.greenF()) * ratio,
                          from.blueF() + (to.blueF() - from.blueF()) * ratio);
}

//------------------------------------------------------------------------------
/// Return the accent tokens derived from an accent color
QColor derivedAccentToken(const QString& name, const QColor& accent, bool dark)
{
  if (name == QLatin1String("accent"))
  {
    return accent;
  }
  if (name == QLatin1String("accent-hover"))
  {
    return shiftLightness(accent, 0.08);
  }
  if (name == QLatin1String("accent-pressed"))
  {
    return shiftLightness(accent, -0.08);
  }
  if (name == QLatin1String("accent-fg"))
  {
    // Lighter accent on dark windows, and readable (WCAG AA contrast for text) on the window color
    QColor hsl = accent.toHsl();
    qreal lightness = dark ? qMax<qreal>(hsl.lightnessF(), 0.70) : qMin<qreal>(hsl.lightnessF(), 0.53);
    QColor foreground = QColor::fromHslF(hsl.hslHueF(), hsl.hslSaturationF(), lightness).toRgb();
    return readableColor(foreground, QColor(dark ? "#161618" : "#ffffff"), 4.5);
  }
  if (name == QLatin1String("accent-subtle"))
  {
    return dark ? mixColors(QColor("#161618"), accent, 0.26) : mixColors(Qt::white, accent, 0.13);
  }
  return QColor();
}

//------------------------------------------------------------------------------
/// Apply the accent color override to the Highlight and Link roles
void applyAccentColor(QPalette& palette, bool dark)
{
  QColor accent = effectiveAccentColor();
  if (!accent.isValid())
  {
    return;
  }
  // White or dark text, whichever is more readable on the accent color
  const QColor lightText(Qt::white);
  const QColor darkText("#1c1c1f");
  const QColor highlightedText = contrastRatio(accent, lightText) >= contrastRatio(accent, darkText) ? lightText : darkText;
  for (QPalette::ColorGroup group : { QPalette::Active, QPalette::Inactive })
  {
    palette.setColor(group, QPalette::Highlight, accent);
    palette.setColor(group, QPalette::HighlightedText, highlightedText);
  }
  palette.setColor(QPalette::Link, qSlicerStyle::tokenColor("accent-fg", dark));
  if (!dark)
  {
    palette.setColor(QPalette::LinkVisited, qSlicerStyle::tokenColor("accent-fg", dark));
  }
}

//------------------------------------------------------------------------------
/// Return true if the window brush of \a palette is transparent (no brush or an alpha 0 color). A stylesheet such as
/// "background: transparent" gives a widget such a brush, which says nothing about the colors behind the widget.
bool hasTransparentWindow(const QPalette& palette)
{
  const QBrush& window = palette.brush(QPalette::Window);
  if (window.style() == Qt::NoBrush)
  {
    return true;
  }
  return window.style() == Qt::SolidPattern && window.color().alpha() == 0;
}

//------------------------------------------------------------------------------
/// Return true if the palette is dark (window color lightness < 128).
/// The color of a gradient brush is black, so for a gradient window brush the lightness is the one
/// that covers most of the widget: the mean lightness of the stops when the gradient is stretched
/// over the widget (e.g. the classic view bars), otherwise the lightness of the last stop, which pads
/// the rest of the widget (e.g. the colored stripe of the flat view bars).
/// A transparent window brush (e.g. from the stylesheet "background: transparent" of a parent panel) is
/// black with lightness 0, which would read as dark in the light theme: it is skipped, and the palette
/// of the closest ancestor of \a widget with an opaque window brush decides, or the application palette
/// when there is none.
bool isDark(const QPalette& palette, const QWidget* widget = nullptr)
{
  if (hasTransparentWindow(palette))
  {
    for (const QWidget* ancestor = widget ? widget->parentWidget() : nullptr; ancestor; ancestor = ancestor->parentWidget())
    {
      if (!hasTransparentWindow(ancestor->palette()))
      {
        return isDark(ancestor->palette());
      }
    }
    return isDark(QApplication::palette());
  }
  const QBrush& window = palette.brush(QPalette::Window);
  const QGradient* gradient = window.gradient();
  if (!gradient || gradient->stops().isEmpty())
  {
    return window.color().lightness() < 128;
  }
  const QGradientStops stops = gradient->stops();
  if (gradient->coordinateMode() == QGradient::LogicalMode || stops.size() == 1)
  {
    return stops.last().second.lightness() < 128;
  }
  // Mean lightness of the piecewise linear gradient between its first and last stops
  qreal integral = 0.;
  for (int index = 1; index < stops.size(); ++index)
  {
    const qreal width = stops[index].first - stops[index - 1].first;
    integral += width * (stops[index].second.lightness() + stops[index - 1].second.lightness()) / 2.;
  }
  const qreal span = stops.last().first - stops.first().first;
  const qreal lightness = span > 0. ? integral / span : stops.last().second.lightness();
  return lightness < 128.;
}

//------------------------------------------------------------------------------
/// Return true if the option is drawn with a dark palette (window color lightness < 128)
bool hasDarkPalette(const QStyleOption* option, const QWidget* widget = nullptr)
{
  return option && isDark(option->palette, widget);
}

//------------------------------------------------------------------------------
/// Return the color of a style token for the palette of \a option
QColor optionToken(const QStyleOption* option, const char* name)
{
  return qSlicerStyle::tokenColor(QLatin1String(name), hasDarkPalette(option));
}

//------------------------------------------------------------------------------
/// Return the "slicerRole" property of \a widget (empty if it has none)
QByteArray widgetRole(const QWidget* widget)
{
  if (!widget)
  {
    return QByteArray();
  }
  const QVariant role = widget->property("slicerRole");
  return role.isValid() ? role.toByteArray() : QByteArray();
}

//------------------------------------------------------------------------------
/// Return true if \a widget is a button of a frame with the "segmented" role
bool isSegment(const QWidget* widget)
{
  return widget && widget->parentWidget() && qobject_cast<const QAbstractButton*>(widget) //
         && widgetRole(widget->parentWidget()) == "segmented";
}

//------------------------------------------------------------------------------
/// Return the widget painted by \a painter, if any
QWidget* paintedWidget(const QPainter* painter)
{
  QPaintDevice* device = painter ? painter->device() : nullptr;
  return (device && device->devType() == QInternal::Widget) ? static_cast<QWidget*>(device) : nullptr;
}

//------------------------------------------------------------------------------
/// Return \a font scaled by \a ratio (pixel or point size, whichever the font uses)
QFont scaledFont(QFont font, qreal ratio)
{
  if (font.pixelSize() > 0)
  {
    font.setPixelSize(qMax(1, qRound(font.pixelSize() * ratio)));
  }
  else if (font.pointSizeF() > 0)
  {
    font.setPointSizeF(font.pointSizeF() * ratio);
  }
  return font;
}

//------------------------------------------------------------------------------
/// Draw a rounded rectangle aligned on pixel centers, filled with \a fill (if valid) and framed
/// with a \a borderWidth line of \a border (if valid). The rectangle stays inside \a rect.
void drawRoundedBox(QPainter* painter, const QRectF& rect, qreal radius, const QColor& fill, const QColor& border, qreal borderWidth = 1.)
{
  painter->save();
  painter->setRenderHint(QPainter::Antialiasing, true);
  if (border.isValid() && border.alpha() > 0 && borderWidth > 0.)
  {
    const qreal inset = borderWidth / 2.;
    painter->setPen(QPen(border, borderWidth));
    painter->setBrush(fill.isValid() ? QBrush(fill) : QBrush(Qt::NoBrush));
    painter->drawRoundedRect(rect.adjusted(inset, inset, -inset, -inset), qMax<qreal>(0., radius - inset), qMax<qreal>(0., radius - inset));
  }
  else if (fill.isValid() && fill.alpha() > 0)
  {
    painter->setPen(Qt::NoPen);
    painter->setBrush(fill);
    painter->drawRoundedRect(rect, radius, radius);
  }
  painter->restore();
}

//------------------------------------------------------------------------------
/// Kinds of button panels, from the widget and its slicerRole
enum ButtonKind
{
  CommandButton, // push button (and "secondary" role)
  ToolButton,    // tool button without auto-raise
  AutoRaiseButton,
  CompactButton,
  PrimaryButton,
  GhostButton,
  ToolTileButton,
  SegmentButton
};

//------------------------------------------------------------------------------
ButtonKind buttonKind(const QStyleOption* option, const QWidget* widget, bool toolPanel)
{
  const QByteArray role = widgetRole(widget);
  if (role == "primary")
  {
    return PrimaryButton;
  }
  if (role == "ghost")
  {
    return GhostButton;
  }
  if (role == "toolTile")
  {
    return ToolTileButton;
  }
  if (role == "compact")
  {
    return CompactButton;
  }
  if (role == "secondary")
  {
    return CommandButton;
  }
  if (isSegment(widget))
  {
    return SegmentButton;
  }
  if (toolPanel || qstyleoption_cast<const QStyleOptionToolButton*>(option))
  {
    return (option->state & QStyle::State_AutoRaise) ? AutoRaiseButton : ToolButton;
  }
  return CommandButton;
}

//------------------------------------------------------------------------------
/// Draw a 2 px keyboard focus ring inside \a rect
void drawFocusRing(QPainter* painter, const QRectF& rect, qreal radius, const QColor& color)
{
  drawRoundedBox(painter, rect, radius, QColor(), color, 2.);
}

//------------------------------------------------------------------------------
/// Draw the panel of a push button or tool button: flat, rounded, with hover and pressed fills.
/// Checked buttons have the "accent-subtle" fill and a 1 px "accent-fg" border.
/// Command (push) buttons also draw the keyboard focus ring; tool buttons draw it as
/// PE_FrameFocusRect, because auto-raise tool buttons have no panel when they are not hovered.
void drawButtonPanel(const QStyleOption* option, QPainter* painter, const QWidget* widget, bool toolPanel)
{
  const QStyle::State state = option->state;
  const bool enabled = state & QStyle::State_Enabled;
  const bool hover = enabled && (state & QStyle::State_MouseOver);
  const bool pressed = enabled && (state & QStyle::State_Sunken);
  const bool checked = state & QStyle::State_On;
  const bool focus = !toolPanel && (state & QStyle::State_HasFocus) && (state & QStyle::State_KeyboardFocusChange);
  const bool dark = hasDarkPalette(option);
  const ButtonKind kind = buttonKind(option, widget, toolPanel);
  auto token = [dark](const char* name) { return qSlicerStyle::tokenColor(QLatin1String(name), dark); };

  QColor fill;
  QColor border;
  // Radii of the visual design: controls 7, tool tiles 9 (see qSlicerStyle.h)
  qreal radius = 7.;
  switch (kind)
  {
    case PrimaryButton: fill = !enabled ? token("border") : pressed ? token("accent-pressed") : hover ? token("accent-hover") : token("accent"); break;
    case GhostButton:
    case AutoRaiseButton:
    case CompactButton:
    case SegmentButton:
      // Transparent until hovered
      fill = pressed ? token("control-pressed") : hover ? token("control-hover") : QColor(Qt::transparent);
      radius = (kind == CompactButton) ? 5. : 6.;
      break;
    case ToolTileButton:
      // Flat tiles: the card fill, a hairline border
      fill = !enabled ? token("card") : pressed ? token("control-pressed") : hover ? token("control-hover") : token("card");
      border = token("hairline");
      radius = 9.;
      break;
    case ToolButton:
      fill = !enabled  ? option->palette.color(QPalette::Disabled, QPalette::Button)
             : pressed ? token("control-pressed")
             : hover   ? token("control-hover")
                       : option->palette.color(QPalette::Button);
      border = hover ? token("control-border") : token("border");
      break;
    case CommandButton:
    default:
      fill = !enabled  ? option->palette.color(QPalette::Disabled, QPalette::Button)
             : pressed ? token("control-pressed")
             : hover   ? token("control-hover")
                       : option->palette.color(QPalette::Button);
      border = enabled ? token("control-border") : token("border");
      // Mark the default button of dialogs (the target of the Enter key), like the base style does
      if (enabled && !checked)
      {
        const QStyleOptionButton* buttonOption = qstyleoption_cast<const QStyleOptionButton*>(option);
        if (buttonOption && (buttonOption->features & QStyleOptionButton::DefaultButton))
        {
          border = token("accent-fg");
        }
      }
      break;
  }
  if (checked && kind == SegmentButton)
  {
    // The selected segment is a raised "selected" fill on its track (a hairline stands for the shadow of the light theme)
    fill = token("selected");
    border = dark ? QColor() : QColor(0, 0, 0, 26);
  }
  else if (checked && kind != PrimaryButton)
  {
    fill = token("accent-subtle");
    if (enabled && (hover || pressed))
    {
      fill = shiftLightness(fill, (dark ? 0.04 : -0.03) * (pressed ? -1. : 1.));
    }
    // Selected tool tiles have an accent border, other checked buttons an accent-fg border
    border = !enabled ? token("border") : (kind == ToolTileButton) ? token("accent") : token("accent-fg");
  }
  const QRectF rect(option->rect);
  radius = qMin(radius, rect.height() / 2.);
  drawRoundedBox(painter, rect, radius, fill, border);
  if (focus)
  {
    // On the accent fill of primary buttons, the ring uses the text color of the button
    const QColor ringColor = (kind == PrimaryButton) ? option->palette.color(QPalette::HighlightedText) : token("accent-fg");
    // Inside the border, so that both pixels of the ring change by more than 3:1 when the focus comes
    const qreal inset = (kind == PrimaryButton) ? 2. : 1.;
    drawFocusRing(painter, rect.adjusted(inset, inset, -inset, -inset), qMax<qreal>(0., radius - inset), ringColor);
  }
}

//------------------------------------------------------------------------------
/// Radius of the frame of input fields (line edits, spin boxes and combo boxes)
const qreal FieldRadius = 7.;

//------------------------------------------------------------------------------
/// Draw an input field (line edit, spin box, combo box): a 1 px "control-border" frame
/// ("accent-fg" when focused or open, "text2" when \a hoverable and hovered, "border" when
/// disabled), which has a contrast of at least 3:1 with the window color, rounded by 7 px.
/// The field is filled with \a fill if it is valid.
/// The base style (Fusion) frames fields with a color derived from the window color, which is
/// almost invisible on dark palettes.
void drawField(const QRect& rect, QStyle::State state, bool dark, const QColor& fill, bool hoverable, QPainter* painter)
{
  QColor frameColor = qSlicerStyle::tokenColor("control-border", dark);
  if (!(state & QStyle::State_Enabled))
  {
    frameColor = qSlicerStyle::tokenColor("border", dark);
  }
  else if (state & (QStyle::State_HasFocus | QStyle::State_On))
  {
    frameColor = qSlicerStyle::tokenColor("accent-fg", dark);
  }
  else if (hoverable && (state & QStyle::State_MouseOver))
  {
    frameColor = qSlicerStyle::tokenColor("text2", dark);
  }
  const QRectF fieldRect(rect);
  drawRoundedBox(painter, fieldRect, qMin(FieldRadius, fieldRect.height() / 2.), fill, frameColor);
}

//------------------------------------------------------------------------------
/// Draw the frame of an input field without fill (see drawField())
void drawFieldFrame(const QRect& rect, QStyle::State state, bool dark, QPainter* painter)
{
  drawField(rect, state, dark, QColor(), false, painter);
}

//------------------------------------------------------------------------------
/// Base color of an input field drawn with \a option
QColor fieldFill(const QStyleOption* option)
{
  return option->palette.color((option->state & QStyle::State_Enabled) ? QPalette::Normal : QPalette::Disabled, QPalette::Base);
}

//------------------------------------------------------------------------------
/// Fill the area of the arrow button \a subControl of a spin box or combo box when it is hovered
/// or pressed. The fill stays inside the rounded frame of the field.
void drawArrowButtonFill(const QStyleOptionComplex* option, QStyle::SubControl subControl, const QRect& buttonRect, bool stepEnabled, QPainter* painter)
{
  const QStyle::State state = option->state;
  if (!(state & QStyle::State_Enabled) || !stepEnabled || !(option->activeSubControls & subControl))
  {
    return;
  }
  const bool pressed = state & QStyle::State_Sunken;
  if (!pressed && !(state & QStyle::State_MouseOver))
  {
    return;
  }
  const QRectF fieldRect = QRectF(option->rect).adjusted(1., 1., -1., -1.);
  const qreal radius = qMax<qreal>(0., qMin(FieldRadius, QRectF(option->rect).height() / 2.) - 1.);
  QPainterPath interior;
  interior.addRoundedRect(fieldRect, radius, radius);
  painter->save();
  painter->setRenderHint(QPainter::Antialiasing, true);
  painter->setClipPath(interior, Qt::IntersectClip);
  painter->fillRect(QRectF(buttonRect).intersected(fieldRect), optionToken(option, pressed ? "control-pressed" : "control-hover"));
  painter->restore();
}

//------------------------------------------------------------------------------
/// Draw a flat spin box: a field, and the arrows (PE_IndicatorArrowUp/Down or
/// PE_IndicatorSpinPlus/Minus) in the button rectangles of the base style, so that the arrows are
/// where the base style handles the mouse (e.g. ctkDoubleSpinBox).
void drawSpinBox(const QStyleOptionSpinBox* spinBox, QPainter* painter, const QWidget* widget, const QStyle* style)
{
  const bool dark = hasDarkPalette(spinBox);
  if (spinBox->subControls & QStyle::SC_SpinBoxFrame)
  {
    if (spinBox->frame)
    {
      drawField(spinBox->rect, spinBox->state, dark, fieldFill(spinBox), false, painter);
    }
    else
    {
      painter->fillRect(spinBox->rect, fieldFill(spinBox));
    }
  }
  if (spinBox->buttonSymbols == QAbstractSpinBox::NoButtons)
  {
    return;
  }
  const bool plusMinus = (spinBox->buttonSymbols == QAbstractSpinBox::PlusMinus);
  for (QStyle::SubControl subControl : { QStyle::SC_SpinBoxUp, QStyle::SC_SpinBoxDown })
  {
    if (!(spinBox->subControls & subControl))
    {
      continue;
    }
    const QRect buttonRect = style->subControlRect(QStyle::CC_SpinBox, spinBox, subControl, widget);
    if (!buttonRect.isValid())
    {
      continue;
    }
    const bool up = (subControl == QStyle::SC_SpinBoxUp);
    const bool stepEnabled = spinBox->stepEnabled & (up ? QAbstractSpinBox::StepUpEnabled : QAbstractSpinBox::StepDownEnabled);
    drawArrowButtonFill(spinBox, subControl, buttonRect, stepEnabled, painter);
    QStyleOption arrow(*spinBox);
    arrow.rect = buttonRect;
    arrow.state &= ~(QStyle::State_Selected | QStyle::State_Sunken | QStyle::State_MouseOver);
    if (!stepEnabled)
    {
      arrow.state &= ~QStyle::State_Enabled;
    }
    const QStyle::PrimitiveElement element = plusMinus ? (up ? QStyle::PE_IndicatorSpinPlus : QStyle::PE_IndicatorSpinMinus) //
                                                       : (up ? QStyle::PE_IndicatorArrowUp : QStyle::PE_IndicatorArrowDown);
    style->drawPrimitive(element, &arrow, painter, widget);
  }
}

//------------------------------------------------------------------------------
/// Draw a flat combo box: a field (only a hover fill without frame) and a chevron in the arrow
/// rectangle of the base style. The current text and icon are drawn by CE_ComboBoxLabel.
void drawComboBox(const QStyleOptionComboBox* comboBox, QPainter* painter, const QWidget* widget, const QStyle* style)
{
  const bool dark = hasDarkPalette(comboBox);
  const QStyle::State state = comboBox->state;
  const bool enabled = state & QStyle::State_Enabled;
  if (comboBox->subControls & QStyle::SC_ComboBoxFrame)
  {
    if (comboBox->frame)
    {
      drawField(comboBox->rect, state, dark, fieldFill(comboBox), !comboBox->editable, painter);
    }
    else if (comboBox->editable)
    {
      // The edit field of a frameless editable combo box (PE_PanelLineEdit is not drawn for it)
      painter->fillRect(comboBox->rect, fieldFill(comboBox));
    }
    else if (enabled && (state & (QStyle::State_MouseOver | QStyle::State_On)))
    {
      drawRoundedBox(painter, QRectF(comboBox->rect), qMin(FieldRadius, comboBox->rect.height() / 2.), optionToken(comboBox, "control-hover"), QColor());
    }
  }
  if (comboBox->subControls & QStyle::SC_ComboBoxArrow)
  {
    const QRect arrowRect = style->subControlRect(QStyle::CC_ComboBox, comboBox, QStyle::SC_ComboBoxArrow, widget);
    if (arrowRect.isValid())
    {
      if (comboBox->editable)
      {
        drawArrowButtonFill(comboBox, QStyle::SC_ComboBoxArrow, arrowRect, true, painter);
      }
      QStyleOption arrow(*comboBox);
      const int side = qMin(10, qMin(arrowRect.width(), arrowRect.height()));
      arrow.rect = QRect(0, 0, side, side);
      arrow.rect.moveCenter(arrowRect.center());
      arrow.state &= ~(QStyle::State_Selected | QStyle::State_Sunken | QStyle::State_MouseOver);
      style->drawPrimitive(QStyle::PE_IndicatorArrowDown, &arrow, painter, widget);
    }
  }
}

//------------------------------------------------------------------------------
/// Draw a flat slider: a 4 px "border" groove, filled with "accent-fg" from the minimum to the
/// handle, subtle tick marks and a round handle.
/// The groove is drawn on the rows of the range drawn by ctkRangeSlider (center - 2 to center + 1
/// of the groove rectangle), which draws its groove with a position out of the range: the groove
/// is then not filled.
/// Return true if \a widget is in the flat bar of a view (an ancestor has the "slicerFlatViewBar" property).
bool isInFlatViewBar(const QWidget* widget)
{
  for (int depth = 0; widget && depth < 4; ++depth, widget = widget->parentWidget())
  {
    if (widget->property("slicerFlatViewBar").toBool())
    {
      return true;
    }
  }
  return false;
}

//------------------------------------------------------------------------------
/// Draw the neutral slider of a flat view bar: a 2 px track (#3f3f46 on dark, "border" on light) filled with "text2"
/// up to a 10 px round thumb.
void drawViewBarSlider(const QStyleOptionSlider* slider, QPainter* painter, const QWidget* widget, const QStyle* style)
{
  const bool dark = hasDarkPalette(slider);
  auto token = [dark](const char* name) { return qSlicerStyle::tokenColor(QLatin1String(name), dark); };
  const bool enabled = slider->state & QStyle::State_Enabled;
  const bool horizontal = slider->orientation == Qt::Horizontal;
  const QRect groove = style->subControlRect(QStyle::CC_Slider, slider, QStyle::SC_SliderGroove, widget);
  const QRect handle = style->subControlRect(QStyle::CC_Slider, slider, QStyle::SC_SliderHandle, widget);
  if ((slider->subControls & QStyle::SC_SliderGroove) && groove.isValid())
  {
    const QPointF center = QRectF(groove).center();
    const QRectF band = horizontal ? QRectF(groove.left(), center.y() - 1., groove.width(), 2.) //
                                   : QRectF(center.x() - 1., groove.top(), 2., groove.height());
    drawRoundedBox(painter, band, 1., dark ? QColor("#3f3f46") : token("border"), QColor());
    if (handle.isValid() && slider->maximum > slider->minimum)
    {
      QRectF filled(band);
      const QPointF handleCenter = QRectF(handle).center();
      if (horizontal)
      {
        slider->upsideDown ? filled.setLeft(handleCenter.x()) : filled.setRight(handleCenter.x());
      }
      else
      {
        slider->upsideDown ? filled.setTop(handleCenter.y()) : filled.setBottom(handleCenter.y());
      }
      drawRoundedBox(painter, filled, 1., enabled ? token("text2") : token("text-disabled"), QColor());
    }
  }
  if ((slider->subControls & QStyle::SC_SliderHandle) && handle.isValid())
  {
    const bool active = enabled && (slider->activeSubControls & QStyle::SC_SliderHandle);
    const bool ring = active && (slider->state & (QStyle::State_Sunken | QStyle::State_MouseOver));
    const bool focus = enabled && (slider->state & QStyle::State_HasFocus) && (slider->state & QStyle::State_KeyboardFocusChange);
    const qreal side = qMin<qreal>(10., qMin(handle.width(), handle.height()));
    QRectF circle(0., 0., side, side);
    circle.moveCenter(QRectF(handle).center());
    const QColor fill = enabled ? (dark ? QColor("#ececef") : QColor("#ffffff")) : token("text-disabled");
    const QColor border = (ring || focus) ? token("accent-fg") : (dark ? QColor() : token("control-border"));
    drawRoundedBox(painter, circle, side / 2., fill, border, (ring || focus) ? 2. : 1.);
  }
}

//------------------------------------------------------------------------------
void drawSlider(const QStyleOptionSlider* slider, QPainter* painter, const QWidget* widget, const QStyle* style)
{
  if (isInFlatViewBar(widget))
  {
    drawViewBarSlider(slider, painter, widget, style);
    return;
  }
  const bool dark = hasDarkPalette(slider);
  auto token = [dark](const char* name) { return qSlicerStyle::tokenColor(QLatin1String(name), dark); };
  const QStyle::State state = slider->state;
  const bool enabled = state & QStyle::State_Enabled;
  const bool horizontal = slider->orientation == Qt::Horizontal;
  const QRect groove = style->subControlRect(QStyle::CC_Slider, slider, QStyle::SC_SliderGroove, widget);
  const QRect handle = style->subControlRect(QStyle::CC_Slider, slider, QStyle::SC_SliderHandle, widget);

  if ((slider->subControls & QStyle::SC_SliderGroove) && groove.isValid())
  {
    const QPoint center = groove.center();
    const QRectF band = horizontal ? QRectF(groove.left(), center.y() - 2, groove.width(), 4) //
                                   : QRectF(center.x() - 2, groove.top(), 4, groove.height());
    drawRoundedBox(painter, band, 2., enabled ? token("border") : token("hairline"), QColor());
    const bool inRange = slider->maximum > slider->minimum && slider->sliderPosition >= slider->minimum && slider->sliderPosition <= slider->maximum;
    if (inRange && handle.isValid())
    {
      QRectF filled(band);
      const QPointF handleCenter = QRectF(handle).center();
      // From the minimum: the left (top) end, or the right (bottom) end when upside down
      if (horizontal && slider->upsideDown)
      {
        filled.setLeft(handleCenter.x());
      }
      else if (horizontal)
      {
        filled.setRight(handleCenter.x());
      }
      else if (slider->upsideDown)
      {
        filled.setTop(handleCenter.y());
      }
      else
      {
        filled.setBottom(handleCenter.y());
      }
      drawRoundedBox(painter, filled, 2., enabled ? token("accent-fg") : token("text-disabled"), QColor());
    }
  }

  if ((slider->subControls & QStyle::SC_SliderTickmarks) && slider->tickPosition != QSlider::NoTicks && slider->maximum > slider->minimum)
  {
    const int length = style->pixelMetric(QStyle::PM_SliderLength, slider, widget);
    const QRect rect = slider->rect;
    const int available = (horizontal ? rect.width() : rect.height()) - length;
    int interval = slider->tickInterval;
    if (interval <= 0)
    {
      interval = slider->singleStep;
      if (QStyle::sliderPositionFromValue(slider->minimum, slider->maximum, interval, available) //
            - QStyle::sliderPositionFromValue(slider->minimum, slider->maximum, 0, available)
          < 3)
      {
        interval = slider->pageStep;
      }
    }
    // At most one tick every 2 pixels
    if (interval > 0 && (qint64(slider->maximum) - slider->minimum) / interval <= available / 2)
    {
      QColor tickColor = token("text3");
      tickColor.setAlphaF(enabled ? 0.6 : 0.3);
      painter->save();
      painter->setPen(tickColor);
      const int tickLength = 3;
      for (qint64 value = slider->minimum; value <= slider->maximum; value += interval)
      {
        const int position = QStyle::sliderPositionFromValue(slider->minimum, slider->maximum, int(value), available, slider->upsideDown) + length / 2;
        for (QSlider::TickPosition side : { QSlider::TicksAbove, QSlider::TicksBelow })
        {
          if (!(slider->tickPosition & side))
          {
            continue;
          }
          if (horizontal)
          {
            const int x = rect.left() + position;
            const int y = (side == QSlider::TicksAbove) ? rect.top() : rect.bottom() - tickLength + 1;
            painter->drawLine(x, y, x, y + tickLength - 1);
          }
          else
          {
            const int y = rect.top() + position;
            const int x = (side == QSlider::TicksAbove) ? rect.left() : rect.right() - tickLength + 1;
            painter->drawLine(x, y, x + tickLength - 1, y);
          }
        }
      }
      painter->restore();
    }
  }

  if ((slider->subControls & QStyle::SC_SliderHandle) && handle.isValid())
  {
    const bool active = enabled && (slider->activeSubControls & QStyle::SC_SliderHandle);
    const bool pressed = active && (state & QStyle::State_Sunken);
    const bool hover = active && (state & QStyle::State_MouseOver);
    const bool focus = enabled && (state & QStyle::State_HasFocus) && (state & QStyle::State_KeyboardFocusChange);
    // Round handle, inside the handle rectangle (ctkRangeSlider clips each handle to it)
    const qreal side = qMin(handle.width(), handle.height());
    QRectF circle(0., 0., side, side);
    circle.moveCenter(QRectF(handle).center());
    QColor fill = slider->palette.color(enabled ? QPalette::Normal : QPalette::Disabled, QPalette::Button);
    if (pressed)
    {
      fill = token("control-pressed");
    }
    const bool ring = pressed || hover || focus;
    const QColor border = !enabled ? token("border") : ring ? token("accent-fg") : token("control-border");
    drawRoundedBox(painter, circle, side / 2., fill, border, ring ? 2. : 1.);
  }
}

//------------------------------------------------------------------------------
/// Return the rectangle of \a subControl of a scroll bar without step buttons: the groove is the
/// whole scroll bar, the handle length is computed like in the common style (QCommonStyle).
QRect thinScrollBarRect(const QStyleOptionSlider* scrollBar, QStyle::SubControl subControl, const QWidget* widget, const QStyle* style)
{
  const QRect rect = scrollBar->rect;
  const bool horizontal = scrollBar->orientation == Qt::Horizontal;
  const int length = horizontal ? rect.width() : rect.height();
  const int thickness = horizontal ? rect.height() : rect.width();
  int sliderLength = length;
  if (scrollBar->maximum != scrollBar->minimum)
  {
    const qint64 range = qint64(scrollBar->maximum) - scrollBar->minimum;
    sliderLength = int((qint64(scrollBar->pageStep) * length) / (range + scrollBar->pageStep));
    const int sliderMinimum = style->pixelMetric(QStyle::PM_ScrollBarSliderMin, scrollBar, widget);
    if (sliderLength < sliderMinimum || range > INT_MAX / 2)
    {
      sliderLength = sliderMinimum;
    }
    sliderLength = qMax(0, qMin(sliderLength, length));
  }
  const int sliderStart = QStyle::sliderPositionFromValue(scrollBar->minimum, scrollBar->maximum, scrollBar->sliderPosition, length - sliderLength, scrollBar->upsideDown);
  const int sliderEnd = sliderStart + sliderLength;
  QRect result;
  switch (subControl)
  {
    case QStyle::SC_ScrollBarSubPage: result = horizontal ? QRect(0, 0, sliderStart, thickness) : QRect(0, 0, thickness, sliderStart); break;
    case QStyle::SC_ScrollBarAddPage: result = horizontal ? QRect(sliderEnd, 0, length - sliderEnd, thickness) : QRect(0, sliderEnd, thickness, length - sliderEnd); break;
    case QStyle::SC_ScrollBarSlider: result = horizontal ? QRect(sliderStart, 0, sliderLength, thickness) : QRect(0, sliderStart, thickness, sliderLength); break;
    case QStyle::SC_ScrollBarGroove: result = QRect(0, 0, rect.width(), rect.height()); break;
    default:
      // No step buttons (SC_ScrollBarAddLine, SC_ScrollBarSubLine)
      return QRect();
  }
  return QStyle::visualRect(scrollBar->direction, rect, result.translated(rect.topLeft()));
}

//------------------------------------------------------------------------------
/// Draw a thin scroll bar: a transparent track and a rounded "text3" handle, which gets wider
/// and more opaque when hovered or pressed.
void drawThinScrollBar(const QStyleOptionSlider* scrollBar, QPainter* painter, const QWidget* widget, const QStyle* style)
{
  if (!(scrollBar->subControls & QStyle::SC_ScrollBarSlider) || scrollBar->maximum <= scrollBar->minimum)
  {
    // No handle when there is nothing to scroll
    return;
  }
  const QRect slider = style->subControlRect(QStyle::CC_ScrollBar, scrollBar, QStyle::SC_ScrollBarSlider, widget);
  if (slider.isEmpty())
  {
    return;
  }
  const QStyle::State state = scrollBar->state;
  const bool enabled = state & QStyle::State_Enabled;
  const bool active = enabled && (scrollBar->activeSubControls & QStyle::SC_ScrollBarSlider);
  const bool pressed = active && (state & QStyle::State_Sunken);
  const bool hover = active && (state & QStyle::State_MouseOver);
  const bool horizontal = scrollBar->orientation == Qt::Horizontal;
  const qreal inset = (pressed || hover) ? 0. : 1.;
  const QRectF handle = horizontal ? QRectF(slider).adjusted(1., inset, -1., -inset) : QRectF(slider).adjusted(inset, 1., -inset, -1.);
  QColor color = optionToken(scrollBar, enabled ? "text3" : "text-disabled");
  color.setAlphaF(!enabled ? 0.3 : pressed ? 0.85 : hover ? 0.7 : 0.45);
  const qreal thickness = horizontal ? handle.height() : handle.width();
  drawRoundedBox(painter, handle, qMin<qreal>(5., thickness / 2.), color, QColor());
}

//------------------------------------------------------------------------------
/// Draw a flat header section (or the empty area of a header): the window color, hover and
/// pressed fills, a "border" line on the side of the items and hairlines between sections.
void drawHeaderSection(const QStyleOption* option, QPainter* painter, bool emptyArea)
{
  const QStyle::State state = option->state;
  const bool enabled = state & QStyle::State_Enabled;
  bool horizontal = state & QStyle::State_Horizontal;
  bool lastSection = false;
  QColor fill = option->palette.color(QPalette::Window);
  if (const QStyleOptionHeader* header = qstyleoption_cast<const QStyleOptionHeader*>(option))
  {
    horizontal = header->orientation == Qt::Horizontal;
    lastSection = (header->position == QStyleOptionHeader::End || header->position == QStyleOptionHeader::OnlyOneSection);
    if (enabled && (state & QStyle::State_Sunken))
    {
      fill = optionToken(option, "control-pressed");
    }
    else if (enabled && (state & QStyle::State_MouseOver))
    {
      fill = optionToken(option, "control-hover");
    }
  }
  const QRect rect = option->rect;
  painter->save();
  painter->fillRect(rect, fill);
  painter->setPen(optionToken(option, "border"));
  if (horizontal)
  {
    painter->drawLine(rect.bottomLeft(), rect.bottomRight());
  }
  else
  {
    painter->drawLine(rect.topRight(), rect.bottomRight());
  }
  if (!emptyArea && !lastSection)
  {
    painter->setPen(optionToken(option, "hairline"));
    if (horizontal)
    {
      const int margin = qMin(4, rect.height() / 4);
      painter->drawLine(rect.right(), rect.top() + margin, rect.right(), rect.bottom() - margin);
    }
    else
    {
      painter->drawLine(rect.bottomLeft(), QPoint(rect.right() - 1, rect.bottom()));
    }
  }
  painter->restore();
}

//------------------------------------------------------------------------------
/// Draw the shape of a tab: no bevel, a hover fill, and a 2 px "accent-fg" line on the side of
/// the selected tab that faces its page.
void drawTabShape(const QStyleOptionTab* tab, QPainter* painter)
{
  const QStyle::State state = tab->state;
  const bool enabled = state & QStyle::State_Enabled;
  const bool selected = state & QStyle::State_Selected;
  const QRectF rect(tab->rect);
  if (!selected && enabled && (state & QStyle::State_MouseOver))
  {
    drawRoundedBox(painter, rect.adjusted(1., 2., -1., -2.), 4., optionToken(tab, "control-hover"), QColor());
  }
  if (!selected)
  {
    return;
  }
  const qreal lineWidth = 2.;
  QRectF line;
  switch (tab->shape)
  {
    case QTabBar::RoundedSouth:
    case QTabBar::TriangularSouth: line = QRectF(rect.left(), rect.top(), rect.width(), lineWidth); break;
    case QTabBar::RoundedWest:
    case QTabBar::TriangularWest: line = QRectF(rect.right() + 1. - lineWidth, rect.top(), lineWidth, rect.height()); break;
    case QTabBar::RoundedEast:
    case QTabBar::TriangularEast: line = QRectF(rect.left(), rect.top(), lineWidth, rect.height()); break;
    case QTabBar::RoundedNorth:
    case QTabBar::TriangularNorth:
    default: line = QRectF(rect.left(), rect.bottom() + 1. - lineWidth, rect.width(), lineWidth); break;
  }
  painter->fillRect(line, optionToken(tab, enabled ? "accent-fg" : "text-disabled"));
}

//------------------------------------------------------------------------------
/// Space between the bar and the text of a horizontal progress bar
constexpr int ProgressBarLabelSpacing = 6;
/// Length of the bar that a long text of a horizontal progress bar leaves at least
constexpr int ProgressBarMinimumLength = 40;

//------------------------------------------------------------------------------
/// Return the width reserved for the text of a horizontal progress bar drawn beside the bar:
/// the width of "100%" at least, so that the bar does not change when the value changes, or the
/// width of a longer text (e.g. a custom format), as long as it leaves ProgressBarMinimumLength
/// pixels to the bar (or half of the progress bar). A text that does not fit is elided by
/// CE_ProgressBarLabel. 0 if the text is not visible or the progress bar is busy.
int progressBarLabelWidth(const QStyleOptionProgressBar* progressBar)
{
  if (!progressBar->textVisible || (progressBar->minimum == 0 && progressBar->maximum == 0))
  {
    return 0;
  }
  const QFontMetrics& metrics = progressBar->fontMetrics;
  const int width = qMax(metrics.horizontalAdvance(progressBar->text), metrics.horizontalAdvance(QStringLiteral("100%")));
  const int totalWidth = progressBar->rect.width();
  const int maximumWidth = qMax(totalWidth / 2, totalWidth - ProgressBarMinimumLength - ProgressBarLabelSpacing);
  return qMax(0, qMin(width, maximumWidth));
}

//------------------------------------------------------------------------------
/// Return the 4 px pill of a progress bar drawn in \a rect (vertically centered)
QRectF progressBarPill(const QRect& rect)
{
  const qreal thickness = qMin<qreal>(4., rect.height());
  return QRectF(rect.left(), rect.top() + qRound((rect.height() - thickness) / 2.), rect.width(), thickness);
}

//------------------------------------------------------------------------------
/// Return true if \a option is a horizontal progress bar, drawn as a pill (vertical progress bars
/// are drawn by the base style)
bool isPillProgressBar(const QStyleOption* option)
{
  return qstyleoption_cast<const QStyleOptionProgressBar*>(option) && (option->state & QStyle::State_Horizontal);
}

//------------------------------------------------------------------------------
/// Draw a splitter handle (or a dock widget separator): a hairline along the handle, a 2 px
/// "accent-fg" line when it is hovered or dragged.
void drawSplitterHandle(const QStyleOption* option, QPainter* painter)
{
  const QStyle::State state = option->state;
  const bool active = (state & QStyle::State_Enabled) && (state & (QStyle::State_MouseOver | QStyle::State_Sunken));
  const qreal lineWidth = active ? 2. : 1.;
  const QRectF rect(option->rect);
  // A horizontal splitter has widgets side by side: its handle is a vertical line
  QRectF line;
  if (state & QStyle::State_Horizontal)
  {
    line = QRectF(qFloor(rect.center().x() - lineWidth / 2. + 0.5), rect.top(), lineWidth, rect.height());
  }
  else
  {
    line = QRectF(rect.left(), qFloor(rect.center().y() - lineWidth / 2. + 0.5), rect.width(), lineWidth);
  }
  painter->fillRect(line, optionToken(option, active ? "accent-fg" : "hairline"));
}

//------------------------------------------------------------------------------
/// Draw a check box (or item view check) or radio button indicator.
/// Unchecked indicators have a 1.5 px "control-border" outline (3:1 contrast with the window),
/// checked indicators are filled with the highlight (accent) color. The keyboard focus ring is
/// drawn around the label (PE_FrameFocusRect).
void drawIndicator(QStyle::PrimitiveElement element, const QStyleOption* option, QPainter* painter)
{
  const QStyle::State state = option->state;
  const bool dark = hasDarkPalette(option);
  const bool enabled = state & QStyle::State_Enabled;
  const bool radio = (element == QStyle::PE_IndicatorRadioButton);
  const bool partial = !radio && (state & QStyle::State_NoChange);
  const bool checked = partial || (state & QStyle::State_On);
  const bool hover = enabled && (state & QStyle::State_MouseOver);
  const bool sunken = enabled && (state & QStyle::State_Sunken);
  const QPalette::ColorGroup group = !enabled ? QPalette::Disabled : (state & QStyle::State_Active) ? QPalette::Active : QPalette::Inactive;

  QColor fillColor;
  QColor frameColor;
  qreal frameWidth = 1.5;
  if (checked)
  {
    fillColor = option->palette.color(group, QPalette::Highlight);
    if (hover)
    {
      fillColor = dark ? fillColor.lighter(112) : fillColor.darker(108);
    }
    else if (sunken)
    {
      fillColor = fillColor.darker(112);
    }
    // Keep the indicator distinct from the highlighted background of selected items
    frameColor = (state & QStyle::State_Selected) ? option->palette.color(group, QPalette::HighlightedText) : fillColor;
    frameWidth = 1.;
  }
  else
  {
    fillColor = sunken ? qSlicerStyle::tokenColor("control-pressed", dark) : option->palette.color(group, QPalette::Base);
    frameColor = !enabled ? qSlicerStyle::tokenColor("border", dark) : hover ? qSlicerStyle::tokenColor("text2", dark) : qSlicerStyle::tokenColor("control-border", dark);
  }
  const QColor markColor = option->palette.color(group, QPalette::HighlightedText);

  // Square indicator centered in the option rectangle
  const qreal side = qMin(option->rect.width(), option->rect.height());
  QRectF boxRect(0., 0., side, side);
  boxRect.moveCenter(QRectF(option->rect).center());

  if (radio)
  {
    painter->save();
    painter->setRenderHint(QPainter::Antialiasing, true);
    const qreal inset = frameWidth / 2.;
    painter->setPen(QPen(frameColor, frameWidth));
    painter->setBrush(fillColor);
    painter->drawEllipse(boxRect.adjusted(inset, inset, -inset, -inset));
    if (checked)
    {
      const qreal dotRadius = side / 5.;
      painter->setPen(Qt::NoPen);
      painter->setBrush(markColor);
      painter->drawEllipse(boxRect.center(), dotRadius, dotRadius);
    }
    painter->restore();
    return;
  }

  drawRoundedBox(painter, boxRect, qMin<qreal>(4., side / 4.), fillColor, frameColor, frameWidth);
  if (checked)
  {
    painter->save();
    painter->setRenderHint(QPainter::Antialiasing, true);
    const qreal penWidth = qBound<qreal>(1.5, side * 0.12, 2.5);
    painter->setPen(QPen(markColor, penWidth, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
    painter->setBrush(Qt::NoBrush);
    const qreal x = boxRect.left();
    const qreal y = boxRect.top();
    const qreal w = boxRect.width();
    if (partial)
    {
      painter->drawLine(QPointF(x + w * 0.28, y + w * 0.5), QPointF(x + w * 0.72, y + w * 0.5));
    }
    else
    {
      QPainterPath checkMark;
      checkMark.moveTo(x + w * 0.26, y + w * 0.52);
      checkMark.lineTo(x + w * 0.44, y + w * 0.69);
      checkMark.lineTo(x + w * 0.75, y + w * 0.32);
      painter->drawPath(checkMark);
    }
    painter->restore();
  }
}

//------------------------------------------------------------------------------
/// Size of the track of a check box with the "switch" slicerRole
const int SwitchWidth = 30;
const int SwitchHeight = 18;
/// Space between the text of a switch and its track
const int SwitchSpacing = 12;

//------------------------------------------------------------------------------
/// Return true if \a widget is a check box drawn as a switch (the "switch" slicerRole)
bool isSwitch(const QWidget* widget)
{
  return widget && qobject_cast<const QCheckBox*>(widget) && widgetRole(widget) == "switch";
}

//------------------------------------------------------------------------------
/// Draw the indicator of a check box with the "switch" slicerRole: a pill track ("accent" when
/// on, "control-border" when off, which has a 3:1 contrast with the window) and a round knob.
void drawSwitch(const QStyleOption* option, QPainter* painter)
{
  const QStyle::State state = option->state;
  const bool dark = hasDarkPalette(option);
  const bool enabled = state & QStyle::State_Enabled;
  const bool on = state & (QStyle::State_On | QStyle::State_NoChange);
  const bool hover = enabled && (state & QStyle::State_MouseOver);
  auto token = [dark](const char* name) { return qSlicerStyle::tokenColor(QLatin1String(name), dark); };
  QRectF track(0., 0., qMin<qreal>(SwitchWidth, option->rect.width()), qMin<qreal>(SwitchHeight, option->rect.height()));
  track.moveCenter(QRectF(option->rect).center());
  QColor fill = on ? token("accent") : token("control-border");
  if (hover)
  {
    fill = on ? token("accent-hover") : shiftLightness(fill, dark ? 0.06 : -0.06);
  }
  painter->save();
  if (!enabled)
  {
    painter->setOpacity(0.4);
  }
  drawRoundedBox(painter, track, track.height() / 2., fill, QColor());
  const qreal knobSide = track.height() - 4.;
  QRectF knob(0., 0., knobSide, knobSide);
  knob.moveCenter(QPointF(on ? track.right() - 2. - knobSide / 2. : track.left() + 2. + knobSide / 2., track.center().y()));
  drawRoundedBox(painter, knob, knobSide / 2., dark ? QColor("#ececef") : QColor(Qt::white), QColor());
  painter->restore();
}

//------------------------------------------------------------------------------
/// Draw a thin chevron (1.5 px line) pointing in the direction of \a element, centered in \a rect
void drawChevron(QStyle::PrimitiveElement element, const QRectF& rect, const QColor& color, QPainter* painter)
{
  const qreal side = qMin(rect.width(), rect.height());
  if (side < 3.)
  {
    return;
  }
  // Half width (across the direction) and half length (along the direction) of the chevron
  const qreal halfWidth = qBound<qreal>(2.5, side * 0.28, 4.5);
  const qreal halfLength = halfWidth / 2.;
  const QPointF c = rect.center();
  QPolygonF chevron;
  switch (element)
  {
    case QStyle::PE_IndicatorArrowUp:
      chevron << QPointF(c.x() - halfWidth, c.y() + halfLength) << QPointF(c.x(), c.y() - halfLength) << QPointF(c.x() + halfWidth, c.y() + halfLength);
      break;
    case QStyle::PE_IndicatorArrowLeft:
      chevron << QPointF(c.x() + halfLength, c.y() - halfWidth) << QPointF(c.x() - halfLength, c.y()) << QPointF(c.x() + halfLength, c.y() + halfWidth);
      break;
    case QStyle::PE_IndicatorArrowRight:
      chevron << QPointF(c.x() - halfLength, c.y() - halfWidth) << QPointF(c.x() + halfLength, c.y()) << QPointF(c.x() - halfLength, c.y() + halfWidth);
      break;
    case QStyle::PE_IndicatorArrowDown:
    default: chevron << QPointF(c.x() - halfWidth, c.y() - halfLength) << QPointF(c.x(), c.y() + halfLength) << QPointF(c.x() + halfWidth, c.y() - halfLength); break;
  }
  painter->save();
  painter->setRenderHint(QPainter::Antialiasing, true);
  painter->setPen(QPen(color, 1.5, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
  painter->setBrush(Qt::NoBrush);
  painter->drawPolyline(chevron);
  painter->restore();
}

//------------------------------------------------------------------------------
/// Color of the arrows (chevrons) drawn for \a option
QColor arrowColor(const QStyleOption* option)
{
  if (!(option->state & QStyle::State_Enabled))
  {
    return optionToken(option, "text-disabled");
  }
  if (option->state & QStyle::State_Selected)
  {
    // e.g. branch indicators of selected tree items
    return option->palette.color(QPalette::HighlightedText);
  }
  return optionToken(option, "text2");
}

//------------------------------------------------------------------------------
/// Draw the surface (PE_PanelMenu) or the frame (PE_FrameMenu) of a menu: a card with a 1 px
/// "control-border" border, so that a menu stands out from the panel or view below it.
/// Menus (QMenu) get rounded corners (see polish()); other popups (e.g. the list of a combo box) are square.
void drawMenuSurface(QStyle::PrimitiveElement element, const QStyleOption* option, QPainter* painter, const QWidget* widget)
{
  const bool dark = hasDarkPalette(option);
  const QColor borderColor = qSlicerStyle::tokenColor("control-border", dark);
  const QColor fill = (element == QStyle::PE_PanelMenu) ? qSlicerStyle::tokenColor("card", dark) : QColor();
  const qreal radius = qobject_cast<const QMenu*>(widget) ? 8. : 0.;
  if (radius > 0.)
  {
    if (fill.isValid())
    {
      // Fill the corners outside of the rounded border, in case the menu is not masked
      painter->fillRect(option->rect, fill);
    }
    drawRoundedBox(painter, QRectF(option->rect), radius, fill, borderColor);
    return;
  }
  painter->save();
  if (fill.isValid())
  {
    const QBrush background(fill);
    qDrawPlainRect(painter, option->rect, borderColor, 1, &background);
  }
  else
  {
    painter->setPen(borderColor);
    painter->setBrush(Qt::NoBrush);
    painter->drawRect(option->rect.adjusted(0, 0, -1, -1));
  }
  painter->restore();
}

//------------------------------------------------------------------------------
/// Draw an item of a popup menu: rounded "accent-subtle" selection, check marks (no empty boxes),
/// a framed accent icon for checked items with an icon and right-aligned shortcuts in "text3".
/// The geometry of the columns is the one of the base style (Fusion), so the item sizes fit.
void drawMenuItem(const QStyleOptionMenuItem* menuItem, QPainter* painter, const QWidget* widget, const QStyle* style)
{
  const bool dark = hasDarkPalette(menuItem);
  auto token = [dark](const char* name) { return qSlicerStyle::tokenColor(QLatin1String(name), dark); };
  const QRect rect = menuItem->rect;
  const bool enabled = menuItem->state & QStyle::State_Enabled;
  painter->save();

  if (menuItem->menuItemType == QStyleOptionMenuItem::Separator)
  {
    const int margin = 8;
    int textWidth = 0;
    if (!menuItem->text.isEmpty())
    {
      painter->setFont(menuItem->font);
      QPalette palette = menuItem->palette;
      palette.setColor(QPalette::Text, token("text3"));
      style->drawItemText(painter, rect.adjusted(margin, 0, -margin, 0), Qt::AlignLeft | Qt::AlignVCenter, palette, enabled, menuItem->text, QPalette::Text);
      textWidth = menuItem->fontMetrics.horizontalAdvance(menuItem->text) + margin;
    }
    const bool reverse = menuItem->direction == Qt::RightToLeft;
    painter->setPen(token("hairline"));
    const int y = rect.center().y();
    painter->drawLine(rect.left() + margin + (reverse ? 0 : textWidth), y, rect.right() - margin - (reverse ? textWidth : 0), y);
    painter->restore();
    return;
  }

  const bool selected = (menuItem->state & QStyle::State_Selected) && enabled;
  if (selected)
  {
    drawRoundedBox(painter, QRectF(rect).adjusted(4., 1., -4., -1.), 4., token("accent-subtle"), QColor());
  }

  // Columns of the base style (Fusion)
  const int itemFrame = 2;
  const int itemHMargin = 3;
  const int rightBorder = 15;
  const int checkColHOffset = itemHMargin + itemFrame - 1;
  int checkColumn = qMax<int>(rect.height() * 0.79, qMax<int>(menuItem->maxIconWidth, 21));
  const bool checkable = menuItem->checkType != QStyleOptionMenuItem::NotCheckable;
  const bool checked = menuItem->checked || (menuItem->state & QStyle::State_On);
  const bool ignoreCheckMark = qobject_cast<const QComboBox*>(widget) != nullptr;
  if (ignoreCheckMark)
  {
    checkColumn = menuItem->icon.isNull() ? 0 : menuItem->maxIconWidth;
  }
  const QRect checkRect = QStyle::visualRect(menuItem->direction, rect, QRect(rect.x() + checkColHOffset, rect.y(), checkColumn, rect.height()));
  const QColor textColor = menuItem->palette.color(enabled ? QPalette::Normal : QPalette::Disabled, QPalette::Text);

  if (!menuItem->icon.isNull())
  {
    const QIcon::Mode mode = !enabled ? QIcon::Disabled : selected ? QIcon::Active : QIcon::Normal;
    const int iconExtent = style->pixelMetric(QStyle::PM_SmallIconSize, menuItem, widget);
    const QSize iconSize(iconExtent, iconExtent);
    if (checkable && checked && !ignoreCheckMark)
    {
      // Checked item with an icon: the icon is framed like a checked tool button
      QRectF frame(0., 0., iconExtent + 6., iconExtent + 6.);
      frame.moveCenter(QRectF(checkRect).center());
      drawRoundedBox(painter, frame, 4., token("accent-subtle"), enabled ? token("accent-fg") : token("border"));
    }
    const QPixmap pixmap = menuItem->icon.pixmap(iconSize, mode, checked ? QIcon::On : QIcon::Off);
    QRect pixmapRect(QPoint(0, 0), iconSize);
    pixmapRect.moveCenter(checkRect.center());
    painter->drawPixmap(pixmapRect, pixmap);
  }
  else if (checkable && checked && !ignoreCheckMark)
  {
    // Check mark or dot, in the accent color
    const QColor markColor = enabled ? token("accent-fg") : token("text-disabled");
    const qreal side = qMin<qreal>(12., checkRect.height() - 6.);
    QRectF markRect(0., 0., side, side);
    markRect.moveCenter(QRectF(checkRect).center());
    painter->setRenderHint(QPainter::Antialiasing, true);
    if (menuItem->checkType & QStyleOptionMenuItem::Exclusive)
    {
      painter->setPen(Qt::NoPen);
      painter->setBrush(markColor);
      painter->drawEllipse(markRect.center(), side / 5., side / 5.);
    }
    else
    {
      painter->setPen(QPen(markColor, 1.75, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
      painter->setBrush(Qt::NoBrush);
      QPainterPath checkMark;
      checkMark.moveTo(markRect.left() + side * 0.12, markRect.top() + side * 0.55);
      checkMark.lineTo(markRect.left() + side * 0.40, markRect.top() + side * 0.82);
      checkMark.lineTo(markRect.left() + side * 0.88, markRect.top() + side * 0.22);
      painter->drawPath(checkMark);
    }
    painter->setRenderHint(QPainter::Antialiasing, false);
  }

  // Text and shortcut
  const int textLeft = checkColHOffset + checkColumn + itemHMargin;
  const QRect textRect(rect.x() + textLeft, rect.y(), rect.width() - textLeft - rightBorder, rect.height());
  QString text = menuItem->text;
  if (!text.isEmpty())
  {
    int textFlags = Qt::AlignVCenter | Qt::TextShowMnemonic | Qt::TextDontClip | Qt::TextSingleLine;
    if (!style->styleHint(QStyle::SH_UnderlineShortcut, menuItem, widget))
    {
      textFlags |= Qt::TextHideMnemonic;
    }
    const int tab = text.indexOf(QLatin1Char('\t'));
    if (tab >= 0)
    {
      const QString shortcut = text.mid(tab + 1);
      text = text.left(tab);
      painter->setFont(menuItem->font);
      painter->setPen(enabled ? token("text3") : textColor);
      painter->drawText(QStyle::visualRect(menuItem->direction, rect, textRect), textFlags | Qt::AlignRight, shortcut);
    }
    QFont font = menuItem->font;
    if (menuItem->menuItemType == QStyleOptionMenuItem::DefaultItem)
    {
      font.setBold(true);
    }
    painter->setFont(font);
    painter->setPen(textColor);
    painter->drawText(QStyle::visualRect(menuItem->direction, rect, textRect), textFlags | Qt::AlignLeft, text);
  }

  // Submenu arrow
  if (menuItem->menuItemType == QStyleOptionMenuItem::SubMenu)
  {
    const int dimension = (rect.height() - 4) / 2;
    const QRect arrowRect = QStyle::visualRect(menuItem->direction, rect, QRect(rect.right() - 6 - dimension, rect.center().y() - dimension / 2, dimension, dimension));
    const bool reverse = menuItem->direction == Qt::RightToLeft;
    drawChevron(reverse ? QStyle::PE_IndicatorArrowLeft : QStyle::PE_IndicatorArrowRight, QRectF(arrowRect), enabled ? token("text2") : token("text-disabled"), painter);
  }
  painter->restore();
}

//------------------------------------------------------------------------------
/// Height of the header of a ctkCollapsibleButton with the "card" slicerRole (a card title row, or a step row)
const int CardHeaderHeight = 44;
/// Side of the status circle of a step row
const int StepCircleSize = 20;

//------------------------------------------------------------------------------
/// Return true if \a widget is a ctkCollapsibleButton with the "card" slicerRole (the cards of the Map workspace)
bool isCardCollapsible(const QWidget* widget)
{
  return widget && widget->inherits("ctkCollapsibleButton") && widgetRole(widget) == "card";
}

//------------------------------------------------------------------------------
/// Draw the header of a card collapsible (see drawCollapsibleHeader()): an optional status circle ("studioStep" number
/// and "studioStepState": "done" check, "action" "!", "current" number in the accent color), the title in 14 px
/// SemiBold, an optional one-line summary in "text3" ("studioStepSummary") and a chevron on the right.
void drawCardHeader(QPainter* painter, const QRect& headerRect, const QPalette& palette, bool enabled, const QString& text, const QWidget* widget)
{
  const bool dark = isDark(palette, widget);
  auto token = [dark](const char* name) { return qSlicerStyle::tokenColor(QLatin1String(name), dark); };
  painter->save();
  painter->setRenderHint(QPainter::Antialiasing, true);
  const int padding = 14;
  int left = headerRect.left() + padding;
  const int step = widget->property("studioStep").toInt();
  const QString stepState = widget->property("studioStepState").toString();
  const QFont baseFont = widget->font();
  if (step > 0)
  {
    QRectF circle(0., 0., StepCircleSize, StepCircleSize);
    circle.moveCenter(QPointF(left + StepCircleSize / 2., headerRect.center().y() + 0.5));
    QColor fill;
    QColor mark;
    QString glyph = QString::number(step);
    if (stepState == QLatin1String("done"))
    {
      mark = token("success");
      fill = mark;
      fill.setAlphaF(0.16);
      glyph.clear();
    }
    else if (stepState == QLatin1String("action"))
    {
      mark = token("warning");
      fill = mark;
      fill.setAlphaF(0.16);
      glyph = QStringLiteral("!");
    }
    else if (stepState == QLatin1String("current"))
    {
      fill = token("accent");
      mark = Qt::white;
    }
    else
    {
      fill = token("control-hover");
      mark = token("text2");
    }
    drawRoundedBox(painter, circle, StepCircleSize / 2., fill, QColor());
    if (glyph.isEmpty())
    {
      // A check mark
      painter->setPen(QPen(mark, 1.75, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
      painter->setBrush(Qt::NoBrush);
      QPainterPath check;
      const qreal x = circle.left();
      const qreal y = circle.top();
      const qreal w = circle.width();
      check.moveTo(x + w * 0.30, y + w * 0.52);
      check.lineTo(x + w * 0.45, y + w * 0.66);
      check.lineTo(x + w * 0.71, y + w * 0.37);
      painter->drawPath(check);
    }
    else
    {
      QFont font = baseFont;
      font.setWeight(QFont::DemiBold);
      font.setPixelSize(qMax(8, qRound(QFontInfo(baseFont).pixelSize() * 0.85)));
      painter->setFont(font);
      painter->setPen(mark);
      painter->drawText(circle, Qt::AlignCenter, glyph);
    }
    left += StepCircleSize + 10;
  }
  // Chevron on the right: right when collapsed, down when expanded
  const qreal chevronSide = 12.;
  const QRectF chevronRect(headerRect.right() - padding - chevronSide + 1, headerRect.center().y() - chevronSide / 2. + 0.5, chevronSide, chevronSide);
  const bool collapsed = widget->property("collapsed").toBool();
  drawChevron(collapsed ? QStyle::PE_IndicatorArrowRight : QStyle::PE_IndicatorArrowDown, chevronRect, enabled ? token("text3") : token("text-disabled"), painter);
  const int right = int(chevronRect.left()) - 8;
  const QString summary = widget->property("studioStepSummary").toString();
  QFont titleFont = baseFont;
  titleFont.setWeight(QFont::DemiBold);
  titleFont.setPixelSize(qMax(8, qRound(QFontInfo(baseFont).pixelSize() * (step > 0 ? 1.0 : 14. / 13.))));
  const QFontMetrics titleMetrics(titleFont);
  QColor titleColor = palette.color(enabled ? QPalette::Normal : QPalette::Disabled, QPalette::WindowText);
  if (!summary.isEmpty())
  {
    QFont summaryFont = baseFont;
    summaryFont.setPixelSize(qMax(8, qRound(QFontInfo(baseFont).pixelSize() * 12. / 13.)));
    const QFontMetrics summaryMetrics(summaryFont);
    const int total = titleMetrics.height() + summaryMetrics.height();
    const int top = headerRect.top() + (headerRect.height() - total) / 2;
    painter->setFont(titleFont);
    painter->setPen(titleColor);
    painter->drawText(QRect(left, top, right - left, titleMetrics.height()), Qt::AlignLeft | Qt::AlignVCenter, titleMetrics.elidedText(text, Qt::ElideRight, right - left));
    painter->setFont(summaryFont);
    painter->setPen(enabled ? token("text3") : token("text-disabled"));
    painter->drawText(QRect(left, top + titleMetrics.height(), right - left, summaryMetrics.height()),
                      Qt::AlignLeft | Qt::AlignVCenter,
                      summaryMetrics.elidedText(summary, Qt::ElideRight, right - left));
  }
  else
  {
    painter->setFont(titleFont);
    painter->setPen(titleColor);
    painter->drawText(
      QRect(left, headerRect.top(), right - left, headerRect.height()), Qt::AlignLeft | Qt::AlignVCenter, titleMetrics.elidedText(text, Qt::ElideRight, right - left));
  }
  painter->restore();
}

//------------------------------------------------------------------------------
/// Draw the flat header of a ctkCollapsibleButton: a hover or pressed fill and a hairline below
/// the header when the section is expanded. The chevron and the text are drawn by the button.
/// With the "card" slicerRole the whole button is a rounded card ("card" fill, hairline, 10 px)
/// and the header a 44 px title row (see drawCardHeader()).
void drawCollapsibleHeader(const QStyleOption* option, QPainter* painter, const QWidget* widget)
{
  const QStyle::State state = option->state;
  const bool enabled = state & QStyle::State_Enabled;
  const QRectF rect(option->rect);
  QColor fill;
  if (enabled && (state & QStyle::State_Sunken))
  {
    fill = optionToken(option, "control-pressed");
  }
  else if (enabled && (state & QStyle::State_MouseOver))
  {
    fill = optionToken(option, "control-hover");
  }
  if (isCardCollapsible(widget))
  {
    const QRectF cardRect(widget->rect());
    drawRoundedBox(painter, cardRect, 10., optionToken(option, "card"), optionToken(option, "hairline"));
    if (fill.isValid())
    {
      // The hover fill of the title row, inside the card's border and rounded like its top corners
      QPainterPath header;
      header.addRoundedRect(cardRect.adjusted(1., 1., -1., -1.), 9., 9.);
      QPainterPath row;
      row.addRect(QRectF(rect.left(), rect.top(), rect.width(), rect.height()));
      painter->save();
      painter->setRenderHint(QPainter::Antialiasing, true);
      painter->fillPath(header.intersected(row), fill);
      painter->restore();
    }
    return;
  }
  if (fill.isValid())
  {
    drawRoundedBox(painter, rect, 6., fill, QColor());
  }
  if (widget && !widget->property("collapsed").toBool())
  {
    painter->save();
    painter->setPen(optionToken(option, "hairline"));
    painter->drawLine(option->rect.bottomLeft(), option->rect.bottomRight());
    painter->restore();
  }
}

//------------------------------------------------------------------------------
/// Background and text colors of a chip, from its "chipLevel" property
void chipColors(const QWidget* widget, bool dark, QColor& background, QColor& foreground)
{
  auto token = [dark](const char* name) { return qSlicerStyle::tokenColor(QLatin1String(name), dark); };
  const QByteArray level = widget ? widget->property("chipLevel").toByteArray() : QByteArray();
  const char* status = nullptr;
  if (level == "ok" || level == "success")
  {
    status = "success";
  }
  else if (level == "warn" || level == "warning")
  {
    status = "warning";
  }
  else if (level == "danger")
  {
    status = "danger";
  }
  if (status)
  {
    foreground = token(status);
    background = foreground;
    background.setAlphaF(0.16);
  }
  else if (level == "busy")
  {
    background = token("accent-subtle");
    foreground = token("accent-fg");
  }
  else
  {
    background = token("control-hover");
    foreground = token("text2");
  }
}

//------------------------------------------------------------------------------
/// Draw the background of a frame or label with a slicerRole: rounded card (10 px, "card" fill),
/// card on the canvas ("cardLow": 12 px, "card-low" fill), icon tile ("iconTile": "accent-subtle"),
/// chip pill, kbd key and segmented control track ("canvas" fill). Return false if the widget has
/// no such role.
bool drawRoleFrame(const QStyleOption* option, QPainter* painter, const QWidget* widget)
{
  const QByteArray role = widgetRole(widget);
  if (role.isEmpty())
  {
    return false;
  }
  const bool dark = hasDarkPalette(option);
  auto token = [dark](const char* name) { return qSlicerStyle::tokenColor(QLatin1String(name), dark); };
  // Chips and keys are painted over the whole widget: the contents margins set in polish() are
  // their inner padding (the frame rectangle of a label excludes its contents margins).
  const bool padded = (role == "chip" || role == "kbd") && widget;
  const QRectF rect(padded ? QRectF(widget->rect()) : QRectF(option->rect));
  if (role == "card")
  {
    drawRoundedBox(painter, rect, 10., token("card"), token("hairline"));
    return true;
  }
  if (role == "cardLow")
  {
    drawRoundedBox(painter, rect, 12., token("card-low"), token("hairline"));
    return true;
  }
  if (role == "iconTile")
  {
    drawRoundedBox(painter, rect, qMin<qreal>(9., qRound(rect.height() * 0.24)), token("accent-subtle"), QColor());
    return true;
  }
  if (role == "segmented")
  {
    // Tall tracks (the workspace switcher) have a 9 px radius, compact ones 8 px
    drawRoundedBox(painter, rect, rect.height() >= 34. ? 9. : 8., token("canvas"), token("hairline"));
    return true;
  }
  if (role == "chip")
  {
    QColor background;
    QColor foreground;
    chipColors(widget, dark, background, foreground);
    drawRoundedBox(painter, rect, qMin<qreal>(10., rect.height() / 2.), background, QColor());
    return true;
  }
  if (role == "kbd")
  {
    // A key cap around the text, vertically centered when a layout stretches the label
    QRectF keyRect(rect);
    if (widget)
    {
      const qreal keyHeight = qMin(rect.height(), qreal(widget->fontMetrics().height() + 4));
      keyRect.setTop(rect.top() + qRound((rect.height() - keyHeight) / 2.));
      keyRect.setHeight(keyHeight);
    }
    drawRoundedBox(painter, keyRect, 5., QColor(), token("border"));
    return true;
  }
  return false;
}

//------------------------------------------------------------------------------
/// Set rounded corners on a menu or tool tip window (a mask, which needs no compositing)
void updateRoundedMask(QWidget* widget)
{
  if (!widget->isWindow() || widget->size().isEmpty())
  {
    return;
  }
  QBitmap mask(widget->size());
  mask.fill(Qt::color0);
  QPainter painter(&mask);
  painter.setPen(Qt::NoPen);
  painter.setBrush(Qt::color1);
  painter.drawRoundedRect(QRect(QPoint(0, 0), widget->size()), 8, 8);
  painter.end();
  widget->setMask(mask);
}

//------------------------------------------------------------------------------
/// Decide the theme followed by SystemTheme (see qSlicerStyle::resolveSystemTheme())
qSlicerStyle::Theme resolveSystemThemeFromHints(int colorScheme, int appsUseLightTheme, int platformWindowLightness)
{
  // Qt::ColorScheme: 0 unknown, 1 light, 2 dark
  if (colorScheme == 2 || colorScheme == 1)
  {
    return colorScheme == 2 ? qSlicerStyle::DarkTheme : qSlicerStyle::LightTheme;
  }
  if (appsUseLightTheme == 0 || appsUseLightTheme == 1)
  {
    return appsUseLightTheme == 0 ? qSlicerStyle::DarkTheme : qSlicerStyle::LightTheme;
  }
  if (platformWindowLightness >= 0)
  {
    return platformWindowLightness < 128 ? qSlicerStyle::DarkTheme : qSlicerStyle::LightTheme;
  }
  return qSlicerStyle::LightTheme;
}

//------------------------------------------------------------------------------
/// Return true if \a widget is a menu or a tool tip, which get rounded corners
bool hasRoundedMask(const QWidget* widget)
{
  return qobject_cast<const QMenu*>(widget) || widget->inherits("QTipLabel");
}

//------------------------------------------------------------------------------
// True while a slider handles a left button press on its handle (see SliderHandlePressFilter)
bool SliderHandlePress = false;

//------------------------------------------------------------------------------
/// Return true if \a pos is on the handle of \a slider
bool isOnSliderHandle(const QSlider* slider, const QPoint& pos)
{
  // Same options as QSlider::initStyleOption() (protected)
  QStyleOptionSlider option;
  option.initFrom(slider);
  option.subControls = QStyle::SC_None;
  option.activeSubControls = QStyle::SC_None;
  option.orientation = slider->orientation();
  option.maximum = slider->maximum();
  option.minimum = slider->minimum();
  option.tickPosition = slider->tickPosition();
  option.tickInterval = slider->tickInterval();
  option.upsideDown = (slider->orientation() == Qt::Horizontal) ? (slider->invertedAppearance() != (option.direction == Qt::RightToLeft)) //
                                                                : !slider->invertedAppearance();
  option.direction = Qt::LeftToRight; // QSlider uses upsideDown instead
  option.sliderPosition = slider->sliderPosition();
  option.sliderValue = slider->value();
  option.singleStep = slider->singleStep();
  option.pageStep = slider->pageStep();
  if (slider->orientation() == Qt::Horizontal)
  {
    option.state |= QStyle::State_Horizontal;
  }
  return slider->style()->subControlRect(QStyle::CC_Slider, &option, QStyle::SC_SliderHandle, slider).contains(pos);
}

//------------------------------------------------------------------------------
/// Event filter of the sliders in the Comfortable density, where a left click on the groove
/// moves the handle there (SH_Slider_AbsoluteSetButtons): a left click on the handle grabs it
/// where it is clicked, instead of moving its center to the mouse (which changes the value).
class SliderHandlePressFilter : public QObject
{
public:
  using QObject::QObject;
  bool eventFilter(QObject* obj, QEvent* event) override
  {
    if (event->type() == QEvent::MouseButtonPress)
    {
      const QMouseEvent* mouseEvent = static_cast<const QMouseEvent*>(event);
#if QT_VERSION >= QT_VERSION_CHECK(6, 0, 0)
      const QPoint pos = mouseEvent->position().toPoint();
#else
      const QPoint pos = mouseEvent->pos();
#endif
      const QSlider* slider = qobject_cast<const QSlider*>(obj);
      SliderHandlePress = slider && mouseEvent->button() == Qt::LeftButton && isOnSliderHandle(slider, pos);
    }
    else if (event->type() == QEvent::MouseButtonRelease)
    {
      SliderHandlePress = false;
    }
    return QObject::eventFilter(obj, event);
  }
};

//------------------------------------------------------------------------------
/// Return the event filter shared by the sliders (see SliderHandlePressFilter)
QObject* sliderHandlePressFilter()
{
  static QPointer<QObject> filter;
  if (!filter)
  {
    filter = new SliderHandlePressFilter(QCoreApplication::instance());
  }
  return filter;
}

} // namespace

// --------------------------------------------------------------------------
// qSlicerStyle methods

// --------------------------------------------------------------------------
qSlicerStyle::qSlicerStyle()
  : Superclass(QStyleFactory::create("fusion"))
{
  this->baseStyle()->setParent(this);
  // Tells Python widgets (e.g. Slicer Studio) that the "slicerRole" widget property is painted
  this->setProperty("supportsSlicerRole", true);
}

// --------------------------------------------------------------------------
qSlicerStyle::~qSlicerStyle() = default;

//------------------------------------------------------------------------------
QStyle::SubControl qSlicerStyle::hitTestComplexControl(ComplexControl cc, const QStyleOptionComplex* opt, const QPoint& pt, const QWidget* widget) const
{
  SubControl sc = SC_None;
  switch (cc)
  {
    // Hot area for the groove shall be the entire height of the widget
#ifndef QT_NO_SLIDER
    case CC_Slider:
      if (const QStyleOptionSlider* slider = qstyleoption_cast<const QStyleOptionSlider*>(opt))
      {
        QRect r = proxy()->subControlRect(cc, slider, SC_SliderHandle, widget);
        if (r.isValid() && r.contains(pt))
        {
          sc = SC_SliderHandle;
        }
        else
        {
          r = proxy()->subControlRect(cc, slider, SC_SliderGroove, widget);
          QPoint center = r.center();
          if (slider->orientation == Qt::Horizontal)
          {
            // If there is no widget, use QStyle::PM_SliderThickness
            r.setHeight(widget ? widget->height() : this->pixelMetric(QStyle::PM_SliderThickness, opt, widget));
          }
          else
          {
            // If there is no widget, use QStyle::PM_SliderThickness
            r.setWidth(widget ? widget->width() : this->pixelMetric(QStyle::PM_SliderThickness, opt, widget));
          }
          r.moveCenter(center);
          if (r.isValid() && r.contains(pt))
          {
            sc = SC_SliderGroove;
          }
        }
      }
      break;
#endif // QT_NO_SLIDER
    default: sc = Superclass::hitTestComplexControl(cc, opt, pt, widget); break;
  }
  return sc;
}

//------------------------------------------------------------------------------
QRect qSlicerStyle::subControlRect(ComplexControl control, const QStyleOptionComplex* option, SubControl subControl, const QWidget* widget) const
{
  if (control == QStyle::CC_ScrollBar && qSlicerStyle::density() == qSlicerStyle::Comfortable)
  {
    if (const QStyleOptionSlider* scrollBar = qstyleoption_cast<const QStyleOptionSlider*>(option))
    {
      return thinScrollBarRect(scrollBar, subControl, widget, this->proxy());
    }
  }
  return this->Superclass::subControlRect(control, option, subControl, widget);
}

//------------------------------------------------------------------------------
QRect qSlicerStyle::subElementRect(SubElement element, const QStyleOption* option, const QWidget* widget) const
{
  switch (element)
  {
    case QStyle::SE_CheckBoxIndicator:
    case QStyle::SE_CheckBoxContents:
    case QStyle::SE_CheckBoxFocusRect:
    case QStyle::SE_CheckBoxClickRect:
      if (option && isSwitch(widget))
      {
        // Switches: the text on the left, the track on the right edge of the check box
        const QRect rect = option->rect;
        const QRect track(rect.right() - SwitchWidth + 1, rect.top() + (rect.height() - SwitchHeight) / 2, SwitchWidth, SwitchHeight);
        QRect result;
        switch (element)
        {
          case QStyle::SE_CheckBoxIndicator: result = track; break;
          case QStyle::SE_CheckBoxContents: result = QRect(rect.left(), rect.top(), qMax(0, rect.width() - SwitchWidth - SwitchSpacing), rect.height()); break;
          case QStyle::SE_CheckBoxFocusRect: result = track.adjusted(-2, -2, 2, 2).intersected(rect); break;
          default: result = rect; break;
        }
        return QStyle::visualRect(option->direction, rect, result);
      }
      break;
    case QStyle::SE_ProgressBarGroove:
    case QStyle::SE_ProgressBarContents:
    case QStyle::SE_ProgressBarLabel:
      if (isPillProgressBar(option))
      {
        // The text is drawn beside the bar, on the right
        const QStyleOptionProgressBar* progressBar = qstyleoption_cast<const QStyleOptionProgressBar*>(option);
        const QRect rect = progressBar->rect;
        const int labelWidth = progressBarLabelWidth(progressBar);
        const int spacing = labelWidth > 0 ? ProgressBarLabelSpacing : 0;
        QRect result = (element == QStyle::SE_ProgressBarLabel) //
                         ? QRect(rect.right() - labelWidth + 1, rect.top(), labelWidth, rect.height())
                         : QRect(rect.left(), rect.top(), rect.width() - labelWidth - spacing, rect.height());
        return QStyle::visualRect(progressBar->direction, rect, result);
      }
      break;
    default: break;
  }
  return this->Superclass::subElementRect(element, option, widget);
}

// --------------------------------------------------------------------------
int qSlicerStyle::pixelMetric(PixelMetric metric, const QStyleOption* option, const QWidget* widget) const
{
  switch (metric)
  {
    case QStyle::PM_LayoutLeftMargin:
    case QStyle::PM_LayoutTopMargin:
    case QStyle::PM_LayoutRightMargin:
    case QStyle::PM_LayoutBottomMargin:
    {
      const bool comfortable = (qSlicerStyle::density() == qSlicerStyle::Comfortable);
      if (isCardCollapsible(widget))
      {
        // Card padding (VISUAL 4.5): 14 px, 2 px under the title row
        return metric == QStyle::PM_LayoutTopMargin ? 2 : 14;
      }
      if (widget && widget->inherits("ctkCollapsibleButton"))
      {
        return comfortable ? 6 : 4; // 9 by default
      }
      return comfortable ? 8 : 6; // 9 by default
      break;
    }
    case QStyle::PM_LayoutHorizontalSpacing:
    case QStyle::PM_LayoutVerticalSpacing:
      return qSlicerStyle::density() == qSlicerStyle::Comfortable ? 4 : 3; // 6 by default
      break;
    case QStyle::PM_SliderLength:
      return qSlicerStyle::density() == qSlicerStyle::Comfortable ? 14 : 12; // default to 27
      break;
    case QStyle::PM_SliderThickness:
      if (qSlicerStyle::density() == qSlicerStyle::Comfortable)
      {
        return 14; // round handle (15 by default)
      }
      break;
    case QStyle::PM_ScrollBarExtent:
      if (qSlicerStyle::density() == qSlicerStyle::Comfortable)
      {
        return 10; // thin scroll bars without step buttons (14 by default)
      }
      break;
    case QStyle::PM_SplitterWidth:
      if (qSlicerStyle::density() == qSlicerStyle::Comfortable)
      {
        return 4;
      }
      break;
    case QStyle::PM_DockWidgetSeparatorExtent:
      if (qSlicerStyle::density() == qSlicerStyle::Comfortable)
      {
        return 3; // 6 by default
      }
      break;
    case QStyle::PM_ButtonIconSize:
      return 24; // Like with cleanlooks style
      break;
    case QStyle::PM_IndicatorWidth:
    case QStyle::PM_IndicatorHeight:
    case QStyle::PM_ExclusiveIndicatorWidth:
    case QStyle::PM_ExclusiveIndicatorHeight:
      if (isSwitch(widget) && (metric == QStyle::PM_IndicatorWidth || metric == QStyle::PM_IndicatorHeight))
      {
        return metric == QStyle::PM_IndicatorWidth ? SwitchWidth : SwitchHeight;
      }
      if (qSlicerStyle::density() == qSlicerStyle::Comfortable)
      {
        return 16; // 14 by default
      }
      break;
    case QStyle::PM_MenuVMargin:
      if (qSlicerStyle::density() == qSlicerStyle::Comfortable)
      {
        return 4; // 0 by default
      }
      break;
    case QStyle::PM_ToolTipLabelFrameWidth:
      if (qSlicerStyle::density() == qSlicerStyle::Comfortable)
      {
        return 5; // 2 by default, the tool tip text margin is one more pixel
      }
      break;
    case QStyle::PM_ToolBarItemSpacing:
    case QStyle::PM_ToolBarItemMargin:
    case QStyle::PM_ToolBarFrameWidth:
    case QStyle::PM_ToolBarHandleExtent:
      if (widgetRole(widget) == "topBar")
      {
        return 0;
      }
      break;
    default: break;
  }
  return Superclass::pixelMetric(metric, option, widget);
}

//------------------------------------------------------------------------------
QPalette qSlicerStyle::standardPalette() const
{
  bool dark = false;
  // The light palette is always used for testing and without main window (historical appearance).
  if (!useClassicAppearance())
  {
    switch (qSlicerStyle::theme())
    {
      case qSlicerStyle::DarkTheme: dark = true; break;
      case qSlicerStyle::LightTheme: dark = false; break;
      case qSlicerStyle::SystemTheme:
      default: dark = (qSlicerStyle::systemTheme() == qSlicerStyle::DarkTheme); break;
    }
  }
  return dark ? this->standardDarkPalette() : this->standardLightPalette();
}

//------------------------------------------------------------------------------
QPalette qSlicerStyle::standardLightPalette() const
{
  QPalette palette = this->Superclass::standardPalette();
  // Customizations from the standard style ("fusion")
  // See https://doc.qt.io/qt-5/qpalette.html#ColorRole-enum
  // Colors set without color group apply to the Active, Inactive and Disabled groups.
  // Central color roles
  palette.setColor(QPalette::Window, Qt::white);
  palette.setColor(QPalette::WindowText, "#18181b");
  palette.setColor(QPalette::Disabled, QPalette::WindowText, "#a3a3ab");
  palette.setColor(QPalette::Base, Qt::white);
  palette.setColor(QPalette::AlternateBase, "#f6f6f8");
  palette.setColor(QPalette::ToolTipBase, "#18181b");
  palette.setColor(QPalette::ToolTipText, "#f6f6f8");
  palette.setColor(QPalette::Text, "#18181b");
  palette.setColor(QPalette::Disabled, QPalette::Text, "#a3a3ab");
  palette.setColor(QPalette::PlaceholderText, "#6b6b74"); // text3
  palette.setColor(QPalette::Disabled, QPalette::PlaceholderText, "#a3a3ab");
  palette.setColor(QPalette::Button, Qt::white);
  palette.setColor(QPalette::Disabled, QPalette::Button, "#f1f1f4");
  palette.setColor(QPalette::ButtonText, "#18181b");
  palette.setColor(QPalette::Disabled, QPalette::ButtonText, "#a3a3ab");
  palette.setColor(QPalette::BrightText, "#cf222e");
  // Color roles used mostly for 3D bevel and shadow effects.
  palette.setColor(QPalette::Light, Qt::white);    // Lighter than Button color.
  palette.setColor(QPalette::Midlight, "#eeeef2"); // Between Button and Light.
  palette.setColor(QPalette::Mid, "#d4d4da");      // Between Button and Dark.
  palette.setColor(QPalette::Dark, "#a1a1aa");     // Darker than Button.
  palette.setColor(QPalette::Shadow, "#52525b");   // A very dark color.
  // Color roles relate to selected (marked) items
  palette.setColor(QPalette::Highlight, "#2f6fe0");
  palette.setColor(QPalette::Disabled, QPalette::Highlight, "#d4d4da");
  palette.setColor(QPalette::HighlightedText, Qt::white);
  // Color roles related to hyperlinks
  // Links in accent-fg, readable (WCAG AA) on the window color
  palette.setColor(QPalette::Link, "#2158bd");
  palette.setColor(QPalette::LinkVisited, "#2158bd");

  applyAccentColor(palette, false);
  return palette;
}

//------------------------------------------------------------------------------
QPalette qSlicerStyle::standardDarkPalette() const
{
  QPalette palette = this->Superclass::standardPalette();
  // See https://doc.qt.io/qt-5/qpalette.html#ColorRole-enum
  // Colors set without color group apply to the Active, Inactive and Disabled groups.
  // Central color roles: graphite surfaces (window #161618), fields on the canvas color
  palette.setColor(QPalette::Window, "#161618");
  palette.setColor(QPalette::WindowText, "#ececef");
  palette.setColor(QPalette::Disabled, QPalette::WindowText, "#6b6b73");
  palette.setColor(QPalette::Base, "#0f0f11");
  palette.setColor(QPalette::Disabled, QPalette::Base, "#18181b");
  palette.setColor(QPalette::AlternateBase, "#141416");
  palette.setColor(QPalette::ToolTipBase, "#0b0b0d");
  palette.setColor(QPalette::ToolTipText, "#ececef");
  palette.setColor(QPalette::Text, "#ececef");
  palette.setColor(QPalette::Disabled, QPalette::Text, "#6b6b73");
  palette.setColor(QPalette::PlaceholderText, "#8b8b94"); // text3
  palette.setColor(QPalette::Disabled, QPalette::PlaceholderText, "#55555c");
  palette.setColor(QPalette::Button, "#1d1d20");
  palette.setColor(QPalette::Disabled, QPalette::Button, "#1a1a1d");
  palette.setColor(QPalette::ButtonText, "#ececef");
  palette.setColor(QPalette::Disabled, QPalette::ButtonText, "#6b6b73");
  palette.setColor(QPalette::BrightText, "#ff6b6b"); // Lighter than Qt::red
  // Color roles used mostly for 3D bevel and shadow effects.
  palette.setColor(QPalette::Light, "#45454c");    // Lighter than Button color.
  palette.setColor(QPalette::Midlight, "#34343a"); // Between Button and Light.
  palette.setColor(QPalette::Mid, "#26262a");      // Between Button and Dark.
  palette.setColor(QPalette::Dark, "#0b0b0d");     // Darker than Button.
  palette.setColor(QPalette::Shadow, Qt::black);   // A very dark color.
  // Color roles relate to selected (marked) items
  palette.setColor(QPalette::Highlight, "#2f6fe0");
  palette.setColor(QPalette::Disabled, QPalette::Highlight, "#34343a");
  palette.setColor(QPalette::HighlightedText, Qt::white);
  palette.setColor(QPalette::Disabled, QPalette::HighlightedText, "#a1a1aa");
  // Color roles related to hyperlinks
  palette.setColor(QPalette::Link, "#7aa7ff");
  palette.setColor(QPalette::LinkVisited, "#a78bfa");

  applyAccentColor(palette, true);
  return palette;
}

//------------------------------------------------------------------------------
int qSlicerStyle::styleHint(StyleHint hint, const QStyleOption* opt, const QWidget* widget, QStyleHintReturn* returnData) const
{
  int res;
  switch (hint)
  {
    case QStyle::SH_ItemView_ActivateItemOnSingleClick:
      if (widget && widget->property("SH_ItemView_ActivateItemOnSingleClick").isValid())
      {
        res = widget->property("SH_ItemView_ActivateItemOnSingleClick").toBool();
        break;
      }
      Q_FALLTHROUGH();
    // Overload the SH_ComboBox_Popup option to prevent issue with checkable
    // combobox. For more details see: https://bugreports.qt.io/browse/QTBUG-19683
    case QStyle::SH_ComboBox_Popup:
    {
      res = 0;
      break;
    }
    case QStyle::SH_Table_GridLineColor:
      if (opt)
      {
        // Visible grid lines of tables, also on dark palettes
        res = static_cast<int>(optionToken(opt, "border").rgba());
        break;
      }
      res = this->Superclass::styleHint(hint, opt, widget, returnData);
      break;
    case QStyle::SH_ToolTip_WakeUpDelay: res = 500; break;
    case QStyle::SH_Slider_AbsoluteSetButtons:
    case QStyle::SH_Slider_PageSetButtons:
      if (qSlicerStyle::density() == qSlicerStyle::Comfortable && !SliderHandlePress)
      {
        // A left click on the groove moves the handle there, the middle button moves by pages
        // (a left click on the handle of a QSlider grabs it, see SliderHandlePressFilter)
        res = static_cast<int>((hint == QStyle::SH_Slider_AbsoluteSetButtons) ? Qt::LeftButton : Qt::MiddleButton);
        break;
      }
      res = this->Superclass::styleHint(hint, opt, widget, returnData);
      break;
    case QStyle::SH_EtchDisabledText: res = 0; break;
    default: res = this->Superclass::styleHint(hint, opt, widget, returnData);
  }
  return res;
}

//------------------------------------------------------------------------------
void qSlicerStyle::drawPrimitive(PrimitiveElement element, const QStyleOption* option, QPainter* painter, const QWidget* widget) const
{
  if (!painter || !option)
  {
    this->Superclass::drawPrimitive(element, option, painter, widget);
    return;
  }
  switch (element)
  {
    case QStyle::PE_PanelButtonCommand:
      // Combo boxes keep the base style (they are drawn as buttons by Fusion)
      if (qobject_cast<const QComboBox*>(widget))
      {
        break;
      }
      drawButtonPanel(option, painter, widget, qstyleoption_cast<const QStyleOptionToolButton*>(option) != nullptr);
      return;
    case QStyle::PE_PanelButtonTool:
      if (widget && widget->inherits("QDockWidgetTitleButton"))
      {
        break;
      }
      drawButtonPanel(option, painter, widget, true);
      return;
    case QStyle::PE_FrameButtonTool: drawRoundedBox(painter, QRectF(option->rect), 6., QColor(), optionToken(option, "border")); return;
    case QStyle::PE_FrameFocusRect:
    {
      // Only for keyboard focus changes
      if (!(option->state & QStyle::State_KeyboardFocusChange))
      {
        return;
      }
      // Around the label of check boxes and radio buttons, around tool buttons (below) and items
      QRectF ringRect(option->rect);
      qreal radius = 4.;
      if (option->state & QStyle::State_Item)
      {
        // Item views always set State_KeyboardFocusChange: show the ring only after keyboard navigation
        if (!widget || !widget->window()->testAttribute(Qt::WA_KeyboardFocusChange))
        {
          return;
        }
        radius = 3.;
      }
      else if (isSwitch(widget))
      {
        // Around the track of a switch (SE_CheckBoxFocusRect)
        radius = ringRect.height() / 2.;
      }
      else if (const QToolButton* toolButton = qobject_cast<const QToolButton*>(widget))
      {
        // The base style insets the focus rectangle of tool buttons by 3 pixels: ring the button
        ringRect = QRectF(toolButton->rect());
        radius = 6.;
      }
      else if (widget)
      {
        // e.g. the label of a check box: leave some space between the text and the ring
        ringRect = QRectF(option->rect.adjusted(-2, -1, 2, 1).intersected(widget->rect()));
      }
      const QColor ringColor = (option->state & QStyle::State_Selected) ? option->palette.color(QPalette::HighlightedText) : optionToken(option, "accent-fg");
      drawFocusRing(painter, ringRect, radius, ringColor);
      return;
    }
    case QStyle::PE_IndicatorArrowUp:
    case QStyle::PE_IndicatorArrowDown:
    case QStyle::PE_IndicatorArrowLeft:
    case QStyle::PE_IndicatorArrowRight:
      if (isCardCollapsible(widget) && option->rect.height() <= CardHeaderHeight && option->rect.top() < CardHeaderHeight)
      {
        // The chevron of a card is drawn on the right of its title row (drawCardHeader())
        return;
      }
      drawChevron(element, QRectF(option->rect), arrowColor(option), painter);
      return;
    case QStyle::PE_IndicatorCheckBox:
    case QStyle::PE_IndicatorRadioButton:
      if (element == QStyle::PE_IndicatorCheckBox && isSwitch(widget))
      {
        drawSwitch(option, painter);
        return;
      }
      // Item view checks and menu check boxes are drawn by the base style as PE_IndicatorCheckBox.
      drawIndicator(element, option, painter);
      return;
    case QStyle::PE_PanelMenu:
    case QStyle::PE_FrameMenu: drawMenuSurface(element, option, painter, widget); return;
    case QStyle::PE_PanelTipLabel:
    {
      // Tool tips are dark in both themes: the border is the one of the dark theme
      const QColor background = option->palette.color(QPalette::ToolTipBase);
      painter->fillRect(option->rect, background);
      drawRoundedBox(painter, QRectF(option->rect), 8., background, qSlicerStyle::tokenColor("border", true));
      return;
    }
    case QStyle::PE_PanelLineEdit:
      if (const QStyleOptionFrame* panel = qstyleoption_cast<const QStyleOptionFrame*>(option))
      {
        // The line edit of a spin box or of a combo box is drawn by the complex control
        if (widget && (qobject_cast<const QAbstractSpinBox*>(widget->parentWidget()) || qobject_cast<const QComboBox*>(widget->parentWidget())))
        {
          return;
        }
        if (panel->lineWidth > 0)
        {
          drawField(option->rect, option->state, hasDarkPalette(option), fieldFill(option), false, painter);
          return;
        }
      }
      break;
    case QStyle::PE_FrameLineEdit:
      // Frame of line edits drawn without their panel
      drawFieldFrame(option->rect, option->state, hasDarkPalette(option), painter);
      return;
    case QStyle::PE_FrameTabWidget: drawRoundedBox(painter, QRectF(option->rect), 6., QColor(), optionToken(option, "hairline")); return;
    case QStyle::PE_FrameTabBarBase:
      if (const QStyleOptionTabBarBase* tabBarBase = qstyleoption_cast<const QStyleOptionTabBarBase*>(option))
      {
        // A hairline on the side of the pages
        const QRect rect = tabBarBase->rect;
        painter->save();
        painter->setPen(optionToken(option, "hairline"));
        switch (tabBarBase->shape)
        {
          case QTabBar::RoundedSouth:
          case QTabBar::TriangularSouth: painter->drawLine(rect.topLeft(), rect.topRight()); break;
          case QTabBar::RoundedWest:
          case QTabBar::TriangularWest: painter->drawLine(rect.topRight(), rect.bottomRight()); break;
          case QTabBar::RoundedEast:
          case QTabBar::TriangularEast: painter->drawLine(rect.topLeft(), rect.bottomLeft()); break;
          default: painter->drawLine(rect.bottomLeft(), rect.bottomRight()); break;
        }
        painter->restore();
        return;
      }
      break;
    case QStyle::PE_IndicatorToolBarHandle:
    {
      if (widgetRole(widget) == "topBar")
      {
        return;
      }
      // Two subtle dots
      painter->save();
      painter->setRenderHint(QPainter::Antialiasing, true);
      painter->setPen(Qt::NoPen);
      painter->setBrush(optionToken(option, "text3"));
      const QPointF center = QRectF(option->rect).center();
      const bool horizontal = option->state & QStyle::State_Horizontal;
      for (int dot : { -1, 1 })
      {
        const QPointF offset = horizontal ? QPointF(0., dot * 2.5) : QPointF(dot * 2.5, 0.);
        painter->drawEllipse(center + offset, 1., 1.);
      }
      painter->restore();
      return;
    }
    case QStyle::PE_IndicatorToolBarSeparator:
    {
      // Hairline
      painter->save();
      painter->setPen(optionToken(option, "hairline"));
      const QRect rect = option->rect;
      const int margin = 6;
      if (option->state & QStyle::State_Horizontal)
      {
        const int x = rect.left() + rect.width() / 2;
        painter->drawLine(x, rect.top() + margin, x, rect.bottom() - margin);
      }
      else
      {
        const int y = rect.top() + rect.height() / 2;
        painter->drawLine(rect.left() + margin, y, rect.right() - margin, y);
      }
      painter->restore();
      return;
    }
    case QStyle::PE_Frame:
    case QStyle::PE_Widget:
      if (widgetRole(widget) == "card" || widgetRole(widget) == "cardLow")
      {
        drawRoleFrame(option, painter, widget);
        return;
      }
      break;
    default: break;
  }
  this->Superclass::drawPrimitive(element, option, painter, widget);
}

//------------------------------------------------------------------------------
void qSlicerStyle::drawControl(ControlElement element, const QStyleOption* option, QPainter* painter, const QWidget* widget) const
{
  if (painter && option)
  {
    switch (element)
    {
      case QStyle::CE_CheckBox:
        if (isSwitch(widget))
        {
          // Switches: the parts at the places of subElementRect() (the base style asks its own geometry)
          if (const QStyleOptionButton* button = qstyleoption_cast<const QStyleOptionButton*>(option))
          {
            QStyleOptionButton part(*button);
            part.rect = this->subElementRect(QStyle::SE_CheckBoxIndicator, button, widget);
            this->proxy()->drawPrimitive(QStyle::PE_IndicatorCheckBox, &part, painter, widget);
            part.rect = this->subElementRect(QStyle::SE_CheckBoxContents, button, widget);
            this->proxy()->drawControl(QStyle::CE_CheckBoxLabel, &part, painter, widget);
            if (button->state & QStyle::State_HasFocus)
            {
              QStyleOptionFocusRect focus;
              focus.QStyleOption::operator=(*button);
              focus.rect = this->subElementRect(QStyle::SE_CheckBoxFocusRect, button, widget);
              this->proxy()->drawPrimitive(QStyle::PE_FrameFocusRect, &focus, painter, widget);
            }
            return;
          }
        }
        break;
      case QStyle::CE_PushButtonBevel:
        if (widget && widget->inherits("ctkCollapsibleButton"))
        {
          drawCollapsibleHeader(option, painter, widget);
          return;
        }
        break;
      case QStyle::CE_MenuItem:
        if (const QStyleOptionMenuItem* menuItem = qstyleoption_cast<const QStyleOptionMenuItem*>(option))
        {
          drawMenuItem(menuItem, painter, widget, this);
          return;
        }
        break;
      case QStyle::CE_ShapedFrame:
        if (drawRoleFrame(option, painter, widget))
        {
          return;
        }
        break;
      case QStyle::CE_ToolBar:
        if (widgetRole(widget) == "topBar")
        {
          painter->fillRect(option->rect, optionToken(option, "view-bar"));
          painter->save();
          painter->setPen(optionToken(option, "hairline"));
          painter->drawLine(option->rect.bottomLeft(), option->rect.bottomRight());
          painter->restore();
          return;
        }
        // Flat: no gradient and no bevel lines
        painter->fillRect(option->rect, option->palette.color(QPalette::Window));
        return;
      case QStyle::CE_HeaderSection:
      case QStyle::CE_HeaderEmptyArea: drawHeaderSection(option, painter, element == QStyle::CE_HeaderEmptyArea); return;
      case QStyle::CE_TabBarTabShape:
        if (const QStyleOptionTab* tab = qstyleoption_cast<const QStyleOptionTab*>(option))
        {
          drawTabShape(tab, painter);
          return;
        }
        break;
      case QStyle::CE_TabBarTabLabel:
        if (const QStyleOptionTab* tab = qstyleoption_cast<const QStyleOptionTab*>(option))
        {
          // Tabs that are not selected nor hovered have "text2" labels, unless the application set a text color
          const bool enabled = tab->state & QStyle::State_Enabled;
          const bool customColor = widget && tab->palette.color(QPalette::WindowText) != widget->palette().color(QPalette::WindowText);
          if (enabled && !customColor && !(tab->state & (QStyle::State_Selected | QStyle::State_MouseOver)))
          {
            QStyleOptionTab label(*tab);
            label.palette.setColor(QPalette::WindowText, optionToken(option, "text2"));
            this->Superclass::drawControl(element, &label, painter, widget);
            return;
          }
        }
        break;
      case QStyle::CE_ProgressBar:
        if (isPillProgressBar(option))
        {
          // The parts are placed by subElementRect() (the base style may not ask the proxy style)
          const QStyleOptionProgressBar* progressBar = qstyleoption_cast<const QStyleOptionProgressBar*>(option);
          QStyleOptionProgressBar part(*progressBar);
          part.rect = this->subElementRect(QStyle::SE_ProgressBarGroove, progressBar, widget);
          this->proxy()->drawControl(QStyle::CE_ProgressBarGroove, &part, painter, widget);
          part.rect = this->subElementRect(QStyle::SE_ProgressBarContents, progressBar, widget);
          this->proxy()->drawControl(QStyle::CE_ProgressBarContents, &part, painter, widget);
          if (progressBar->textVisible)
          {
            part.rect = this->subElementRect(QStyle::SE_ProgressBarLabel, progressBar, widget);
            this->proxy()->drawControl(QStyle::CE_ProgressBarLabel, &part, painter, widget);
          }
          return;
        }
        break;
      case QStyle::CE_ProgressBarGroove:
        if (isPillProgressBar(option))
        {
          drawRoundedBox(painter, progressBarPill(option->rect), 2., optionToken(option, "border"), QColor());
          return;
        }
        break;
      case QStyle::CE_ProgressBarContents:
        if (isPillProgressBar(option))
        {
          const QStyleOptionProgressBar* progressBar = qstyleoption_cast<const QStyleOptionProgressBar*>(option);
          const bool enabled = option->state & QStyle::State_Enabled;
          const QColor fill = optionToken(option, enabled ? "accent-fg" : "text-disabled");
          const QRectF pill = progressBarPill(option->rect);
          if (progressBar->minimum == 0 && progressBar->maximum == 0)
          {
            // Busy indicator: an accent segment moving along the groove. The (invisible) contents
            // of the base style keep its animation running, which repaints the progress bar.
            painter->save();
            painter->setOpacity(0.);
            this->Superclass::drawControl(element, option, painter, widget);
            painter->restore();
            const qreal period = 1500.;
            const qreal phase = std::fmod(static_cast<qreal>(QDateTime::currentMSecsSinceEpoch()), period) / period;
            const qreal segmentWidth = qMax<qreal>(12., pill.width() * 0.3);
            QRectF segment(pill.left() - segmentWidth + phase * (pill.width() + segmentWidth), pill.top(), segmentWidth, pill.height());
            if (progressBar->direction == Qt::RightToLeft)
            {
              segment.moveRight(pill.right() - (segment.left() - pill.left()));
            }
            QPainterPath clip;
            clip.addRoundedRect(pill, 2., 2.);
            painter->save();
            painter->setClipPath(clip, Qt::IntersectClip);
            drawRoundedBox(painter, segment, 2., fill, QColor());
            painter->restore();
            return;
          }
          if (progressBar->maximum <= progressBar->minimum || progressBar->progress < progressBar->minimum)
          {
            return;
          }
          const qreal ratio = qBound<qreal>(0., qreal(qint64(progressBar->progress) - progressBar->minimum) / (qint64(progressBar->maximum) - progressBar->minimum), 1.);
          if (ratio <= 0.)
          {
            return;
          }
          const bool reverse = (progressBar->direction == Qt::RightToLeft) != progressBar->invertedAppearance;
          QRectF filled(pill);
          filled.setWidth(pill.width() * ratio);
          if (reverse)
          {
            filled.moveRight(pill.right());
          }
          drawRoundedBox(painter, filled, 2., fill, QColor());
          return;
        }
        break;
      case QStyle::CE_ProgressBarLabel:
        if (isPillProgressBar(option))
        {
          const QStyleOptionProgressBar* progressBar = qstyleoption_cast<const QStyleOptionProgressBar*>(option);
          QPalette palette(progressBar->palette);
          palette.setColor(QPalette::WindowText, optionToken(option, "text2"));
          const Qt::Alignment alignment = Qt::AlignRight | Qt::AlignVCenter;
          // A text longer than its slot keeps its beginning and ends with an ellipsis (the slot is
          // measured with horizontalAdvance(), which is rounded, see progressBarLabelWidth())
          const QFontMetrics& metrics = progressBar->fontMetrics;
          const QString text = (metrics.horizontalAdvance(progressBar->text) <= option->rect.width()) //
                                 ? progressBar->text
                                 : metrics.elidedText(progressBar->text, Qt::ElideRight, option->rect.width());
          this->proxy()->drawItemText(painter,
                                      option->rect,
                                      static_cast<int>(QStyle::visualAlignment(progressBar->direction, alignment)) | Qt::TextSingleLine,
                                      palette,
                                      progressBar->state & QStyle::State_Enabled,
                                      text,
                                      QPalette::WindowText);
          return;
        }
        break;
      case QStyle::CE_Splitter: drawSplitterHandle(option, painter); return;
      default: break;
    }
  }
  this->Superclass::drawControl(element, option, painter, widget);
}

//------------------------------------------------------------------------------
void qSlicerStyle::drawComplexControl(ComplexControl control, const QStyleOptionComplex* option, QPainter* painter, const QWidget* widget) const
{
  if (control == QStyle::CC_ToolButton && option)
  {
    const QByteArray role = widgetRole(widget);
    const QStyleOptionToolButton* toolButton = qstyleoption_cast<const QStyleOptionToolButton*>(option);
    if (toolButton && (toolButton->features & QStyleOptionToolButton::HasMenu) //
        && (role == "ghost" || role == "toolTile" || role == "secondary" || isSegment(widget)))
    {
      // These buttons show their menu without menu indicator
      QStyleOptionToolButton withoutIndicator(*toolButton);
      withoutIndicator.features &= ~QStyleOptionToolButton::HasMenu;
      this->Superclass::drawComplexControl(control, &withoutIndicator, painter, widget);
      return;
    }
    const QString key = (role == "toolTile" && widget && painter) ? widget->property("studioKey").toString() : QString();
    if (!key.isEmpty())
    {
      // Tool tiles: the key of the tool in the top-right corner (10 px, "text3")
      this->Superclass::drawComplexControl(control, option, painter, widget);
      painter->save();
      QFont font = painter->font();
      font.setPixelSize(qMax(8, qRound(QFontInfo(widget->font()).pixelSize() * 0.9)));
      painter->setFont(font);
      painter->setPen(optionToken(option, (option->state & QStyle::State_Enabled) ? "text3" : "text-disabled"));
      painter->drawText(QRectF(option->rect).adjusted(4., 3., -5., -3.), Qt::AlignRight | Qt::AlignTop, key);
      painter->restore();
      return;
    }
  }
  if (painter && option)
  {
    switch (control)
    {
      case QStyle::CC_Slider:
        if (const QStyleOptionSlider* slider = qstyleoption_cast<const QStyleOptionSlider*>(option))
        {
          drawSlider(slider, painter, widget, this->proxy());
          return;
        }
        break;
      case QStyle::CC_ScrollBar:
        if (const QStyleOptionSlider* scrollBar = qstyleoption_cast<const QStyleOptionSlider*>(option))
        {
          // The Compact density keeps the scroll bars of the base style (with step buttons)
          if (qSlicerStyle::density() == qSlicerStyle::Comfortable)
          {
            drawThinScrollBar(scrollBar, painter, widget, this->proxy());
            return;
          }
        }
        break;
      case QStyle::CC_SpinBox:
        if (const QStyleOptionSpinBox* spinBox = qstyleoption_cast<const QStyleOptionSpinBox*>(option))
        {
          drawSpinBox(spinBox, painter, widget, this->proxy());
          return;
        }
        break;
      case QStyle::CC_ComboBox:
        if (const QStyleOptionComboBox* comboBox = qstyleoption_cast<const QStyleOptionComboBox*>(option))
        {
          drawComboBox(comboBox, painter, widget, this->proxy());
          return;
        }
        break;
      default: break;
    }
  }
  this->Superclass::drawComplexControl(control, option, painter, widget);
}

//------------------------------------------------------------------------------
void qSlicerStyle::drawItemText(QPainter* painter, const QRect& rect, int flags, const QPalette& pal, bool enabled, const QString& text, QPalette::ColorRole textRole) const
{
  QWidget* widget = paintedWidget(painter);
  if (!widget || text.isEmpty())
  {
    this->Superclass::drawItemText(painter, rect, flags, pal, enabled, text, textRole);
    return;
  }
  if (isCardCollapsible(widget) && rect.top() < CardHeaderHeight)
  {
    // The title row of a card: the whole header is laid out here
    drawCardHeader(painter, QRect(0, 0, widget->width(), qMin(widget->height(), CardHeaderHeight)), pal, enabled, text, widget);
    return;
  }
  if (widget->inherits("ctkCollapsibleButton"))
  {
    // SemiBold section titles
    painter->save();
    QFont font = painter->font();
    font.setWeight(QFont::DemiBold);
    painter->setFont(font);
    this->Superclass::drawItemText(painter, rect, flags, pal, enabled, text, textRole);
    painter->restore();
    return;
  }
  const QByteArray role = widgetRole(widget);
  const bool segment = role.isEmpty() && isSegment(widget);
  if ((role.isEmpty() && !segment) || !enabled || textRole == QPalette::NoRole)
  {
    this->Superclass::drawItemText(painter, rect, flags, pal, enabled, text, textRole);
    return;
  }
  const bool dark = isDark(pal, widget);
  auto token = [dark](const char* name) { return qSlicerStyle::tokenColor(QLatin1String(name), dark); };
  const QAbstractButton* button = qobject_cast<const QAbstractButton*>(widget);
  // Buttons that are hovered, pressed or checked use the normal text color
  const bool active = button && (button->underMouse() || button->isDown() || button->isChecked());
  QColor color;
  if (role == "primary")
  {
    color = pal.color(QPalette::HighlightedText);
  }
  else if (role == "ghost" || role == "toolTile" || segment)
  {
    color = active ? pal.color(textRole) : token("text2");
  }
  else if (role == "sectionHeader" || role == "secondaryText" || role == "kbd" || role == "overline")
  {
    color = token("text2");
  }
  else if (role == "caption")
  {
    color = token("text3");
  }
  else if (role == "chip")
  {
    QColor background;
    chipColors(widget, dark, background, color);
  }
  if (!color.isValid())
  {
    this->Superclass::drawItemText(painter, rect, flags, pal, enabled, text, textRole);
    return;
  }
  QPalette palette(pal);
  palette.setColor(textRole, color);
  this->Superclass::drawItemText(painter, rect, flags, palette, enabled, text, textRole);
}

//------------------------------------------------------------------------------
QSize qSlicerStyle::sizeFromContents(ContentsType type, const QStyleOption* option, const QSize& contentsSize, const QWidget* widget) const
{
  QSize size = this->Superclass::sizeFromContents(type, option, contentsSize, widget);
  const QByteArray role = widgetRole(widget);
  const bool comfortable = (qSlicerStyle::density() == qSlicerStyle::Comfortable);
  switch (type)
  {
    case QStyle::CT_PushButton:
      if (widget && widget->inherits("ctkCollapsibleButton"))
      {
        if (isCardCollapsible(widget))
        {
          size.setHeight(CardHeaderHeight);
        }
        else if (comfortable)
        {
          size.setHeight(qMax(size.height(), 28));
        }
        break;
      }
      // Sizes of the roles (as the scoped stylesheets of Slicer Studio used to set them)
      if (role == "primary")
      {
        size.setHeight(qMax(size.height(), 32));
      }
      else if (role == "secondary")
      {
        size.setHeight(qMax(size.height(), 32));
      }
      if (comfortable)
      {
        size.setHeight(qMax(size.height(), 26));
      }
      break;
    case QStyle::CT_LineEdit:
      // The line edit of a spin box or of an editable combo box is sized by its parent
      if (widget && (qobject_cast<const QAbstractSpinBox*>(widget->parentWidget()) || qobject_cast<const QComboBox*>(widget->parentWidget())))
      {
        break;
      }
      Q_FALLTHROUGH();
    case QStyle::CT_ComboBox:
    case QStyle::CT_SpinBox:
      if (comfortable)
      {
        size.setHeight(qMax(size.height(), 26));
      }
      break;
    case QStyle::CT_ToolButton:
      if (role == "compact")
      {
        // No padding around the icon (QToolButton { padding: 0px; })
        size = contentsSize + QSize(5, 5);
      }
      else if (role == "toolTile")
      {
        // 60 px tall tiles, five in a row of the 340 px inspector
        size = size.expandedTo(QSize(56, 60));
      }
      else if (isSegment(widget))
      {
        // Horizontal padding of the segments
        size.setWidth(size.width() + 16);
      }
      break;
    case QStyle::CT_MenuItem:
      if (comfortable)
      {
        const QStyleOptionMenuItem* menuItem = qstyleoption_cast<const QStyleOptionMenuItem*>(option);
        if (menuItem && menuItem->menuItemType == QStyleOptionMenuItem::Separator)
        {
          size.setHeight(qMax(size.height(), 9));
        }
        else
        {
          size.setHeight(qMax(size.height(), 26));
        }
      }
      break;
    default: break;
  }
  return size;
}

//------------------------------------------------------------------------------
QRect qSlicerStyle::itemTextRect(const QFontMetrics& metrics, const QRect& rect, int alignment, bool enabled, const QString& text) const
{
  QRect textRect = this->Superclass::itemTextRect(metrics, rect, alignment, enabled, text);
  // Disabled text is not etched (SH_EtchDisabledText), but the Compact density keeps the size of
  // the base style, which reserves one more pixel for the etched text.
  if (!enabled && !text.isEmpty() && qSlicerStyle::density() == qSlicerStyle::Compact //
      && this->baseStyle()->styleHint(QStyle::SH_EtchDisabledText) && !this->styleHint(QStyle::SH_EtchDisabledText, nullptr, nullptr, nullptr))
  {
    textRect.setWidth(textRect.width() + 1);
    textRect.setHeight(textRect.height() + 1);
  }
  return textRect;
}

//------------------------------------------------------------------------------
void qSlicerStyle::polish(QWidget* widget)
{
  this->Superclass::polish(widget);
  if (!widget)
  {
    return;
  }
  if (qobject_cast<QAbstractButton*>(widget))
  {
    widget->setAttribute(Qt::WA_Hover, true);
    if (qSlicerStyle::density() == qSlicerStyle::Comfortable && widget->inherits("ctkCollapsibleButton"))
    {
      // Collapsed sections stack tightly, unless the application chose another collapsed height
      const QVariant collapsedHeight = widget->property("collapsedHeight");
      if (collapsedHeight.isValid() && collapsedHeight.toInt() == CTKDefaultCollapsedHeight)
      {
        widget->setProperty("collapsedHeight", ComfortableCollapsedHeight);
      }
    }
  }
  if (qSlicerStyle::density() == qSlicerStyle::Comfortable && qobject_cast<QSlider*>(widget) && !widget->inherits("ctkRangeSlider"))
  {
    // ctkRangeSlider handles the presses on its handles itself
    widget->installEventFilter(sliderHandlePressFilter());
  }
  if (hasRoundedMask(widget))
  {
    widget->installEventFilter(this);
    if (qSlicerStyle::density() == qSlicerStyle::Comfortable && widget->inherits("QTipLabel"))
    {
      // Tool tip padding of 6 x 8 pixels (the margin is PM_ToolTipLabelFrameWidth + 1)
      widget->setContentsMargins(2, 0, 2, 0);
    }
  }

  const QByteArray role = widgetRole(widget);
  if (role.isEmpty())
  {
    return;
  }
  // Role fonts, unless the application set a font on the widget
  if (!widget->testAttribute(Qt::WA_SetFont))
  {
    if (role == "sectionHeader")
    {
      // Section labels: 12 px (at a 13 px body), SemiBold, sentence case
      QFont font = scaledFont(widget->font(), 0.92);
      font.setWeight(QFont::DemiBold);
      widget->setFont(font);
    }
    else if (role == "overline")
    {
      // The rare all-caps labels ("CONTINUE", the group headers of Search): 11 px, SemiBold, +4% letter spacing
      QFont font = scaledFont(widget->font(), 0.85);
      font.setWeight(QFont::DemiBold);
      font.setCapitalization(QFont::AllUppercase);
      font.setLetterSpacing(QFont::PercentageSpacing, 104.);
      widget->setFont(font);
    }
    else if (role == "secondaryText" || role == "caption" || role == "chip")
    {
      widget->setFont(scaledFont(widget->font(), 0.92));
    }
    else if (role == "kbd" || role == "toolTile")
    {
      widget->setFont(scaledFont(widget->font(), 0.85));
    }
  }
  // Horizontal padding of the label roles
  if (QLabel* label = qobject_cast<QLabel*>(widget))
  {
    if (role == "chip")
    {
      label->setContentsMargins(8, 0, 8, 0);
    }
    else if (role == "kbd")
    {
      label->setContentsMargins(5, 0, 5, 0);
    }
  }
}

//------------------------------------------------------------------------------
void qSlicerStyle::unpolish(QWidget* widget)
{
  if (qobject_cast<QSlider*>(widget))
  {
    widget->removeEventFilter(sliderHandlePressFilter());
  }
  if (widget && hasRoundedMask(widget))
  {
    widget->removeEventFilter(this);
    widget->clearMask();
  }
  this->Superclass::unpolish(widget);
}

//------------------------------------------------------------------------------
void qSlicerStyle::polish(QApplication* application)
{
  this->Superclass::polish(application);
#if QT_VERSION >= QT_VERSION_CHECK(6, 5, 0)
  // Follow the color scheme of the operating system while the "System" theme is selected.
  // Only the base "Slicer" style follows the theme ("Dark Slicer" and "Light Slicer" have their own palette).
  if (application && this->metaObject() == &qSlicerStyle::staticMetaObject && !this->property("_slicerFollowsColorScheme").toBool())
  {
    this->setProperty("_slicerFollowsColorScheme", true);
    QObject::connect(QGuiApplication::styleHints(),
                     &QStyleHints::colorSchemeChanged,
                     this,
                     [this]()
                     {
                       if (qSlicerStyle::theme() != qSlicerStyle::SystemTheme || useClassicAppearance() || QApplication::style() != this)
                       {
                         return;
                       }
                       QApplication::setPalette(this->standardPalette());
                     });
  }
#endif
}

//------------------------------------------------------------------------------
bool qSlicerStyle::eventFilter(QObject* obj, QEvent* event)
{
  QWidget* widget = qobject_cast<QWidget*>(obj);
  if (!widget)
  {
    return this->Superclass::eventFilter(obj, event);
  }
  switch (event->type())
  {
    case QEvent::Resize:
    case QEvent::Show:
      // Rounded corners of menus and tool tips (the filter is installed on them by polish())
      if (widget->isWindow() && hasRoundedMask(widget))
      {
        updateRoundedMask(widget);
      }
      break;
    case QEvent::Wheel:
      if (qobject_cast<QAbstractScrollArea*>(widget) || //
          qobject_cast<QScrollBar*>(widget) ||          //
          qobject_cast<QAbstractScrollArea*>(widget->parentWidget()))
      {
        break;
      }
      for (QWidget* ancestor = widget->parentWidget(); ancestor; ancestor = ancestor->parentWidget())
      {
        if (QAbstractScrollArea* scrollArea = qobject_cast<QAbstractScrollArea*>(ancestor))
        {
          if (scrollArea->verticalScrollBar()->minimum() != scrollArea->verticalScrollBar()->maximum())
          {
            event->ignore();
            return true;
          }
        }
      }
      break;
    default: break;
  }
  return this->Superclass::eventFilter(obj, event);
}

//------------------------------------------------------------------------------
QColor qSlicerStyle::tokenColor(const QString& name, bool dark)
{
  QColor accent = effectiveAccentColor();
  if (accent.isValid() && name.startsWith(QLatin1String("accent")))
  {
    return derivedAccentToken(name, accent, dark);
  }
  for (const StyleToken& token : StyleTokens)
  {
    if (name != QLatin1String(token.Name))
    {
      continue;
    }
    const char* value = dark ? token.Dark : token.Light;
    if (value)
    {
      return QColor(value);
    }
    // Derived from the default accent color of the theme
    return derivedAccentToken(name, QColor("#2f6fe0"), dark);
  }
  return QColor();
}

//------------------------------------------------------------------------------
QColor qSlicerStyle::token(const QString& name) const
{
  return qSlicerStyle::tokenColor(name, this->isDarkPalette());
}

//------------------------------------------------------------------------------
bool qSlicerStyle::isDarkPalette() const
{
  return QApplication::palette().color(QPalette::Window).lightness() < 128;
}

//------------------------------------------------------------------------------
void qSlicerStyle::setDensity(Density density)
{
  StyleDensity = density;
}

//------------------------------------------------------------------------------
qSlicerStyle::Density qSlicerStyle::density()
{
  // Compact metrics are always used for testing.
  if (isTestingEnabled())
  {
    return qSlicerStyle::Compact;
  }
  return StyleDensity;
}

//------------------------------------------------------------------------------
void qSlicerStyle::setTheme(Theme theme)
{
  StyleTheme = theme;
}

//------------------------------------------------------------------------------
qSlicerStyle::Theme qSlicerStyle::theme()
{
  return StyleTheme;
}

//------------------------------------------------------------------------------
void qSlicerStyle::setPlatformPalette(const QPalette& palette)
{
  PlatformWindowLightness = palette.color(QPalette::Window).lightness();
}

//------------------------------------------------------------------------------
qSlicerStyle::Theme qSlicerStyle::systemTheme()
{
  int colorScheme = 0;
#if QT_VERSION >= QT_VERSION_CHECK(6, 5, 0)
  // macOS, Windows and desktops that report their color scheme
  if (qobject_cast<QGuiApplication*>(QCoreApplication::instance()))
  {
    colorScheme = static_cast<int>(QGuiApplication::styleHints()->colorScheme());
  }
#endif
  int appsUseLightTheme = -1;
#ifdef Q_OS_WIN
  QSettings settings("HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize", QSettings::NativeFormat);
  const QVariant appsUseLightThemeValue = settings.value("AppsUseLightTheme");
  if (appsUseLightThemeValue.isValid())
  {
    appsUseLightTheme = appsUseLightThemeValue.toInt() == 0 ? 0 : 1;
  }
#endif
  return resolveSystemThemeFromHints(colorScheme, appsUseLightTheme, PlatformWindowLightness);
}

//------------------------------------------------------------------------------
int qSlicerStyle::resolveSystemTheme(int colorScheme, int appsUseLightTheme, int platformWindowLightness) const
{
  return resolveSystemThemeFromHints(colorScheme, appsUseLightTheme, platformWindowLightness);
}

//------------------------------------------------------------------------------
void qSlicerStyle::setAccentColor(const QColor& color)
{
  StyleAccentColor = color.isValid() ? color.toRgb() : QColor();
}

//------------------------------------------------------------------------------
QColor qSlicerStyle::accentColor()
{
  return StyleAccentColor;
}

//------------------------------------------------------------------------------
QString qSlicerStyle::themeToString(Theme theme)
{
  switch (theme)
  {
    case qSlicerStyle::DarkTheme: return QStringLiteral("Dark");
    case qSlicerStyle::LightTheme: return QStringLiteral("Light");
    case qSlicerStyle::SystemTheme:
    default: return QStringLiteral("System");
  }
}

//------------------------------------------------------------------------------
qSlicerStyle::Theme qSlicerStyle::themeFromString(const QString& name, Theme defaultTheme)
{
  for (Theme theme : { qSlicerStyle::SystemTheme, qSlicerStyle::DarkTheme, qSlicerStyle::LightTheme })
  {
    if (name.compare(qSlicerStyle::themeToString(theme), Qt::CaseInsensitive) == 0)
    {
      return theme;
    }
  }
  return defaultTheme;
}

//------------------------------------------------------------------------------
QString qSlicerStyle::densityToString(Density density)
{
  return density == qSlicerStyle::Comfortable ? QStringLiteral("Comfortable") : QStringLiteral("Compact");
}

//------------------------------------------------------------------------------
qSlicerStyle::Density qSlicerStyle::densityFromString(const QString& name, Density defaultDensity)
{
  for (Density density : { qSlicerStyle::Compact, qSlicerStyle::Comfortable })
  {
    if (name.compare(qSlicerStyle::densityToString(density), Qt::CaseInsensitive) == 0)
    {
      return density;
    }
  }
  return defaultDensity;
}
