"""
Interfaz gráfica (tkinter).

tkinter no es thread-safe, así que los hilos de trabajo (escaneo, vista previa,
extracción) no tocan widgets: envían eventos a una cola y el hilo principal los
procesa cada 50 ms con root.after().
"""

from __future__ import annotations

import io
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Dict, Optional

from . import __version__
from .carver import (FoundFile, SectorReader, extract, format_size, read_file_bytes,
                     scan, write_report)
from .signatures import FILE_TYPES
from .sources import SECTOR_SIZE, SectorSource, list_optical_drives, open_source

try:  # Pillow es opcional; sin él no hay miniaturas
    from PIL import Image, ImageTk
except ImportError:  # pragma: no cover
    Image = ImageTk = None

APP_NAME = "Recuperador CD/DVD"
PREVIEW_LIMIT = 32 * 1024 * 1024

TYPE_LABELS = {"jpg": "JPEG", "png": "PNG", "gif": "GIF", "bmp": "BMP", "pdf": "PDF",
               "zip": "ZIP/Office", "riff": "WAV/AVI", "mp4": "MP4/MOV"}

COLOR_DAMAGED = "#b3261e"
COLOR_ESTIMATED = "#7a5f00"


def hexdump(data: bytes, width: int = 16) -> str:
    lines = []
    for i in range(0, len(data), width):
        chunk = data[i:i + width]
        hexs = " ".join(f"{b:02x}" for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{i:08x}  {hexs:<{width * 3}} {text}")
    return "\n".join(lines)


def short_status(ff: FoundFile) -> str:
    if ff.damaged:
        return "Sectores ilegibles"
    if not ff.size_known:
        return "Tamaño estimado"
    return "Correcto"


class RecoveryApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("980x620")
        self.root.minsize(820, 480)

        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.source: Optional[SectorSource] = None
        self.reader: Optional[SectorReader] = None
        self.found: Dict[str, FoundFile] = {}
        self._scan_started = 0.0
        self._preview_img = None
        self._sort_reverse: Dict[str, bool] = {}

        self._setup_style()
        self._build_menu()
        self._build_ui()
        self._bind_shortcuts()
        self.refresh_drives()
        self.scan_btn.focus_set()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(50, self._poll_events)

    # ------------------------------------------------------------------ UI
    def _setup_style(self):
        style = ttk.Style()
        # En Windows, el tema nativo; en el resto, "clam", que es neutro.
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Treeview", rowheight=22)
        style.configure("Status.TLabel", padding=(8, 3))
        style.configure("Key.TLabel", foreground="#666666")

    def _build_menu(self):
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Abrir imagen de disco...", underline=0,
                              accelerator="Ctrl+O", command=self.browse_image)
        file_menu.add_command(label="Carpeta de destino...", underline=0,
                              command=self.browse_dest)
        file_menu.add_separator()
        file_menu.add_command(label="Salir", underline=0, command=self.on_close)
        menubar.add_cascade(label="Archivo", underline=0, menu=file_menu)

        scan_menu = tk.Menu(menubar, tearoff=0)
        scan_menu.add_command(label="Iniciar escaneo", underline=0, accelerator="F5",
                              command=self.start_scan)
        scan_menu.add_command(label="Detener", underline=0, accelerator="Esc",
                              command=self.stop_scan)
        scan_menu.add_separator()
        scan_menu.add_command(label="Extraer selección", underline=8, accelerator="Ctrl+E",
                              command=self.extract_selected)
        scan_menu.add_command(label="Extraer todo", underline=8,
                              accelerator="Ctrl+Mayús+E", command=self.extract_all)
        menubar.add_cascade(label="Escaneo", underline=0, menu=scan_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="Atajos de teclado", underline=0, command=self.show_shortcuts)
        help_menu.add_command(label="Acerca de", underline=0, command=self.show_about)
        menubar.add_cascade(label="Ayuda", underline=1, menu=help_menu)
        self.root.config(menu=menubar)

    def _bind_shortcuts(self):
        r = self.root
        r.bind("<F5>", lambda e: self.start_scan())
        r.bind("<Escape>", lambda e: self.stop_scan())
        r.bind("<Control-o>", lambda e: self.browse_image())
        r.bind("<Control-e>", lambda e: self.extract_selected())
        r.bind("<Control-E>", lambda e: self.extract_all())  # Ctrl+Mayús+E
        self.tree.bind("<Control-a>", self._select_all)
        self.tree.bind("<Return>", lambda e: self.extract_selected())
        self.tree.bind("<Button-3>", self._show_context_menu)
        self.tree.bind("<Shift-F10>", self._show_context_menu)
        self.tree.bind("<App>" if sys.platform == "win32" else "<Menu>",
                       self._show_context_menu)

        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="Extraer", command=self.extract_selected)
        self.context_menu.add_command(label="Copiar sector y offset",
                                      command=self._copy_location)

    def _select_all(self, _event=None):
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def _show_context_menu(self, event):
        if event.type == tk.EventType.ButtonPress:
            row = self.tree.identify_row(event.y)
            if row and row not in self.tree.selection():
                self.tree.selection_set(row)
                self.tree.focus(row)
            x, y = event.x_root, event.y_root
        else:  # abierto con teclado: junto a la fila con foco
            bbox = self.tree.bbox(self.tree.focus()) or (0, 0, 0, 0)
            x = self.tree.winfo_rootx() + 20
            y = self.tree.winfo_rooty() + bbox[1] + bbox[3]
        if self.tree.selection() and not self.busy:
            self.context_menu.tk_popup(x, y)
        return "break"

    def _copy_location(self):
        sel = self.tree.selection()
        if not sel:
            return
        lines = [f"{self.found[i].filename}\tsector {self.found[i].sector}\t"
                 f"offset 0x{self.found[i].sector * SECTOR_SIZE:X}" for i in sel]
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(lines))
        self.set_status(f"Copiada la ubicación de {len(sel)} archivo(s) al portapapeles.")

    def _build_ui(self):
        # --- Origen y tipos -------------------------------------------------
        top = ttk.Frame(self.root, padding=(10, 10, 10, 6))
        top.pack(fill=tk.X)

        self.mode = tk.StringVar(value="drive")
        ttk.Radiobutton(top, text="Unidad óptica", underline=0, variable=self.mode, value="drive",
                        command=self._update_mode).grid(row=0, column=0, sticky=tk.W)
        self.drive_combo = ttk.Combobox(top, width=6, state="readonly")
        self.drive_combo.grid(row=0, column=1, sticky=tk.W, padx=(6, 4))
        self.refresh_btn = ttk.Button(top, text="Actualizar", command=self.refresh_drives)
        self.refresh_btn.grid(row=0, column=2, sticky=tk.W)

        ttk.Radiobutton(top, text="Imagen de disco", underline=0, variable=self.mode, value="image",
                        command=self._update_mode).grid(row=1, column=0, sticky=tk.W,
                                                        pady=(6, 0))
        self.image_entry = ttk.Entry(top)
        self.image_entry.grid(row=1, column=1, columnspan=3, sticky=tk.EW, padx=(6, 4),
                              pady=(6, 0))
        self.image_btn = ttk.Button(top, text="Examinar...", command=self.browse_image)
        self.image_btn.grid(row=1, column=4, sticky=tk.W, pady=(6, 0))

        buttons = ttk.Frame(top)
        buttons.grid(row=0, column=5, rowspan=2, sticky=tk.NE, padx=(16, 0))
        self.scan_btn = ttk.Button(buttons, text="Escanear", default="active",
                                   command=self.start_scan, width=12)
        self.scan_btn.pack(fill=tk.X)
        self.cancel_btn = ttk.Button(buttons, text="Detener", command=self.stop_scan,
                                     state=tk.DISABLED, width=12)
        self.cancel_btn.pack(fill=tk.X, pady=(4, 0))
        top.columnconfigure(3, weight=1)

        types = ttk.Frame(top)
        types.grid(row=2, column=0, columnspan=6, sticky=tk.W, pady=(10, 0))
        ttk.Label(types, text="Tipos:").pack(side=tk.LEFT, padx=(0, 8))
        self.type_vars = {}
        for key in FILE_TYPES:
            var = tk.BooleanVar(value=True)
            self.type_vars[key] = var
            ttk.Checkbutton(types, text=TYPE_LABELS.get(key, key.upper()),
                            variable=var).pack(side=tk.LEFT, padx=(0, 10))

        ttk.Separator(self.root).pack(fill=tk.X)

        # --- Resultados y vista previa ---------------------------------------
        body = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)

        left = ttk.Frame(body)
        cols = ("archivo", "tipo", "sector", "tamano", "estado")
        heads = ("Archivo", "Tipo", "Sector", "Tamaño", "Estado")
        widths = (250, 70, 80, 80, 130)
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="extended")
        for c, h, w in zip(cols, heads, widths):
            anchor = tk.E if c in ("sector", "tamano") else tk.W
            self.tree.heading(c, text=h, anchor=anchor, command=lambda c=c: self.sort_by(c))
            self.tree.column(c, width=w, minwidth=50, anchor=anchor,
                             stretch=(c == "archivo"))
        self.tree.tag_configure("damaged", foreground=COLOR_DAMAGED)
        self.tree.tag_configure("estimated", foreground=COLOR_ESTIMATED)
        sb = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        body.add(left, weight=3)

        right = ttk.LabelFrame(body, text="Vista previa", padding=8)
        self.preview_area = ttk.Frame(right, width=300, height=260)
        self.preview_area.pack(fill=tk.BOTH, expand=True)
        self.preview_area.pack_propagate(False)
        self.preview_label = ttk.Label(self.preview_area, anchor=tk.CENTER,
                                       text="Selecciona un archivo")
        self.preview_text = tk.Text(self.preview_area, wrap=tk.NONE, height=12,
                                    relief=tk.FLAT, borderwidth=0,
                                    font=("Consolas", 9) if sys.platform == "win32"
                                    else ("Courier", 9))
        self.preview_label.pack(fill=tk.BOTH, expand=True)

        details = ttk.Frame(right)
        details.pack(fill=tk.X, pady=(8, 0))
        self.detail_vars = {}
        for row, key in enumerate(("Tipo", "Sector", "Offset", "Tamaño", "Estado")):
            ttk.Label(details, text=key, style="Key.TLabel").grid(row=row, column=0,
                                                                  sticky=tk.W, padx=(0, 12))
            var = tk.StringVar(value="")
            self.detail_vars[key] = var
            ttk.Label(details, textvariable=var).grid(row=row, column=1, sticky=tk.W)
        body.add(right, weight=2)

        # --- Destino y extracción -------------------------------------------
        bottom = ttk.Frame(self.root, padding=(10, 0, 10, 8))
        bottom.pack(fill=tk.X)
        ttk.Label(bottom, text="Guardar en:").pack(side=tk.LEFT)
        self.dest_entry = ttk.Entry(bottom)
        self.dest_entry.insert(0, os.path.join(os.path.expanduser("~"), "Desktop",
                                               "Archivos recuperados"))
        self.dest_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Button(bottom, text="Examinar...", command=self.browse_dest).pack(side=tk.LEFT)
        self.extract_all_btn = ttk.Button(bottom, text="Extraer todo",
                                          command=self.extract_all, state=tk.DISABLED)
        self.extract_all_btn.pack(side=tk.RIGHT, padx=(6, 0))
        self.extract_sel_btn = ttk.Button(bottom, text="Extraer selección",
                                          command=self.extract_selected, state=tk.DISABLED)
        self.extract_sel_btn.pack(side=tk.RIGHT, padx=(16, 0))

        # --- Barra de estado --------------------------------------------------
        ttk.Separator(self.root).pack(fill=tk.X)
        status = ttk.Frame(self.root)
        status.pack(fill=tk.X)
        self.status_label = ttk.Label(status, text="Listo", style="Status.TLabel")
        self.status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.progress = ttk.Progressbar(status, mode="determinate", length=220)
        self.progress.pack(side=tk.RIGHT, padx=8, pady=3)

        self._update_mode()

    def _update_mode(self):
        drive = self.mode.get() == "drive"
        self.drive_combo.configure(state="readonly" if drive else tk.DISABLED)
        self.refresh_btn.configure(state=tk.NORMAL if drive else tk.DISABLED)
        self.image_entry.configure(state=tk.DISABLED if drive else tk.NORMAL)
        self.image_btn.configure(state=tk.DISABLED if drive else tk.NORMAL)

    def _set_busy(self, busy: bool, scanning: bool = False):
        self.busy = busy
        self.scan_btn.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self.cancel_btn.configure(state=tk.NORMAL if scanning else tk.DISABLED)
        state = tk.NORMAL if (not busy and self.found) else tk.DISABLED
        self.extract_all_btn.configure(state=state)
        self.extract_sel_btn.configure(state=state)

    def set_status(self, text: str):
        self.status_label.configure(text=text)

    # --------------------------------------------------------------- acciones
    def refresh_drives(self):
        drives = list_optical_drives()
        self.drive_combo["values"] = drives or ["D:"]
        if self.drive_combo.get() not in (drives or ["D:"]):
            self.drive_combo.set(drives[0] if drives else "D:")

    def browse_image(self):
        path = filedialog.askopenfilename(
            title="Abrir imagen de disco",
            filetypes=[("Imágenes de disco", "*.iso *.img *.bin *.dd"), ("Todos", "*.*")])
        if path:
            self.mode.set("image")
            self._update_mode()
            self.image_entry.delete(0, tk.END)
            self.image_entry.insert(0, path)

    def browse_dest(self):
        folder = filedialog.askdirectory(title="Carpeta de destino")
        if folder:
            self.dest_entry.delete(0, tk.END)
            self.dest_entry.insert(0, folder)

    def stop_scan(self):
        if self.busy and not self.cancel.is_set():
            self.cancel.set()
            self.set_status("Deteniendo...")

    def show_shortcuts(self):
        messagebox.showinfo(
            "Atajos de teclado",
            "F5\tIniciar escaneo\n"
            "Esc\tDetener\n"
            "Ctrl+O\tAbrir imagen de disco\n"
            "Ctrl+E\tExtraer selección\n"
            "Ctrl+Mayús+E\tExtraer todo\n"
            "Ctrl+A\tSeleccionar todos (en la lista)\n"
            "Intro\tExtraer selección (en la lista)\n"
            "Mayús+F10\tMenú contextual")

    def show_about(self):
        messagebox.showinfo(
            "Acerca de",
            f"{APP_NAME} {__version__}\n\n"
            "Recuperación de archivos de CD/DVD dañados mediante lectura directa "
            "de sectores.\n\nLicencia MIT")

    def _close_source(self):
        if self.source is not None:
            self.source.close()
        self.source = self.reader = None

    def start_scan(self):
        if self.busy:
            return
        types = [k for k, v in self.type_vars.items() if v.get()]
        if not types:
            messagebox.showwarning(APP_NAME, "Marca al menos un tipo de archivo.")
            return
        try:
            if self.mode.get() == "drive":
                source = open_source(drive=self.drive_combo.get())
            else:
                source = open_source(image=self.image_entry.get().strip())
        except (OSError, ValueError) as exc:
            messagebox.showerror(APP_NAME, f"No se pudo abrir el origen.\n\n{exc}")
            return

        self._close_source()
        self.source = source
        self.reader = SectorReader(source,
                                   on_bad_sector=lambda lba: self.events.put(("bad", lba)))
        self.tree.delete(*self.tree.get_children())
        self.found.clear()
        self._clear_preview()
        self.cancel.clear()
        self.progress.configure(maximum=max(source.total_sectors, 1), value=0)
        self._scan_started = time.monotonic()
        self._set_busy(True, scanning=True)
        self.set_status(f"Escaneando {source.description}...")

        def worker(reader=self.reader):
            try:
                result = scan(reader, types=types, cancel=self.cancel,
                              on_found=lambda f: self.events.put(("found", f)),
                              on_progress=lambda d, t: self.events.put(("progress", d, t)))
                self.events.put(("scan_done", result))
            except Exception as exc:
                self.events.put(("error", f"Error durante el escaneo: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def on_select(self, _event=None):
        sel = self.tree.selection()
        if len(sel) != 1 or self.reader is None:
            return
        ff = self.found[sel[0]]
        self._show_details(ff)

        def worker(reader=self.reader, item=sel[0]):
            try:
                data = read_file_bytes(reader, ff, PREVIEW_LIMIT if ff.kind.is_image else 512)
                img = None
                if ff.kind.is_image and Image is not None:
                    try:
                        img = Image.open(io.BytesIO(data))
                        img.thumbnail((300, 260))
                        img = img.convert("RGB")
                    except Exception:
                        img = None
                self.events.put(("preview", item, img, hexdump(data[:512])))
            except Exception as exc:
                self.events.put(("preview", item, None, f"No se pudo leer: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def extract_selected(self):
        if self.busy or not self.found:
            return
        items = self.tree.selection()
        if not items:
            messagebox.showinfo(APP_NAME, "Selecciona uno o varios archivos.")
            return
        self._extract([self.found[i] for i in items])

    def extract_all(self):
        if self.busy or not self.found:
            return
        self._extract([self.found[i] for i in self.tree.get_children()])

    def _extract(self, files):
        dest = self.dest_entry.get().strip()
        if not dest or self.reader is None:
            return
        self._set_busy(True)
        self.progress.configure(maximum=len(files), value=0)

        def worker(reader=self.reader, desc=self.source.description):
            try:
                done = []
                for i, f in enumerate(files, 1):
                    done.append(extract(reader, f, dest))
                    self.events.put(("extract_progress", i, len(files), f.filename))
                report = write_report(done, dest, desc)
                self.events.put(("extract_done", dest, done, report))
            except OSError as exc:
                self.events.put(("error", f"No se pudo escribir en {dest}: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def sort_by(self, col):
        rev = self._sort_reverse.get(col, False)
        key = {"sector": lambda i: self.found[i].sector,
               "tamano": lambda i: self.found[i].size or -1}.get(
            col, lambda i: str(self.tree.set(i, col)).lower())
        for idx, item in enumerate(sorted(self.tree.get_children(), key=key, reverse=rev)):
            self.tree.move(item, "", idx)
        self._sort_reverse[col] = not rev

    # ----------------------------------------------------- bucle de eventos
    def _poll_events(self):
        try:
            for _ in range(500):
                ev = self.events.get_nowait()
                getattr(self, f"_on_{ev[0]}")(*ev[1:])
        except queue.Empty:
            pass
        self.root.after(50, self._poll_events)

    def _on_found(self, ff: FoundFile):
        iid = f"f{ff.index}"
        self.found[iid] = ff
        tag = "damaged" if ff.damaged else ("estimated" if not ff.size_known else "")
        self.tree.insert("", tk.END, iid=iid, tags=(tag,) if tag else (), values=(
            ff.filename, ff.extension.upper(), ff.sector, format_size(ff.size),
            short_status(ff)))

    def _on_bad(self, lba: int):
        pass  # se refleja en la barra de estado

    def _on_progress(self, done: int, total: int):
        self.progress.configure(value=done)
        elapsed = max(time.monotonic() - self._scan_started, 1e-6)
        speed = done * SECTOR_SIZE / elapsed / (1024 * 1024)
        remaining = (total - done) * SECTOR_SIZE / max(speed * 1024 * 1024, 1)
        bad = len(self.reader.bad_sectors) if self.reader else 0
        pct = 100 * done / max(total, 1)
        text = f"Escaneando... {pct:.0f} %   {len(self.found)} archivos"
        if bad:
            text += f"   {bad} sectores ilegibles"
        text += f"   {speed:.1f} MB/s   quedan {int(remaining // 60)}:{int(remaining % 60):02d}"
        self.set_status(text)

    def _on_scan_done(self, result):
        self._set_busy(False)
        prefix = "Escaneo detenido" if result.cancelled else "Escaneo completado"
        text = f"{prefix}. {len(result.files)} archivos encontrados"
        if result.bad_sectors:
            text += f", {result.bad_sectors} sectores ilegibles"
        self.set_status(text + ".")
        children = self.tree.get_children()
        if children:
            self.tree.focus_set()
            self.tree.focus(children[0])
            self.tree.selection_set(children[0])

    def _show_details(self, ff: FoundFile):
        self.detail_vars["Tipo"].set(f"{ff.kind.name} ({ff.extension.upper()})")
        self.detail_vars["Sector"].set(f"{ff.sector:,}".replace(",", "."))
        self.detail_vars["Offset"].set(f"0x{ff.sector * SECTOR_SIZE:X}")
        self.detail_vars["Tamaño"].set(format_size(ff.size) if ff.size_known
                                       else f"desconocido (se extraerán "
                                            f"{format_size(ff.extract_size)})")
        self.detail_vars["Estado"].set(short_status(ff))

    def _on_preview(self, item, img, text):
        sel = self.tree.selection()
        if len(sel) != 1 or sel[0] != item:
            return  # ya hay otro archivo seleccionado
        ff = self.found[item]
        if img is not None and ImageTk is not None:
            self.preview_text.pack_forget()
            self._preview_img = ImageTk.PhotoImage(img)
            self.preview_label.configure(image=self._preview_img, text="")
            self.preview_label.pack(fill=tk.BOTH, expand=True)
        else:
            self.preview_label.pack_forget()
            self.preview_text.configure(state=tk.NORMAL)
            self.preview_text.delete("1.0", tk.END)
            if ff.kind.is_image:
                self.preview_text.insert(tk.END, "No se puede mostrar la imagen "
                                                 "(datos dañados).\n\n")
            self.preview_text.insert(tk.END, text)
            self.preview_text.configure(state=tk.DISABLED)
            self.preview_text.pack(fill=tk.BOTH, expand=True)

    def _clear_preview(self):
        self._preview_img = None
        self.preview_text.pack_forget()
        self.preview_label.configure(image="", text="Selecciona un archivo")
        self.preview_label.pack(fill=tk.BOTH, expand=True)
        for var in self.detail_vars.values():
            var.set("")

    def _on_extract_progress(self, i, n, name):
        self.progress.configure(value=i)
        self.set_status(f"Extrayendo {i} de {n}: {name}")

    def _on_extract_done(self, dest, done, report):
        self._set_busy(False)
        damaged = sum(1 for e in done if e.found.damaged)
        self.set_status(f"{len(done)} archivos guardados en {dest}")
        msg = (f"Se han guardado {len(done)} archivos en:\n{dest}\n\n"
               f"El informe con los hashes SHA-256 está en {os.path.basename(report)}.")
        if damaged:
            msg += (f"\n\n{damaged} de ellos tienen sectores ilegibles, "
                    "rellenos con ceros.")
        if sys.platform == "win32":
            if messagebox.askyesno(APP_NAME, msg + "\n\n¿Abrir la carpeta?"):
                os.startfile(dest)  # type: ignore[attr-defined]
        else:
            messagebox.showinfo(APP_NAME, msg)

    def _on_error(self, msg):
        self._set_busy(False)
        self.set_status(msg)
        messagebox.showerror(APP_NAME, msg)

    def on_close(self):
        self.cancel.set()
        self._close_source()
        self.root.destroy()


def run() -> None:
    if sys.platform == "win32":
        try:  # texto nítido con escalado de pantalla al 125 % / 150 %
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    RecoveryApp(root)
    root.mainloop()
