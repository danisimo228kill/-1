"""Small Qt renderer for locally stored Protomaps OSM vector tiles."""
import gzip
import struct
import mmap
from functools import lru_cache
from PyQt5.QtCore import Qt, QPointF, QRectF
from PyQt5.QtGui import QPixmap, QImage, QPainter, QPainterPath, QPen, QColor, QFont, QTransform
from vendor.pmtiles.reader import Reader
from vendor.pmtiles.tile import Compression, TileType


def varint(data, offset):
    value=0
    for shift in range(0,70,7):
        byte=data[offset];offset+=1;value|=(byte&127)<<shift
        if byte<128:return value,offset
    raise ValueError('Invalid vector tile integer')


def fields(data):
    offset=0
    while offset<len(data):
        key,offset=varint(data,offset);wire=key&7
        if wire==0:value,offset=varint(data,offset)
        elif wire in (1,5):
            size=8 if wire==1 else 4;value=data[offset:offset+size];offset+=size
        elif wire==2:
            size,offset=varint(data,offset);value=data[offset:offset+size];offset+=size
        else:raise ValueError('Unsupported vector tile field')
        if offset>len(data):raise ValueError('Truncated vector tile')
        yield key>>3,value


def packed(data):
    offset=0
    while offset<len(data):
        value,offset=varint(data,offset);yield value


def value_of(data):
    for key,value in fields(data):
        if key==1:return value.decode('utf-8','replace')
        if key==2:return struct.unpack('<f',value)[0]
        if key==3:return struct.unpack('<d',value)[0]
        if key in (4,5):return value
        if key==6:return (value>>1)^-(value&1)
        if key==7:return bool(value)


@lru_cache(maxsize=96)
def decode(blob):
    if blob[:2]==b'\x1f\x8b':blob=gzip.decompress(blob)
    layers={}
    for number,raw in fields(blob):
        if number!=3:continue
        name='';extent=4096;keys=[];values=[];features=[]
        for key,value in fields(raw):
            if key==1:name=value.decode('utf-8')
            elif key==2:features.append(value)
            elif key==3:keys.append(value.decode('utf-8'))
            elif key==4:values.append(value_of(value))
            elif key==5:extent=value
        parsed=[]
        for raw_feature in features:
            tags=[];kind=0;geometry=[]
            for key,value in fields(raw_feature):
                if key==2:tags=list(packed(value))
                elif key==3:kind=value
                elif key==4:geometry=list(packed(value))
            props={keys[tags[i]]:values[tags[i+1]] for i in range(0,len(tags),2)}
            parsed.append((kind,geometry,props))
        layers[name]=(extent,parsed)
    return layers


def geometry(commands, factor):
    path=QPainterPath();path.setFillRule(Qt.OddEvenFill)
    points=[];x=y=0;i=0
    while i<len(commands):
        command=commands[i]&7;count=commands[i]>>3;i+=1
        if command==7:
            path.closeSubpath();continue
        if command not in (1,2):raise ValueError('Invalid vector geometry')
        for _ in range(count):
            a,b=commands[i:i+2];i+=2
            x+=(a>>1)^-(a&1);y+=(b>>1)^-(b&1)
            point=QPointF(x*factor,y*factor);points.append(point)
            path.moveTo(point) if command==1 else path.lineTo(point)
    return path,points


