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
#include <QEvent>
#include <QHBoxLayout>
#include <QKeyEvent>
#include <QLineEdit>
#include <QLinearGradient>
#include <QMetaObject>
#include <QPainter>
#include <QPainterPath>
#include <QStyle>
#include <QVBoxLayout>
#include <QLabel>
#include <QToolButton>

// CTK includes
#include <ctkPopupWidget.h>

// VTK includes
#include <vtkCommand.h>

// qMRML includes
#include "qMRMLSymbolicIconEngine.h"
#include "qMRMLViewControllerBar_p.h"

// MRML includes
#include "vtkMRMLLayoutNode.h"
#include "vtkMRMLAbstractViewNode.h"
#include "vtkMRMLScene.h"

namespace
{
/// Presentation of the bars created from now on (qMRMLViewControllerBar::setDefaultFlatStyle)
bool DefaultFlatStyle = false;
/// Height of the flat bar strip, and size of its tool buttons and icons
const int FlatBarHeight = 34;
const int FlatButtonSize = 22;
const int FlatIconSize = 14;
/// The flat bar is drawn as a pill (radius FlatPillRadius) inset in its strip by these margins
const int FlatPillHorizontalInset = 6;
const int FlatPillVerticalInset = 2;
const qreal FlatPillRadius = 8.;
/// Size of the view color dot of the flat bar
const int FlatViewDotSize = 8;

//---------------------------------------------------------------------------
/// Application event filter of an open flat popup: Esc closes the popup (qMRMLViewControllerBarPrivate::closePopupOnEscape)
class qMRMLViewControllerBarPopupEscapeFilter : public QObject
{
public:
  qMRMLViewControllerBarPopupEscapeFilter(qMRMLViewControllerBarPrivate* bar)
    : QObject(bar)
    , Bar(bar)
  {
  }
  bool eventFilter(QObject* object, QEvent* event) override
  {
    if (this->Bar->closePopupOnEscape(object, event))
    {
      return true;
    }
    return this->QObject::eventFilter(object, event);
  }

private:
  qMRMLViewControllerBarPrivate* Bar;
};
} // namespace

//--------------------------------------------------------------------------
// qMRMLViewControllerBarPrivate methods

//---------------------------------------------------------------------------
qMRMLViewControllerBarPrivate::qMRMLViewControllerBarPrivate(qMRMLViewControllerBar& object)
  : QObject(nullptr)
  , q_ptr(&object)
{
}

//---------------------------------------------------------------------------
qMRMLViewControllerBarPrivate::~qMRMLViewControllerBarPrivate()
{
  this->setPopupEscapeFilterInstalled(false);
  delete this->PopupWidget;
  this->PopupWidget = nullptr;
  delete this->BarLayout;
  this->BarLayout = nullptr;
  delete this->BarWidget;
  this->BarWidget = nullptr;
  delete this->ControllerLayout;
  this->ControllerLayout = nullptr;
}

