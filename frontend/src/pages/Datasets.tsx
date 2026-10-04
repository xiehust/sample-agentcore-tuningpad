import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";

import { JobPanel } from "../components/JobPanel";
import { StatusTag } from "../components/StatusTag";
import { agentApi, datasetApi, errorMessage } from "../lib/api";
import { dateTime, num } from "../lib/format";
import { useLocalized } from "../lib/localized";
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
  SubTabs,
  Table,
  Tag,
} from "../v2/ui";

function DatasetList() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const list = useLoad(datasetApi.list, "datasets");
  usePoll(list.reload, LIST_POLL_MS);
  return (
    <>
      <PageHeader
        title={t("pages.datasets.title")}
        desc={t("pages.datasets.desc")}
        end={<Button kind="primary" onClick={() => navigate("/datasets?view=new")} testId="tp-dataset-new">{t("datasets.create")}</Button>}
      />
      {list.data?.length === 0 ? (
        <Empty action={<Button kind="primary" onClick={() => navigate("/datasets?view=new")}>{t("datasets.create")}</Button>}>{t("datasets.empty")}</Empty>
      ) : (
        <Card flush>
          <Table
            rowKey={(d) => d.id}
            rows={list.data ?? []}
            loading={list.loading && !list.data}
            error={list.error}
            columns={[
              { key: "n", title: t("common.name"), render: (d) => <button type="button" className="v2-link" onClick={() => navigate(`/datasets?view=detail&id=${d.id}`)}>{d.name}</button> },
              { key: "s", title: t("common.status"), render: (d) => <StatusTag status={d.status} /> },
              { key: "src", title: t("datasets.source"), render: (d) => d.source },
              { key: "sp", title: t("datasets.splits"), render: (d) => Object.entries(d.splits).map(([k, v]) => `${k} ${num(v.rows, 0)}`).join(" · ") || "—" },
              { key: "r", title: t("common.region"), render: (d) => d.region },
              { key: "c", title: t("common.created"), render: (d) => dateTime(d.created_at) },
            ]}
          />
        </Card>
      )}
    </>
  );
}

