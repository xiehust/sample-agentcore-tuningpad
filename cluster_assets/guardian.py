#!/usr/bin/env python3
"""TuningPad in-cluster guardian (runs as a CronJob on the system node).

Independent of the TuningPad backend: if the console is down, this still
scales idle GPU instance groups to zero and enforces the cluster budget.

Config (mounted ConfigMap /config/guardian.json):
  {"region", "cluster", "bucket", "ledger_key", "budget_usd",
   "groups": {"<group>": {"idle_minutes": 30, "price_per_hour": 66.0, "managed": true}},
   "eks_cluster": "<eks>", "nodegroups": {"<node group>": {...same keys...}}}

`groups` are HyperPod instance groups (scaled via UpdateCluster); `nodegroups` are EKS
managed node groups (plain EC2, scaled via UpdateNodegroupConfig). Pool names are unique
across both, so they share one ledger namespace.

A group is "busy" while any non-terminal pod in the tuningpad namespace runs on
one of its nodes or is pending with a nodeSelector for it (a RayJob waiting for
scale-up must not be scaled away).
"""

import json
import os
import ssl
import sys
import time
import urllib.request

import boto3

NS = "tuningpad"
GROUP_LABEL = "sagemaker.amazonaws.com/instance-group-name"
POOL_LABEL = "tuningpad.io/pool"
LABELS = (GROUP_LABEL, POOL_LABEL)
SA = "/var/run/secrets/kubernetes.io/serviceaccount"
COPY_KEYS = (
    "InstanceGroupName", "InstanceType", "ExecutionRole", "LifeCycleConfig", "ThreadsPerCore",
    "TrainingPlanArn", "OnStartDeepHealthChecks", "CapacityRequirements",
    "InstanceStorageConfigs", "OverrideVpcConfig",
)


