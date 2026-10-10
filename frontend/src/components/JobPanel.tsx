import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, localizedMessage, type Job } from "../lib/api";
import { LIVE_JOB, useJob } from "../lib/useJob";
import { DETAIL_POLL_MS, usePoll } from "../lib/poll";
import { duration } from "../lib/format";
import { Alert, Button, Card } from "../v2/ui";
import { StatusTag } from "./StatusTag";


/** Stage bar of one pipeline job (launchpad LaunchSequence pattern). */
export function StageBar({ job }: { job: Job }) {
  const { t, i18n } = useTranslation();
  return (
    <div className="tp-stages" data-testid="tp-stages">
      {job.stages.map((st, i) => {
        const key = `stages.${st.name}`;
        return (
          <span key={st.name} className={`tp-stage ${st.status}`} title={st.detail ?? undefined}>
            <span className="n">{st.status === "succeeded" ? "✓" : st.status === "failed" ? "✕" : i + 1}</span>
            {i18n.exists(key) ? t(key) : st.name}
            {st.status === "running" && st.detail && <span className="d">· {st.detail}</span>}
            {st.status === "succeeded" && st.started_at && st.ended_at && (
              <span className="d">{duration(st.started_at, st.ended_at)}</span>
            )}
          </span>
        );
      })}
    </div>
  );
}

/** Incremental log tail by byte offset (backend caps each chunk). */
export function JobLog({ jobId, live }: { jobId: string; live: boolean }) {
  const { t } = useTranslation();
  const [text, setText] = useState("");
  const offset = useRef(0);
  const box = useRef<HTMLPreElement>(null);

  useEffect(() => {
    offset.current = 0;
    setText("");
  }, [jobId]);

  const tick = useCallback(async () => {
    try {
      for (let guard = 0; guard < 40; guard += 1) {
        const chunk = await api.jobLog(jobId, offset.current);
        offset.current = chunk.next_offset;
        if (chunk.content) setText((prev) => prev + chunk.content);
        if (chunk.eof) return;
      }
    } catch {
      /* transient — next tick retries */
    }
  }, [jobId]);

  useEffect(() => {
    void tick();
  }, [tick]);
  usePoll(() => void tick(), DETAIL_POLL_MS, live);

  useEffect(() => {
    if (live && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [text, live]);

  return (
    <pre ref={box} className="v2-pre tp-log" data-testid="tp-job-log">
      {text || <span className="v2-muted">{t("job.logWaiting")}</span>}
    </pre>
  );
}

export function JobPanel({
  jobId,
  title,
  onDone,
  defaultLog = false,
}: {
  jobId: string;
  title?: string;
  onDone?: (job: Job) => void;
  defaultLog?: boolean;
}) {
  const { t } = useTranslation();
  const { job, error, reload } = useJob(jobId, onDone);
  const [showLog, setShowLog] = useState(defaultLog);
  const [busy, setBusy] = useState(false);
  if (error) return <Alert tone="error">{error}</Alert>;
  if (!job) return null;
  const jobError = job.error_code && job.error ? localizedMessage(job.error_code, job.error) : job.error;
  const live = LIVE_JOB.has(job.status);
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
      await reload();
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card
      title={title ?? t("job.title")}
      sub={<StatusTag status={job.status} />}
      end={
        <div className="tp-row">
          {live && (
            <Button size="sm" disabled={busy} onClick={() => void act(() => api.cancelJob(job.id))}>
              {t("job.cancel")}
            </Button>
          )}
          {(job.status === "failed" || job.status === "cancelled") && (
            <Button size="sm" kind="primary" disabled={busy} onClick={() => void act(() => api.retryJob(job.id))}>
              {t("job.retry")}
            </Button>
          )}
          <Button size="sm" onClick={() => setShowLog((v) => !v)}>
            {showLog ? t("job.hideLog") : t("job.showLog")}
          </Button>
        </div>
      }
      testId="tp-job-panel"
    >
      <StageBar job={job} />
      {job.error && (
        <div style={{ marginTop: 12, overflowWrap: "anywhere" }}>
          <Alert tone="error">
            <div>{job.error_code && <code className="tp-mono">{job.error_code}</code>} {jobError}</div>
            {jobError !== job.error && (
              <div className="v2-muted" style={{ marginTop: 4, whiteSpace: "pre-wrap" }}>{job.error}</div>
            )}
          </Alert>
        </div>
      )}
      {showLog && (
        <div style={{ marginTop: 12 }}>
          <JobLog jobId={job.id} live={live} />
        </div>
      )}
    </Card>
  );
}
