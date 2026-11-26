"""Regenerate ``schemas/`` from the Pydantic models — the models are the single source of truth.

    python -m app.schema_export            # write schemas/*.schema.json and schemas/openapi.json
    python -m app.schema_export --check    # exit 1 if the committed files have drifted

Every file declares ``"$schema": "https://json-schema.org/draft/2020-12/schema"`` and a URN ``$id``.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from .models import EXTRA_SCHEMAS, PAYLOAD_MODELS, SCHEMA_FAMILIES

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"
DIALECT = "https://json-schema.org/draft/2020-12/schema"


def schema_for(name: str, model) -> dict:
    return {"$schema": DIALECT, "$id": f"urn:athena:n0:schema:{name}", **model.model_json_schema(mode="validation")}


def all_schemas() -> dict[str, dict]:
    out = {f"{name}.schema.json": schema_for(name, model) for name, model in {**SCHEMA_FAMILIES, **EXTRA_SCHEMAS}.items()}
    for model in PAYLOAD_MODELS.values():
        name = model.model_config["title"]
        out[f"payloads/{name}.schema.json"] = schema_for(name, model)
    return out


def openapi_spec() -> dict:
    from .config import Settings
    from .main import create_app
    with tempfile.TemporaryDirectory() as d:
        app = create_app(Settings(database_url=f"sqlite:///{d}/spec.db", storage_root=f"{d}/objects",
                                  auto_create_schema=False))
        return app.openapi()


def render(obj: dict) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def expected_files() -> dict[str, str]:
    files = {path: render(s) for path, s in all_schemas().items()}
    files["openapi.json"] = render(openapi_spec())
    return files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="fail if schemas/ is out of date")
    ap.add_argument("--out", type=Path, default=SCHEMA_DIR)
    args = ap.parse_args(argv)
    files = expected_files()
    if args.check:
        drift = [p for p, content in files.items()
                 if not (args.out / p).exists() or (args.out / p).read_text(encoding="utf-8") != content]
        for p in drift:
            print(f"out of date: schemas/{p}", file=sys.stderr)
        return 1 if drift else 0
    for p, content in files.items():
        target = args.out / p
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    print(f"wrote {len(files)} files to {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
