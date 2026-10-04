"""Agent templates (templates/<id>/template.yaml + agent/ overlay)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .core.config import REPO_ROOT
from .core.errors import NotFound

TEMPLATES_DIR = REPO_ROOT / "templates"


@lru_cache(maxsize=1)
def _load_all() -> dict[str, dict[str, Any]]:
    out = {}
    for f in sorted(TEMPLATES_DIR.glob("*/template.yaml")):
        t = yaml.safe_load(f.read_text())
        t["_dir"] = str(f.parent)
        out[t["id"]] = t
    return out


def list_templates() -> list[dict[str, Any]]:
    return [{k: v for k, v in t.items() if not k.startswith("_")} for t in _load_all().values()]


def get_template(template_id: str) -> dict[str, Any]:
    t = _load_all().get(template_id)
    if not t:
        raise NotFound("template.not_found", f"template {template_id} not found")
    return t


def template_dir(template_id: str) -> Path:
    return Path(get_template(template_id)["_dir"])


def resolve_params(template_id: str, given: dict[str, Any] | None) -> dict[str, Any]:
    """Defaults + validated operator overrides for a template's editable params."""
    from .core.errors import AppError

    out: dict[str, Any] = {}
    given = given or {}
    for p in get_template(template_id).get("params", []):
        v = given.get(p["key"], p.get("default"))
        if p["type"] == "select" and v not in p["options"]:
            raise AppError("template.invalid_param", f"{p['key']} must be one of {p['options']}")
        if p["type"] == "number":
            v = float(v)
            if ("min" in p and v < p["min"]) or ("max" in p and v > p["max"]):
                raise AppError("template.invalid_param", f"{p['key']} out of range")
        if p["type"] == "text" and (not isinstance(v, str) or not v.strip()):
            raise AppError("template.invalid_param", f"{p['key']} must be non-empty text")
        out[p["key"]] = v
    return out
