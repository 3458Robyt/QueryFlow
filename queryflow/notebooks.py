from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


class NotebookError(RuntimeError):
    """A notebook is malformed or cannot be safely edited."""


def _parse(raw: bytes) -> dict:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise NotebookError("El notebook no es JSON UTF-8 válido") from error
    if not isinstance(value, dict) or not isinstance(value.get("cells"), list):
        raise NotebookError("El notebook no contiene una lista cells")
    return value


def _cell_source(cell: dict) -> str:
    source = cell.get("source", "")
    if isinstance(source, str):
        return source
    if isinstance(source, list) and all(isinstance(item, str) for item in source):
        return "".join(source)
    raise NotebookError("Una celda contiene un source inválido")


def extract_code_cells(raw: bytes) -> list[tuple[int, str, str]]:
    notebook = _parse(raw)
    result: list[tuple[int, str, str]] = []
    for index, cell in enumerate(notebook["cells"]):
        if not isinstance(cell, dict) or cell.get("cell_type") != "code":
            continue
        metadata = cell.get("metadata") or {}
        language = str(
            metadata.get("language")
            or notebook.get("metadata", {}).get("kernelspec", {}).get("language")
            or notebook.get("metadata", {}).get("language_info", {}).get("name")
            or "sql"
        ).lower()
        result.append((index, language, _cell_source(cell)))
    return result


def extract_notebook_cells(raw: bytes) -> list[tuple[int, str, str, str]]:
    """Return every notebook cell as (index, type, language, source)."""
    notebook = _parse(raw)
    result: list[tuple[int, str, str, str]] = []
    for index, cell in enumerate(notebook["cells"]):
        if not isinstance(cell, dict):
            continue
        cell_type = str(cell.get("cell_type") or "")
        metadata = cell.get("metadata") or {}
        language = str(
            metadata.get("language")
            or notebook.get("metadata", {}).get("kernelspec", {}).get("language")
            or ("markdown" if cell_type == "markdown" else "python")
        ).lower()
        result.append((index, cell_type, language, _cell_source(cell)))
    return result


_SQL_START = re.compile(
    r"^(?:SELECT|WITH|EXPLAIN|SHOW|DESCRIBE|INSERT|UPDATE|DELETE|MERGE|CREATE|"
    r"ALTER|DROP|TRUNCATE|EXPORT|LOAD|CALL)\b",
    re.IGNORECASE,
)


def _looks_like_sql(value: str) -> bool:
    return bool(_SQL_START.match(value.lstrip()))


def _without_cell_magics(source: str) -> str:
    lines = source.splitlines()
    while lines and lines[0].lstrip().startswith("%"):
        lines.pop(0)
    return "\n".join(lines)


def _literal_sql_from_python(source: str) -> list[str]:
    try:
        tree = ast.parse(_without_cell_magics(source))
    except SyntaxError:
        return []
    values: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and _looks_like_sql(node.value):
            if node.value not in values:
                values.append(node.value)
    return values


@dataclass(frozen=True)
class SqlExtraction:
    fragments: list[tuple[int, str]]
    dynamic_cells: list[int]

    def to_dict(self) -> dict[str, object]:
        return {
            "fragments": [
                {
                    "cell": index,
                    "sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                    "characters": len(sql),
                }
                for index, sql in self.fragments
            ],
            "dynamic_cells": self.dynamic_cells,
        }


