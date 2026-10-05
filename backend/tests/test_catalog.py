import hashlib
import json

import pytest

from app.catalog import instances, models
from app.core.errors import AppError
from tests.conftest import StubClient


@pytest.fixture(autouse=True)
def _clear():
    instances.clear_cache()
    models.clear_cache()


def test_training_catalog_is_p_family_only():
    assert all(t.startswith("p") for t in instances.CATALOG)
    assert instances.CATALOG["p5.48xlarge"].efa == 32
    assert not instances.CATALOG["p5.4xlarge"].multi_node
    with pytest.raises(AppError) as e:
        instances.spec("g6e.12xlarge")
    assert e.value.code == "catalog.unsupported_instance"
    assert instances.spec("ml.p5en.48xlarge").gpu == "H200"


def test_serving_types_are_inference_only():
    g = instances.spec("g5.2xlarge", serving=True)
    assert (g.gpu, g.gpus, g.training, g.multi_node) == ("A10G", 1, False, False)
    assert all(not s.training for s in instances.SERVING_CATALOG.values())
    assert not set(instances.SERVING_CATALOG) & set(instances.CATALOG)
    with pytest.raises(AppError):  # training paths keep rejecting G-family
        instances.spec("g5.2xlarge")
    view = instances.catalog_view("us-east-1", live=False)
    rows = {r["type"]: r for r in view["instances"]}
    assert rows["g5.2xlarge"]["training"] is False and rows["p5.48xlarge"]["training"] is True


def test_nodegroup_launch_template_accepts_serving_type():
    from app.services import nodegroups as ngs

    data = ngs.launch_template_data("g5.2xlarge", ["sg-1"], False, None)
    assert data["SecurityGroupIds"] == ["sg-1"]
    assert "NetworkInterfaces" not in data


def test_vllm_on_g5_sizes_shm_to_node_memory():
    from app.services import serving

    dep, _ = serving.vllm_manifests(
        endpoint_id="ep-1",
        model="/fsx/exports/ex-1",
        served_name="m",
        group="ec2-g5",
        instance_type="g5.2xlarge",
        tp=1,
        replicas=1,
        tool_parser=None,
        reasoning_parser=None,
        sg_nlb="sg-n",
        subnets=["s1"],
        max_model_len=8192,
    )
    vols = {v["name"]: v for v in dep["spec"]["template"]["spec"]["volumes"]}
    assert vols["dshm"]["emptyDir"]["sizeLimit"] == "8Gi"
    with pytest.raises(AppError) as e:
        serving.vllm_manifests(
            endpoint_id="ep-1",
            model="m",
            served_name="m",
            group="g",
            instance_type="g5.2xlarge",
            tp=2,
            replicas=1,
            tool_parser=None,
            reasoning_parser=None,
            sg_nlb="sg",
            subnets=[],
            max_model_len=None,
        )
    assert e.value.code == "inference.tp_too_large"


def test_catalog_view_live(stub_aws):
    price_item = json.dumps(
        {
            "terms": {
                "OnDemand": {"x": {"priceDimensions": {"y": {"pricePerUnit": {"USD": "66.048"}}}}}
            }
        }
    )
    stubs = stub_aws(
        {
            "pricing": StubClient(get_products=lambda **kw: {"PriceList": [price_item]}),
            "service-quotas": StubClient(
                list_service_quotas={
                    "Quotas": [
                        {"QuotaName": "ml.p5.48xlarge for cluster usage", "Value": 2.0},
                        {
                            "QuotaName": "ml.p5.48xlarge for cluster spot instance usage",
                            "Value": 0.0,
                        },
                        {
                            "QuotaName": "Maximum number instances allowed per SageMaker HyperPod cluster",
                            "Value": 20.0,
                        },
                        {"QuotaName": "unrelated", "Value": 1.0},
                    ]
                }
            ),
        }
    )
    view = instances.catalog_view("us-east-1")
    p5 = next(r for r in view["instances"] if r["type"] == "p5.48xlarge")
    assert p5["price_per_hour"] == 66.048
    assert p5["quota"] == {"on_demand": 2.0, "spot": 0.0}
    assert view["limits"]["per_cluster"] == 20.0
    usage = stubs["pricing"].calls[0][1]["Filters"][0]["Value"]
    assert usage == "USE1-Cluster:ml.p4d.24xlarge"


