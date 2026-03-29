#!/usr/bin/env python3
"""Generate docs/CONFIG-REFERENCE.md from config dataclasses/defaults."""

from __future__ import annotations

import argparse
import ast
import dataclasses
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, get_args, get_origin


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _config_py() -> Path:
    return _repo_root() / "modules" / "quaid" / "config.py"


def _output_path() -> Path:
    return _repo_root() / "projects" / "quaid" / "reference" / "config-reference.md"


def _type_to_string(tp: Any) -> str:
    origin = get_origin(tp)
    if origin is None:
        if hasattr(tp, "__name__"):
            return tp.__name__
        return str(tp).replace("typing.", "")
    args = get_args(tp)
    if origin in (list, List):
        return f"list[{_type_to_string(args[0])}]" if args else "list[Any]"
    if origin in (dict, Dict):
        if len(args) >= 2:
            return f"dict[{_type_to_string(args[0])}, {_type_to_string(args[1])}]"
        return "dict[Any, Any]"
    if origin in (tuple, Tuple):
        return f"tuple[{', '.join(_type_to_string(a) for a in args)}]"
    if origin is Optional:
        return f"Optional[{_type_to_string(args[0])}]" if args else "Optional[Any]"
    if str(origin).endswith("UnionType") or str(origin).endswith("Union"):
        return " | ".join(_type_to_string(a) for a in args)
    return str(tp).replace("typing.", "")


def _is_dataclass_instance(value: Any) -> bool:
    return dataclasses.is_dataclass(value) and not isinstance(value, type)


def _format_default(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return json.dumps(value)
    if isinstance(value, list):
        if len(value) <= 6 and all(isinstance(v, (str, int, float, bool, type(None))) for v in value):
            return json.dumps(value)
        return f"<list len={len(value)}>"
    if isinstance(value, dict):
        if len(value) <= 5 and all(isinstance(k, str) for k in value.keys()):
            try:
                rendered = json.dumps(value, sort_keys=True)
            except TypeError:
                rendered = f"<dict keys={len(value)}>"
            if len(rendered) <= 120:
                return rendered
        return f"<dict keys={len(value)}>"
    return str(value)


def _read_field_comments(config_source: str) -> Dict[str, str]:
    """Extract inline comments from dataclass field definitions.

    Returns mapping: "ClassName.field_name" -> "comment text"
    """
    comments: Dict[str, str] = {}
    lines = config_source.splitlines()
    tree = ast.parse(config_source)
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        is_dataclass = any(
            isinstance(dec, ast.Name) and dec.id == "dataclass"
            for dec in node.decorator_list
        )
        if not is_dataclass:
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                lineno = stmt.lineno - 1
                if 0 <= lineno < len(lines):
                    raw = lines[lineno]
                    if "#" in raw:
                        comment = raw.split("#", 1)[1].strip()
                        if comment:
                            comments[f"{node.name}.{stmt.target.id}"] = comment
    return comments


def _walk_dataclass(
    value: Any,
    class_name: str,
    prefix: str,
    comments: Dict[str, str],
    rows: List[Dict[str, str]],
) -> None:
    for field in dataclasses.fields(value):
        if field.name.startswith("_"):
            continue
        field_value = getattr(value, field.name)
        key_path = f"{prefix}.{field.name}" if prefix else field.name
        type_str = _type_to_string(field.type)
        note = comments.get(f"{class_name}.{field.name}", "")

        if _is_dataclass_instance(field_value):
            _walk_dataclass(
                field_value,
                field_value.__class__.__name__,
                key_path,
                comments,
                rows,
            )
            continue

        rows.append(
            {
                "key": key_path,
                "type": type_str,
                "default": _format_default(field_value),
                "notes": note,
            }
        )


def _group_by_top_level(rows: List[Dict[str, str]]) -> Dict[str, List[Dict[str, str]]]:
    grouped: Dict[str, List[Dict[str, str]]] = {}
    for row in rows:
        top = row["key"].split(".", 1)[0]
        grouped.setdefault(top, []).append(row)
    return grouped


def _escape_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _render_markdown(rows: List[Dict[str, str]], source_hash: str) -> str:
    grouped = _group_by_top_level(rows)
    section_order = sorted(grouped.keys())
    out: List[str] = []
    out.append("# Config Reference")
    out.append("")
    out.append("Auto-generated from `modules/quaid/config.py` dataclasses and defaults.")
    out.append("Do not edit manually. Regenerate with:")
    out.append("")
    out.append("```bash")
    out.append("python3 modules/quaid/scripts/generate-config-reference.py")
    out.append("```")
    out.append("")
    out.append(f"Source hash: `{source_hash}`")
    out.append("")
    out.append("Notes:")
    out.append("- Keys are documented in `snake_case` (loader also accepts camelCase aliases).")
    out.append("- `default` is the runtime dataclass default; nested dataclass containers are flattened.")
    out.append("- Inline `notes` come from trailing `# ...` comments in `config.py` field definitions.")
    out.append("")

    for section in section_order:
        out.append(f"## `{section}`")
        out.append("")
        out.append("| Key | Type | Default | Notes |")
        out.append("|---|---|---|---|")
        for row in grouped[section]:
            out.append(
                "| "
                f"`{_escape_cell(row['key'])}` | "
                f"`{_escape_cell(row['type'])}` | "
                f"`{_escape_cell(row['default'])}` | "
                f"{_escape_cell(row['notes'])} |"
            )
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate docs/CONFIG-REFERENCE.md")
    parser.add_argument("--check", action="store_true", help="Fail if output is not up to date")
    parser.add_argument("--output", type=Path, default=_output_path(), help="Output markdown path")
    args = parser.parse_args()

    repo_root = _repo_root()
    config_py = _config_py()
    source_text = config_py.read_text(encoding="utf-8")
    source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()[:12]

    # Import config module from repo source tree.
    sys.path.insert(0, str(repo_root / "modules" / "quaid"))
    import config as quaid_config  # type: ignore

    comments = _read_field_comments(source_text)
    cfg = quaid_config.MemoryConfig()
    rows: List[Dict[str, str]] = []
    _walk_dataclass(cfg, "MemoryConfig", "", comments, rows)
    rendered = _render_markdown(rows, source_hash)

    if args.check:
        existing = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        if existing != rendered:
            print(
                "[config-reference] OUTDATED: regenerate with "
                "python3 modules/quaid/scripts/generate-config-reference.py",
                file=sys.stderr,
            )
            return 1
        print("[config-reference] up to date")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"[config-reference] wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
