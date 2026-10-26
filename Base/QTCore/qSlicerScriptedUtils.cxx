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

#include "qSlicerScriptedUtils_p.h"

// Qt includes
#include <QDir>
#include <QFileInfo>
#include <QSet>
#include <QSettings>

// PythonQt includes
#include <PythonQt.h>

// CTK includes
#include <ctkScopedCurrentDir.h>

// Slicer includes
#include "qSlicerCoreApplication.h"
#include "qSlicerCorePythonManager.h"

//-----------------------------------------------------------------------------
bool qSlicerScriptedUtils::loadSourceAsModule(const QString& moduleName, const QString& filePath, PyObject* global_dict, PyObject* local_dict)
{
  PyObject* pyRes = nullptr;

  if (filePath.endsWith(".py") || filePath.endsWith(".pyc"))
  {
    QString pyRunStr = QString("import importlib.util;"
                               "import sys;"
                               "spec = importlib.util.spec_from_file_location(%2, %1);"
                               "module = importlib.util.module_from_spec(spec);"
                               "sys.modules[%2] = module;"
                               "spec.loader.exec_module(module)")
                         .arg(qSlicerCorePythonManager::toPythonStringLiteral(filePath))
                         .arg(qSlicerCorePythonManager::toPythonStringLiteral(moduleName));

    pyRes = PyRun_String(pyRunStr.toUtf8(), Py_file_input, global_dict, local_dict);
  }
  if (!pyRes)
  {
    PythonQt::self()->handleError();
    qCritical() << "loadSourceAsModule - Failed to load file" << filePath << " as module" << moduleName << "!";
    return false;
  }
  Py_DECREF(pyRes);
  return true;
}

//-----------------------------------------------------------------------------
bool qSlicerScriptedUtils::setModuleAttribute(const QString& moduleName, const QString& attributeName, PyObject* attributeValue)
{
  if (!attributeValue)
  {
    return false;
  }

  // Import module
  PyObject* module = PythonQt::self()->getMainModule();
  if (!moduleName.isEmpty())
  {
    module = PyImport_ImportModule(moduleName.toUtf8());
    if (!module)
    {
      PythonQt::self()->handleError();
      return false;
    }
  }

  // Add the object to the imported module
  int ret = PyObject_SetAttrString(module, attributeName.toUtf8(), attributeValue);
  if (ret != 0)
  {
    PythonQt::self()->handleError();
    return false;
  }
  return true;
}

//-----------------------------------------------------------------------------
bool qSlicerScriptedUtils::importModulePythonExtensions(qSlicerCorePythonManager* pythonManager, const QString& intDir, const QString& modulePath, bool isEmbedded)
{
  Q_UNUSED(intDir);

  if (!pythonManager)
  {
    return false;
  }

  QString pythonModuleDir = QFileInfo(modulePath).absoluteFilePath();
  if (!QFileInfo(pythonModuleDir).isDir())
  {
    pythonModuleDir = QFileInfo(pythonModuleDir).absolutePath();
  }

  // Update current application directory, so that *PythonD modules can be loaded
  ctkScopedCurrentDir scopedCurrentDir(pythonModuleDir);

  if (!isEmbedded)
  {
    QStringList paths;
    paths << scopedCurrentDir.currentPath();
    pythonManager->appendPythonPaths(paths);
  }

  // With the setting Studio/LazyPythonWrappers, register the class index generated at build time
  // (Utilities/Scripts/GenerateSlicerWrapperIndex.py) instead of importing every wrapper of the
  // directory: each wrapper is then imported on first use, and the remaining ones while the
  // application is idle after startup (slicer.util._registerLazyWrapperIndex). Directories without
  // an index, such as those of extensions, are imported eagerly.
  static QSet<QString> lazyDirectories;
  const QString directory = scopedCurrentDir.currentPath();
  if (lazyDirectories.contains(directory))
  {
    return true;
  }
  qSlicerCoreApplication* app = qSlicerCoreApplication::application();
  QSettings* settings = app ? app->userSettings() : nullptr;
  // On by default since the full sweep passed with it (work package W4-03): startup, visiting every
  // module, the slicer package tests, module self-tests and generic tests with --no-main-window.
  const bool lazyByDefault = true;
  if (settings && settings->value("Studio/LazyPythonWrappers", lazyByDefault).toBool())
  {
    const QString indexPath = QDir(directory).filePath("SlicerWrapperIndex.json");
    if (QFileInfo::exists(indexPath))
    {
      QVariant registered = pythonManager->executeString(QString("__import__('slicer.util').util._registerLazyWrapperIndex(%1, %2)")
                                                           .arg(qSlicerCorePythonManager::toPythonStringLiteral(directory))
                                                           .arg(qSlicerCorePythonManager::toPythonStringLiteral(indexPath)),
                                                         ctkAbstractPythonManager::EvalInput);
      // On failure (for example an unreadable index), fall back to the eager import below
      if (!pythonManager->pythonErrorOccured() && registered.toBool())
      {
        lazyDirectories.insert(directory);
        return true;
      }
    }
  }

  pythonManager->executeString(QString("from slicer.util import importVTKClassesFromDirectory;"
                                       "importVTKClassesFromDirectory(%1, 'slicer', filematch='vtkSlicer*ModuleLogicPython.*');"
                                       "importVTKClassesFromDirectory(%1, 'slicer', filematch='vtkSlicer*ModuleMRMLPython.*');"
                                       "importVTKClassesFromDirectory(%1, 'slicer', filematch='vtkSlicer*ModuleMRMLDisplayableManagerPython.*');"
                                       "importVTKClassesFromDirectory(%1, 'slicer', filematch='vtkSlicer*ModuleVTKWidgetsPython.*');")
                                 .arg(qSlicerCorePythonManager::toPythonStringLiteral(scopedCurrentDir.currentPath())));
  pythonManager->executeString(QString("from slicer.util import importQtClassesFromDirectory;"
                                       "importQtClassesFromDirectory(%1, 'slicer', filematch='qSlicer*PythonQt.*');")
                                 .arg(qSlicerCorePythonManager::toPythonStringLiteral(scopedCurrentDir.currentPath())));
  return !pythonManager->pythonErrorOccured();
}

