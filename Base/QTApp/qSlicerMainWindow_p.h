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

  This file was originally developed by Jean-Christophe Fillion-Robin, Kitware Inc.
  and was partially funded by NIH grant 3P41RR013218-12S1

==============================================================================*/

#ifndef __qSlicerMainWindow_p_h
#define __qSlicerMainWindow_p_h

// Qt includes
#include <QPointer>
#include <QQueue>
class QTimer;
class QToolButton;

// Slicer includes
#include "qSlicerBaseQTAppExport.h"
#include "qSlicerMainWindow.h"
#include <qSlicerIO.h>
#include "ui_qSlicerMainWindow.h"

class qSlicerModuleSelectorToolBar;
class qSlicerMainWindowPrivate;
class qSlicerLayoutManager;

//-----------------------------------------------------------------------------
class Q_SLICER_BASE_QTAPP_EXPORT qSlicerMainWindowPrivate : public Ui_qSlicerMainWindow
{
  Q_DECLARE_PUBLIC(qSlicerMainWindow);

protected:
  qSlicerMainWindow* const q_ptr;

public:
  typedef qSlicerMainWindowPrivate Self;
  qSlicerMainWindowPrivate(qSlicerMainWindow& object);
  virtual ~qSlicerMainWindowPrivate();

  virtual void init();
  virtual void setupUi(QMainWindow* mainWindow);
  virtual void setupStatusBar();

  virtual void setupRecentlyLoadedMenu(const QList<qSlicerIO::IOProperties>& fileProperties);

  virtual void filterRecentlyLoadedFileProperties();

  static QList<qSlicerIO::IOProperties> readRecentlyLoadedFiles();
  static void writeRecentlyLoadedFiles(const QList<qSlicerIO::IOProperties>& fileProperties);

  virtual bool isSceneContentModifiedSinceRead(QString& details);
  virtual bool confirmCloseApplication();
  virtual bool confirmCloseScene();

  /// Exit confirmations described by the application (closeSummaryRequested()). Return true to close.
  bool confirmCloseApplicationUnsavedLesion(const QString& summary);
  bool confirmCloseApplicationLesionSaved(const QString& summary);

  /// Add the menu actions that have a shortcut to the main window, so that their shortcuts keep working
  /// when the menu bar is hidden.
  void addMenuShortcutActions();

  /// Show the focus mode in the View menu action.
  void updateFocusModeAction();

  /// True if the main window keeps the module panel width between modules (see panelWidth).
  bool isPanelWidthKept() const;
  /// Set the module panel to the kept width (or wider if the module needs it) when the event loop runs again.
  void schedulePanelWidthUpdate();
  void updatePanelWidth();
  /// The user resized the module panel: the new width is kept.
  void onPanelResized();
  void persistPanelWidth();

  void setErrorLogIconHighlighted(bool);

  void updatePythonConsolePalette();

  // Add module action to the favorite modules toolbar
  void addFavoriteModule(const QString& moduleName);

  // clang-format off
#ifdef Slicer_USE_PYTHONQT
  QDockWidget*                    PythonConsoleDockWidget{ nullptr };
  QAction*                        PythonConsoleToggleViewAction{ nullptr };
#endif
  QDockWidget*                    ErrorLogDockWidget{ nullptr };
  QAction*                        ErrorLogToggleViewAction{ nullptr };
  ctkErrorLogWidget*              ErrorLogWidget{ nullptr };
  QToolButton*                    ErrorLogToolButton{ nullptr };
  QIcon                           ErrorLogIconHighlighted;
  QIcon                           ErrorLogIconNormal;
  bool                            ErrorLogIconIsHighlighted{ false };
  QToolButton*                    LayoutButton{ nullptr };
  qSlicerModuleSelectorToolBar*   ModuleSelectorToolBar{ nullptr };
  QStringList                     FavoriteModules;
  // In case of a custom CentralWidget is used, the layout manager may get deleted.
  // Use QPointer to detect if the underlying object is deleted.
  QPointer<qSlicerLayoutManager>  LayoutManager;
  QQueue<qSlicerIO::IOProperties> RecentlyLoadedFileProperties;

  QByteArray                      StartupState;

  bool                            WindowInitialShowCompleted{ false };
  bool                            IsClosing{ false };

  // Main window state read by restoreGUIState() before the window is first shown, restored when it is shown
  QByteArray                      DeferredWindowState;
  bool                            WindowStateRestoreDeferred{ false };

  bool                            FocusMode{ false };
  bool                            FocusModeChanging{ false };
  QList<QPointer<QWidget>>        FocusModeHiddenWidgets;

  int                             PanelWidth{ 0 };
  bool                            PanelWidthChosen{ false };
  bool                            PanelWidthUpdatePending{ false };
  QTimer*                         PanelWidthPersistTimer{ nullptr };
  int                             PanelWidthPersistWindowWidth{ -1 };
  // clang-format on
};

#endif
