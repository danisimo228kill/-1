"""Shared text for native and Yandex target labels; units are always explicit."""
import math

FIELDS = ('speed', 'altitude', 'range')
MARK_NAMES = {'green': 'Свой', 'red': 'Чужой', 'blue': 'Неопознанный'}


def target_name(track_id):
    text = str(track_id)
    if text.startswith('T-') and text[2:].isdigit():
        return f'Цель №{int(text[2:])}'
    return text


def metric_lines(sample, fields):
    values = {
        'speed': ('V', sample.speed_mps, 1, 'м/с'),
        'altitude': ('H', sample.altitude_m, 0, 'м'),
        'range': ('R', None if sample.ground_range_m is None else sample.ground_range_m / 1000, 2, 'км'),
    }
    parts = []
    for field in FIELDS:
        if field in fields:
            name, value, precision, unit = values[field]
            text = '—' if value is None or not math.isfinite(value) else f'{value:.{precision}f} {unit}'
            parts.append(f'{name} {text}')
    return parts


def target_table_name(track_id, mark=None):
    return f'{target_name(track_id)} · {MARK_NAMES.get(mark, "Неопознанный")}'
