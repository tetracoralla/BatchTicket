from __future__ import annotations

import json
from pathlib import Path

from data_transformer.contracts import TransformationPlan


def main() -> None:
    schema = TransformationPlan.model_json_schema(by_alias=True)
    schema.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://openadam.local/schemas/data-transformer/plan-v1.json",
            "title": "Agent Data Transformer Plan v1",
        }
    )
    target = Path(__file__).resolve().parents[1] / "schemas" / "transformation-plan.schema.json"
    target.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
