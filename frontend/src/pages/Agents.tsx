import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";

import { JobPanel } from "../components/JobPanel";
import { StatusTag } from "../components/StatusTag";
import { agentApi, clusterApi, errorMessage, type AgentView, type Template } from "../lib/api";
import { useLocalized } from "../lib/localized";
import { dateTime } from "../lib/format";
import { DETAIL_POLL_MS, LIST_POLL_MS, usePoll } from "../lib/poll";
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
  OptionCard,
  PageHeader,
  Select,
  Spin,
  Steps,
  Table,
  Tag,
} from "../v2/ui";

function AgentList() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const list = useLoad(agentApi.list, "agents");
  usePoll(list.reload, LIST_POLL_MS);
  return (
    <>
      <PageHeader
        title={t("pages.agents.title")}
        desc={t("pages.agents.desc")}
        end={<Button kind="primary" onClick={() => navigate("/agents?view=new")} testId="tp-agent-new">{t("agents.create")}</Button>}
      />
      {list.data?.length === 0 ? (
        <Empty action={<Button kind="primary" onClick={() => navigate("/agents?view=new")}>{t("agents.create")}</Button>}>{t("agents.empty")}</Empty>
      ) : (
        <Card flush>
          <Table
            rowKey={(a) => a.id}
            rows={list.data ?? []}
            loading={list.loading && !list.data}
            error={list.error}
            testId="tp-agent-table"
            columns={[
              { key: "n", title: t("common.name"), render: (a) => <button type="button" className="v2-link" onClick={() => navigate(`/agents?view=detail&id=${a.id}`)}>{a.name}</button> },
              { key: "s", title: t("common.status"), render: (a) => <StatusTag status={a.status} /> },
              { key: "src", title: t("agents.source"), render: (a) => t(`agents.source_${a.source}`) + (a.template_id ? ` · ${a.template_id}` : "") },
              {
                key: "rt",
                title: t("agents.runtimes"),
                render: (a) => (
                  <div className="tp-row">
                    {a.runtimes.map((r) => (
                      <Tag key={r.id} tone={r.status === "ready" ? "green" : r.status === "failed" ? "red" : "gray"}>
                        {r.network_mode === "VPC" ? t("agents.vpcRuntime") : t("agents.smokeRuntime")}
                      </Tag>
                    ))}
                  </div>
                ),
              },
              { key: "c", title: t("common.created"), render: (a) => dateTime(a.created_at) },
            ]}
          />
        </Card>
      )}
    </>
  );
}

function ParamFields({ tpl, values, onChange }: { tpl: Template; values: Record<string, unknown>; onChange: (v: Record<string, unknown>) => void }) {
  const loc = useLocalized();
  if (!tpl.params.length) return null;
  return (
    <div className="v2-form">
      {tpl.params.map((p) => (
        <Field key={p.key} label={loc(p.label)} full={p.type === "text"}>
          {p.type === "text" ? (
            <textarea className="v2-textarea" rows={4} value={String(values[p.key] ?? p.default)} onChange={(e) => onChange({ ...values, [p.key]: e.target.value })} />
          ) : p.type === "select" ? (
            <Select value={String(values[p.key] ?? p.default)} options={(p.options ?? []).map((o) => ({ value: o, label: o }))} onChange={(v) => onChange({ ...values, [p.key]: v })} />
          ) : (
            <input className="v2-input" type="number" min={p.min} max={p.max} step="any" value={String(values[p.key] ?? p.default)} onChange={(e) => onChange({ ...values, [p.key]: Number(e.target.value) })} />
          )}
        </Field>
      ))}
    </div>
  );
}

