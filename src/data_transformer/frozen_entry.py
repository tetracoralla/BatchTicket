from __future__ import annotations

import sys
from multiprocessing import freeze_support

RUNTIME_MODES = {"mcp", "capability", "transport-schema-probe"}


def _runtime_mode(arguments: list[str]) -> str:
    if not arguments:
        return "mcp"
    if len(arguments) == 1 and arguments[0] in RUNTIME_MODES:
        return arguments[0]
    raise ValueError("expected one of: mcp, capability, transport-schema-probe")


def main() -> None:
    freeze_support()
    try:
        mode = _runtime_mode(sys.argv[1:])
    except ValueError as exc:
        raise SystemExit(str(exc)) from None

    if mode == "capability":
        from data_transformer.capability_adapter import main as run
    elif mode == "transport-schema-probe":
        from data_transformer.transport_schema_probe import main as run
    else:
        from data_transformer.mcp_server import main as run

    run()


if __name__ == "__main__":
    main()
