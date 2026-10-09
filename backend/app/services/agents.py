"""Agent images and AgentCore runtimes.

Build:   assemble a docker context (template overlay / uploaded zip) + toolkit wheel
         → static contract checks → `docker buildx --platform linux/arm64` (the
         platform host is arm64) → local /ping probe → push to ECR.
Import:  validate an existing ECR image (arm64, ≤ 2 GB).
Deploy:  create/update an AgentCore runtime (VPC mode in the cluster's private
         subnets with sg-acr; PUBLIC only for a standalone smoke runtime).
Smoke:   RolloutClient → InvokeAgentRuntime against Bedrock's OpenAI-compatible
         endpoint (short-lived token); pass = status_code 200 + numeric rewards.
"""

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

from ..core import aws
from ..core.config import get_settings
from ..core.errors import AppError
from ..templates_lib import get_template, resolve_params, template_dir

ACR_IMAGE_LIMIT_BYTES = 2 * 1024**3
SMOKE_MODEL = "openai.gpt-oss-20b-1:0"
RUNTIME_NAME_RE = re.compile(r"[^a-zA-Z0-9_]")


def build_dir(agent_id: str) -> Path:
    d = get_settings().data_dir / "builds" / agent_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def uploads_dir() -> Path:
    d = get_settings().data_dir / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def runtime_name(agent_name: str, suffix: str) -> str:
    base = RUNTIME_NAME_RE.sub("_", f"tp_{agent_name}_{suffix}")
    return base[:48]


# ---------------- context ----------------


