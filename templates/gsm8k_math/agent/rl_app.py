"""GSM8K calculator agent (examples/strands_math_agent/rl_app.py) with the
operator-editable knobs read from /app/tp_config.json, written by TuningPad at
build time. Contract: `_rollout.{base_url, model_id, api_key, sampling_params}`
in, `{"rewards": float}` out."""

import json
import logging
from pathlib import Path

from models import InvocationRequest
from reward import GSM8KReward
from strands import Agent
from strands.agent.conversation_manager import NullConversationManager
from strands.models.openai import OpenAIModel
from strands_tools import calculator

from agentcore_rl_toolkit import AgentCoreRLApp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CONFIG = json.loads((Path(__file__).parent / "tp_config.json").read_text())
SYSTEM_PROMPT = CONFIG["system_prompt"]
REWARD_METHOD = CONFIG.get("reward_method", "strict")
FORMAT_SCORE = float(CONFIG.get("format_score", 0.0))

app = AgentCoreRLApp()
reward_fn = GSM8KReward()


@app.rollout_entrypoint
def invoke_agent(payload: dict, context):
    rollout = payload["_rollout"]
    model = OpenAIModel(
        # The gateway keys trajectory capture off the api-key slot: always forward it.
        client_args={"api_key": rollout.get("api_key") or "EMPTY", "base_url": rollout["base_url"]},
        model_id=rollout["model_id"],
        params=rollout.get("sampling_params", {}),
    )
    agent = Agent(
        model=model,
        tools=[calculator],
        system_prompt=SYSTEM_PROMPT,
        conversation_manager=NullConversationManager(),
    )
    request = InvocationRequest(**payload)  # prompt must be a str (no toolUse injection)
    response = agent(request.prompt)
    content = response.message.get("content") or []
    text = "".join(block["text"] for block in content if "text" in block)
    rewards = reward_fn(
        response_text=text, ground_truth=request.answer, method=REWARD_METHOD,
        format_score=FORMAT_SCORE,
    )
    return {"rewards": rewards}


if __name__ == "__main__":
    app.run()