//---------------------------------------------------------------------------
void qMRMLViewControllerBarPrivate::init()
{
  Q_Q(qMRMLViewControllerBar);
  // Widget contains
  // VBoxLayout (ControllerLayout)
  //    Widget (BarWidget)
  //        HBoxLayout (BarLayout)
  //    Optional PopupWidget (packed under BarWidget if ControllerBar is a panel)

  this->setParent(q);

  this->BarWidget = new QWidget(q);
  this->BarWidget->setObjectName("BarWidget");
  this->BarWidget->setAutoFillBackground(true); // color the bar

  this->ControllerLayout = new QVBoxLayout(q);
  this->ControllerLayout->setSpacing(2);
  QMargins cmargins = this->ControllerLayout->contentsMargins();
  cmargins.setTop(0);
  cmargins.setBottom(0);
  cmargins.setLeft(0);
  cmargins.setRight(0);
  this->ControllerLayout->setContentsMargins(cmargins);

  this->BarLayout = new QHBoxLayout();
  this->BarLayout->setSpacing(2);
  QMargins margins = this->BarLayout->contentsMargins();
  margins.setTop(0);
  margins.setBottom(0);
  this->BarLayout->setContentsMargins(margins);

  this->PopupWidget = new ctkPopupWidget(q);
  this->setupPopupUi();

  this->PinButton = new QToolButton(q);
  this->PinButton->setObjectName("PinButton");
  this->PinButton->setCheckable(true);
  this->PinButton->setAutoRaise(true);
  QIcon pushPinIcon;
  pushPinIcon.addFile(":/Icons/PushPinIn.png", QSize(), QIcon::Normal, QIcon::On);
  pushPinIcon.addFile(":/Icons/PushPinOut.png", QSize(), QIcon::Normal, QIcon::Off);
  this->PinButton->setIcon(pushPinIcon);
  QObject::connect(this->PinButton, SIGNAL(toggled(bool)), this->PopupWidget, SLOT(pinPopup(bool)));
  this->PinButton->installEventFilter(this);
  this->BarLayout->addWidget(this->PinButton);

  this->ViewLabel = new QLabel(q);
  this->ViewLabel->setObjectName("ViewLabel");
  this->ViewLabel->setAlignment(Qt::AlignHCenter | Qt::AlignCenter);
  // Slice controller background color is independent from the color palette, therefore the color of text and controls are hardcoded to black
  this->ViewLabel->setStyleSheet("color: black; background-color: transparent;");
  this->ViewLabel->setMinimumWidth(this->ViewLabel->fontMetrics().horizontalAdvance("XX"));
  this->BarLayout->addWidget(this->ViewLabel);

  this->ViewMaximizeIcon = QIcon(":Icons/ViewMaximize.png");
  this->ViewRestoreIcon = QIcon(":Icons/ViewRestore.png");

  this->MaximizeViewButton = new QToolButton(q);
  this->MaximizeViewButton->setObjectName("MaximizeViewButton");
  this->MaximizeViewButton->setToolTip(tr("Maximize/restore view"));
  this->MaximizeViewButton->setAutoRaise(true);
  this->MaximizeViewButton->setIcon(this->ViewMaximizeIcon);
  this->BarLayout->addWidget(this->MaximizeViewButton);
  QObject::connect(this->MaximizeViewButton, SIGNAL(clicked()), q, SLOT(maximizeView()));

  // Flat presentation: the popup opens on click (it is hidden in the classic presentation, where it takes no space)
  this->FlatMaximizeIcon = qMRMLSymbolicIconEngine::icon("maximize-2");
  this->FlatRestoreIcon = qMRMLSymbolicIconEngine::icon("minimize-2");
  this->OptionsButton = this->createFlatToolButton("OptionsButton");
  this->OptionsButton->setIcon(qMRMLSymbolicIconEngine::icon("sliders-horizontal"));
  this->OptionsButton->setToolTip(
    qMRMLViewControllerBarPrivate::flatToolTip(qMRMLViewControllerBar::tr("Options"), QString(), qMRMLViewControllerBar::tr("Layers, opacity, interpolation")));
  this->BarLayout->addWidget(this->OptionsButton);
  QObject::connect(this->OptionsButton,
                   &QToolButton::clicked,
                   this,
                   [this]()
                   {
                     if (this->PopupWidget->isVisible())
                     {
                       this->PopupEscapeArmed = false;
                       this->PopupWidget->hidePopup();
                     }
                     else
                     {
                       // Esc closes the popup first (USABILITY Esc ladder, rung 0.5)
                       this->setPopupEscapeFilterInstalled(true);
                       this->PopupEscapeArmed = true;
                       this->PopupWidget->showPopup();
                     }
                   });
  QObject::connect(this->PopupWidget,
                   &ctkPopupWidget::popupOpened,
                   this,
                   [this](bool open)
                   {
                     if (open && this->AppliedFlatStyle && this->LayoutBehavior == qMRMLViewControllerBar::Popup)
                     {
                       // Also when the popup was opened from code
                       this->setPopupEscapeFilterInstalled(true);
                       this->PopupEscapeArmed = true;
                     }
                     else if (!open)
                     {
                       this->PopupEscapeArmed = false;
                       this->setPopupEscapeFilterInstalled(false);
                     }
                   });

  this->BarLayout->addSpacing(5);

  this->BarWidget->setLayout(this->BarLayout);
  this->ControllerLayout->addWidget(this->BarWidget);
  q->setLayout(this->ControllerLayout);

  this->FlatLeadingWidgets << this->PinButton << this->ViewLabel;
  this->FlatTrailingWidgets << this->OptionsButton << this->MaximizeViewButton;

  // The flat presentation is applied at the end of the initialization of the subclasses
  // (ensureFlatStyleApplied), or when the bar is polished
  this->FlatStyle = DefaultFlatStyle;
  q->installEventFilter(this);
  this->BarWidget->installEventFilter(this);
}

