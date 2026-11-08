"""
Motor de recuperación (file carving), independiente de la interfaz.

En ISO 9660 y UDF cada archivo empieza al principio de un sector de 2048 bytes,
así que solo se comprueba la cabecera de cada sector en vez de buscar firmas
byte a byte. Es más rápido y reduce mucho los falsos positivos.

No depende de tkinter: lo usan la GUI, la CLI y los tests.
"""

from __future__ import annotations

import csv
import hashlib
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Set

from .signatures import FILE_TYPES, FileType, identify
from .sources import SECTOR_SIZE, ReadError, SectorSource


# ---------------------------------------------------------------------------
# Lector con caché y tolerancia a sectores dañados
# ---------------------------------------------------------------------------

class SectorReader:
    """
    Envuelve una ``SectorSource`` y añade:

    * Lectura por bloques (32 sectores = 64 KB por llamada al sistema), que es
      más rápido que leer sector a sector.
    * Si un bloque falla, se reintenta sector a sector para aislar exactamente
      cuáles están dañados; esos se sustituyen por ceros y se registran.
    * Caché LRU, para que buscar el final de un archivo y seguir escaneando no
      obligue a releer el disco.
    * Un cerrojo, porque la GUI puede pedir una vista previa mientras otro hilo lee.
    """

    def __init__(self, source: SectorSource, block_sectors: int = 32,
                 retries: int = 2, cache_blocks: int = 256,
                 on_bad_sector: Optional[Callable[[int], None]] = None):
        self.source = source
        self.block_sectors = block_sectors
        self.retries = retries
        self.bad_sectors: Set[int] = set()
        self.on_bad_sector = on_bad_sector
        self._cache: "OrderedDict[int, bytes]" = OrderedDict()
        self._cache_blocks = cache_blocks
        self._lock = threading.RLock()

    @property
    def total_sectors(self) -> int:
        return self.source.total_sectors

    @property
    def size_bytes(self) -> int:
        return self.source.total_sectors * SECTOR_SIZE

    def _read_one_sector(self, lba: int) -> bytes:
        for _ in range(self.retries + 1):
            try:
                return self.source.read_sectors(lba, 1)
            except ReadError:
                continue
        if lba not in self.bad_sectors:
            self.bad_sectors.add(lba)
            if self.on_bad_sector:
                self.on_bad_sector(lba)
        return b"\x00" * SECTOR_SIZE

    def read_block(self, block: int) -> bytes:
        with self._lock:
            cached = self._cache.get(block)
            if cached is not None:
                self._cache.move_to_end(block)
                return cached
            first = block * self.block_sectors
            count = min(self.block_sectors, self.total_sectors - first)
            if count <= 0:
                return b""
            try:
                data = self.source.read_sectors(first, count)
            except ReadError:
                data = b"".join(self._read_one_sector(first + i) for i in range(count))
            self._cache[block] = data
            if len(self._cache) > self._cache_blocks:
                self._cache.popitem(last=False)
            return data

    def read(self, offset: int, length: int) -> bytes:
        """Lectura a nivel de byte (puede cruzar bloques). Se recorta al final del medio."""
        if offset < 0 or length <= 0 or offset >= self.size_bytes:
            return b""
        length = min(length, self.size_bytes - offset)
        block_bytes = self.block_sectors * SECTOR_SIZE
        out = bytearray()
        while length > 0:
            block, inner = divmod(offset, block_bytes)
            data = self.read_block(block)
            piece = data[inner:inner + length]
            if not piece:
                break
            out += piece
            offset += len(piece)
            length -= len(piece)
        return bytes(out)

    def has_bad_sectors(self, first_lba: int, n_sectors: int) -> bool:
        return any(first_lba <= b < first_lba + n_sectors for b in self.bad_sectors)


# ---------------------------------------------------------------------------
# Resultados
# ---------------------------------------------------------------------------

@dataclass
class FoundFile:
    index: int
    kind: FileType
    sector: int
    size: Optional[int]       # None = no se encontró el final (se usará fallback)
    extension: str
    damaged: bool = False     # contiene sectores que no se pudieron leer

    @property
    def size_known(self) -> bool:
        return self.size is not None

    @property
    def extract_size(self) -> int:
        return self.size if self.size is not None else self.kind.fallback_size

    @property
    def filename(self) -> str:
        return f"recuperado_{self.index:05d}_sector{self.sector}.{self.extension}"

    @property
    def status(self) -> str:
        if self.damaged:
            return "Dañado (sectores ilegibles)"
        if not self.size_known:
            return "Tamaño estimado"
        return "OK"


