import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";

import { JobPanel } from "../components/JobPanel";
import { LineChart } from "../components/LineChart";
import { StatusTag } from "../components/StatusTag";
import { TrainingPlanField } from "../components/TrainingPlanField";
import {
  agentApi,
  catalogApi,
  clusterApi,
  datasetApi,
  errorMessage,
  runApi,
  servingApi,
  type CreateRunBody,
  type Plan,
  type RunCompute,
  type RunEstimate,
  type RunView,
  type TrainingPlan,
} from "../lib/api";
import { dateTime, duration, money, num } from "../lib/format";
import { useLocalized } from "../lib/localized";
import { DETAIL_POLL_MS, LIST_POLL_MS, usePoll } from "../lib/poll";
import { useLoad, useV2Toast } from "../v2/hooks";
import {
  Alert,
  Button,
  Card,
  Confirm,
  Descriptions,
  Empty,
  Field,
  FlowHeader,
  Kpi,
  PageHeader,
  Segmented,
  Select,
  Spin,
  Steps,
  SubTabs,
  Table,
  Tag,
} from "../v2/ui";
import { PlanCard } from "../components/ModelCards";

const ACTIVE = new Set(["queued", "preparing", "waiting_capacity", "running", "retrying"]);

/* ------------------------------------------------------------------ list */

function RunList() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const list = useLoad(runApi.list, "runs");
  usePoll(list.reload, LIST_POLL_MS);
  return (
    <>
      <PageHeader
        title={t("pages.runs.title")}
        desc={t("pages.runs.desc")}
        end={<Button kind="primary" onClick={() => navigate("/runs?view=new")} testId="tp-run-new">{t("runs.create")}</Button>}
      />
      {list.data?.length === 0 ? (
        <Empty action={<Button kind="primary" onClick={() => navigate("/runs?view=new")}>{t("runs.create")}</Button>}>{t("runs.empty")}</Empty>
      ) : (
        <Card flush>
          <Table
            rowKey={(r) => r.id}
            rows={list.data ?? []}
            loading={list.loading && !list.data}
            error={list.error}
            columns={[
              { key: "n", title: t("common.name"), render: (r) => <button type="button" className="v2-link" onClick={() => navigate(`/runs?view=detail&id=${r.id}`)}>{r.name}</button> },
              { key: "s", title: t("common.status"), render: (r) => <StatusTag status={r.status} /> },
              { key: "m", title: t("runs.model"), render: (r) => <span className="tp-mono">{r.model_id}</span> },
              { key: "c", title: t("runs.compute"), render: (r) => `${r.compute.nodes} × ${r.compute.instance_type}` },
              { key: "st", title: t("runs.step"), render: (r) => num(r.summary.last_step, 0) },
              { key: "v", title: t("runs.valReward"), render: (r) => num(r.summary.val_reward, 3) },
              { key: "cost", title: t("runs.cost"), render: (r) => money(r.est_cost_usd) },
              { key: "d", title: t("runs.duration"), render: (r) => duration(r.started_at, r.ended_at) },
            ]}
          />
        </Card>
      )}
    </>
  );
}

/* --------------------------------------------------------------- wizard */

const BASIC_KEYS = ["train_batch_size", "rollout_n", "lr", "total_training_steps", "total_epochs", "max_model_len", "max_prompt_length", "max_response_length"] as const;
const ADVANCED_KEYS = ["kl_loss_coef", "rollout_is_threshold", "temperature", "val_temperature", "val_n", "gpu_memory_utilization", "max_tokens_per_turn", "tps_limit", "max_rollout_time", "save_freq", "test_freq", "max_actor_ckpt_to_keep"] as const;

function NumField({ k, params, defaults, setParams }: { k: string; params: Record<string, unknown>; defaults: Record<string, unknown>; setParams: (p: Record<string, unknown>) => void }) {
  const { t } = useTranslation();
  const v = params[k];
  const fallback = defaults[k];
  const placeholder = typeof fallback === "number" ? String(fallback)
    : k === "total_training_steps" ? t("params.runAllEpochs")
    : k === "lr" || k === "gpu_memory_utilization" ? t("runs.auto") : "";
  return (
    <Field label={t(`params.${k}`)} hint={t(`params.${k}_hint`)}>
      <input
        className="v2-input"
        type="number"
        step="any"
        value={v === undefined || v === null ? "" : String(v)}
        placeholder={placeholder}
        data-testid={`tp-run-param-${k}`}
        onChange={(e) => setParams({ ...params, [k]: e.target.value === "" ? undefined : Number(e.target.value) })}
      />
    </Field>
  );
}

