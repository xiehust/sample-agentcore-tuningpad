from __future__ import annotations

from fastapi import APIRouter

from ..core.config import get_settings

router = APIRouter(prefix="/api/meta", tags=["meta"])


@router.get("")
def meta():
    s = get_settings()
    return {
        "version": "0.1.0",
        "default_region": s.default_region,
        "resource_tag": s.resource_tag,
        "toolkit_path": str(s.toolkit_path),
        "toolkit_present": (s.toolkit_path / "pyproject.toml").is_file(),
        "run_mode": s.run_mode,
    }
