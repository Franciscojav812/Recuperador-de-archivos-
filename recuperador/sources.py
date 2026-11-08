"""
Fuentes de sectores: de dónde se leen los datos en bruto.

Las dos fuentes (unidad física en Windows e imagen de disco) implementan la
misma interfaz ``SectorSource``, así que el motor de recuperación funciona igual
con cualquiera de ellas. La imagen de disco permite ejecutar los tests en
cualquier sistema y analizar copias hechas con ``ddrescue``.
"""

from __future__ import annotations

import os
import re
import sys
from typing import List, Optional

SECTOR_SIZE = 2048  # Tamaño de sector de usuario en CD/DVD (modo 1 / ISO 9660 / UDF)


class ReadError(IOError):
    """No se pudo leer un rango de sectores (sector dañado, disco retirado...)."""


class SectorSource:
    """Interfaz común para cualquier origen de sectores."""

    sector_size: int = SECTOR_SIZE
    total_sectors: int = 0
    description: str = ""

    def read_sectors(self, lba: int, count: int) -> bytes:
        """Devuelve ``count`` sectores empezando en ``lba``. Lanza ``ReadError`` si falla."""
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ---------------------------------------------------------------------------
# Imagen de disco (.iso / .img / volcado de ddrescue), multiplataforma
# ---------------------------------------------------------------------------

class ImageFileSource(SectorSource):
    """Lee sectores de un fichero de imagen. Funciona en cualquier sistema operativo."""

    def __init__(self, path: str):
        self.path = path
        self._fh = open(path, "rb")
        size = os.fstat(self._fh.fileno()).st_size
        # Si la imagen no termina en un sector completo, el último parcial también cuenta.
        self.total_sectors = (size + SECTOR_SIZE - 1) // SECTOR_SIZE
        self._size = size
        self.description = f"Imagen {os.path.basename(path)}"

    def read_sectors(self, lba: int, count: int) -> bytes:
        try:
            self._fh.seek(lba * SECTOR_SIZE)
            data = self._fh.read(count * SECTOR_SIZE)
        except OSError as exc:
            raise ReadError(str(exc)) from exc
        # Rellenamos el último sector parcial para mantener la alineación.
        expected = min(count, self.total_sectors - lba) * SECTOR_SIZE
        if len(data) < expected:
            data += b"\x00" * (expected - len(data))
        return data

    def close(self) -> None:
        self._fh.close()


# ---------------------------------------------------------------------------
# Unidad óptica física (Windows, acceso RAW mediante la API Win32)
# ---------------------------------------------------------------------------

_DRIVE_RE = re.compile(r"^([A-Za-z]):?\\?$")


