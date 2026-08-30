from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from .dataset import DataSet
from .errors import DataTransformerError
from .formats import infer_format
from .json_values import json_safe
from .workspace import INTERNAL_ORDER, Workspace, quote_identifier, resolve_resource_path


def preflight_output(
    output: dict[str, Any],
    base_dir: Path,
    *,
    resource_root: Path | None = None,
    restricted_paths: bool = False,
) -> dict[str, Any]:
    if not isinstance(output, dict) or not isinstance(output.get("path"), str):
        raise DataTransformerError("E_OUTPUT_INVALID", "output requires a path")
    target = resolve_resource_path(
        output["path"],
        base_dir,
        resource_root=resource_root,
        restricted_paths=restricted_paths,
    )
    if not target.parent.exists() or not target.parent.is_dir():
        raise DataTransformerError(
            "E_OUTPUT_DIRECTORY", "output directory does not exist", {"path": str(target.parent)}
        )
    if target.exists() and (target.is_symlink() or not target.is_file()):
        raise DataTransformerError(
            "E_OUTPUT_INVALID",
            "output target must be a regular file or a new file path",
            {"path": str(target)},
        )
    if target.exists() and not output.get("overwrite", False):
        raise DataTransformerError(
            "E_OUTPUT_EXISTS",
            "output file already exists; set overwrite to true to replace it",
            {"path": str(target)},
        )
    return {
        "target": target,
        "format": infer_format(target, output.get("format")),
        "overwrite": bool(output.get("overwrite", False)),
    }


def reserve_staging_output(target: Path) -> Path:
    try:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.adt-stage-", dir=target.parent
        )
        os.close(descriptor)
        return Path(temp_name)
    except OSError as exc:
        raise DataTransformerError(
            "E_IO", "could not reserve output staging file", {"path": str(target)}
        ) from exc


def publish_staged_output(staging: Path, target: Path, overwrite: bool) -> None:
    try:
        if overwrite:
            os.replace(staging, target)
        else:
            try:
                os.link(staging, target)
            except FileExistsError as exc:
                raise DataTransformerError(
                    "E_OUTPUT_EXISTS",
                    "output file was created concurrently; it was not replaced",
                    {"path": str(target)},
                ) from exc
            staging.unlink()
    except DataTransformerError:
        raise
    except OSError as exc:
        raise DataTransformerError(
            "E_IO", "could not publish output file", {"path": str(target)}
        ) from exc


def check_output_feasibility(
    workspace: Workspace, dataset: DataSet, data_format: str
) -> None:
    """Reject format/data combinations the writer cannot represent, before any write.

    Dry-run calls the same check so it reaches the same decision as the real run.
    """
    if not dataset.is_table:
        if data_format not in {"json", "yaml"}:
            raise DataTransformerError(
                "E_TYPE_MISMATCH",
                "tree output supports only JSON and YAML",
                {"format": data_format},
            )
        return
    columns = workspace.columns(dataset.table_name or "")
    if not columns:
        if data_format == "parquet":
            raise DataTransformerError(
                "E_OUTPUT_EMPTY_SCHEMA",
                "cannot write schema-less empty data to Parquet",
            )
        return
    if workspace.row_count(dataset.table_name or "") == 0 and data_format in {
        "json",
        "jsonl",
        "yaml",
    }:
        raise DataTransformerError(
            "E_OUTPUT_SCHEMA_LOSS",
            "this output format cannot preserve the schema of an empty table",
            {"format": data_format, "fields": columns},
        )


