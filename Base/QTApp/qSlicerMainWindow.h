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

#ifndef __qSlicerMainWindow_h
#define __qSlicerMainWindow_h

// Qt includes
#include <QMainWindow>
#include <QVariantMap>

// CTK includes
#include <ctkErrorLogModel.h>

// Slicer includes
#include "qSlicerBaseQTAppExport.h"
#include "qSlicerIO.h"
#include "vtkSlicerConfigure.h" // For Slicer_BUILD_DICOM_SUPPORT, Slicer_USE_PYTHONQT, Slicer_USE_QtTesting

class ctkErrorLogWidget;
class ctkPythonConsole;
class qSlicerModuleSelectorToolBar;
class qSlicerMainWindowPrivate;
class vtkMRMLAbstractViewNode;
class vtkObject;

class Q_SLICER_BASE_QTAPP_EXPORT qSlicerMainWindow : public QMainWindow
{
  Q_OBJECT

  /// Focus mode (View > Maximize view, Ctrl+6 or F11): the module panel, the toolbars and the dock widgets
  /// are hidden, except toolbars and dock widgets that have the dynamic property SlicerKeepInFocusMode set to
  /// true. Leaving the focus mode shows exactly what entering it hid. Showing the module panel (for example
  /// by selecting a module) leaves the focus mode.
  /// \sa focusModeChanged()
  Q_PROPERTY(bool focusMode READ focusMode WRITE setFocusMode NOTIFY focusModeChanged)

  /// Width of the module panel that is kept when another module is selected (0 if not known yet).
  /// The panel is only made wider when the module needs more room. A width chosen by the user (by dragging
  /// the panel edge) or set with setPanelWidth() is saved in the application settings (Studio/ClassicPanelWidth).
  /// The width is not managed while the application manages it itself (the main window property
  /// SlicerStudioConstrainedPanel is true, or the module panel has a larger minimum width than its layout needs),
  /// nor in testing mode.
  Q_PROPERTY(int panelWidth READ panelWidth WRITE setPanelWidth)

public:
  typedef QMainWindow Superclass;

  qSlicerMainWindow(QWidget* parent = nullptr);
  ~qSlicerMainWindow() override;

  /// Return a pointer to the module selector toolbar that can change the
  /// current module.
  /// \sa pythonConsole(), errorLogWidget()
  Q_INVOKABLE qSlicerModuleSelectorToolBar* moduleSelector() const;

#ifdef Slicer_USE_PYTHONQT
  /// Return a pointer to the python console.
  /// \sa moduleSelector(), errorLogWidget()
  Q_INVOKABLE ctkPythonConsole* pythonConsole() const;
#endif
  /// Return a pointer to the error log widget.
  /// \sa moduleSelector(), pythonConsole()
  Q_INVOKABLE ctkErrorLogWidget* errorLogWidget() const;
  /// Return a pointer to the dock widget that shows the Error Log.
  Q_INVOKABLE QDockWidget* errorLogDockWidget() const;

  /// \sa focusMode, setFocusMode()
  bool focusMode() const;

  /// \sa panelWidth, setPanelWidth()
  int panelWidth() const;

public slots:
  virtual void setHomeModuleCurrent();
  virtual void restoreToolbars();

  virtual void on_FileFavoriteModulesAction_triggered();
  virtual void on_FileAddDataAction_triggered();
  virtual void on_FileLoadDataAction_triggered();
  virtual void on_FileImportSceneAction_triggered();
  virtual void on_FileLoadSceneAction_triggered();
  virtual void on_FileAddVolumeAction_triggered();
  virtual void on_FileAddTransformAction_triggered();
  virtual void on_FileSaveSceneAction_triggered();
  virtual void on_FileExitAction_triggered();
  virtual void onFileRecentLoadedActionTriggered();
  virtual void on_SDBSaveToDirectoryAction_triggered();
  virtual void on_SDBSaveToMRBAction_triggered();
  virtual void on_FileCloseSceneAction_triggered();
  virtual void on_LoadDICOMAction_triggered();

  virtual void on_EditRecordMacroAction_triggered();
  virtual void on_EditPlayMacroAction_triggered();
  virtual void on_EditUndoAction_triggered();
  virtual void on_EditRedoAction_triggered();

  virtual void on_ModuleHomeAction_triggered();

  virtual void onLayoutActionTriggered(QAction* action);
  virtual void onLayoutCompareActionTriggered(QAction* action);
  virtual void onLayoutCompareGridActionTriggered(QAction* action);

  /// Set the view layout.
  virtual void setLayout(int);

  /// Restores the original (non-maximized) layout in all viewports.
  virtual void removeAllMaximizedViewNodes();

  virtual void setLayoutNumberOfCompareViewRows(int);
  virtual void setLayoutNumberOfCompareViewColumns(int);

  virtual void onPythonConsoleToggled(bool);
  virtual void onErrorLogToggled(bool);

  virtual void on_WindowToolbarsResetToDefaultAction_triggered();

  virtual void on_EditApplicationSettingsAction_triggered();
  virtual void on_CutAction_triggered();
  virtual void on_CopyAction_triggered();
  virtual void on_PasteAction_triggered();
  virtual void on_ViewExtensionsManagerAction_triggered();

  virtual void on_ShowStatusBarAction_triggered(bool);

