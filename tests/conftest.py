"""Utilidades comunes de los tests."""

import pytest

from recuperador.demo import build_demo_image
from recuperador.sources import SECTOR_SIZE, ReadError, SectorSource


class MemorySource(SectorSource):
    def __init__(self, data, bad=()):
        self.data = bytes(data)
        self.total_sectors = len(self.data) // SECTOR_SIZE
        self.bad = set(bad)
        self.description = "memoria"
        self.calls = 0

    def read_sectors(self, lba, count):
        self.calls += 1
        if any(lba <= b < lba + count for b in self.bad):
            raise ReadError(f"sector dañado en {lba}..{lba + count - 1}")
        return self.data[lba * SECTOR_SIZE:(lba + count) * SECTOR_SIZE]


@pytest.fixture
def disc():
    """Imagen de 600 sectores (~1,2 MB) con un archivo de cada formato."""
    return build_demo_image()