def _literal_sql_values(node: ast.AST) -> list[str]:
    """Return concrete SQL strings from a Python expression."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and _looks_like_sql(node.value):
        return [node.value]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "replace":
        if node.args:
            return _literal_sql_values(node.args[0])
    return []


def _python_symbol_table(trees: list[ast.AST]) -> tuple[dict[str, list[str]], set[str]]:
    symbols: dict[str, list[str]] = {}
    unresolved: set[str] = set()
    for tree in trees:
        for node in ast.walk(tree):
            targets: list[ast.Name] = []
            value: ast.AST | None = None
            if isinstance(node, ast.Assign):
                targets = [target for target in node.targets if isinstance(target, ast.Name)]
                value = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets = [node.target]
                value = node.value
            if not targets or value is None:
                continue
            values = _literal_sql_values(value)
            for target in targets:
                if values:
                    symbols[target.id] = values
                    unresolved.discard(target.id)
                else:
                    unresolved.add(target.id)
                    symbols.pop(target.id, None)
    return symbols, unresolved


def analyze_sql_fragments(raw: bytes) -> SqlExtraction:
    """Extract concrete SQL and report cells whose executed SQL is dynamic."""
    fragments: list[tuple[int, str]] = []
    dynamic_cells: list[int] = []
    python_trees: list[tuple[int, ast.AST]] = []
    python_sources: dict[int, str] = {}
    for index, language, source in extract_code_cells(raw):
        stripped = source.lstrip()
        if language in {"sql", "bigquery"} or stripped.lower().startswith(("%%sql", "%%bigquery")):
            body = _without_cell_magics(source).strip()
            if body and _looks_like_sql(body):
                fragments.append((index, body))
            continue
        try:
            parsed_tree: ast.AST = ast.parse(_without_cell_magics(source))
        except SyntaxError:
            continue
        python_trees.append((index, parsed_tree))
        python_sources[index] = source

    symbols, unresolved = _python_symbol_table([tree for _index, tree in python_trees])
    for index, tree in python_trees:
        cell_dynamic = False
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr) and any(
                isinstance(value, ast.Constant)
                and isinstance(value.value, str)
                and _looks_like_sql(value.value)
                for value in node.values
            ):
                cell_dynamic = True
            if not isinstance(node, ast.Call) or not node.args:
                continue
            function_name = ""
            if isinstance(node.func, ast.Attribute):
                function_name = node.func.attr
            elif isinstance(node.func, ast.Name):
                function_name = node.func.id
            if function_name not in {"query", "read_gbq", "read_gbq_query"}:
                continue
            argument = node.args[0]
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                continue
            if isinstance(argument, ast.Name) and argument.id in symbols and argument.id not in unresolved:
                continue
            cell_dynamic = True
        values: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and _looks_like_sql(node.value):
                values.append(node.value)
        if cell_dynamic:
            dynamic_cells.append(index)
            continue
        for value in values:
            if (index, value) not in fragments:
                fragments.append((index, value))
    return SqlExtraction(fragments=fragments, dynamic_cells=sorted(set(dynamic_cells)))


def extract_sql_fragments(raw: bytes) -> list[tuple[int, str]]:
    """Extract literal SQL from SQL cells and Python cells.

    BigQuery Studio notebooks are commonly Python notebooks with SQL stored in
    triple-quoted variables passed to ``client.query``. Treating the complete
    Python source as SQL produces false validation failures, so only explicit
    SQL cells or literal strings beginning with a SQL statement are returned.
    Dynamic SQL is deliberately skipped and remains visible in the notebook
    diff for manual review.
    """
    return [(index, sql.strip()) for index, sql in analyze_sql_fragments(raw).fragments]


def write_cell_files(raw: bytes, destination: Path) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, language, source in extract_code_cells(raw):
        suffix = "sql" if language in {"sql", "bigquery"} else "py"
        path = destination / f"{index:04d}.{suffix}"
        path.write_text(source, encoding="utf-8")
        paths.append(path)
    return paths


def _source_suffix(cell_type: str, language: str) -> str:
    if cell_type == "markdown":
        return "md"
    if language in {"sql", "bigquery"}:
        return "sql"
    return "py"


def build_cell_index(raw: bytes) -> dict[str, Any]:
    notebook = _parse(raw)
    entries: list[dict[str, Any]] = []
    for index, cell in enumerate(notebook["cells"]):
        if not isinstance(cell, dict):
            raise NotebookError(f"La celda {index} no es un objeto")
        cell_type = str(cell.get("cell_type") or "")
        if cell_type not in {"code", "markdown", "raw"}:
            raise NotebookError(f"Tipo de celda no soportado: {cell_type}")
        metadata = cell.get("metadata") or {}
        language = str(
            metadata.get("language")
            or notebook.get("metadata", {}).get("kernelspec", {}).get("language")
            or ("markdown" if cell_type == "markdown" else "python")
        ).lower()
        entries.append(
            {
                "id": f"b{index:04d}",
                "baseline_index": index,
                "cell_type": cell_type,
                "language": language,
                "path": f"b{index:04d}.{_source_suffix(cell_type, language)}",
                "deleted": False,
            }
        )
    return {"schema_version": 1, "cells": entries}


def write_cell_workspace(raw: bytes, destination: Path) -> list[Path]:
    """Write all notebook cells and an ordered editable index."""
    destination.mkdir(parents=True, exist_ok=True)
    notebook = _parse(raw)
    index = build_cell_index(raw)
    paths: list[Path] = []
    for entry, cell in zip(index["cells"], notebook["cells"]):
        source = _cell_source(cell)
        path = destination / str(entry["path"])
        path.write_text(source, encoding="utf-8")
        paths.append(path)
    (destination / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return paths


def empty_notebook() -> bytes:
    return (
        json.dumps(
            {
                "cells": [],
                "metadata": {
                    "kernelspec": {
                        "display_name": "Python 3",
                        "language": "python",
                        "name": "python3",
                    },
                    "language_info": {"name": "python"},
                },
                "nbformat": 4,
                "nbformat_minor": 5,
            },
            ensure_ascii=False,
            indent=1,
        )
        + "\n"
    ).encode("utf-8")


def initialize_new_notebook_workspace(
    destination: Path,
    *,
    display_name: str,
    project: str,
    location: str,
) -> None:
    """Create a safe, editable Python/BigQuery notebook proposal."""
    destination.mkdir(parents=True, exist_ok=True)
    cells = [
        {
            "id": "n0000",
            "baseline_index": None,
            "cell_type": "markdown",
            "language": "markdown",
            "path": "n0000.md",
            "deleted": False,
            "source": f"# {display_name}\n",
        },
        {
            "id": "n0001",
            "baseline_index": None,
            "cell_type": "code",
            "language": "python",
            "path": "n0001.py",
            "deleted": False,
            "source": "from google.cloud import bigquery\n",
        },
        {
            "id": "n0002",
            "baseline_index": None,
            "cell_type": "code",
            "language": "python",
            "path": "n0002.py",
            "deleted": False,
            "source": (
                f'PROJECT_ID = "{project}"\n'
                f'LOCATION = "{location}"\n'
                "client = bigquery.Client(project=PROJECT_ID, location=LOCATION)\n"
            ),
        },
        {
            "id": "n0003",
            "baseline_index": None,
            "cell_type": "code",
            "language": "python",
            "path": "n0003.py",
            "deleted": False,
            "source": 'sql = """\n"""\n',
        },
        {
            "id": "n0004",
            "baseline_index": None,
            "cell_type": "code",
            "language": "python",
            "path": "n0004.py",
            "deleted": False,
            "source": "df = client.query(sql).to_dataframe()\n",
        },
    ]
    index = {"schema_version": 1, "cells": [{key: value for key, value in cell.items() if key != "source"} for cell in cells]}
    (destination / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for cell in cells:
        (destination / str(cell["path"])).write_text(str(cell["source"]), encoding="utf-8")


def _safe_cell_path(directory: Path, relative: str) -> Path:
    candidate = (directory / relative).resolve()
    root = directory.resolve()
    if root not in candidate.parents or candidate.name == "index.json":
        raise NotebookError("La ruta de celda queda fuera del workspace")
    return candidate


def rebuild_notebook_from_workspace(baseline: bytes, directory: Path) -> bytes:
    """Rebuild a notebook from the ordered cell index without touching the baseline."""
    index_path = directory / "index.json"
    if not index_path.exists():
        return rebuild_notebook(baseline, {})
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise NotebookError("cells/index.json no es JSON válido") from error
    if not isinstance(index, dict) or index.get("schema_version") != 1 or not isinstance(index.get("cells"), list):
        raise NotebookError("cells/index.json tiene un esquema inválido")
    original = _parse(baseline)
    output_cells: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for entry in index["cells"]:
        if not isinstance(entry, dict):
            raise NotebookError("Una entrada de cells/index.json no es un objeto")
        cell_id = entry.get("id")
        path = entry.get("path")
        cell_type = entry.get("cell_type")
        if not isinstance(cell_id, str) or cell_id in seen_ids:
            raise NotebookError("cells/index.json contiene IDs duplicados o inválidos")
        if not isinstance(path, str) or not isinstance(cell_type, str):
            raise NotebookError("Una entrada de cells/index.json no contiene path o cell_type")
        seen_ids.add(cell_id)
        if entry.get("deleted"):
            continue
        source = _safe_cell_path(directory, path).read_text(encoding="utf-8")
        baseline_index = entry.get("baseline_index")
        if baseline_index is None:
            cell: dict[str, Any] = {
                "cell_type": cell_type,
                "metadata": {},
                "source": source.splitlines(keepends=True),
            }
            if cell_type == "code":
                cell.update({"execution_count": None, "outputs": []})
        else:
            if not isinstance(baseline_index, int) or baseline_index < 0 or baseline_index >= len(original["cells"]):
                raise NotebookError(f"baseline_index inválido para {cell_id}")
            cell = deepcopy(original["cells"][baseline_index])
            if not isinstance(cell, dict) or cell.get("cell_type") != cell_type:
                raise NotebookError(f"El tipo de celda cambió para {cell_id}")
            cell["source"] = source.splitlines(keepends=True) if isinstance(cell.get("source"), list) else source
        output_cells.append(cell)
    result = deepcopy(original)
    result["cells"] = output_cells
    # A cell workspace is also created for an untouched notebook.  Avoid
    # changing indentation/trailing-newline bytes when the reconstructed
    # notebook is semantically identical to the baseline; its hash is part of
    # the migration integrity contract.
    if result == original:
        return baseline
    return (json.dumps(result, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


def rebuild_notebook(raw: bytes, changed_cells: dict[int, str]) -> bytes:
    notebook = _parse(raw)
    for index, source in changed_cells.items():
        if not isinstance(index, int) or index < 0 or index >= len(notebook["cells"]):
            raise NotebookError(f"Índice de celda inválido: {index}")
        cell = notebook["cells"][index]
        if not isinstance(cell, dict) or cell.get("cell_type") != "code":
            raise NotebookError(f"La celda {index} no es de código")
        original_source = cell.get("source", "")
        if isinstance(original_source, list):
            cell["source"] = source.splitlines(keepends=True)
        elif isinstance(original_source, str):
            cell["source"] = source
        else:
            raise NotebookError(f"La celda {index} contiene source inválido")
    return (json.dumps(notebook, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


def changed_cells_from_directory(directory: Path, *, allowed: Optional[Iterable[int]] = None) -> Dict[int, str]:
    allowed_set = set(allowed) if allowed is not None else None
    changed: dict[int, str] = {}
    if not directory.exists():
        return changed
    for path in sorted(directory.iterdir()):
        if path.suffix not in {".sql", ".txt", ".py", ".md"}:
            continue
        try:
            index = int(path.stem)
        except ValueError:
            continue
        if allowed_set is not None and index not in allowed_set:
            raise NotebookError(f"La celda {index} no está en el baseline")
        changed[index] = path.read_text(encoding="utf-8")
    return changed