def write_output(
    workspace: Workspace,
    dataset: DataSet,
    output: dict[str, Any],
    base_dir: Path,
) -> dict[str, Any]:
    prepared = preflight_output(
        output,
        base_dir,
        resource_root=workspace.resource_root,
        restricted_paths=workspace.restricted_paths,
    )
    target = prepared["target"]
    data_format = prepared["format"]
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{target.name}.adt-", dir=target.parent)
    os.close(descriptor)
    temporary = Path(temp_name)
    try:
        if dataset.is_table:
            temporary.unlink()
            _write_table(workspace, dataset, temporary, data_format)
        else:
            _write_tree(dataset.value, temporary, data_format)
        workspace.check_temp_budget([temporary])
        if output.get("overwrite", False):
            os.replace(temporary, target)
        else:
            try:
                os.link(temporary, target)
            except FileExistsError as exc:
                raise DataTransformerError(
                    "E_OUTPUT_EXISTS",
                    "output file was created concurrently; it was not replaced",
                    {"path": str(target)},
                ) from exc
            temporary.unlink()
    except DataTransformerError:
        temporary.unlink(missing_ok=True)
        raise
    except (OSError, ValueError) as exc:
        temporary.unlink(missing_ok=True)
        raise DataTransformerError(
            "E_IO", "could not write output file", {"path": str(target)}
        ) from exc
    return {
        "path": str(target),
        "format": data_format,
        "bytes": target.stat().st_size,
        "sha256": _sha256(target),
    }


def _write_table(workspace: Workspace, dataset: DataSet, path: Path, data_format: str) -> None:
    check_output_feasibility(workspace, dataset, data_format)
    table = dataset.table_name or ""
    columns = workspace.columns(table)
    if not columns:
        if data_format == "json":
            path.write_text("[]\n", encoding="utf-8")
        elif data_format in {"jsonl", "csv", "tsv", "yaml"}:
            path.write_text("", encoding="utf-8")
        return
    select = ", ".join(quote_identifier(column) for column in columns)
    query = (
        f"SELECT {select} FROM {quote_identifier(table)} "
        f"ORDER BY {quote_identifier(INTERNAL_ORDER)}"
    )
    sql_path = "'" + str(path).replace("'", "''") + "'"
    try:
        if data_format == "json":
            with path.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write("[\n")
                first = True
                for row in workspace.iter_rows(dataset):
                    if not first:
                        handle.write(",\n")
                    handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
                    first = False
                handle.write("\n]\n")
        elif data_format == "jsonl":
            with path.open("w", encoding="utf-8", newline="\n") as handle:
                for row in workspace.iter_rows(dataset):
                    handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
                    handle.write("\n")
        elif data_format in {"csv", "tsv"}:
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=columns,
                    delimiter="," if data_format == "csv" else "\t",
                    lineterminator="\n",
                )
                writer.writeheader()
                for row in workspace.iter_rows(dataset):
                    writer.writerow(
                        {field: _encode_delimited_value(row[field]) for field in columns}
                    )
        elif data_format == "parquet":
            workspace.connection.execute(f"COPY ({query}) TO {sql_path} (FORMAT PARQUET)")
        elif data_format == "yaml":
            with path.open("w", encoding="utf-8", newline="\n") as handle:
                for row in workspace.iter_rows(dataset):
                    handle.write(yaml.safe_dump([row], allow_unicode=True, sort_keys=False))
        else:
            raise DataTransformerError(
                "E_FORMAT_UNSUPPORTED", "unsupported output format", {"format": data_format}
            )
    except DataTransformerError:
        raise
    except Exception as exc:
        raise DataTransformerError(
            "E_IO", "tabular output serialization failed", {"format": data_format}
        ) from exc


def _write_tree(value: Any, path: Path, data_format: str) -> None:
    if data_format not in {"json", "yaml"}:
        raise DataTransformerError(
            "E_TYPE_MISMATCH",
            "tree output supports only JSON and YAML",
            {"format": data_format},
        )
    if data_format == "json":
        path.write_text(
            json.dumps(json_safe(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    elif data_format == "yaml":
        path.write_text(
            yaml.safe_dump(json_safe(value), allow_unicode=True, sort_keys=False), encoding="utf-8"
        )


def _encode_delimited_value(value: Any) -> Any:
    if value is None:
        return r"\N"
    if isinstance(value, str) and value.startswith("\\"):
        return "\\" + value
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
