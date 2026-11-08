import csv
import hashlib
import io
import os
import threading

import pytest
from PIL import Image

from recuperador import cli
from recuperador.carver import (REPORT_NAME, SectorReader, extract, read_file_bytes,
                                scan, write_report)
from recuperador.signatures import identify
from recuperador.sources import SECTOR_SIZE, ImageFileSource

from recuperador.demo import DiscImage, make_jpeg, make_png

from conftest import MemorySource


def _scan(data, **kw):
    reader = SectorReader(MemorySource(data, bad=kw.pop("bad", ())))
    return reader, scan(reader, **kw)


def test_fixture_files_do_not_overlap(disc):
    img, files = disc
    spans = sorted((s, s + -(-len(p) // SECTOR_SIZE)) for s, (_, p) in files.items())
    for (a0, a1), (b0, _) in zip(spans, spans[1:]):
        assert a1 <= b0, "los archivos de prueba se solapan"


def test_finds_every_file_with_exact_size_and_type(disc):
    img, files = disc
    _, result = _scan(img.data)
    by_sector = {f.sector: f for f in result.files}
    assert set(by_sector) == set(files)
    for sector, (ext, payload) in files.items():
        f = by_sector[sector]
        assert f.extension == ext, f"sector {sector}"
        assert f.size == len(payload), f"tamaño incorrecto para {ext} en sector {sector}"
        assert f.status == "OK"


def test_extracted_bytes_match_original(disc, tmp_path):
    img, files = disc
    reader, result = _scan(img.data)
    for f in result.files:
        e = extract(reader, f, str(tmp_path))
        original = files[f.sector][1]
        assert open(e.path, "rb").read() == original
        assert e.sha256 == hashlib.sha256(original).hexdigest()


def test_recovered_images_open_in_pillow(disc):
    img, _ = disc
    reader, result = _scan(img.data, types=["jpg", "png", "gif", "bmp"])
    assert len(result.files) == 5
    for f in result.files:
        Image.open(io.BytesIO(read_file_bytes(reader, f))).load()


def test_type_filter(disc):
    img, _ = disc
    _, result = _scan(img.data, types=["png"])
    assert [f.extension for f in result.files] == ["png"]


def test_scans_the_whole_medium_not_just_first_600mb():
    # El original paraba en 300.000 sectores. Usamos un medio "virtual" mayor.
    class BigSparse(MemorySource):
        def __init__(self):
            self.total_sectors = 300_100
            self.png_lba = 300_050
            self.png = make_png()
            self.bad = set()

        def read_sectors(self, lba, count):
            out = bytearray(count * SECTOR_SIZE)
            start = (self.png_lba - lba) * SECTOR_SIZE
            if -len(self.png) < start < len(out):
                s = max(start, 0)
                out[s:start + len(self.png)] = self.png[s - start:len(out) - start]
            return bytes(out)

    src = BigSparse()
    result = scan(SectorReader(src, block_sectors=512))
    assert [(f.sector, f.size) for f in result.files] == [(src.png_lba, len(src.png))]


def test_bad_sector_does_not_desync_scan():
    """Regresión del fallo original: un sector ilegible desplazaba todo lo demás."""
    img = DiscImage(200)
    jpg, png = make_jpeg(), make_png()
    img.place(20, jpg)
    img.place(120, png)
    bad = {5, 6, 90}
    reader, result = _scan(img.data, bad=bad)
    assert [(f.sector, f.extension) for f in result.files] == [(20, "jpg"), (120, "png")]
    assert result.bad_sectors == len(bad)
    assert all(not f.damaged for f in result.files)


def test_file_overlapping_bad_sector_is_flagged(tmp_path):
    img = DiscImage(100)
    img.place(10, make_png())
    reader, result = _scan(img.data, bad={12})
    (f,) = result.files
    # El sector 12 se lee como ceros: la estructura PNG se rompe o el archivo
    # queda marcado como dañado; en ningún caso se presenta como "OK".
    assert f.status != "OK"


def test_truncated_jpeg_uses_fallback_and_is_marked():
    img = DiscImage(100)
    img.place(3, make_jpeg()[:5000])  # sin marcador EOI
    _, result = _scan(img.data)
    (f,) = result.files
    assert f.size is None and f.status == "Tamaño estimado"


def test_cancel_stops_scan(disc):
    img, _ = disc
    cancel = threading.Event()
    seen = []

    def on_found(f):
        seen.append(f)
        cancel.set()

    _, result = _scan(img.data, on_found=on_found, cancel=cancel)
    assert result.cancelled and len(result.files) == 1


def test_progress_reaches_total(disc):
    img, _ = disc
    calls = []
    _, result = _scan(img.data, on_progress=lambda d, t: calls.append((d, t)))
    assert calls[-1] == (result.total_sectors, result.total_sectors)
    assert [d for d, _ in calls] == sorted(d for d, _ in calls)


def test_noise_produces_no_false_positives():
    img = DiscImage(2000, seed=99)
    _, result = _scan(img.data)
    assert result.files == []


def test_identify_rejects_weak_bmp_signature():
    # "BM" son solo 2 bytes: sin validar la cabecera habría falsos positivos constantes.
    assert identify(b"BM" + b"\x00" * 2046) is None
    assert identify(b"BMtexto cualquiera".ljust(2048, b" ")) is None


def test_report_contains_hashes(disc, tmp_path):
    img, files = disc
    reader, result = _scan(img.data, types=["png", "pdf"])
    extracted = [extract(reader, f, str(tmp_path)) for f in result.files]
    path = write_report(extracted, str(tmp_path), "test")
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig"), delimiter=";"))
    assert len(rows) == 3
    for row in rows:
        payload = files[int(row["sector_inicio"])][1]
        assert row["sha256"] == hashlib.sha256(payload).hexdigest()


def test_image_file_source_and_cli(disc, tmp_path):
    img, files = disc
    iso = img.write(tmp_path / "disco.iso")
    with ImageFileSource(iso) as src:
        assert src.total_sectors == 600
    out = tmp_path / "salida"
    code = cli.main(["--imagen", iso, "--destino", str(out), "--tipos", "jpg,png", "--quiet"])
    assert code == 0
    names = sorted(os.listdir(out))
    assert REPORT_NAME in names
    assert len([n for n in names if n.endswith((".jpg", ".png"))]) == 3


def test_cli_list_only_does_not_write(disc, tmp_path, capsys):
    img, _ = disc
    iso = img.write(tmp_path / "disco.iso")
    assert cli.main(["--imagen", iso, "--solo-listar"]) == 0
    out = capsys.readouterr().out
    assert "11 archivos" in out
    assert not (tmp_path / "Recuperados").exists()