def normalize_drive(text: str) -> str:
    """'d', 'D:', 'D:\\' -> 'D:'. Lanza ValueError si no es una letra de unidad válida."""
    m = _DRIVE_RE.match(text.strip())
    if not m:
        raise ValueError(f"'{text}' no es una letra de unidad válida (ejemplo: D:)")
    return m.group(1).upper() + ":"


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x00000080
    FILE_BEGIN = 0
    DRIVE_CDROM = 5
    IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
    ERROR_ACCESS_DENIED = 5
    ERROR_NOT_READY = 21

    # Declarar argtypes/restype es imprescindible: sin ello ctypes asume ``int``
    # de 32 bits y en Python de 64 bits los HANDLE se truncan.
    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.HANDLE]
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    _k32.ReadFile.restype = wintypes.BOOL
    # SetFilePointerEx usa un desplazamiento de 64 bits: SetFilePointer (32 bits)
    # falla en cuanto se superan los 2 GB, es decir, en media DVD.
    _k32.SetFilePointerEx.argtypes = [wintypes.HANDLE, wintypes.LARGE_INTEGER,
                                      ctypes.POINTER(wintypes.LARGE_INTEGER), wintypes.DWORD]
    _k32.SetFilePointerEx.restype = wintypes.BOOL
    _k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    _k32.DeviceIoControl.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    _k32.GetDriveTypeW.restype = wintypes.UINT
    _k32.GetLogicalDrives.restype = wintypes.DWORD
    _k32.GetDiskFreeSpaceExW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_ulonglong),
                                         ctypes.POINTER(ctypes.c_ulonglong),
                                         ctypes.POINTER(ctypes.c_ulonglong)]
    _k32.GetDiskFreeSpaceExW.restype = wintypes.BOOL

    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    def list_optical_drives() -> List[str]:
        """Letras de las unidades de CD/DVD presentes en el sistema."""
        mask = _k32.GetLogicalDrives()
        drives = []
        for i in range(26):
            if mask & (1 << i):
                letter = f"{chr(65 + i)}:"
                if _k32.GetDriveTypeW(letter + "\\") == DRIVE_CDROM:
                    drives.append(letter)
        return drives

    class WindowsDriveSource(SectorSource):
        """Acceso RAW a ``\\\\.\\D:`` saltándose el sistema de archivos."""

        def __init__(self, drive: str):
            self.drive = normalize_drive(drive)
            self.handle = _k32.CreateFileW(
                f"\\\\.\\{self.drive}", GENERIC_READ,
                FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING,
                FILE_ATTRIBUTE_NORMAL, None,
            )
            if self.handle in (None, INVALID_HANDLE_VALUE):
                err = ctypes.get_last_error()
                if err == ERROR_ACCESS_DENIED:
                    raise PermissionError(
                        "Acceso denegado a la unidad. Ejecuta el programa como Administrador.")
                if err == ERROR_NOT_READY:
                    raise ReadError(f"La unidad {self.drive} no está lista (¿hay un disco dentro?).")
                raise ReadError(f"No se pudo abrir {self.drive}: {ctypes.FormatError(err)}")
            self.total_sectors = self._query_length() // SECTOR_SIZE
            self.description = f"Unidad {self.drive}"
            self._buf = None
            self._buf_len = 0

        def _query_length(self) -> int:
            # 1) Longitud del medio vía IOCTL (no depende del sistema de archivos).
            length = ctypes.c_longlong(0)
            returned = wintypes.DWORD(0)
            if _k32.DeviceIoControl(self.handle, IOCTL_DISK_GET_LENGTH_INFO, None, 0,
                                    ctypes.byref(length), ctypes.sizeof(length),
                                    ctypes.byref(returned), None) and length.value > 0:
                return length.value
            # 2) Plan B: tamaño que informa el sistema de archivos montado.
            total = ctypes.c_ulonglong(0)
            if _k32.GetDiskFreeSpaceExW(self.drive + "\\", None, ctypes.byref(total), None) \
                    and total.value > 0:
                return total.value
            self.close()
            raise ReadError("No se pudo determinar el tamaño del disco.")

        def read_sectors(self, lba: int, count: int) -> bytes:
            size = count * SECTOR_SIZE
            if self._buf_len < size:
                self._buf = ctypes.create_string_buffer(size)
                self._buf_len = size
            # Posicionamos SIEMPRE antes de leer: si un ReadFile previo falló, el
            # puntero del handle puede no haber avanzado y desincronizar el escaneo.
            if not _k32.SetFilePointerEx(self.handle, lba * SECTOR_SIZE, None, FILE_BEGIN):
                raise ReadError(f"Seek al sector {lba}: {ctypes.FormatError(ctypes.get_last_error())}")
            read = wintypes.DWORD(0)
            ok = _k32.ReadFile(self.handle, self._buf, size, ctypes.byref(read), None)
            if not ok or read.value != size:
                raise ReadError(f"Error leyendo sectores {lba}-{lba + count - 1}")
            return self._buf.raw[:size]

        def close(self) -> None:
            if getattr(self, "handle", None) not in (None, INVALID_HANDLE_VALUE):
                _k32.CloseHandle(self.handle)
                self.handle = None

else:  # pragma: no cover - solo informativo fuera de Windows
    def list_optical_drives() -> List[str]:
        return []

    class WindowsDriveSource(SectorSource):  # type: ignore[no-redef]
        def __init__(self, drive: str):
            raise OSError("El acceso RAW a unidades solo está implementado en Windows. "
                          "Usa una imagen de disco (.iso), p. ej. creada con ddrescue.")


def open_source(drive: Optional[str] = None, image: Optional[str] = None) -> SectorSource:
    """Abre la fuente adecuada según lo indicado por el usuario."""
    if image:
        return ImageFileSource(image)
    if drive:
        return WindowsDriveSource(drive)
    raise ValueError("Indica una unidad o una imagen de disco.")
