import { AlertCircle, RefreshCw } from "lucide-react";
import { useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { servingApi, type EvalTrace, type TraceSpan, type TraceSpanCategory } from "../lib/api";
import { millis, num } from "../lib/format";
import { useLoad } from "../v2/hooks";
import { Alert, Button, Card, Descriptions, LinkButton, Spin, Tag, type TagTone } from "../v2/ui";
import { Messages } from "./Transcript";

const CATEGORIES: TraceSpanCategory[] = ["agent", "llm", "tool", "other"];
const TICKS = [0, 25, 50, 75, 100];
const MAX_INDENT_DEPTH = 8;
const INDENT_PX = 14;
const MIN_BAR_PCT = 0.4;
const ATTRS_SHOWN = 12;

type Trace = NonNullable<EvalTrace["trace"]>;

/** Default selection: the first LLM call, else the root span. */
function defaultSpan(spans: TraceSpan[]): TraceSpan | null {
  return spans.find((s) => s.category === "llm") ?? spans[0] ?? null;
}

function CategoryChip({ category }: { category: TraceSpanCategory }) {
  const { t } = useTranslation();
  return <span className={`tp-wf-cat ${category}`}>{t(`evals.trace.cat_${category}`)}</span>;
}

function StatusChip({ status }: { status: TraceSpan["status"] }) {
  const { t } = useTranslation();
  return <Tag tone={status === "error" ? "red" : "green"}>{t(`evals.trace.status_${status}`)}</Tag>;
}

function Stat({ label, value, tone }: { label: string; value: ReactNode; tone?: TagTone }) {
  return (
    <Tag tone={tone ?? "outline"}>
      <span className="tp-wf-stat-l">{label}</span>
      <b>{value}</b>
    </Tag>
  );
}

/** Span list with indentation, category dots, relative time bars and a time axis. */
function Waterfall({
  trace,
  selected,
  onSelect,
}: {
  trace: Trace;
  selected: string | null;
  onSelect: (spanId: string) => void;
}) {
  const { t } = useTranslation();
  const rows = useRef<(HTMLDivElement | null)[]>([]);
  const spans = trace.spans;
  const move = (to: number) => {
    const j = Math.max(0, Math.min(spans.length - 1, to));
    onSelect(spans[j].span_id);
    rows.current[j]?.focus();
  };
  const onKey = (e: KeyboardEvent<HTMLDivElement>, i: number) => {
    if (e.key === "Enter" || e.key === " ") onSelect(spans[i].span_id);
    else if (e.key === "ArrowDown") move(i + 1);
    else if (e.key === "ArrowUp") move(i - 1);
    else if (e.key === "Home") move(0);
    else if (e.key === "End") move(spans.length - 1);
    else return;
    e.preventDefault();
  };
  const present = CATEGORIES.filter((c) => spans.some((s) => s.category === c));
  return (
    <div className="tp-wf-list">
      <div className="tp-legend tp-wf-legend">
        {present.map((c) => (
          <span key={c}>
            <i className={`tp-wf-dot ${c}`} aria-hidden="true" />
            {t(`evals.trace.cat_${c}`)}
          </span>
        ))}
      </div>
      <div className="tp-wf-row tp-wf-head" aria-hidden="true">
        <span>{t("evals.trace.colSpan")}</span>
        <span className="tp-wf-axis">
          {TICKS.map((p) => (
            <span key={p} style={{ left: `${p}%` }}>{millis((trace.duration_ms * p) / 100)}</span>
          ))}
        </span>
        <span className="dur">{t("evals.trace.colDuration")}</span>
      </div>
      <div role="listbox" aria-label={t("evals.trace.waterfall")} className="tp-wf-rows" data-testid="tp-wf-rows">
        {spans.map((s, i) => {
          const on = s.span_id === selected;
          const width = Math.min(100, Math.max(s.width_pct, MIN_BAR_PCT));
          const left = Math.max(0, Math.min(s.offset_pct, 100 - width));
          const err = s.status === "error";
          return (
            <div
              key={s.span_id}
              ref={(el) => {
                rows.current[i] = el;
              }}
              role="option"
              aria-selected={on}
              tabIndex={on ? 0 : -1}
              className={`tp-wf-row${on ? " on" : ""}${err ? " err" : ""}`}
              onClick={() => onSelect(s.span_id)}
              onKeyDown={(e) => onKey(e, i)}
              data-testid="tp-wf-row"
              data-depth={s.depth}
            >
              <span className="nm" style={{ paddingLeft: Math.min(s.depth, MAX_INDENT_DEPTH) * INDENT_PX }} title={s.name}>
                <i className={`tp-wf-dot ${s.category}`} aria-hidden="true" />
                <span className="t">{s.name}</span>
                {err && <AlertCircle size={13} className="tp-wf-errmark" role="img" aria-label={t("evals.trace.status_error")} />}
              </span>
              <span className="lane">
                <span className="track">
                  <span className={`bar ${s.category}`} style={{ left: `${left}%`, width: `${width}%` }} />
                </span>
              </span>
              <span className="dur">{millis(s.duration_ms)}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function AttrValue({ value }: { value: unknown }) {
  if (value !== null && typeof value === "object") return <pre className="v2-pre">{JSON.stringify(value, null, 2)}</pre>;
  return <span className="tp-mono">{String(value)}</span>;
}

/** One OTEL `exception` event: type + message, stack trace on demand. */
function SpanException({ ex }: { ex: NonNullable<TraceSpan["exceptions"]>[number] }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  return (
    <div className="tp-wf-exc" data-testid="tp-trace-exception">
      <Alert tone="error">
        <b>{ex.type || t("evals.trace.exception")}</b>
        {ex.message ? ` · ${ex.message}` : null}
      </Alert>
      {ex.stacktrace && (
        <>
          <LinkButton onClick={() => setOpen(!open)}>
            {open ? t("evals.trace.stackHide") : t("evals.trace.stackShow")}
          </LinkButton>
          {open && <pre className="v2-pre">{ex.stacktrace}</pre>}
        </>
      )}
    </div>
  );
}

/** Selected span: identity, timing, tokens, input/output messages and attributes. */
function SpanDetail({ span }: { span: TraceSpan }) {
  const { t } = useTranslation();
  const [allAttrs, setAllAttrs] = useState(false);
  const attrs = Object.entries(span.attributes);
  const shown = allAttrs ? attrs : attrs.slice(0, ATTRS_SHOWN);
  const hasTokens = span.tokens.input !== null || span.tokens.output !== null;
  const toolTone: TagTone = span.tool_status === "success" ? "green" : span.tool_status === "error" ? "red" : "gray";
  const noMessages = span.input_messages.length === 0 && span.output_messages.length === 0;
  return (
    <Card
      title={<span className="tp-wf-title" title={span.name}>{span.name}</span>}
      end={
        <span className="tp-row">
          <CategoryChip category={span.category} />
          <StatusChip status={span.status} />
        </span>
      }
      testId="tp-trace-span"
    >
      <Descriptions
        one
        items={[
          ...(span.model ? [{ label: t("evals.trace.model"), value: <span className="tp-mono">{span.model}</span> }] : []),
          ...(span.tool
            ? [{
                label: t("evals.trace.tool"),
                value: (
                  <span className="tp-row">
                    <span className="tp-mono">{span.tool}</span>
                    {span.tool_status && <Tag tone={toolTone}>{span.tool_status}</Tag>}
                  </span>
                ),
              }]
            : []),
          { label: t("evals.trace.start"), value: t("evals.trace.startValue", { offset: millis(span.start_ms), pct: num(span.offset_pct, 1) }) },
          { label: t("evals.trace.duration"), value: millis(span.duration_ms) },
          ...(hasTokens
            ? [{
                label: t("evals.trace.tokens"),
                value: t("evals.trace.tokensInOut", { input: num(span.tokens.input, 0), output: num(span.tokens.output, 0) }),
              }]
            : []),
          { label: t("evals.trace.spanId"), value: <span className="tp-mono">{span.span_id}</span> },
          { label: t("evals.trace.traceId"), value: <span className="tp-mono">{span.trace_id}</span> },
        ]}
      />
      {(span.exceptions ?? []).map((ex, i) => (
        <SpanException key={i} ex={ex} />
      ))}
      {span.input_messages.length > 0 && (
        <>
          <div className="v2-sub-title">{t("evals.trace.inputMessages", { n: span.input_messages.length })}</div>
          <Messages messages={span.input_messages} />
        </>
      )}
      {span.output_messages.length > 0 && (
        <>
          <div className="v2-sub-title">{t("evals.trace.outputMessages", { n: span.output_messages.length })}</div>
          <Messages messages={span.output_messages} related={span.input_messages} />
        </>
      )}
      {noMessages && <p className="v2-muted tp-wf-note">{t(span.category === "llm" ? "evals.trace.noMessagesLlm" : "evals.trace.noMessages")}</p>}
      <div className="v2-sub-title tp-row">
        {t("evals.trace.attributes", { n: attrs.length })}
        {attrs.length > ATTRS_SHOWN && (
          <LinkButton onClick={() => setAllAttrs(!allAttrs)} testId="tp-trace-attrs-toggle">
            {allAttrs ? t("evals.trace.attrsLess") : t("evals.trace.attrsAll", { n: attrs.length })}
          </LinkButton>
        )}
      </div>
      {attrs.length === 0 ? (
        <p className="v2-muted tp-wf-note">{t("evals.trace.noAttributes")}</p>
      ) : (
        <dl className="tp-wf-attrs">
          {shown.map(([k, v]) => (
            <div key={k}>
              <dt title={k}>{k}</dt>
              <dd><AttrValue value={v} /></dd>
            </div>
          ))}
        </dl>
      )}
    </Card>
  );
}

/** Ready trace: summary strip, waterfall (left) and the selected span (right). */
function TraceView({ trace, refresh }: { trace: Trace; refresh: ReactNode }) {
  const { t } = useTranslation();
  const [selected, setSelected] = useState<string | null>(null);
  const span = trace.spans.find((s) => s.span_id === selected) ?? defaultSpan(trace.spans);
  const tt = trace.totals;
  return (
    <>
      <div className="tp-wf-summary" data-testid="tp-trace-summary">
        <Stat label={t("evals.trace.totalDuration")} value={millis(trace.duration_ms)} />
        <Stat label={t("evals.trace.spans")} value={tt.spans} />
        <Stat label={t("evals.trace.llmCalls")} value={tt.llm_calls} />
        <Stat label={t("evals.trace.toolCalls")} value={tt.tool_calls} />
        <Stat label={t("evals.trace.errors")} value={tt.errors} tone={tt.errors > 0 ? "red" : undefined} />
        <Stat label={t("evals.trace.inputTokens")} value={num(tt.input_tokens, 0)} />
        <Stat label={t("evals.trace.outputTokens")} value={num(tt.output_tokens, 0)} />
        <span className="tp-wf-summary-end">{refresh}</span>
      </div>
      <div className="tp-wf">
        <Card
          title={t("evals.trace.waterfall")}
          sub={t("evals.trace.waterfallSub", { n: trace.spans.length, dur: millis(trace.duration_ms) })}
          testId="tp-trace-waterfall"
        >
          <Waterfall trace={trace} selected={span?.span_id ?? null} onSelect={setSelected} />
        </Card>
        <div className="tp-wf-side">{span && <SpanDetail key={span.span_id} span={span} />}</div>
      </div>
    </>
  );
}

/**
 * Trace tab of an eval sample: AgentCore spans of the sample's runtime session as a
 * Langfuse-style waterfall. Fetched when mounted (the tab is opened); refresh skips the
 * backend cache.
 */
export function TraceWaterfall({ evalId, index }: { evalId: string; index: number }) {
  const { t } = useTranslation();
  const [force, setForce] = useState(0);
  const load = useLoad(() => servingApi.evalSampleTrace(evalId, index, force > 0), `${evalId}-${index}-${force}`);
  const refresh = (
    <Button size="sm" onClick={() => setForce((n) => n + 1)} disabled={load.loading} testId="tp-trace-refresh">
      <RefreshCw size={13} aria-hidden="true" />
      {t("evals.trace.refresh")}
    </Button>
  );
  const d = load.data;
  let body: ReactNode;
  let state = "loading";
  if (!d) {
    if (load.error) {
      state = "error";
      body = (
        <Alert tone="error" action={<Button size="sm" onClick={load.reload} testId="tp-trace-retry">{t("v2.common.retry")}</Button>}>
          {t("evals.trace.loadFailed", { msg: load.error })}
        </Alert>
      );
    } else {
      body = <Spin label={t("evals.trace.loading")} />;
    }
  } else if (d.state === "pending") {
    state = "pending";
    body = <Alert tone="info" action={refresh}>{t("evals.trace.pending")}</Alert>;
  } else if (d.state === "ready" && d.trace && d.trace.spans.length > 0) {
    state = "ready";
    body = <TraceView trace={d.trace} refresh={refresh} />;
  } else {
    state = d.reason === "no_session" ? "no_session" : "no_spans";
    body = (
      <Alert tone={state === "no_session" ? "warn" : "info"} action={state === "no_spans" ? refresh : undefined}>
        {t(state === "no_session" ? "evals.trace.noSession" : "evals.trace.noSpans")}
      </Alert>
    );
  }
  return (
    <div className="tp-stack tp-wf-host" data-testid="tp-trace" data-state={state}>
      {d && load.error && <Alert tone="error">{t("evals.trace.loadFailed", { msg: load.error })}</Alert>}
      {body}
    </div>
  );
}