  /// Write GUI state to application settings.
  ///
  /// GUI state includes:
  /// - main window state and geometry (only if MainWindow/geometry application setting is
  ///   enabled or force argument is set to true)
  /// - current view layout ID
  /// - favorite modules
  /// - recently loaded files
  /// - module panel width
  ///
  /// The focus mode is left first, so that the saved state never has hidden panels.
  ///
  /// \sa restoreGUIState()
  virtual void saveGUIState(bool force = false);

  /// Read GUI state from application settings and update the user interface accordingly.
  ///
  /// Before the window is first shown, the main window state (toolbars and dock widgets) is restored only
  /// when the window is first shown, once all modules have added their toolbars. Meanwhile the main window has
  /// the dynamic property SlicerDeferRestoreState set to true: modules that add a toolbar do not need to
  /// restore the main window state themselves.
  /// \sa saveGUIState()
  virtual void restoreGUIState(bool force = false);

  virtual void addFileToRecentFiles(const qSlicerIO::IOProperties& fileProperties);

  /// Enter or leave the focus mode.
  /// \sa focusMode
  void setFocusMode(bool enabled);

  /// Set the width of the module panel kept between modules.
  /// \sa panelWidth
  void setPanelWidth(int width);

  /// Refresh favorite modules toolbar from application settings
  virtual void on_FavoriteModulesChanged();

signals:
  /// Emitted when the window is first shown to the user.
  /// \sa showEvent(QShowEvent*)
  void initialWindowShown();

  /// Emitted when the focus mode is entered or left.
  /// \sa focusMode
  void focusModeChanged(bool enabled);

  /// Emitted instead of showing the startup disclaimer popup (when the event loop runs after the window is
  /// first shown) if the application property SlicerDisclaimerHandledByApplication is true then: the application
  /// shows the message itself. It is not emitted if the user chose not to show the message again.
  /// \sa disclaimer()
  void disclaimerRequested(const QString& message);

  /// Emitted when the application is about to ask whether to save before exit, before it reads the main
  /// window properties SlicerStudioCloseMode and SlicerStudioCloseSummary (cleared before the signal is
  /// emitted). A handler connected with a direct connection may set them:
  /// - "unsavedLesion": the summary is shown with the buttons "Save lesion...", "Don't save" and "Cancel";
  /// - "lesionSaved": the summary is shown with the buttons "Quit", "Save session..." and "Cancel";
  /// - empty (the default): the usual confirmation.
  /// \sa saveRequested()
  void closeSummaryRequested();

  /// Emitted when the user chose "Save lesion..." in the exit confirmation. A handler connected with a direct
  /// connection saves and sets the main window property SlicerStudioSaveResult to "saved" (anything else,
  /// for example "cancelled", keeps the application open).
  /// \sa closeSummaryRequested()
  void saveRequested();

protected slots:
  virtual void onModuleLoaded(const QString& moduleName);
  virtual void onModuleAboutToBeUnloaded(const QString& moduleName);
  virtual void onNewFileLoaded(const qSlicerIO::IOProperties& fileProperties);
  virtual void onFileSaved(const qSlicerIO::IOProperties& fileProperties);

  virtual void onMRMLSceneModified(vtkObject*);
  virtual void onLayoutChanged(int);
  virtual void onWarningsOrErrorsOccurred(ctkErrorLogLevel::LogLevel logLevel);

  // Show/hide update indicator on Extensions Manager toolbar icon
  void setExtensionUpdatesAvailable(bool updateAvailable);

#ifdef Slicer_USE_PYTHONQT
  virtual void onPythonConsoleUserInput(const QString&);
#endif

  /// The Error Log widget was docked into a different screen region,
  /// adjust its orientation as needed.
  virtual void onErrorLogDockWidgetAreaChanged(Qt::DockWidgetArea);

  /// User interacted with the Error Log window,
  /// hide the new error notification.
  void onUserViewedErrorLog();

protected:
  /// Connect MainWindow action with slots defined in MainWindowCore
  virtual void setupMenuActions();

  /// Open Python console if it was requested
  virtual void pythonConsoleInitialDisplay();

  /// Open a popup to warn the user Slicer is not for clinical use.
  /// \sa disclaimerRequested()
  virtual void disclaimer();

  /// Forward the dragEnterEvent to the IOManager which will
  /// decide if it could accept a drag/drop or not.
  /// \sa dropEvent()
  void dragEnterEvent(QDragEnterEvent* event) override;

  /// Forward the dropEvent to the IOManager.
  /// \sa dragEnterEvent()
  void dropEvent(QDropEvent* event) override;

  /// Reimplemented to catch activationChange/show/hide events.
  /// More specifically it allows to:
  ///  1. update the state of the errorLog and python console QAction when
  ///  associated dialog are visible or not.
  ///  2. set the state of ErrorLog button based on the activation state of
  ///  the error log dialog.
  bool eventFilter(QObject* object, QEvent* event) override;

  void closeEvent(QCloseEvent* event) override;
  void showEvent(QShowEvent* event) override;

  void changeEvent(QEvent* event) override;

protected:
  QScopedPointer<qSlicerMainWindowPrivate> d_ptr;

  qSlicerMainWindow(qSlicerMainWindowPrivate* pimpl, QWidget* parent);

private:
  Q_DECLARE_PRIVATE(qSlicerMainWindow);
  Q_DISABLE_COPY(qSlicerMainWindow);
};

#endif
