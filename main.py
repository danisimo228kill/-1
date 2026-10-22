"""Astra desktop visualization prototype. Launch with python3 main.py."""
import csv
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from PyQt5.QtCore import Qt, QTimer, QPointF, QRectF, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen, QPolygonF, QPainterPath, QFont, QFontDatabase, QWheelEvent, QPixmap, QIcon
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QPushButton, QDoubleSpinBox, QComboBox, QFormLayout,
    QGroupBox, QTableWidget, QTableWidgetItem, QHeaderView, QFileDialog,
    QMessageBox, QDialog, QAbstractItemView, QScrollArea, QCheckBox, QStackedWidget, QSizePolicy, QTabWidget, QDialogButtonBox)
from model import TrafficSource, Tracks, world_pixel, latlon_from_pixel, destination, RADAR_RATE
from labels import FIELDS, metric_lines, target_name, target_table_name, MARK_NAMES
from yandex_map import api_key, create_view, snapshot
from offline_map import OfflineMap, DEFAULT_MAP, contains
from markers import icon_kind, draw_icon, display_type

from dashboard import DashboardMixin, clock_text, region_bounds, radar_bounds, MAIN_RADIUS_M
from coordinates import WGS84, SK42, NAMES, convert, coordinate_text, bearing_sector, scale_bar
from region_maps import find_region, RegionDownload, DETAIL_ZOOM

MARK_COLORS = {'green': '#36c96c', 'red': '#ff5656', 'blue': '#448dff'}

CYAN = QColor('#59dbdf')
GOLD = QColor('#ffca72')


class PanelWheelMixin:
    def wheelEvent(self, event):
        parent = self.parentWidget()
        while parent is not None:
            if isinstance(parent, QScrollArea):
                viewport = parent.viewport()
                forwarded = QWheelEvent(QPointF(viewport.mapFromGlobal(event.globalPos())), event.globalPosF(),
                    event.pixelDelta(), event.angleDelta(), event.buttons(), event.modifiers(),
                    event.phase(), event.inverted())
                QApplication.sendEvent(viewport, forwarded)
                event.accept()
                return
            parent = parent.parentWidget()
        event.ignore()


class NoWheelSpinBox(PanelWheelMixin, QDoubleSpinBox):
    pass


class NoWheelComboBox(PanelWheelMixin, QComboBox):
    def __init__(self, *args):
        super().__init__(*args)
        self.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(12)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)


