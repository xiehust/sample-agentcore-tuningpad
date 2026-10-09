import { Wrench } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import type { EvalSampleDetail, EvalSampleState, TranscriptBlock, TranscriptMessage } from "../lib/api";
import { Alert, Card, Descriptions, LinkButton, Tag, type TagTone } from "../v2/ui";

const SAMPLE_STATE_TONE: Record<EvalSampleState, TagTone> = {
  scored: "green",
  truncated: "orange",
  acr_failed: "red",
  invoke_failed: "red",
};

/** Localized state chip of one eval sample. */
export function SampleStateTag({ state }: { state: EvalSampleState }) {
  const { t } = useTranslation();
  return (
    <Tag tone={SAMPLE_STATE_TONE[state] ?? "gray"} dot>
      {t(`evals.state.${state}`)}
    </Tag>
  );
}

const LONG_CHARS = 1500;
const LONG_LINES = 30;

/** Pre-wrapped text that collapses beyond ~1500 characters / 30 lines. */
function LongText({ text, pre }: { text: string; pre?: boolean }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const lines = text.split("\n");
  const long = text.length > LONG_CHARS || lines.length > LONG_LINES;
  const shown = !long || open ? text : `${lines.slice(0, LONG_LINES).join("\n").slice(0, LONG_CHARS)}…`;
  return (
    <>
      {pre ? <pre className="v2-pre">{shown}</pre> : <div className="tp-msg-text">{shown}</div>}
      {long && (
        <div className="tp-msg-more">
          <LinkButton onClick={() => setOpen(!open)}>
            {open ? t("evals.detail.showLess") : t("evals.detail.showMore", { lines: lines.length, chars: text.length })}
          </LinkButton>
        </div>
      )}
    </>
  );
}

function TruncatedNote() {
  const { t } = useTranslation();
  return <div className="tp-msg-note">{t("evals.detail.blockTruncated")}</div>;
}

function Block({ block, toolNames }: { block: TranscriptBlock; toolNames: Map<string, string> }) {
  const { t } = useTranslation();
  if (block.type === "tool_use") {
    return (
      <div className="tp-msg-tool">
        <div className="tp-msg-tool-head">
          <Wrench size={13} aria-hidden="true" />
          <span>{t("evals.detail.toolCall")}</span>
          <Tag tone="blue">{block.name || "—"}</Tag>
          {block.tool_use_id && <span className="tp-msg-id" title={block.tool_use_id}>{block.tool_use_id}</span>}
        </div>
        <LongText text={block.text} pre />
        {block.truncated && <TruncatedNote />}
      </div>
    );
  }
  if (block.type === "tool_result") {
    const name = block.tool_use_id ? toolNames.get(block.tool_use_id) : undefined;
    const tone: TagTone = block.status === "success" ? "green" : block.status === "error" ? "red" : "gray";
    return (
      <div className="tp-msg-tool result">
        <div className="tp-msg-tool-head">
          <span>{t("evals.detail.toolResult")}</span>
          {name && <Tag tone="outline">{name}</Tag>}
          {block.status && <Tag tone={tone}>{block.status}</Tag>}
        </div>
        <LongText text={block.text} pre />
        {block.truncated && <TruncatedNote />}
      </div>
    );
  }
  if (block.type === "reasoning") {
    return (
      <div className="tp-msg-reasoning">
        <div className="tp-msg-label">{t("evals.detail.reasoning")}</div>
        <LongText text={block.text} />
        {block.truncated && <TruncatedNote />}
      </div>
    );
  }
  return (
    <div>
      <LongText text={block.text} pre={block.type === "other"} />
      {block.truncated && <TruncatedNote />}
    </div>
  );
}

type Speaker = "user" | "assistant" | "tool" | "other";

function speaker(m: TranscriptMessage): Speaker {
  if (m.role === "assistant") return "assistant";
  if (m.role === "tool") return "tool"; // span messages use the OTel role name
  if (m.blocks.length > 0 && m.blocks.every((b) => b.type === "tool_result")) return "tool";
  return m.role === "user" ? "user" : "other";
}

