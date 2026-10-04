import { useState } from "react";
import { useTranslation } from "react-i18next";

import { StatusTag } from "../components/StatusTag";
import { errorMessage, resourceApi, runApi } from "../lib/api";
import { money, num } from "../lib/format";
import { regionOptions } from "../lib/regions";
import { useLoad, useV2Toast } from "../v2/hooks";
import { Button, Card, Confirm, PageHeader, Select, Table } from "../v2/ui";

const mb = (b?: number) => (b === undefined ? "—" : b > 1024 ** 3 ? `${(b / 1024 ** 3).toFixed(2)} GB` : `${(b / 1024 ** 2).toFixed(1)} MB`);

export default function ResourcesPage() {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [region, setRegion] = useState("us-east-1");
  const r = useLoad(() => resourceApi.get(region), region);
  const images = useLoad(runApi.images, "images");
  const [purge, setPurge] = useState<string | null>(null);
  const doPurge = async () => {
    try {
      const out = await resourceApi.purge(region, purge!);
      toast("success", t("resources.purged", { n: out.deleted }));
      r.reload();
    } catch (err) {
      toast("error", errorMessage(err));
    }
    setPurge(null);
  };
  return (
    <>
      <PageHeader title={t("pages.resources.title")} desc={t("pages.resources.desc")} end={<Select value={region} options={regionOptions} onChange={setRegion} />} />
      <div className="tp-stack">
        <Card title={t("nav.clusters")} flush>
          <Table rowKey={(c) => c.id} rows={r.data?.clusters ?? []} loading={r.loading} columns={[
            { key: "n", title: t("common.name"), render: (c) => c.name },
            { key: "s", title: t("common.status"), render: (c) => <StatusTag status={c.status} /> },
            { key: "st", title: "CloudFormation", render: (c) => <span className="tp-mono">{c.cfn_stack ?? "—"}</span> },
            { key: "g", title: t("clusters.spent"), render: (c) => money(c.gpu_cost_usd) },
          ]} />
        </Card>
        <Card title={t("resources.storage")} sub={r.data?.project.bucket} flush>
          <Table rowKey={(s) => s.prefix} rows={r.data?.storage ?? []} loading={r.loading} columns={[
            { key: "p", title: t("resources.prefix"), render: (s) => <span className="tp-mono">{s.prefix}</span> },
            { key: "o", title: t("resources.objects"), render: (s) => num(s.objects, 0) },
            { key: "b", title: t("resources.size"), render: (s) => mb(s.bytes) },
            { key: "m", title: t("resources.monthly"), render: (s) => money(s.monthly_usd) },
            { key: "a", title: "", render: (s) => (s.purgeable && (s.objects ?? 0) > 0 ? <Button size="sm" kind="danger" onClick={() => setPurge(s.prefix)}>{t("resources.purge")}</Button> : null) },
          ]} />
        </Card>
        <div className="tp-grid c2">
          <Card title="ECR" flush>
            <Table rowKey={(e) => e.repository} rows={r.data?.ecr ?? []} columns={[
              { key: "r", title: t("resources.repository"), render: (e) => <span className="tp-mono">{e.repository}</span> },
              { key: "i", title: t("resources.images"), render: (e) => e.images },
              { key: "b", title: t("resources.size"), render: (e) => mb(e.bytes) },
              { key: "m", title: t("resources.monthly"), render: (e) => money(e.monthly_usd) },
            ]} />
          </Card>
          <Card title={t("resources.runtimes")} flush>
            <Table rowKey={(x) => x.id} rows={r.data?.runtimes ?? []} columns={[
              { key: "i", title: "Runtime", render: (x) => <span className="tp-mono">{x.runtime_id ?? "—"}</span> },
              { key: "m", title: t("agents.network"), render: (x) => x.network_mode },
              { key: "s", title: t("common.status"), render: (x) => <StatusTag status={x.status} /> },
            ]} />
          </Card>
        </div>
        <Card title={t("resources.trainerImages")} sub={t("resources.trainerImagesDesc")} flush>
          <Table rowKey={(x) => x.id} rows={images.data ?? []} columns={[
            { key: "p", title: t("models.profile"), render: (x) => x.profile },
            { key: "s", title: t("common.status"), render: (x) => <StatusTag status={x.status} /> },
            { key: "d", title: t("common.detail"), render: (x) => x.job?.stages.find((s) => s.status === "running")?.detail ?? x.job?.error ?? "" },
            { key: "u", title: "URI", render: (x) => <span className="tp-mono">{x.image_uri}</span> },
            { key: "a", title: "", render: (x) => (x.status === "failed" ? <Button size="sm" onClick={() => void runApi.buildImage({ region: x.region, profile: x.profile }).then(images.reload)}>{t("job.retry")}</Button> : null) },
          ]} />
          <div className="tp-row" style={{ padding: "12px 24px" }}>
            <Button size="sm" onClick={() => void runApi.buildImage({ region, profile: "fsdp" }).then(images.reload)}>{t("resources.buildFsdp")}</Button>
            <Button size="sm" onClick={() => void runApi.buildImage({ region, profile: "megatron" }).then(images.reload)}>{t("resources.buildMegatron")}</Button>
          </div>
        </Card>
      </div>
      <Confirm open={!!purge} title={t("resources.purgeTitle")} body={t("resources.purgeBody", { prefix: purge })} confirmLabel={t("resources.purge")} danger onConfirm={() => void doPurge()} onClose={() => setPurge(null)} />
    </>
  );
}
