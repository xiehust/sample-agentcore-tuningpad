"""Ledger tables. JSON columns hold spec/progress blobs whose shape is owned by
the module that writes them (pipelines/*, schemas)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .core.db import Base, TimestampMixin


class Project(Base, TimestampMixin):
    """Singleton per workspace: account-level resources created by Setup."""

    __tablename__ = "project"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str | None] = mapped_column(String(32))
    principal_arn: Mapped[str | None] = mapped_column(String(512))
    default_region: Mapped[str] = mapped_column(String(32))
    # per-region resources: {region: {bucket, acr_role_arn, ...}}
    regions: Mapped[dict] = mapped_column(JSON, default=dict)
    setup_status: Mapped[str] = mapped_column(String(32), default="pending")
    last_preflight: Mapped[dict] = mapped_column(JSON, default=dict)


class Cluster(Base, TimestampMixin):
    __tablename__ = "clusters"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    region: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(16))  # create | import
    status: Mapped[str] = mapped_column(String(32), default="pending")
    cfn_stack: Mapped[str | None] = mapped_column(String(128))
    eks_name: Mapped[str | None] = mapped_column(String(128))
    hyperpod_name: Mapped[str | None] = mapped_column(String(128))
    hyperpod_arn: Mapped[str | None] = mapped_column(String(512))
    # vpc_id, private_subnets[], az, sg_efa, sg_gw, sg_acr, sg_nlb, fsx_id, fsx_mount, nat
    network: Mapped[dict] = mapped_column(JSON, default=dict)
    components: Mapped[dict] = mapped_column(JSON, default=dict)  # {name: {status, detail}}
    params: Mapped[dict] = mapped_column(JSON, default=dict)  # creation parameters
    idle_policy: Mapped[dict] = mapped_column(JSON, default=dict)  # {idle_minutes, budget_usd}


class Agent(Base, TimestampMixin):
    __tablename__ = "agents"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(16))  # template | upload | image
    template_id: Mapped[str | None] = mapped_column(String(64))
    contract: Mapped[str] = mapped_column(String(16), default="verl")
    config: Mapped[dict] = mapped_column(JSON, default=dict)  # template params / upload key
    image_uri: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    checks: Mapped[dict] = mapped_column(JSON, default=dict)  # static/probe findings


class AgentRuntime(Base, TimestampMixin):
    __tablename__ = "agent_runtimes"
    __table_args__ = (UniqueConstraint("agent_id", "cluster_id"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"))
    # null = standalone PUBLIC runtime used only for contract smoke tests
    cluster_id: Mapped[str | None] = mapped_column(ForeignKey("clusters.id", ondelete="CASCADE"))
    region: Mapped[str] = mapped_column(String(32))
    network_mode: Mapped[str] = mapped_column(String(16), default="VPC")
    runtime_id: Mapped[str | None] = mapped_column(String(128))
    runtime_arn: Mapped[str | None] = mapped_column(String(512))
    image_uri: Mapped[str | None] = mapped_column(String(512))
    # requested before deploy, then the version AgentCore reports (V1 | V2)
    platform_version: Mapped[str | None] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    last_smoke: Mapped[dict] = mapped_column(JSON, default=dict)


class TrainerImage(Base, TimestampMixin):
    __tablename__ = "trainer_images"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    profile: Mapped[str] = mapped_column(String(16))  # fsdp | megatron
    region: Mapped[str] = mapped_column(String(32))
    toolkit_sha: Mapped[str] = mapped_column(String(64))
    image_uri: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    build_id: Mapped[str | None] = mapped_column(String(256))


class Dataset(Base, TimestampMixin):
    __tablename__ = "datasets"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    region: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(32))  # upload | builtin:<template>
    template_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    # {split: {s3_key, rows}}
    splits: Mapped[dict] = mapped_column(JSON, default=dict)
    prompt_field: Mapped[str] = mapped_column(String(64), default="prompt")
    has_prompt_column: Mapped[bool] = mapped_column(default=False)
    sample: Mapped[list] = mapped_column(JSON, default=list)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)


class Run(Base, TimestampMixin):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    agent_runtime_id: Mapped[str] = mapped_column(ForeignKey("agent_runtimes.id"))
    cluster_id: Mapped[str] = mapped_column(ForeignKey("clusters.id"))
    train_dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"))
    val_dataset_id: Mapped[str | None] = mapped_column(ForeignKey("datasets.id"))
    model_id: Mapped[str] = mapped_column(String(256))
    # {strategy, profile, params(basic/advanced), plan(planner output)}
    spec: Mapped[dict] = mapped_column(JSON, default=dict)
    # {instance_group, instance_type, nodes, scale_down_after, budget_usd, max_hours, auto_resume}
    compute: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    rayjob_name: Mapped[str | None] = mapped_column(String(128))
    retries: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    node_hours: Mapped[float] = mapped_column(Float, default=0.0)
    est_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    progress: Mapped[dict] = mapped_column(JSON, default=dict)  # {step, total_steps, log_offset}
    error: Mapped[str | None] = mapped_column(Text)


class RunMetric(Base):
    __tablename__ = "run_metrics"
    __table_args__ = (UniqueConstraint("run_id", "step", "key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    step: Mapped[int] = mapped_column(Integer)
    key: Mapped[str] = mapped_column(String(128))
    value: Mapped[float] = mapped_column(Float)


class Export(Base, TimestampMixin):
    __tablename__ = "exports"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    step: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    fsx_path: Mapped[str | None] = mapped_column(String(512))
    s3_uri: Mapped[str | None] = mapped_column(String(512))
    k8s_job: Mapped[str | None] = mapped_column(String(128))
    error: Mapped[str | None] = mapped_column(Text)


class InferenceEndpoint(Base, TimestampMixin):
    __tablename__ = "inference_endpoints"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    cluster_id: Mapped[str] = mapped_column(ForeignKey("clusters.id"))
    # {kind: hf|export, model_id?, export_id?, path?}
    model_source: Mapped[dict] = mapped_column(JSON, default=dict)
    served_model_name: Mapped[str] = mapped_column(String(256))
    instance_group: Mapped[str] = mapped_column(String(64))
    replicas: Mapped[int] = mapped_column(Integer, default=1)
    tp: Mapped[int] = mapped_column(Integer, default=1)
    url: Mapped[str | None] = mapped_column(String(512))
    api_key_secret_arn: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    error: Mapped[str | None] = mapped_column(Text)


class Eval(Base, TimestampMixin):
    __tablename__ = "evals"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    endpoint_id: Mapped[str] = mapped_column(ForeignKey("inference_endpoints.id"))
    agent_runtime_id: Mapped[str] = mapped_column(ForeignKey("agent_runtimes.id"))
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"))
    split: Mapped[str] = mapped_column(String(32), default="val")
    limit: Mapped[int] = mapped_column(Integer, default=200)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    # {n, succeeded, failed, mean_reward, acr_failed_rate}
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    results_s3: Mapped[str | None] = mapped_column(String(512))
    error: Mapped[str | None] = mapped_column(Text)


class Job(Base, TimestampMixin):
    """One pipeline execution (setup/cluster/agent/dataset/run/...)."""

    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(64), index=True)
    target_id: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    # [{name, status, detail, started_at, ended_at}]
    stages: Mapped[list] = mapped_column(JSON, default=list)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    context: Mapped[dict] = mapped_column(JSON, default=dict)  # durable outputs between stages
    error: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
