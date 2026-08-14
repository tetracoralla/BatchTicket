from __future__ import annotations

import re
from typing import Any

from .errors import DataTransformerError

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


def parse_selector(selector: str) -> list[str | int]:
    text = selector.strip()
    if text.startswith("$"):
        text = text[1:]
        if text.startswith("."):
            text = text[1:]
    if not text:
        return []

    tokens: list[str | int] = []
    position = 0
    expect_name = True
    while position < len(text):
        if text[position] == ".":
            if expect_name:
                raise _selector_error(selector, position)
            position += 1
            expect_name = True
            continue
        if text[position] == "[":
            end = text.find("]", position + 1)
            if end == -1:
                raise _selector_error(selector, position)
            raw = text[position + 1 : end]
            if raw == "*":
                tokens.append("*")
            elif raw.isdigit():
                tokens.append(int(raw))
            else:
                raise _selector_error(selector, position)
            position = end + 1
            expect_name = False
            continue
        match = _NAME.match(text, position)
        if match is None or not expect_name:
            raise _selector_error(selector, position)
        tokens.append(match.group(0))
        position = match.end()
        expect_name = False
    if expect_name:
        raise _selector_error(selector, len(text) - 1)
    return tokens


def select_value(value: Any, selector: str) -> Any:
    current: list[Any] = [value]
    expanded = False
    for token in parse_selector(selector):
        next_values: list[Any] = []
        for item in current:
            if token == "*":
                if not isinstance(item, list):
                    raise DataTransformerError(
                        "E_SELECTOR_TYPE",
                        "selector wildcard requires an array",
                        {"selector": selector},
                    )
                next_values.extend(item)
                expanded = True
            elif isinstance(token, int):
                if not isinstance(item, list) or token >= len(item):
                    raise DataTransformerError(
                        "E_SELECTOR_NOT_FOUND",
                        "selector index does not exist",
                        {"selector": selector, "index": token},
                    )
                next_values.append(item[token])
            else:
                if not isinstance(item, dict) or token not in item:
                    raise DataTransformerError(
                        "E_SELECTOR_NOT_FOUND",
                        "selector field does not exist",
                        {"selector": selector, "field": token},
                    )
                next_values.append(item[token])
        current = next_values
    if expanded:
        return current
    return current[0] if current else None


def _selector_error(selector: str, position: int) -> DataTransformerError:
    return DataTransformerError(
        "E_SELECTOR_SYNTAX",
        "selector uses unsupported syntax",
        {"selector": selector, "position": position},
    )