def toolkit_wheel(dest: Path) -> Path:
    """Build the toolkit wheel from the configured checkout into dest/dist."""
    tk = get_settings().toolkit_path
    if not (tk / "pyproject.toml").is_file():
        raise AppError("agent.toolkit_missing", f"agentcore-rl-toolkit not found at {tk}")
    dist = dest / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    for old in dist.glob("*.whl"):
        old.unlink()
    proc = subprocess.run(
        ["uv", "build", "--wheel", "-o", str(dist)],
        cwd=tk,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        raise AppError("agent.wheel_failed", "uv build --wheel failed", detail=proc.stderr[-3000:])
    wheels = list(dist.glob("agentcore_rl_toolkit-*.whl"))
    if not wheels:
        raise AppError("agent.wheel_failed", "toolkit wheel not produced")
    return wheels[0]


def safe_extract_zip(zip_path: Path, dest: Path, max_bytes: int = 200 * 1024**2) -> None:
    """Extract an uploaded agent zip without path traversal or zip bombs."""
    total = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename
            if name.startswith("/") or ".." in Path(name).parts:
                raise AppError("agent.bad_zip", f"unsafe path in zip: {name}")
            total += info.file_size
            if total > max_bytes:
                raise AppError("agent.bad_zip", "zip expands beyond 200 MB")
        zf.extractall(dest)
    # a single top-level folder is unwrapped
    entries = [p for p in dest.iterdir() if p.name != "dist"]
    if len(entries) == 1 and entries[0].is_dir() and not (dest / "Dockerfile").exists():
        inner = entries[0]
        for p in inner.iterdir():
            shutil.move(str(p), dest / p.name)
        inner.rmdir()


def prepare_context(agent: dict[str, Any]) -> Path:
    ctx = build_dir(agent["id"]) / "context"
    if ctx.exists():
        shutil.rmtree(ctx)
    ctx.mkdir(parents=True)
    if agent["source"] == "template":
        t = get_template(agent["template_id"])
        tk = get_settings().toolkit_path / t["toolkit_dir"]
        for f in t.get("toolkit_files", []):
            shutil.copy2(tk / f, ctx / f)
        for f in (template_dir(agent["template_id"]) / "agent").iterdir():
            shutil.copy2(f, ctx / f.name)
        params = resolve_params(agent["template_id"], agent["config"].get("params"))
        (ctx / "tp_config.json").write_text(json.dumps(params, indent=2))
    elif agent["source"] == "upload":
        zip_path = uploads_dir() / f"{agent['id']}.zip"
        if not zip_path.exists():
            raise AppError("agent.upload_missing", "uploaded agent zip not found")
        safe_extract_zip(zip_path, ctx)
    else:
        raise AppError("agent.no_context", "image agents are not built")
    toolkit_wheel(ctx)
    return ctx


def static_checks(ctx: Path) -> dict[str, Any]:
    """Contract heuristics for the verl backend; `errors` block the build."""
    errors: list[str] = []
    warnings: list[str] = []
    if not (ctx / "Dockerfile").is_file():
        errors.append("Dockerfile missing at the root of the agent folder")
    sources = "\n".join(
        p.read_text(errors="ignore") for p in ctx.rglob("*.py") if "dist" not in p.parts
    )
    if "AgentCoreRLApp" not in sources:
        errors.append("no AgentCoreRLApp found: the entrypoint must use agentcore_rl_toolkit")
    if "rollout_entrypoint" not in sources:
        errors.append("no @app.rollout_entrypoint found")
    if "_rollout" not in sources:
        warnings.append("payload['_rollout'] is never read: base_url/model_id come from it")
    if "api_key" not in sources:
        warnings.append(
            "_rollout.api_key is not forwarded to the model client; the gateway "
            "cannot attribute tokens and training will fail"
        )
    if '"rewards"' not in sources and "'rewards'" not in sources:
        warnings.append("no 'rewards' key returned; verl scores such rollouts 0")
    dockerfile = (ctx / "Dockerfile").read_text() if (ctx / "Dockerfile").is_file() else ""
    if "8080" not in dockerfile:
        warnings.append("Dockerfile does not EXPOSE 8080 (the AgentCore HTTP contract port)")
    for p in sorted(ctx.rglob("*.py")):
        if "dist" in p.parts:
            continue
        for lineno, call in snapshot_unsafe_calls(p.read_text(errors="ignore")):
            warnings.append(
                f"{p.relative_to(ctx)}:{lineno}: {call}() runs at import time. On AgentCore "
                "Runtime V2 import-time state is captured once in a snapshot and shared by "
                "every session; move it into the rollout handler (or deploy with V1)"
            )
    return {"errors": errors, "warnings": warnings}


# Values that must differ per session or can expire: computing them at import time is
# wrong on Runtime V2, where every instance restores the same post-startup snapshot.
_SNAPSHOT_UNSAFE = {
    ("uuid", "uuid1"),
    ("uuid", "uuid4"),
    ("os", "urandom"),
    ("os", "getpid"),
    ("socket", "gethostname"),
    ("time", "time"),
    ("time", "monotonic"),
    ("datetime", "now"),
    ("datetime", "utcnow"),
    ("random", "*"),
    ("secrets", "*"),
}


def snapshot_unsafe_calls(source: str) -> list[tuple[int, str]]:
    """Module-level (import-time) calls such as uuid.uuid4() or random.random(); code in
    function and class bodies runs per request and is not reported."""
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found: list[tuple[int, str]] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            return  # deferred: runs per call, not at import
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            base = node.func.value
            # module.fn() or module.Class.fn() (datetime.datetime.now)
            root = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", None)
            name = node.func.attr
            if root and ((root, name) in _SNAPSHOT_UNSAFE or (root, "*") in _SNAPSHOT_UNSAFE):
                found.append((node.lineno, f"{root}.{name}"))
        for child in ast.iter_child_nodes(node):
            visit(child)

    for stmt in tree.body:
        if isinstance(stmt, ast.If) and "__main__" in ast.unparse(stmt.test):
            continue  # local-run guard, not executed when the runtime imports the app
        visit(stmt)
    return found


# ---------------- docker ----------------


def _run(
    cmd: list[str], log, timeout: int = 3600, input_text: str | None = None, cwd: Path | None = None
) -> str:
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        stdin=subprocess.PIPE if input_text else None,
        cwd=cwd,
    )
    if input_text:
        assert proc.stdin
        proc.stdin.write(input_text)
        proc.stdin.close()
    out_lines: list[str] = []
    assert proc.stdout
    start = time.monotonic()
    for line in proc.stdout:
        out_lines.append(line)
        log(line.rstrip())
        if time.monotonic() - start > timeout:
            proc.kill()
            raise AppError("agent.timeout", f"{cmd[0]} timed out")
    rc = proc.wait()
    if rc != 0:
        raise AppError(
            "agent.command_failed",
            f"{' '.join(cmd[:3])} exited {rc}",
            detail="".join(out_lines[-40:]),
        )
    return "".join(out_lines)


def docker_build(ctx: Path, tag: str, log) -> None:
    if not shutil.which("docker"):
        raise AppError("agent.no_docker", "docker is required to build agent images")
    _run(
        ["docker", "buildx", "build", "--platform", "linux/arm64", "--load", "-t", tag, "."],
        log,
        cwd=ctx,
        timeout=3600,
    )


