import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";

import { StatusTag } from "../components/StatusTag";
import { TraceWaterfall } from "../components/TraceWaterfall";
import { SampleStateTag, Transcript } from "../components/Transcript";
import { servingApi, type EvalSampleFilter, type EvalSampleRow } from "../lib/api";
import { num } from "../lib/format";
import { DETAIL_POLL_MS, usePoll } from "../lib/poll";
import { useLoad } from "../v2/hooks";
import { Alert, Button, Card, Descriptions, Drawer, FlowHeader, Kpi, Pager, Segmented, Spin, SubTabs, Table, Tag } from "../v2/ui";

const PAGE = 20;
// "preparing": the eval-only OTEL runtime is being deployed before the rollouts start
const ACTIVE = new Set(["queued", "preparing", "running"]);

/** Tabs of the sample drawer: "trace" (span waterfall) only when the eval recorded traces. */
type SampleTab = "conversation" | "trace";

function Seconds({ s }: { s: number | null }) {
  const { t } = useTranslation();
  return <>{s === null ? "—" : t("evals.detail.seconds", { s: num(s, 1) })}</>;
}

function SampleView({ evalId, index, observe }: { evalId: string; index: number; observe: boolean }) {
  const { t } = useTranslation();
  const [tab, setTab] = useState<SampleTab>("conversation");
  // the trace is fetched the first time its tab is opened, then kept mounted (selection survives)
  const [traceOpened, setTraceOpened] = useState(false);
  const d = useLoad(() => servingApi.evalSample(evalId, index), `${evalId}-${index}`);
  if (!d.data) return d.error ? <Alert tone="error">{d.error}</Alert> : <Spin />;
  const s = d.data;
  const tabs: { value: SampleTab; label: string }[] = [
    { value: "conversation", label: t("evals.detail.tabConversation") },
    ...(observe ? [{ value: "trace" as const, label: t("evals.trace.tab") }] : []),
  ];
  const pick = (v: SampleTab) => {
    setTab(v);
    if (v === "trace") setTraceOpened(true);
  };
  return (
    <div className="tp-stack" data-testid="tp-eval-sample">
      <Descriptions
        items={[
          { label: t("common.status"), value: <SampleStateTag state={s.state} /> },
          { label: t("evals.detail.reward"), value: <b>{num(s.reward, 3)}</b> },
          { label: t("evals.detail.elapsed"), value: <Seconds s={s.elapsed_s} /> },
          { label: t("evals.detail.turnsTools"), value: `${s.turns} / ${s.tool_calls}` },
          { label: t("evals.detail.stopReason"), value: <span className="tp-mono">{s.stop_reason ?? "—"}</span> },
          { label: t("evals.detail.session"), value: <span className="tp-mono">{s.session_id ?? "—"}</span> },
          ...Object.entries(s.extra).map(([k, v]) => ({ label: k, value: <span className="tp-mono">{String(v)}</span> })),
        ]}
      />
      <SubTabs value={tab} onChange={pick} tabs={tabs} />
      {tab === "conversation" && <Transcript sample={s} />}
      {observe && traceOpened && (
        <div hidden={tab !== "trace"}>
          <TraceWaterfall evalId={evalId} index={index} />
        </div>
      )}
    </div>
  );
}

