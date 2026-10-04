import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { StatusTag } from "../components/StatusTag";
import { resourceApi } from "../lib/api";
import { money, num } from "../lib/format";
import { LIST_POLL_MS, usePoll } from "../lib/poll";
import { useLoad } from "../v2/hooks";
import { Alert, Button, Card, Kpi, PageHeader, Spin, Table } from "../v2/ui";

const FLOW = ["settings", "clusters", "agents", "datasets", "runs", "exports", "inference", "evals"] as const;

export default function OverviewPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const o = useLoad(resourceApi.overview, "overview");
  usePoll(o.reload, LIST_POLL_MS * 2);
  if (!o.data) return o.error ? <Alert tone="error">{o.error}</Alert> : <Spin />;
  const d = o.data;
  const setupReady = Object.values(d.setup).some((r) => r.status === "ready");
  const done: Record<string, boolean> = {
    settings: setupReady, clusters: d.clusters.some((c) => c.status === "ready"), agents: d.counts.agents > 0,
    datasets: d.counts.datasets > 0, runs: d.counts.runs > 0, exports: d.counts.exports > 0,
    inference: d.counts.endpoints > 0, evals: d.counts.evals > 0,
  };
  return (
    <>
      <PageHeader title={t("pages.overview.title")} desc={t("pages.overview.desc")} />
      <div className="tp-stack">
        <div className="tp-grid c4">
          <Kpi label={t("overview.clusters")} value={d.counts.clusters} />
          <Kpi label={t("overview.activeRuns")} value={d.counts.active_runs} sub={t("overview.totalRuns", { n: d.counts.runs })} />
          <Kpi label={t("overview.gpuCost")} value={money(d.run_cost_usd)} sub={`${num(d.node_hours)} ${t("runs.nodeHours")}`} />
          <Kpi label={t("overview.endpoints")} value={d.counts.endpoints} />
        </div>
        <Card title={t("overview.flow")} sub={t("overview.flowDesc")}>
          <div className="tp-tiles">
            {FLOW.map((k, i) => (
              <button key={k} type="button" className={done[k] ? "v2-option on" : "v2-option"} onClick={() => navigate(k === "settings" ? "/settings" : `/${k}`)}>
                <span className="t">{i + 1}. {t(`overview.step_${k}`)} {done[k] ? "✓" : ""}</span>
                <span className="d">{t(`overview.step_${k}_desc`)}</span>
              </button>
            ))}
          </div>
        </Card>
        <Card title={t("overview.activeRuns")} end={<Button size="sm" kind="primary" onClick={() => navigate("/runs?view=new")}>{t("runs.create")}</Button>} flush>
          <Table
            rowKey={(r) => r.id}
            rows={d.active_runs}
            empty={t("overview.noActive")}
            columns={[
              { key: "n", title: t("common.name"), render: (r) => <button type="button" className="v2-link" onClick={() => navigate(`/runs?view=detail&id=${r.id}`)}>{r.name}</button> },
              { key: "s", title: t("common.status"), render: (r) => <StatusTag status={r.status} /> },
              { key: "st", title: t("runs.step"), render: (r) => num(r.step, 0) },
              { key: "c", title: t("runs.cost"), render: (r) => money(r.est_cost_usd) },
            ]}
          />
        </Card>
      </div>
    </>
  );
}
