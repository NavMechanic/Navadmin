"""Regenerate ``schemas/`` from the Pydantic models (N0 families, N1 ``n1_*``, N2 ``n2_*``, N3 ``n3_*``,
N4 ``n4_*``, N5 ``n5_*``, N6 ``n6_*``, N7 ``n7_*`` and N8 ``n8_*`` schemas and the combined
``N0_N1_N2_N3_N4_N5_OpenAPI.json``).

    python -m app.schema_export            # write
    python -m app.schema_export --check    # exit 1 if the committed files have drifted
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from .models import EXTRA_SCHEMAS, PAYLOAD_MODELS, SCHEMA_FAMILIES
from .n1.models import N1_SCHEMAS
from .n2.models import N2_SCHEMAS
from .n3.models import N3_EXTRA_SCHEMAS, N3_SCHEMAS
from .n4.models import N4_EXTRA_SCHEMAS, N4_SCHEMAS
from .n5.models import N5_EXTRA_SCHEMAS, N5_SCHEMAS
from .n6.service import N6_SCHEMAS
from .n7.service import N7_SCHEMAS
from .n8.service import N8_SCHEMAS

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"
DIALECT = "https://json-schema.org/draft/2020-12/schema"


def schema_for(name: str, model) -> dict:
    return {"$schema": DIALECT, "$id": f"urn:athena:schema:{name}", **model.model_json_schema(mode="validation")}


def all_schemas() -> dict[str, dict]:
    out = {f"{n}.schema.json": schema_for(n, m) for n, m in {**SCHEMA_FAMILIES, **EXTRA_SCHEMAS}.items()}
    for m in PAYLOAD_MODELS.values():
        out[f"payloads/{m.model_config['title']}.schema.json"] = schema_for(m.model_config["title"], m)
    for n, m in {**N1_SCHEMAS, **N2_SCHEMAS, **N3_SCHEMAS, **N3_EXTRA_SCHEMAS, **N4_SCHEMAS, **N4_EXTRA_SCHEMAS,
                 **N5_SCHEMAS, **N5_EXTRA_SCHEMAS, **N6_SCHEMAS, **N7_SCHEMAS, **N8_SCHEMAS}.items():
        out[f"{n}.schema.json"] = schema_for(n, m)
    return out


def openapi_spec() -> dict:
    from .config import Settings
    from .main import create_app
    from .n1.config import N1Settings
    from .n3.config import N3Settings
    with tempfile.TemporaryDirectory() as d:
        app = create_app(Settings(database_url=f"sqlite:///{d}/spec.db", storage_root=f"{d}/objects",
                                  auto_create_schema=False, jwt_secret="x" * 32), n1_settings=N1Settings(),
                         n3_settings=N3Settings(kicad_cli="kicad-cli", ngspice="ngspice", workroot=f"{d}/jobs"))
        spec = app.openapi()
        app.state.n3.worker.shutdown()
        return spec


def render(obj: dict) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def expected_files() -> dict[str, str]:
    files = {p: render(s) for p, s in all_schemas().items()}
    files["N0_N1_N2_N3_N4_N5_OpenAPI.json"] = render(openapi_spec())
    return files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--out", type=Path, default=SCHEMA_DIR)
    args = ap.parse_args(argv)
    files = expected_files()
    if args.check:
        drift = [p for p, c in files.items() if not (args.out / p).exists() or (args.out / p).read_text() != c]
        for p in drift:
            print(f"out of date: schemas/{p}", file=sys.stderr)
        return 1 if drift else 0
    for p, c in files.items():
        (args.out / p).parent.mkdir(parents=True, exist_ok=True)
        (args.out / p).write_text(c, encoding="utf-8")
    print(f"wrote {len(files)} files to {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