def test_unknown_region_has_no_price(stub_aws):
    assert instances.on_demand_price("xx-nowhere-1", "p5.48xlarge") is None


class _Sibling:
    def __init__(self, name):
        self.rfilename = name


class _Info:
    def __init__(self, files, total=2_000_000_000, gated=False):
        self.siblings = [_Sibling(f) for f in files]
        self.gated = gated

        class _ST:
            pass

        self.safetensors = _ST()
        self.safetensors.total = total


def _fake_hf(monkeypatch, tmp_path, files: dict[str, str], total=2_000_000_000):
    import huggingface_hub

    for name, content in files.items():
        (tmp_path / name).write_text(content)
    monkeypatch.setattr(
        huggingface_hub.HfApi, "model_info", lambda self, mid: _Info(list(files), total)
    )
    monkeypatch.setattr(
        huggingface_hub, "hf_hub_download", lambda mid, name, token=None: str(tmp_path / name)
    )


def test_check_model_compatible_via_registry(monkeypatch, tmp_path):
    template = "{% for m in messages %}{{ m.content }}{% endfor %}"
    digest = hashlib.sha256(template.encode()).hexdigest()
    from agentcore_rl_toolkit.rollout_gateway import response_schemas

    monkeypatch.setitem(response_schemas._TEMPLATE_HASHES, digest, "qwen3_5")
    cfg = {"architectures": ["X"], "text_config": {"max_position_embeddings": 262144}}
    _fake_hf(
        monkeypatch, tmp_path, {"chat_template.jinja": template, "config.json": json.dumps(cfg)}
    )
    r = models.check_model("org/model-2b")
    assert r.compatible and r.schema == "qwen3_5"
    assert r.template_source == "chat_template.jinja"
    assert r.multimodal and r.max_position_embeddings == 262144
    assert r.tool_call_parser == "qwen3_coder"
    assert r.params_b == 2.0


def test_check_model_incompatible_and_moe(monkeypatch, tmp_path):
    tc = {"chat_template": "unknown template"}
    cfg = {"num_local_experts": 32, "num_experts_per_tok": 4, "max_position_embeddings": 131072}
    _fake_hf(
        monkeypatch,
        tmp_path,
        {"tokenizer_config.json": json.dumps(tc), "config.json": json.dumps(cfg)},
        total=20_000_000_000,
    )
    r = models.check_model("org/moe")
    assert not r.compatible and "registry" in r.reason
    assert r.is_moe and r.num_experts == 32
    assert 2 < r.active_params_b < 5


def test_check_model_invalid_id():
    with pytest.raises(AppError):
        models.check_model("no-slash")


def test_plan_endpoint(client, monkeypatch):
    fake = models.ModelCheck(
        model_id="Qwen/Qwen3.6-27B",
        compatible=True,
        schema="qwen3_5",
        reason=None,
        template_source="chat_template.jinja",
        params=27_800_000_000,
        params_b=27.8,
        is_moe=False,
        num_experts=None,
        active_params_b=None,
        max_position_embeddings=262144,
    )
    monkeypatch.setattr(models, "check_model", lambda mid, refresh=False: fake)
    r = client.post(
        "/api/catalog/plan",
        json={
            "model_id": "Qwen/Qwen3.6-27B",
            "instance_type": "p5.48xlarge",
            "max_model_len": 131072,
        },
    )
    body = r.json()
    assert r.status_code == 200
    assert body["plan"]["strategy"] == "megatron_lora" and body["plan"]["tp"] == 4
