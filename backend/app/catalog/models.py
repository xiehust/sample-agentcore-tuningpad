"""Model catalog + rollout-gateway compatibility check.

The gateway parses tool calls only for chat templates whose sha256 is in the
toolkit's registry (`rollout_gateway.response_schemas._TEMPLATE_HASHES`). We
fetch the template the way transformers loads it (chat_template.jinja wins over
tokenizer_config.json) plus config.json and parameter counts — never weights.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, dataclass, field
from typing import Any

from ..core.errors import AppError

# Tool-call parser / reasoning parser per schema, for vLLM serving.
VLLM_PARSERS: dict[str, dict[str, str | None]] = {
    "qwen3": {"tool_call_parser": "hermes", "reasoning_parser": "qwen3"},
    "qwen3_5": {"tool_call_parser": "qwen3_coder", "reasoning_parser": "qwen3"},
    "glm4moe": {"tool_call_parser": "glm45", "reasoning_parser": "glm45"},
    "gptoss": {"tool_call_parser": "openai", "reasoning_parser": "openai_gptoss"},
}

# Presets shown in the UI. `verified` = a training recipe in the toolkit repo ran it.
PRESETS: list[dict[str, Any]] = [
    {"id": "Qwen/Qwen3.5-2B", "verified": "verl FSDP, 1 & 8 GPU (experiments/qwen35_2b_gsm8k)"},
    {"id": "Qwen/Qwen3-4B-Instruct-2507", "verified": "verl FSDP full/LoRA, Megatron LoRA (math)"},
    {"id": "Qwen/Qwen3.5-4B", "verified": None},
    {"id": "Qwen/Qwen3.5-9B", "verified": None},
    {"id": "Qwen/Qwen3-8B", "verified": None},
    {"id": "Qwen/Qwen3.6-27B", "verified": "verl Megatron LoRA TP4 CP2 (officebench, migration)"},
    {"id": "Qwen/Qwen3-Coder-30B-A3B-Instruct", "verified": "verl Megatron LoRA (swe, migration)"},
    {"id": "openai/gpt-oss-20b", "verified": None},
    {"id": "zai-org/GLM-4.5-Air", "verified": None},
]


@dataclass
class ModelCheck:
    model_id: str
    compatible: bool
    schema: str | None
    reason: str | None
    template_source: str | None
    params: int | None
    params_b: float | None
    is_moe: bool
    num_experts: int | None
    active_params_b: float | None
    max_position_embeddings: int | None
    architectures: list[str] = field(default_factory=list)
    multimodal: bool = False
    gated: bool = False
    tool_call_parser: str | None = None
    reasoning_parser: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_lock = threading.Lock()
_cache: dict[str, ModelCheck] = {}


def _text_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Multimodal configs (Qwen3.5/3.6) nest the LM under text_config."""
    return cfg.get("text_config") or cfg.get("llm_config") or cfg


def _moe_info(cfg: dict[str, Any]) -> tuple[bool, int | None, int | None]:
    t = _text_config(cfg)
    experts = t.get("num_experts") or t.get("num_local_experts") or t.get("n_routed_experts")
    per_tok = t.get("num_experts_per_tok") or t.get("moe_topk")
    return bool(experts and experts > 1), experts, per_tok


def _estimate_active(params: int | None, experts: int | None, per_tok: int | None) -> float | None:
    """Very rough active-parameter estimate for MoE (expert FFNs dominate)."""
    if not params or not experts or not per_tok:
        return None
    expert_share = 0.9  # fraction of params in expert FFNs, typical for fine-grained MoE
    active = params * ((1 - expert_share) + expert_share * per_tok / experts)
    return round(active / 1e9, 2)


def schema_for_template(template: str | None) -> str | None:
    if not template:
        return None
    from agentcore_rl_toolkit.rollout_gateway.response_schemas import _TEMPLATE_HASHES

    return _TEMPLATE_HASHES.get(hashlib.sha256(template.encode("utf-8")).hexdigest())


def check_model(model_id: str, *, hf_token: str | None = None, refresh: bool = False) -> ModelCheck:
    model_id = model_id.strip()
    if not model_id or "/" not in model_id:
        raise AppError("model.invalid_id", "expected a Hugging Face id like 'Qwen/Qwen3.5-2B'")
    with _lock:
        if not refresh and model_id in _cache:
            return _cache[model_id]

    from huggingface_hub import HfApi, hf_hub_download
    from huggingface_hub.utils import GatedRepoError, RepositoryNotFoundError

    api = HfApi(token=hf_token)
    try:
        info = api.model_info(model_id)
    except RepositoryNotFoundError as e:
        raise AppError(
            "model.not_found", f"{model_id} not found on Hugging Face", status=404
        ) from e
    files = {s.rfilename for s in (info.siblings or [])}
    gated = bool(info.gated)
    params = info.safetensors.total if info.safetensors else None

    def fetch_json(name: str) -> dict[str, Any] | None:
        if name not in files:
            return None
        with open(hf_hub_download(model_id, name, token=hf_token)) as f:
            return json.load(f)

    template, source, reason = None, None, None
    cfg: dict[str, Any] = {}
    try:
        cfg = fetch_json("config.json") or {}
        if "chat_template.jinja" in files:
            with open(hf_hub_download(model_id, "chat_template.jinja", token=hf_token)) as f:
                template, source = f.read(), "chat_template.jinja"
        else:
            tc = fetch_json("tokenizer_config.json") or {}
            raw = tc.get("chat_template")
            if isinstance(raw, list):  # named templates: transformers uses "default"
                raw = {t.get("name"): t.get("template") for t in raw}.get("default")
            template, source = raw, "tokenizer_config.json" if raw else None
    except GatedRepoError:
        reason = "gated repository: accept the license on Hugging Face and provide a token"

    schema = schema_for_template(template)
    if reason is None and template is None:
        reason = "no chat template found"
    elif reason is None and schema is None:
        reason = (
            "chat template is not in the rollout gateway registry "
            "(supported: Qwen2.5/Qwen3/Qwen3.5/Qwen3.6/Qwen3-Coder/Nemotron-3, GLM4-MoE, GPT-OSS)"
        )
    is_moe, experts, per_tok = _moe_info(cfg)
    t = _text_config(cfg)
    parsers = VLLM_PARSERS.get(schema or "", {})
    result = ModelCheck(
        model_id=model_id,
        compatible=schema is not None,
        schema=schema,
        reason=None if schema else reason,
        template_source=source,
        params=params,
        params_b=round(params / 1e9, 2) if params else None,
        is_moe=is_moe,
        num_experts=experts,
        active_params_b=_estimate_active(params, experts, per_tok) if is_moe else None,
        max_position_embeddings=t.get("max_position_embeddings"),
        architectures=list(cfg.get("architectures") or []),
        multimodal="text_config" in cfg or "vision_config" in cfg,
        gated=gated,
        tool_call_parser=parsers.get("tool_call_parser"),
        reasoning_parser=parsers.get("reasoning_parser"),
    )
    with _lock:
        _cache[model_id] = result
    return result


def clear_cache() -> None:
    with _lock:
        _cache.clear()