//---------------------------------------------------------------------------
void qMRMLViewControllerBarPrivate::setPopupEscapeFilterInstalled(bool install)
{
  if (install && !this->PopupEscapeFilter && qApp)
  {
    this->PopupEscapeFilter = new qMRMLViewControllerBarPopupEscapeFilter(this);
    qApp->installEventFilter(this->PopupEscapeFilter);
  }
  else if (!install && this->PopupEscapeFilter)
  {
    if (qApp)
    {
      qApp->removeEventFilter(this->PopupEscapeFilter);
    }
    // The filter may be running (the popup can report that it closed while an event is filtered)
    this->PopupEscapeFilter->deleteLater();
    this->PopupEscapeFilter = nullptr;
    this->PopupEscapeArmed = false;
    this->PopupEscapeHandled = false;
  }
}

//---------------------------------------------------------------------------
bool qMRMLViewControllerBarPrivate::closePopupOnEscape(QObject* object, QEvent* event)
{
  Q_Q(qMRMLViewControllerBar);
  Q_UNUSED(object);
  const QEvent::Type type = event->type();
  if (type != QEvent::ShortcutOverride && type != QEvent::KeyPress)
  {
    return false;
  }
  QKeyEvent* keyEvent = static_cast<QKeyEvent*>(event);
  if (keyEvent->key() != Qt::Key_Escape)
  {
    return false;
  }
  // Qt sends the shortcut override, then the key press (to the window, then to the focus widget)
  if (type == QEvent::ShortcutOverride)
  {
    if (this->PopupEscapeHandled && keyEvent->timestamp() == this->PopupEscapeTimestamp)
    {
      // Qt sends the shortcut override again for a key press sent by code: it is the same key press
      event->accept();
      return true;
    }
    this->PopupEscapeHandled = false;
  }
  else if (this->PopupEscapeHandled)
  {
    this->PopupEscapeHandled = false;
    return true;
  }
  if (keyEvent->modifiers() & (Qt::ShiftModifier | Qt::ControlModifier | Qt::AltModifier | Qt::MetaModifier))
  {
    return false;
  }
  if (!this->PopupEscapeArmed || !this->AppliedFlatStyle || !this->PopupWidget || !this->PopupWidget->isVisible())
  {
    return false;
  }
  // Rung 0 keeps Esc: a Qt popup or menu (for example a combo box of the popup), a modal dialog,
  // or a line edit with unsent edits
  if (QApplication::activePopupWidget() || QApplication::activeModalWidget())
  {
    return false;
  }
  QWidget* focusWidget = QApplication::focusWidget();
  QLineEdit* lineEdit = qobject_cast<QLineEdit*>(focusWidget);
  if (lineEdit && lineEdit->isModified() && !this->PopupWidget->isAncestorOf(lineEdit))
  {
    return false;
  }
  // Esc pressed in another window (for example a floating Python console) is not for this popup
  QWidget* activeWindow = QApplication::activeWindow();
  if (activeWindow && activeWindow != q->window() && activeWindow != this->PopupWidget)
  {
    return false;
  }
  this->PopupEscapeArmed = false;
  this->PopupWidget->hidePopup();
  if (type == QEvent::ShortcutOverride)
  {
    event->accept();
    this->PopupEscapeHandled = true;
    this->PopupEscapeTimestamp = keyEvent->timestamp();
  }
  return true;
}

//---------------------------------------------------------------------------
QToolButton* qMRMLViewControllerBarPrivate::createFlatToolButton(const QString& objectName)
{
  Q_Q(qMRMLViewControllerBar);
  QToolButton* button = new QToolButton(this->BarWidget ? this->BarWidget : static_cast<QWidget*>(q));
  button->setObjectName(objectName);
  button->setAutoRaise(true);
  button->setIconSize(QSize(FlatIconSize, FlatIconSize));
  button->setFixedSize(FlatButtonSize, FlatButtonSize);
  button->setVisible(false);
  this->FlatOnlyWidgets << button;
  return button;
}

//---------------------------------------------------------------------------
QString qMRMLViewControllerBarPrivate::flatToolTip(const QString& name, const QString& key, const QString& description)
{
  QStringList parts;
  for (const QString& part : { name, key, description })
  {
    if (!part.isEmpty())
    {
      parts << part;
    }
  }
  return parts.join(QString::fromUtf8(" \u00B7 "));
}

//---------------------------------------------------------------------------
void qMRMLViewControllerBarPrivate::setFlatFixedSize(QWidget* widget, const QSize& size)
{
  if (!widget)
  {
    return;
  }
  if (!this->ClassicSizes.contains(widget))
  {
    this->ClassicSizes.insert(widget, ClassicSize{ widget->minimumSize(), widget->maximumSize() });
  }
  widget->setFixedSize(size);
}