function RunWizard() {
  const { t } = useTranslation();
  const loc = useLocalized();
  const navigate = useNavigate();
  const toast = useV2Toast();
  const agents = useLoad(agentApi.list, "agents");
  const datasets = useLoad(datasetApi.list, "datasets");
  const clusters = useLoad(clusterApi.list, "clusters");
  const templates = useLoad(agentApi.templates, "templates");
  const defaults = useLoad(runApi.defaults, "run-defaults");
  const [step, setStep] = useState(0);
  const [name, setName] = useState(`run-${new Date().toISOString().slice(5, 16).replace(/[-:T]/g, "")}`);
  const [runtimeId, setRuntimeId] = useState("");
  const [trainId, setTrainId] = useState("");
  const [valId, setValId] = useState("");
  const [valSplit, setValSplit] = useState("val");
  const [modelId, setModelId] = useState("Qwen/Qwen3.5-2B");
  const [prefer, setPrefer] = useState("auto");
  const [tuning, setTuning] = useState("auto");
  const [params, setParams] = useState<Record<string, unknown>>({});
  const [compute, setCompute] = useState<RunCompute>({
    instance_group: "gpu-p5", instance_type: "p5.48xlarge", nodes: 1, provider: "hyperpod", capacity: "on_demand",
    scale_up: true, scale_down_after: true, max_hours: 6, budget_usd: null, max_retries: 2,
  });
  const [preview, setPreview] = useState<{ plan: Plan; estimate: RunEstimate; training_plan: TrainingPlan | null } | null>(null);
  const [planOk, setPlanOk] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const instances = useLoad(() => catalogApi.instances("us-east-1"), "inst");

  const runtimes = useMemo(
    () => (agents.data ?? []).flatMap((a) => a.runtimes.filter((r) => r.network_mode === "VPC" && r.status === "ready").map((r) => ({ agent: a, rt: r }))),
    [agents.data],
  );
  const chosen = runtimes.find((x) => x.rt.id === runtimeId);
  const tpl = templates.data?.find((x) => x.id === chosen?.agent.template_id);
  const cluster = clusters.data?.find((c) => c.id === chosen?.rt.cluster_id);
  const readyDatasets = (datasets.data ?? []).filter((d) => d.status === "ready");
  const valDs = readyDatasets.find((d) => d.id === valId);
  const needsTemplate = !!chosen?.agent.template_id;
  const defaultsLoading = defaults.loading || (needsTemplate && templates.loading);
  const defaultsError = defaults.error || (needsTemplate && (
    templates.error || (!templates.loading && !tpl ? t("params.templateDefaultsUnavailable") : null)
  ));
  const defaultsReady = !!defaults.data && !defaultsLoading && !defaultsError;
  // Display inherited defaults without turning them into submitted overrides.
  // An unresolved template is not a template-less runtime: don't show generic values.
  const paramDefaults = { ...defaults.data?.params };
  for (const [k, fallback] of Object.entries(defaults.data?.agent_loop ?? {})) {
    if (!needsTemplate || tpl) paramDefaults[k] = tpl?.agent_loop[k] ?? fallback;
  }

  const applyPreset = (pid: string) => {
    const p = tpl?.presets.find((x) => x.id === pid);
    if (!p) return;
    setModelId(p.model_id);
    setCompute((c) => ({ ...c, instance_type: p.instance_type, nodes: p.nodes, instance_group: groupName(p.instance_type, c.capacity, c.provider) }));
    setParams({ ...p.params });
  };

  const body = (): CreateRunBody => ({
    name, agent_runtime_id: runtimeId, train_dataset_id: trainId, val_dataset_id: valId || null,
    val_split: valSplit, model_id: modelId, prefer, tuning,
    params: Object.fromEntries(Object.entries(params).filter(([, v]) => v !== undefined && v !== "")),
    compute: { ...compute, training_plan_arn: compute.capacity === "training_plan" ? compute.training_plan_arn?.trim() || null : null },
  });

  const next = async () => {
    if (step === 2 || step === 3) {
      setBusy(true);
      try {
        setPreview(await runApi.preview(body()));
        setStep(step + 1);
      } catch (err) {
        toast("error", errorMessage(err));
      } finally {
        setBusy(false);
      }
      return;
    }
    setStep(step + 1);
  };

  const submit = async () => {
    setBusy(true);
    try {
      const r = await runApi.create({ ...body(), confirm_cost: true });
      navigate(`/runs?view=detail&id=${r.id}`);
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
      setConfirm(false);
    }
  };

  const canNext = [!!runtimeId && !!name, !!trainId, !!modelId && defaultsReady, !!compute.instance_type && compute.nodes > 0 && (compute.capacity !== "training_plan" || planOk), true][step];
  const itype = instances.data?.instances.find((i) => i.type === compute.instance_type);
  const ec2 = compute.provider === "ec2";

  return (
    <>
      <FlowHeader
        title={t("runs.create")}
        onBack={() => (step ? setStep(step - 1) : navigate("/runs"))}
        steps={<Steps steps={[t("runs.stepAgent"), t("runs.stepData"), t("runs.stepModel"), t("runs.stepCompute"), t("runs.stepReview")]} current={step} onSelect={setStep} />}
        end={
          step < 4 ? (
            <Button kind="primary" disabled={!canNext || busy} onClick={() => void next()} testId="tp-run-next">{t("common.next")}</Button>
          ) : (
            <Button kind="primary" disabled={busy} onClick={() => setConfirm(true)} testId="tp-run-start">{t("runs.start")}</Button>
          )
        }
      />
      {step === 0 && (
        <div className="tp-stack">
          <Card title={t("runs.stepAgent")} sub={t("runs.agentDesc")}>
            <div className="v2-form cols-2">
              <Field label={t("common.name")} required>
                <input className="v2-input" value={name} onChange={(e) => setName(e.target.value)} />
              </Field>
              <Field label={t("runs.runtime")} required hint={t("runs.runtimeHint")}>
                <Select
                  value={runtimeId}
                  onChange={setRuntimeId}
                  placeholder={t("runs.pickRuntime")}
                  options={runtimes.map(({ agent, rt }) => ({ value: rt.id, label: `${agent.name} · ${(clusters.data ?? []).find((c) => c.id === rt.cluster_id)?.name ?? rt.cluster_id}` }))}
                />
              </Field>
            </div>
            {runtimes.length === 0 && !agents.loading && <Alert tone="warn">{t("runs.noRuntime")}</Alert>}
          </Card>
        </div>
      )}
      {step === 1 && (
        <Card title={t("runs.stepData")}>
          <div className="v2-form cols-2">
            <Field label={t("runs.trainData")} required>
              <Select value={trainId} onChange={setTrainId} placeholder={t("runs.pickDataset")} options={readyDatasets.map((d) => ({ value: d.id, label: `${d.name} · train ${num(d.splits.train?.rows, 0)}` }))} />
            </Field>
            <Field label={t("runs.valData")} hint={t("runs.valDataHint")}>
              <Select value={valId} onChange={setValId} options={[{ value: "", label: t("runs.sameDataset") }, ...readyDatasets.map((d) => ({ value: d.id, label: d.name }))]} />
            </Field>
            {valDs && (
              <Field label={t("runs.valSplit")}>
                <Select value={valSplit} onChange={setValSplit} options={Object.keys(valDs.splits).map((k) => ({ value: k, label: `${k} (${num(valDs.splits[k].rows, 0)})` }))} />
              </Field>
            )}
          </div>
        </Card>
      )}
      {step === 2 && (
        <div className="tp-stack">
          {defaultsLoading && <Spin />}
          {defaultsError && <Alert tone="error" action={<Button size="sm" onClick={() => { defaults.reload(); templates.reload(); }}>{t("v2.common.retry")}</Button>}>{defaultsError}</Alert>}
          {tpl && tpl.presets.length > 0 && (
            <Card title={t("runs.presets")} sub={t("runs.presetsDesc")}>
              <div className="tp-row">
                {tpl.presets.map((p) => <Button key={p.id} size="sm" onClick={() => applyPreset(p.id)}>{loc(p.label)}</Button>)}
              </div>
            </Card>
          )}
          <Card title={t("runs.modelStrategy")}>
            <div className="v2-form cols-2">
              <Field label={t("models.hfId")} required full>
                <input className="v2-input tp-mono" value={modelId} onChange={(e) => setModelId(e.target.value)} />
              </Field>
              <Field label={t("runs.backend")}>
                <Select value={prefer} onChange={setPrefer} options={[{ value: "auto", label: t("runs.auto") }, { value: "fsdp", label: "FSDP" }, { value: "megatron", label: "Megatron" }]} />
              </Field>
              <Field label={t("runs.tuning")}>
                <Select value={tuning} onChange={setTuning} options={[{ value: "auto", label: t("runs.auto") }, { value: "full", label: t("runs.full") }, { value: "lora", label: "LoRA" }]} />
              </Field>
            </div>
          </Card>
          <Card title={t("runs.basicParams")}>
            <div className="v2-form cols-2">{BASIC_KEYS.map((k) => <NumField key={k} k={k} params={params} defaults={paramDefaults} setParams={setParams} />)}</div>
          </Card>
          <details className="v2-card" style={{ padding: "16px 24px" }}>
            <summary className="v2-sec-title" style={{ cursor: "pointer" }}>{t("runs.advancedParams")}</summary>
            <div className="v2-form cols-2">{ADVANCED_KEYS.map((k) => <NumField key={k} k={k} params={params} defaults={paramDefaults} setParams={setParams} />)}</div>
          </details>
        </div>
      )}
      {step === 3 && (
        <div className="tp-stack">
          <Card title={t("runs.stepCompute")} sub={cluster ? `${cluster.name} · ${cluster.region}` : ""}>
            <div className="v2-form cols-2">
              <Field label={t("runs.computeSource")} hint={t(ec2 ? "runs.computeSourceEc2Hint" : "runs.computeSourceHpHint")} full>
                <Segmented
                  value={compute.provider ?? "hyperpod"}
                  onChange={(v: "hyperpod" | "ec2") => {
                    const capacity = v === "ec2" && compute.capacity === "training_plan" ? "spot" : compute.capacity;
                    setCompute({ ...compute, provider: v, capacity, training_plan_arn: null, instance_group: groupName(compute.instance_type, capacity, v) });
                  }}
                  options={[
                    { value: "hyperpod", label: t("runs.source_hyperpod") },
                    { value: "ec2", label: t("runs.source_ec2") },
                  ]}
                />
              </Field>
              <Field label={t("models.instanceType")} required>
                <Select
                  value={compute.instance_type}
                  onChange={(v) => setCompute({ ...compute, instance_type: v, instance_group: groupName(v, compute.capacity, compute.provider) })}
                  options={(instances.data?.instances ?? []).filter((i) => i.training).map((i) => ({
                    value: i.type,
                    label: ec2
                      ? `${i.type} · ${i.gpus}×${i.gpu} · EC2 ${money(i.ec2_price_per_hour)}/h`
                      : `${i.ml_type} · ${i.gpus}×${i.gpu} · ${money(i.price_per_hour)}/h · quota ${num(i.quota.on_demand, 0)}`,
                  }))}
                />
              </Field>
              <Field label={t("models.nodes")} hint={itype && !itype.multi_node ? t("runs.singleNodeOnly") : t("runs.nodesHint")}>
                <input className="v2-input" type="number" min={1} max={itype?.multi_node ? 20 : 1} value={compute.nodes} onChange={(e) => setCompute({ ...compute, nodes: Math.max(1, Number(e.target.value) || 1) })} />
              </Field>
              <Field label={t("clusters.groupName")} hint={t("runs.groupHint")}>
                <input className="v2-input" value={compute.instance_group} onChange={(e) => setCompute({ ...compute, instance_group: e.target.value })} />
              </Field>
              <Field label={t("clusters.capacity")}>
                <Select value={compute.capacity} onChange={(v) => setCompute({ ...compute, capacity: v as RunCompute["capacity"], instance_group: groupName(compute.instance_type, v, compute.provider) })} options={[
                  { value: "on_demand", label: t("clusters.capacity_on_demand") },
                  ...(ec2 ? [] : [{ value: "training_plan", label: t("clusters.capacity_training_plan") }]),
                  { value: "spot", label: t("clusters.capacity_spot") },
                ]} />
              </Field>
              {compute.capacity === "training_plan" && cluster && (
                <TrainingPlanField
                  region={cluster.region}
                  clusterId={cluster.id}
                  instanceType={compute.instance_type}
                  count={compute.nodes}
                  value={compute.training_plan_arn ?? ""}
                  onChange={(arn) => setCompute((c) => ({ ...c, training_plan_arn: arn }))}
                  onValid={(p) => setPlanOk(!!p)}
                />
              )}
              <Field label={t("runs.maxHours")} hint={t("runs.maxHoursHint")}>
                <input className="v2-input" type="number" min={0.5} step={0.5} value={compute.max_hours ?? ""} onChange={(e) => setCompute({ ...compute, max_hours: e.target.value ? Number(e.target.value) : null })} />
              </Field>
              <Field label={t("runs.budget")} hint={t("runs.budgetHint")}>
                <input className="v2-input" type="number" min={1} value={compute.budget_usd ?? ""} onChange={(e) => setCompute({ ...compute, budget_usd: e.target.value ? Number(e.target.value) : null })} />
              </Field>
              <Field label={t("runs.maxRetries")}>
                <input className="v2-input" type="number" min={0} max={10} value={compute.max_retries} onChange={(e) => setCompute({ ...compute, max_retries: Number(e.target.value) || 0 })} />
              </Field>
              <Field label={t("runs.scaleDown")}>
                <label className="tp-row"><input type="checkbox" checked={compute.scale_down_after} onChange={(e) => setCompute({ ...compute, scale_down_after: e.target.checked })} />{t("runs.scaleDownHint")}</label>
              </Field>
            </div>
          </Card>
          {preview && <PlanCard plan={preview.plan} />}
        </div>
      )}
      {step === 4 && preview && (
        <div className="tp-stack">
          <div className="tp-grid c4">
            <Kpi label={t("runs.hourly")} value={money(preview.estimate.hourly_usd)} sub={preview.estimate.prepaid ? t("plans.prepaid") : `${preview.estimate.nodes} × ${money(preview.estimate.price_per_node_hour)}`} />
            <Kpi label={t("runs.maxCost")} value={money(preview.estimate.max_cost_usd)} sub={t("runs.maxCostSub", { h: preview.estimate.max_hours ?? "∞" })} />
            <Kpi label={t("runs.strategy")} value={t(`strategy.${preview.plan.strategy}`)} />
            <Kpi label={t("models.world")} value={preview.plan.world_size} />
          </div>
          <Card title={t("runs.stepReview")}>
            <Descriptions
              items={[
                { label: t("runs.runtime"), value: chosen ? `${chosen.agent.name} · ${cluster?.name}` : "—" },
                { label: t("runs.trainData"), value: readyDatasets.find((d) => d.id === trainId)?.name },
                { label: t("runs.model"), value: <span className="tp-mono">{modelId}</span> },
                { label: t("runs.compute"), value: `${compute.nodes} × ml.${compute.instance_type} (${compute.instance_group})` },
                { label: t("clusters.capacity"), value: `${t(`runs.source_${compute.provider ?? "hyperpod"}`)} · ${t(`clusters.capacity_${compute.capacity}`)}` },
                ...(preview.training_plan
                  ? [{ label: t("clusters.trainingPlan"), value: <span className="tp-mono">{`${preview.training_plan.name} · ${preview.training_plan.az ?? preview.training_plan.az_id ?? ""} · ${dateTime(preview.training_plan.end)}`}</span> }]
                  : []),
                { label: t("runs.params"), value: <span className="tp-mono">{JSON.stringify(body().params)}</span> },
              ]}
            />
          </Card>
          <PlanCard plan={preview.plan} />
        </div>
      )}
      <Confirm
        open={confirm}
        title={t("runs.confirmTitle")}
        body={preview ? t("runs.confirmBody", { hourly: money(preview.estimate.hourly_usd), max: money(preview.estimate.max_cost_usd) }) : ""}
        confirmLabel={t("runs.start")}
        danger
        busy={busy}
        onConfirm={() => void submit()}
        onClose={() => setConfirm(false)}
      />
    </>
  );
}

