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

#ifndef __qMRMLViewControllerBar_p_h
#define __qMRMLViewControllerBar_p_h

//
//  W A R N I N G
//  -------------
//
// This file is not part of the Slicer API.  It exists purely as an
// implementation detail.  This header file may change from version to
// version without notice, or even be removed.
//
// We mean it.
//

// Qt includes
#include <QHash>
#include <QIcon>
#include <QList>
#include <QMargins>
#include <QObject>
#include <QPointer>
#include <QSize>

// qMRML includes
#include "qMRMLViewControllerBar.h"

// MRML includes
#include "vtkMRMLAbstractViewNode.h"
#include "vtkMRMLLayoutNode.h"

// VTK includes
#include "vtkWeakPointer.h"

class ctkPopupWidget;
class QLabel;
class QLayoutItem;
class QToolButton;
class QHBoxLayout;
class QVBoxLayout;
class vtkMRMLLayoutNode;

//-----------------------------------------------------------------------------
class QMRML_WIDGETS_EXPORT qMRMLViewControllerBarPrivate : public QObject
{
  Q_OBJECT
  Q_DECLARE_PUBLIC(qMRMLViewControllerBar);

protected:
  qMRMLViewControllerBar* const q_ptr;

public:
  typedef QObject Superclass;
  qMRMLViewControllerBarPrivate(qMRMLViewControllerBar& object);
  ~qMRMLViewControllerBarPrivate() override;

  virtual void init();
  virtual void setColor(QColor color);
  virtual QColor color() const;

  /// Apply the presentation selected by FlatStyle (flat or classic) to the bar.
  /// Subclasses extend it; the base class arranges the bar layout, sets the colors and
  /// emits flatStyleChanged() at the end.
  virtual void updateFlatStyle();
  /// Apply the presentation if it differs from the one shown (subclasses call it at the end of init()).
  void ensureFlatStyleApplied();

  /// Fill color of the flat bar: the "view-bar" token of the application style, or the window
  /// color of the palette, darkened.
  QColor viewBarColor() const;

  /// Return the flat bar tooltip "Name · key · description" (empty parts are skipped).
  static QString flatToolTip(const QString& name, const QString& key, const QString& description);

  /// Give \a widget the fixed size \a size in the flat presentation (the classic size constraints are restored
  /// by restoreClassicSize()).
  void setFlatFixedSize(QWidget* widget, const QSize& size);
  void restoreClassicSize(QWidget* widget);

  /// Create a tool button of the flat bar (hidden until the flat presentation is shown).
  QToolButton* createFlatToolButton(const QString& objectName);

  // Need to observe the view and layout nodes to update maximize/restore button state.
  vtkWeakPointer<vtkMRMLAbstractViewNode> ViewNode;
  vtkWeakPointer<vtkMRMLLayoutNode> LayoutNode;

  QToolButton* PinButton{ nullptr };
  QLabel* ViewLabel{ nullptr };
  QToolButton* MaximizeViewButton{ nullptr };
  ctkPopupWidget* PopupWidget{ nullptr };
  QWidget* BarWidget{ nullptr };
  QHBoxLayout* BarLayout{ nullptr };
  QVBoxLayout* ControllerLayout{ nullptr };
  qMRMLViewControllerBar::LayoutBehavior LayoutBehavior{ qMRMLViewControllerBar::Popup };
  QColor BarColor;
  QIcon ViewMaximizeIcon;
  QIcon ViewRestoreIcon;
  bool ShowMaximizeViewButton{ true };

  /// Flat presentation requested (qMRMLViewControllerBar::flatStyle)
  bool FlatStyle{ false };
  /// Presentation shown: it differs from FlatStyle until updateFlatStyle() is called
  bool AppliedFlatStyle{ false };
  /// Button that opens the popup on click in the flat presentation
  QToolButton* OptionsButton{ nullptr };
  /// Fill color of the flat bar when its palette was last set (see viewBarColor())
  QColor FlatBarFillColor;
  QIcon FlatMaximizeIcon;
  QIcon FlatRestoreIcon;
  /// Widgets placed first and last in the flat bar, in these orders. The other items of the bar layout
  /// keep their relative order in between.
  QList<QPointer<QWidget>> FlatLeadingWidgets;
  QList<QPointer<QWidget>> FlatTrailingWidgets;
  /// Widgets shown only in the flat presentation
  QList<QPointer<QWidget>> FlatOnlyWidgets;

  /// Classic order of the bar layout items, recorded the first time the layout is arranged
  struct BarLayoutItem
  {
    QLayoutItem* Item;
    int Stretch;
  };
  QList<BarLayoutItem> ClassicBarLayoutItems;
  bool ClassicBarLayoutRecorded{ false };
  QMargins ClassicBarMargins;
  int ClassicBarSpacing{ 2 };
  QString ClassicViewLabelStyleSheet;
  QString ClassicPinButtonToolTip;
  QSize ClassicMaximizeIconSize;
  /// Classic size constraints of widgets resized by setFlatFixedSize()
  struct ClassicSize
  {
    QSize Minimum;
    QSize Maximum;
  };
  QHash<QWidget*, ClassicSize> ClassicSizes;
  /// Pin state of the classic presentation (the pin is unchecked while the bar is flat)
  bool ClassicPinButtonChecked{ false };

  /// Application event filter installed while the popup of the flat bar is open, so that Esc closes it
  /// before any other rung of the Esc ladder (it is not a Qt popup: Qt does not close it on Esc)
  QObject* PopupEscapeFilter{ nullptr };
  /// The open popup closes on the next Esc
  bool PopupEscapeArmed{ false };
  /// Esc closed the popup on the shortcut override: the key press that follows is consumed too
  bool PopupEscapeHandled{ false };
  /// Time stamp of the key press of Esc that closed the popup
  ulong PopupEscapeTimestamp{ 0 };
  /// Install (or remove) the application event filter that closes the flat popup on Esc.
  void setPopupEscapeFilterInstalled(bool install);
  /// Close the open flat popup if \a event is a key press of Esc without modifiers. Return true if the
  /// event is consumed (called by the application event filter).
  bool closePopupOnEscape(QObject* object, QEvent* event);

  bool eventFilter(QObject* object, QEvent* event) override;

signals:
  /// Emitted at the end of updateFlatStyle() (subclasses without their own private
  /// implementation of updateFlatStyle() react to it).
  void flatStyleChanged(bool flat);

protected:
  virtual void setupPopupUi();
  /// Reorder the bar layout items for the presentation selected by FlatStyle.
  void arrangeBarLayout();
  /// Show the pin or the options button depending on the presentation and the layout behavior.
  void updatePopupButtonsVisibility();
};

#endif
