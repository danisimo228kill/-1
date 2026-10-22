"""Offline tiles with background rendering and a bounded visible-tile queue."""
from collections import OrderedDict
import atexit
from queue import SimpleQueue
from pathlib import Path
from PyQt5.QtCore import QObject, QTimer, QThread, QCoreApplication, pyqtSignal
from PyQt5.QtGui import QPixmap, QImage, QPainter, QColor
from model import MBTiles

DEFAULT_MAP = Path(__file__).parent / 'data' / 'spb-north-osm.mbtiles'
DETAIL_ZOOM = 12
_RENDERER = None


def contains(bounds, lat, lon):
    west, south, east, north = bounds
    return south <= lat <= north and (west <= lon <= east if west <= east else lon >= west or lon <= east)


def render_payload(payload, key):
    blob, vector, parent_z = payload
    z, x, y, theme = key
    scale = 2 ** (z-parent_z)
    if vector:
        from vector_map import render_image
        return render_image(blob, z, theme, (z-parent_z, x%scale, y%scale))
    image = QImage.fromData(blob)
    if scale > 1:
        image = image.copy(x%scale*256//scale, y%scale*256//scale,
                           max(1,256//scale), max(1,256//scale)).scaled(256,256)
    if theme != 'day' and not image.isNull():
        image.invertPixels()
        painter = QPainter(image)
        painter.fillRect(image.rect(), QColor(18,32,48,100 if theme=='night' else 65))
        painter.end()
    return image


class TileRenderer(QThread):
    """A Qt-owned thread also releases Qt font resources before application exit."""
    def __init__(self):
        super().__init__()
        self.tasks = SimpleQueue()

    def run(self):
        while not self.isInterruptionRequested():
            task = self.tasks.get()
            if task is None:return
            owner,payload,key = task
            if owner._closed:continue
            try:image = render_payload(payload,key)
            except Exception:image = QImage()
            if not owner._closed:
                try:owner._completed.emit(key,image)
                except RuntimeError:pass

    def stop(self):
        self.requestInterruption();self.tasks.put(None);self.wait()


def stop_renderer():
    if _RENDERER is not None:_RENDERER.stop()


atexit.register(stop_renderer)


class OfflineMap(QObject):
    ready = pyqtSignal()
    _completed = pyqtSignal(object, object)

    def __init__(self, path=DEFAULT_MAP, async_render=False):
        super().__init__()
        from vector_map import VectorArchive
        self.archive = VectorArchive(path) if Path(path).suffix=='.pmtiles' else MBTiles(path)
        if self.archive.metadata.get('kontur_download_complete') == 'false':
            self.archive.close()
            raise ValueError('Пакет карты не завершён')
        overview = Path(__file__).parent / 'data' / 'regions' / 'russia-overview.pmtiles'
        self.fallback = VectorArchive(overview) if overview.exists() and Path(path).resolve()!=overview.resolve() else None
        self.bounds = self.fallback.bounds if self.fallback else self.archive.bounds
        self.additional = []
        for candidate in sorted(overview.parent.glob('*.pmtiles')):
            if candidate.resolve() in (Path(path).resolve(),overview.resolve()):continue
            try:
                archive = VectorArchive(candidate)
                if archive.max_zoom >= DETAIL_ZOOM:self.additional.append(archive)
                else:archive.close()
            except (OSError,ValueError,KeyError,IndexError):continue
        self.layers = sorted([a for a in (self.archive,*self.additional,self.fallback) if a],
                             key=lambda a:a.max_zoom, reverse=True)
        self.cache = OrderedDict()
        self.theme = 'night'
        self.async_render = async_render
        self.revision = 0
        self._closed = False
        self._views = {}
        self._pending = OrderedDict()
        self._active = None
        self._completed.connect(self._accept)
        if async_render:
            QCoreApplication.instance().aboutToQuit.connect(self.close)

    def begin_view(self, scope):
        self._views[scope] = set()

    def end_view(self, scope):
        needed = set().union(*self._views.values()) if self._views else set()
        for key in list(self._pending):
            if key not in needed:self._pending.pop(key,None)

    def _payload(self, key):
        z,x,y,_ = key
        for archive in self.layers:
            parent_z = min(z,archive.max_zoom,DETAIL_ZOOM)
            scale = 2 ** (z-parent_z)
            blob = archive.tile(parent_z,x//scale,y//scale)
            if blob:return blob,archive.metadata.get('format')=='pbf',parent_z
        return None

    def _store(self, key, pixmap):
        self.cache[key] = pixmap
        self.cache.move_to_end(key)
        if len(self.cache)>384:self.cache.popitem(last=False)
        self.revision += 1

    def tile(self, z, x, y, scope='direct'):
        key = z,x%2**z,y,self.theme
        if self._closed:return QPixmap()
        if self.async_render:self._views.setdefault(scope,set()).add(key)
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        if not self.async_render:
            payload = self._payload(key)
            self._store(key,QPixmap.fromImage(render_payload(payload,key)) if payload else QPixmap())
            return self.cache[key]
        if key != self._active and key not in self._pending:
            self._pending[key] = None
            if len(self._pending)>192:self._pending.popitem(last=False)
            QTimer.singleShot(0,self._pump)
        # Keep existing geography visible until the sharper tile is ready.
        for parent_z in range(z-1,max(-1,z-7),-1):
            scale = 2 ** (z-parent_z)
            parent = self.cache.get((parent_z,key[1]//scale,y//scale,self.theme))
            if parent is not None and not parent.isNull():
                return parent.copy(key[1]%scale*256//scale,y%scale*256//scale,
                                   max(1,256//scale),max(1,256//scale)).scaled(256,256)
        return QPixmap()

    def _pump(self):
        global _RENDERER
        if self._closed or self._active is not None or not self._pending:return
        key,_ = self._pending.popitem(last=False)
        payload = self._payload(key)
        if payload is None:
            self._store(key,QPixmap());self.ready.emit()
            QTimer.singleShot(0,self._pump)
            return
        self._active = key
        if _RENDERER is None:
            _RENDERER = TileRenderer()
            QCoreApplication.instance().aboutToQuit.connect(stop_renderer)
            _RENDERER.start()
        _RENDERER.tasks.put((self,payload,key))

    def _accept(self, key, image):
        if self._closed:return
        self._active = None
        self._store(key,QPixmap.fromImage(image))
        self.ready.emit()
        QTimer.singleShot(0,self._pump)

    def close(self):
        if self._closed:return
        self._closed = True
        self._pending.clear();self._views.clear()
        # Workers hold copied bytes, never archives or GUI pixmaps.
        self.archive.close()
        if self.fallback:self.fallback.close()
        for archive in self.additional:archive.close()
        self.cache.clear()
