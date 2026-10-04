"""P-family instance catalog for HyperPod instance groups.

Static specs were read from `ec2 describe-instance-types` (us-east-1, 2026-10-04);
p5e is absent from that API in us-east-1 and comes from the EC2 accelerated
instance docs. Prices and quotas are always fetched live (Pricing API with the
`<REGION>-Cluster:ml.<type>` usage type; Service Quotas "for cluster usage").
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass

from ..core import aws


@dataclass(frozen=True)
class InstanceSpec:
    type: str  # without the ml. prefix
    gpu: str
    gpus: int
    gpu_mem_gib: float
    vcpus: int
    mem_gib: int
    efa: int  # max EFA interfaces = vpc.amazonaws.com/efa resource per node
    arch: str  # ampere | hopper | blackwell

    @property
    def ml_type(self) -> str:
        return f"ml.{self.type}"

    @property
    def multi_node(self) -> bool:
        return self.efa >= 4


CATALOG: dict[str, InstanceSpec] = {
    s.type: s
    for s in [
        InstanceSpec("p4d.24xlarge", "A100", 8, 40, 96, 1152, 4, "ampere"),
        InstanceSpec("p4de.24xlarge", "A100", 8, 80, 96, 1152, 4, "ampere"),
        InstanceSpec("p5.4xlarge", "H100", 1, 80, 16, 256, 1, "hopper"),
        InstanceSpec("p5.48xlarge", "H100", 8, 80, 192, 2048, 32, "hopper"),
        InstanceSpec("p5e.48xlarge", "H200", 8, 141, 192, 2048, 32, "hopper"),
        InstanceSpec("p5en.48xlarge", "H200", 8, 141, 192, 2048, 16, "hopper"),
        InstanceSpec("p6-b200.48xlarge", "B200", 8, 179, 192, 2048, 8, "blackwell"),
        InstanceSpec("p6-b300.48xlarge", "B300", 8, 268, 192, 4096, 16, "blackwell"),
    ]
}

# Pricing API usage-type region prefixes
_USAGE_PREFIX = {
    "us-east-1": "USE1",
    "us-east-2": "USE2",
    "us-west-2": "USW2",
    "us-west-1": "USW1",
    "eu-west-1": "EU",
    "eu-central-1": "EUC1",
    "eu-north-1": "EUN1",
    "ap-northeast-1": "APN1",
    "ap-southeast-2": "APS2",
    "ap-south-1": "APS3",
}

_CACHE_TTL_S = 3600
_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}


def _cached(key: str, loader):
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < _CACHE_TTL_S:
            return hit[1]
    value = loader()
    with _lock:
        _cache[key] = (time.time(), value)
    return value


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def spec(instance_type: str) -> InstanceSpec:
    t = instance_type.removeprefix("ml.")
    if t not in CATALOG:
        from ..core.errors import AppError

        raise AppError(
            "catalog.unsupported_instance",
            f"{instance_type} is not a supported P-family instance type",
            detail={"supported": sorted(CATALOG)},
        )
    return CATALOG[t]


def on_demand_price(region: str, instance_type: str) -> float | None:
    """USD/hour for a HyperPod cluster instance, or None when not listed."""
    prefix = _USAGE_PREFIX.get(region)
    if not prefix:
        return None
    ml = f"ml.{instance_type.removeprefix('ml.')}"

    def load():
        import json

        pricing = aws.client("pricing", "us-east-1")  # Pricing API lives in us-east-1
        resp = pricing.get_products(
            ServiceCode="AmazonSageMaker",
            Filters=[
                {"Type": "TERM_MATCH", "Field": "usagetype", "Value": f"{prefix}-Cluster:{ml}"}
            ],
            MaxResults=1,
        )
        for raw in resp.get("PriceList", []):
            item = json.loads(raw) if isinstance(raw, str) else raw
            for term in item.get("terms", {}).get("OnDemand", {}).values():
                for dim in term.get("priceDimensions", {}).values():
                    usd = dim.get("pricePerUnit", {}).get("USD")
                    if usd is not None:
                        return float(usd)
        return None

    return _cached(f"price:{region}:{ml}", load)


def ec2_on_demand_price(region: str, instance_type: str) -> float | None:
    """USD/hour for a plain EC2 Linux instance (EKS managed node group)."""
    t = instance_type.removeprefix("ml.")

    def load():
        import json

        pricing = aws.client("pricing", "us-east-1")
        flt = {
            "instanceType": t,
            "regionCode": region,
            "operatingSystem": "Linux",
            "tenancy": "Shared",
            "preInstalledSw": "NA",
            "capacitystatus": "Used",
            "licenseModel": "No License required",
        }
        resp = pricing.get_products(
            ServiceCode="AmazonEC2",
            Filters=[{"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in flt.items()],
            MaxResults=5,
        )
        for raw in resp.get("PriceList", []):
            item = json.loads(raw) if isinstance(raw, str) else raw
            for term in item.get("terms", {}).get("OnDemand", {}).values():
                for dim in term.get("priceDimensions", {}).values():
                    usd = dim.get("pricePerUnit", {}).get("USD")
                    if usd is not None and float(usd) > 0:
                        return float(usd)
        return None

    return _cached(f"ec2price:{region}:{t}", load)


def ec2_spot_price(region: str, instance_type: str) -> float | None:
    """Current lowest Linux Spot price across the region's AZs (advisory; it moves)."""
    t = instance_type.removeprefix("ml.")

    def load():
        from datetime import UTC, datetime

        resp = aws.client("ec2", region).describe_spot_price_history(
            InstanceTypes=[t],
            ProductDescriptions=["Linux/UNIX"],
            StartTime=datetime.now(UTC),
            MaxResults=50,
        )
        prices = [float(p["SpotPrice"]) for p in resp.get("SpotPriceHistory", [])]
        return min(prices) if prices else None

    with _lock:  # Spot prices move: cache for 10 minutes only
        hit = _cache.get(f"spot:{region}:{t}")
        if hit and time.time() - hit[0] < 600:
            return hit[1]  # type: ignore[return-value]
    value = load()
    with _lock:
        _cache[f"spot:{region}:{t}"] = (time.time(), value)
    return value


