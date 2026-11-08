# Recuperador CD/DVD

[![CI](https://github.com/Franciscojav812/Recuperador-de-archivos-/actions/workflows/ci.yml/badge.svg)](https://github.com/Franciscojav812/Recuperador-de-archivos-/actions/workflows/ci.yml)

Aplicación de escritorio para recuperar archivos de CD y DVD dañados leyendo el
disco sector a sector, sin depender del sistema de archivos.

<!-- Añadir captura o GIF en docs/ y descomentar:
![Recuperador CD/DVD](docs/captura.png)
-->

## El problema

Cuando un disco óptico se raya o se degrada, lo habitual es que Windows no pueda
leer la tabla de directorios y dé el disco por perdido, aunque la mayoría de los
datos sigan ahí. Las herramientas que lo resuelven suelen ser de pago o muy
complejas.

Este programa lee el disco en bruto, reconoce los archivos por su firma binaria
(magic bytes) y los reconstruye, aunque el sistema de archivos esté corrupto o
haya sectores que no se puedan leer.

## Funcionalidades

- Lectura directa de unidades ópticas en Windows y de imágenes de disco
  (`.iso`, `.img`, volcados de `ddrescue`) en cualquier sistema.
- JPEG, PNG, GIF, BMP, PDF, ZIP, DOCX, XLSX, PPTX, ODT, WAV, AVI y MP4/MOV.
- Calcula el tamaño real de cada archivo a partir de su estructura interna.
- Los sectores ilegibles se aíslan y se rellenan con ceros, y los archivos
  afectados quedan marcados.
- Vista previa de imágenes y volcado hexadecimal del resto de formatos.
- Genera un informe CSV con sector, tamaño, estado y hash SHA-256 de cada archivo.
- Interfaz gráfica y línea de comandos.

## Tecnologías

| | Motivo |
|---|---|
| Python 3.9+ | Rápido de desarrollar y suficiente: el cuello de botella es la velocidad del lector, no la CPU (analizar un DVD completo cuesta solo unos segundos de CPU). |
| ctypes + API Win32 | Acceso RAW a `\\.\D:` (`CreateFileW`, `ReadFile`, `SetFilePointerEx`, `DeviceIoControl`) sin compilar extensiones en C. |
| tkinter | Viene con Python: no añade dependencias y el ejecutable final es pequeño. |
| Pillow | Miniaturas en la vista previa. Es opcional. |
| pytest | Tests sobre imágenes de disco sintéticas, sin necesidad de lector físico. |
| GitHub Actions + PyInstaller | Tests en Linux y Windows en cada push, y compilación del `.exe` en cada release. |

## Ejecutar en local

```bash
git clone https://github.com/Franciscojav812/Recuperador-de-archivos-.git
cd Recuperador-de-archivos-
pip install -r requirements.txt
python main.py
```

Para leer directamente la unidad hay que abrir la terminal como Administrador.
Para probarlo sin lector se puede generar una imagen de demostración con varios
archivos ya colocados y abrirla desde *Archivo > Abrir imagen de disco*:

```bash
python -m recuperador.demo demo.iso
```

También hay un ejecutable para Windows en
[Releases](https://github.com/Franciscojav812/Recuperador-de-archivos-/releases).

### Línea de comandos

```
$ python main.py --imagen demo.iso --destino salida --quiet
11 archivos encontrados en Imagen demo.iso (0 sectores ilegibles).
  recuperado_00001_sector10.jpg            Imagen JPEG                   23.1 KB  OK
  recuperado_00002_sector60.jpg            Imagen JPEG                   20.8 KB  OK
  recuperado_00003_sector110.png           Imagen PNG                    14.2 KB  OK
  ...
  recuperado_00011_sector450.mp4           Vídeo MP4/MOV                  9.3 KB  OK
Guardados en salida. Informe: informe_recuperacion.csv
```

Otras opciones: `--unidad D:`, `--tipos jpg,png,pdf`, `--solo-listar`. Ver `python main.py --help`.

### Atajos de teclado

`F5` escanear, `Esc` detener, `Ctrl+O` abrir imagen, `Ctrl+E` extraer selección,
`Ctrl+Mayús+E` extraer todo, `Mayús+F10` menú contextual.

## Cómo funciona

1. Se lee el disco en bloques de 64 KB. Si un bloque falla, se reintenta sector a
   sector para saber exactamente cuáles están dañados.
2. En ISO 9660 y UDF cada archivo empieza al inicio de un sector de 2048 bytes, así
   que solo se comprueba la cabecera de cada sector. Es más rápido que buscar firmas
   byte a byte y casi no da falsos positivos.
3. Cuando una cabecera coincide, se recorre la estructura del formato para saber
   dónde termina el archivo:

| Formato | Cómo se calcula el final |
|---|---|
| JPEG | Segmentos hasta SOS y después datos comprimidos hasta el marcador EOI. |
| PNG | Chunks hasta `IEND`. |
| GIF | Bloques y sub-bloques hasta el byte `0x3B`. |
| PDF | Último `%%EOF`, incluidas las actualizaciones incrementales. |
| ZIP y Office | Registro final (EOCD), comprobando que cuadra con el directorio central. |
| BMP, WAV, AVI | Tamaño declarado en la cabecera. |
| MP4/MOV | Recorrido de las cajas de nivel superior. |

Si no se encuentra el final (archivo truncado), se extrae un tamaño máximo por
formato y el archivo aparece como "Tamaño estimado".

## Estructura

```
main.py                 Punto de entrada (GUI sin argumentos, CLI con argumentos)
recuperador/
  sources.py            Lectura de sectores: unidad Win32 e imagen de disco
  signatures.py         Firmas y cálculo del final de cada formato
  carver.py             Escaneo, tolerancia a errores, extracción e informe
  gui.py                Interfaz gráfica
  cli.py                Línea de comandos
  demo.py               Generador de imágenes de prueba
tests/                  Tests con pytest
```

El motor (`sources`, `signatures`, `carver`) no depende de la interfaz. La GUI y la
CLI lo usan igual, y los tests lo prueban sin ventana ni lector. En la GUI, los
hilos de escaneo y extracción se comunican con la interfaz mediante una cola,
porque tkinter no admite llamadas desde otros hilos.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Comprueban, entre otras cosas, que los archivos recuperados son idénticos byte a
byte a los originales, que un sector ilegible no desplaza el resto del escaneo, que
se recorre el disco completo y que el ruido aleatorio no genera falsos positivos.

## Limitaciones

- Supone que cada archivo ocupa sectores contiguos, lo normal en discos ópticos
  pero no siempre (por ejemplo, en discos multisesión modificados).
- No recupera los nombres originales: los archivos se nombran por el sector donde
  empiezan.
- No busca vídeo de DVD (VOB): cada sector de un VOB empieza con una cabecera
  MPEG y aparecerían miles de coincidencias.
- La lectura directa de la unidad solo está implementada en Windows. En Linux o
  macOS se puede crear antes una imagen con `ddrescue /dev/sr0 disco.iso disco.map`.

## Próximos pasos

- Leer ISO 9660/Joliet y UDF para recuperar nombres y carpetas cuando el sistema
  de archivos esté parcialmente sano.
- Detección de sesiones en discos multisesión.
- Lectura directa en Linux (`/dev/sr0`).

## Licencia

MIT. Úsalo solo con discos propios o con autorización del propietario.
