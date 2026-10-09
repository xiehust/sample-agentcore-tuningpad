"""Run helpers: rendering a run into k8s objects, RayJob status, S3 log tail."""

from __future__ import annotations

import json
from typing import Any

from botocore.exceptions import ClientError

from ..catalog import instances
from ..core import aws
from ..render import train as rt
from . import kube, pools

RAYJOB_API = "ray.io/v1"


def rayjob_name(run_id: str, attempt: int) -> str:
    return f"{run_id}-a{attempt}".lower()


def config_map_name(run_id: str) -> str:
    return f"{run_id}-cfg".lower()


def run_dir(run_id: str) -> str:
    return f"{rt.FSX_MOUNT}/runs/{run_id}"


def render(
    run: dict[str, Any],
    *,
    cluster: dict[str, Any],
    runtime_arn: str,
    bucket: str,
    image: str,
    template_loop: dict[str, Any],
    datasets: dict[str, str],
    attempt: int,
) -> dict[str, Any]:
    spec = instances.spec(run["compute"]["instance_type"])
    nodes = int(run["compute"]["nodes"])
    plan = run["spec"]["plan"]
    params = rt.merge_params(run["spec"].get("params"))
    rdir = run_dir(run["id"])
    model_dir = f"{rt.FSX_MOUNT}/models/{run['model_id'].replace('/', '--')}"
    data_files = {f"{rdir}/data/train.parquet": datasets["train"]}
    if datasets.get("val"):
        data_files[f"{rdir}/data/val.parquet"] = datasets["val"]
    hydra = rt.overrides(
        plan=plan,
        params=params,
        spec=spec,
        nodes=nodes,
        model_path=model_dir,
        train_file=f"{rdir}/data/train.parquet",
        val_file=f"{rdir}/data/val.parquet" if datasets.get("val") else None,
        ckpt_dir=f"{rdir}/ckpt",
        project="tuningpad",
        experiment=run["id"],
    )
    script = rt.train_script(
        run_id=run["id"],
        bucket=bucket,
        region=cluster["region"],
        model_id=run["model_id"],
        model_dir=model_dir,
        data=data_files,
        hydra=hydra,
        profile=plan["profile"],
        run_dir=rdir,
    )
    cm = rt.config_map(
        config_map_name(run["id"]),
        kube.NAMESPACE,
        run["id"],
        {
            "train.sh": script,
            "agentcore_agent.yaml": rt.agent_loop_yaml(template_loop, params),
            "hydra.json": json.dumps(hydra, indent=1),
        },
    )
    env = rt.pod_env(
        run_id=run["id"],
        region=cluster["region"],
        bucket=bucket,
        runtime_arn=runtime_arn,
        multi_node=nodes > 1,
        spec=spec,
    )
    max_hours = run["compute"].get("max_hours")
    job = rt.rayjob(
        run_id=run["id"],
        name=rayjob_name(run["id"], attempt),
        namespace=kube.NAMESPACE,
        image=image,
        spec=spec,
        nodes=nodes,
        group=run["compute"]["instance_group"],
        env=env,
        config_map=config_map_name(run["id"]),
        service_account="tuningpad-workload",
        pvc="fsx",
        active_deadline_s=int(max_hours * 3600) if max_hours else None,
        node_selector=pools.node_selector(
            run["compute"].get("provider", pools.HYPERPOD), run["compute"]["instance_group"]
        ),
    )
    return {"config_map": cm, "rayjob": job, "hydra": hydra}


def rayjob_state(region: str, eks: str, name: str) -> dict[str, Any] | None:
    obj = kube.get(region, eks, RAYJOB_API, "RayJob", name)
    if not obj:
        return None
    st = obj.get("status") or {}
    return {
        "deployment": st.get("jobDeploymentStatus"),
        "job": st.get("jobStatus"),
        "message": st.get("message") or st.get("reason"),
        "start": st.get("startTime"),
        "end": st.get("endTime"),
        "cluster": st.get("rayClusterName"),
    }


def run_pods(region: str, eks: str, run_id: str) -> list[dict[str, Any]]:
    pods = kube.list_objects(
        region, eks, "v1", "Pod", label_selector="ray.io/node-type"
    )  # head+worker pods
    out = []
    for p in pods:
        labels = p["metadata"].get("labels") or {}
        cluster = labels.get("ray.io/cluster", "")
        if not cluster.startswith(run_id.lower()):
            continue
        st = p.get("status") or {}
        waiting = next(
            (
                c.get("state", {}).get("waiting", {}).get("reason")
                for c in st.get("containerStatuses") or []
                if c.get("state", {}).get("waiting")
            ),
            None,
        )
        out.append(
            {
                "name": p["metadata"]["name"],
                "type": labels.get("ray.io/node-type"),
                "phase": st.get("phase"),
                "node": p.get("spec", {}).get("nodeName"),
                "ip": st.get("podIP"),
                "reason": waiting,
            }
        )
    return out


def read_log(
    region: str, bucket: str, run_id: str, offset: int, max_bytes: int = 4 * 1024**2
) -> tuple[str, int]:
    """Bytes [offset, offset+max) of runs/<id>/logs/train.log; ("", offset) if none yet."""
    try:
        r = aws.client("s3", region).get_object(
            Bucket=bucket,
            Key=f"runs/{run_id}/logs/train.log",
            Range=f"bytes={offset}-{offset + max_bytes - 1}",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] in {"NoSuchKey", "InvalidRange", "404", "416"}:
            return "", offset
        raise
    data = r["Body"].read()
    if len(data) == max_bytes:
        # don't split a multi-byte UTF-8 character across pages: hold back its lead bytes
        cut = len(data)
        for i in range(1, min(4, len(data)) + 1):
            b = data[-i]
            if b & 0xC0 != 0x80:  # lead byte (or ASCII) of the last character
                need = 1 if b < 0x80 else 2 if b >> 5 == 6 else 3 if b >> 4 == 14 else 4
                cut = len(data) - i if need > i else len(data)
                break
        data = data[:cut]
    return data.decode("utf-8", errors="replace"), offset + len(data)


def ckpt_steps(region: str, bucket: str, run_id: str) -> list[int]:
    try:
        body = (
            aws.client("s3", region)
            .get_object(Bucket=bucket, Key=f"runs/{run_id}/ckpt_index.json")["Body"]
            .read()
        )
    except ClientError:
        return []
    steps = []
    for name in json.loads(body).get("steps", []):
        try:
            steps.append(int(name.removeprefix("global_step_")))
        except ValueError:
            continue
    return sorted(steps)
