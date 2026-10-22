import math
from datetime import datetime, timezone

from PyQt5.QtCore import Qt, QPointF, QRectF, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen, QPainterPath
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QFormLayout, QTabWidget, QTabBar, QStackedWidget, QScrollArea, QGroupBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QSlider,
    QRadioButton, QButtonGroup, QComboBox, QDialog, QSizePolicy,
    QStyledItemDelegate, QStyle, QProgressBar)

from model import world_pixel, destination
from coordinates import coordinate_text
from markers import icon_kind, draw_icon
from labels import target_name, target_table_name

REGION_SIZE_M = 10000
MAIN_RADIUS_M = 70000
COLORS = {'green': '#36c96c', 'red': '#ff5656', 'blue': '#448dff'}


def region_bounds(lat, lon):
    south = destination(lat, lon, 180, REGION_SIZE_M/2)[0]
    north = destination(lat, lon, 0, REGION_SIZE_M/2)[0]
    west = destination(lat, lon, 270, REGION_SIZE_M/2)[1]
    east = destination(lat, lon, 90, REGION_SIZE_M/2)[1]
    return [[south, west], [north, east]]


def radar_bounds(lat, lon, radius=MAIN_RADIUS_M):
    points = [destination(lat, lon, bearing, radius) for bearing in range(0, 360, 2)]
    longitudes = [lon + (p[1]-lon+180)%360-180 for p in points]
    return [[min(p[0] for p in points), min(longitudes)],
            [max(p[0] for p in points), max(longitudes)]]


def clock_text(now=None):
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).strftime('UTC  %H:%M:%S')


def region_coverage(region, layer):
    if not region or not layer or not layer.bounds:
        return 'none'
    south_west, north_east = region_bounds(region['lat'], region['lon'])
    west, south, east, north = layer.bounds
    if north_east[0] <= south or south_west[0] >= north or (east-west < 359 and (north_east[1] <= west or south_west[1] >= east)):
        return 'none'
    if south_west[0] >= south and north_east[0] <= north and (east-west >= 359 or south_west[1] >= west and north_east[1] <= east):
        return 'full'
    return 'partial'


