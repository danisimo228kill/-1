"""Local map catalog and cancellable preparation of detailed OSM regions."""
from pathlib import Path
from dashboard import radar_bounds
from model import world_pixel, destination
from vector_map import VectorArchive

ROOT = Path(__file__).resolve().parent
REGIONS = ROOT / 'data' / 'regions'
DETAIL_ZOOM = 12


def covers(bounds, lat, lon):
    sw, ne = radar_bounds(lat, lon)
    west, south, east, north = bounds
    return south <= sw[0] and north >= ne[0] and (east-west >= 359 or west <= sw[1] and east >= ne[1])


def find_region(lat, lon, folder=REGIONS, min_zoom=0):
    candidates = []
    for path in sorted(folder.glob('*.pmtiles')):
        archive = None
        try:
            archive = VectorArchive(path)
            if archive.max_zoom < min_zoom or not covers(archive.bounds, lat, lon):
                continue
            z = min(archive.max_zoom, 8)
            points = [(lat, lon)] + [destination(lat, lon, a, 70000) for a in range(0, 360, 45)]
            if all(archive.tile(z, int(world_pixel(a,b,z)[0]//256), int(world_pixel(a,b,z)[1]//256)) for a,b in points):
                candidates.append((archive.max_zoom, path))
        except (OSError, ValueError, KeyError, IndexError):
            continue
        finally:
            if archive:
                archive.close()
    return max(candidates, key=lambda item:item[0])[1] if candidates else None


def download_geometry(lat, lon):
    sw, ne = radar_bounds(lat, lon, 72000)
    west, south, east, north = sw[1], max(-85.0511, sw[0]), ne[1], min(85.0511, ne[0])
    segments = [(west, east)]
    if east > 180:
        segments = [(west, 180), (-180, east-360)]
    elif west < -180:
        segments = [(west+360, 180), (-180, east)]
    rings = [[[[a,south],[b,south],[b,north],[a,north],[a,south]]] for a,b in segments]
    return {'type':'MultiPolygon','coordinates':rings}


import json
import re
import subprocess
import sys
import uuid
from urllib.request import Request, urlopen
from PyQt5.QtCore import QThread, pyqtSignal


class RegionDownload(QThread):
    progress = pyqtSignal(str)
    completed = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, lat, lon, parent=None):
        super().__init__(parent)
        self.lat, self.lon = lat, lon
        self.process = None

    def cancel(self):
        self.requestInterruption()
        if self.process and self.process.poll() is None:
            self.process.terminate()

    def run(self):
        temporary = geometry_path = None
        try:
            self.progress.emit('Подключаемся к источнику карты…')
            request = Request('https://build-metadata.protomaps.dev/builds.json', headers={'User-Agent':'KonturOffline/1.0'})
            with urlopen(request, timeout=20) as response:
                builds = json.load(response)
            builds = [b for b in builds if re.fullmatch(r'\d{8}\.pmtiles', b.get('key','')) and str(b.get('version','')).startswith('4.')]
            if not builds:
                raise ValueError('Нет совместимой сборки карты')
            source = 'https://build.protomaps.com/'+max(builds, key=lambda b:b['key'])['key']
            if self.isInterruptionRequested():raise InterruptedError()
            REGIONS.mkdir(parents=True, exist_ok=True)
            temporary = REGIONS/(uuid.uuid4().hex+'.partial')
            geometry_path = temporary.with_suffix('.geojson')
            geometry_path.write_text(json.dumps(download_geometry(self.lat,self.lon)), encoding='utf-8')
            target = REGIONS/f'osm-{self.lat:.5f}-{self.lon:.5f}-70km-z{DETAIL_ZOOM}.pmtiles'
            executable = ROOT/'tools'/('pmtiles.exe' if sys.platform=='win32' else 'pmtiles')
            if not executable.exists():raise ValueError('Не найден tools/pmtiles')
            if sys.platform!='win32':executable.chmod(0o755)
            self.progress.emit('Скачиваем район 70 км…')
            self.process = subprocess.Popen([str(executable),'extract',source,str(temporary),
                '--region='+str(geometry_path),f'--maxzoom={DETAIL_ZOOM}','--download-threads=4'],
                stdout=subprocess.PIPE,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if self.isInterruptionRequested():self.process.terminate()
            buffer = bytearray()
            while True:
                byte = self.process.stdout.read(1)
                if not byte:break
                if byte in (b'\r',b'\n'):
                    line=buffer.decode('utf-8','replace');buffer.clear()
                    match=re.search(r'(\d+)%',line)
                    if match:self.progress.emit('Скачиваем район 70 км: '+match[1]+'%.')
                elif len(buffer)<4096:buffer.extend(byte)
            code=self.process.wait()
            if self.isInterruptionRequested():raise InterruptedError()
            if code:raise RuntimeError('Загрузка не завершена. Проверь интернет и свободное место; сохранённые карты доступны.')
            archive=VectorArchive(temporary)
            try:
                valid=covers(archive.bounds,self.lat,self.lon) and archive.max_zoom>=DETAIL_ZOOM
                z=archive.max_zoom
                x,y=world_pixel(self.lat,self.lon,z)
                valid=valid and bool(archive.tile(z,int(x//256),int(y//256)))
            finally:archive.close()
            if not valid:raise ValueError('Подробная карта не покрывает текущий район')
            temporary.replace(target);temporary=None
            self.completed.emit(str(target))
        except InterruptedError:
            self.failed.emit('Загрузка отменена. Сохранённые карты доступны.')
        except Exception as exc:
            self.failed.emit('Не удалось скачать район. '+str(exc)[:180])
        finally:
            if self.process and self.process.poll() is None:
                self.process.terminate();self.process.wait()
            for path in (temporary, geometry_path):
                if path and path.exists():path.unlink()