function CreateDataset() {
  const { t } = useTranslation();
  const loc = useLocalized();
  const navigate = useNavigate();
  const toast = useV2Toast();
  const templates = useLoad(agentApi.templates, "templates");
  const [mode, setMode] = useState<"builtin" | "upload">("builtin");
  const [templateId, setTemplateId] = useState("gsm8k_math");
  const [name, setName] = useState("");
  const [region, setRegion] = useState("us-east-1");
  const [limit, setLimit] = useState("");
  const [train, setTrain] = useState<File | null>(null);
  const [val, setVal] = useState<File | null>(null);
  const [promptField, setPromptField] = useState("prompt");
  const [explicitPrompt, setExplicitPrompt] = useState(false);
  const [required, setRequired] = useState("");
  const [valFraction, setValFraction] = useState("0.05");
  const [forTemplate, setForTemplate] = useState("");
  const [busy, setBusy] = useState(false);
  const builtinTemplates = (templates.data ?? []).filter((x) => x.dataset?.builtin);

  const submit = async () => {
    setBusy(true);
    try {
      let id: string;
      if (mode === "builtin") {
        id = (await datasetApi.builtin({ template_id: templateId, name: name || undefined, region, limit: limit ? Number(limit) : null })).id;
      } else {
        if (!train) throw new Error(t("datasets.trainRequired"));
        const f = new FormData();
        f.append("name", name || train.name);
        f.append("train", train);
        if (val) f.append("val", val);
        f.append("prompt_field", promptField);
        f.append("explicit_prompt_column", String(explicitPrompt));
        f.append("required_fields", required);
        f.append("val_fraction", val ? "0" : valFraction || "0");
        if (forTemplate) f.append("template_id", forTemplate);
        f.append("region", region);
        id = (await datasetApi.upload(f)).id;
      }
      navigate(`/datasets?view=detail&id=${id}`);
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <FlowHeader
        title={t("datasets.create")}
        onBack={() => navigate("/datasets")}
        end={<Button kind="primary" disabled={busy} onClick={() => void submit()} testId="tp-dataset-submit">{t("datasets.createConfirm")}</Button>}
      />
      <div className="tp-stack">
        <Card title={t("datasets.mode")}>
          <div className="tp-tiles">
            <OptionCard title={t("datasets.builtin")} desc={t("datasets.builtinDesc")} on={mode === "builtin"} onClick={() => setMode("builtin")} />
            <OptionCard title={t("datasets.upload")} desc={t("datasets.uploadDesc")} on={mode === "upload"} onClick={() => setMode("upload")} />
          </div>
        </Card>
        <Card title={t("agents.basics")}>
          <div className="v2-form cols-2">
            <Field label={t("common.name")}>
              <input className="v2-input" value={name} placeholder={mode === "builtin" ? templateId : ""} onChange={(e) => setName(e.target.value)} />
            </Field>
            <Field label={t("common.region")}>
              <Select value={region} options={regionOptions} onChange={setRegion} />
            </Field>
          </div>
        </Card>
        {mode === "builtin" ? (
          <Card title={t("datasets.builtinPick")}>
            <div className="tp-tiles">
              {builtinTemplates.map((x) => (
                <OptionCard key={x.id} title={loc(x.name)} desc={t(`datasets.builtin_${x.dataset?.builtin}`)} on={templateId === x.id} onClick={() => setTemplateId(x.id)} />
              ))}
            </div>
            {templateId === "officebench" && (
              <div className="v2-form cols-2" style={{ marginTop: 16 }}>
                <Field label={t("datasets.limit")} hint={t("datasets.limitHint")}>
                  <input className="v2-input" type="number" min={1} value={limit} onChange={(e) => setLimit(e.target.value)} />
                </Field>
              </div>
            )}
          </Card>
        ) : (
          <Card title={t("datasets.files")} sub={t("datasets.filesDesc")}>
            <div className="v2-form cols-2">
              <Field label={t("datasets.trainFile")} required>
                <input type="file" accept=".jsonl,.json,.csv,.parquet,.ndjson" onChange={(e) => setTrain(e.target.files?.[0] ?? null)} />
              </Field>
              <Field label={t("datasets.valFile")} hint={t("datasets.valFileHint")}>
                <input type="file" accept=".jsonl,.json,.csv,.parquet,.ndjson" onChange={(e) => setVal(e.target.files?.[0] ?? null)} />
              </Field>
              <Field label={t("datasets.promptField")} hint={t("datasets.promptFieldHint")}>
                <input className="v2-input" value={promptField} disabled={explicitPrompt} onChange={(e) => setPromptField(e.target.value)} />
              </Field>
              <Field label={t("datasets.valFraction")}>
                <input className="v2-input" type="number" step="0.01" min={0} max={0.5} value={valFraction} disabled={!!val} onChange={(e) => setValFraction(e.target.value)} />
              </Field>
              <Field label={t("datasets.forTemplate")} hint={t("datasets.forTemplateHint")}>
                <Select value={forTemplate} onChange={setForTemplate} placeholder="—" options={[{ value: "", label: "—" }, ...(templates.data ?? []).map((x) => ({ value: x.id, label: loc(x.name) }))]} />
              </Field>
              <Field label={t("datasets.required")} hint={t("datasets.requiredHint")}>
                <input className="v2-input" value={required} placeholder="answer" onChange={(e) => setRequired(e.target.value)} />
              </Field>
              <Field label={t("datasets.explicitPrompt")} full>
                <label className="tp-row"><input type="checkbox" checked={explicitPrompt} onChange={(e) => setExplicitPrompt(e.target.checked)} />{t("datasets.explicitPromptHint")}</label>
              </Field>
            </div>
            <Alert tone="info">{t("datasets.formatInfo")}</Alert>
          </Card>
        )}
      </div>
    </>
  );
}

function DatasetDetail({ id }: { id: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const d = useLoad(() => datasetApi.get(id), id);
  const [split, setSplit] = useState("train");
  const prev = useLoad(() => datasetApi.preview(id, split), `${id}-${split}-${d.data?.status}`);
  const [del, setDel] = useState(false);
  usePoll(d.reload, d.data && d.data.status !== "ready" && d.data.status !== "failed" ? DETAIL_POLL_MS : 60000);
  if (!d.data) return d.error ? <Alert tone="error">{d.error}</Alert> : <Spin />;
  const ds = d.data;
  return (
    <>
      <FlowHeader
        title={<>{ds.name} <StatusTag status={ds.status} /></>}
        onBack={() => navigate("/datasets")}
        end={<Button kind="danger" size="sm" onClick={() => setDel(true)}>{t("common.delete")}</Button>}
      />
      <div className="tp-stack">
        {ds.job && <JobPanel jobId={ds.job.id} onDone={() => d.reload()} />}
        <Card title={t("datasets.summary")}>
          <Descriptions
            items={[
              { label: t("datasets.source"), value: ds.source },
              { label: t("datasets.promptField"), value: ds.has_prompt_column ? t("datasets.explicitPromptShort") : ds.prompt_field },
              ...Object.entries(ds.splits).map(([k, v]) => ({ label: `${k} (${num(v.rows, 0)})`, value: <span className="tp-mono">{v.s3_uri}</span> })),
            ]}
          />
        </Card>
        {ds.status === "ready" && (
          <Card
            title={t("datasets.preview")}
            end={<SubTabs value={split} onChange={setSplit} tabs={Object.keys(ds.splits).map((k) => ({ value: k, label: k }))} />}
          >
            {prev.loading ? <Spin /> : (
              <Table
                rowKey={(r) => JSON.stringify(r).slice(0, 200)}
                rows={prev.data ?? []}
                columns={[
                  { key: "p", title: "payload", render: (r) => <code className="tp-mono">{JSON.stringify(r.payload).slice(0, 400)}</code> },
                  ...(ds.has_prompt_column ? [{ key: "pr", title: "prompt", render: (r: Record<string, unknown>) => <span>{JSON.stringify(r.prompt).slice(0, 200)}</span> }] : []),
                ]}
              />
            )}
            {ds.template_id && <div style={{ marginTop: 8 }}><Tag tone="blue">{ds.template_id}</Tag></div>}
          </Card>
        )}
      </div>
      <Confirm open={del} title={t("datasets.deleteTitle")} body={t("datasets.deleteBody")} confirmLabel={t("common.delete")} danger onConfirm={() => void datasetApi.remove(id).then(() => navigate("/datasets"))} onClose={() => setDel(false)} />
    </>
  );
}

export default function DatasetsPage() {
  const [params] = useSearchParams();
  const view = params.get("view");
  const id = params.get("id");
  if (view === "new") return <CreateDataset />;
  if (view === "detail" && id) return <DatasetDetail id={id} />;
  return <DatasetList />;
}
