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

==============================================================================*/

#ifndef __qMRMLSymbolicIconEngine_h
#define __qMRMLSymbolicIconEngine_h

// Qt includes
#include <QIcon>
#include <QIconEngine>
#include <QString>

// qMRMLWidget includes
#include "qMRMLWidgetsExport.h"

class QPalette;

/// \brief Icon engine that draws monochrome SVG symbols tinted with the application palette.
///
/// The symbols are the Lucide icons vendored in qMRMLWidgets.qrc (":/Icons/Lucide/<name>.svg").
/// Their "currentColor" is replaced by a color derived from the application palette:
/// - Normal mode: ButtonText at 85% opacity;
/// - Active mode (hovered) and Selected mode: ButtonText;
/// - Disabled mode: the ButtonText color of the Disabled color group;
/// - On state (checked buttons, unless disabled): the "accent-fg" token of the application style when the
///   style provides one (Slicer styles do), otherwise Highlight.
///
/// The symbol is rendered with QSvgRenderer at the device pixel size, so icons stay crisp on high-DPI screens,
/// and follow the palette when the theme changes (pixmaps are cached by name, size, mode, state and palette).
///
/// An icon can show a different symbol in the On state (for example "unlink" when off and "link" when on).
class QMRML_WIDGETS_EXPORT qMRMLSymbolicIconEngine : public QIconEngine
{
public:
  /// Create an engine for the symbol \a offName, and \a onName in the On state (\a offName if empty).
  explicit qMRMLSymbolicIconEngine(const QString& offName, const QString& onName = QString());
  qMRMLSymbolicIconEngine(const qMRMLSymbolicIconEngine& other);
  ~qMRMLSymbolicIconEngine() override;

  /// Return a palette-tinted icon of the symbol \a name (for example "link").
  /// \sa hasSymbol()
  static QIcon icon(const QString& name);
  /// Return a palette-tinted icon that shows \a offName in the Off state and \a onName in the On state.
  static QIcon icon(const QString& offName, const QString& onName);

  /// Return true if the symbol \a name is available.
  static bool hasSymbol(const QString& name);

  /// Return the color of the symbols drawn in \a mode and \a state with \a palette.
  static QColor symbolColor(const QPalette& palette, QIcon::Mode mode, QIcon::State state);

  void paint(QPainter* painter, const QRect& rect, QIcon::Mode mode, QIcon::State state) override;
  QPixmap pixmap(const QSize& size, QIcon::Mode mode, QIcon::State state) override;
#if QT_VERSION >= QT_VERSION_CHECK(6, 0, 0)
  QPixmap scaledPixmap(const QSize& size, QIcon::Mode mode, QIcon::State state, qreal scale) override;
#endif
  QIconEngine* clone() const override;
  QString key() const override;

protected:
  /// Return the pixmap of the symbol rendered at \a pixelSize device pixels (device pixel ratio 1).
  QPixmap renderPixmap(const QSize& pixelSize, QIcon::Mode mode, QIcon::State state) const;

  QString OffName;
  QString OnName;

private:
  qMRMLSymbolicIconEngine& operator=(const qMRMLSymbolicIconEngine&) = delete;
};

#endif
