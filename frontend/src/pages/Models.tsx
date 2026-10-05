import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { ModelCheckCard, PlanCard } from "../components/ModelCards";
import { catalogApi, errorMessage, type ModelCheck, type Plan } from "../lib/api";
import { money, num } from "../lib/format";
import { regionOptions } from "../lib/regions";
import { useLoad, useV2Toast } from "../v2/hooks";
import { Alert, Button, Card, Field, PageHeader, Select, SubTabs, Table, Tag } from "../v2/ui";

function ModelsTab() {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const presets = useLoad(catalogApi.presets, "presets");
  const instances = useLoad(() => catalogApi.instances("us-east-1"), "inst");
  const [modelId, setModelId] = useState("Qwen/Qwen3.5-2B");
  const [check, setCheck] = useState<ModelCheck | null>(null);
  const [itype, setItype] = useState("p5.48xlarge");
  const [nodes, setNodes] = useState(1);
  const [ctxLen, setCtxLen] = useState(4096);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [busy, setBusy] = useState(false);

  const run = async (id = modelId) => {
    setBusy(true);
    setPlan(null);
    try {
      setModelId(id);
      const c = await catalogApi.checkModel(id);
      setCheck(c);
      if (c.compatible) setPlan((await catalogApi.plan({ model_id: id, instance_type: itype, nodes, max_model_len: ctxLen })).plan);
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="tp-stack">
      <Card title={t("models.check")} sub={t("models.checkDesc")}>
        <div className="v2-form cols-2">
          <Field label={t("models.hfId")} full>
            <input className="v2-input" value={modelId} onChange={(e) => setModelId(e.target.value)} data-testid="tp-model-id" />
          </Field>
          <Field label={t("models.instanceType")}>
            <Select
              value={itype}
              onChange={setItype}
              options={(instances.data?.instances ?? []).filter((i) => i.training).map((i) => ({ value: i.type, label: `${i.type} · ${i.gpus}×${i.gpu} ${i.gpu_mem_gib}G` }))}
            />
          </Field>
          <Field label={t("models.nodes")}>
            <input className="v2-input" type="number" min={1} max={20} value={nodes} onChange={(e) => setNodes(Number(e.target.value) || 1)} />
          </Field>
          <Field label={t("models.maxModelLen")}>
            <input className="v2-input" type="number" min={512} step={1024} value={ctxLen} onChange={(e) => setCtxLen(Number(e.target.value) || 4096)} />
          </Field>
        </div>
        <div className="tp-row end" style={{ marginTop: 12 }}>
          <Button kind="primary" disabled={busy || !modelId} onClick={() => void run()} testId="tp-model-run">
            {t("models.runCheck")}
          </Button>
        </div>
      </Card>
      {check && (
        <div className="tp-grid c2">
          <ModelCheckCard check={check} />
          {plan && <PlanCard plan={plan} />}
        </div>
      )}
      <Card title={t("models.presets")} flush>
        <Table
          rowKey={(p) => p.id}
          rows={presets.data ?? []}
          loading={presets.loading}
          error={presets.error}
          columns={[
            { key: "id", title: t("models.hfId"), render: (p) => <code className="tp-mono">{p.id}</code> },
            { key: "v", title: t("models.verified"), render: (p) => (p.verified ? <Tag tone="green">{p.verified}</Tag> : <span className="v2-muted">{t("models.unverified")}</span>) },
            { key: "a", title: "", width: 110, render: (p) => <Button size="sm" disabled={busy} onClick={() => void run(p.id)}>{t("models.runCheck")}</Button> },
          ]}
        />
      </Card>
    </div>
  );
}

function InstancesTab() {
  const { t } = useTranslation();
  const [region, setRegion] = useState("us-east-1");
  const cat = useLoad(() => catalogApi.instances(region), region);
  return (
    <Card
      title={t("models.instances")}
      sub={t("models.instancesDesc")}
      end={<Select value={region} options={regionOptions} onChange={setRegion} />}
      flush
    >
      {cat.data?.errors.length ? <Alert tone="warn">{cat.data.errors.join("; ")}</Alert> : null}
      <Table
        rowKey={(i) => i.type}
        rows={(cat.data?.instances ?? []).filter((i) => i.training)}
        loading={cat.loading}
        error={cat.error}
        testId="tp-instances"
        columns={[
          { key: "t", title: t("models.instanceType"), render: (i) => <code className="tp-mono">{i.ml_type}</code> },
          { key: "g", title: "GPU", render: (i) => `${i.gpus} × ${i.gpu} ${i.gpu_mem_gib} GiB` },
          { key: "efa", title: "EFA", render: (i) => (i.multi_node ? i.efa : <span className="v2-muted">{t("models.singleNode")}</span>) },
          { key: "p", title: t("models.price"), render: (i) => money(i.price_per_hour) },
          { key: "q", title: t("models.quotaOnDemand"), render: (i) => num(i.quota.on_demand, 0) },
          { key: "s", title: t("models.quotaSpot"), render: (i) => num(i.quota.spot, 0) },
        ]}
      />
      {cat.data && (
        <p className="v2-muted" style={{ padding: "12px 24px" }}>
          {t("models.limits", { per: num(cat.data.limits.per_cluster, 0), total: num(cat.data.limits.total, 0), spot: num(cat.data.limits.spot_total, 0) })}
        </p>
      )}
    </Card>
  );
}

export default function ModelsPage() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as "models" | "instances") ?? "models";
  return (
    <>
      <PageHeader
        title={t("pages.models.title")}
        desc={t("pages.models.desc")}
        tabs={
          <SubTabs
            value={tab}
            onChange={(v) => setParams({ tab: v })}
            tabs={[
              { value: "models", label: t("models.tabModels") },
              { value: "instances", label: t("models.tabInstances") },
            ]}
          />
        }
      />
      {tab === "models" ? <ModelsTab /> : <InstancesTab />}
    </>
  );
}
