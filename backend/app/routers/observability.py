from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from ..services import observability as obs

router = APIRouter(prefix="/api/observability", tags=["observability"])

REGION = r"^[a-z]{2}(-[a-z]+)+-\d$"


@router.get("/status")
def status(region: str = Query(pattern=REGION)):
    return obs.transaction_search_status(region)


class EnableBody(BaseModel):
    region: str = Field(pattern=REGION)
    confirm: bool = False


@router.post("/transaction-search")
def enable(body: EnableBody):
    return obs.enable_transaction_search(body.region, body.confirm)
