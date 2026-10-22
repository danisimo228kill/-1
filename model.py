"""Display model. Hardware decoding and track association are deliberately external."""
from __future__ import annotations
from dataclasses import dataclass
import math
import random
import sqlite3
from pathlib import Path

EARTH_RADIUS = 6371008.8
MERCATOR_LIMIT = 85.05112878
RADAR_RATE = 45.0


def world_pixel(lat, lon, zoom):
    lat = max(-MERCATOR_LIMIT, min(MERCATOR_LIMIT, lat))
    size = 256 * 2 ** zoom
    sin_lat = math.sin(math.radians(lat))
    return ((lon + 180) / 360 * size,
            (0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)) * size)


def latlon_from_pixel(x, y, zoom):
    size = 256 * 2 ** zoom
    y = min(size, max(0, y))
    return (math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / size)))),
            (x / size * 360) % 360 - 180)


def destination(lat, lon, bearing_deg, distance_m):
    """Spherical geodesic for demonstration paths only, not sensor calibration."""
    lat, lon, bearing = map(math.radians, (lat, lon, bearing_deg))
    arc = distance_m / EARTH_RADIUS
    out_lat = math.asin(math.sin(lat) * math.cos(arc) +
                        math.cos(lat) * math.sin(arc) * math.cos(bearing))
    out_lon = lon + math.atan2(math.sin(bearing) * math.sin(arc) * math.cos(lat),
                               math.cos(arc) - math.sin(lat) * math.sin(out_lat))
    return math.degrees(out_lat), (math.degrees(out_lon) + 180) % 360 - 180


@dataclass(frozen=True)
class Sample:
    track_id: str
    timestamp: float
    lat: float
    lon: float
    altitude_m: float | None
    ground_range_m: float | None
    speed_mps: float | None
    course_deg: float | None
    object_type: str = 'Не определён'
    appearance: int = 0


class Tracks:
    def __init__(self):
        self.history = {}
        self.active_ids = set()
        self.episode_starts = {}

    def add(self, sample):
        if not sample.track_id or not all(math.isfinite(v) for v in
                (sample.timestamp, sample.lat, sample.lon)):
            raise ValueError('Invalid sample')
        if not -90 <= sample.lat <= 90 or not -180 <= sample.lon <= 180:
            raise ValueError('Coordinates out of range')
        history = self.history.setdefault(sample.track_id, [])
        if history and sample.timestamp <= history[-1].timestamp:
            return False
        if not history or history[-1].appearance != sample.appearance or sample.track_id not in self.active_ids:
            self.episode_starts[sample.track_id] = len(history)
        history.append(sample)
        self.active_ids.add(sample.track_id)
        return True

    def synchronize(self, samples):
        """Replace the active set, retaining ended tracks for session export."""
        samples = list(samples)
        for sample in samples:
            self.add(sample)
        self.active_ids = {s.track_id for s in samples}

    def active_history(self):
        return {key: self.history[key] for key in sorted(self.active_ids)}

    def tail(self, track_id, mode):
        points = self.history.get(track_id, [])
        start = self.episode_starts.get(track_id, 0)
        if mode == 'short':
            start = max(start, len(points)-5)
        return points[start:] if mode in ('short', 'full') else []



