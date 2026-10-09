import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";

import { JobPanel } from "../components/JobPanel";
import { StatusTag } from "../components/StatusTag";
import { ApiError, agentApi, datasetApi, errorMessage, obsApi, servingApi } from "../lib/api";
import { dateTime, num } from "../lib/format";
import { LIST_POLL_MS, usePoll } from "../lib/poll";
import { useLoad, useV2Toast } from "../v2/hooks";
import { Alert, Button, Card, Confirm, Field, Modal, PageHeader, Select, Table } from "../v2/ui";
import EvalDetail from "./EvalDetail";

/** `detail.region` of an `eval.observability_off` error, if the server sent one. */
function regionOf(detail: unknown): string | undefined {
  if (detail !== null && typeof detail === "object" && "region" in detail && typeof detail.region === "string") return detail.region;
  return undefined;
}

export default function EvalsPage() {
  const [params] = useSearchParams();
  const view = params.get("view");
  const id = params.get("id");
  if (view === "detail" && id) return <EvalDetail id={id} />;
  return <EvalList />;
}

function EvalList() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const toast = useV2Toast();
  const list = useLoad(servingApi.evals, "evals");
  const endpoints = useLoad(servingApi.endpoints, "endpoints");
  const agents = useLoad(agentApi.list, "agents");
  const datasets = useLoad(datasetApi.list, "datasets");
  usePoll(list.reload, LIST_POLL_MS);
  const [open, setOpen] = useState(false);
  const [job, setJob] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Region whose CloudWatch Transaction Search must be enabled before a trace-recording eval
  const [tsRegion, setTsRegion] = useState<string | null>(null);
  const [form, setForm] = useState({ name: "eval", endpoint_id: "", agent_runtime_id: "", dataset_id: "", split: "val", limit: 200, observe: false });
  const ready = (endpoints.data ?? []).filter((e) => e.status === "ready");
  const ep = ready.find((e) => e.id === form.endpoint_id);
  const runtimes = (agents.data ?? []).flatMap((a) => a.runtimes.filter((r) => r.status === "ready" && r.cluster_id && (!ep || r.cluster_id === ep.cluster_id)).map((r) => ({ a, r })));
  const rt = runtimes.find(({ r }) => r.id === form.agent_runtime_id)?.r;
  const ds = (datasets.data ?? []).find((d) => d.id === form.dataset_id);
  const epName = (id: string) => endpoints.data?.find((e) => e.id === id)?.served_model_name ?? id;
  const create = async () => {
    const r = await servingApi.createEval(form);
    setJob(r.job_id);
    setOpen(false);
    list.reload();
  };
  const submit = async () => {
    setBusy(true);
    try {
      await create();
    } catch (err) {
      // trace recording needs Transaction Search in the runtime's Region: ask before enabling it
      const region = err instanceof ApiError && err.code === "eval.observability_off" ? (regionOf(err.detail) ?? rt?.region) : undefined;
      if (region) setTsRegion(region);
      else toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  const enableAndSubmit = async () => {
    if (!tsRegion || busy) return;
    setBusy(true);
    try {
      await obsApi.enableTransactionSearch(tsRegion);
      await create();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
      setTsRegion(null);
    }
  };
  const obsHint = () => {
    const st = rt?.obs?.status;
    if (st === "ready") return t("evals.observeReuse");
    if (st === "deploying") return t("evals.observeDeploying");
    if (st === "failed") return t("evals.observeFailed", { error: rt?.obs?.error ?? "—" });
    return t("evals.observeHint");
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
            { key: "n", title: t("common.name"), render: (e) => <button type="button" className="v2-link" onClick={() => navigate(`/evals?view=detail&id=${e.id}`)}>{e.name}</button> },
            { key: "m", title: t("inference.served"), render: (e) => <span className="tp-mono">{epName(e.endpoint_id)}</span> },
            { key: "s", title: t("common.status"), render: (e) => <StatusTag status={e.status} /> },
            { key: "d", title: t("evals.data"), render: (e) => `${datasets.data?.find((d) => d.id === e.dataset_id)?.name ?? e.dataset_id} · ${e.split} · ${e.limit}` },
            { key: "r", title: t("evals.meanReward"), render: (e) => <b style={{ color: e.summary.mean_reward === best && best >= 0 ? "var(--v2-success)" : undefined }}>{num(e.summary.mean_reward, 3)}</b> },
            { key: "f", title: t("evals.failed"), render: (e) => num(e.summary.acr_failed_rate, 3) },
            { key: "tr", title: t("evals.truncated"), render: (e) => (e.summary.n ? `${e.summary.truncated ?? 0}/${e.summary.n}` : "—") },
            { key: "c", title: t("evals.count"), render: (e) => `${e.summary.scored ?? 0}/${e.summary.n ?? 0}` },
            { key: "t", title: t("common.created"), render: (e) => dateTime(e.created_at) },
            { key: "a", title: "", render: (e) => (
              <div className="tp-row">
                <Button size="sm" onClick={() => navigate(`/evals?view=detail&id=${e.id}`)} testId={`tp-eval-open-${e.id}`}>{t("evals.detail.open")}</Button>
                {e.job ? <Button size="sm" onClick={() => setJob(e.job!.id)}>{t("job.showLog")}</Button> : null}
              </div>
            ) },
          ]}
        />
      </Card>
      {/* hidden (not reset) while the Transaction Search confirm is up: one dialog owns Escape */}
      <Modal open={open && tsRegion === null} title={t("evals.create")} onClose={() => setOpen(false)} footer={<><Button onClick={() => setOpen(false)}>{t("v2.common.cancel")}</Button><Button kind="primary" disabled={busy || !form.endpoint_id || !form.agent_runtime_id || !form.dataset_id} onClick={() => void submit()} testId="tp-eval-submit">{t("evals.run")}</Button></>}>
        <div className="v2-form cols-2">
          <Field label={t("common.name")}><input className="v2-input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></Field>
          <Field label={t("evals.endpoint")}><Select value={form.endpoint_id} onChange={(v) => setForm({ ...form, endpoint_id: v })} placeholder="—" options={ready.map((e) => ({ value: e.id, label: e.served_model_name }))} /></Field>
          <Field label={t("runs.runtime")}><Select value={form.agent_runtime_id} onChange={(v) => setForm({ ...form, agent_runtime_id: v })} placeholder="—" options={runtimes.map(({ a, r }) => ({ value: r.id, label: a.name }))} /></Field>
          <Field label={t("evals.data")}><Select value={form.dataset_id} onChange={(v) => setForm({ ...form, dataset_id: v })} placeholder="—" options={(datasets.data ?? []).filter((d) => d.status === "ready").map((d) => ({ value: d.id, label: d.name }))} /></Field>
          {ds && <Field label={t("runs.valSplit")}><Select value={form.split} onChange={(v) => setForm({ ...form, split: v })} options={Object.keys(ds.splits).map((k) => ({ value: k, label: `${k} (${ds.splits[k].rows})` }))} /></Field>}
          <Field label={t("evals.limit")}><input className="v2-input" type="number" min={1} value={form.limit} onChange={(e) => setForm({ ...form, limit: Number(e.target.value) || 1 })} /></Field>
          <Field label={t("evals.observe")} hint={obsHint()} full>
            <label className="v2-check">
              <input type="checkbox" checked={form.observe} onChange={(e) => setForm({ ...form, observe: e.target.checked })} data-testid="tp-eval-observe" />
              {t("evals.observeLabel")}
            </label>
          </Field>
        </div>
      </Modal>
      <Confirm
        open={tsRegion !== null}
        title={t("evals.tsConfirmTitle")}
        body={
          <div className="tp-stack" style={{ gap: 8 }} data-testid="tp-eval-ts-confirm">
            <p style={{ margin: 0 }}>{t("evals.tsConfirmBody", { region: tsRegion ?? "" })}</p>
            <p style={{ margin: 0 }}>{t("evals.tsConfirmScope", { region: tsRegion ?? "" })}</p>
            <p className="v2-muted" style={{ margin: 0 }}>{t("evals.tsConfirmOff")}</p>
          </div>
        }
        confirmLabel={t("evals.tsConfirmOk")}
        busy={busy}
        onConfirm={() => void enableAndSubmit()}
        onClose={() => {
          if (!busy) setTsRegion(null);
        }}
      />
    </>
  );
}