//---------------------------------------------------------------------------
void qMRMLViewControllerBarPrivate::restoreClassicSize(QWidget* widget)
{
  if (!widget || !this->ClassicSizes.contains(widget))
  {
    return;
  }
  const ClassicSize& classicSize = this->ClassicSizes[widget];
  widget->setMinimumSize(classicSize.Minimum);
  widget->setMaximumSize(classicSize.Maximum);
}

//---------------------------------------------------------------------------
QColor qMRMLViewControllerBarPrivate::styleToken(const QString& name) const
{
  Q_Q(const qMRMLViewControllerBar);
  QStyle* style = q->style();
  QColor color;
  if (style && style->metaObject()->indexOfMethod("token(QString)") >= 0)
  {
    QMetaObject::invokeMethod(style, "token", Qt::DirectConnection, Q_RETURN_ARG(QColor, color), Q_ARG(QString, name));
  }
  return color;
}

//---------------------------------------------------------------------------
QColor qMRMLViewControllerBarPrivate::viewBarColor() const
{
  Q_Q(const qMRMLViewControllerBar);
  QColor color = this->styleToken("view-bar");
  if (color.isValid())
  {
    return color;
  }
  // Styles without tokens: a slightly darker window color
  QColor window = q->palette().color(QPalette::Window);
  return window.lightness() < 128 ? window.darker(118) : window.darker(105);
}

//---------------------------------------------------------------------------
QColor qMRMLViewControllerBarPrivate::flatStripColor() const
{
  Q_Q(const qMRMLViewControllerBar);
  QColor color = this->styleToken("canvas");
  if (color.isValid())
  {
    return color;
  }
  QColor window = q->palette().color(QPalette::Window);
  return window.lightness() < 128 ? window.darker(140) : window.darker(108);
}

//---------------------------------------------------------------------------
void qMRMLViewControllerBarPrivate::paintFlatPill()
{
  // The pill: the "view-bar" fill with a 1 px border (white 6% on dark, black 8% on light), inset in the strip
  QPainter painter(this->BarWidget);
  painter.setRenderHint(QPainter::Antialiasing, true);
  const bool dark = this->FlatBarFillColor.lightness() < 128;
  QColor border = dark ? QColor(255, 255, 255, 18) : QColor(0, 0, 0, 22);
  QRectF pill = QRectF(this->BarWidget->rect()).adjusted(FlatPillHorizontalInset + 0.5, FlatPillVerticalInset + 0.5, -FlatPillHorizontalInset - 0.5, -FlatPillVerticalInset - 0.5);
  QPainterPath path;
  path.addRoundedRect(pill, FlatPillRadius, FlatPillRadius);
  painter.fillPath(path, this->FlatBarFillColor);
  painter.setPen(QPen(border, 1.));
  painter.drawPath(path);
}

//---------------------------------------------------------------------------
void qMRMLViewControllerBarPrivate::ensureFlatStyleApplied()
{
  if (this->FlatStyle != this->AppliedFlatStyle)
  {
    this->updateFlatStyle();
  }
}

