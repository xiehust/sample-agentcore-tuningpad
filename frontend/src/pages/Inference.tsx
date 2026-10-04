import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";

import { JobPanel } from "../components/JobPanel";
import { StatusTag } from "../components/StatusTag";
import { clusterApi, errorMessage, servingApi } from "../lib/api";
import { dateTime } from "../lib/format";
import { LIST_POLL_MS, usePoll } from "../lib/poll";
import { useLoad, useV2Toast } from "../v2/hooks";
import { Alert, Button, Card, Confirm, Empty, Field, FlowHeader, OptionCard, PageHeader, Select, Table } from "../v2/ui";

function NewEndpoint() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const toast = useV2Toast();
  const [params] = useSearchParams();
  const clusters = useLoad(clusterApi.list, "clusters");
  const exports = useLoad(servingApi.exports, "exports");
  const [source, setSource] = useState<"hf" | "export">(params.get("export") ? "export" : "hf");
  const [exportId, setExportId] = useState(params.get("export") ?? "");
  const [modelId, setModelId] = useState("Qwen/Qwen3.5-2B");
  const [name, setName] = useState("eval-endpoint");
  const [clusterId, setClusterId] = useState("");
  const [group, setGroup] = useState("gpu-p5");
  const [tp, setTp] = useState(1);
  const [maxLen, setMaxLen] = useState("");
  const [busy, setBusy] = useState(false);
  const c = useLoad(() => (clusterId ? clusterApi.get(clusterId) : Promise.resolve(null)), clusterId || "none");
  const groups = [
    ...(c.data?.live?.instance_groups ?? []).filter((g) => g.name !== "system").map((g) => ({ ...g, src: "HyperPod" })),
    ...(c.data?.live?.node_groups ?? []).map((g) => ({ ...g, src: "EC2" })),
  ];
  const submit = async () => {
    setBusy(true);
    try {
      await servingApi.createEndpoint({ name, cluster_id: clusterId, instance_group: group, source, model_id: source === "hf" ? modelId : undefined, export_id: source === "export" ? exportId : undefined, tp, replicas: 1, max_model_len: maxLen ? Number(maxLen) : null });
      navigate("/inference");
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <FlowHeader title={t("inference.create")} onBack={() => navigate("/inference")} end={<Button kind="primary" disabled={busy || !clusterId || !group} onClick={() => void submit()}>{t("inference.deploy")}</Button>} />
      <div className="tp-stack">
        <Card title={t("inference.model")}>
          <div className="tp-tiles">
            <OptionCard title={t("inference.fromHf")} desc={t("inference.fromHfDesc")} on={source === "hf"} onClick={() => setSource("hf")} />
            <OptionCard title={t("inference.fromExport")} desc={t("inference.fromExportDesc")} on={source === "export"} onClick={() => setSource("export")} />
          </div>
          <div className="v2-form cols-2" style={{ marginTop: 16 }}>
            {source === "hf" ? (
              <Field label={t("models.hfId")}><input className="v2-input tp-mono" value={modelId} onChange={(e) => setModelId(e.target.value)} /></Field>
            ) : (
              <Field label={t("inference.export")}>
                <Select value={exportId} onChange={setExportId} placeholder="—" options={(exports.data ?? []).filter((e) => e.status === "succeeded").map((e) => ({ value: e.id, label: `${e.run_id} · step ${e.step}` }))} />
              </Field>
            )}
            <Field label={t("common.name")}><input className="v2-input" value={name} onChange={(e) => setName(e.target.value)} /></Field>
            <Field label={t("inference.maxLen")}><input className="v2-input" type="number" value={maxLen} placeholder={t("runs.auto")} onChange={(e) => setMaxLen(e.target.value)} /></Field>
          </div>
        </Card>
        <Card title={t("inference.placement")}>
          <div className="v2-form cols-2">
            <Field label={t("nav.clusters")}>
              <Select value={clusterId} onChange={setClusterId} placeholder="—" options={(clusters.data ?? []).filter((x) => x.status === "ready").map((x) => ({ value: x.id, label: x.name }))} />
            </Field>
            <Field label={t("clusters.group")} hint={t("inference.groupHint")}>
              <Select value={group} onChange={setGroup} placeholder="—" options={groups.map((g) => ({ value: g.name, label: `${g.name} · ${g.src} · ${g.instance_type} · ${g.current}/${g.target}` }))} />
            </Field>
            <Field label={t("inference.tp")}><input className="v2-input" type="number" min={1} max={8} value={tp} onChange={(e) => setTp(Number(e.target.value) || 1)} /></Field>
          </div>
          <Alert tone="info">{t("inference.networkInfo")}</Alert>
        </Card>
      </div>
    </>
  );
}

function EndpointList() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const list = useLoad(servingApi.endpoints, "endpoints");
  const [job, setJob] = useState<string | null>(null);
  const [del, setDel] = useState<string | null>(null);
  usePoll(list.reload, LIST_POLL_MS);
  return (
    <>
      <PageHeader title={t("pages.inference.title")} desc={t("pages.inference.desc")} end={<Button kind="primary" onClick={() => navigate("/inference?view=new")}>{t("inference.create")}</Button>} />
      {job && <div style={{ marginBottom: 16 }}><JobPanel jobId={job} onDone={() => list.reload()} /></div>}
      {list.data?.length === 0 ? <Empty>{t("inference.empty")}</Empty> : (
        <Card flush>
          <Table
            rowKey={(e) => e.id}
            rows={list.data ?? []}
            loading={list.loading && !list.data}
            error={list.error}
            columns={[
              { key: "n", title: t("common.name"), render: (e) => e.name },
              { key: "s", title: t("common.status"), render: (e) => <StatusTag status={e.status} /> },
              { key: "m", title: t("inference.served"), render: (e) => <span className="tp-mono">{e.served_model_name}</span> },
              { key: "u", title: "URL", render: (e) => <span className="tp-mono">{e.url ?? e.job?.stages[0]?.detail ?? "—"}</span> },
              { key: "g", title: t("clusters.group"), render: (e) => `${e.instance_group} · TP ${e.tp}` },
              { key: "c", title: t("common.created"), render: (e) => dateTime(e.created_at) },
              { key: "a", title: "", render: (e) => (
                <div className="tp-row">
                  {e.job && <Button size="sm" onClick={() => setJob(e.job!.id)}>{t("job.showLog")}</Button>}
                  <Button size="sm" kind="danger" onClick={() => setDel(e.id)}>{t("common.delete")}</Button>
                </div>
              ) },
            ]}
          />
        </Card>
      )}
      {list.data?.some((e) => e.error) && <Alert tone="error">{list.data.find((e) => e.error)?.error}</Alert>}
      <Confirm open={!!del} title={t("inference.deleteTitle")} body={t("inference.deleteBody")} confirmLabel={t("common.delete")} danger onConfirm={() => { void servingApi.deleteEndpoint(del!).then((r) => setJob(r.job_id)); setDel(null); }} onClose={() => setDel(null)} />
    </>
  );
}

export default function InferencePage() {
  const [params] = useSearchParams();
  return params.get("view") === "new" ? <NewEndpoint /> : <EndpointList />;
}