@dataclass
class ScanResult:
    files: List[FoundFile]
    sectors_scanned: int
    total_sectors: int
    bad_sectors: int
    cancelled: bool


def format_size(n: Optional[int]) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


# ---------------------------------------------------------------------------
# Escaneo
# ---------------------------------------------------------------------------

def scan(reader: SectorReader,
         types: Optional[Iterable[str]] = None,
         on_found: Optional[Callable[[FoundFile], None]] = None,
         on_progress: Optional[Callable[[int, int], None]] = None,
         cancel: Optional[threading.Event] = None,
         skip_found: bool = True) -> ScanResult:
    """
    Recorre todos los sectores buscando cabeceras conocidas.

    :param types: claves de ``FILE_TYPES`` a buscar (None = todas).
    :param skip_found: si se conoce el tamaño de un archivo, salta sus sectores
        (evita detectar, p. ej., JPEG incrustados dentro de un ZIP o un PDF).
    """
    enabled = set(types) if types is not None else set(FILE_TYPES)
    total = reader.total_sectors
    found: List[FoundFile] = []
    lba = 0
    bs = reader.block_sectors

    while lba < total:
        if cancel is not None and cancel.is_set():
            break
        block = reader.read_block(lba // bs)
        inner = (lba % bs) * SECTOR_SIZE
        sector = block[inner:inner + SECTOR_SIZE]

        kind = identify(sector, enabled)
        next_lba = lba + 1
        if kind is not None:
            base = lba * SECTOR_SIZE
            max_size = min(kind.max_size, reader.size_bytes - base)
            result = kind.find_end(lambda off, n: reader.read(base + off, n), max_size)
            size = result.size if result else None
            ext = (result.extension if result and result.extension else kind.extension)
            n_sectors = -(-(size or kind.fallback_size) // SECTOR_SIZE)
            ff = FoundFile(index=len(found) + 1, kind=kind, sector=lba, size=size,
                           extension=ext, damaged=reader.has_bad_sectors(lba, n_sectors))
            found.append(ff)
            if on_found:
                on_found(ff)
            if skip_found and size:
                next_lba = lba + max(1, -(-size // SECTOR_SIZE))

        # Informamos del progreso una vez por bloque, no por sector.
        if on_progress and (next_lba // bs != lba // bs or next_lba >= total):
            on_progress(min(next_lba, total), total)
        lba = next_lba

    return ScanResult(files=found, sectors_scanned=min(lba, total), total_sectors=total,
                      bad_sectors=len(reader.bad_sectors),
                      cancelled=bool(cancel is not None and cancel.is_set()))


# ---------------------------------------------------------------------------
# Extracción e informe
# ---------------------------------------------------------------------------

def read_file_bytes(reader: SectorReader, ff: FoundFile, limit: Optional[int] = None) -> bytes:
    size = ff.extract_size if limit is None else min(limit, ff.extract_size)
    return reader.read(ff.sector * SECTOR_SIZE, size)


def extract(reader: SectorReader, ff: FoundFile, dest_dir: str) -> "ExtractedFile":
    """Vuelca el archivo a disco y calcula su SHA-256 mientras escribe."""
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, ff.filename)
    sha = hashlib.sha256()
    offset = ff.sector * SECTOR_SIZE
    remaining = min(ff.extract_size, reader.size_bytes - offset)
    written = 0
    with open(path, "wb") as out:
        while remaining > 0:
            chunk = reader.read(offset + written, min(1024 * 1024, remaining))
            if not chunk:
                break
            out.write(chunk)
            sha.update(chunk)
            written += len(chunk)
            remaining -= len(chunk)
    # El daño puede descubrirse al extraer (si el escaneo saltó esos sectores).
    ff.damaged = ff.damaged or reader.has_bad_sectors(ff.sector, -(-written // SECTOR_SIZE))
    return ExtractedFile(ff, path, written, sha.hexdigest())


@dataclass
class ExtractedFile:
    found: FoundFile
    path: str
    size: int
    sha256: str


REPORT_NAME = "informe_recuperacion.csv"


def write_report(extracted: List[ExtractedFile], dest_dir: str, source_desc: str) -> str:
    """
    Informe CSV con hash SHA-256 de cada archivo: permite verificar después que
    la copia no se ha alterado (cadena de custodia básica, como en forense).
    """
    path = os.path.join(dest_dir, REPORT_NAME)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["archivo", "tipo", "sector_inicio", "offset_bytes", "tamano_bytes",
                    "estado", "sha256", "origen"])
        for e in extracted:
            ff = e.found
            w.writerow([os.path.basename(e.path), ff.kind.name, ff.sector,
                        ff.sector * SECTOR_SIZE, e.size, ff.status, e.sha256, source_desc])
    return path