//---------------------------------------------------------------------------
void qMRMLViewControllerBarPrivate::updateFlatStyle()
{
  Q_Q(qMRMLViewControllerBar);
  const bool flat = this->FlatStyle;
  if (!this->ClassicBarLayoutRecorded)
  {
    this->ClassicBarMargins = this->BarLayout->contentsMargins();
    this->ClassicBarSpacing = this->BarLayout->spacing();
    this->ClassicViewLabelStyleSheet = this->ViewLabel->styleSheet();
    this->ClassicPinButtonToolTip = this->PinButton->toolTip();
    this->ClassicMaximizeIconSize = this->MaximizeViewButton->iconSize();
  }
  this->arrangeBarLayout();
  this->AppliedFlatStyle = flat;

  if (flat)
  {
    this->BarWidget->setFixedHeight(FlatBarHeight);
    // The content is inside the pill: 10 px after its left edge, 4 px before its right edge
    this->BarLayout->setContentsMargins(FlatPillHorizontalInset + 10, 0, FlatPillHorizontalInset + 4, 0);
    this->BarLayout->setSpacing(4);
    this->setFlatFixedSize(this->ViewLabel, QSize(FlatViewDotSize, FlatViewDotSize));
    this->setFlatFixedSize(this->MaximizeViewButton, QSize(FlatButtonSize, FlatButtonSize));
    this->MaximizeViewButton->setIconSize(QSize(FlatIconSize, FlatIconSize));
    this->PinButton->setToolTip(
      qMRMLViewControllerBarPrivate::flatToolTip(qMRMLViewControllerBar::tr("Pin options"), QString(), qMRMLViewControllerBar::tr("Keep the view options open")));
  }
  else
  {
    this->BarWidget->setMinimumHeight(0);
    this->BarWidget->setMaximumHeight(QWIDGETSIZE_MAX);
    this->BarLayout->setContentsMargins(this->ClassicBarMargins);
    this->BarLayout->setSpacing(this->ClassicBarSpacing);
    this->restoreClassicSize(this->ViewLabel);
    this->restoreClassicSize(this->MaximizeViewButton);
    this->MaximizeViewButton->setIconSize(this->ClassicMaximizeIconSize);
    this->ViewLabel->setStyleSheet(this->ClassicViewLabelStyleSheet);
    this->PinButton->setToolTip(this->ClassicPinButtonToolTip);
  }
  // Read by the application style: neutral slider of the flat bar (2 px track, small thumb)
  this->BarWidget->setProperty("slicerFlatViewBar", flat ? QVariant(true) : QVariant());
  for (QWidget* widget : this->FlatOnlyWidgets)
  {
    if (widget && widget != this->OptionsButton)
    {
      widget->setVisible(flat);
    }
  }
  this->updatePopupButtonsVisibility();
  if (!flat)
  {
    // Esc does not close the classic popup: it is shown on hover or pinned
    this->setPopupEscapeFilterInstalled(false);
    if (this->ClassicPinButtonChecked && this->LayoutBehavior == qMRMLViewControllerBar::Popup)
    {
      this->PinButton->setChecked(true);
    }
    this->ClassicPinButtonChecked = false;
  }

  // Colors: the flat bar uses the palette, the classic bar the view color
  QColor color = this->BarColor;
  this->BarColor = QColor();
  this->setColor(color);

  // Maximize button icon and tooltip
  q->updateWidgetFromMRMLView();

  emit this->flatStyleChanged(flat);
}

//---------------------------------------------------------------------------
void qMRMLViewControllerBarPrivate::updatePopupButtonsVisibility()
{
  const bool popup = (this->LayoutBehavior == qMRMLViewControllerBar::Popup);
  const bool flat = this->AppliedFlatStyle;
  this->PinButton->setVisible(popup && !flat);
  this->OptionsButton->setVisible(popup && flat);
  if (flat && popup && this->PinButton->isChecked())
  {
    // The pin is not available in the flat presentation (it is checked again in the classic presentation)
    this->ClassicPinButtonChecked = true;
    this->PinButton->setChecked(false);
  }
}

//---------------------------------------------------------------------------
void qMRMLViewControllerBarPrivate::arrangeBarLayout()
{
  QList<QLayoutItem*> currentItems;
  QHash<QLayoutItem*, int> currentStretches;
  for (int index = 0; index < this->BarLayout->count(); ++index)
  {
    QLayoutItem* item = this->BarLayout->itemAt(index);
    currentItems << item;
    currentStretches[item] = this->BarLayout->stretch(index);
  }
  if (!this->ClassicBarLayoutRecorded)
  {
    // The bar layout has its classic order until it is arranged for the first time
    for (QLayoutItem* item : currentItems)
    {
      this->ClassicBarLayoutItems << BarLayoutItem{ item, currentStretches[item] };
    }
    this->ClassicBarLayoutRecorded = true;
  }

  QList<QLayoutItem*> orderedItems;
  QHash<QLayoutItem*, int> stretches = currentStretches;
  if (this->FlatStyle)
  {
    auto layoutItems = [this](const QList<QPointer<QWidget>>& widgets)
    {
      QList<QLayoutItem*> items;
      for (QWidget* widget : widgets)
      {
        int index = widget ? this->BarLayout->indexOf(widget) : -1;
        if (index >= 0)
        {
          items << this->BarLayout->itemAt(index);
        }
      }
      return items;
    };
    QList<QLayoutItem*> leadingItems = layoutItems(this->FlatLeadingWidgets);
    QList<QLayoutItem*> trailingItems = layoutItems(this->FlatTrailingWidgets);
    orderedItems = leadingItems;
    for (QLayoutItem* item : currentItems)
    {
      if (!leadingItems.contains(item) && !trailingItems.contains(item))
      {
        orderedItems << item;
      }
    }
    orderedItems << trailingItems;
  }
  else
  {
    for (const BarLayoutItem& classicItem : this->ClassicBarLayoutItems)
    {
      if (currentItems.contains(classicItem.Item))
      {
        orderedItems << classicItem.Item;
        stretches[classicItem.Item] = classicItem.Stretch;
      }
    }
    // Items added after the classic order was recorded keep their order, at the end
    for (QLayoutItem* item : currentItems)
    {
      if (!orderedItems.contains(item))
      {
        orderedItems << item;
      }
    }
  }

  bool changed = (orderedItems != currentItems);
  for (int index = 0; !changed && index < orderedItems.count(); ++index)
  {
    changed = (this->BarLayout->stretch(index) != stretches[orderedItems[index]]);
  }
  if (!changed)
  {
    return;
  }
  // Taking an item out of the layout does not delete it (nor its widget)
  while (this->BarLayout->count() > 0)
  {
    this->BarLayout->takeAt(0);
  }
  for (int index = 0; index < orderedItems.count(); ++index)
  {
    this->BarLayout->insertItem(index, orderedItems[index]);
    this->BarLayout->setStretch(index, stretches[orderedItems[index]]);
  }
}

