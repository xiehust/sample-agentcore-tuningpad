import { useCallback, useEffect, useRef, useState } from "react";

import { api, errorMessage, type Job } from "./api";
import { DETAIL_POLL_MS, usePoll } from "./poll";

export const LIVE_JOB = new Set(["queued", "running"]);

/** Poll a job until terminal; `onDone` fires once on the transition. */
export function useJob(jobId: string | null | undefined, onDone?: (job: Job) => void) {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  const doneRef = useRef(onDone);
  doneRef.current = onDone;
  const prev = useRef<string | null>(null);

  const load = useCallback(async () => {
    if (!jobId) return;
    try {
      const j = await api.job(jobId);
      setJob(j);
      setError(null);
      if (prev.current && LIVE_JOB.has(prev.current) && !LIVE_JOB.has(j.status)) doneRef.current?.(j);
      prev.current = j.status;
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [jobId]);

  useEffect(() => {
    prev.current = null;
    setJob(null);
    void load();
  }, [load]);
  usePoll(() => void load(), DETAIL_POLL_MS, !!jobId && (!job || LIVE_JOB.has(job.status)));
  return { job, error, reload: load };
}

