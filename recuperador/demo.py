"""
Generador de imágenes de disco de demostración.

Crea una imagen con ruido aleatorio y archivos reales (JPEG, PNG, PDF, DOCX...)
colocados al inicio de sectores concretos, como los dejaría un sistema de
archivos ISO 9660/UDF. Sirve para probar la aplicación sin lector de DVD y es
la misma base que usan los tests.

Uso:
    python -m recuperador.demo demo.iso
"""

from __future__ import annotations

import io
import os
import random
import struct
import sys
import wave
import zipfile

from PIL import Image

from .sources import SECTOR_SIZE



def _gradient(w=320, h=240, mode="RGB"):
    img = Image.new(mode, (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (x % 256, y % 256, (x * y) % 256) if mode == "RGB" else (x + y) % 256
    return img


def make_jpeg(progressive=False, exif=False):
    buf = io.BytesIO()
    kwargs = {"quality": 85, "progressive": progressive}
    if exif:
        ex = Image.Exif()
        ex[0x010F] = "CamaraDePrueba"  # Make
        kwargs["exif"] = ex.tobytes()
    _gradient().save(buf, "JPEG", **kwargs)
    return buf.getvalue()


def make_png():
    buf = io.BytesIO()
    _gradient(200, 150).save(buf, "PNG")
    return buf.getvalue()


def make_gif():
    frames = [_gradient(64, 64, "L").convert("P"), _gradient(64, 64, "L").rotate(90).convert("P")]
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    return buf.getvalue()


def make_bmp():
    buf = io.BytesIO()
    _gradient(50, 40).save(buf, "BMP")
    return buf.getvalue()


def make_pdf(incremental=False):
    body = (b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
            b"xref\n0 3\n0000000000 65535 f \ntrailer\n<< /Root 1 0 R >>\n"
            b"startxref\n9\n%%EOF\n")
    if incremental:
        body += (b"3 0 obj\n<< /Producer (edicion) >>\nendobj\n"
                 b"xref\n3 1\ntrailer\n<< /Root 1 0 R /Prev 9 >>\nstartxref\n120\n%%EOF\n")
    return body


def make_docx():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document>" + "hola " * 2000 + "</w:document>")
    return buf.getvalue()


def make_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("notas.txt", os.urandom(30000))  # datos incompresibles
        z.writestr("leeme.txt", "contenido")
    return buf.getvalue()


def make_wav():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(os.urandom(16000))
    return buf.getvalue()


def make_mp4():
    def box(t, payload):
        return struct.pack(">I", 8 + len(payload)) + t + payload
    return (box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2mp41")
            + box(b"moov", os.urandom(500))
            + box(b"mdat", os.urandom(9000)))


class DiscImage:
    """Construye la imagen: ``place(sector, datos)`` y luego ``write(ruta)``."""

    def __init__(self, n_sectors, seed=1234):
        rnd = random.Random(seed)
        self.data = bytearray(rnd.getrandbits(8) for _ in range(n_sectors * SECTOR_SIZE))
        self.placed = {}

    def place(self, sector, payload):
        off = sector * SECTOR_SIZE
        self.data[off:off + len(payload)] = payload
        # Relleno con ceros hasta el final del sector, como hace ISO 9660.
        end = off + len(payload)
        pad = (-end) % SECTOR_SIZE
        self.data[end:end + pad] = b"\x00" * pad
        self.placed[sector] = payload

    def write(self, path):
        with open(path, "wb") as fh:
            fh.write(self.data)
        return str(path)


def demo_files():
    """Sector de inicio -> (extensión, contenido) de cada archivo de la demo."""
    return {
        10: ("jpg", make_jpeg()),
        60: ("jpg", make_jpeg(progressive=True, exif=True)),
        110: ("png", make_png()),
        170: ("gif", make_gif()),
        200: ("bmp", make_bmp()),
        250: ("pdf", make_pdf()),
        260: ("pdf", make_pdf(incremental=True)),
        300: ("docx", make_docx()),
        350: ("zip", make_zip()),
        400: ("wav", make_wav()),
        450: ("mp4", make_mp4()),
    }


def build_demo_image(n_sectors: int = 600, seed: int = 1234):
    img = DiscImage(n_sectors, seed=seed)
    files = demo_files()
    for sector, (_, payload) in files.items():
        img.place(sector, payload)
    return img, files


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    path = argv[0] if argv else "demo.iso"
    img, files = build_demo_image()
    img.write(path)
    print(f"Creada {path} ({len(img.data) // 1024} KB) con {len(files)} archivos.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