//---------------------------------------------------------------------------
void qMRMLViewControllerBarPrivate::setupPopupUi()
{
  this->PopupWidget->setAutoShow(false);
  this->PopupWidget->setAutoHide(true);
  this->PopupWidget->setOrientation(Qt::Vertical);
  this->PopupWidget->setSizePolicy(QSizePolicy::Minimum, QSizePolicy::Maximum);
}

//---------------------------------------------------------------------------
bool qMRMLViewControllerBarPrivate::eventFilter(QObject* object, QEvent* event)
{
  Q_Q(qMRMLViewControllerBar);
  if (object == this->PinButton && event->type() == QEvent::Enter)
  {
    // The popup opens on hover in the classic presentation only (the options button opens it on click)
    if (this->LayoutBehavior == qMRMLViewControllerBar::Popup && !this->AppliedFlatStyle)
    {
      this->PopupWidget->showPopup();
    }
  }
  else if (object == q && event->type() == QEvent::Polish)
  {
    // Subclasses that do not apply the presentation at the end of their initialization
    this->ensureFlatStyleApplied();
  }
  else if (object == this->BarWidget && event->type() == QEvent::Paint && this->AppliedFlatStyle)
  {
    // The strip is filled (auto fill background) before the paint event: draw the pill over it, the children
    // are painted after
    this->paintFlatPill();
  }
  else if (object == this->BarWidget && event->type() == QEvent::PaletteChange && this->AppliedFlatStyle)
  {
    // The flat bar color follows the palette (theme changes)
    QColor barColor = this->viewBarColor();
    if (barColor != this->FlatBarFillColor)
    {
      QColor color = this->BarColor;
      this->BarColor = QColor();
      this->setColor(color);
    }
  }
  return this->QObject::eventFilter(object, event);
}

//---------------------------------------------------------------------------
void qMRMLViewControllerBarPrivate::setColor(QColor barColor)
{
  if (barColor.isValid() && barColor == this->BarColor)
  {
    // Setting the palette makes the whole bar lay out and repaint again
    return;
  }
  this->BarColor = barColor;

  if (this->AppliedFlatStyle)
  {
    // Flat bar: a pill with the "view-bar" fill (paintFlatPill) on a strip of the canvas color, and an 8 px dot of
    // the view color. The other colors follow the application palette (no forced black text).
    this->FlatBarFillColor = this->viewBarColor();
    QColor viewColor = barColor.isValid() ? barColor : this->FlatBarFillColor;
    QPalette flatPalette;
    flatPalette.setColor(QPalette::Window, this->flatStripColor());
    this->BarWidget->setPalette(flatPalette);
    // View label: a dot of the view color, without the letter (it would read as a direction)
    this->ViewLabel->setStyleSheet(QString("QLabel { background-color: %1; color: transparent; border-radius: %2px; }").arg(viewColor.name()).arg(FlatViewDotSize / 2));
    return;
  }

  QPalette palette = this->BarWidget->palette();
  QLinearGradient gradient(QPointF(0., 0.), QPointF(0., 1.));
  gradient.setCoordinateMode(QGradient::StretchToDeviceMode);
  // Light gradient
  // gradient.setColorAt(0., barColor.lighter(102));
  // gradient.setColorAt(0.1, barColor);
  // gradient.setColorAt(0.8, barColor.darker(102));
  // gradient.setColorAt(1., barColor.darker(108));
  // Glass effect
  // gradient.setColorAt(0., barColor.lighter(120));
  // gradient.setColorAt(0.498, barColor.darker(118));
  // gradient.setColorAt(0.5, barColor.darker(145));
  // gradient.setColorAt(1., barColor.darker(165));
  // Dialog effect
  int hue = barColor.hue();
  int sat = barColor.saturation();
  double valueCoef = static_cast<double>(barColor.value()) / 240.;
  gradient.setColorAt(0.0, QColor::fromHsv(hue, sat, qMin(255., 358. * valueCoef)));
  gradient.setColorAt(0.15, QColor::fromHsv(hue, sat, qMin(255., 224 * valueCoef)));
  gradient.setColorAt(0.24, QColor::fromHsv(hue, sat, qMin(255., 213 * valueCoef)));
  gradient.setColorAt(0.65, QColor::fromHsv(hue, sat, qMin(255., 255 * valueCoef)));
  gradient.setColorAt(0.71, QColor::fromHsv(hue, sat, qMin(255., 246 * valueCoef)));
  gradient.setColorAt(0.85, QColor::fromHsv(hue, sat, qMin(255., 253 * valueCoef)));
  gradient.setColorAt(1.0, QColor::fromHsv(hue, sat, qMin(255., 191 * valueCoef)));
  palette.setBrush(QPalette::Window, gradient);
  palette.setBrush(QPalette::Text, Qt::black);
  this->BarWidget->setPalette(palette);
}

