from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a JSON artifact against a JSON Schema file.")
    parser.add_argument("--schema", required=True, help="Path to the JSON Schema file.")
    parser.add_argument("--file", required=True, help="Path to the JSON artifact file.")
    args = parser.parse_args()

    schema_path = Path(args.schema)
    file_path = Path(args.file)

    try:
        schema = load_json(schema_path)
        document = load_json(file_path)
    except FileNotFoundError as exc:
        emit({"status": "error", "message": f"file not found: {exc.filename}"})
        return 2
    except json.JSONDecodeError as exc:
        emit({"status": "error", "message": f"invalid JSON: {exc}"})
        return 2

    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        emit(
            {
                "status": "dependency_missing",
                "message": "jsonschema is not installed. Install it to run full JSON Schema validation.",
                "schema": str(schema_path),
                "file": str(file_path),
            }
        )
        return 2

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.path))
    if errors:
        emit(
            {
                "status": "fail",
                "schema": str(schema_path),
                "file": str(file_path),
                "errors": [
                    {
                        "path": "/".join(str(part) for part in error.path),
                        "message": error.message,
                    }
                    for error in errors
                ],
            }
        )
        return 1

    emit({"status": "pass", "schema": str(schema_path), "file": str(file_path)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
