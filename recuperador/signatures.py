"""
Firmas de archivo (magic bytes) y detección del final de cada formato.

Para no cortar el archivo ni arrastrar datos de los sectores siguientes, el
final se calcula recorriendo la estructura interna de cada formato:

* JPEG  -> segmentos con longitud hasta SOS, datos entrópicos hasta el marcador EOI.
* PNG   -> chunks (longitud + tipo + CRC) hasta IEND.
* GIF   -> bloques y sub-bloques hasta el trailer 0x3B.
* PDF   -> último ``%%EOF`` (sigue las actualizaciones incrementales).
* ZIP   -> registro EOCD, validado contra el offset del directorio central
           (incluye DOCX/XLSX/PPTX/ODT, que son ZIP).
* BMP, WAV, AVI -> tamaño declarado en la cabecera.
* MP4/MOV -> recorrido de cajas (boxes) de nivel superior.

Cada detector recibe ``read(offset, n)`` (offset relativo al inicio del archivo)
y devuelve un ``EndResult`` o ``None`` si no logra determinar el final.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

ReadFn = Callable[[int, int], bytes]

MB = 1024 * 1024
_SCAN_CHUNK = 64 * 1024


@dataclass
class EndResult:
    size: int
    extension: Optional[str] = None   # Para afinar el tipo (p. ej. ZIP -> DOCX)


@dataclass
class FileType:
    key: str                       # identificador corto: "jpg", "png"...
    name: str                      # nombre legible para la interfaz
    extension: str
    prefixes: List[bytes]          # cabeceras posibles al inicio del sector
    find_end: Callable[[ReadFn, int], Optional[EndResult]]
    max_size: int                  # límite de búsqueda del final
    fallback_size: int             # cuánto extraer si no se encuentra el final
    validate: Callable[[bytes], bool] = field(default=lambda s: True)
    is_image: bool = False

    def matches(self, sector: bytes) -> bool:
        prefix_ok = not self.prefixes or any(sector.startswith(p) for p in self.prefixes)
        return prefix_ok and self.validate(sector)


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _find(read: ReadFn, needle: bytes, start: int, limit: int) -> int:
    """Busca ``needle`` desde ``start`` sin pasar de ``limit``. -1 si no aparece."""
    pos = start
    overlap = len(needle) - 1
    while pos < limit:
        chunk = read(pos, min(_SCAN_CHUNK, limit - pos) + overlap)
        if not chunk:
            return -1
        idx = chunk.find(needle)
        if idx != -1 and pos + idx + len(needle) <= limit:
            return pos + idx
        if len(chunk) <= overlap:
            return -1
        pos += len(chunk) - overlap
    return -1


# ---------------------------------------------------------------------------
# JPEG
# ---------------------------------------------------------------------------

def _jpeg_end(read: ReadFn, max_size: int) -> Optional[EndResult]:
    pos = 2  # tras SOI (FF D8)
    while pos < max_size:
        head = read(pos, 4)
        if len(head) < 2 or head[0] != 0xFF:
            return None
        # Bytes de relleno 0xFF permitidos antes de un marcador.
        while len(head) >= 2 and head[1] == 0xFF:
            pos += 1
            head = read(pos, 4)
        if len(head) < 2:
            return None
        marker = head[1]
        if marker == 0xD9:                       # EOI: fin de imagen
            return EndResult(pos + 2)
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            pos += 2                             # marcadores sin longitud
            continue
        if len(head) < 4:
            return None
        length = struct.unpack(">H", head[2:4])[0]
        if length < 2:
            return None
        pos += 2 + length
        if marker == 0xDA:                       # SOS: siguen datos comprimidos
            pos = _jpeg_skip_entropy(read, pos, max_size)
            if pos < 0:
                return None
    return None


def _jpeg_skip_entropy(read: ReadFn, pos: int, limit: int) -> int:
    """Avanza por los datos entrópicos hasta el siguiente marcador real (FF xx)."""
    while pos < limit:
        chunk = read(pos, min(_SCAN_CHUNK, limit - pos) + 1)
        if len(chunk) < 2:
            return -1
        i = chunk.find(b"\xff")
        while i != -1 and i + 1 < len(chunk):
            nxt = chunk[i + 1]
            # FF00 = byte FF escapado; FFD0-D7 = reinicios; FFFF = relleno.
            if nxt != 0x00 and not (0xD0 <= nxt <= 0xD7) and nxt != 0xFF:
                return pos + i
            i = chunk.find(b"\xff", i + 1)
        pos += len(chunk) - 1
    return -1


# ---------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------

def _png_end(read: ReadFn, max_size: int) -> Optional[EndResult]:
    pos = 8
    while pos < max_size:
        head = read(pos, 8)
        if len(head) < 8:
            return None
        length, ctype = struct.unpack(">I4s", head)
        if length > 0x7FFFFFFF or not ctype.isalpha():
            return None
        pos += 12 + length                       # longitud + tipo + datos + CRC
        if ctype == b"IEND":
            return EndResult(pos)
    return None


# ---------------------------------------------------------------------------
# GIF
# ---------------------------------------------------------------------------

def _gif_skip_subblocks(read: ReadFn, pos: int, limit: int) -> int:
    while pos < limit:
        n = read(pos, 1)
        if not n:
            return -1
        pos += 1
        if n[0] == 0:
            return pos
        pos += n[0]
    return -1


def _gif_end(read: ReadFn, max_size: int) -> Optional[EndResult]:
    lsd = read(6, 7)
    if len(lsd) < 7:
        return None
    pos = 13
    if lsd[4] & 0x80:                                     # tabla de color global
        pos += 3 * (2 ** ((lsd[4] & 0x07) + 1))
    while pos < max_size:
        b = read(pos, 1)
        if not b:
            return None
        if b[0] == 0x3B:                                  # trailer
            return EndResult(pos + 1)
        if b[0] == 0x21:                                  # extensión
            pos = _gif_skip_subblocks(read, pos + 2, max_size)
        elif b[0] == 0x2C:                                # descriptor de imagen
            desc = read(pos, 10)
            if len(desc) < 10:
                return None
            pos += 10
            if desc[9] & 0x80:                            # tabla de color local
                pos += 3 * (2 ** ((desc[9] & 0x07) + 1))
            pos = _gif_skip_subblocks(read, pos + 1, max_size)  # +1: LZW min code size
        else:
            return None
        if pos < 0:
            return None
    return None


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

_PDF_CONTINUES = re.compile(rb"^\s*(\d+\s+\d+\s+obj|xref)")


def _pdf_end(read: ReadFn, max_size: int) -> Optional[EndResult]:
    pos, end = 5, None
    while True:
        idx = _find(read, b"%%EOF", pos, max_size)
        if idx < 0:
            break
        end = idx + 5
        tail = read(end, 2)
        if tail.startswith(b"\r\n"):
            end += 2
        elif tail[:1] in (b"\r", b"\n"):
            end += 1
        # ¿Sigue una actualización incremental? Entonces el PDF continúa.
        if not _PDF_CONTINUES.match(read(end, 64)):
            break
        pos = end
    return EndResult(end) if end else None


# ---------------------------------------------------------------------------
# ZIP / Office Open XML / OpenDocument
# ---------------------------------------------------------------------------

def _zip_kind(central_dir: bytes) -> str:
    if b"word/" in central_dir:
        return "docx"
    if b"xl/" in central_dir:
        return "xlsx"
    if b"ppt/" in central_dir:
        return "pptx"
    return "zip"


def _zip_end(read: ReadFn, max_size: int) -> Optional[EndResult]:
    pos = 4
    while True:
        idx = _find(read, b"PK\x05\x06", pos, max_size)
        if idx < 0:
            return None
        eocd = read(idx, 22)
        if len(eocd) < 22:
            return None
        cd_size, cd_offset, comment_len = struct.unpack("<IIH", eocd[12:22])
        # El directorio central debe acabar justo en el EOCD.
        # Descarta firmas EOCD casuales dentro de datos comprimidos.
        if cd_offset + cd_size == idx or cd_offset == 0xFFFFFFFF:
            ext = _zip_kind(read(cd_offset, min(cd_size, 64 * 1024))) \
                if cd_offset != 0xFFFFFFFF else "zip"
            if ext == "zip":
                first = read(30, 8 + 39)  # nombre de la 1.ª entrada: 'mimetype' en ODF
                if first.startswith(b"mimetype") and b"opendocument.text" in first:
                    ext = "odt"
            return EndResult(idx + 22 + comment_len, ext)
        pos = idx + 4


# ---------------------------------------------------------------------------
# Formatos con tamaño en la cabecera
# ---------------------------------------------------------------------------

def _bmp_valid(s: bytes) -> bool:
    if len(s) < 18:
        return False
    size, reserved, offset, dib = struct.unpack("<IIII", s[2:18])
    return reserved == 0 and 26 <= offset <= 4096 and dib in (12, 40, 52, 56, 108, 124) \
        and size > offset


def _bmp_end(read: ReadFn, max_size: int) -> Optional[EndResult]:
    size = struct.unpack("<I", read(2, 4))[0]
    return EndResult(size) if size <= max_size else None


def _riff_valid(s: bytes) -> bool:
    return s[8:12] in (b"WAVE", b"AVI ")


def _riff_end(read: ReadFn, max_size: int) -> Optional[EndResult]:
    head = read(4, 8)
    size = struct.unpack("<I", head[:4])[0] + 8
    ext = "wav" if head[4:8] == b"WAVE" else "avi"
    return EndResult(size, ext) if size <= max_size else None


# ---------------------------------------------------------------------------
# MP4 / MOV (ISO Base Media File Format)
# ---------------------------------------------------------------------------

_MP4_BOX = re.compile(rb"^[a-zA-Z0-9 ]{4}$")


def _mp4_valid(s: bytes) -> bool:
    size = struct.unpack(">I", s[:4])[0]
    return s[4:8] == b"ftyp" and 8 <= size <= 256


def _mp4_end(read: ReadFn, max_size: int) -> Optional[EndResult]:
    pos = 0
    seen_media = False
    while pos < max_size:
        head = read(pos, 16)
        if len(head) < 8 or not _MP4_BOX.match(head[4:8]):
            break
        size = struct.unpack(">I", head[:4])[0]
        if size == 1:                                # tamaño de 64 bits
            if len(head) < 16:
                break
            size = struct.unpack(">Q", head[8:16])[0]
        elif size == 0:                              # "hasta el final" -> desconocido
            return None
        if size < 8:
            break
        if head[4:8] in (b"moov", b"mdat"):
            seen_media = True
        pos += size
    if pos == 0 or not seen_media:
        return None
    return EndResult(min(pos, max_size))


# ---------------------------------------------------------------------------
# Registro de formatos
# ---------------------------------------------------------------------------

FILE_TYPES: Dict[str, FileType] = {t.key: t for t in [
    FileType("jpg", "Imagen JPEG", "jpg",
             [b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"\xff\xd8\xff\xdb",
              b"\xff\xd8\xff\xee", b"\xff\xd8\xff\xe2"],
             _jpeg_end, 64 * MB, 8 * MB, is_image=True),
    FileType("png", "Imagen PNG", "png", [b"\x89PNG\r\n\x1a\n"],
             _png_end, 64 * MB, 8 * MB, is_image=True),
    FileType("gif", "Imagen GIF", "gif", [b"GIF87a", b"GIF89a"],
             _gif_end, 32 * MB, 4 * MB, is_image=True),
    FileType("bmp", "Imagen BMP", "bmp", [b"BM"],
             _bmp_end, 256 * MB, 8 * MB, validate=_bmp_valid, is_image=True),
    FileType("pdf", "Documento PDF", "pdf", [b"%PDF-"],
             _pdf_end, 256 * MB, 16 * MB),
    FileType("zip", "ZIP / Office", "zip", [b"PK\x03\x04"],
             _zip_end, 1024 * MB, 32 * MB),
    FileType("riff", "Audio/Vídeo RIFF (WAV/AVI)", "wav", [b"RIFF"],
             _riff_end, 4096 * MB, 32 * MB, validate=_riff_valid),
    FileType("mp4", "Vídeo MP4/MOV", "mp4", [],
             _mp4_end, 4096 * MB, 64 * MB, validate=_mp4_valid),
]}

# Índice por los 2 primeros bytes: evita probar todos los formatos en cada sector.
_BY_PREFIX: Dict[bytes, List[FileType]] = {}
for _t in FILE_TYPES.values():
    for _p in _t.prefixes:
        _BY_PREFIX.setdefault(_p[:2], []).append(_t)


def identify(sector: bytes, enabled: Optional[set] = None) -> Optional[FileType]:
    """Devuelve el formato cuya firma aparece al inicio del sector, o None."""
    for t in _BY_PREFIX.get(sector[:2], ()):
        if (enabled is None or t.key in enabled) and t.matches(sector):
            return t
    if sector[4:8] == b"ftyp":
        mp4 = FILE_TYPES["mp4"]
        if (enabled is None or "mp4" in enabled) and mp4.matches(sector):
            return mp4
    return None