EC2_QUOTAS = {"on_demand": "L-417A185B", "spot": "L-7212CCBC"}  # P-family vCPUs


def ec2_quotas(region: str) -> dict[str, float | None]:
    def load():
        sq = aws.client("service-quotas", region)
        out: dict[str, float | None] = {}
        for k, code in EC2_QUOTAS.items():
            try:
                out[k] = float(
                    sq.get_service_quota(ServiceCode="ec2", QuotaCode=code)["Quota"]["Value"]
                )
            except Exception:
                out[k] = None
        return out

    return _cached(f"ec2quotas:{region}", load)


def cluster_quotas(region: str) -> dict[str, float]:
    """All SageMaker quotas whose name mentions HyperPod/cluster usage, by name."""

    def load():
        sq = aws.client("service-quotas", region)
        out: dict[str, float] = {}
        token = None
        while True:
            kwargs = {"ServiceCode": "sagemaker", "MaxResults": 100}
            if token:
                kwargs["NextToken"] = token
            resp = sq.list_service_quotas(**kwargs)
            for q in resp.get("Quotas", []):
                name = q.get("QuotaName", "")
                if "cluster" in name.lower() or "hyperpod" in name.lower():
                    out[name] = float(q.get("Value", 0))
            token = resp.get("NextToken")
            if not token:
                return out

    return _cached(f"quotas:{region}", load)


def quota_for(quotas: dict[str, float], instance_type: str) -> dict[str, float | None]:
    ml = f"ml.{instance_type.removeprefix('ml.')}"
    return {
        "on_demand": quotas.get(f"{ml} for cluster usage"),
        "spot": quotas.get(f"{ml} for cluster spot instance usage"),
    }


def catalog_view(region: str, *, live: bool = True) -> dict:
    quotas: dict[str, float] = {}
    errors: list[str] = []
    if live:
        try:
            quotas = cluster_quotas(region)
        except Exception as e:  # quota visibility is advisory
            errors.append(f"quotas: {type(e).__name__}: {e}")
    rows = []
    for s in CATALOG.values():
        price = ec2_price = None
        if live:
            try:
                price = on_demand_price(region, s.type)
            except Exception as e:
                errors.append(f"price {s.type}: {type(e).__name__}")
            try:
                ec2_price = ec2_on_demand_price(region, s.type)
            except Exception as e:
                errors.append(f"ec2 price {s.type}: {type(e).__name__}")
        rows.append(
            {
                **asdict(s),
                "ml_type": s.ml_type,
                "multi_node": s.multi_node,
                "price_per_hour": price,
                "ec2_price_per_hour": ec2_price,
                "quota": quota_for(quotas, s.type),
            }
        )
    limits = {
        "per_cluster": quotas.get(
            "Maximum number instances allowed per SageMaker HyperPod cluster"
        ),
        "total": quotas.get("Total number of instances allowed across SageMaker HyperPod clusters"),
        "spot_total": quotas.get(
            "Total number of Spot instances allowed across SageMaker HyperPod clusters"
        ),
    }
    ec2 = {}
    if live:
        try:
            ec2 = ec2_quotas(region)
        except Exception as e:
            errors.append(f"ec2 quotas: {type(e).__name__}")
    limits["ec2_p_vcpus"] = ec2
    return {"region": region, "instances": rows, "limits": limits, "errors": errors}