//-----------------------------------------------------------------------------
qSlicerPythonCppAPI::qSlicerPythonCppAPI() = default;

//-----------------------------------------------------------------------------
qSlicerPythonCppAPI::~qSlicerPythonCppAPI()
{
  if (this->PythonSelf)
  {
    this->APIMethods.clear();
  }
}

//-----------------------------------------------------------------------------
QString qSlicerPythonCppAPI::objectName() const
{
  return this->ObjectName;
}

//-----------------------------------------------------------------------------
void qSlicerPythonCppAPI::setObjectName(const QString& name)
{
  this->ObjectName = name;
}

//-----------------------------------------------------------------------------
void qSlicerPythonCppAPI::declareMethod(int id, const char* name)
{
  if (!name)
  {
    return;
  }
  this->APIMethods[id] = QString(name);
}

//-----------------------------------------------------------------------------
PyObject* qSlicerPythonCppAPI::instantiateClass(QObject* cpp, const QString& className, PyObject* classToInstantiate)
{
  PyObject* wrappedThis = PythonQt::self()->priv()->wrapQObject(cpp);
  if (!wrappedThis)
  {
    PythonQt::self()->handleError();
    qCritical() << "qSlicerPythonCppAPI::instantiateClass"
                << " - [" << this->ObjectName << "]"
                << " - Failed to wrap" << cpp->metaObject()->className() << "associated with " << className;
    return nullptr;
  }

  PyObject* arguments = PyTuple_New(1);
  PyTuple_SET_ITEM(arguments, 0, wrappedThis);

  // Attempt to instantiate the associated python class
  PythonQtObjectPtr self;

  if (PyType_Check(classToInstantiate))
  {
    // New style class
    self.setNewRef(PyObject_Call(classToInstantiate, arguments, nullptr));
  }
  else
  {
    self.setNewRef(PyObject_CallObject(classToInstantiate, arguments));
  }

  Py_DECREF(arguments);

  if (!self)
  {
    PythonQt::self()->handleError();
    qCritical() << "qSlicerPythonCppAPI::instantiateClass"
                << " - [" << this->ObjectName << "]"
                << "- Failed to instantiate scripted pythonqt class" << className << classToInstantiate;
    return nullptr;
  }

  for (const int& methodId : this->APIMethods.keys())
  {
    QString methodName = this->APIMethods.value(methodId);
    if (!PyObject_HasAttrString(self.object(), methodName.toUtf8()))
    {
      continue;
    }
    PythonQtObjectPtr method;
    method.setNewRef(PyObject_GetAttrString(self.object(), methodName.toUtf8()));
    this->PythonAPIMethods[methodId] = method;
  }

  this->PythonSelf = self;

  return self.object();
}

//-----------------------------------------------------------------------------
PyObject* qSlicerPythonCppAPI::callMethod(int id, PyObject* arguments)
{
  if (!this->PythonAPIMethods.contains(id))
  {
    return nullptr;
  }
  PyObject* method = this->PythonAPIMethods.value(id).object();
  PythonQt::self()->clearError();
  PyObject* result = PyObject_CallObject(method, arguments);
  if (PythonQt::self()->handleError())
  {
    return nullptr;
  }
  return result;
}

//-----------------------------------------------------------------------------
PyObject* qSlicerPythonCppAPI::pythonSelf() const
{
  // returned object needs to get an extra ref count!
  // Without increasing the ref count, the reference count would be decreased
  // by one each time the object is passed to Python.
  // For example, each call of calling of
  // slicer.modules.endoscopy.widgetRepresentation().self()
  // would decrease the ref count of the Python widget class by one.
  Py_XINCREF(this->PythonSelf);
  return this->PythonSelf;
}