@lru_cache(maxsize=48)
def prepared_geometry(blob):
    layers=decode(blob)
    result=[]
    for name in ('earth','landcover','landuse','water','boundaries','roads','places'):
        extent,features=layers.get(name,(4096,[]))
        for kind,commands,props in sorted(features,key=lambda f:f[2].get('sort_rank',0)):
            if name=='roads' and props.get('kind') in ('path','aerialway','other'):continue
            path,points=geometry(commands,256/extent)
            if points:result.append((name,kind,path,points[0],points[len(points)//2],len(points),props))
    return result


def render_image(blob, zoom, theme='day', subtile=(0, 0, 0)):
    delta, offset_x, offset_y = subtile
    scale = 2 ** delta
    visible=QRectF(offset_x*256/scale,offset_y*256/scale,256/scale,256/scale).adjusted(-4/scale,-4/scale,4/scale,4/scale)
    transform=QTransform(scale,0,0,scale,-offset_x*256,-offset_y*256)
    night = {'#b9d7e8':'#122b40', '#edf0df':'#23343e', '#c0d7b4':'#263f36',
             '#e2e1d8':'#34404b', '#d8e5c9':'#2e4138', '#bfd8b4':'#29443b',
             '#dfdfd3':'#35434a', '#aaa5ad':'#687383', '#c8bcb1':'#50606c',
             '#d7a66a':'#b09261', '#ffffff':'#738493', '#8e8e8e':'#63727d', '#273543':'#e0e7ed'}
    def color_of(value):
        color = QColor(night.get(value, value) if theme != 'day' else value)
        return color.lighter(122) if theme == 'dusk' else color
    image=QImage(256,256,QImage.Format_ARGB32_Premultiplied);image.fill(color_of('#b9d7e8'))
    p=QPainter(image);p.setRenderHint(QPainter.Antialiasing)
    labels=[]
    for name,kind,original,first,middle,count,props in prepared_geometry(blob):
        minimum=props.get('min_zoom',0)
        if isinstance(minimum,(int,float)) and minimum>zoom+1:continue
        if not original.boundingRect().adjusted(-1,-1,1,1).intersects(visible):continue
        path=transform.map(original) if delta else original
        subtype=props.get('kind','')
        color='#edf0df';width=0
        if name=='landcover':color={'forest':'#c0d7b4','urban_area':'#e2e1d8'}.get(subtype,'#d8e5c9')
        elif name=='landuse':color='#bfd8b4' if subtype in ('wood','forest','park','garden','grass','nature_reserve','national_park','meadow') else '#dfdfd3'
        elif name=='water':color='#b9d7e8';width=1 if kind==2 else 0
        elif name=='boundaries':color='#aaa5ad';width=.6
        elif name=='roads':
            width={'highway':2.6,'major_road':2,'medium_road':1.5,'minor_road':.9,'rail':.7}.get(subtype,.55)
            color='#d7a66a' if subtype in ('highway','major_road') else '#ffffff' if subtype!='rail' else '#8e8e8e'
        elif name=='places':
            if subtype not in ('city','town','village','hamlet','locality') and zoom<15:continue
            text=props.get('name:ru') or props.get('pgf:name:ru') or props.get('name')
            if text:labels.append((0 if subtype in ('city','town') else 1,transform.map(first),str(text)))
            continue
        if kind==3:p.setPen(Qt.NoPen);p.setBrush(color_of(color))
        else:p.setPen(QPen(color_of(color),width or .8,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin));p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        if name=='roads' and zoom>=13:
            if subtype not in ('highway','major_road') and zoom<16:continue
            text=props.get('name:ru') or props.get('name')
            if text and count>1:labels.append((2,transform.map(middle),str(text)))
    occupied=[]
    for rank,point,text in sorted(labels,key=lambda v:v[0]):
        p.setFont(QFont('DejaVu Sans',11 if rank else 12,QFont.Bold if rank==0 else QFont.Normal))
        fm=p.fontMetrics();rect=QRectF(point.x()-fm.horizontalAdvance(text)/2,point.y()-fm.height()/2,fm.horizontalAdvance(text),fm.height())
        if not QRectF(2,2,252,252).contains(rect) or any(rect.adjusted(-4,-3,4,3).intersects(r) for r in occupied):continue
        occupied.append(rect)
        p.setPen(QColor('#ffffff' if theme == 'day' else '#172631'))
        for dx,dy in ((-1,0),(1,0),(0,-1),(0,1)):p.drawText(rect.translated(dx,dy),Qt.AlignCenter,text)
        p.setPen(color_of('#273543'));p.drawText(rect,Qt.AlignCenter,text)
    p.end();return image


def render_tile(blob, zoom, theme='day', subtile=(0, 0, 0)):
    return QPixmap.fromImage(render_image(blob, zoom, theme, subtile))


class VectorArchive:
    def __init__(self,path):
        self.file=open(path,'rb')
        self.mapping=None
        try:
            self.mapping=mmap.mmap(self.file.fileno(),0,access=mmap.ACCESS_READ)
            self.reader=Reader(self.read_bytes)
            header=self.reader.header()
            if header['tile_type']!=TileType.MVT:raise ValueError('Нужна векторная карта OSM')
            self.metadata=self.reader.metadata()
            self.metadata['format']='pbf'
            self.min_zoom,self.max_zoom=header['min_zoom'],header['max_zoom']
            self.bounds=tuple(header[k]/10000000 for k in ('min_lon_e7','min_lat_e7','max_lon_e7','max_lat_e7'))
        except Exception:
            self.read_bytes.cache_clear()
            if self.mapping:self.mapping.close()
            self.file.close()
            raise

    @lru_cache(maxsize=64)
    def read_bytes(self,offset,length):return self.mapping[offset:offset+length]

    def tile(self,z,x,y):
        if not 0<=y<2**z:return None
        return self.reader.get(z,x%(2**z),y)

    def close(self):
        self.read_bytes.cache_clear();self.mapping.close();self.file.close()
