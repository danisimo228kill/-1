"""Geographic degrees. EPSG:5044, Pulkovo 1942 to WGS 84 (20).

Parameters: PROJ data/sql/helmert_transformation.sql, EPSG 5044.
Coordinate-frame rotation, accuracy 3 m in the operation's Russian area.
Heights are not transformed: this is the EPSG geog2D operation (input h=0).
"""
import math

WGS84 = 'WGS84'
SK42 = 'SK42'
NAMES = {WGS84: 'WGS 84', SK42: 'СК-42'}
ELLIPSOIDS = {WGS84: (6378137.0, 298.257223563), SK42: (6378245.0, 298.3)}


def _cartesian(lat, lon, system):
    a, inverse_f = ELLIPSOIDS[system]
    f = 1 / inverse_f
    e2 = f * (2 - f)
    lat, lon = math.radians(lat), math.radians(lon)
    n = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    return n * math.cos(lat) * math.cos(lon), n * math.cos(lat) * math.sin(lon), n * (1-e2) * math.sin(lat)


def _geographic(x, y, z, system):
    a, inverse_f = ELLIPSOIDS[system]
    f = 1 / inverse_f
    e2 = f * (2-f)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1-e2))
    for _ in range(12):
        n = a / math.sqrt(1-e2*math.sin(lat)**2)
        lat = math.atan2(z+e2*n*math.sin(lat), p)
    return math.degrees(lat), math.degrees(math.atan2(y, x))


def convert(lat, lon, source=WGS84, target=WGS84):
    if source not in NAMES or target not in NAMES:
        raise ValueError('Неизвестная система координат')
    if not all(math.isfinite(v) for v in (lat, lon)) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError('Некорректные координаты')
    if source == target:
        return lat, lon
    x, y, z = _cartesian(lat, lon, source)
    # EPSG coordinate-frame convention, opposite signs to position-vector.
    ry, rz = math.radians(.35/3600), math.radians(.79/3600)
    scale = 1 - .22e-6
    if source == SK42:
        x, y, z = (23.57 + scale*(x-rz*y+ry*z),
                   -140.95 + scale*(rz*x+y),
                   -79.8 + scale*(-ry*x+z))
    else:
        a, b, c = (x-23.57)/scale, (y+140.95)/scale, (z+79.8)/scale
        x = (a+rz*b-ry*c)/(1+rz*rz+ry*ry)
        y, z = b-rz*x, c+ry*x
    return _geographic(x, y, z, target)


def coordinate_text(lat, lon, system=WGS84):
    lat, lon = convert(lat, lon, WGS84, system)
    return f'{NAMES[system]} · {lat:.6f}°, {lon:.6f}°'


def bearing_sector(origin, position):
    a, b = math.radians(origin[0]), math.radians(position[0])
    d = math.radians(position[1]-origin[1])
    bearing = math.degrees(math.atan2(math.sin(d)*math.cos(b),
        math.cos(a)*math.sin(b)-math.sin(a)*math.cos(b)*math.cos(d))) % 360
    start = int(bearing // 30) * 30
    return f'{start:03d}–{start+30:03d}°'


def scale_bar(lat, zoom, max_pixels=140):
    mpp = 2*math.pi*6378137*math.cos(math.radians(lat))/(256*2**zoom)
    maximum = mpp*max_pixels
    power = 10**math.floor(math.log10(maximum))
    length = max(v*power for v in (.1, .2, .5, 1, 2, 5) if v*power <= maximum)
    label = f'{length/1000:g} км' if length >= 1000 else f'{length:g} м'
    return length/mpp, label, mpp
