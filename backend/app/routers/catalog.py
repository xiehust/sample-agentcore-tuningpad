from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..catalog import instances, models
from ..core.config import get_settings
from ..planner import plan

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


@router.get("/instances")
def list_instances(region: str | None = None, live: bool = True):
    return instances.catalog_view(region or get_settings().default_region, live=live)


@router.get("/models/presets")
def model_presets():
    return models.PRESETS


class ModelCheckBody(BaseModel):
    model_id: str
    refresh: bool = False


@router.post("/models/check")
def check_model(body: ModelCheckBody):
    return models.check_model(body.model_id, refresh=body.refresh).to_dict()


class PlanBody(BaseModel):
    model_id: str
    instance_type: str
    nodes: int = Field(1, ge=1, le=20)
    max_model_len: int = Field(4096, ge=512, le=1_048_576)
    prefer: str = "auto"
    tuning: str = "auto"


@router.post("/plan")
def make_plan(body: PlanBody):
    m = models.check_model(body.model_id)
    spec = instances.spec(body.instance_type)
    p = plan(
        params_b=m.params_b or 0.0,
        spec=spec,
        nodes=body.nodes,
        max_model_len=body.max_model_len,
        is_moe=m.is_moe,
        prefer=body.prefer,
        tuning=body.tuning,  # type: ignore[arg-type]
    )
    out = p.to_dict()
    if not m.params_b:
        out["warnings"].append("parameter count unknown; memory estimate is unreliable")
    if m.max_position_embeddings and body.max_model_len > m.max_position_embeddings:
        out["warnings"].append(
            f"max_model_len {body.max_model_len} exceeds the model's "
            f"{m.max_position_embeddings} positions"
        )
    if not m.compatible:
        out["warnings"].insert(0, f"model is not gateway-compatible: {m.reason}")
    return {"model": m.to_dict(), "instance": spec.type, "plan": out}