/** Eval detail: KPIs from the per-sample states, filterable sample table, transcript drawer. */
export default function EvalDetail({ id }: { id: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [filter, setFilter] = useState<EvalSampleFilter>("all");
  const [page, setPage] = useState(1);
  const evals = useLoad(servingApi.evals, "evals");
  const samples = useLoad(
    () => servingApi.evalSamples(id, { status: filter, offset: (page - 1) * PAGE, limit: PAGE }),
    `${id}-${filter}-${page}`,
  );
  const ev = evals.data?.find((e) => e.id === id);
  const live = !!ev && ACTIVE.has(ev.status);
  usePoll(() => { evals.reload(); samples.reload(); }, DETAIL_POLL_MS * 2, live);
  const raw = params.get("sample");
  const selected = raw !== null && /^\d+$/.test(raw) ? Number(raw) : null;
  const openSample = (index: number | null) =>
    setParams(index === null ? { view: "detail", id } : { view: "detail", id, sample: String(index) });

  if (!samples.data && samples.error) {
    return (
      <>
        <FlowHeader title={ev?.name ?? id} onBack={() => navigate("/evals")} />
        <Alert tone="error" action={<Button size="sm" onClick={samples.reload}>{t("v2.common.retry")}</Button>}>{samples.error}</Alert>
      </>
    );
  }
  const c = samples.data?.counts;
  const all = c ? c.scored + c.truncated + c.acr_failed + c.invoke_failed : null;
  const failed = c ? c.acr_failed + c.invoke_failed : null;
  const pages = Math.max(1, Math.ceil((samples.data?.total ?? 0) / PAGE));
  const label = (key: string, n: number | null) => (n === null ? t(key) : `${t(key)} (${n})`);
  const reason = (r: EvalSampleRow) => r.error ?? r.stop_reason;
  const observe = samples.data?.observe ?? !!ev?.observe;
  return (
    <>
      <FlowHeader
        title={
          <span className="tp-row">
            {ev?.name ?? id}
            {ev && <StatusTag status={ev.status} />}
            {observe && <Tag tone="blue" title={t("evals.trace.recordedHint")}>{t("evals.trace.recorded")}</Tag>}
          </span>
        }
        onBack={() => navigate("/evals")}
      />
      <div className="tp-stack">
        {ev?.error && <Alert tone="error">{ev.error}</Alert>}
        <div className="v2-kpis tp-kpis">
          <Kpi label={t("evals.meanReward")} value={num(ev?.summary.mean_reward, 3)} sub={t("evals.detail.meanScored", { v: num(ev?.summary.mean_reward_scored, 3) })} testId="tp-eval-kpi-mean" />
          <Kpi label={t("evals.state.scored")} value={c ? c.scored : "—"} sub={all !== null ? t("evals.detail.ofTotal", { n: all }) : undefined} tone={c && c.scored > 0 ? "good" : undefined} />
          <Kpi label={t("evals.state.truncated")} value={c ? c.truncated : "—"} />
          <Kpi label={t("evals.state.acr_failed")} value={c ? c.acr_failed : "—"} tone={c && c.acr_failed > 0 ? "bad" : undefined} />
          <Kpi label={t("evals.state.invoke_failed")} value={c ? c.invoke_failed : "—"} tone={c && c.invoke_failed > 0 ? "bad" : undefined} />
          <Kpi label={t("evals.detail.samples")} value={all ?? "—"} sub={ev ? `${ev.split} · ${t("evals.limit")} ${ev.limit}` : undefined} />
        </div>
        <Card
          title={t("evals.detail.samples")}
          end={
            <Segmented<EvalSampleFilter>
              value={filter}
              ariaLabel={t("evals.detail.filter")}
              onChange={(v) => { setFilter(v); setPage(1); }}
              options={[
                { value: "all", label: label("evals.detail.filterAll", all) },
                { value: "failed", label: label("evals.detail.filterFailed", failed) },
                { value: "truncated", label: label("evals.detail.filterTruncated", c?.truncated ?? null) },
              ]}
            />
          }
          flush
        >
          <Table
            testId="tp-eval-samples"
            rowKey={(r) => String(r.index)}
            rows={samples.data?.samples ?? []}
            loading={samples.loading && !samples.data}
            error={samples.error}
            onRetry={samples.reload}
            selectedKey={selected === null ? null : String(selected)}
            empty={filter === "all" ? t("evals.detail.noSamples") : t("evals.detail.noMatch")}
            columns={[
              { key: "i", title: "#", width: 64, render: (r) => <button type="button" className="v2-link" onClick={() => openSample(r.index)}>{r.index}</button> },
              { key: "s", title: t("common.status"), render: (r) => <SampleStateTag state={r.state} /> },
              { key: "r", title: t("evals.detail.reward"), render: (r) => <b>{num(r.reward, 3)}</b> },
              {
                key: "why",
                title: t("evals.detail.stopReason"),
                render: (r) => {
                  const text = reason(r);
                  return text ? <span className={r.state === "scored" ? "tp-ellipsis v2-muted" : "tp-ellipsis"} title={text}>{text}</span> : "—";
                },
              },
              { key: "t", title: t("evals.detail.turns"), render: (r) => r.turns },
              { key: "tc", title: t("evals.detail.toolCalls"), render: (r) => r.tool_calls },
              { key: "e", title: t("evals.detail.elapsed"), render: (r) => <Seconds s={r.elapsed_s} /> },
              { key: "a", title: "", render: (r) => <Button size="sm" onClick={() => openSample(r.index)} testId={`tp-eval-sample-${r.index}`}>{t("evals.detail.open")}</Button> },
            ]}
          />
          {samples.data && <Pager page={page} pages={pages} total={samples.data.total} onPage={setPage} />}
        </Card>
      </div>
      <Drawer
        wide
        open={selected !== null}
        title={selected === null ? "" : t("evals.detail.sampleTitle", { index: selected })}
        onClose={() => openSample(null)}
        testId="tp-eval-drawer"
      >
        {selected !== null && <SampleView key={selected} evalId={id} index={selected} observe={observe} />}
      </Drawer>
    </>
  );
}