def k8s(path):
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    token = open(f"{SA}/token").read()
    ctx = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    req = urllib.request.Request(f"https://{host}:{port}{path}",
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
        return json.load(r)


def _pool_of(labels):
    return labels.get(POOL_LABEL) or labels.get(GROUP_LABEL)


def busy_groups():
    node_group = {
        n["metadata"]["name"]: _pool_of(n["metadata"].get("labels", {}))
        for n in k8s("/api/v1/nodes")["items"]
    }
    busy = set()
    for p in k8s(f"/api/v1/namespaces/{NS}/pods")["items"]:
        phase = p.get("status", {}).get("phase")
        if phase not in ("Pending", "Running"):
            continue
        node = p.get("spec", {}).get("nodeName")
        if node and node_group.get(node):
            busy.add(node_group[node])
        sel = _pool_of(p.get("spec", {}).get("nodeSelector") or {})
        if sel:
            busy.add(sel)
    return busy


eks = None


def nodegroups(cfg):
    """{name: {desired, max, status, running}} for the configured EKS node groups."""
    global eks
    out = {}
    if not cfg.get("nodegroups"):
        return out
    eks = boto3.client("eks", region_name=cfg["region"])
    asg = boto3.client("autoscaling", region_name=cfg["region"])
    for name in cfg["nodegroups"]:
        try:
            ng = eks.describe_nodegroup(clusterName=cfg["eks_cluster"], nodegroupName=name)["nodegroup"]
        except eks.exceptions.ResourceNotFoundException:
            continue
        sc = ng.get("scalingConfig") or {}
        names = [a["name"] for a in (ng.get("resources") or {}).get("autoScalingGroups", [])]
        running = 0
        if names:  # billed instances, not just Ready nodes
            for g in asg.describe_auto_scaling_groups(AutoScalingGroupNames=names)["AutoScalingGroups"]:
                running += sum(1 for i in g.get("Instances", [])
                               if not i["LifecycleState"].startswith("Terminat"))
        out[name] = {"desired": sc.get("desiredSize", 0), "max": sc.get("maxSize", 1),
                     "status": ng.get("status"), "running": running}
    return out


def main():
    cfg = json.load(open("/config/guardian.json"))
    s3 = boto3.client("s3", region_name=cfg["region"])
    sm = boto3.client("sagemaker", region_name=cfg["region"])
    try:
        ledger = json.loads(s3.get_object(Bucket=cfg["bucket"], Key=cfg["ledger_key"])["Body"].read())
    except s3.exceptions.NoSuchKey:
        ledger = {"groups": {}, "total_cost_usd": 0.0, "actions": []}
    now = time.time()
    last = ledger.get("updated_at_epoch", now)
    elapsed_h = max(0.0, (now - last) / 3600)

    desc = sm.describe_cluster(ClusterName=cfg["cluster"])
    raw = {g["InstanceGroupName"]: g for g in desc.get("InstanceGroups", [])}
    ngs = nodegroups(cfg)
    busy = busy_groups()
    over_budget = False
    budget = cfg.get("budget_usd") or 0
    pools = [(n, c, "hyperpod") for n, c in cfg.get("groups", {}).items()] + [
        (n, c, "ec2") for n, c in (cfg.get("nodegroups") or {}).items()
    ]

    for name, gcfg, kind in pools:
        if kind == "hyperpod":
            g = raw.get(name)
            if not g:
                continue
            count, target = g.get("CurrentCount", 0), g.get("TargetCount", g.get("InstanceCount"))
        else:
            if name not in ngs:
                continue
            count, target = ngs[name]["running"], ngs[name]["desired"]
        st = ledger["groups"].setdefault(name, {"node_hours": 0.0, "cost_usd": 0.0, "idle_since": None})
        st["provider"] = kind
        st["node_hours"] += count * elapsed_h
        st["cost_usd"] += count * elapsed_h * float(gcfg.get("price_per_hour") or 0)
        st["current"] = count
        st["target"] = target
        if name in busy:
            st["idle_since"] = None
        elif count > 0 and st["idle_since"] is None:
            st["idle_since"] = now
    ledger["total_cost_usd"] = sum(s["cost_usd"] for s in ledger["groups"].values())
    if budget and ledger["total_cost_usd"] >= budget:
        over_budget = True
        ledger["alert"] = f"budget ${budget} reached (${ledger['total_cost_usd']:.2f})"

    for name, gcfg, kind in pools:
        g, st = (raw.get(name) if kind == "hyperpod" else ngs.get(name)), ledger["groups"].get(name)
        if not g or not st or not gcfg.get("managed", True):
            continue
        target = g.get("TargetCount", g.get("InstanceCount", 0)) if kind == "hyperpod" else g["desired"]
        if target == 0:
            continue
        idle_s = now - st["idle_since"] if st.get("idle_since") else 0
        reason = None
        if over_budget:
            reason = "budget"
        elif st.get("idle_since") and idle_s >= int(gcfg.get("idle_minutes", 30)) * 60:
            reason = f"idle {int(idle_s // 60)} min"
        if reason:
            if kind == "hyperpod":
                spec = {k: g[k] for k in COPY_KEYS if g.get(k) is not None}
                spec["InstanceCount"] = 0
                sm.update_cluster(ClusterName=cfg["cluster"], InstanceGroups=[spec])
            elif g["status"] in ("ACTIVE", "DEGRADED"):
                eks.update_nodegroup_config(
                    clusterName=cfg["eks_cluster"], nodegroupName=name,
                    scalingConfig={"minSize": 0, "maxSize": max(1, g["max"]), "desiredSize": 0})
            else:
                print(f"{name} is {g['status']}; retry scale-to-zero next run")
                continue
            ledger["actions"] = (ledger.get("actions") or [])[-49:] + [
                {"at": now, "group": name, "action": "scale_to_zero", "reason": reason}]
            print(f"scaled {name} to 0 ({reason})")

    ledger["updated_at_epoch"] = now
    s3.put_object(Bucket=cfg["bucket"], Key=cfg["ledger_key"], Body=json.dumps(ledger).encode(),
                  ContentType="application/json")
    print(json.dumps({"busy": sorted(busy), "total_cost_usd": round(ledger["total_cost_usd"], 2)}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # surface in the CronJob's pod status
        print(f"guardian error: {type(e).__name__}: {e}", file=sys.stderr)
        raise
