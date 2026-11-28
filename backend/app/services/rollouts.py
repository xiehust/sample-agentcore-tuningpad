"""Run agent tasks on an AgentCore runtime outside training (smoke tests, evals).

Toolkit v0.2 contract: the task payload carries `_config` (base_url, model_id, api_key,
sampling_params); the agent's `AgentCoreRuntimeApp` runs it in the background and
`AgentCoreHttpClient` polls `start/get` until the agent's JSON result is ready. Each
task gets its own runtime session, which `AgentCoreHttpSession` stops on success,
failure and timeout.

Records: {index, session_id, success, failure, result, error, elapsed}. `failure` is
"agent" when the agent itself raised (or broke the result contract) and "invoke" when
the result could not be obtained (InvokeAgentRuntime error, timeout).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from typing import Any

from . import toolkit_source

Record = dict[str, Any]


def http_client(runtime_arn: str, tps_limit: int):
    """Factory seam (tests patch this). The client opens its own aiobotocore session:
    the toolkit needs an async client, which `core.aws` (boto3) does not provide."""
    toolkit_source.validate_installation()
    from agentcore_rl_toolkit import AgentCoreHttpClient
    from agentcore_rl_toolkit.concurrency.rate_limiter import LocalRateLimiter

    # Shared by start, get (poll) and stop requests: InvokeAgentRuntime is TPS-limited.
    return AgentCoreHttpClient(runtime_arn, request_rate_limiter=LocalRateLimiter(tps_limit))


def with_config(
    payload: dict[str, Any],
    *,
    base_url: str,
    model_id: str,
    api_key: str,
    sampling_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The task the agent receives: dataset payload plus the model configuration."""
    return {
        **payload,
        "_config": {
            "base_url": base_url,
            "model_id": model_id,
            "api_key": api_key,
            "sampling_params": dict(sampling_params or {}),
        },
    }


async def _run_one(client, index: int, task: dict[str, Any], timeout_s: float) -> Record:
    from agentcore_rl_toolkit.rollout_session.agentcore_http_session import AgentCoreHttpSession
    from agentcore_rl_toolkit.rollout_session.errors import RolloutContractError

    sid = str(uuid.uuid4())
    rec: Record = {"index": index, "session_id": sid, "success": False, "failure": None}
    t0 = time.monotonic()
    try:
        async with AgentCoreHttpSession.with_client(sid, client) as session:
            async with asyncio.timeout(timeout_s):
                dump = await session.run(task)
        if dump.exception is not None:
            rec.update(failure="agent", error=dump.exception)
        else:
            result = {k: v for k, v in (dump.task_output or {}).items() if k != "payload"}
            rec.update(success=True, result=result)
    except RolloutContractError as e:
        rec.update(failure="agent", error=f"{type(e).__name__}: {e}")
    except TimeoutError:
        rec.update(failure="invoke", error=f"TimeoutError: no result after {timeout_s:.0f}s")
    except Exception as e:
        rec.update(failure="invoke", error=f"{type(e).__name__}: {e}")
    rec["elapsed"] = round(time.monotonic() - t0, 3)
    return rec


async def _run_all(
    runtime_arn: str,
    tasks: list[dict[str, Any]],
    *,
    max_concurrent: int,
    timeout_s: float,
    tps_limit: int,
    on_result: Callable[[Record], None],
) -> list[Record]:
    client = http_client(runtime_arn, tps_limit)
    gate = asyncio.Semaphore(max_concurrent)
    out: list[Record] = []

    async def guarded(i: int, task: dict[str, Any]) -> Record:
        async with gate:
            return await _run_one(client, i, task, timeout_s)

    # Plain tasks, not a TaskGroup: an exception from on_result (job cancelled) must
    # propagate as itself, not wrapped in an ExceptionGroup.
    futures = [asyncio.ensure_future(guarded(i, t)) for i, t in enumerate(tasks)]
    try:
        for fut in asyncio.as_completed(futures):
            rec = await fut
            out.append(rec)
            on_result(rec)
    finally:
        for f in futures:
            f.cancel()  # each cancelled session still stops its runtime session on exit
        await asyncio.gather(*futures, return_exceptions=True)
        await client.aclose()
    return sorted(out, key=lambda r: r["index"])


def run_batch(
    runtime_arn: str,
    tasks: list[dict[str, Any]],
    *,
    max_concurrent: int = 32,
    timeout_s: float = 1800,
    tps_limit: int = 8,
    on_result: Callable[[Record], None] | None = None,
) -> list[Record]:
    """Run every task in its own runtime session; blocks the calling (job) thread.

    `on_result` sees each record as it completes; an exception it raises stops the batch
    and the remaining sessions are stopped on the way out.
    """
    return asyncio.run(
        _run_all(
            runtime_arn,
            tasks,
            max_concurrent=max_concurrent,
            timeout_s=timeout_s,
            tps_limit=tps_limit,
            on_result=on_result or (lambda _rec: None),
        )
    )
