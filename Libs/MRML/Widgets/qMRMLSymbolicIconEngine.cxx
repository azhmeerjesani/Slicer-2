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

// Qt includes
#include <QApplication>
#include <QFile>
#include <QHash>
#include <QImage>
#include <QMetaObject>
#include <QPainter>
#include <QPalette>
#include <QPixmap>
#include <QPixmapCache>
#include <QStyle>
#include <QSvgRenderer>

// qMRML includes
#include "qMRMLSymbolicIconEngine.h"

namespace
{
//-----------------------------------------------------------------------------
QString symbolPath(const QString& name)
{
  return QString(":/Icons/Lucide/%1.svg").arg(name);
}

//-----------------------------------------------------------------------------
/// Return the SVG source of the symbol \a name (read once), or an empty array if it does not exist.
QByteArray symbolSource(const QString& name)
{
  static QHash<QString, QByteArray> sources;
  QHash<QString, QByteArray>::const_iterator it = sources.constFind(name);
  if (it != sources.constEnd())
  {
    return it.value();
  }
  QByteArray source;
  QFile file(symbolPath(name));
  if (file.open(QIODevice::ReadOnly))
  {
    source = file.readAll();
  }
  sources.insert(name, source);
  return source;
}

//-----------------------------------------------------------------------------
/// Return the "accent-fg" token of the application style, or an invalid color if the style has no tokens.
QColor styleAccentColor()
{
  QStyle* style = QApplication::style();
  if (!style || style->metaObject()->indexOfMethod("token(QString)") < 0)
  {
    return QColor();
  }
  QColor color;
  QMetaObject::invokeMethod(style, "token", Qt::DirectConnection, Q_RETURN_ARG(QColor, color), Q_ARG(QString, QString("accent-fg")));
  return color;
}
} // namespace

//-----------------------------------------------------------------------------
qMRMLSymbolicIconEngine::qMRMLSymbolicIconEngine(const QString& offName, const QString& onName)
  : OffName(offName)
  , OnName(onName.isEmpty() ? offName : onName)
{
}

//-----------------------------------------------------------------------------
qMRMLSymbolicIconEngine::qMRMLSymbolicIconEngine(const qMRMLSymbolicIconEngine& other)
  : QIconEngine(other)
  , OffName(other.OffName)
  , OnName(other.OnName)
{
}

//-----------------------------------------------------------------------------
qMRMLSymbolicIconEngine::~qMRMLSymbolicIconEngine() = default;

//-----------------------------------------------------------------------------
QIcon qMRMLSymbolicIconEngine::icon(const QString& name)
{
  return QIcon(new qMRMLSymbolicIconEngine(name));
}

//-----------------------------------------------------------------------------
QIcon qMRMLSymbolicIconEngine::icon(const QString& offName, const QString& onName)
{
  return QIcon(new qMRMLSymbolicIconEngine(offName, onName));
}

//-----------------------------------------------------------------------------
bool qMRMLSymbolicIconEngine::hasSymbol(const QString& name)
{
  return !symbolSource(name).isEmpty();
}

//-----------------------------------------------------------------------------
QColor qMRMLSymbolicIconEngine::symbolColor(const QPalette& palette, QIcon::Mode mode, QIcon::State state)
{
  if (mode == QIcon::Disabled)
  {
    return palette.color(QPalette::Disabled, QPalette::ButtonText);
  }
  if (state == QIcon::On)
  {
    QColor accent = styleAccentColor();
    return accent.isValid() ? accent : palette.color(QPalette::Active, QPalette::Highlight);
  }
  QColor color = palette.color(QPalette::Active, QPalette::ButtonText);
  if (mode == QIcon::Normal)
  {
    color.setAlphaF(color.alphaF() * 0.85);
  }
  return color;
}

//-----------------------------------------------------------------------------
QPixmap qMRMLSymbolicIconEngine::renderPixmap(const QSize& pixelSize, QIcon::Mode mode, QIcon::State state) const
{
  if (pixelSize.isEmpty())
  {
    return QPixmap();
  }
  const QString& name = (state == QIcon::On) ? this->OnName : this->OffName;
  const QPalette palette = QApplication::palette();
  const QString cacheKey = QString("qMRMLSymbolicIcon:%1:%2x%3:%4:%5:%6")
                             .arg(name)
                             .arg(pixelSize.width())
                             .arg(pixelSize.height())
                             .arg(static_cast<int>(mode))
                             .arg(static_cast<int>(state))
                             .arg(palette.cacheKey());
  QPixmap pixmap;
  if (QPixmapCache::find(cacheKey, &pixmap))
  {
    return pixmap;
  }

  QImage image(pixelSize, QImage::Format_ARGB32_Premultiplied);
  image.fill(Qt::transparent);
  QByteArray source = symbolSource(name);
  if (!source.isEmpty())
  {
    QColor color = qMRMLSymbolicIconEngine::symbolColor(palette, mode, state);
    source.replace("currentColor", color.name(QColor::HexRgb).toLatin1());
    QSvgRenderer renderer(source);
    if (renderer.isValid())
    {
      // The symbols are square: center them in non-square sizes.
      const int side = qMin(pixelSize.width(), pixelSize.height());
      const QRectF target((pixelSize.width() - side) / 2., (pixelSize.height() - side) / 2., side, side);
      QPainter painter(&image);
      painter.setRenderHint(QPainter::Antialiasing);
      painter.setOpacity(color.alphaF());
      renderer.render(&painter, target);
    }
  }
  pixmap = QPixmap::fromImage(image);
  QPixmapCache::insert(cacheKey, pixmap);
  return pixmap;
}

//-----------------------------------------------------------------------------
void qMRMLSymbolicIconEngine::paint(QPainter* painter, const QRect& rect, QIcon::Mode mode, QIcon::State state)
{
  if (!painter || rect.isEmpty())
  {
    return;
  }
  const qreal devicePixelRatio = painter->device() ? painter->device()->devicePixelRatioF() : 1.;
  QPixmap pixmap = this->renderPixmap(rect.size() * devicePixelRatio, mode, state);
  pixmap.setDevicePixelRatio(devicePixelRatio);
  painter->drawPixmap(rect, pixmap);
}

//-----------------------------------------------------------------------------
QPixmap qMRMLSymbolicIconEngine::pixmap(const QSize& size, QIcon::Mode mode, QIcon::State state)
{
  // Qt 5 QIcon::pixmap() requests the size in device pixels and sets the device pixel ratio itself.
  return this->renderPixmap(size, mode, state);
}

#if QT_VERSION >= QT_VERSION_CHECK(6, 0, 0)
//-----------------------------------------------------------------------------
QPixmap qMRMLSymbolicIconEngine::scaledPixmap(const QSize& size, QIcon::Mode mode, QIcon::State state, qreal scale)
{
  // Qt 6 requests the size in device-independent pixels and the scale separately.
  QPixmap pixmap = this->renderPixmap(size * scale, mode, state);
  pixmap.setDevicePixelRatio(scale);
  return pixmap;
}
#endif

//-----------------------------------------------------------------------------
QIconEngine* qMRMLSymbolicIconEngine::clone() const
{
  return new qMRMLSymbolicIconEngine(*this);
}

//-----------------------------------------------------------------------------
QString qMRMLSymbolicIconEngine::key() const
{
  return QString("qMRMLSymbolicIconEngine");
}
