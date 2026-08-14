from __future__ import annotations

from multiprocessing import freeze_support


def main() -> None:
    freeze_support()

    from data_transformer.mcp_server import main as run_server

    run_server()


if __name__ == "__main__":
    main()