/* --------------------------------------------------------------- detail */

function RunLog({ id, live }: { id: string; live: boolean }) {
  const { t } = useTranslation();
  const [text, setText] = useState("");
  const offset = useRef(0);
  const box = useRef<HTMLPreElement>(null);
  const tick = useCallback(async () => {
    try {
      for (let i = 0; i < 20; i += 1) {
        const c = await runApi.log(id, offset.current);
        offset.current = c.next_offset;
        if (c.content) setText((p) => (p + c.content).slice(-400_000));
        if (c.eof) return;
      }
    } catch {
      /* retried on next tick */
    }
  }, [id]);
  useEffect(() => {
    void tick();
  }, [tick]);
  usePoll(() => void tick(), 10000, live);
  useEffect(() => {
    if (live && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [text, live]);
  // eslint-disable-next-line no-control-regex
  const clean = text.replace(/\x1b\[[0-9;]*m/g, "");
  return <pre ref={box} className="v2-pre tp-log">{clean || <span className="v2-muted">{t("runs.logWaiting")}</span>}</pre>;
}

const CHARTS: { key: string; title: string; series: string[]; fixed01?: boolean }[] = [
  { key: "reward", title: "runs.chartReward", series: ["val-core/unknown/reward/mean@1", "critic/score/mean"], fixed01: true },
  { key: "kl", title: "runs.chartKl", series: ["actor/kl_loss"] },
  { key: "len", title: "runs.chartLength", series: ["response_length/mean"] },
  { key: "health", title: "runs.chartHealth", series: ["training/rollout_failure/total_missing_sessions", "training/rollout_failure/total_transient_retry", "training/rollout_failure/total_model_train", "val-aux/unknown/acr_failed/mean@1"] },
  { key: "time", title: "runs.chartTime", series: ["timing_s/step", "timing_s/gen"] },
  { key: "grad", title: "runs.chartGrad", series: ["actor/grad_norm", "actor/entropy"] },
];

function RunDetail({ id }: { id: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const toast = useV2Toast();
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") ?? "metrics";
  const r = useLoad(() => runApi.get(id), id);
  const live = !!r.data && ACTIVE.has(r.data.status);
  usePoll(r.reload, live ? DETAIL_POLL_MS * 4 : 60000);
  const pods = useLoad(() => runApi.pods(id).catch(() => []), `pods-${id}-${tab}`);
  const [stop, setStop] = useState(false);
  if (!r.data) return r.error ? <Alert tone="error">{r.error}</Alert> : <Spin />;
  const run: RunView = r.data;
  const s = run.summary;
  const act = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      r.reload();
    } catch (err) {
      toast("error", errorMessage(err));
    }
  };
  const exportStep = async (step: number) => {
    await act(() => servingApi.createExport({ run_id: id, step }));
    navigate("/exports");
  };
  return (
    <>
      <FlowHeader
        title={<>{run.name} <StatusTag status={run.status} /></>}
        onBack={() => navigate("/runs")}
        end={
          <div className="tp-row">
            {live ? (
              <Button kind="danger" size="sm" onClick={() => setStop(true)} testId="tp-run-stop">{t("runs.stop")}</Button>
            ) : (
              <Button size="sm" onClick={() => void act(() => runApi.resume(id))}>{t("runs.resume")}</Button>
            )}
          </div>
        }
      />
      <div className="tp-stack">
        <div className="tp-grid c4">
          <Kpi label={t("runs.step")} value={num(s.last_step, 0)} sub={s.sec_per_step ? t("runs.secPerStep", { s: s.sec_per_step }) : undefined} />
          <Kpi label={t("runs.valReward")} value={num(s.val_reward, 3)} sub={t("runs.baseline", { v: num(s.baseline_val_reward, 3) })} tone={s.val_reward && s.baseline_val_reward && s.val_reward > s.baseline_val_reward ? "good" : undefined} />
          <Kpi label={t("runs.bestVal")} value={num(s.best_val_reward, 3)} sub={s.best_step !== null && s.best_step !== undefined ? t("runs.atStep", { s: s.best_step }) : undefined} />
          <Kpi label={t("runs.cost")} value={money(run.est_cost_usd)} sub={`${num(run.node_hours)} ${t("runs.nodeHours")} · ${duration(run.started_at, run.ended_at)}`} />
        </div>
        {run.error && <Alert tone="error">{run.error}</Alert>}
        {run.job && <JobPanel jobId={run.job.id} onDone={() => r.reload()} />}
        <SubTabs
          value={tab}
          onChange={(v) => setParams({ view: "detail", id, tab: v })}
          tabs={[
            { value: "metrics", label: t("runs.tabMetrics") },
            { value: "log", label: t("runs.tabLog") },
            { value: "pods", label: t("runs.tabPods") },
            { value: "ckpt", label: t("runs.tabCkpt") },
            { value: "config", label: t("runs.tabConfig") },
          ]}
        />
        {tab === "metrics" && (
          <div className="tp-grid c2">
            {CHARTS.map((c) => (
              <Card key={c.key} title={t(c.title)}>
                <LineChart
                  fixed01={c.fixed01}
                  empty={t("runs.noMetrics")}
                  series={c.series.map((k) => ({ name: k, points: run.series?.[k] ?? [] })).filter((x) => x.points.length)}
                />
              </Card>
            ))}
          </div>
        )}
        {tab === "log" && <Card title={t("runs.tabLog")}><RunLog id={id} live={live} /></Card>}
        {tab === "pods" && (
          <Card title={t("runs.tabPods")} flush>
            <Table
              rowKey={(p) => p.name}
              rows={pods.data ?? []}
              loading={pods.loading}
              empty={t("runs.noPods")}
              columns={[
                { key: "n", title: t("common.name"), render: (p) => <span className="tp-mono">{p.name}</span> },
                { key: "t", title: t("runs.podType"), render: (p) => p.type },
                { key: "p", title: t("common.status"), render: (p) => <StatusTag status={p.phase} /> },
                { key: "r", title: t("common.detail"), render: (p) => p.reason ?? "" },
                { key: "ip", title: "IP", render: (p) => <span className="tp-mono">{p.ip ?? "—"}</span> },
                { key: "node", title: t("runs.node"), render: (p) => <span className="tp-mono">{p.node ?? "—"}</span> },
              ]}
            />
          </Card>
        )}
        {tab === "ckpt" && (
          <Card title={t("runs.tabCkpt")} sub={t("runs.ckptDesc")} flush>
            <Table
              rowKey={(x) => String(x)}
              rows={run.progress.ckpt_steps ?? []}
              empty={t("runs.noCkpt")}
              columns={[
                { key: "s", title: t("runs.step"), render: (x) => `global_step_${x}` },
                { key: "v", title: t("runs.valReward"), render: (x) => num(run.series?.["val-core/unknown/reward/mean@1"]?.find((p) => p[0] === x)?.[1], 3) },
                { key: "a", title: "", render: (x) => <Button size="sm" kind="primary" onClick={() => void exportStep(x)}>{t("runs.export")}</Button> },
              ]}
            />
          </Card>
        )}
        {tab === "config" && (
          <Card title={t("runs.tabConfig")}>
            <Descriptions
              items={[
                { label: t("runs.model"), value: <span className="tp-mono">{run.model_id}</span> },
                { label: t("runs.strategy"), value: <Tag tone="blue">{t(`strategy.${run.spec.plan.strategy}`)}</Tag> },
                { label: t("runs.compute"), value: `${run.compute.nodes} × ml.${run.compute.instance_type} · ${run.compute.instance_group}` },
                { label: t("runs.computeSource"), value: t(`runs.source_${run.compute.provider ?? "hyperpod"}`) },
                { label: t("clusters.capacity"), value: run.compute.training_plan_arn ? <span className="tp-mono">{run.compute.training_plan_arn.split("/").pop()}</span> : t(`clusters.capacity_${run.compute.capacity ?? "on_demand"}`) },
                { label: "RayJob", value: <span className="tp-mono">{run.rayjob_name ?? "—"}</span> },
                { label: t("runs.retries"), value: `${run.retries} / ${run.compute.max_retries}` },
                { label: t("common.created"), value: dateTime(run.created_at) },
              ]}
            />
            <pre className="v2-pre tp-log" style={{ marginTop: 12 }}>{(run.progress.hydra ?? []).join("\n")}</pre>
          </Card>
        )}
      </div>
      <Confirm open={stop} title={t("runs.stopTitle")} body={t("runs.stopBody")} confirmLabel={t("runs.stop")} danger onConfirm={() => { setStop(false); void act(() => runApi.stop(id)); }} onClose={() => setStop(false)} />
    </>
  );
}

export default function RunsPage() {
  const [params] = useSearchParams();
  const view = params.get("view");
  const id = params.get("id");
  if (view === "new") return <RunWizard />;
  if (view === "detail" && id) return <RunDetail id={id} />;
  return <RunList />;
}

/** Separate group per capacity source: HyperPod keeps a group's source when it is rescaled. */
function groupName(instanceType: string, capacity: string, provider = "hyperpod"): string {
  const base = `${provider === "ec2" ? "ec2" : "gpu"}-${instanceType.split(".")[0]}`;
  return capacity === "spot" ? `${base}-spot` : capacity === "training_plan" ? `${base}-ftp` : base;
}
