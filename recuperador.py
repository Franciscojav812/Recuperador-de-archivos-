import os
import sys
import ctypes
import tempfile
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from PIL import Image, ImageTk  # Requiere: pip install pillow

# Constantes de Windows para acceso directo al hardware
GENERIC_READ = 0x80000000
OPEN_EXISTING = 3
FILE_SHARE_READ = 0x00000001
FILE_ATTRIBUTE_NORMAL = 0x00000080

class DVDRecoveryApp:
    def __init__(self, root):
        self.root = root
        self.root.title("DIY DVD Data Recovery - Pro Scanner")
        self.root.geometry("850x550")
        self.root.configure(bg="#1e1e1e")
        
        self.scanning = False
        self.found_files = {} # Guardaremos la info indexada por el ID del árbol
        self.temp_dir = tempfile.mkdtemp() # Para las vistas previas temporales
        
        self.setup_ui()
        
    def setup_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TLabel", background="#1e1e1e", foreground="#ffffff", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 9, "bold"), background="#333333", foreground="#ffffff")
        style.map("TButton", background=[("active", "#555555")])
        
        # --- Panel Superior (Configuración de Entrada y Destino) ---
        top_frame = tk.LabelFrame(self.root, text=" Configuración ", bg="#1e1e1e", fg="#ffffff", padx=10, pady=10)
        top_frame.pack(fill=tk.X, padx=10, pady=5)
        
        ttk.Label(top_frame, text="Unidad DVD:").grid(row=0, column=0, sticky=tk.W, pady=2)
        self.drive_entry = ttk.Entry(top_frame, width=5, font=("Segoe UI", 10))
        self.drive_entry.insert(0, "D:")
        self.drive_entry.grid(row=0, column=1, sticky=tk.W, padx=5, pady=2)
        
        self.scan_btn = ttk.Button(top_frame, text="Escanear Sectores", command=self.start_scan)
        self.scan_btn.grid(row=0, column=2, padx=10, pady=2)
        
        ttk.Label(top_frame, text="Carpeta de Destino en PC:").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.dest_entry = ttk.Entry(top_frame, width=60, font=("Segoe UI", 10))
        self.dest_entry.insert(0, os.path.join(os.path.expanduser("~"), "Desktop", "Fotos_Recuperadas"))
        self.dest_entry.grid(row=1, column=1, columnspan=2, sticky=tk.W, padx=5, pady=5)
        
        self.browse_btn = ttk.Button(top_frame, text="Examinar...", command=self.browse_destination)
        self.browse_btn.grid(row=1, column=3, padx=5, pady=5)
        
        # --- Cuerpo Principal (Izquierda Lista, Derecha Vista Previa) ---
        main_frame = tk.Frame(self.root, bg="#1e1e1e", padx=10, pady=5)
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # Lado Izquierdo: Tabla
        left_frame = tk.Frame(main_frame, bg="#1e1e1e")
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        columns = ("id", "tipo", "sector", "tamano")
        self.tree = ttk.Treeview(left_frame, columns=columns, show="headings", selectmode="extended")
        self.tree.heading("id", text="Archivo Detectado")
        self.tree.heading("tipo", text="Tipo")
        self.tree.heading("sector", text="Sector Inicio")
        self.tree.heading("tamano", text="Tamaño Est.")
        
        self.tree.column("id", width=130)
        self.tree.column("tipo", width=120)
        self.tree.column("sector", width=90)
        self.tree.column("tamano", width=90)
        self.tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        
        scrollbar = ttk.Scrollbar(left_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(fill=tk.Y, side=tk.RIGHT)
        
        self.tree.bind("<Double-1>", self.on_double_click) # Doble clic para previsualizar
        
        # Lado Derecho: Vista Previa
        self.right_frame = tk.LabelFrame(main_frame, text=" Vista Previa (Doble clic en la lista) ", bg="#1e1e1e", fg="#ffffff", width=250)
        self.right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, padx=(10, 0))
        self.right_frame.pack_propagate(False)
        
        self.preview_label = tk.Label(self.right_frame, text="Sin vista previa", bg="#2a2a2a", fg="#aaaaaa")
        self.preview_label.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # --- Panel Inferior (Estado y Acciones) ---
        bottom_frame = tk.Frame(self.root, bg="#2d2d2d", padx=10, pady=10)
        bottom_frame.pack(fill=tk.X)
        
        self.status_label = ttk.Label(bottom_frame, text="Estado: Listo", background="#2d2d2d")
        self.status_label.pack(side=tk.LEFT)
        
        self.extract_all_btn = ttk.Button(bottom_frame, text="Extraer TODO", command=self.extract_all, state=tk.DISABLED)
        self.extract_all_btn.pack(side=tk.RIGHT, padx=5)
        
        self.extract_sel_btn = ttk.Button(bottom_frame, text="Extraer Selección", command=self.extract_selected, state=tk.DISABLED)
        self.extract_sel_btn.pack(side=tk.RIGHT, padx=5)

    def log_status(self, text):
        self.status_label.config(text=f"Estado: {text}")
        self.root.update_idletasks()

    def browse_destination(self):
        folder = filedialog.askdirectory(title="Selecciona la carpeta de destino")
        if folder:
            self.dest_entry.delete(0, tk.END)
            self.dest_entry.insert(0, folder)

    def start_scan(self):
        if self.scanning:
            return
        self.scanning = True
        self.scan_btn.config(state=tk.DISABLED)
        self.extract_all_btn.config(state=tk.DISABLED)
        self.extract_sel_btn.config(state=tk.DISABLED)
        self.tree.delete(*self.tree.get_children())
        self.found_files = {}
        
        threading.Thread(target=self.scan_sectors_raw, daemon=True).start()

    def scan_sectors_raw(self):
        drive_letter = self.drive_entry.get().upper().strip().replace("\\", "").replace("/", "")
        if not drive_letter.endswith(":"): drive_letter += ":"
        
        handle = ctypes.windll.kernel32.CreateFileW(
            f"\\\\.\\{drive_letter}", GENERIC_READ, FILE_SHARE_READ, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None
        )
        
        if handle == -1:
            messagebox.showerror("Error", "No se pudo acceder a la unidad física. ¿Ejecutaste como Administrador?")
            self.end_scan()
            return

        sector_size = 2048
        buffer = ctypes.create_string_buffer(sector_size)
        bytes_read = ctypes.c_ulong(0)
        
        sector_index = 0
        file_count = 0
        max_sectors = 300000 
        
        while sector_index < max_sectors:
            if sector_index % 500 == 0:
                self.log_status(f"Escaneando sector {sector_index}...")
            
            ret = ctypes.windll.kernel32.ReadFile(handle, buffer, sector_size, ctypes.byref(bytes_read), None)
            if not ret or bytes_read.value == 0:
                sector_index += 1
                continue
            
            data = buffer.raw
            if data[0:4] in (b'\xff\xd8\xff\xe0', b'\xff\xd8\xff\xe1'):
                file_count += 1
                item_id = f"item_{file_count}"
                filename = f"Recuperado_{file_count}.jpg"
                
                # Guardamos la metadata del sector de inicio
                self.found_files[item_id] = {"sector": sector_index, "filename": filename}
                
                self.tree.insert("", tk.END, iid=item_id, values=(filename, "Imagen JPEG (RAW)", sector_index, "2 - 5 MB"))
            
            sector_index += 1
            
        ctypes.windll.kernel32.CloseHandle(handle)
        self.end_scan()

    def end_scan(self):
        self.scanning = False
        self.scan_btn.config(state=tk.NORMAL)
        if self.found_files:
            self.extract_all_btn.config(state=tk.NORMAL)
            self.extract_sel_btn.config(state=tk.NORMAL)
            self.log_status(f"¡Análisis finalizado! {len(self.found_files)} fotos encontradas.")
        else:
            self.log_status("No se detectaron firmas RAW.")

    def dump_file_from_sector(self, start_sector, target_path):
        """Lee bloques secuenciales desde un sector y los guarda en un archivo"""
        drive_letter = self.drive_entry.get().upper().strip().replace("\\", "").replace("/", "")
        if not drive_letter.endswith(":"): drive_letter += ":"
        
        handle = ctypes.windll.kernel32.CreateFileW(
            f"\\\\.\\{drive_letter}", GENERIC_READ, FILE_SHARE_READ, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None
        )
        if handle == -1: return False
        
        sector_size = 2048
        buffer = ctypes.create_string_buffer(sector_size)
        bytes_read = ctypes.c_ulong(0)
        
        ctypes.windll.kernel32.SetFilePointer(handle, start_sector * sector_size, None, 0)
        
        with open(target_path, "wb") as f_out:
            for _ in range(1500): # Volcar 1500 sectores (~3MB de margen por foto)
                ret = ctypes.windll.kernel32.ReadFile(handle, buffer, sector_size, ctypes.byref(bytes_read), None)
                if not ret or bytes_read.value == 0: break
                f_out.write(buffer.raw)
                
        ctypes.windll.kernel32.CloseHandle(handle)
        return True

    def on_double_click(self, event):
        """Extrae temporalmente el archivo seleccionado y muestra su miniatura"""
        selected_item = self.tree.focus()
        if not selected_item or selected_item not in self.found_files: return
        
        info = self.found_files[selected_item]
        temp_path = os.path.join(self.temp_dir, info["filename"])
        
        self.log_status(f"Generando vista previa de {info['filename']}...")
        
        if self.dump_file_from_sector(info["sector"], temp_path):
            try:
                img = Image.open(temp_path)
                img.thumbnail((230, 350)) # Ajustar al recuadro lateral
                img_tk = ImageTk.PhotoImage(img)
                self.preview_label.config(image=img_tk, text="")
                self.preview_label.image = img_tk
                self.log_status("Vista previa cargada.")
            except:
                self.preview_label.config(image="", text="[Error al renderizar imagen\nSector dañado o incompleto]")

    def extract_selected(self):
        selected_items = self.tree.selection()
        if not selected_items:
            messagebox.showwarning("Selección vacía", "Por favor, selecciona primero uno o varios archivos de la lista.")
            return
        self._process_extraction(selected_items)

    def extract_all(self):
        self._process_extraction(self.found_files.keys())

    def _process_extraction(self, items_to_extract):
        output_dir = self.dest_entry.get().strip()
        if not output_dir: return
        
        os.makedirs(output_dir, exist_ok=True)
        count = 0
        
        for item_id in items_to_extract:
            info = self.found_files[item_id]
            final_path = os.path.join(output_dir, info["filename"])
            self.log_status(f"Guardando {info['filename']}...")
            if self.dump_file_from_sector(info["sector"], final_path):
                count += 1
                
        self.log_status(f"¡Proceso finalizado! {count} archivos guardados.")
        messagebox.showinfo("Éxito", f"Se han extraído los archivos seleccionados en:\n{output_dir}")

if __name__ == "__main__":
    if not ctypes.windll.shell32.IsUserAnAdmin():
        messagebox.showwarning("Permisos requeridos", "Por favor, ejecuta la terminal como Administrador.")
        sys.exit(0)
        
    root = tk.Tk()
    app = DVDRecoveryApp(root)
    root.mainloop()