def ecr_login(region: str, log) -> str:
    token = aws.client("ecr", region).get_authorization_token()["authorizationData"][0]
    import base64

    user, password = base64.b64decode(token["authorizationToken"]).decode().split(":", 1)
    registry = token["proxyEndpoint"].removeprefix("https://")
    _run(
        ["docker", "login", "--username", user, "--password-stdin", registry],
        log,
        input_text=password,
    )
    return registry


def docker_push(local_tag: str, remote: str, region: str, log) -> None:
    ecr_login(region, log)
    _run(["docker", "tag", local_tag, remote], log)
    _run(["docker", "push", remote], log, timeout=3600)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def local_probe(tag: str, log, timeout_s: int = 90) -> dict[str, Any]:
    """Run the image locally and require GET /ping == 200 (the ACR health contract)."""
    port = _free_port()
    name = f"tp-probe-{port}"
    _run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:8080",
            "-e",
            "AWS_REGION=us-east-1",
            tag,
        ],
        log,
    )
    try:
        end = time.monotonic() + timeout_s
        last = None
        while time.monotonic() < end:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=3) as r:
                    body = r.read().decode()[:200]
                    if r.status == 200:
                        return {"ping": 200, "body": body}
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
            time.sleep(2)
        logs = subprocess.run(
            ["docker", "logs", "--tail", "60", name], capture_output=True, text=True
        ).stdout
        raise AppError("agent.probe_failed", f"/ping never returned 200 ({last})", detail=logs)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


# ---------------- ECR image inspection ----------------


def parse_image_uri(uri: str) -> tuple[str, str, str, str]:
    m = re.match(r"^(\d{12})\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com/([^:@]+)[:@](.+)$", uri)
    if not m:
        raise AppError(
            "agent.bad_image_uri", "expected <account>.dkr.ecr.<region>.amazonaws.com/<repo>:<tag>"
        )
    return m.group(1), m.group(2), m.group(3), m.group(4)


def inspect_image(uri: str) -> dict[str, Any]:
    account, region, repo, ref = parse_image_uri(uri)
    ecr = aws.client("ecr", region)
    image_id = {"imageDigest": ref} if ref.startswith("sha256:") else {"imageTag": ref}
    try:
        desc = ecr.describe_images(registryId=account, repositoryName=repo, imageIds=[image_id])[
            "imageDetails"
        ][0]
    except ClientError as e:
        raise AppError("agent.image_not_found", f"{uri}: {e.response['Error']['Message']}") from e
    size = desc.get("imageSizeInBytes", 0)
    manifest_type = desc.get("imageManifestMediaType", "")
    arches: list[str] = []
    got = ecr.batch_get_image(
        registryId=account,
        repositoryName=repo,
        imageIds=[image_id],
        acceptedMediaTypes=[manifest_type] if manifest_type else None,
    )
    manifest = json.loads(got["images"][0]["imageManifest"]) if got.get("images") else {}
    if "manifests" in manifest:  # index / manifest list
        arches = [
            m.get("platform", {}).get("architecture")
            for m in manifest["manifests"]
            if m.get("platform", {}).get("architecture") not in (None, "unknown")
        ]
    elif manifest.get("config", {}).get("digest"):
        url = ecr.get_download_url_for_layer(
            registryId=account, repositoryName=repo, layerDigest=manifest["config"]["digest"]
        )["downloadUrl"]
        with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310 - presigned ECR URL
            arches = [json.loads(r.read()).get("architecture")]
    result = {
        "uri": uri,
        "region": region,
        "size_bytes": size,
        "architectures": arches,
        "digest": desc.get("imageDigest"),
    }
    if "arm64" not in arches:
        raise AppError(
            "agent.image_not_arm64",
            f"AgentCore Runtime needs linux/arm64; image is {arches or 'unknown'}",
            detail=result,
        )
    if size > ACR_IMAGE_LIMIT_BYTES:
        raise AppError(
            "agent.image_too_large",
            f"image is {size / 1024**3:.2f} GB; AgentCore Runtime limit is 2 GB",
            detail=result,
        )
    return result


# ---------------- runtime ----------------


def network_config(
    mode: str, subnets: list[str] | None = None, security_groups: list[str] | None = None
) -> dict[str, Any]:
    if mode == "PUBLIC":
        return {"networkMode": "PUBLIC"}
    if not subnets or not security_groups:
        raise AppError("agent.vpc_incomplete", "VPC mode needs cluster subnets and sg-acr")
    return {
        "networkMode": "VPC",
        "networkModeConfig": {"subnets": subnets, "securityGroups": security_groups},
    }