class MapCanvas(QWidget):
    selected = pyqtSignal(str)
    changed = pyqtSignal()
    map_ready = pyqtSignal()
    region_clicked = pyqtSignal(float, float)

    def __init__(self, tracks):
        super().__init__()
        self.tracks = tracks
        self.lat, self.lon, self.zoom = 60.022612, 30.293248, 11
        self.antenna = (60.022612, 30.293248)
        self.azimuth = 0
        self.sector_width = 60
        self.radius_m = MAIN_RADIUS_M
        self.ring_step_m = 10000
        self.auto_fit = False
        self.selected_id = None
        self.paused = False
        self.tail_mode = 'short'
        self.label_fields = set()
        self.selected_card = True
        self.target_marks = {}
        self.theme = 'night'
        self.display_crs = WGS84
        self.pinned_regions = [None, None, None]
        self.active_region = 0
        self.live_only = False
        self.show_previews = False
        self.offline = None
        self.credit = QLabel(self)
        self.credit.setText('<a style="color:#bacbd7" href="https://www.openstreetmap.org/copyright">© OpenStreetMap contributors · ODbL</a>')
        self.credit.setOpenExternalLinks(True)
        self.credit.setStyleSheet('background:#101f2b; padding:3px; font-size:11px;')
        self.credit.hide()
        self.hit_positions = {}
        self.label_rects = {}
        self.drag_pos = None
        self.press_pos = None
        self.setMinimumSize(240, 240)
        self.setMouseTracking(True)
        self.zoom_buttons = {}
        for index, (title, delta) in enumerate((('+', 1), ('−', -1))):
            button = QPushButton(title, self)
            button.setObjectName('mapZoomButton')
            button.setFixedSize(38, 38)
            button.move(16, 48 + index*42)
            button.setToolTip('Приблизить карту' if delta > 0 else 'Отдалить карту')
            button.setAccessibleName(button.toolTip())
            button.clicked.connect(lambda checked=False, d=delta: self.set_zoom(self.zoom+d))
            self.zoom_buttons[delta] = button

    def point(self, lat, lon):
        cx, cy = world_pixel(self.lat, self.lon, self.zoom)
        px, py = world_pixel(lat, lon, self.zoom)
        size = 256 * 2 ** self.zoom
        dx = (px - cx + size / 2) % size - size / 2
        return QPointF(self.width() / 2 + dx, self.height() / 2 + py - cy)

    def focus_antenna(self):
        self.lat, self.lon = self.antenna
        self.auto_fit = True
        self.fit_radar()
        self.update()

    def fit_radar(self):
        bounds = radar_bounds(*self.antenna, self.radius_m)
        cx, cy = world_pixel(*self.antenna, 0)
        x0, y0 = world_pixel(bounds[0][0], bounds[0][1], 0)
        x1, y1 = world_pixel(bounds[1][0], bounds[1][1], 0)
        span_x = 2 * max(abs(x0-cx), abs(x1-cx))
        span_y = 2 * max(abs(y0-cy), abs(y1-cy))
        self.zoom = max(2, min(18, math.log2(min(max(1,self.width()-64)/span_x,
                                                            max(1,self.height()-64)/span_y))))

    def set_zoom(self, zoom):
        self.auto_fit = False
        self.zoom = max(2, min(19, zoom))
        self.update()
        self.changed.emit()

    def load_offline(self, path=DEFAULT_MAP):
        layer = OfflineMap(path, async_render=True)
        layer.ready.connect(self.map_ready)
        if self.offline:
            self.offline.close()
        self.offline = layer
        self.offline.theme = self.theme
        self.offline_path = Path(path)
        if self.auto_fit:
            self.fit_radar()
        self.credit.show()
        self.credit.adjustSize()
        self.credit.move(max(0, self.width()-self.credit.width()-8), self.height()-28)

    def coverage_rect(self):
        west, south, east, north = self.offline.bounds
        if east-west >= 359:
            return QRectF(0, self.point(north, self.lon).y(), self.width(),
                          self.point(south, self.lon).y()-self.point(north, self.lon).y())
        top_left = self.point(north, west)
        bottom_right = self.point(south, east)
        if bottom_right.x() < top_left.x():
            bottom_right.setX(bottom_right.x() + 256 * 2 ** self.zoom)
        return QRectF(top_left, bottom_right)

    def resizeEvent(self, event):
        if self.auto_fit:
            self.fit_radar()
        self.credit.adjustSize()
        self.credit.move(max(0, self.width()-self.credit.width()-8), self.height()-28)
        super().resizeEvent(event)

    def paint_basemap(self, p):
        if self.offline:self.offline.begin_view("main")
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor('#edf0df' if self.theme == 'day' else '#182733'))
        tile_zoom = max(0, min(19, math.ceil(self.zoom)))
        factor = 2 ** (self.zoom-tile_zoom)
        cx, cy = world_pixel(self.lat, self.lon, tile_zoom)
        left, top = cx - self.width() / (2*factor), cy - self.height() / (2*factor)
        right, bottom = left+self.width()/factor, top+self.height()/factor
        p.setPen(QPen(QColor('#243846'), 1))
        p.save()
        if self.offline and self.offline.bounds:
            west, south, east, north = self.offline.bounds
            p.setClipRect(self.coverage_rect())
            bx0, by0 = world_pixel(north, west, tile_zoom)
            bx1, by1 = world_pixel(south, east, tile_zoom)
            if east-west >= 359:
                bx0, bx1 = left, right
        else:
            bx0, by0, bx1, by1 = left, top, right, bottom
        for x in range(math.floor(max(left,bx0) / 256), math.floor(min(right,bx1) / 256) + 1):
            for y in range(math.floor(max(top,by0) / 256), math.floor(min(bottom,by1) / 256) + 1):
                rect = QRectF((x * 256 - left)*factor, (y * 256 - top)*factor, 256*factor, 256*factor)
                tile = self.offline.tile(tile_zoom, x, y, scope="main") if self.offline else None
                if tile is not None and not tile.isNull():
                    p.drawPixmap(rect, tile, QRectF(tile.rect()))
                else:
                    p.drawRect(rect)
        p.restore()
        if self.offline:self.offline.end_view("main")

    def paintEvent(self, event):
        p = QPainter(self)
        key = (self.width(),self.height(),self.lat,self.lon,self.zoom,self.theme,
               id(self.offline),self.offline.revision if self.offline else 0)
        if getattr(self,'_basemap_key',None) != key:
            self._basemap = QPixmap(self.size())
            painter = QPainter(self._basemap)
            self.paint_basemap(painter)
            painter.end()
            self._basemap_key = key
        p.drawPixmap(0,0,self._basemap)
        if self.offline and self.offline.bounds:
            west, south, east, north = self.offline.bounds
            p.setPen(QPen(QColor('#6f8b9b'), 1, Qt.DashLine))
            p.drawRect(self.coverage_rect())
        p.setRenderHint(QPainter.Antialiasing)
        antenna = self.point(*self.antenna)
        p.save()
        # Demonstration coverage sector, unrelated to actual sensor detections.
        polygon = QPolygonF([antenna])
        for angle in range(-30, 31, 2):
            polygon.append(self.point(*destination(*self.antenna, self.azimuth + angle, self.radius_m)))
        polygon.append(antenna)
        p.setBrush(QColor(89, 219, 223, 24))
        p.setPen(QPen(QColor(89, 219, 223, 100), 1))
        p.drawPolygon(polygon)
        p.setBrush(Qt.NoBrush)
        for angle in range(0, 360, 30):
            edge = self.point(*destination(*self.antenna, angle, self.radius_m))
            p.setPen(QPen(QColor(89, 219, 223, 125), 1, Qt.DashLine))
            p.drawLine(antenna, edge)
            label = self.point(*destination(*self.antenna, angle, self.radius_m*.92))
            p.setPen(QColor('#174b60' if self.theme == 'day' else '#edf5fa'))
            p.drawText(label, f'{angle:03d}°')
        for radius in range(self.ring_step_m, int(self.radius_m)+1, self.ring_step_m):
            ring = QPainterPath()
            for angle in range(0, 361, 4):
                point = self.point(*destination(*self.antenna, angle, radius))
                if angle == 0:
                    ring.moveTo(point)
                else:
                    ring.lineTo(point)
            p.setPen(QPen(QColor(89, 219, 223, 65), 1, Qt.DashLine))
            p.drawPath(ring)
            label = self.point(*destination(*self.antenna, 45, radius))
            p.setPen(QColor('#174b60' if self.theme == 'day' else '#edf5fa'))
            p.drawText(label, f'{radius/1000:g} км')
        p.restore()
        p.save()
        self.hit_positions = {}
        for track_id, history in self.tracks.active_history().items():
            sample = history[-1]
            stale = not self.paused and time.time() - sample.timestamp > 10
            color = QColor(MARK_COLORS[self.target_marks[track_id]]) if track_id in self.target_marks else QColor('#78909c') if stale else GOLD if track_id == self.selected_id else QColor('#007487') if self.theme == 'day' else CYAN
            points = self.tracks.tail(track_id, self.tail_mode)
            if points:
                p.setBrush(Qt.NoBrush)
                path = QPainterPath(self.point(points[0].lat, points[0].lon))
                for item in points[1:]:
                    path.lineTo(self.point(item.lat, item.lon))
                p.setPen(QPen(color, 2 if track_id == self.selected_id else 1.3))
                p.drawPath(path)
                for item in (points[:-1] if self.tail_mode == 'short' else []):
                    pos = self.point(item.lat, item.lon)
                    p.setBrush(color)
                    p.drawEllipse(pos, 2, 2)
            pos = self.point(sample.lat, sample.lon)
            self.hit_positions[track_id] = pos
            p.setPen(QPen(color, 2))
            p.setBrush(QColor('#101f2b'))
            if track_id == self.selected_id:
                p.drawEllipse(pos, 14, 14)
            p.setBrush(color)
            p.save()
            p.translate(pos)
            kind = icon_kind(sample)
            p.rotate(0 if kind == 'tower' else sample.course_deg or 0)
            draw_icon(p, kind)
            p.restore()
        # Draw labels after all paths/markers so traces cannot cover the cards.
        self.label_rects = {}
        # Selected card gets first choice; others move around it to reduce overlap.
        ordered = sorted(self.tracks.active_ids, key=lambda key: key != self.selected_id)
        occupied = [QRectF(pos.x()-13, pos.y()-13, 26, 26) for pos in self.hit_positions.values()]
        for track_id in ordered:
            s = self.tracks.history[track_id][-1]
            selected = track_id == self.selected_id
            color = QColor(MARK_COLORS[self.target_marks[track_id]]) if track_id in self.target_marks else GOLD if selected else CYAN
            pos = self.hit_positions[track_id]
            fields = FIELDS if selected and self.selected_card else self.label_fields
            metrics = metric_lines(s, fields)
            mark = self.target_marks.get(track_id)
            lines = [target_name(track_id) + (f' · {MARK_NAMES[mark]}' if mark else '')] + ([' · '.join(metrics)] if metrics else [])
            fm = p.fontMetrics()
            width = max(fm.horizontalAdvance(line) for line in lines) + 16
            height = len(lines) * fm.height() + 10
            if not self.rect().adjusted(-10, -10, 10, 10).contains(pos.toPoint()):
                continue
            candidates = []
            label_area = QRectF(self.rect()).adjusted(3, 35, -3, -36)
            for dx, dy in [(-width / 2, -height - 18), (18, -height / 2), (-width - 18, -height / 2),
                           (-width / 2, 18), (-width / 2, -height - 60)]:
                x = max(label_area.left(), min(label_area.right() - width, pos.x() + dx))
                y = max(label_area.top(), min(label_area.bottom() - height, pos.y() + dy))
                candidates.append(QRectF(x, y, width, height))
            rect = min(candidates, key=lambda r: sum(r.intersected(old).width() * r.intersected(old).height()
                        for old in occupied if r.intersects(old)))
            occupied.append(rect.adjusted(-4, -4, 4, 4))
            self.label_rects[track_id] = rect
            p.setPen(QPen(QColor(140, 170, 185, 110), 1))
            p.drawLine(pos, QPointF(rect.center().x(), rect.bottom()))
            p.setPen(QPen(color if selected else QColor('#466073'), 1))
            p.setBrush(QColor(16, 31, 43, 235))
            p.drawRoundedRect(rect, 4, 4)
            for index, line in enumerate(lines):
                p.setPen(color if index == 0 else QColor('#edf5fa'))
                p.drawText(rect.adjusted(8, 5 + index * fm.height(), -8, -5), Qt.AlignTop | Qt.AlignHCenter, line)
        p.restore()
        for i, region in enumerate(self.pinned_regions):
            if not region:
                continue
            bounds = region_bounds(region['lat'], region['lon'])
            rect = QRectF(self.point(bounds[1][0], bounds[0][1]), self.point(bounds[0][0], bounds[1][1]))
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor('#00c9ed'), 1, Qt.DashLine))
            p.drawRect(rect)
            p.drawText(rect.topLeft()+QPointF(5, 15), f'Участок {i+1}')
        p.setBrush(GOLD)
        p.setPen(QPen(GOLD, 2))
        p.drawLine(antenna + QPointF(0, -12), antenna + QPointF(0, 10))
        p.drawLine(antenna + QPointF(-7, 10), antenna + QPointF(7, 10))
        p.drawEllipse(antenna + QPointF(0, -12), 3, 3)
        p.setBrush(Qt.NoBrush)
        p.drawArc(QRectF(antenna.x() - 10, antenna.y() - 23, 20, 20), 20 * 16, 140 * 16)
        p.drawText(antenna + QPointF(14, 18), 'АНТЕННА')
        _, _, mpp = scale_bar(self.lat, self.zoom)
        p.fillRect(QRectF(10, self.height()-99, 310, 62), QColor(12, 22, 32, 230))
        p.setPen(QPen(QColor('#edf5fa'), 2))
        width_m = self.width()*mpp
        width_text = f'{width_m/1000:.1f} км' if width_m >= 1000 else f'{width_m:.0f} м'
        p.drawText(QPointF(22, self.height()-76), f'Радиус: {self.radius_m/1000:g} км · кольца: {self.ring_step_m/1000:g} км')
        p.drawText(QPointF(22, self.height()-54), f'Масштаб: ≈ {width_text} по ширине')
        p.setPen(QColor('#174b60' if self.theme == 'day' else '#dbe8ee'))
        p.drawText(QRectF(18, 14, self.width() - 36, 28), Qt.AlignLeft, 'N ↑     СЕВЕР')
        p.fillRect(QRectF(0, self.height() - 31, self.width(), 31), QColor(12, 22, 32, 230))
        if self.offline and self.offline.bounds:
            west, south, east, north = self.offline.bounds
            if not contains(self.offline.bounds, self.lat, self.lon):
                p.drawText(QRectF(18, 43, self.width()-36, 25), Qt.AlignLeft, 'За пределами сохранённого района')

    def wheelEvent(self, event):
        if not event.angleDelta().y():
            return
        position = event.pos()
        cx, cy = world_pixel(self.lat, self.lon, self.zoom)
        anchor = latlon_from_pixel(cx+position.x()-self.width()/2, cy+position.y()-self.height()/2, self.zoom)
        self.set_zoom(self.zoom + (1 if event.angleDelta().y() > 0 else -1))
        px, py = world_pixel(*anchor, self.zoom)
        self.lat, self.lon = latlon_from_pixel(px-position.x()+self.width()/2, py-position.y()+self.height()/2, self.zoom)
        self.update()
        event.accept()

    def mousePressEvent(self, event):
        self.press_pos = event.pos()
        self.drag_pos = event.pos() if event.button() == Qt.LeftButton else None
        self.dragged = False

    def mouseMoveEvent(self, event):
        if self.drag_pos is None:
            return
        if not self.dragged and (event.pos()-self.press_pos).manhattanLength() < 5:
            return
        self.dragged = True
        delta = event.pos()-self.drag_pos
        cx, cy = world_pixel(self.lat, self.lon, self.zoom)
        self.lat, self.lon = latlon_from_pixel(cx-delta.x(), cy-delta.y(), self.zoom)
        self.drag_pos = event.pos()
        self.auto_fit = False
        self.update()

    def mouseReleaseEvent(self, event):
        self.drag_pos = None
        if self.press_pos is None or self.dragged or (event.pos()-self.press_pos).manhattanLength() >= 5:
            self.press_pos = None
            return
        self.press_pos = None
        if event.button() == Qt.RightButton:
            cx, cy = world_pixel(self.lat, self.lon, self.zoom)
            lat, lon = latlon_from_pixel(cx+event.pos().x()-self.width()/2,
                                        cy+event.pos().y()-self.height()/2, self.zoom)
            self.region_clicked.emit(lat, lon)
        elif event.button() == Qt.LeftButton:
            for track_id, rect in self.label_rects.items():
                if rect.contains(QPointF(event.pos())):
                    self.selected.emit(track_id)
                    return
            nearest = sorted(self.hit_positions,
                key=lambda key: (self.hit_positions[key]-QPointF(event.pos())).manhattanLength())
            if nearest and (self.hit_positions[nearest[0]]-QPointF(event.pos())).manhattanLength() < 16:
                self.selected.emit(nearest[0])


