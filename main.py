"""
Lanzador: ``python main.py`` abre la interfaz gráfica.
``python main.py --help`` muestra las opciones de línea de comandos.

También es el script que se empaqueta con PyInstaller para generar el .exe.
"""

import sys

from recuperador.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
