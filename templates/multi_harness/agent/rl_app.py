"""multi_harness Agent entrypoint (toolkit v0.2 contract).

payload: task_id, task_uri, input_uri, optional harness, and `_config` (base_url, model_id,
api_key = the gateway session id). Returns {"reward": float | None, ...}; None means the
Verifier produced no result, which the trainer treats as unscorable rather than wrong.
"""

import json
import logging
import os
from pathlib import Path

import boto3
from agentcore_rl_toolkit import AgentCoreRuntimeApp
from harness_agent import rollout

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = AgentCoreRuntimeApp()
PARAMS = json.loads((Path(__file__).parent / "tp_config.json").read_text())


def _split(uri: str) -> tuple[str, str]:
    bucket, _, key = uri.removeprefix("s3://").partition("/")
    return bucket, key


@app.entrypoint
def invoke_agent(payload: dict, context):
    s3 = boto3.client("s3")

    def read(uri: str) -> bytes:
        bucket, key = _split(uri)
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read()

    def fetch_tree(uri: str, dest: Path) -> None:
        bucket, prefix = _split(uri)
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                rel = obj["Key"][len(prefix) :]
                if not rel or rel.endswith("/"):
                    continue
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                s3.download_file(bucket, obj["Key"], str(target))

    result = rollout.run_rollout(
        payload,
        params=PARAMS,
        paths=rollout.CONTAINER_PATHS,
        read=read,
        fetch_tree=fetch_tree,
        run=rollout.run_process,
        region=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
    )
    logger.info(
        "task %s: reward=%s %s", payload.get("task_id"), result["reward"], result["metrics"]
    )
    return result


if __name__ == "__main__":
    app.run()
