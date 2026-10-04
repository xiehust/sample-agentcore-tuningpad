import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";

import { JobPanel } from "../components/JobPanel";
import { StatusTag } from "../components/StatusTag";
import { NodeGroupsCard } from "../components/NodeGroupsCard";
import { TrainingPlanField } from "../components/TrainingPlanField";
import {
  catalogApi,
  clusterApi,
  errorMessage,
  plansApi,
  type Cluster,
  type ClusterPreview,
  type GroupBody,
  type GroupView,
  type PlanOffering,
} from "../lib/api";
import { dateTime, money, num } from "../lib/format";
import { LIST_POLL_MS, DETAIL_POLL_MS, usePoll } from "../lib/poll";
import { regionOptions } from "../lib/regions";
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
  Modal,
  OptionCard,
  PageHeader,
  Select,
  Spin,
  SubTabs,
  Table,
  Tag,
} from "../v2/ui";

const COMPONENT_ORDER = ["access", "reach", "namespace", "irsa", "node_ecr", "security_groups", "kuberay", "lbc", "fsx", "guardian"];
const LIVE_STATUSES = new Set(["queued", "creating", "deleting"]);

/* ------------------------------------------------------------------ list */

function ClusterList() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const list = useLoad(clusterApi.list, "clusters");
  usePoll(list.reload, LIST_POLL_MS);
  return (
    <>
      <PageHeader
        title={t("pages.clusters.title")}
        desc={t("pages.clusters.desc")}
        end={
          <>
            <Button onClick={() => navigate("/clusters?view=import")} testId="tp-cluster-import">
              {t("clusters.import")}
            </Button>
            <Button kind="primary" onClick={() => navigate("/clusters?view=new")} testId="tp-cluster-new">
              {t("clusters.create")}
            </Button>
          </>
        }
      />
      {list.data && list.data.length === 0 ? (
        <Empty action={<Button kind="primary" onClick={() => navigate("/clusters?view=new")}>{t("clusters.create")}</Button>}>
          {t("clusters.empty")}
        </Empty>
      ) : (
        <Card flush>
          <Table
            rowKey={(c) => c.id}
            rows={list.data ?? []}
            loading={list.loading && !list.data}
            error={list.error}
            onRetry={list.reload}
            testId="tp-cluster-table"
            columns={[
              {
                key: "n",
                title: t("common.name"),
                render: (c) => (
                  <button type="button" className="v2-link" onClick={() => navigate(`/clusters?view=detail&id=${c.id}`)}>
                    {c.name}
                  </button>
                ),
              },
              { key: "s", title: t("common.status"), render: (c) => <StatusTag status={c.status} /> },
              { key: "r", title: t("common.region"), render: (c) => c.region },
              { key: "src", title: t("clusters.source"), render: (c) => t(`clusters.source_${c.source}`) },
              { key: "hp", title: "HyperPod", render: (c) => <span className="tp-mono">{c.hyperpod_name ?? "—"}</span> },
              { key: "eks", title: "EKS", render: (c) => <span className="tp-mono">{c.eks_name ?? "—"}</span> },
              { key: "c", title: t("common.created"), render: (c) => dateTime(c.created_at) },
            ]}
          />
        </Card>
      )}
    </>
  );
}

/* ---------------------------------------------------------------- create */

