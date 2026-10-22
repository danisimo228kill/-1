"""Shared vector symbols for Qt and the web map; classification is heuristic."""
import math
from html import escape

KINDS = {'Самолёт':'plane', 'БПЛА':'drone', 'Стационарная вышка':'tower',
         'Наземный':'ground', 'Вертолёт':'helicopter'}
ICONS = {
    'plane': {'polygons': [[(0,-11),(2,-4),(10,2),(10,4),(2,1),(2,7),(5,10),(5,11),(0,9),(-5,11),(-5,10),(-2,7),(-2,1),(-10,4),(-10,2),(-2,-4)]]},
    'drone': {'lines': [[(-7,-7),(7,7)],[(-7,7),(7,-7)]], 'circles':[(-7,-7,4),(7,-7,4),(-7,7,4),(7,7,4),(0,0,2)]},
    'tower': {'lines': [[(-7,11),(0,-5),(7,11)],[(-4,5),(4,5)],[(0,-5),(0,-11)],[(-7,-11),(-9,-7),(-7,-3)],[(7,-11),(9,-7),(7,-3)]], 'circles':[(0,-7,2)]},
    'ground': {'polygons':[[(-6,-7),(6,-7),(8,-2),(8,7),(-8,7),(-8,-2)]], 'lines':[[(-5,-2),(5,-2)],[(-5,8),(-5,11)],[(5,8),(5,11)]]},
    'helicopter': {'lines':[[(-11,-8),(11,-8)],[(0,-8),(0,8)],[(-5,8),(5,8)],[(-7,0),(-7,5)],[(7,0),(7,5)]], 'circles':[(0,-1,5)]},
    'unknown': {'lines':[[(0,-9),(9,0),(0,9),(-9,0),(0,-9)]]},
}


def icon_kind(sample):
    if sample.object_type in KINDS:
        return KINDS[sample.object_type]
    speed, height = sample.speed_mps, sample.altitude_m
    if speed is None or height is None or not math.isfinite(speed) or not math.isfinite(height):
        return 'unknown'
    if height <= 5 and speed <= .3:
        return 'tower'
    if height <= 10:
        return 'ground'
    if speed >= 55 and height >= 300:
        return 'plane'
    return 'drone'


def display_type(sample):
    if sample.object_type in KINDS:
        return sample.object_type
    kind = icon_kind(sample)
    if kind == 'unknown':
        return 'Не определён'
    return {'plane':'Самолёт?', 'drone':'БПЛА?', 'ground':'Наземный?', 'tower':'Стационарный?'}[kind]


def web_icons():
    result = []
    for kind, data in ICONS.items():
        parts = []
        for polygon in data.get('polygons', []):
            points = ' '.join(f'{x},{y}' for x,y in polygon)
            parts.append(f'<polygon points="{points}" fill="currentColor"/>')
        for line in data.get('lines', []):
            points = ' '.join(f'{x},{y}' for x,y in line)
            parts.append(f'<polyline points="{points}"/>')
        for x,y,r in data.get('circles', []):
            parts.append(f'<circle cx="{x}" cy="{y}" r="{r}"/>')
        result.append(f'<svg class="type-icon {escape(kind)}-icon" viewBox="-14 -14 28 28" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">'+''.join(parts)+'</svg>')
    return ''.join(result)


def draw_icon(painter, kind):
    from PyQt5.QtCore import QPointF, Qt
    from PyQt5.QtGui import QPolygonF
    data = ICONS[kind]
    for polygon in data.get('polygons', []):
        painter.drawPolygon(QPolygonF([QPointF(x,y) for x,y in polygon]))
    painter.setBrush(Qt.NoBrush)
    for line in data.get('lines', []):
        painter.drawPolyline(QPolygonF([QPointF(x,y) for x,y in line]))
    for x,y,r in data.get('circles', []):
        painter.drawEllipse(QPointF(x,y), r,r)
