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
#include <QAbstractScrollArea>
#include <QApplication>
#include <QStyleFactory>
#include <QDebug>
#include <QEvent>
#include <QGroupBox>
#include <QScrollBar>
#include <QSettings>
#include <QStyleOptionGroupBox>

// Slicer includes
#include "qSlicerCoreApplication.h"

// qMRML includes
#include "qSlicerStyle.h"

namespace
{
// Settings shared by all Slicer styles (the style object is replaced when the style is changed).
qSlicerStyle::Density StyleDensity = qSlicerStyle::Compact;
qSlicerStyle::Theme StyleTheme = qSlicerStyle::SystemTheme;
QColor StyleAccentColor;

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
  { "text2", "#a1a1aa", "#5c5c66" },           //
  { "text3", "#71717a", "#8e8e98" },           //
  { "accent", "#3568d4", "#2f6fde" },          //
  { "accent-hover", "#3f74e3", nullptr },      //
  { "accent-pressed", "#2c5bbf", nullptr },    //
  { "accent-fg", "#6ea3ff", "#2f6fde" },       // icons, slider fill, focus ring, links
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
/// Accent color override in effect (invalid if the default accent colors must be used)
QColor effectiveAccentColor()
{
  if (!StyleAccentColor.isValid() || isTestingEnabled())
  {
    return QColor();
  }
  return StyleAccentColor;
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
    // Keep the foreground accent readable on the window color
    QColor hsl = accent.toHsl();
    qreal lightness = dark ? qMax<qreal>(hsl.lightnessF(), 0.70) : qMin<qreal>(hsl.lightnessF(), 0.53);
    return QColor::fromHslF(hsl.hslHueF(), hsl.hslSaturationF(), lightness).toRgb();
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
  // Dark text on light accent colors
  const qreal luminance = 0.299 * accent.redF() + 0.587 * accent.greenF() + 0.114 * accent.blueF();
  const QColor highlightedText = luminance > 0.6 ? QColor("#1c1c1f") : QColor(Qt::white);
  for (QPalette::ColorGroup group : { QPalette::Active, QPalette::Inactive })
  {
    palette.setColor(group, QPalette::Highlight, accent);
    palette.setColor(group, QPalette::HighlightedText, highlightedText);
  }
  palette.setColor(QPalette::Link, qSlicerStyle::tokenColor("accent-fg", dark));
}
} // namespace

// --------------------------------------------------------------------------
// qSlicerStyle methods

// --------------------------------------------------------------------------
qSlicerStyle::qSlicerStyle()
  : Superclass(QStyleFactory::create("fusion"))
{
  this->baseStyle()->setParent(this);
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
    default: return Superclass::pixelMetric(metric, option, widget); break;
  }
}

//------------------------------------------------------------------------------
QPalette qSlicerStyle::standardPalette() const
{
  bool dark = false;
  // The light palette is always used for testing.
  if (!isTestingEnabled())
  {
    switch (qSlicerStyle::theme())
    {
      case qSlicerStyle::DarkTheme: dark = true; break;
      case qSlicerStyle::LightTheme: dark = false; break;
      case qSlicerStyle::SystemTheme:
      default:
      {
#ifdef Q_OS_WIN
        // Qt on macOS already dynamically picks light/dark theme based on the OS setting
        QSettings settings("HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize", QSettings::NativeFormat);
        if (settings.value("AppsUseLightTheme") == 0)
        {
          dark = true;
        }
#endif
        break;
      }
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
  palette.setColor(QPalette::PlaceholderText, "#8e8e98");
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
  palette.setColor(QPalette::Link, "#2f6fde");
  palette.setColor(QPalette::LinkVisited, "#7c3aed");

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
  palette.setColor(QPalette::PlaceholderText, "#71717a");
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
    default: res = this->Superclass::styleHint(hint, opt, widget, returnData);
  }
  return res;
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
