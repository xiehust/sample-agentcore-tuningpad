from __future__ import annotations

from fastapi import APIRouter

from ..core.config import get_settings
from ..services import toolkit_source

router = APIRouter(prefix="/api/meta", tags=["meta"])


@router.get("")
def meta():
    s = get_settings()
    toolkit_path, toolkit_present, _ = toolkit_source.inspect_source()
    return {
        "version": "0.1.0",
        "default_region": s.default_region,
        "resource_tag": s.resource_tag,
        "toolkit_path": str(toolkit_path),
        "toolkit_present": toolkit_present,
        "run_mode": s.run_mode,
    }