class RegionPreview(QWidget):
    activated = pyqtSignal(int)
    cleared = pyqtSignal(int)

    def __init__(self, canvas, index):
        super().__init__()
        self.canvas, self.index = canvas, index
        self.setMinimumSize(160, 130)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.clear_button = QPushButton('×', self)
        self.clear_button.setToolTip('Освободить участок')
        self.clear_button.setFixedSize(24, 22)
        self.clear_button.clicked.connect(lambda: self.cleared.emit(self.index))
        self.clear_button.hide()

    def resizeEvent(self, event):
        self.clear_button.move(self.width()-28, 4)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.activated.emit(self.index)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor('#edf0df' if self.canvas.theme == 'day' else '#192936'))
        region = self.canvas.pinned_regions[self.index]
        active = self.canvas.active_region == self.index
        self.clear_button.setVisible(region is not None)
        layer = self.canvas.offline
        scope = "preview-"+str(self.index)
        if layer:layer.begin_view(scope)
        if region:
            lat, lon = region['lat'], region['lon']
            bounds = region_bounds(lat, lon)
            side = max(20, min(self.width()-16, self.height()-52))
            mpp = REGION_SIZE_M/side
            exact_zoom = math.log2(2*math.pi*6378137*math.cos(math.radians(lat))/(256*mpp))
            layer = self.canvas.offline
            z = max(0, min(19, math.ceil(exact_zoom)))
            factor = 2**(exact_zoom-z)
            cx, cy = world_pixel(lat, lon, z)
            center = QPointF(self.width()/2, (self.height()+24)/2)
            def point(a, b):
                x, y = world_pixel(a, b, z)
                world = 256*2**z
                dx = (x-cx+world/2)%world-world/2
                return center + QPointF(dx*factor, (y-cy)*factor)
            p.save()
            p.setClipRect(QRectF(2, 27, self.width()-4, self.height()-29))
            p.save()
            if layer and layer.bounds:
                west, south, east, north = layer.bounds
                if east-west < 359:
                    p.setClipRect(QRectF(point(north, west), point(south, east)), Qt.IntersectClip)
            left, top = cx-center.x()/factor, cy-(center.y()-27)/factor
            for x in range(math.floor(left/256), math.ceil((left+self.width()/factor)/256)+1):
                for y in range(math.floor(top/256), math.ceil((top+self.height()/factor)/256)+1):
                    tile = layer.tile(z, x % (2**z), y, scope=scope) if layer else None
                    if tile and not tile.isNull():
                        rect = QRectF(center.x()+(x*256-cx)*factor, center.y()+(y*256-cy)*factor, 256*factor, 256*factor)
                        p.drawPixmap(rect, tile, QRectF(tile.rect()))
            p.restore()
            p.setRenderHint(QPainter.Antialiasing)
            for key, history in self.canvas.tracks.active_history().items():
                sample = history[-1]
                pos = point(sample.lat, sample.lon)
                color = QColor(COLORS.get(self.canvas.target_marks.get(key), '#007487' if self.canvas.theme == 'day' else '#59dbdf'))
                p.setPen(QPen(color, 1.5))
                path = QPainterPath()
                for i, item in enumerate(self.canvas.tracks.tail(key, self.canvas.tail_mode)):
                    q = point(item.lat, item.lon)
                    path.moveTo(q) if i == 0 else path.lineTo(q)
                p.setBrush(Qt.NoBrush)
                p.drawPath(path)
                p.save()
                p.translate(pos)
                p.rotate(0 if icon_kind(sample) == 'tower' else sample.course_deg or 0)
                p.setBrush(color)
                draw_icon(p, icon_kind(sample))
                p.restore()
                p.drawText(pos+QPointF(10, -10), target_name(key))
            p.setPen(QPen(QColor('#f08080'), 1))
            p.drawLine(QPointF(center.x(), 27), QPointF(center.x(), self.height()))
            p.drawLine(QPointF(0, center.y()), QPointF(self.width(), center.y()))
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor('#00c9ed'), 1, Qt.DashLine))
            p.drawRect(QRectF(point(bounds[1][0], bounds[0][1]), point(bounds[0][0], bounds[1][1])))
            coverage = region_coverage(region, layer)
            if coverage != 'full':
                warning = ('Нет офлайн-карты этого участка' if coverage == 'none'
                           else 'Офлайн-карта покрывает часть участка')
                notice = QRectF(4, 29, self.width()-8, 36)
                p.fillRect(notice, QColor(12, 24, 34, 235))
                p.setPen(QColor('#ffca72'))
                p.drawText(notice.adjusted(5,0,-5,0), Qt.AlignCenter | Qt.TextWordWrap, warning)
            p.restore()
            p.fillRect(QRectF(2, self.height()-23, self.width()-4, 21), QColor('#edf2f6' if self.canvas.theme == 'day' else '#0c1822'))
            p.setPen(QColor('#142b3c' if self.canvas.theme == 'day' else '#e3edf4'))
            p.drawText(QRectF(8, self.height()-23, self.width()-12, 21), Qt.AlignVCenter,
                       f'{lat:.5f}°, {lon:.5f}°')
        else:
            p.setPen(QColor('#91a7b7'))
            p.drawText(self.rect().adjusted(10, 30, -10, -10), Qt.AlignCenter | Qt.TextWordWrap,
                       'ПКМ на основной карте\nзакрепляет участок')
        p.setPen(QColor('#142b3c' if self.canvas.theme == 'day' else '#e3edf4'))
        p.drawText(QRectF(9, 3, self.width()-42, 22), Qt.AlignVCenter,
                   f'Участок {self.index+1} · 10 × 10 км')
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor('#00c9ed' if active else '#16849d'), 2))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 3, 3)

        if layer:layer.end_view(scope)


class TargetCardDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        painter.save()
        day = self.parent().property('theme') == 'day'
        painter.fillRect(option.rect, QColor(('#c2dae8' if day else '#294758') if option.state & QStyle.State_Selected else ('#ffffff' if day else '#10202c')))
        mark = index.data(Qt.UserRole+1)
        if mark in COLORS:
            painter.fillRect(option.rect.adjusted(1, 1, -option.rect.width()+5, -1), QColor(COLORS[mark]))
        lines = (index.data() or '').splitlines()
        x, y, width = option.rect.x()+10, option.rect.y()+5, option.rect.width()-20
        painter.setPen(QColor(COLORS.get(mark, '#142b3c' if day else '#e3edf4')))
        font = painter.font()
        font.setPixelSize(15);font.setBold(True);painter.setFont(font)
        if lines:
            while painter.fontMetrics().horizontalAdvance(lines[0]) > width and font.pixelSize() > 12:
                font.setPixelSize(font.pixelSize()-1)
                painter.setFont(font)
            painter.drawText(QRectF(x,y,width,20),Qt.AlignLeft|Qt.AlignVCenter,lines[0])
        painter.setPen(QPen(QColor('#304553'),1))
        painter.drawLine(QPointF(x,y+21),QPointF(x+width,y+21))
        painter.setPen(QColor('#40596b' if day else '#bacbd7'))
        font.setPixelSize(12);font.setBold(False);painter.setFont(font)
        for i,line in enumerate(lines[1:]):
            cell_width = width/2
            text = painter.fontMetrics().elidedText(line,Qt.ElideRight,max(1,int(cell_width-8)))
            painter.drawText(QRectF(x+(i%2)*cell_width,y+25+(i//2)*20,cell_width-8,18),
                             Qt.AlignLeft|Qt.AlignVCenter,text)
        painter.restore()


class DashboardMixin:
    def build_dashboard(self, preferences):
        old_root = self.centralWidget()
        old_tabs = self.tabs
        self.canvas.pinned_regions = [None, None, None]
        for i, r in enumerate(preferences.get('pinned_regions', [])[:3] if isinstance(preferences.get('pinned_regions'), list) else []):
            if (isinstance(r, dict) and all(isinstance(r.get(k), (int, float)) and math.isfinite(r[k]) for k in ('lat', 'lon'))
                    and -85 <= r['lat'] <= 85 and -180 <= r['lon'] <= 180):
                self.canvas.pinned_regions[i] = dict(lat=r['lat'], lon=r['lon'])
        self.canvas.active_region = next((i for i,r in enumerate(self.canvas.pinned_regions) if r is None), 0)

        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        top = QHBoxLayout()
        self.navigation = QTabBar()
        for name in ('Радар',):
            self.navigation.addTab(name)
        self.navigation.setExpanding(False)
        top.addWidget(self.navigation)
        top.addStretch()
        top.addWidget(self.utc_label)
        top.addSpacing(14)
        root_layout.addLayout(top)
        self.screen_stack = QStackedWidget()
        root_layout.addWidget(self.screen_stack, 1)
        radar = QWidget()
        content = QHBoxLayout(radar)
        content.setContentsMargins(6, 6, 6, 6)
        content.setSpacing(8)
        content.addWidget(self.map_stack, 78)
        self.right_panel = QWidget()
        right = QVBoxLayout(self.right_panel)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(16)
        self.target_cards = QTableWidget(0, 1)
        self.target_cards.setObjectName('targetCards')
        self.target_cards.setItemDelegate(TargetCardDelegate(self.target_cards))
        self.target_cards.horizontalHeader().hide()
        self.target_cards.verticalHeader().hide()
        self.target_cards.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.target_cards.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.target_cards.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.target_cards.setSelectionMode(QAbstractItemView.SingleSelection)
        self.target_cards.setWordWrap(True)
        self.target_cards.setShowGrid(True)
        self.target_cards.verticalHeader().setDefaultSectionSize(78)
        self.target_cards.cellClicked.connect(self.card_clicked)
        right.addWidget(self.target_cards, 45)

        self.control_tabs = QTabWidget()
        self.control_tabs.setObjectName('controlTabs')
        self.control_tabs.setMinimumWidth(285)
        right.addWidget(self.control_tabs, 55)
        content.addWidget(self.right_panel, 22)
        self.screen_stack.addWidget(radar)
        self.control_tabs.addTab(self.left_scroll, "Цели")
        def page(name):
            widget = QWidget()
            column = QVBoxLayout(widget)
            column.setContentsMargins(10, 10, 10, 10)
            column.setSpacing(10)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setWidget(widget)
            self.control_tabs.addTab(scroll, name)
            return column, scroll
        debug, self.debug_scroll = page('Отладка')
        work, self.work_scroll = page('Работа')
        settings_layout = self.settings_scroll.widget().layout()
        operator_layout = self.operator_scroll.widget().layout()
        while operator_layout.count():
            item = operator_layout.takeAt(0)
            if item.widget():
                settings_layout.insertWidget(settings_layout.count()-1, item.widget())
        self.operator_scroll = self.settings_scroll
        self.control_tabs.addTab(self.settings_scroll, 'Настройки')
        self.tabs = self.control_tabs
        self.control_tabs.setCurrentIndex(0)

        synth = QGroupBox('Управление синтезатором')
        sl = QVBoxLayout(synth)
        sl.addWidget(QLabel('Частота (МГц):'))
        self.synth_frequency = QComboBox()
        for freq in range(8550, 8651, 10):
            self.synth_frequency.addItem(f'{freq} МГц', freq)
        sl.addWidget(self.synth_frequency)
        buttons = QHBoxLayout()
        for name in ('Записать', 'Проверить'):
            button = QPushButton(name)
            button.clicked.connect(self.hardware_unavailable)
            buttons.addWidget(button)
        sl.addLayout(buttons)
        debug.addWidget(synth)
        gain = QGroupBox('Усиление')
        gl = QVBoxLayout(gain)
        self.gain = QSlider(Qt.Horizontal)
        self.gain.setRange(0, 2400)
        self.gain.setValue(1200)
        self.gain_label = QLabel('Задано: 1200 · не применено')
        self.gain.valueChanged.connect(lambda v: self.gain_label.setText(f'Задано: {v} · не применено'))
        gl.addWidget(self.gain)
        gl.addWidget(self.gain_label)
        debug.addWidget(gain)
        coords = QGroupBox('Координаты РЛС')
        cl = QVBoxLayout(coords)
        self.radar_coordinates = QLabel()
        self.radar_coordinates.setWordWrap(True)
        cl.addWidget(self.radar_coordinates)
        determine = QPushButton('Определить координаты')
        determine.clicked.connect(self.hardware_unavailable)
        cl.addWidget(determine)
        manual = QPushButton('Ввести координаты')
        manual.clicked.connect(lambda: self.control_tabs.setCurrentWidget(self.settings_scroll))
        cl.addWidget(manual)
        debug.addWidget(coords)
        debug.addStretch()

        emission = QGroupBox('ИЗЛУЧЕНИЕ')
        el = QVBoxLayout(emission)
        row = QHBoxLayout()
        on, off = QPushButton('ВКЛ'), QPushButton('ВЫКЛ')
        on.setObjectName('primaryButton')
        off.setObjectName('stopButton')
        on.clicked.connect(self.hardware_unavailable)
        off.clicked.connect(self.hardware_unavailable)
        row.addWidget(on); row.addWidget(off)
        el.addLayout(row)
        el.addWidget(QLabel('Статус: нет подключения'))
        work.addWidget(emission)
        mode = QGroupBox('РЕЖИМ РАБОТЫ')
        ml = QVBoxLayout(mode)
        self.mode_group = QButtonGroup(self)
        for i, name in enumerate(('Ближняя зона (10 км)', 'Дальняя зона (70 км)', 'Ближняя + Дальняя', 'Сверхдальняя (300 км)')):
            button = QRadioButton(name)
            self.mode_group.addButton(button, i)
            ml.addWidget(button)
            button.setChecked(i == 1)
        apply_mode = QPushButton('Применить')
        apply_mode.clicked.connect(self.hardware_unavailable)
        ml.addWidget(apply_mode)
        work.addWidget(mode)
        temperatures = QGroupBox('ТЕМПЕРАТУРА СИНТЕЗАТОРОВ')
        tl = QVBoxLayout(temperatures)
        for i in range(1, 4):
            row = QHBoxLayout()
            row.addWidget(QLabel(f'T{i}: — °C'))
            bar = QProgressBar()
            bar.setRange(0,100)
            bar.setValue(0)
            bar.setFormat('нет данных')
            bar.setAlignment(Qt.AlignCenter)
            row.addWidget(bar,1)
            tl.addLayout(row)
        work.addWidget(temperatures)

        work.addStretch()

        self.map_stack.removeWidget(self.canvas)
        self.offline_page = QWidget()
        self.offline_layout = QHBoxLayout(self.offline_page)
        self.offline_layout.setContentsMargins(0, 0, 0, 0)
        self.offline_layout.setSpacing(6)
        self.preview_column = QWidget()
        self.preview_layout = QVBoxLayout(self.preview_column)
        self.preview_layout.setContentsMargins(0, 0, 0, 0)
        self.preview_layout.setSpacing(6)
        self.previews = [RegionPreview(self.canvas, i) for i in range(3)]
        for preview in self.previews:
            preview.activated.connect(self.activate_region)
            preview.cleared.connect(self.clear_region)
            self.preview_layout.addWidget(preview, 1)
        self.offline_layout.addWidget(self.preview_column, 30)
        self.map_frame = QFrame()
        self.map_frame.setObjectName('mainMapFrame')
        map_layout = QVBoxLayout(self.map_frame)
        map_layout.setContentsMargins(0, 0, 0, 0)
        map_layout.addWidget(self.canvas)
        self.canvas.show()
        self.offline_layout.addWidget(self.map_frame, 70)
        self.map_stack.addWidget(self.offline_page)
        self.map_stack.setCurrentWidget(self.offline_page)

        self.control_tabs.currentChanged.connect(self.fit_sidebar)
        self.canvas.region_clicked.connect(self.pin_region)
        self.statusBar().addWidget(self.antenna_label, 1)
        self.statusBar().addPermanentWidget(self.status)
        self.antenna_label.setWordWrap(False)
        # Temporary messages hide normal widgets. Reserve the same height either way.
        self.statusBar().setFixedHeight(max(self.antenna_label.fontMetrics().height(),
                                            self.status.fontMetrics().height()) + 20)
        self.setCentralWidget(root)
        self.setWindowTitle('Контур · наблюдение [ИМИТАТОР]')
        self.navigate(0)
        old_root.deleteLater()
        self.change_theme(save=False)

    def hardware_unavailable(self):
        self.statusBar().showMessage('Команда не отправлена: оборудование и драйвер не подключены.', 6000)

    def navigate(self, index):
        self.screen_stack.setCurrentIndex(index)
        self.canvas.show_previews = True
        self.refresh_map()
        if index == 0:
            self.canvas.focus_antenna()
            if self.yandex:
                self.yandex.focus_antenna()

    def activate_region(self, index):
        if 0 <= index < 3:
            self.canvas.active_region = index
            self.refresh_map()

    def clear_region(self, index):
        if 0 <= index < 3:
            self.canvas.pinned_regions[index] = None
            self.canvas.active_region = index
            self.refresh_map()
            self.save_preferences()

    def pin_region(self, lat, lon):
        if not all(math.isfinite(v) for v in (lat, lon)) or not -85 <= lat <= 85 or not -180 <= lon <= 180:
            return
        index = self.canvas.active_region
        self.canvas.pinned_regions[index] = dict(lat=lat, lon=lon)
        self.canvas.active_region = next((i for i in range(index+1, index+4) if self.canvas.pinned_regions[i%3] is None), index+1)%3
        self.refresh_map()
        self.save_preferences()

    def card_clicked(self, row, column):
        item = self.target_cards.item(row, column)
        if item:
            self.select(item.data(Qt.UserRole))

    def refresh_cards(self):
        if not hasattr(self, 'target_cards'):
            return
        self.target_cards.setRowCount(len(self.table_ids))
        for row, key in enumerate(self.table_ids):
            s = self.tracks.history[key][-1]
            a, b = math.radians(self.canvas.antenna[0]), math.radians(s.lat)
            d = math.radians(s.lon-self.canvas.antenna[1])
            az = math.degrees(math.atan2(math.sin(d)*math.cos(b), math.cos(a)*math.sin(b)-math.sin(a)*math.cos(b)*math.cos(d)))%360
            distance = self.number(s.ground_range_m/1000 if s.ground_range_m is not None else None, 2)
            text = (f'{target_table_name(key, self.canvas.target_marks.get(key))}\nДальность · {distance} км\nАзимут · {az:.1f}°'
                    f'\nВысота · {self.number(s.altitude_m,0)} м\nСкорость · {self.number(s.speed_mps,0)} м/с')
            item = self.target_cards.item(row, 0)
            if item is None:
                item = QTableWidgetItem()
                self.target_cards.setItem(row, 0, item)
            item.setText(text)
            item.setToolTip(text)
            item.setData(Qt.UserRole, key)
            item.setData(Qt.UserRole+1, self.canvas.target_marks.get(key))
            item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        if self.canvas.selected_id in self.table_ids:
            self.target_cards.selectRow(self.table_ids.index(self.canvas.selected_id))