class Window(DashboardMixin, QMainWindow):
    def __init__(self, enable_yandex=True):
        super().__init__()
        self.enable_yandex = enable_yandex
        self.preferences_path = Path(__file__).parent / 'data' / 'map-settings.json'
        preferences = {}
        if enable_yandex and '--smoke-test' not in sys.argv:
            try:
                preferences = json.loads(self.preferences_path.read_text(encoding='utf-8'))
                if not isinstance(preferences, dict):
                    preferences = {}
            except (OSError, ValueError):
                pass
        coordinates = preferences.get('antenna', [60.022612, 30.293248, 180])
        if (not isinstance(coordinates, list) or len(coordinates) != 3 or
                not all(isinstance(v, (int, float)) and math.isfinite(v) for v in coordinates) or
                not -85 <= coordinates[0] <= 85 or not -180 <= coordinates[1] <= 180):
            coordinates = [60.022612, 30.293248, 180]
        self.setWindowTitle('Контур · наблюдение')
        screen = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1440, screen.width() - 24), min(900, screen.height() - 48))
        self.tracks = Tracks()
        self.elapsed = 0.0
        self.previous_tick = time.monotonic()
        self.running = True
        self.canvas = MapCanvas(self.tracks)
        self.canvas.map_ready.connect(self.refresh_map)
        self.canvas.antenna = tuple(coordinates[:2])
        self.canvas.focus_antenna()
        self.antenna_altitude = coordinates[2]
        self.input_system = WGS84
        self.offline_error = ''
        self.download_job = None
        self.pending_region = False
        initial_map = find_region(*coordinates[:2]) or DEFAULT_MAP
        if initial_map.exists():
            try:
                self.canvas.load_offline(initial_map)
            except Exception as exc:
                self.offline_error = f'Ошибка офлайн-карты: {exc}'
        self.source = TrafficSource(*coordinates[:2])
        self.yandex = None
        self.map_stack = QStackedWidget()
        self.map_stack.addWidget(self.canvas)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self.tick)
        self.animation_timer = QTimer(self)
        self.animation_timer.setInterval(33)
        self.animation_timer.setTimerType(Qt.PreciseTimer)
        self.animation_timer.timeout.connect(self.animate_radar)
        self.last_animation = time.monotonic()
        self.last_status = 0.0

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 10, 16, 12)
        layout.setSpacing(10)
        title = QLabel('КОНТУР  /  наблюдение')
        title.setObjectName('title')
        self.status = QLabel()
        header = QHBoxLayout()
        header.addWidget(title)
        header.addStretch()
        self.utc_label = QLabel()
        header.addWidget(self.utc_label)
        header.addSpacing(20)
        header.addWidget(self.status)
        layout.addLayout(header)
        self.antenna_label = QLabel()
        self.antenna_label.setWordWrap(True)
        layout.addWidget(self.antenna_label)
        content = QHBoxLayout()
        content.setSpacing(12)
        layout.addLayout(content, 1)
        self.tabs = QTabWidget()
        self.tabs.setFixedWidth(270)
        content.addWidget(self.tabs, 0, Qt.AlignTop)

        def page(name):
            widget = QWidget()
            column = QVBoxLayout(widget)
            column.setContentsMargins(10, 10, 10, 10)
            column.setSpacing(14)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(widget)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            scroll.setFrameShape(QScrollArea.NoFrame)
            self.tabs.addTab(scroll, name)
            return column, scroll

        display, self.left_scroll = page('Обзор')
        self.selected_title = QLabel('Цель не выбрана')
        self.selected_title.setWordWrap(True)
        self.selected_title.setObjectName('sectionTitle')
        display.addWidget(self.selected_title)
        display.addWidget(QLabel('След за целью'))
        self.tail = NoWheelComboBox()
        for text, mode in [('Последние 5 отметок', 'short'), ('Вся трасса', 'full'), ('Без следа', 'none')]:
            self.tail.addItem(text, mode)
        self.tail.currentIndexChanged.connect(self.change_tail)
        display.addWidget(self.tail)
        self.parameters_check = QCheckBox('Параметры всех целей')
        self.parameters_check.setToolTip('Скорость, высота и дальность. У выбранной цели параметры видны всегда.')
        self.parameters_check.toggled.connect(self.change_labels)
        display.addWidget(self.parameters_check)
        self.target_controls_layout = display
        display.addStretch()

        settings, self.settings_scroll = page('Настройки')
        box = QGroupBox('Положение антенны')
        form = QFormLayout(box)
        form.setRowWrapPolicy(QFormLayout.WrapAllRows)
        self.lat = self.spin(-85, 85, coordinates[0], 6)
        self.lon = self.spin(-180, 180, coordinates[1], 6)
        self.alt = self.spin(-500, 10000, coordinates[2], 1)
        self.input_crs = NoWheelComboBox()
        for key, name in NAMES.items():
            self.input_crs.addItem(name + ' · градусы', key)
        self.input_crs.currentIndexChanged.connect(self.change_input_crs)
        form.addRow('Система ввода', self.input_crs)
        for text, widget in [('Широта, °', self.lat), ('Долгота, °', self.lon), ('Высота, м', self.alt)]:
            form.addRow(text, widget)
        place = QPushButton('Установить антенну')
        place.clicked.connect(self.move_antenna)
        form.addRow(place)
        settings.addWidget(box)
        map_box = QGroupBox('Карта')
        ml = QVBoxLayout(map_box)
        self.map_mode = NoWheelComboBox()
        self.map_mode.addItem('Офлайн карта', 'offline')
        self.map_mode.addItem('Яндекс · онлайн', 'yandex')
        mode = 'offline'
        if enable_yandex:
            try:
                mode = json.loads(self.preferences_path.read_text(encoding='utf-8'))['map_mode']
            except (OSError, ValueError, KeyError, TypeError):
                pass
        self.map_mode.setCurrentIndex(max(0, self.map_mode.findData(mode)))
        self.map_mode.currentIndexChanged.connect(self.change_map)
        ml.addWidget(self.map_mode)
        self.map_label = QLabel('Не подключено')
        self.map_label.setWordWrap(True)
        self.map_label.setObjectName('muted')
        ml.addWidget(self.map_label)
        self.region_status = QLabel()
        self.region_status.setWordWrap(True)
        self.region_status.hide()
        ml.addWidget(self.region_status)
        self.region_retry = QPushButton('Повторить скачивание')
        self.region_retry.clicked.connect(self.prepare_detail)
        self.region_retry.hide()
        ml.addWidget(self.region_retry)
        self.region_cancel = QPushButton('Отменить загрузку')
        self.region_cancel.clicked.connect(self.cancel_region)
        self.region_cancel.hide()
        ml.addWidget(self.region_cancel)
        center = QPushButton('Обзор 70 км · к антенне')
        center.clicked.connect(self.focus_antenna)
        ml.addWidget(center)
        settings.addWidget(map_box)
        settings.addWidget(QLabel('Оборудование: не подключено'))
        settings.addStretch()

        operator, self.operator_scroll = page('Оператор')
        operator.addWidget(QLabel('Отображение координат'))
        self.display_crs = NoWheelComboBox()
        for key, name in NAMES.items():
            self.display_crs.addItem(name + ' · градусы', key)
        self.display_crs.setCurrentIndex(max(0, self.display_crs.findData(preferences.get('display_crs', WGS84))))
        self.canvas.display_crs = self.display_crs.currentData()
        operator.addWidget(self.display_crs)
        operator.addWidget(QLabel('Оформление'))
        self.theme_choice = NoWheelComboBox()
        for title, key in [('День', 'day'), ('Сумерки', 'dusk'), ('Ночь', 'night')]:
            self.theme_choice.addItem(title, key)
        self.theme_choice.setCurrentIndex(max(0, self.theme_choice.findData(preferences.get('theme', 'night'))))
        operator.addWidget(self.theme_choice)
        display.insertWidget(display.count()-1, QLabel('Принадлежность цели'))
        self.mark_color = NoWheelComboBox()
        for key, name in MARK_NAMES.items():
            swatch = QPixmap(14, 14)
            swatch.fill(QColor(MARK_COLORS[key]))
            self.mark_color.addItem(QIcon(swatch), name, key)
        display.insertWidget(display.count()-1, self.mark_color)
        self.mark_target_button = QPushButton('Отметить цель')
        self.mark_target_button.clicked.connect(self.mark_target)
        display.insertWidget(display.count()-1, self.mark_target_button)
        self.clear_target_button = QPushButton('Снять метку')
        self.clear_target_button.clicked.connect(self.clear_target_mark)
        display.insertWidget(display.count()-1, self.clear_target_button)
        export_targets = QPushButton('Экспорт CSV')
        export_targets.clicked.connect(self.export)
        display.insertWidget(display.count()-1, export_targets)
        operator.addStretch()
        self.display_crs.currentIndexChanged.connect(self.change_display_crs)
        self.theme_choice.currentIndexChanged.connect(self.change_theme)
        self.change_theme(save=False)

        content.addWidget(self.map_stack, 1)
        self.table_window = QDialog(self, Qt.Window | Qt.WindowTitleHint | Qt.WindowSystemMenuHint | Qt.WindowCloseButtonHint)
        self.table_window.setWindowTitle('Цели')
        self.table_window.setModal(False)
        bl = QVBoxLayout(self.table_window)
        bl.setContentsMargins(8, 8, 8, 8)
        bl.setSpacing(6)
        table_header = QHBoxLayout()
        self.table_count = QLabel()
        table_header.addWidget(self.table_count)
        table_header.addStretch()
        export = QPushButton('Экспорт CSV')
        export.clicked.connect(self.export)
        table_header.addWidget(export)
        bl.addLayout(table_header)
        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(['Цель', 'Тип', 'Скорость\nм/с', 'Высота\nм', 'Дальность\nкм', 'Курс\n°', 'Широта\n°', 'Долгота\n°', 'Время\nUTC'])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setStyleSheet('QHeaderView::section {padding:5px;}')
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.cellClicked.connect(self.table_clicked)
        bl.addWidget(self.table)
        self.tabs.currentChanged.connect(self.fit_sidebar)
        QTimer.singleShot(0, self.fit_sidebar)
        self.canvas.selected.connect(self.select)
        self.build_dashboard(preferences)
        self.canvas.focus_antenna()
        self.prepare_region()
        self.tick()
        self.refresh_selection()
        self.change_tail()
        self.timer.start()
        self.animation_timer.start()
        if self.map_mode.currentData() == 'offline' or (enable_yandex and '--smoke-test' not in sys.argv):
            self.change_map()
        if enable_yandex and '--smoke-test' not in sys.argv:
            QTimer.singleShot(0, self.prepare_detail)

    def change_input_crs(self):
        system = self.input_crs.currentData()
        lat, lon = convert(self.lat.value(), self.lon.value(), self.input_system, system)
        self.lat.setValue(lat)
        self.lon.setValue(lon)
        self.input_system = system

    def change_display_crs(self):
        self.canvas.display_crs = self.display_crs.currentData()
        self.refresh_status()
        self.refresh_table()
        self.refresh_selection()
        self.refresh_map()
        self.save_preferences()

    def change_theme(self, index=0, save=True):
        self.canvas.theme = self.theme_choice.currentData()
        if self.canvas.offline:
            self.canvas.offline.theme = self.canvas.theme
        if hasattr(self, 'target_cards'):
            self.target_cards.setProperty('theme', self.canvas.theme)
            self.target_cards.viewport().update()
        app = QApplication.instance()
        if not hasattr(app, 'kontur_base_style'):
            app.kontur_base_style = app.styleSheet()
        style = app.kontur_base_style + """
            QWidget {font-size:12px;}
            QTabBar::tab {min-width:80px; padding:12px 15px; border:1px solid #304553;}
            QTabBar::tab:selected {border-bottom:3px solid #00c9ed; background:#10202c;}
            QTabWidget#controlTabs::pane,QTableWidget#targetCards {border:2px solid #00c9ed; border-radius:3px;}
            QTabWidget#controlTabs QTabBar::tab {min-width:0px; padding:10px 6px;}
            QFrame#mainMapFrame {border:3px solid #00c9ed;}
            QTableWidget#targetCards {background:#10202c; gridline-color:#304553;}
            QTableWidget#targetCards::item {padding:5px;}
            QPushButton#primaryButton {background:#00b9d2; color:white; font-weight:bold;}
            QPushButton#stopButton {background:#bf342a; color:white; font-weight:bold;}
            QGroupBox {margin-top:12px; padding-top:12px;}
            QPushButton {padding:6px;}
            QPushButton#mapZoomButton {padding:0; font-size:24px; font-weight:bold;}
        """
        if self.canvas.theme == 'day':
            replacements = {'#15232f':'#edf2f6', '#e3edf4':'#142b3c', '#ffffff':'#102c40',
                '#edf6fb':'#173c50', '#91a7b7':'#40596b', '#1b2e3d':'#dce6ed',
                '#294758':'#c2dae8', '#90adbe':'#365467', '#253e50':'#d6e5ee',
                '#325269':'#bed8e8', '#10202c':'#ffffff', '#172b39':'#edf4f8',
                '#243948':'#cfdee8', '#3a5362':'#b7d8eb', '#ffca72':'#523300'}
        elif self.canvas.theme == 'dusk':
            replacements = {'#15232f':'#303545', '#e3edf4':'#f1e7d8', '#ffffff':'#fff0d4',
                '#1b2e3d':'#383e50', '#294758':'#50566b', '#10202c':'#272b39',
                '#253e50':'#444b60', '#325269':'#58617a', '#172b39':'#333849'}
        else:
            replacements = {}
        import re
        style = re.sub(r'#[0-9a-fA-F]{6}', lambda m: replacements.get(m.group(), m.group()), style)
        self.setStyleSheet(style)
        self.canvas.update()
        for preview in getattr(self, 'previews', []):
            preview.update()
        if self.yandex:
            self.refresh_map()
        if save:
            self.save_preferences()

    def mark_target(self):
        if self.canvas.selected_id in self.tracks.active_ids:
            self.canvas.target_marks[self.canvas.selected_id] = self.mark_color.currentData()
            self.refresh_selection()
            self.refresh_table()
            self.refresh_map()

    def clear_target_mark(self):
        self.canvas.target_marks.pop(self.canvas.selected_id, None)
        self.refresh_selection()
        self.refresh_table()
        self.refresh_map()

    @staticmethod
    def spin(lo, hi, value, decimals):
        widget = NoWheelSpinBox()
        widget.setRange(lo, hi)
        widget.setDecimals(decimals)
        widget.setValue(value)
        widget.setKeyboardTracking(False)
        return widget

    def tick(self):
        now = time.monotonic()
        bounds_changed = self.source.set_bounds(self.traffic_bounds())
        if self.running:
            self.elapsed += now - self.previous_tick
        if self.running or bounds_changed:
            self.tracks.synchronize(self.source.samples(self.elapsed, time.time()))
        self.previous_tick = now
        self.canvas.paused = not self.running
        if self.canvas.selected_id not in self.tracks.active_ids:
            self.canvas.selected_id = None
        self.refresh_status()
        self.refresh_table()
        self.refresh_selection()
        self.refresh_map()

    def traffic_bounds(self):
        return None

    @staticmethod
    def number(value, digits=1):
        return '—' if value is None or not math.isfinite(value) else f'{value:.{digits}f}'

    def refresh_table(self):
        self.table_ids = sorted(self.tracks.active_ids)
        scroll = self.table.verticalScrollBar().value()
        self.table.setRowCount(len(self.table_ids))
        system = self.canvas.display_crs
        self.table.horizontalHeaderItem(6).setText('Широта, °\n'+NAMES[system])
        self.table.horizontalHeaderItem(7).setText('Долгота, °\n'+NAMES[system])
        for row, track_id in enumerate(self.table_ids):
            s = self.tracks.history[track_id][-1]
            lat, lon = convert(s.lat, s.lon, WGS84, system)
            values = [target_table_name(track_id, self.canvas.target_marks.get(track_id)), display_type(s), self.number(s.speed_mps), self.number(s.altitude_m, 0),
                self.number(s.ground_range_m / 1000 if s.ground_range_m is not None else None, 2),
                self.number(s.course_deg), self.number(lat, 6), self.number(lon, 6),
                datetime.fromtimestamp(s.timestamp, timezone.utc).strftime('%H:%M:%S')]
            for column, value in enumerate(values):
                item = self.table.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    self.table.setItem(row, column, item)
                item.setText(value)
                item.setData(Qt.UserRole, track_id)
                item.setTextAlignment(Qt.AlignVCenter | (Qt.AlignLeft if column < 2 else Qt.AlignRight))
        self.table.verticalScrollBar().setValue(scroll)
        self.table_count.setText(f'Цели · {len(self.table_ids)} из {self.source.POOL_SIZE}')
        self.fit_table()
        self.refresh_cards()

    def fit_table(self):
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)

    def table_clicked(self, row, column):
        item = self.table.item(row, 0)
        if item:
            self.select(item.data(Qt.UserRole))

    def refresh_map(self):
        self.canvas.update()
        for preview in getattr(self, 'previews', ()):
            preview.update()
        if self.yandex:
            self.yandex.send(snapshot(self.canvas))

    def change_labels(self):
        self.canvas.label_fields = set(FIELDS) if self.parameters_check.isChecked() else set()
        self.refresh_map()

    def refresh_status(self):
        self.utc_label.setText(clock_text())
        self.antenna_label.setText('АНТЕННА · ' + coordinate_text(*self.canvas.antenna, self.canvas.display_crs) + f' · высота {self.antenna_altitude:g} м')
        self.status.setText(f'АЗИМУТ  {self.canvas.azimuth:05.1f}°')

        if hasattr(self, 'radar_coordinates'):
            self.radar_coordinates.setText(coordinate_text(*self.canvas.antenna, self.canvas.display_crs) + f'\nВысота: {self.antenna_altitude:g} м')

    def animate_radar(self):
        now = time.monotonic()
        dt, self.last_animation = now - self.last_animation, now
        if self.running:
            self.canvas.azimuth = (self.canvas.azimuth + dt * RADAR_RATE) % 360
            if self.map_stack.currentWidget() is self.offline_page:
                self.canvas.update()
        if now - self.last_status >= .1:
            self.refresh_status()
            self.last_status = now

    def show_table(self):
        self.table_window.resize(1050,430)
        self.table_window.show()
        self.table_window.raise_()

    def fit_sidebar(self):
        if hasattr(self, 'right_panel'):
            self.navigation.setFixedWidth(150)
            self.navigation.setExpanding(True)
            self.right_panel.setFixedWidth(max(285, round(self.width()*.2125)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'tabs'):
            self.fit_sidebar()

    def change_map(self):
        mode = self.map_mode.currentData()
        self.save_preferences()

        if mode == 'offline':
            self.dispose_yandex()
            self.map_stack.setCurrentWidget(self.offline_page)
            self.offline_status()
            self.tick()
            return
        key = api_key()
        if not key:
            self.map_failed('Не задан ключ Яндекс API')
            return
        try:
            if self.yandex is None:
                self.yandex = create_view(self.map_stack, key)
                self.yandex.bridge.selected.connect(self.select)
                self.yandex.bridge.region_clicked.connect(self.pin_region)
                self.yandex.bridge.region_activated.connect(self.activate_region)
                self.yandex.bridge.region_cleared.connect(self.clear_region)
                self.yandex.bridge.failed.connect(self.map_failed)
                self.yandex.bridge.loaded.connect(self.map_loaded)
                self.map_stack.addWidget(self.yandex)
            self.map_label.setText('Подключение…')
            self.map_stack.setCurrentWidget(self.yandex)
            self.yandex.activate()
            self.refresh_map()
        except ImportError:
            self.map_failed('Не установлен Qt WebEngine')

    def save_preferences(self):
        if self.enable_yandex and '--smoke-test' not in sys.argv:
            try:
                self.preferences_path.parent.mkdir(parents=True, exist_ok=True)
                temp = self.preferences_path.with_suffix('.partial')
                temp.write_text(json.dumps({'map_mode': self.map_mode.currentData(),
                    'antenna': [*self.canvas.antenna, self.antenna_altitude],
                    'display_crs': self.canvas.display_crs, 'theme': self.canvas.theme,
                    'pinned_regions': self.canvas.pinned_regions}), encoding='utf-8')
                temp.replace(self.preferences_path)
            except OSError:
                self.statusBar().showMessage('Не удалось сохранить настройки: проверь права на папку data.', 8000)

    def map_failed(self, message):
        if self.map_mode.currentData() != 'yandex':
            return
        self.map_label.setText(message)
        self.map_stack.setCurrentWidget(self.offline_page)
        self.tick()

    def map_loaded(self):
        if self.map_mode.currentData() != 'yandex':
            return
        self.map_label.setText(self.yandex.api_warning or 'Подключено')
        self.map_stack.setCurrentWidget(self.yandex)
        self.tick()

    def focus_antenna(self):
        self.canvas.focus_antenna()
        if self.yandex and self.map_stack.currentWidget() is self.yandex:
            self.yandex.focus_antenna()

    def zoom_by(self, delta):
        if self.yandex and self.map_stack.currentWidget() is self.yandex:
            self.yandex.zoom_by(delta)
        else:
            self.canvas.set_zoom(self.canvas.zoom + delta)

    def refresh_selection(self):
        track_id = self.canvas.selected_id
        title = f'{target_name(track_id)} · выбрана' if track_id else 'Цель не выбрана'
        if track_id in self.tracks.active_ids:
            sample = self.tracks.history[track_id][-1]
            title += '\n' + coordinate_text(sample.lat, sample.lon, self.canvas.display_crs)
            title += '\nСектор ' + bearing_sector(self.canvas.antenna, (sample.lat, sample.lon))
        self.selected_title.setText(title)
        self.selected_title.setMinimumHeight(self.selected_title.heightForWidth(238))
        self.fit_sidebar()
        self.mark_target_button.setEnabled(bool(track_id))
        self.clear_target_button.setEnabled(track_id in self.canvas.target_marks)
        if track_id in self.table_ids:
            # Keep a stable selection without scrolling the table every tick.
            scroll = self.table.verticalScrollBar().value()
            self.table.selectRow(self.table_ids.index(track_id))
            self.table.verticalScrollBar().setValue(scroll)
        else:
            self.table.clearSelection()
            self.table.setCurrentItem(None)

    def select(self, track_id):
        if track_id not in self.tracks.active_ids:
            return
        self.canvas.selected_id = None if self.canvas.selected_id == track_id else track_id
        self.refresh_selection()
        self.refresh_map()

    def change_tail(self):
        self.canvas.tail_mode = self.tail.currentData()
        self.refresh_map()

    def offline_status(self):
        layer = self.canvas.offline
        text = ('Обзор России · район ещё не сохранён' if layer.archive.max_zoom < DETAIL_ZOOM else 'Район 70 км сохранён · без интернета') if layer else self.offline_error or 'Файл офлайн-карты не найден'
        self.map_label.setText(text)

    def prepare_region(self):
        self.region_status.hide()
        cached = find_region(*self.canvas.antenna)
        if cached:
            if not self.canvas.offline or self.canvas.offline_path != cached:
                self.canvas.load_offline(cached)
        else:
            if self.canvas.offline:
                self.canvas.offline.close()
                self.canvas.offline = None
        self.offline_status()
        self.refresh_map()

    def prepare_detail(self):
        position = self.canvas.antenna
        if self.download_job:
            if (self.download_job.lat, self.download_job.lon) != position:
                self.pending_region = True
                self.download_job.cancel()
            return
        if find_region(*position, min_zoom=DETAIL_ZOOM):
            self.region_retry.hide()
            self.prepare_region()
            return
        job = RegionDownload(*position, self)
        self.download_job = job
        self.region_status.setText('Подключаемся к источнику карты…')
        self.region_status.show()
        self.region_retry.hide()
        self.region_cancel.show()
        job.progress.connect(self.region_status.setText)
        job.progress.connect(lambda message:self.map_label.setText('Скачиваем район · '+message.split('%')[0].split(': ')[-1]+'%' if '%' in message else 'Готовим район через интернет…'))
        job.failed.connect(self.region_status.setText)
        job.failed.connect(lambda message:self.region_retry.show() if not self.pending_region else None)
        job.failed.connect(lambda message:self.map_label.setText('Загрузка остановлена · доступна сохранённая карта'))
        job.completed.connect(self.detail_ready)
        job.finished.connect(self.region_finished)
        job.start()

    def detail_ready(self, path):
        job = self.download_job
        if job and (job.lat, job.lon) == self.canvas.antenna:
            self.prepare_region()

    def cancel_region(self):
        self.pending_region = False
        if self.download_job:self.download_job.cancel()

    def region_finished(self):
        job, self.download_job = self.download_job, None
        if job:job.deleteLater()
        self.region_cancel.hide()
        if self.pending_region:
            self.pending_region = False
            QTimer.singleShot(0, self.prepare_detail)

    def move_antenna(self):
        if self.tracks.history and QMessageBox.question(self, 'Новый сеанс',
                'Перемещение антенны начнёт новый сеанс. Текущая история будет сброшена. '
                'При необходимости сначала экспортируйте CSV. Продолжить?') != QMessageBox.Yes:
            return
        lat, lon = convert(self.lat.value(), self.lon.value(), self.input_system, WGS84)
        if not -85 <= lat <= 85:
            QMessageBox.warning(self, 'Координаты', 'Широта карты должна быть в пределах ±85°.')
            return
        self.source = TrafficSource(lat, lon, bounds=self.traffic_bounds())
        self.antenna_altitude = self.alt.value()
        self.canvas.target_marks.clear()
        self.canvas.antenna = self.source.lat, self.source.lon
        self.prepare_region()
        self.save_preferences()
        self.offline_status()
        self.canvas.focus_antenna()
        self.tracks.history.clear()
        self.tracks.active_ids.clear()
        self.tracks.episode_starts.clear()
        self.canvas.selected_id = None
        self.elapsed = 0
        self.canvas.azimuth = 0
        self.last_animation = time.monotonic()
        self.previous_tick = time.monotonic()
        self.tracks.synchronize(self.source.samples(0, time.time()))
        self.tick()
        self.focus_antenna()
        if self.enable_yandex and '--smoke-test' not in sys.argv:
            self.prepare_detail()

    def dispose_yandex(self):
        # A fresh page/profile avoids Qt 5's asynchronous about:blank/cache
        # clearing race when reconnecting or rapidly switching back online.
        old, self.yandex = self.yandex, None
        if old:
            old.bridge.loaded.disconnect(self.map_loaded)
            old.bridge.failed.disconnect(self.map_failed)
            old.bridge.selected.disconnect(self.select)
            old.bridge.region_clicked.disconnect(self.pin_region)
            old.bridge.region_activated.disconnect(self.activate_region)
            old.bridge.region_cleared.disconnect(self.clear_region)
            self.map_stack.setCurrentWidget(self.offline_page)
            self.map_stack.removeWidget(old)
            old.hide()
            old.shutdown()
            old.deleteLater()

    def export(self):
        path, _ = QFileDialog.getSaveFileName(self, 'История сеанса', 'tracks.csv', 'CSV (*.csv)')
        if not path:
            return
        try:
            with open(path, 'w', newline='', encoding='utf-8-sig') as output:
                writer = csv.writer(output)
                writer.writerow(['source', 'track_id', 'object_type', 'timestamp_utc', 'lat_wgs84', 'lon_wgs84',
                                 'altitude_m', 'ground_range_m', 'speed_mps', 'course_deg', 'display_crs', 'display_lat', 'display_lon', 'manual_color', 'appearance'])
                for history in self.tracks.history.values():
                    for s in history:
                        writer.writerow(['synthetic', s.track_id, s.object_type, datetime.fromtimestamp(s.timestamp, timezone.utc).isoformat(),
                                         s.lat, s.lon, s.altitude_m, s.ground_range_m, s.speed_mps, s.course_deg,
                                         self.canvas.display_crs, *convert(s.lat, s.lon, WGS84, self.canvas.display_crs), self.canvas.target_marks.get(s.track_id, ''), s.appearance])
            self.statusBar().showMessage(f'История сохранена: {path}', 6000)
        except OSError as exc:
            QMessageBox.warning(self, 'Ошибка экспорта', str(exc))

    def closeEvent(self, event):
        if self.tracks.history and '--smoke-test' not in sys.argv:
            answer = QMessageBox.question(self, 'Закрыть сеанс?',
                'История хранится в памяти. При необходимости экспортируйте CSV перед выходом. Закрыть?',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        self.table_window.close()
        self.timer.stop()
        self.animation_timer.stop()
        if self.yandex:
            self.yandex.shutdown()
        if self.download_job:
            self.pending_region = False
            self.download_job.cancel()
            self.download_job.wait()
        if self.canvas.offline:
            self.canvas.offline.close()
        event.accept()


def create_application():
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    if sys.platform.startswith('linux'):
        # Astra/VirtualBox can expose a raster window surface that Qt Quick's
        # OpenGL compositor cannot use. Chromium's --disable-gpu alone does
        # not disable that compositor; select the software scene graph too.
        os.environ.setdefault('QT_QUICK_BACKEND', 'software')
        os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS', '--disable-gpu')
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    font_family = 'DejaVu Sans'
    if sys.platform == 'win32':
        font_id = QFontDatabase.addApplicationFont('C:/Windows/Fonts/arial.ttf')
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            font_family = families[0]
    app.setFont(QFont(font_family, 10))
    app.setStyleSheet('''
        QWidget {background:#15232f; color:#e3edf4; font-size:12px;}
        QMainWindow {background:#15232f;}
        QLabel#title {font-size:20px; font-weight:600; color:#ffffff; padding:4px 0;}
        QLabel#sectionTitle {font-size:14px; font-weight:600; color:#edf6fb;}
        QLabel#muted {color:#91a7b7; font-size:11px;}
        QTabWidget::pane {border:1px solid #304553; border-radius:4px;}
        QTabBar::tab {background:#1b2e3d; color:#91a7b7; padding:9px 13px;}
        QTabBar::tab:selected {background:#294758; color:#edf6fb;}
        QLabel#banner {background:#344038; color:#f1d395; padding:10px; border-radius:5px;}
        QGroupBox {border:1px solid #344857; border-radius:6px; margin-top:15px; padding-top:15px;}
        QGroupBox::title {subcontrol-origin:margin; left:10px; color:#90adbe;}
        QPushButton {background:#253e50; border:1px solid #39566a; border-radius:4px; padding:8px;}
        QPushButton:hover {background:#325269;}
        QComboBox,QDoubleSpinBox {background:#10202c; border:1px solid #395061; padding:6px; border-radius:3px;}
        QTableWidget {background:#10202c; alternate-background-color:#172b39; gridline-color:#263d4c; border:1px solid #344857;}
        QHeaderView::section {background:#243948; border:0; padding:8px;}
        QTableWidget::item:selected {background:#3a5362; color:#ffca72;}
        QScrollBar:vertical {background:#10202c; width:10px; margin:0; border:0;}
        QScrollBar::handle:vertical {background:#496477; min-height:32px; border-radius:4px;}
        QScrollBar::handle:vertical:hover {background:#678b9f;}
        QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {height:0;}
        QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical {background:none;}
    ''')
    return app


def main():
    app = create_application()
    window = Window()
    window.showMaximized()
    if '--smoke-test' in sys.argv:
        def verify():
            for _ in range(7):
                window.elapsed += .5
                for sample in window.source.samples(window.elapsed, time.time() + window.elapsed):
                    window.tracks.add(sample)
            window.select('T-001')
            window.tail.setCurrentIndex(1)
            window.canvas.update()
            app.processEvents()
            assert len(window.tracks.active_ids) <= TrafficSource.MAX_OBJECTS
            assert window.canvas.tail_mode == 'full'
            out = Path(__file__).parent / 'preview.png'
            assert window.grab().save(str(out))
            print(f'UI smoke test passed; screenshot: {out}', flush=True)
            app.quit()
        QTimer.singleShot(300, verify)
    return app.exec_()


if __name__ == '__main__':
    sys.exit(main())
