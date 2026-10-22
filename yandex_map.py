"""Official Yandex JS API satellite layer in an in-memory Qt WebEngine profile."""
from model import RADAR_RATE
import json
import os
import threading
import time
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PyQt5.QtCore import QObject, QUrl, Qt, pyqtSignal, pyqtSlot
from labels import FIELDS, metric_lines, target_name
from model import destination
from markers import icon_kind, web_icons
from coordinates import coordinate_text
from dashboard import region_bounds, radar_bounds

ROOT = Path(__file__).resolve().parent


def api_key():
    value = os.environ.get('YANDEX_MAPS_API_KEY', '').strip()
    if value:
        return value
    try:
        return json.loads((ROOT / 'config.local.json').read_text(encoding='utf-8-sig')).get('yandex_api_key', '').strip()
    except (OSError, ValueError, AttributeError):
        return ''


def snapshot(canvas):
    targets = []
    for track_id, history in canvas.tracks.active_history().items():
        s = history[-1]
        selected = track_id == canvas.selected_id
        fields = FIELDS if selected and canvas.selected_card else canvas.label_fields
        targets.append(dict(id=track_id, name=target_name(track_id), position=[s.lat, s.lon], course=s.course_deg or 0,
            manual_color=canvas.target_marks.get(track_id, ''),
            shape='auto', icon=icon_kind(s), selected=selected,
            stale=not canvas.paused and time.time() - s.timestamp > 10, metrics=metric_lines(s, fields),
            short=canvas.tail_mode == 'short',
            tail=[[p.lat, p.lon] for p in canvas.tracks.tail(track_id, canvas.tail_mode)]))
    sector = [list(canvas.antenna)] + [list(destination(*canvas.antenna, canvas.azimuth + a, canvas.radius_m))
                                     for a in range(-30, 31, 2)] + [list(canvas.antenna)]
    return dict(antenna=list(canvas.antenna), targets=targets, sector=sector,
                azimuth=canvas.azimuth, rotating=not canvas.paused, rotation_rate=RADAR_RATE,
                radius_m=canvas.radius_m, theme=canvas.theme,
                ring_step_m=canvas.ring_step_m, main_bounds=radar_bounds(*canvas.antenna, canvas.radius_m),
                pinned_regions=[dict(r, bounds=region_bounds(r['lat'], r['lon'])) if r else None for r in canvas.pinned_regions],
                active_region=canvas.active_region, live_only=False, show_previews=canvas.show_previews)


class Bridge(QObject):
    selected = pyqtSignal(str)
    region_clicked = pyqtSignal(float, float)
    region_activated = pyqtSignal(int)
    region_cleared = pyqtSignal(int)
    loaded = pyqtSignal()
    failed = pyqtSignal(str)

    @pyqtSlot(str)
    def select(self, track_id):
        self.selected.emit(track_id)

    @pyqtSlot(float, float)
    def pinRegion(self, lat, lon):
        self.region_clicked.emit(lat, lon)

    @pyqtSlot(int)
    def activateRegion(self, index):
        self.region_activated.emit(index)

    @pyqtSlot(int)
    def clearRegion(self, index):
        self.region_cleared.emit(index)

    @pyqtSlot()
    def ready(self):
        self.loaded.emit()

    @pyqtSlot(str)
    def error(self, message):
        self.failed.emit(message[:200])


def create_view(parent, key):
    # Astra uses its system Qt libraries. Optional bindings are extracted locally;
    # we never prepend the failed PyPI build or replace any system Qt files.
    import PyQt5
    local_bindings = ROOT / '.qt-debian/usr/lib/python3/dist-packages/PyQt5'
    if local_bindings.exists() and str(local_bindings) not in PyQt5.__path__:
        PyQt5.__path__.append(str(local_bindings))
    from PyQt5.QtWebChannel import QWebChannel
    from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEnginePage, QWebEngineProfile

    class SatelliteView(QWebEngineView):
        def __init__(self):
            super().__init__(parent)
            self.setContextMenuPolicy(Qt.NoContextMenu)
            self.ready = False
            self.active = False
            self.generation = 0
            self.api_warning = ''
            self.latest = None
            self.bridge = Bridge(self)
            self.profile = QWebEngineProfile(self)  # Off the record, no disk tile cache.
            self.profile.setHttpCacheType(QWebEngineProfile.MemoryHttpCache)
            self.profile.setPersistentCookiesPolicy(QWebEngineProfile.NoPersistentCookies)
            owner = self

            class MapPage(QWebEnginePage):
                def javaScriptConsoleMessage(page, level, message, line, source):
                    if '(Yandex Maps JS API): Invalid API key' in message:
                        owner.api_warning = 'Яндекс: Invalid API key.\nСнимки отображаются; проверьте активацию ключа и переподключите карту.'
                        owner.bridge.failed.emit(owner.api_warning)
                    else:
                        super().javaScriptConsoleMessage(level, message, line, source)

            self.setPage(MapPage(self.profile, self))
            self.channel = QWebChannel(self.page())
            self.channel.registerObject('bridge', self.bridge)
            self.page().setWebChannel(self.channel)
            self.bridge.loaded.connect(self.on_ready)
            html = (ROOT / 'yandex_map.html').read_text(encoding='utf-8')
            html = html.replace('__ICONS_MARKUP__', web_icons())
            html = html.replace('__API_KEY_JSON__', json.dumps(key).replace('<', '\\u003c'))
            payload = html.encode('utf-8')

            class Handler(BaseHTTPRequestHandler):
                def do_GET(handler):
                    if urlsplit(handler.path).path != '/':
                        handler.send_error(404)
                        return
                    handler.send_response(200)
                    handler.send_header('Content-Type', 'text/html; charset=utf-8')
                    handler.send_header('Cache-Control', 'no-store')
                    handler.send_header('Content-Length', str(len(payload)))
                    handler.end_headers()
                    handler.wfile.write(payload)

                def log_message(handler, *args):
                    pass

            self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            self.server.daemon_threads = True
            threading.Thread(target=self.server.serve_forever, daemon=True).start()
            self.url = QUrl(f'http://localhost:{self.server.server_port}/')

        def activate(self):
            if not self.active:
                self.active = True
                self.api_warning = ''
                self.ready = False
                self.generation += 1
                # A unique URL forces a new document even if about:blank is
                # still pending after a rapid offline/online toggle in Qt.
                self.setUrl(QUrl(self.url.toString() + f'?session={self.generation}'))

        def deactivate(self):
            self.active = False
            self.ready = False
            self.stop()
            self.setUrl(QUrl('about:blank'))
            self.profile.clearHttpCache()

        def on_ready(self):
            self.ready = True
            if self.latest:
                self.send(self.latest)

        def send(self, data):
            self.latest = data
            if self.ready:
                self.page().runJavaScript('window.renderSnapshot(' + json.dumps(data, allow_nan=False) + ')')

        def focus_antenna(self):
            if self.ready and self.latest:
                self.page().runJavaScript('window.focusAntenna()')

        def zoom_by(self, delta):
            if self.ready:
                self.page().runJavaScript(f'window.zoomBy({int(delta)})')

        def shutdown(self):
            # Destruction must not race asynchronous navigation/cache clearing.
            # The off-the-record profile discards its cache when destroyed.
            self.active = False
            self.ready = False
            self.stop()
            self.server.shutdown()
            self.server.server_close()
            from PyQt5 import sip
            sip.delete(self.page())
            sip.delete(self.profile)

    return SatelliteView()