//---------------------------------------------------------------------------
QColor qMRMLViewControllerBarPrivate::color() const
{
  return this->BarColor;
}

// --------------------------------------------------------------------------
// qMRMLViewControllerBar methods

// --------------------------------------------------------------------------
qMRMLViewControllerBar::qMRMLViewControllerBar(QWidget* parentWidget)
  : Superclass(parentWidget)
  , d_ptr(new qMRMLViewControllerBarPrivate(*this))
{
  Q_D(qMRMLViewControllerBar);
  d->init();
}

// --------------------------------------------------------------------------
qMRMLViewControllerBar::qMRMLViewControllerBar(qMRMLViewControllerBarPrivate* pimpl, QWidget* parentWidget)
  : Superclass(parentWidget)
  , d_ptr(pimpl)
{
  // init() should be called in the subclass constructor
}

// --------------------------------------------------------------------------
qMRMLViewControllerBar::~qMRMLViewControllerBar() = default;

// --------------------------------------------------------------------------
void qMRMLViewControllerBar::setLayoutBehavior(LayoutBehavior behavior)
{
  Q_D(qMRMLViewControllerBar);

  d->LayoutBehavior = behavior;

  if (d->PopupWidget)
  {
    if (behavior == qMRMLViewControllerBar::Popup)
    {
      d->PopupWidget->setAutoShow(false);
      d->PopupWidget->setAutoHide(true);
      d->ControllerLayout->removeWidget(d->PopupWidget);
      d->PopupWidget->setActive(true);
      d->PopupWidget->setWindowFlags(d->PopupWidget->windowFlags() | Qt::ToolTip);
      d->PinButton->show();
    }
    else
    {
      d->PopupWidget->setAutoShow(false);
      d->PopupWidget->setAutoHide(false);
      d->ControllerLayout->addWidget(d->PopupWidget);
      d->PinButton->hide();
      // d->PinButton->setDown(1);
      d->PopupWidget->setActive(false);
      d->PopupWidget->setWindowFlags(d->PopupWidget->windowFlags() & ~Qt::ToolTip);
      d->PopupWidget->pinPopup(true);
    }
    if (d->AppliedFlatStyle)
    {
      d->updatePopupButtonsVisibility();
    }
  }
}

// --------------------------------------------------------------------------
QLayout* qMRMLViewControllerBar::barLayout()
{
  Q_D(qMRMLViewControllerBar);

  return d->BarLayout;
}

// --------------------------------------------------------------------------
QWidget* qMRMLViewControllerBar::barWidget()
{
  Q_D(qMRMLViewControllerBar);

  return d->BarWidget;
}

// --------------------------------------------------------------------------
QToolButton* qMRMLViewControllerBar::pinButton()
{
  Q_D(qMRMLViewControllerBar);
  return d->PinButton;
}

// --------------------------------------------------------------------------
QLabel* qMRMLViewControllerBar::viewLabel()
{
  Q_D(qMRMLViewControllerBar);
  return d->ViewLabel;
}