function CreateCluster() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const toast = useV2Toast();
  const [step, setStep] = useState(0);
  const [name, setName] = useState("rl-dev");
  const [region, setRegion] = useState("us-east-1");
  const [azIds, setAzIds] = useState<string[]>([]);
  const [fsx, setFsx] = useState(1200);
  const [idle, setIdle] = useState(30);
  const [budget, setBudget] = useState<string>("");
  const [preview, setPreview] = useState<ClusterPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const azs = useLoad(() => clusterApi.azs(region), region);
  const instances = useLoad(() => catalogApi.instances(region), `inst-${region}`);

  const body = () => ({
    name, region, az_ids: azIds, fsx_capacity_gib: fsx, idle_minutes: idle,
    budget_usd: budget ? Number(budget) : null,
  });

  const next = async () => {
    if (step === 0) {
      setBusy(true);
      try {
        setPreview(await clusterApi.preview(body()));
        setStep(1);
      } catch (err) {
        toast("error", errorMessage(err));
      } finally {
        setBusy(false);
      }
      return;
    }
    setBusy(true);
    try {
      const r = await clusterApi.create(body());
      navigate(`/clusters?view=detail&id=${r.id}`);
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const toggleAz = (id: string) =>
    setAzIds((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  const quotaRows = (instances.data?.instances ?? []).filter((i) => (i.quota.on_demand ?? 0) > 0);

  return (
    <>
      <FlowHeader
        title={t("clusters.create")}
        onBack={() => (step ? setStep(0) : navigate("/clusters"))}
        end={
          <Button kind="primary" disabled={busy || !name || azIds.length === 0} onClick={() => void next()} testId="tp-cluster-next">
            {step === 0 ? t("common.next") : t("clusters.createConfirm")}
          </Button>
        }
      />
      {step === 0 ? (
        <div className="tp-stack">
          <Card title={t("clusters.basics")}>
            <div className="v2-form cols-2">
              <Field label={t("common.name")} required hint={t("clusters.nameHint")}>
                <input className="v2-input" value={name} onChange={(e) => setName(e.target.value.toLowerCase())} data-testid="tp-cluster-name" />
              </Field>
              <Field label={t("common.region")} required>
                <Select value={region} options={regionOptions} onChange={(r) => { setRegion(r); setAzIds([]); }} />
              </Field>
              <Field label={t("clusters.azs")} required hint={t("clusters.azsHint")} full>
                <div className="tp-row">
                  {(azs.data ?? []).map((z) => (
                    <button
                      key={z.id}
                      type="button"
                      className={azIds.includes(z.id) ? "v2-btn sm primary" : "v2-btn sm"}
                      onClick={() => toggleAz(z.id)}
                    >
                      {z.name} · {z.id}
                    </button>
                  ))}
                </div>
              </Field>
              <Field label={t("clusters.fsx")} hint={t("clusters.fsxHint")}>
                <Select
                  value={String(fsx)}
                  onChange={(v) => setFsx(Number(v))}
                  options={[1200, 2400, 4800, 9600].map((v) => ({ value: String(v), label: `${v} GiB` }))}
                />
              </Field>
            </div>
          </Card>
          <Card title={t("clusters.guardrails")} sub={t("clusters.guardrailsDesc")}>
            <div className="v2-form cols-2">
              <Field label={t("clusters.idleMinutes")} hint={t("clusters.idleHint")}>
                <input className="v2-input" type="number" min={5} value={idle} onChange={(e) => setIdle(Number(e.target.value) || 30)} />
              </Field>
              <Field label={t("clusters.budget")} hint={t("clusters.budgetHint")}>
                <input className="v2-input" type="number" min={0} value={budget} placeholder="—" onChange={(e) => setBudget(e.target.value)} />
              </Field>
            </div>
          </Card>
          <Card title={t("clusters.quota")} sub={region}>
            {quotaRows.length === 0 ? (
              <Alert tone="warn">{t("clusters.noQuota")}</Alert>
            ) : (
              <div className="tp-row">
                {quotaRows.map((i) => (
                  <Tag key={i.type} tone="blue">{i.ml_type} × {num(i.quota.on_demand, 0)} · {money(i.price_per_hour)}/h</Tag>
                ))}
              </div>
            )}
          </Card>
        </div>
      ) : (
        preview && (
          <div className="tp-stack">
            <Alert tone="info">{t("clusters.createInfo")}</Alert>
            <div className="tp-grid c3">
              <Kpi label={t("clusters.systemNode")} value={money(preview.system_price_per_hour)} sub={`${preview.system_instance} / h`} />
              <Kpi label={t("clusters.fsxCost")} value={money(preview.fsx_monthly_usd_estimate, 0)} sub={t("clusters.perMonth")} />
              <Kpi label={t("clusters.gpuCost")} value="$0" sub={t("clusters.gpuCostSub")} />
            </div>
            <Card title={t("clusters.stack")} sub={<span className="tp-mono">{preview.stack_name}</span>}>
              <ul className="v2-muted">{preview.notes.map((n) => <li key={n}>{n}</li>)}</ul>
              <Table
                rowKey={(p) => p.ParameterKey}
                rows={preview.parameters}
                columns={[
                  { key: "k", title: t("clusters.parameter"), width: 280, render: (p) => <code className="tp-mono">{p.ParameterKey}</code> },
                  { key: "v", title: t("clusters.value"), render: (p) => <span className="tp-mono">{p.ParameterValue}</span> },
                ]}
              />
            </Card>
          </div>
        )
      )}
    </>
  );
}

/* ---------------------------------------------------------------- import */

function ImportCluster() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const toast = useV2Toast();
  const [region, setRegion] = useState("us-east-1");
  const [selected, setSelected] = useState("");
  const [busy, setBusy] = useState(false);
  const found = useLoad(() => clusterApi.discoverable(region), region);
  const submit = async () => {
    setBusy(true);
    try {
      const r = await clusterApi.import({ region, hyperpod_name: selected, idle_minutes: 30, budget_usd: null });
      navigate(`/clusters?view=detail&id=${r.id}`);
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <FlowHeader
        title={t("clusters.import")}
        onBack={() => navigate("/clusters")}
        end={<Button kind="primary" disabled={!selected || busy} onClick={() => void submit()}>{t("clusters.importConfirm")}</Button>}
      />
      <Card title={t("clusters.importPick")} sub={t("clusters.importDesc")}>
        <Field label={t("common.region")}>
          <Select value={region} options={regionOptions} onChange={setRegion} />
        </Field>
        <div className="tp-tiles" style={{ marginTop: 16 }}>
          {found.loading && <Spin />}
          {found.error && <Alert tone="error">{found.error}</Alert>}
          {(found.data ?? []).map((c) => (
            <OptionCard key={c.name} title={c.name} desc={c.status} on={selected === c.name} onClick={() => setSelected(c.name)} />
          ))}
          {found.data?.length === 0 && <p className="v2-muted">{t("clusters.importNone")}</p>}
        </div>
      </Card>
    </>
  );
}

/* ---------------------------------------------------------------- detail */

function ComponentsCard({ cluster, onRepair }: { cluster: Cluster; onRepair: () => void }) {
  const { t } = useTranslation();
  return (
    <Card
      title={t("clusters.components")}
      sub={t("clusters.componentsDesc")}
      end={<Button size="sm" onClick={onRepair} disabled={!cluster.hyperpod_arn}>{t("clusters.repair")}</Button>}
    >
      <Table
        rowKey={(r) => r}
        rows={COMPONENT_ORDER}
        columns={[
          { key: "n", title: t("clusters.component"), width: 220, render: (r) => t(`components.${r}`) },
          { key: "s", title: t("common.status"), width: 120, render: (r) => <StatusTag status={cluster.components[r]?.status ?? "pending"} /> },
          { key: "d", title: t("common.detail"), render: (r) => <span className="v2-muted">{cluster.components[r]?.detail ?? ""}</span> },
        ]}
      />
    </Card>
  );
}

function GroupForm({ cluster, initial, onClose, onStarted }: {
  cluster: Cluster;
  initial?: GroupView;
  onClose: () => void;
  onStarted: (jobId: string) => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const instances = useLoad(() => catalogApi.instances(cluster.region), `inst-${cluster.region}`);
  const [group, setGroup] = useState(initial?.name ?? "gpu-p5");
  const [itype, setItype] = useState(initial?.instance_type?.replace(/^ml\./, "") ?? "p5.48xlarge");
  const [count, setCount] = useState(initial ? initial.target : 1);
  const [capacity, setCapacity] = useState<GroupBody["capacity"]>(initial?.spot ? "spot" : initial?.training_plan_arn ? "training_plan" : "on_demand");
  const [planArn, setPlanArn] = useState<string>(initial?.training_plan_arn ?? "");
  const [planOk, setPlanOk] = useState(false);
  const [dhc, setDhc] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const row = instances.data?.instances.find((i) => i.type === itype);
  const prepaid = capacity === "training_plan";
  const hourly = prepaid ? 0 : (row?.price_per_hour ?? 0) * count;

  const submit = async () => {
    setBusy(true);
    try {
      const r = await clusterApi.scale(cluster.id, {
        group, instance_type: initial ? undefined : itype, count, capacity,
        training_plan_arn: capacity === "training_plan" ? planArn : null, deep_health_checks: dhc,
        confirm_cost: true,
      });
      onStarted(r.job_id);
      onClose();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
      setConfirm(false);
    }
  };

  return (
    <>
      <Modal
        open
        title={initial ? t("clusters.scaleGroup", { name: initial.name }) : t("clusters.addGroup")}
        onClose={onClose}
        footer={
          <>
            <Button onClick={onClose}>{t("v2.common.cancel")}</Button>
            <Button kind="primary" disabled={busy || !group || (prepaid && count > 0 && !planOk)} onClick={() => (count > 0 ? setConfirm(true) : void submit())} testId="tp-group-submit">
              {t("common.apply")}
            </Button>
          </>
        }
      >
        <div className="v2-form cols-2">
          <Field label={t("clusters.groupName")} required>
            <input className="v2-input" value={group} disabled={!!initial} onChange={(e) => setGroup(e.target.value)} />
          </Field>
          <Field label={t("models.instanceType")} required>
            <Select
              value={itype}
              disabled={!!initial}
              onChange={setItype}
              options={(instances.data?.instances ?? []).map((i) => ({
                value: i.type,
                label: `${i.ml_type} · ${i.gpus}×${i.gpu} · ${money(i.price_per_hour)}/h · quota ${num(i.quota.on_demand, 0)}`,
              }))}
            />
          </Field>
          <Field label={t("clusters.count")} required hint={t("clusters.countHint")}>
            <input className="v2-input" type="number" min={0} max={20} value={count} onChange={(e) => setCount(Math.max(0, Number(e.target.value) || 0))} />
          </Field>
          <Field label={t("clusters.capacity")}>
            <Select
              value={capacity}
              disabled={!!initial}
              onChange={(v) => setCapacity(v as GroupBody["capacity"])}
              options={[
                { value: "on_demand", label: t("clusters.capacity_on_demand") },
                { value: "training_plan", label: t("clusters.capacity_training_plan") },
                { value: "spot", label: t("clusters.capacity_spot") },
              ]}
            />
          </Field>
          {capacity === "training_plan" && (
            <TrainingPlanField
              region={cluster.region}
              clusterId={cluster.id}
              instanceType={itype}
              count={count}
              value={planArn}
              onChange={setPlanArn}
              onValid={(p) => setPlanOk(!!p)}
              disabled={!!initial}
            />
          )}
          {!initial && (
            <Field label={t("clusters.deepHealth")} hint={t("clusters.deepHealthHint")} full>
              <label className="tp-row">
                <input type="checkbox" checked={dhc} onChange={(e) => setDhc(e.target.checked)} />
                {t("clusters.deepHealthOn")}
              </label>
            </Field>
          )}
        </div>
        {count > 0 && (
          <div style={{ marginTop: 12 }}>
            {prepaid ? <Alert tone="info">{t("plans.prepaidWarn")}</Alert> : <Alert tone="warn">{t("clusters.costWarn", { cost: money(hourly) })}</Alert>}
          </div>
        )}
      </Modal>
      <Confirm
        open={confirm}
        title={t("clusters.confirmCostTitle")}
        body={t("clusters.confirmCostBody", { count, type: itype, cost: money(hourly) })}
        confirmLabel={t("clusters.confirmCost")}
        danger
        busy={busy}
        onConfirm={() => void submit()}
        onClose={() => setConfirm(false)}
      />
    </>
  );
}

function GroupsCard({ cluster, onJob }: { cluster: Cluster; onJob: (id: string) => void }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [form, setForm] = useState<{ open: boolean; group?: GroupView }>({ open: false });
  const [del, setDel] = useState<string | null>(null);
  const groups = cluster.live?.instance_groups ?? [];
  const remove = async () => {
    if (!del) return;
    try {
      onJob((await clusterApi.deleteGroup(cluster.id, del)).job_id);
    } catch (err) {
      toast("error", errorMessage(err));
    }
    setDel(null);
  };
  return (
    <Card
      title={t("clusters.groups")}
      sub={t("clusters.groupsDesc")}
      end={<Button kind="primary" size="sm" disabled={cluster.status !== "ready"} onClick={() => setForm({ open: true })} testId="tp-add-group">{t("clusters.addGroup")}</Button>}
      flush
    >
      <Table
        rowKey={(g) => g.name}
        rows={groups}
        columns={[
          { key: "n", title: t("common.name"), render: (g) => <b>{g.name}</b> },
          { key: "t", title: t("models.instanceType"), render: (g) => <span className="tp-mono">{g.instance_type}</span> },
          { key: "c", title: t("clusters.nodesCol"), render: (g) => `${g.current} / ${g.target}` },
          { key: "s", title: t("common.status"), render: (g) => <StatusTag status={g.status} /> },
          { key: "cap", title: t("clusters.capacity"), render: (g) => (g.spot ? t("clusters.capacity_spot") : g.training_plan_arn ? t("clusters.capacity_training_plan") : t("clusters.capacity_on_demand")) },
          { key: "p", title: t("clusters.hourly"), render: (g) => money((g.price_per_hour ?? 0) * g.current) },
          {
            key: "a",
            title: "",
            render: (g) =>
              g.name === "system" ? (
                <span className="v2-muted">{t("clusters.systemGroup")}</span>
              ) : (
                <div className="tp-row">
                  <Button size="sm" onClick={() => setForm({ open: true, group: g })}>{t("clusters.scale")}</Button>
                  <Button size="sm" kind="danger" onClick={() => setDel(g.name)}>{t("common.delete")}</Button>
                </div>
              ),
          },
        ]}
      />
      {form.open && <GroupForm cluster={cluster} initial={form.group} onClose={() => setForm({ open: false })} onStarted={onJob} />}
      <Confirm
        open={!!del}
        title={t("clusters.deleteGroupTitle")}
        body={t("clusters.deleteGroupBody", { name: del })}
        confirmLabel={t("common.delete")}
        danger
        onConfirm={() => void remove()}
        onClose={() => setDel(null)}
      />
    </Card>
  );
}

function NodesCard({ cluster }: { cluster: Cluster }) {
  const { t } = useTranslation();
  const nodes = useLoad(() => clusterApi.nodes(cluster.id), `nodes-${cluster.id}`);
  usePoll(nodes.reload, LIST_POLL_MS);
  return (
    <Card title={t("clusters.nodes")} flush>
      <Table
        rowKey={(n) => n.id}
        rows={nodes.data ?? []}
        loading={nodes.loading && !nodes.data}
        error={nodes.error}
        columns={[
          { key: "i", title: t("clusters.instanceId"), render: (n) => <span className="tp-mono">{n.id}</span> },
          { key: "g", title: t("clusters.group"), render: (n) => n.group },
          { key: "t", title: t("models.instanceType"), render: (n) => n.type },
          { key: "s", title: t("common.status"), render: (n) => <StatusTag status={n.status} /> },
          { key: "m", title: t("common.detail"), render: (n) => <span className="v2-muted">{n.message ?? ""}</span> },
          { key: "l", title: t("clusters.launched"), render: (n) => dateTime(n.launch_time) },
        ]}
      />
    </Card>
  );
}

function CostCard({ cluster, onSaved }: { cluster: Cluster; onSaved: () => void }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const ledger = useLoad(() => clusterApi.ledger(cluster.id), `ledger-${cluster.id}`);
  const [idle, setIdle] = useState(cluster.idle_policy.idle_minutes ?? 30);
  const [budget, setBudget] = useState(cluster.idle_policy.budget_usd ? String(cluster.idle_policy.budget_usd) : "");
  const save = async () => {
    try {
      const r = await clusterApi.setPolicy(cluster.id, { idle_minutes: idle, budget_usd: budget ? Number(budget) : null });
      toast("success", r.applied_to_cluster ? t("clusters.policySaved") : t("clusters.policySavedLocal"));
      onSaved();
    } catch (err) {
      toast("error", errorMessage(err));
    }
  };
  const l = ledger.data;
  return (
    <div className="tp-stack">
      <div className="tp-grid c3">
        <Kpi label={t("clusters.spent")} value={money(l?.total_cost_usd ?? 0)} sub={t("clusters.spentSub")} />
        <Kpi label={t("clusters.budget")} value={cluster.idle_policy.budget_usd ? money(cluster.idle_policy.budget_usd) : "—"} />
        <Kpi label={t("clusters.lastCheck")} value={l?.updated_at_epoch ? dateTime(l.updated_at_epoch) : "—"} />
      </div>
      {l?.alert && <Alert tone="error">{l.alert}</Alert>}
      <Card title={t("clusters.guardrails")} sub={t("clusters.guardrailsDesc")} end={<Button kind="primary" size="sm" onClick={() => void save()}>{t("common.save")}</Button>}>
        <div className="v2-form cols-2">
          <Field label={t("clusters.idleMinutes")} hint={t("clusters.idleHint")}>
            <input className="v2-input" type="number" min={5} value={idle} onChange={(e) => setIdle(Number(e.target.value) || 30)} />
          </Field>
          <Field label={t("clusters.budget")} hint={t("clusters.budgetHint")}>
            <input className="v2-input" type="number" min={0} value={budget} onChange={(e) => setBudget(e.target.value)} />
          </Field>
        </div>
      </Card>
      <Card title={t("clusters.ledger")} flush>
        <Table
          rowKey={([name]) => name}
          rows={Object.entries(l?.groups ?? {})}
          columns={[
            { key: "g", title: t("clusters.group"), render: ([name]) => name },
            { key: "h", title: t("clusters.nodeHours"), render: ([, g]) => num(g.node_hours) },
            { key: "c", title: t("clusters.cost"), render: ([, g]) => money(g.cost_usd) },
            { key: "i", title: t("clusters.idleSince"), render: ([, g]) => (g.idle_since ? dateTime(g.idle_since) : "—") },
          ]}
        />
      </Card>
      {(l?.actions?.length ?? 0) > 0 && (
        <Card title={t("clusters.actions")} flush>
          <Table
            rowKey={(a) => `${a.at}-${a.group}`}
            rows={[...(l?.actions ?? [])].reverse()}
            columns={[
              { key: "t", title: t("common.time"), render: (a) => dateTime(a.at) },
              { key: "g", title: t("clusters.group"), render: (a) => a.group },
              { key: "a", title: t("clusters.action"), render: (a) => a.action },
              { key: "r", title: t("clusters.reason"), render: (a) => a.reason },
            ]}
          />
        </Card>
      )}
    </div>
  );
}

function PlansCard({ cluster }: { cluster: Cluster }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const plans = useLoad(() => plansApi.list(cluster.region), `plans-${cluster.region}`);
  const [itype, setItype] = useState("p5.48xlarge");
  const [count, setCount] = useState(1);
  const [hours, setHours] = useState(24);
  const [offers, setOffers] = useState<PlanOffering[] | null>(null);
  const [buy, setBuy] = useState<PlanOffering | null>(null);
  const [busy, setBusy] = useState(false);
  const search = async () => {
    setBusy(true);
    try {
      setOffers(await plansApi.search({ region: cluster.region, instance_type: itype, count, duration_hours: hours }));
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  const purchase = async () => {
    if (!buy) return;
    setBusy(true);
    try {
      await plansApi.buy({ region: cluster.region, offering_id: buy.id, name: `tp-${cluster.name}-${Date.now() % 100000}`, confirm_upfront_fee: buy.upfront_fee });
      toast("success", t("plans.bought"));
      plans.reload();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
      setBuy(null);
    }
  };
  return (
    <div className="tp-stack">
      <Card title={t("plans.search")} sub={t("plans.searchDesc")}>
        <div className="v2-form cols-2">
          <Field label={t("models.instanceType")}>
            <Select value={itype} onChange={setItype} options={["p4d.24xlarge", "p4de.24xlarge", "p5.48xlarge", "p5e.48xlarge", "p5en.48xlarge", "p6-b200.48xlarge", "p6-b300.48xlarge"].map((v) => ({ value: v, label: `ml.${v}` }))} />
          </Field>
          <Field label={t("clusters.count")}>
            <input className="v2-input" type="number" min={1} value={count} onChange={(e) => setCount(Number(e.target.value) || 1)} />
          </Field>
          <Field label={t("plans.duration")}>
            <input className="v2-input" type="number" min={24} step={24} value={hours} onChange={(e) => setHours(Number(e.target.value) || 24)} />
          </Field>
        </div>
        <div className="tp-row end" style={{ marginTop: 12 }}>
          <Button kind="primary" disabled={busy} onClick={() => void search()}>{t("plans.searchBtn")}</Button>
        </div>
        {offers && (
          <Table
            rowKey={(o) => o.id}
            rows={offers}
            empty={t("plans.none")}
            columns={[
              { key: "s", title: t("plans.start"), render: (o) => dateTime(o.start) },
              { key: "e", title: t("plans.end"), render: (o) => dateTime(o.end) },
              { key: "az", title: "AZ", render: (o) => o.az },
              { key: "f", title: t("plans.fee"), render: (o) => `${money(o.upfront_fee)} ${o.currency}` },
              { key: "h", title: t("plans.hourly"), render: (o) => money(o.upfront_fee / Math.max(1, o.duration_hours * o.count)) },
              { key: "a", title: "", render: (o) => <Button size="sm" kind="danger" onClick={() => setBuy(o)}>{t("plans.buy")}</Button> },
            ]}
          />
        )}
      </Card>
      <Card title={t("plans.mine")} flush>
        <Table
          rowKey={(p) => p.arn}
          rows={plans.data ?? []}
          loading={plans.loading}
          error={plans.error}
          columns={[
            { key: "n", title: t("common.name"), render: (p) => p.name },
            { key: "s", title: t("common.status"), render: (p) => <StatusTag status={p.status} /> },
            { key: "st", title: t("plans.start"), render: (p) => dateTime(p.start) },
            { key: "e", title: t("plans.end"), render: (p) => dateTime(p.end) },
            { key: "t", title: t("models.instanceType"), render: (p) => <span className="tp-mono">{p.instance_type ?? "—"}</span> },
            { key: "z", title: t("plans.az"), render: (p) => p.az ?? p.az_id ?? "—" },
            { key: "i", title: t("plans.availableCol"), render: (p) => `${p.available ?? "?"} / ${p.instances}` },
          ]}
        />
      </Card>
      <Confirm
        open={!!buy}
        title={t("plans.confirmTitle")}
        body={buy ? t("plans.confirmBody", { fee: money(buy.upfront_fee), start: dateTime(buy.start), end: dateTime(buy.end) }) : ""}
        confirmLabel={t("plans.confirmBuy")}
        danger
        busy={busy}
        onConfirm={() => void purchase()}
        onClose={() => setBuy(null)}
      />
    </div>
  );
}

function ClusterDetail({ id }: { id: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const toast = useV2Toast();
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") ?? "overview";
  const c = useLoad(() => clusterApi.get(id), id);
  const [jobId, setJobId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [confirmName, setConfirmName] = useState("");
  const live = c.data && (LIVE_STATUSES.has(c.data.status) || c.data.job?.status === "running");
  usePoll(c.reload, live ? DETAIL_POLL_MS * 2 : LIST_POLL_MS * 2);

  if (!c.data) return c.error ? <Alert tone="error">{c.error}</Alert> : <Spin />;
  const cl = c.data;
  const activeJob = jobId ?? cl.job?.id ?? null;
  const setTab = (v: string) => setParams({ view: "detail", id, tab: v });
  const repair = async () => {
    try {
      setJobId((await clusterApi.repair(id)).job_id);
    } catch (err) {
      toast("error", errorMessage(err));
    }
  };
  const remove = async () => {
    try {
      setJobId((await clusterApi.remove(id, confirmName)).job_id);
      setDeleting(false);
      c.reload();
    } catch (err) {
      toast("error", errorMessage(err));
    }
  };

  return (
    <>
      <FlowHeader
        title={<>{cl.name} <StatusTag status={cl.status} /></>}
        onBack={() => navigate("/clusters")}
        end={<Button kind="danger" size="sm" onClick={() => setDeleting(true)} disabled={cl.status === "deleting"}>{t("clusters.delete")}</Button>}
      />
      <div style={{ marginBottom: 16 }}>
        <SubTabs
          value={tab}
          onChange={setTab}
          tabs={[
            { value: "overview", label: t("clusters.tabOverview") },
            { value: "groups", label: t("clusters.tabGroups") },
            { value: "nodes", label: t("clusters.tabNodes") },
            { value: "cost", label: t("clusters.tabCost") },
            { value: "plans", label: t("clusters.tabPlans") },
          ]}
        />
      </div>
      <div className="tp-stack">
        {activeJob && <JobPanel jobId={activeJob} onDone={() => c.reload()} />}
        {cl.live?.failure && <Alert tone="error">{cl.live.failure}</Alert>}
        {tab === "overview" && (
          <>
            <Card title={t("clusters.overview")}>
              <Descriptions
                items={[
                  { label: t("common.region"), value: cl.region },
                  { label: "HyperPod", value: <span className="tp-mono">{cl.hyperpod_name ?? "—"}</span> },
                  { label: t("clusters.liveStatus"), value: cl.live ? <StatusTag status={cl.live.status} /> : "—" },
                  { label: "EKS", value: <span className="tp-mono">{cl.eks_name ?? "—"} {String(cl.params.eks_version ?? "")}</span> },
                  { label: "VPC", value: <span className="tp-mono">{cl.network.vpc_id ?? "—"}</span> },
                  { label: t("clusters.subnets"), value: <span className="tp-mono">{(cl.network.private_subnets ?? []).join(", ") || "—"}</span> },
                  { label: t("clusters.azs"), value: (cl.network.azs ?? []).join(", ") || "—" },
                  { label: "FSx", value: <span className="tp-mono">{cl.network.fsx_id ?? "—"}</span> },
                  { label: t("clusters.sgAcr"), value: <span className="tp-mono">{cl.network.sg_acr ?? "—"}</span> },
                  { label: t("clusters.provisioning"), value: cl.live?.node_provisioning_mode ?? "—" },
                  { label: "CloudFormation", value: <span className="tp-mono">{cl.cfn_stack ?? "—"}</span> },
                  { label: t("clusters.source"), value: t(`clusters.source_${cl.source}`) },
                ]}
              />
            </Card>
            <ComponentsCard cluster={cl} onRepair={() => void repair()} />
          </>
        )}
        {tab === "groups" && (
          <>
            <GroupsCard cluster={cl} onJob={(j) => { setJobId(j); c.reload(); }} />
            <NodeGroupsCard cluster={cl} onJob={(j) => { setJobId(j); c.reload(); }} />
          </>
        )}
        {tab === "nodes" && <NodesCard cluster={cl} />}
        {tab === "cost" && <CostCard cluster={cl} onSaved={c.reload} />}
        {tab === "plans" && <PlansCard cluster={cl} />}
      </div>
      <Modal
        open={deleting}
        title={t("clusters.deleteTitle")}
        onClose={() => setDeleting(false)}
        footer={
          <>
            <Button onClick={() => setDeleting(false)}>{t("v2.common.cancel")}</Button>
            <Button kind="danger" disabled={confirmName !== cl.name} onClick={() => void remove()}>{t("clusters.delete")}</Button>
          </>
        }
      >
        <Alert tone="warn">{cl.source === "create" ? t("clusters.deleteBodyCreate") : t("clusters.deleteBodyImport")}</Alert>
        <Field label={t("clusters.typeName", { name: cl.name })}>
          <input className="v2-input" value={confirmName} onChange={(e) => setConfirmName(e.target.value)} />
        </Field>
      </Modal>
    </>
  );
}

export default function ClustersPage() {
  const [params] = useSearchParams();
  const view = params.get("view");
  const id = params.get("id");
  if (view === "new") return <CreateCluster />;
  if (view === "import") return <ImportCluster />;
  if (view === "detail" && id) return <ClusterDetail id={id} />;
  return <ClusterList />;
}
