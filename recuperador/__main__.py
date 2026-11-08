"""Punto de entrada: sin argumentos abre la GUI; con argumentos, la CLI."""

import sys


def main() -> int:
    if len(sys.argv) > 1:
        from .cli import main as cli_main
        return cli_main()
    from .gui import run
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