def find_runtime(region: str, name: str) -> dict[str, Any] | None:
    ctl = aws.client("bedrock-agentcore-control", region)
    token = None
    while True:
        kwargs: dict[str, Any] = {"maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        resp = ctl.list_agent_runtimes(**kwargs)
        for r in resp.get("agentRuntimes", []):
            if r.get("agentRuntimeName") == name:
                return r
        token = resp.get("nextToken")
        if not token:
            return None


# AgentCore Runtime platform versions: V1 initializes the container on every cold start;
# V2 restores a snapshot taken after the first healthy /ping (consistent cold starts,
# pay-for-use memory) but create/update take minutes. V2 is offered in these regions only.
PLATFORM_VERSIONS = ("V1", "V2")
PLATFORM_CHOICES = ("auto", *PLATFORM_VERSIONS)
V2_REGIONS = frozenset({"us-east-1", "us-east-2", "us-west-2", "eu-west-1", "ap-northeast-1"})
# terminal-state wait for create/update; V2 prepares a snapshot first
READY_TIMEOUT_S = {"V1": 900, "V2": 1800}


def resolve_platform_version(region: str, requested: str | None) -> str:
    """`auto` (or empty) → V2 where offered, else V1. An explicit V2 must be offered."""
    choice = requested or get_settings().agent_platform_version
    if choice not in PLATFORM_CHOICES:
        raise AppError(
            "agent.bad_platform_version",
            f"platform version must be one of {', '.join(PLATFORM_CHOICES)}",
        )
    if choice == "auto":
        return "V2" if region in V2_REGIONS else "V1"
    if choice == "V2" and region not in V2_REGIONS:
        raise AppError(
            "agent.platform_unsupported_region",
            f"AgentCore Runtime V2 is not offered in {region}",
            detail={"region": region, "supported": sorted(V2_REGIONS)},
        )
    return choice


def obs_runtime_name(base: str) -> str:
    """The eval-only OTEL runtime next to a training runtime (`<base>_obs`, ≤ 48 chars)."""
    return f"{base[:44]}_obs"


def obs_environment(name: str) -> dict[str, str]:
    """Runtime env of the eval-only runtime. The template entrypoint (tp_entry.sh) starts
    `opentelemetry-instrument` only when TP_OBSERVABILITY=1; training runtimes never get
    these, so their process is unchanged. ADOT fills in the OTLP exporters/endpoints from
    AGENT_OBSERVABILITY_ENABLED; content stays on the spans (not a separate logs pipeline)
    so the trace view reads one source."""
    return {
        "TP_OBSERVABILITY": "1",
        "AGENT_OBSERVABILITY_ENABLED": "true",
        "AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT": "true",
        # split delivery (aws/spans): unified delivery, the default for runtimes created after
        # 2026-07-20, needs logs:PutResourcePolicy on the agent execution role
        "UNIFIED_TRACES_DESTINATION_ENABLED": "false",
        "OTEL_PYTHON_DISTRO": "aws_distro",
        "OTEL_PYTHON_CONFIGURATOR": "aws_configurator",
        "OTEL_RESOURCE_ATTRIBUTES": f"service.name={name}",
    }


def deploy_runtime(
    region: str,
    name: str,
    image_uri: str,
    role_arn: str,
    network: dict[str, Any],
    runtime_id: str | None = None,
    platform_version: str = "V1",
    environment: dict[str, str] | None = None,
) -> dict[str, str]:
    ctl = aws.client("bedrock-agentcore-control", region)
    common = {
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": image_uri}},
        "roleArn": role_arn,
        "networkConfiguration": network,
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        # long agent rollouts: keep sessions alive up to the 8h maximum
        "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
        # always explicit: an omitted value on update keeps the runtime's current version
        "platformVersion": platform_version,
    }
    if environment:  # eval-only OTEL runtime; training runtimes pass none
        common["environmentVariables"] = environment
    existing = {"agentRuntimeId": runtime_id} if runtime_id else find_runtime(region, name)
    if existing:
        r = ctl.update_agent_runtime(agentRuntimeId=existing["agentRuntimeId"], **common)
    else:
        r = ctl.create_agent_runtime(agentRuntimeName=name, tags=aws.tag_map(), **common)
    return {"runtime_id": r["agentRuntimeId"], "runtime_arn": r["agentRuntimeArn"]}


def supported_az_ids(reason: str) -> list[str]:
    """Parse 'Supported availability zones are: use1-az4, use1-az1' from a failure reason."""
    m = re.search(r"Supported availability zones are:\s*([a-z0-9,\s-]+)", reason)
    return [z.strip() for z in m.group(1).split(",") if z.strip()] if m else []


def subnets_in_azs(region: str, subnets: list[str], az_ids: list[str]) -> list[str]:
    found = aws.client("ec2", region).describe_subnets(SubnetIds=subnets)["Subnets"]
    return [s["SubnetId"] for s in found if s["AvailabilityZoneId"] in az_ids]


def runtime_info(region: str, runtime_id: str) -> dict[str, Any]:
    """Status, failure reason and the platform version AgentCore actually applied
    (create/update responses do not return it)."""
    r = aws.client("bedrock-agentcore-control", region).get_agent_runtime(agentRuntimeId=runtime_id)
    return {
        "status": r["status"],
        "reason": r.get("failureReason"),
        "platform_version": r.get("platformVersion") or "V1",
    }


def runtime_status(region: str, runtime_id: str) -> tuple[str, str | None]:
    info = runtime_info(region, runtime_id)
    return info["status"], info["reason"]


def runtime_busy(status: str) -> bool:
    """CREATING / UPDATING / DELETING: update or delete now returns ConflictException."""
    return status.endswith("ING")


def delete_runtime(region: str, runtime_id: str) -> None:
    try:
        aws.client("bedrock-agentcore-control", region).delete_agent_runtime(
            agentRuntimeId=runtime_id
        )
    except ClientError as e:
        code, msg = e.response["Error"]["Code"], e.response["Error"].get("Message", "")
        if code == "ResourceNotFoundException":
            return
        if code == "ConflictException" and "DELETING" in msg:
            return  # a delete is already in flight
        raise


# ---------------- smoke ----------------


def rollout_client(**kwargs):
    """Factory seam (tests patch this); RolloutClient creates its own boto3 clients."""
    from agentcore_rl_toolkit import RolloutClient

    return RolloutClient(**kwargs)


def bedrock_openai(region: str) -> tuple[str, str]:
    from aws_bedrock_token_generator import provide_token

    return f"https://bedrock-runtime.{region}.amazonaws.com/openai/v1", provide_token(region=region)


def smoke(
    region: str,
    runtime_arn: str,
    bucket: str,
    agent_id: str,
    payloads: list[dict[str, Any]],
    log,
    timeout_s: int = 600,
    model_id: str = SMOKE_MODEL,
) -> dict[str, Any]:
    if not payloads:
        raise AppError("agent.no_smoke_payloads", "no sample payloads to smoke-test with")
    base_url, token = bedrock_openai(region)
    client = rollout_client(
        agent_runtime_arn=runtime_arn,
        s3_bucket=bucket,
        exp_id=f"smoke/{agent_id}",
        base_url=base_url,
        model_id=model_id,
        tps_limit=2,
        sampling_params={"max_tokens": 1024},
    )
    results = []
    futures = [
        client.invoke(p, input_id=f"smoke{i}", api_key=token) for i, p in enumerate(payloads)
    ]
    for i, f in enumerate(futures):
        try:
            r = f.result(timeout=timeout_s)
            rewards = r.get("rewards")
            ok = r.get("status_code", 200) == 200 and _numeric_reward(rewards)
            results.append(
                {
                    "index": i,
                    "ok": ok,
                    "status_code": r.get("status_code", 200),
                    "rewards": rewards,
                    "stop_reason": r.get("stop_reason"),
                    "error": None if ok else (r.get("error") or "missing numeric rewards"),
                }
            )
        except Exception as e:
            results.append({"index": i, "ok": False, "error": f"{type(e).__name__}: {e}"})
        log(f"smoke #{i}: {results[-1]}")
    passed = sum(r["ok"] for r in results)
    return {
        "model_id": model_id,
        "passed": passed,
        "total": len(results),
        "results": results,
        "ok": passed == len(results),
    }


def _numeric_reward(rewards: Any) -> bool:
    if isinstance(rewards, bool):
        return False
    if isinstance(rewards, int | float):
        return True
    if isinstance(rewards, list) and rewards:
        return all(isinstance(x, int | float) and not isinstance(x, bool) for x in rewards)
    return False