// --------------------------------------------------------------------------
void qMRMLViewControllerBar::maximizeView()
{
  Q_D(qMRMLViewControllerBar);
  if (!this->mrmlViewNode())
  {
    qCritical() << Q_FUNC_INFO << ": Invalid view node.";
    return;
  }

  bool isMaximized = false;
  bool canBeMaximized = false;
  vtkMRMLLayoutNode* layoutNode = this->mrmlViewNode()->GetMaximizedState(isMaximized, canBeMaximized);
  if (!layoutNode || !canBeMaximized)
  {
    return;
  }

  if (isMaximized)
  {
    layoutNode->RemoveMaximizedViewNode(this->mrmlViewNode());
  }
  else
  {
    layoutNode->AddMaximizedViewNode(this->mrmlViewNode());
  }
}

//---------------------------------------------------------------------------
void qMRMLViewControllerBar::setMRMLViewNode(vtkMRMLAbstractViewNode* viewNode)
{
  Q_D(qMRMLViewControllerBar);
  this->qvtkReconnect(d->ViewNode, viewNode, vtkCommand::ModifiedEvent, this, SLOT(updateWidgetFromMRMLView()));
  d->ViewNode = viewNode;
  this->updateWidgetFromMRMLView();
}

//---------------------------------------------------------------------------
vtkMRMLAbstractViewNode* qMRMLViewControllerBar::mrmlViewNode() const
{
  Q_D(const qMRMLViewControllerBar);
  return d->ViewNode;
}

//---------------------------------------------------------------------------
void qMRMLViewControllerBar::updateWidgetFromMRMLView()
{
  Q_D(qMRMLViewControllerBar);

  // The view node gets modified whenever it is added/remoeved from the layout
  // therefore we can update the maximize button status here (there is no need
  // to observe the layout).
  bool isMaximized = false;
  bool canBeMaximized = false;
  if (d->ViewNode)
  {
    d->ViewNode->GetMaximizedState(isMaximized, canBeMaximized);
  }

  d->MaximizeViewButton->setVisible(canBeMaximized && d->ShowMaximizeViewButton);
  if (canBeMaximized && d->ShowMaximizeViewButton)
  {
    // This is called at every change of the view node: only update the button when its state changes
    QString toolTip;
    if (d->AppliedFlatStyle)
    {
      toolTip = isMaximized ? qMRMLViewControllerBarPrivate::flatToolTip(tr("Restore layout"), tr("double-click"), tr("Show all the views again"))
                            : qMRMLViewControllerBarPrivate::flatToolTip(tr("Maximize"), tr("double-click"), tr("Show this view alone (Esc restores)"));
    }
    else
    {
      toolTip = isMaximized ? tr("Restore view layout") : tr("Maximize view");
    }
    const QIcon& icon = d->AppliedFlatStyle ? (isMaximized ? d->FlatRestoreIcon : d->FlatMaximizeIcon) //
                                            : (isMaximized ? d->ViewRestoreIcon : d->ViewMaximizeIcon);
    if (d->MaximizeViewButton->toolTip() != toolTip)
    {
      d->MaximizeViewButton->setToolTip(toolTip);
    }
    if (d->MaximizeViewButton->icon().cacheKey() != icon.cacheKey())
    {
      d->MaximizeViewButton->setIcon(icon);
    }
  }
}

//-----------------------------------------------------------------------------
bool qMRMLViewControllerBar::showMaximizeViewButton() const
{
  Q_D(const qMRMLViewControllerBar);
  return d->ShowMaximizeViewButton;
}

//-----------------------------------------------------------------------------
void qMRMLViewControllerBar::setShowMaximizeViewButton(bool show)
{
  Q_D(qMRMLViewControllerBar);
  d->ShowMaximizeViewButton = show;
  this->updateWidgetFromMRMLView();
}

//-----------------------------------------------------------------------------
void qMRMLViewControllerBar::setDefaultFlatStyle(bool flat)
{
  DefaultFlatStyle = flat;
}

//-----------------------------------------------------------------------------
bool qMRMLViewControllerBar::defaultFlatStyle()
{
  return DefaultFlatStyle;
}

//-----------------------------------------------------------------------------
bool qMRMLViewControllerBar::flatStyle() const
{
  Q_D(const qMRMLViewControllerBar);
  return d->FlatStyle;
}

//-----------------------------------------------------------------------------
void qMRMLViewControllerBar::setFlatStyle(bool flat)
{
  Q_D(qMRMLViewControllerBar);
  d->FlatStyle = flat;
  d->ensureFlatStyleApplied();
}
