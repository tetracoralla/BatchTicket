from __future__ import annotations

import argparse
import json
from pathlib import Path

from data_transformer.contracts import TransformationPlan


def _render_schema() -> str:
    schema = TransformationPlan.model_json_schema(by_alias=True)
    schema.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://openadam.local/schemas/data-transformer/plan-v1.json",
            "title": "BatchTicket Transformation Plan v1",
        }
    )
    return json.dumps(schema, ensure_ascii=False, indent=2) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the published Plan v1 schema")
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the published schema differs from the live contract",
    )
    args = parser.parse_args()
    target = Path(__file__).resolve().parents[1] / "schemas" / "transformation-plan.schema.json"
    rendered = _render_schema()
    if args.check:
        if not target.exists() or target.read_text(encoding="utf-8") != rendered:
            raise SystemExit("published Plan v1 schema is out of date")
        return
    target.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