function Message({ message, toolNames }: { message: TranscriptMessage; toolNames: Map<string, string> }) {
  const { t } = useTranslation();
  const who = speaker(message);
  return (
    <div className={`tp-msg ${who}`}>
      <div className="tp-msg-who">{who === "other" ? message.role : t(`evals.detail.role_${who}`)}</div>
      <div className="tp-msg-body">
        {message.blocks.length === 0 ? (
          <span className="v2-muted">{t("evals.detail.emptyMessage")}</span>
        ) : (
          message.blocks.map((b, i) => <Block key={i} block={b} toolNames={toolNames} />)
        )}
      </div>
    </div>
  );
}

/**
 * Rendered message list (text, reasoning, tool calls, tool results). Tool results show the
 * name of their tool call; `related` adds messages to look those names up in without
 * rendering them (a span's output may answer a call from its input).
 */
export function Messages({ messages, related = [] }: { messages: TranscriptMessage[]; related?: TranscriptMessage[] }) {
  const toolNames = new Map<string, string>();
  for (const m of [...related, ...messages]) {
    for (const b of m.blocks) if (b.type === "tool_use" && b.tool_use_id && b.name) toolNames.set(b.tool_use_id, b.name);
  }
  return (
    <div className="tp-msgs">
      {messages.map((m, i) => <Message key={i} message={m} toolNames={toolNames} />)}
    </div>
  );
}

/** Failure banner: stop_reason / error, plus the (collapsed) traceback. */
function Failure({ sample }: { sample: EvalSampleDetail }) {
  const { t } = useTranslation();
  const [tb, setTb] = useState(false);
  const failed = sample.state === "acr_failed" || sample.state === "invoke_failed";
  if (!failed && sample.state !== "truncated" && !sample.traceback) return null;
  const reason = [sample.error, sample.stop_reason].filter(Boolean).join(" · ");
  return (
    <div>
      <Alert tone={failed || sample.traceback ? "error" : "warn"}>
        <b>{t(`evals.state.${sample.state}`)}</b>
        {sample.status_code !== null && sample.status_code !== 200 && <> · HTTP {sample.status_code}</>}
        <div className="tp-msg-reason">{reason || t("evals.detail.noReason")}</div>
        {sample.state === "truncated" && <div>{t("evals.detail.truncatedHint")}</div>}
      </Alert>
      {sample.traceback && (
        <>
          <LinkButton onClick={() => setTb(!tb)} testId="tp-eval-traceback">
            {tb ? t("evals.detail.hideTraceback") : t("evals.detail.showTraceback")}
          </LinkButton>
          {tb && <pre className="v2-pre tp-log" style={{ marginTop: 8 }}>{sample.traceback}</pre>}
        </>
      )}
    </div>
  );
}

/** Task input, conversation (text / tool calls / tool results) and failure info of one sample. */
export function Transcript({ sample }: { sample: EvalSampleDetail }) {
  const { t } = useTranslation();
  const fields = Object.entries(sample.input.fields);
  return (
    <div className="tp-stack" data-testid="tp-eval-transcript">
      <Failure sample={sample} />
      <Card title={t("evals.detail.input")}>
        {sample.input.prompt ? (
          <LongText text={sample.input.prompt} />
        ) : (
          <span className="v2-muted">{t("evals.detail.noPrompt")}</span>
        )}
        {fields.length > 0 && (
          <div style={{ marginTop: 12 }}>
            <Descriptions one items={fields.map(([k, v]) => ({ label: k, value: <span className="tp-mono">{v}</span> }))} />
          </div>
        )}
      </Card>
      <Card title={t("evals.detail.conversation")} sub={sample.messages.length ? t("evals.detail.messageCount", { n: sample.messages.length }) : undefined}>
        {sample.messages.length === 0 ? (
          <Alert tone="info">
            {!sample.has_transcript && sample.state === "scored" ? t("evals.detail.noTranscript") : t("evals.detail.noTranscriptFailed")}
          </Alert>
        ) : (
          <Messages messages={sample.messages} />
        )}
        {sample.messages_truncated && <Alert tone="warn">{t("evals.detail.messagesTruncated")}</Alert>}
      </Card>
    </div>
  );
}