class TrafficSource:
    """Synthetic traffic with bounded manoeuvres, arrivals and departures.

    A fixed fleet reappears in separate observation episodes. Profiles are
    visual scenarios; altitude is an arbitrary local reference.
    Fixed integration steps keep behaviour independent of display frequency.
    """
    MAX_OBJECTS = 10
    MIN_OBJECTS = 7
    POOL_SIZE = 15
    AIRBORNE_TYPES = ('Самолёт', 'БПЛА', 'Вертолёт')
    MAX_RANGE_M = 50000
    PROFILES = {
        'Самолёт': (65, 145, 900, 2600, 2.0, 1.3, 4.0),
        'БПЛА': (5, 24, 40, 220, 12.0, 1.5, 1.3),
        'Стационарная вышка': (0, 0, 15, 60, 0, 0, 0),
        'Наземный': (2, 16, 0, 0, 22.0, 1.4, 0.0),
        'Вертолёт': (12, 55, 100, 500, 7.0, 2.0, 2.5),
    }

    def __init__(self, lat=60.022612, lon=30.293248, seed=None, bounds=None):
        self.lat, self.lon = lat, lon
        self.random = random.Random(seed)
        self.objects = {}
        self.pool = {f'T-{i+1:03}': dict(kind=self.AIRBORNE_TYPES[i % 3],
                     appearance=0, last_seen=-1) for i in range(self.POOL_SIZE)}
        self.time = 0.0
        self.bounds = None
        self.motion_bounds = None
        self.set_bounds(bounds, replenish=False)
        self.next_spawn = self.random.uniform(2, 5)
        for i in range(self.MAX_OBJECTS):
            self._spawn()

    def set_bounds(self, bounds, replenish=True):
        """Constrain synthetic traffic to the currently displayed offline map."""
        bounds = tuple(bounds) if bounds else None
        if bounds == self.bounds:
            return False
        self.bounds = bounds
        if bounds:
            west, south, east, north = bounds
            # Leave a small buffer so symbols do not start on the map edge.
            dx, dy = (east-west)*.04, (north-south)*.04
            self.motion_bounds = west+dx, south+dy, east-dx, north-dy
        else:
            self.motion_bounds = None
        for key, obj in list(self.objects.items()):
            if not self._inside(obj['lat'], obj['lon']):
                del self.objects[key]
        if replenish:
            while len(self.objects) < self.MIN_OBJECTS:
                if not self._spawn():
                    break
        return True

    def _inside(self, lat, lon):
        if self.motion_bounds is None:
            return True
        west, south, east, north = self.motion_bounds
        return west <= lon <= east and south <= lat <= north

    def _spawn(self, lifetime=None):
        available = [key for key in self.pool if key not in self.objects]
        if not available or len(self.objects) >= self.MAX_OBJECTS:
            return False
        if self.bounds:
            west, south, east, north = self.bounds
            if not (west <= self.lon <= east and south <= self.lat <= north):
                return False
        rng = self.random
        # Rotate through the fixed fleet, preferring objects absent the longest.
        key = min(available, key=lambda key: (self.pool[key]['last_seen'], key))
        identity = self.pool[key]
        kind = identity['kind']
        lo, hi, low_h, high_h, *_ = self.PROFILES[kind]
        angle = (int(key[2:]) * 137.5 + rng.uniform(-14, 14)) % 360
        # Spread starting positions across the view, away from the antenna.
        lat, lon = destination(self.lat, self.lon, angle, math.sqrt(rng.uniform(0, self.MAX_RANGE_M ** 2)))
        if not self._inside(lat, lon):
            for _ in range(64):
                angle = rng.uniform(0, 360)
                lat, lon = destination(self.lat, self.lon, angle, math.sqrt(rng.uniform(0, self.MAX_RANGE_M ** 2)))
                if self._inside(lat, lon):
                    break
            else:
                west, south, east, north = self.motion_bounds
                lat, lon = min(north, max(south, lat)), min(east, max(west, lon))
        course = (angle + 180 + rng.uniform(-55, 55)) % 360
        speed, altitude = rng.uniform(lo, hi), rng.uniform(low_h, high_h)
        identity['appearance'] += 1
        identity['last_seen'] = self.time
        self.objects[key] = dict(kind=kind, lat=lat, lon=lon, appearance=identity['appearance'],
            course=course, wanted_course=course, speed=speed, wanted_speed=speed,
            altitude=altitude, wanted_altitude=altitude,
            born=self.time, change=self.time + rng.uniform(2, 4),
            expires=self.time + (lifetime if lifetime is not None else rng.uniform(10, 15)))
        return True

    @staticmethod
    def approach(value, target, step):
        return value + max(-step, min(step, target - value))

    def _step(self, dt):
        self.time += dt
        rng = self.random
        for key, obj in list(self.objects.items()):
            if self.time >= obj['expires']:
                del self.objects[key]
                continue
            lo, hi, low_h, high_h, turn, accel, climb = self.PROFILES[obj['kind']]
            if self.time >= obj['change']:
                obj['wanted_course'] = (obj['course'] + rng.uniform(-65, 65)) % 360
                obj['wanted_speed'] = rng.uniform(lo, hi)
                if obj['kind'] in ('Вертолёт', 'БПЛА', 'Наземный') and rng.random() < 0.22:
                    obj['wanted_speed'] = 0
                obj['wanted_altitude'] = rng.uniform(low_h, high_h)
                obj['change'] = self.time + rng.uniform(2, 7)
            delta = (obj['wanted_course'] - obj['course'] + 180) % 360 - 180
            obj['course'] = (obj['course'] + max(-turn * dt, min(turn * dt, delta))) % 360
            obj['speed'] = self.approach(obj['speed'], obj['wanted_speed'], accel * dt)
            obj['altitude'] = self.approach(obj['altitude'], obj['wanted_altitude'], climb * dt)
            lat, lon = destination(obj['lat'], obj['lon'], obj['course'], obj['speed'] * dt)
            if self._inside(lat, lon) and self.range_m(lat, lon) <= self.MAX_RANGE_M:
                obj['lat'], obj['lon'] = lat, lon
            else:
                # Stay in coverage while turning inward; do not shorten an appearance.
                a, b = math.radians(obj['lat']), math.radians(self.lat)
                d = math.radians(self.lon-obj['lon'])
                obj['wanted_course'] = math.degrees(math.atan2(math.sin(d)*math.cos(b),
                    math.cos(a)*math.sin(b)-math.sin(a)*math.cos(b)*math.cos(d))) % 360
        while len(self.objects) < self.MIN_OBJECTS:
            if not self._spawn():
                break
        if self.time >= self.next_spawn:
            batch = min(1, self.MAX_OBJECTS - len(self.objects))
            for _ in range(batch):
                self._spawn()
            self.next_spawn = self.time + rng.uniform(2, 7)

    def range_m(self, lat, lon):
        a, b = math.radians(self.lat), math.radians(lat)
        dlat, dlon = b - a, math.radians(lon - self.lon)
        h = math.sin(dlat / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(dlon / 2) ** 2
        return 2 * EARTH_RADIUS * math.asin(min(1, math.sqrt(h)))

    def samples(self, elapsed, timestamp):
        while self.time + 0.25 <= elapsed + 1e-9:
            self._step(0.25)
        return [Sample(key, timestamp, o['lat'], o['lon'], o['altitude'],
                       self.range_m(o['lat'], o['lon']), o['speed'], o['course'], o['kind'], o['appearance'])
                for key, o in self.objects.items()]


class MBTiles:
    """Read-only raster MBTiles. Vector archives are rejected explicitly."""
    def __init__(self, path):
        resolved = Path(path).resolve()
        self.db = sqlite3.connect(resolved.as_uri() + '?mode=ro', uri=True)
        try:
            self.metadata = dict(self.db.execute('SELECT name,value FROM metadata'))
            fmt = self.metadata.get('format', '').lower()
            if fmt not in ('png', 'jpg', 'jpeg', 'webp'):
                raise ValueError('Нужен растровый MBTiles (PNG/JPEG/WebP), не векторный.')
            if self.metadata.get('scheme', 'tms').lower() != 'tms':
                raise ValueError('Поддерживается стандартная схема MBTiles TMS.')
            lo, hi = self.db.execute('SELECT MIN(zoom_level),MAX(zoom_level) FROM tiles').fetchone()
            if lo is None or not 0 <= lo <= hi <= 22:
                raise ValueError('В файле нет поддерживаемых уровней масштаба 0–22.')
            self.min_zoom, self.max_zoom = lo, hi
            self.bounds = None
            if 'bounds' in self.metadata:
                bounds = tuple(map(float, self.metadata['bounds'].split(',')))
                if len(bounds) == 4 and all(math.isfinite(v) for v in bounds):
                    self.bounds = bounds
        except Exception:
            self.db.close()
            raise

    def tile(self, z, x, y):
        if not 0 <= y < 2 ** z:
            return None
        row = self.db.execute(
            'SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?',
            (z, x % 2 ** z, 2 ** z - 1 - y)).fetchone()
        return row[0] if row else None

    def close(self):
        self.db.close()
