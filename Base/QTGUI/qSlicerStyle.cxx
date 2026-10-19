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
#include <QComboBox>
#include <QStyleFactory>
#include <QDebug>
#include <QEvent>
#include <QGroupBox>
#include <QLabel>
#include <QMenu>
#include <QPainter>
#include <QPainterPath>
#include <qdrawutil.h>
#include <QScrollBar>
#include <QSettings>
#include <QStyleHints>
#include <QStyleOptionComboBox>
#include <QStyleOptionGroupBox>
#include <QStyleOptionSpinBox>
#include <QToolButton>
#include <QtMath>

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
  { "canvas", "#141416", "#e4e4e8" },          //
  { "card", "#232327", "#ffffff" },            //
  { "control-hover", "#35353b", "#eeeef2" },   //
  { "control-pressed", "#26262a", "#e4e4e9" }, //
  { "hairline", "#2e2e34", "#e1e1e6" },        //
  { "border", "#3a3a41", "#cacad1" },          //
  { "control-border", "#71717a", "#7a7a84" },  // non-text outlines (fields, check boxes): 3:1 on the window
  { "text2", "#a1a1aa", "#5c5c66" },           //
  { "text3", "#94949c", "#63636c" },           // informative secondary text: 4.5:1 on the window
  { "text-disabled", "#71717a", "#8e8e98" },   // disabled glyphs
  { "accent", "#3568d4", "#2f6fde" },          //
  { "accent-hover", "#3f74e3", nullptr },      //
  { "accent-pressed", "#2c5bbf", nullptr },    //
  { "accent-fg", "#6ea3ff", "#2158bd" },       // icons, slider fill, focus ring, links
  { "accent-subtle", "#22324f", "#e3ecfc" },   // checked tiles
  { "success", "#3fb950", "#1a7f37" },         //
  { "warning", "#e3b341", "#9a6700" },         //
  { "danger", "#f85149", "#cf222e" },          //
  { "view-bar", "#161618", "#e9e9ed" },        //
  { "lesion", "#f2545b", "#f2545b" },          //
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
    return readableColor(foreground, QColor(dark ? "#1c1c1f" : "#f4f4f6"), 4.5);
  }
  if (name == QLatin1String("accent-subtle"))
  {
    return dark ? mixColors(QColor("#1c1c1f"), accent, 0.26) : mixColors(Qt::white, accent, 0.13);
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
/// Return true if the palette is dark (window color lightness < 128)
bool isDark(const QPalette& palette)
{
  return palette.color(QPalette::Window).lightness() < 128;
}

//------------------------------------------------------------------------------
/// Return true if the option is drawn with a dark palette (window color lightness < 128)
bool hasDarkPalette(const QStyleOption* option)
{
  return option && isDark(option->palette);
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
  qreal radius = 6.;
  switch (kind)
  {
    case PrimaryButton: fill = !enabled ? token("border") : pressed ? token("accent-pressed") : hover ? token("accent-hover") : token("accent"); break;
    case GhostButton:
    case AutoRaiseButton:
    case CompactButton:
    case SegmentButton:
      // Transparent until hovered
      fill = pressed ? token("control-pressed") : hover ? token("control-hover") : QColor(Qt::transparent);
      radius = (kind == CompactButton) ? 4. : (kind == SegmentButton) ? 5. : 6.;
      break;
    case ToolButton:
    case ToolTileButton:
      fill = !enabled  ? option->palette.color(QPalette::Disabled, QPalette::Button)
             : pressed ? token("control-pressed")
             : hover   ? token("control-hover")
                       : option->palette.color(QPalette::Button);
      border = (hover && kind == ToolButton) ? token("control-border") : token("border");
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
  if (checked && kind != PrimaryButton)
  {
    fill = token("accent-subtle");
    if (enabled && (hover || pressed))
    {
      fill = shiftLightness(fill, (dark ? 0.04 : -0.03) * (pressed ? -1. : 1.));
    }
    // Segments are checked on their track without border
    border = (kind == SegmentButton) ? QColor() : enabled ? token("accent-fg") : token("border");
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
/// Draw the frame of an input field (line edit, spin box, editable combo box): a 1 px
/// "control-border" line ("accent-fg" when focused, "border" when disabled), which has a contrast
/// of at least 3:1 with the window color.
/// The base style (Fusion) frames fields with a color derived from the window color, which is
/// almost invisible on dark palettes.
/// \a rect is the rectangle framed by the base style, the frame has the same geometry to cover it.
void drawFieldFrame(const QRect& rect, QStyle::State state, bool dark, QPainter* painter)
{
  QColor frameColor = qSlicerStyle::tokenColor("control-border", dark);
  if (!(state & QStyle::State_Enabled))
  {
    frameColor = qSlicerStyle::tokenColor("border", dark);
  }
  else if (state & QStyle::State_HasFocus)
  {
    frameColor = qSlicerStyle::tokenColor("accent-fg", dark);
  }
  painter->save();
  painter->setRenderHint(QPainter::Antialiasing, true);
  // Line on pixel centers, like the base style
  painter->translate(0.5, 0.5);
  painter->setPen(frameColor);
  painter->setBrush(Qt::NoBrush);
  painter->drawRoundedRect(QRectF(rect.adjusted(0, 0, -1, -1)), 2., 2.);
  painter->restore();
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
  const qreal radius = qobject_cast<const QMenu*>(widget) ? 6. : 0.;
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
/// Draw the flat header of a ctkCollapsibleButton: a hover or pressed fill and a hairline below
/// the header when the section is expanded. The chevron and the text are drawn by the button.
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
/// Draw the background of a frame or label with a slicerRole: rounded card, chip pill, kbd key
/// and segmented control track. Return false if the widget has no such role.
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
    drawRoundedBox(painter, rect, 8., token("card"), token("hairline"));
    return true;
  }
  if (role == "segmented")
  {
    drawRoundedBox(painter, rect, 7., token("control-pressed"), token("hairline"));
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
    drawRoundedBox(painter, keyRect, 4., QColor(), token("border"));
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
  painter.drawRoundedRect(QRect(QPoint(0, 0), widget->size()), 6, 6);
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
      return 12; // default to 27
      break;
    case QStyle::PM_ButtonIconSize:
      return 24; // Like with cleanlooks style
      break;
    case QStyle::PM_IndicatorWidth:
    case QStyle::PM_IndicatorHeight:
    case QStyle::PM_ExclusiveIndicatorWidth:
    case QStyle::PM_ExclusiveIndicatorHeight:
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
  palette.setColor(QPalette::Window, "#f4f4f6");
  palette.setColor(QPalette::WindowText, "#1c1c1f");
  palette.setColor(QPalette::Disabled, QPalette::WindowText, "#a3a3ab");
  palette.setColor(QPalette::Base, Qt::white);
  palette.setColor(QPalette::AlternateBase, "#f7f7f9");
  palette.setColor(QPalette::ToolTipBase, "#1c1c1f");
  palette.setColor(QPalette::ToolTipText, "#f4f4f6");
  palette.setColor(QPalette::Text, "#1c1c1f");
  palette.setColor(QPalette::Disabled, QPalette::Text, "#a3a3ab");
  palette.setColor(QPalette::PlaceholderText, "#63636c"); // text3
  palette.setColor(QPalette::Disabled, QPalette::PlaceholderText, "#a3a3ab");
  palette.setColor(QPalette::Button, Qt::white);
  palette.setColor(QPalette::Disabled, QPalette::Button, "#f0f0f3");
  palette.setColor(QPalette::ButtonText, "#1c1c1f");
  palette.setColor(QPalette::Disabled, QPalette::ButtonText, "#a3a3ab");
  palette.setColor(QPalette::BrightText, "#cf222e");
  // Color roles used mostly for 3D bevel and shadow effects.
  palette.setColor(QPalette::Light, Qt::white);    // Lighter than Button color.
  palette.setColor(QPalette::Midlight, "#ededf1"); // Between Button and Light.
  palette.setColor(QPalette::Mid, "#d4d4da");      // Between Button and Dark.
  palette.setColor(QPalette::Dark, "#a1a1aa");     // Darker than Button.
  palette.setColor(QPalette::Shadow, "#5c5c66");   // A very dark color.
  // Color roles relate to selected (marked) items
  palette.setColor(QPalette::Highlight, "#2f6fde");
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
  // Central color roles
  palette.setColor(QPalette::Window, "#1c1c1f");
  palette.setColor(QPalette::WindowText, "#e8e8ec");
  palette.setColor(QPalette::Disabled, QPalette::WindowText, "#6b6b73");
  palette.setColor(QPalette::Base, "#18181b");
  palette.setColor(QPalette::Disabled, QPalette::Base, "#1d1d20");
  palette.setColor(QPalette::AlternateBase, "#1e1e22");
  palette.setColor(QPalette::ToolTipBase, "#0b0b0d");
  palette.setColor(QPalette::ToolTipText, "#e8e8ec");
  palette.setColor(QPalette::Text, "#e8e8ec");
  palette.setColor(QPalette::Disabled, QPalette::Text, "#6b6b73");
  palette.setColor(QPalette::PlaceholderText, "#94949c"); // text3
  palette.setColor(QPalette::Disabled, QPalette::PlaceholderText, "#55555c");
  palette.setColor(QPalette::Button, "#2c2c31");
  palette.setColor(QPalette::Disabled, QPalette::Button, "#26262a");
  palette.setColor(QPalette::ButtonText, "#e8e8ec");
  palette.setColor(QPalette::Disabled, QPalette::ButtonText, "#6b6b73");
  palette.setColor(QPalette::BrightText, "#ff6b6b"); // Lighter than Qt::red
  // Color roles used mostly for 3D bevel and shadow effects.
  palette.setColor(QPalette::Light, "#4a4a52");    // Lighter than Button color.
  palette.setColor(QPalette::Midlight, "#3a3a41"); // Between Button and Light.
  palette.setColor(QPalette::Mid, "#2a2a2f");      // Between Button and Dark.
  palette.setColor(QPalette::Dark, "#121214");     // Darker than Button.
  palette.setColor(QPalette::Shadow, Qt::black);   // A very dark color.
  // Color roles relate to selected (marked) items
  palette.setColor(QPalette::Highlight, "#3568d4");
  palette.setColor(QPalette::Disabled, QPalette::Highlight, "#3a3a41");
  palette.setColor(QPalette::HighlightedText, Qt::white);
  palette.setColor(QPalette::Disabled, QPalette::HighlightedText, "#a1a1aa");
  // Color roles related to hyperlinks
  palette.setColor(QPalette::Link, "#6ea3ff");
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
    case QStyle::PE_IndicatorArrowRight: drawChevron(element, QRectF(option->rect), arrowColor(option), painter); return;
    case QStyle::PE_IndicatorCheckBox:
    case QStyle::PE_IndicatorRadioButton:
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
      drawRoundedBox(painter, QRectF(option->rect), 6., background, qSlicerStyle::tokenColor("border", true));
      return;
    }
    case QStyle::PE_FrameLineEdit:
      // Line edits (PE_PanelLineEdit fills them with the base color) and editable combo boxes
      drawFieldFrame(option->rect, option->state, hasDarkPalette(option), painter);
      return;
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
      if (widgetRole(widget) == "card")
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
        break;
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
        && (role == "ghost" || role == "toolTile" || isSegment(widget)))
    {
      // These buttons show their menu without menu indicator
      QStyleOptionToolButton withoutIndicator(*toolButton);
      withoutIndicator.features &= ~QStyleOptionToolButton::HasMenu;
      this->Superclass::drawComplexControl(control, &withoutIndicator, painter, widget);
      return;
    }
  }
  this->Superclass::drawComplexControl(control, option, painter, widget);
  if (!painter || !option)
  {
    return;
  }
  // The base style (Fusion) draws the frame of spin boxes itself, and draws the arrow button of
  // editable combo boxes over their PE_FrameLineEdit frame: draw the frame of fields over them.
  const bool dark = hasDarkPalette(option);
  switch (control)
  {
    case QStyle::CC_SpinBox:
      if (const QStyleOptionSpinBox* spinBox = qstyleoption_cast<const QStyleOptionSpinBox*>(option))
      {
        if (spinBox->frame)
        {
          // The base style leaves one pixel above and below the frame
          drawFieldFrame(spinBox->rect.adjusted(0, 1, 0, -1), spinBox->state, dark, painter);
        }
      }
      break;
    case QStyle::CC_ComboBox:
      if (const QStyleOptionComboBox* comboBox = qstyleoption_cast<const QStyleOptionComboBox*>(option))
      {
        // Non-editable combo boxes are drawn as buttons
        if (comboBox->editable && comboBox->frame)
        {
          drawFieldFrame(comboBox->rect, comboBox->state, dark, painter);
        }
      }
      break;
    default: break;
  }
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
  const bool dark = isDark(pal);
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
  else if (role == "sectionHeader" || role == "secondaryText" || role == "kbd")
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
        if (comfortable)
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
        size.setHeight(qMax(size.height(), 28));
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
        size = size.expandedTo(QSize(70, 60));
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
      QFont font = scaledFont(widget->font(), 0.85);
      font.setWeight(QFont::DemiBold);
      font.setCapitalization(QFont::AllUppercase);
      font.setLetterSpacing(QFont::AbsoluteSpacing, 0.6);
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
    return derivedAccentToken(name, QColor(dark ? "#3568d4" : "#2f6fde"), dark);
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
