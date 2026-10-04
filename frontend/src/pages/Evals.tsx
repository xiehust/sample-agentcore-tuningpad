import { useState } from "react";
import { useTranslation } from "react-i18next";

import { JobPanel } from "../components/JobPanel";
import { StatusTag } from "../components/StatusTag";
import { agentApi, datasetApi, errorMessage, servingApi } from "../lib/api";
import { dateTime, num } from "../lib/format";
import { LIST_POLL_MS, usePoll } from "../lib/poll";
import { useLoad, useV2Toast } from "../v2/hooks";
import { Alert, Button, Card, Field, Modal, PageHeader, Select, Table } from "../v2/ui";

export default function EvalsPage() {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const list = useLoad(servingApi.evals, "evals");
  const endpoints = useLoad(servingApi.endpoints, "endpoints");
  const agents = useLoad(agentApi.list, "agents");
  const datasets = useLoad(datasetApi.list, "datasets");
  usePoll(list.reload, LIST_POLL_MS);
  const [open, setOpen] = useState(false);
  const [job, setJob] = useState<string | null>(null);
  const [form, setForm] = useState({ name: "eval", endpoint_id: "", agent_runtime_id: "", dataset_id: "", split: "val", limit: 200 });
  const ready = (endpoints.data ?? []).filter((e) => e.status === "ready");
  const ep = ready.find((e) => e.id === form.endpoint_id);
  const runtimes = (agents.data ?? []).flatMap((a) => a.runtimes.filter((r) => r.status === "ready" && r.cluster_id && (!ep || r.cluster_id === ep.cluster_id)).map((r) => ({ a, r })));
  const ds = (datasets.data ?? []).find((d) => d.id === form.dataset_id);
  const epName = (id: string) => endpoints.data?.find((e) => e.id === id)?.served_model_name ?? id;
  const submit = async () => {
    try {
      const r = await servingApi.createEval(form);
      setJob(r.job_id);
      setOpen(false);
      list.reload();
    } catch (err) {
      toast("error", errorMessage(err));
    }
  };
  const rows = list.data ?? [];
  const best = Math.max(...rows.map((e) => e.summary.mean_reward ?? -1));
  return (
    <>
      <PageHeader title={t("pages.evals.title")} desc={t("pages.evals.desc")} end={<Button kind="primary" onClick={() => setOpen(true)}>{t("evals.create")}</Button>} />
      <Alert tone="info">{t("evals.how")}</Alert>
      <div style={{ height: 16 }} />
      {job && <div style={{ marginBottom: 16 }}><JobPanel jobId={job} onDone={() => list.reload()} /></div>}
      <Card title={t("evals.compare")} flush>
        <Table
          rowKey={(e) => e.id}
          rows={rows}
          loading={list.loading && !list.data}
          error={list.error}
          empty={t("evals.empty")}
          columns={[
            { key: "n", title: t("common.name"), render: (e) => e.name },
            { key: "m", title: t("inference.served"), render: (e) => <span className="tp-mono">{epName(e.endpoint_id)}</span> },
            { key: "s", title: t("common.status"), render: (e) => <StatusTag status={e.status} /> },
            { key: "d", title: t("evals.data"), render: (e) => `${datasets.data?.find((d) => d.id === e.dataset_id)?.name ?? e.dataset_id} · ${e.split} · ${e.limit}` },
            { key: "r", title: t("evals.meanReward"), render: (e) => <b style={{ color: e.summary.mean_reward === best && best >= 0 ? "var(--v2-success)" : undefined }}>{num(e.summary.mean_reward, 3)}</b> },
            { key: "f", title: t("evals.failed"), render: (e) => num(e.summary.acr_failed_rate, 3) },
            { key: "c", title: t("evals.count"), render: (e) => `${e.summary.scored ?? 0}/${e.summary.n ?? 0}` },
            { key: "t", title: t("common.created"), render: (e) => dateTime(e.created_at) },
            { key: "a", title: "", render: (e) => (e.job ? <Button size="sm" onClick={() => setJob(e.job!.id)}>{t("job.showLog")}</Button> : null) },
          ]}
        />
      </Card>
      <Modal open={open} title={t("evals.create")} onClose={() => setOpen(false)} footer={<><Button onClick={() => setOpen(false)}>{t("v2.common.cancel")}</Button><Button kind="primary" disabled={!form.endpoint_id || !form.agent_runtime_id || !form.dataset_id} onClick={() => void submit()}>{t("evals.run")}</Button></>}>
        <div className="v2-form cols-2">
          <Field label={t("common.name")}><input className="v2-input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></Field>
          <Field label={t("evals.endpoint")}><Select value={form.endpoint_id} onChange={(v) => setForm({ ...form, endpoint_id: v })} placeholder="—" options={ready.map((e) => ({ value: e.id, label: e.served_model_name }))} /></Field>
          <Field label={t("runs.runtime")}><Select value={form.agent_runtime_id} onChange={(v) => setForm({ ...form, agent_runtime_id: v })} placeholder="—" options={runtimes.map(({ a, r }) => ({ value: r.id, label: a.name }))} /></Field>
          <Field label={t("evals.data")}><Select value={form.dataset_id} onChange={(v) => setForm({ ...form, dataset_id: v })} placeholder="—" options={(datasets.data ?? []).filter((d) => d.status === "ready").map((d) => ({ value: d.id, label: d.name }))} /></Field>
          {ds && <Field label={t("runs.valSplit")}><Select value={form.split} onChange={(v) => setForm({ ...form, split: v })} options={Object.keys(ds.splits).map((k) => ({ value: k, label: `${k} (${ds.splits[k].rows})` }))} /></Field>}
          <Field label={t("evals.limit")}><input className="v2-input" type="number" min={1} value={form.limit} onChange={(e) => setForm({ ...form, limit: Number(e.target.value) || 1 })} /></Field>
        </div>
      </Modal>
    </>
  );
}
