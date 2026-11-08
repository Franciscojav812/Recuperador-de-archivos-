"""
Interfaz de línea de comandos.

Ejemplos::

    python main.py --unidad D: --destino C:\\Recuperados
    python main.py --imagen copia.iso --tipos jpg,png --destino ./salida
    python main.py --imagen copia.iso --solo-listar
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from typing import List, Optional

from .carver import SectorReader, extract, format_size, scan, write_report
from .signatures import FILE_TYPES
from .sources import open_source


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="recuperador",
        description="Recupera archivos de CD/DVD dañados leyendo los sectores en bruto.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--unidad", metavar="D:", help="unidad óptica (solo Windows, requiere admin)")
    src.add_argument("--imagen", metavar="RUTA", help="imagen de disco .iso/.img/.bin")
    p.add_argument("--destino", default="Recuperados", help="carpeta de salida (por defecto: %(default)s)")
    p.add_argument("--tipos", default=",".join(FILE_TYPES),
                   help="formatos separados por comas (por defecto: todos). "
                        f"Disponibles: {', '.join(FILE_TYPES)}")
    p.add_argument("--solo-listar", action="store_true", help="solo listar, sin extraer")
    p.add_argument("--quiet", action="store_true", help="sin barra de progreso")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    types = [t.strip() for t in args.tipos.split(",") if t.strip()]
    unknown = [t for t in types if t not in FILE_TYPES]
    if unknown:
        print(f"Formatos desconocidos: {', '.join(unknown)}", file=sys.stderr)
        return 2

    try:
        source = open_source(drive=args.unidad, image=args.imagen)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    def progress(done: int, total: int) -> None:
        if not args.quiet:
            pct = 100 * done / max(total, 1)
            print(f"\rEscaneando... {pct:5.1f}%  ({done}/{total} sectores)", end="", flush=True)

    with source:
        reader = SectorReader(source)
        cancel = threading.Event()
        try:
            result = scan(reader, types=types, on_progress=progress, cancel=cancel)
        except KeyboardInterrupt:
            print("\nCancelado por el usuario.")
            return 130
        if not args.quiet:
            print()

        print(f"{len(result.files)} archivos encontrados en {source.description} "
              f"({result.bad_sectors} sectores ilegibles).")
        for f in result.files:
            print(f"  {f.filename:<40} {f.kind.name:<26} {format_size(f.size):>10}  {f.status}")

        if args.solo_listar or not result.files:
            return 0

        extracted = []
        for i, f in enumerate(result.files, 1):
            if not args.quiet:
                print(f"\rExtrayendo {i}/{len(result.files)}...", end="", flush=True)
            extracted.append(extract(reader, f, args.destino))
        report = write_report(extracted, args.destino, source.description)
        if not args.quiet:
            print()
        print(f"Guardados en {os.path.abspath(args.destino)}. Informe: {os.path.basename(report)}")
    return 0