function CreateAgent() {
  const { t } = useTranslation();
  const loc = useLocalized();
  const navigate = useNavigate();
  const toast = useV2Toast();
  const templates = useLoad(agentApi.templates, "templates");
  const [step, setStep] = useState(0);
  const [source, setSource] = useState<"template" | "upload" | "image">("template");
  const [templateId, setTemplateId] = useState("gsm8k_math");
  const [params, setParams] = useState<Record<string, unknown>>({});
  const [name, setName] = useState("gsm8k-math");
  const [region, setRegion] = useState("us-east-1");
  const [imageUri, setImageUri] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [smoke, setSmoke] = useState("[]");
  const [busy, setBusy] = useState(false);
  const tpl = templates.data?.find((x) => x.id === templateId);

  const submit = async () => {
    setBusy(true);
    try {
      let payloads: unknown[] = [];
      if (source !== "template") {
        payloads = JSON.parse(smoke || "[]");
        if (!Array.isArray(payloads)) throw new Error(t("agents.smokeJsonError"));
      }
      let id: string;
      if (source === "upload") {
        if (!file) throw new Error(t("agents.zipRequired"));
        const form = new FormData();
        form.append("name", name);
        form.append("file", file);
        form.append("smoke_payloads", JSON.stringify(payloads));
        form.append("region", region);
        id = (await agentApi.upload(form)).id;
      } else {
        id = (await agentApi.create({ name, source: source === "template" ? "template" : "image", template_id: source === "template" ? templateId : undefined, params, image_uri: source === "image" ? imageUri : undefined, smoke_payloads: payloads, region })).id;
      }
      navigate(`/agents?view=detail&id=${id}`);
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <FlowHeader
        title={t("agents.create")}
        onBack={() => (step ? setStep(0) : navigate("/agents"))}
        steps={<Steps steps={[t("agents.stepSource"), t("agents.stepConfig")]} current={step} onSelect={setStep} />}
        end={
          step === 0 ? (
            <Button kind="primary" onClick={() => setStep(1)} testId="tp-agent-next">{t("common.next")}</Button>
          ) : (
            <Button kind="primary" disabled={busy || !name} onClick={() => void submit()} testId="tp-agent-submit">{t("agents.createConfirm")}</Button>
          )
        }
      />
      {step === 0 ? (
        <div className="tp-stack">
          <Card title={t("agents.stepSource")}>
            <div className="tp-tiles">
              <OptionCard title={t("agents.source_template")} desc={t("agents.sourceTemplateDesc")} on={source === "template"} onClick={() => setSource("template")} />
              <OptionCard title={t("agents.source_upload")} desc={t("agents.sourceUploadDesc")} on={source === "upload"} onClick={() => setSource("upload")} />
              <OptionCard title={t("agents.source_image")} desc={t("agents.sourceImageDesc")} on={source === "image"} onClick={() => setSource("image")} />
            </div>
          </Card>
          {source === "template" && (
            <Card title={t("agents.templates")}>
              {templates.loading ? <Spin /> : (
                <div className="tp-tiles">
                  {(templates.data ?? []).map((x) => (
                    <OptionCard
                      key={x.id}
                      title={loc(x.name)}
                      desc={loc(x.description)}
                      on={templateId === x.id}
                      badge={x.verified ? <Tag tone="green">{t("agents.verified")}</Tag> : undefined}
                      onClick={() => { setTemplateId(x.id); setName(x.id.replace(/_/g, "-")); setParams({}); }}
                      testId={`tp-template-${x.id}`}
                    />
                  ))}
                </div>
              )}
            </Card>
          )}
          <Alert tone="info">{t("agents.contractInfo")}</Alert>
        </div>
      ) : (
        <div className="tp-stack">
          <Card title={t("agents.basics")}>
            <div className="v2-form cols-2">
              <Field label={t("common.name")} required hint={t("agents.nameHint")}>
                <input className="v2-input" value={name} onChange={(e) => setName(e.target.value.toLowerCase())} />
              </Field>
              <Field label={t("common.region")}>
                <Select value={region} options={regionOptions} onChange={setRegion} />
              </Field>
            </div>
          </Card>
          {source === "template" && tpl && tpl.params.length > 0 && (
            <Card title={t("agents.templateParams")} sub={loc(tpl.name)}>
              <ParamFields tpl={tpl} values={params} onChange={setParams} />
            </Card>
          )}
          {source === "upload" && (
            <Card title={t("agents.uploadTitle")} sub={t("agents.uploadDesc")}>
              <input type="file" accept=".zip" onChange={(e) => setFile(e.target.files?.[0] ?? null)} data-testid="tp-agent-zip" />
            </Card>
          )}
          {source === "image" && (
            <Card title={t("agents.imageTitle")}>
              <Field label={t("agents.imageUri")} hint={t("agents.imageHint")} full>
                <input className="v2-input tp-mono" value={imageUri} placeholder="123456789012.dkr.ecr.us-east-1.amazonaws.com/repo:tag" onChange={(e) => setImageUri(e.target.value)} />
              </Field>
            </Card>
          )}
          {source !== "template" && (
            <Card title={t("agents.smokePayloads")} sub={t("agents.smokePayloadsDesc")}>
              <textarea className="v2-textarea tp-mono" rows={6} value={smoke} onChange={(e) => setSmoke(e.target.value)} />
            </Card>
          )}
        </div>
      )}
    </>
  );
}

function AgentDetail({ id }: { id: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const toast = useV2Toast();
  const a = useLoad(() => agentApi.get(id), id);
  const clusters = useLoad(clusterApi.list, "clusters");
  const [target, setTarget] = useState<string>("smoke");
  const [jobId, setJobId] = useState<string | null>(null);
  const [del, setDel] = useState(false);
  const live = a.data && (["queued", "building"].includes(a.data.status) || a.data.runtimes.some((r) => ["queued", "deploying"].includes(r.status)));
  usePoll(a.reload, live ? DETAIL_POLL_MS : LIST_POLL_MS * 2);
  if (!a.data) return a.error ? <Alert tone="error">{a.error}</Alert> : <Spin />;
  const ag: AgentView = a.data;
  const deploy = async () => {
    try {
      const r = await agentApi.deploy(id, { cluster_id: target === "smoke" ? null : target });
      setJobId(r.job_id);
      a.reload();
    } catch (err) {
      toast("error", errorMessage(err));
    }
  };
  const readyClusters = (clusters.data ?? []).filter((c) => c.status === "ready" && c.region === (ag.config.region ?? c.region));
  return (
    <>
      <FlowHeader
        title={<>{ag.name} <StatusTag status={ag.status} /></>}
        onBack={() => navigate("/agents")}
        end={
          <div className="tp-row">
            <Button size="sm" onClick={() => void agentApi.rebuild(id).then((r) => setJobId(r.job_id))}>{ag.source === "image" ? t("agents.reinspect") : t("agents.rebuild")}</Button>
            <Button size="sm" kind="danger" onClick={() => setDel(true)}>{t("common.delete")}</Button>
          </div>
        }
      />
      <div className="tp-stack">
        {(jobId ?? ag.job?.id) && <JobPanel jobId={(jobId ?? ag.job?.id) as string} onDone={() => a.reload()} />}
        <Card title={t("agents.image")}>
          <Descriptions
            items={[
              { label: t("agents.source"), value: t(`agents.source_${ag.source}`) + (ag.template_id ? ` · ${ag.template_id}` : "") },
              { label: t("agents.contract"), value: ag.contract },
              { label: t("agents.imageUri"), value: ag.image_uri && <span className="tp-mono">{ag.image_uri}</span> },
              { label: t("agents.imageSize"), value: ag.checks.image ? `${(ag.checks.image.size_bytes / 1024 ** 2).toFixed(0)} MB · ${ag.checks.image.architectures.join(", ")}` : "—" },
              { label: t("agents.probe"), value: ag.checks.probe ? `/ping ${ag.checks.probe.ping}` : "—" },
              { label: t("common.region"), value: ag.config.region },
            ]}
          />
          {ag.checks.static?.warnings.map((w) => <div key={w} style={{ marginTop: 8 }}><Alert tone="warn">{w}</Alert></div>)}
          {ag.checks.static?.errors.map((w) => <div key={w} style={{ marginTop: 8 }}><Alert tone="error">{w}</Alert></div>)}
        </Card>
        <Card
          title={t("agents.runtimes")}
          sub={t("agents.runtimesDesc")}
          end={
            <div className="tp-row">
              <Select
                value={target}
                onChange={setTarget}
                options={[{ value: "smoke", label: t("agents.smokeRuntimeOpt") }, ...readyClusters.map((c) => ({ value: c.id, label: t("agents.clusterRuntimeOpt", { name: c.name }) }))]}
              />
              <Button kind="primary" size="sm" disabled={ag.status !== "ready"} onClick={() => void deploy()} testId="tp-agent-deploy">{t("agents.deploy")}</Button>
            </div>
          }
          flush
        >
          <Table
            rowKey={(r) => r.id}
            rows={ag.runtimes}
            empty={t("agents.noRuntimes")}
            columns={[
              { key: "m", title: t("agents.network"), render: (r) => (r.network_mode === "VPC" ? t("agents.vpcRuntime") : t("agents.smokeRuntime")) },
              { key: "c", title: t("nav.clusters"), render: (r) => (clusters.data ?? []).find((c) => c.id === r.cluster_id)?.name ?? "—" },
              { key: "s", title: t("common.status"), render: (r) => <StatusTag status={r.status} /> },
              { key: "id", title: "Runtime", render: (r) => <span className="tp-mono">{r.runtime_id ?? "—"}</span> },
              {
                key: "sm",
                title: t("agents.smoke"),
                render: (r) =>
                  r.last_smoke.total ? (
                    <Tag tone={r.last_smoke.ok ? "green" : "red"}>
                      {r.last_smoke.passed}/{r.last_smoke.total} · rewards {(r.last_smoke.results ?? []).map((x) => JSON.stringify(x.rewards ?? "✕")).join(", ")}
                    </Tag>
                  ) : "—",
              },
              {
                key: "a",
                title: "",
                render: (r) => (
                  <div className="tp-row">
                    {r.job && <Button size="sm" onClick={() => setJobId(r.job!.id)}>{t("job.showLog")}</Button>}
                    <Button size="sm" kind="danger" onClick={() => void agentApi.deleteRuntime(id, r.id).then(a.reload)}>{t("common.delete")}</Button>
                  </div>
                ),
              },
            ]}
          />
        </Card>
      </div>
      <Confirm
        open={del}
        title={t("agents.deleteTitle")}
        body={t("agents.deleteBody", { name: ag.name })}
        confirmLabel={t("common.delete")}
        danger
        onConfirm={() => void agentApi.remove(id).then(() => navigate("/agents"))}
        onClose={() => setDel(false)}
      />
    </>
  );
}

export default function AgentsPage() {
  const [params] = useSearchParams();
  const view = params.get("view");
  const id = params.get("id");
  if (view === "new") return <CreateAgent />;
  if (view === "detail" && id) return <AgentDetail id={id} />;
  return <AgentList />;
}
