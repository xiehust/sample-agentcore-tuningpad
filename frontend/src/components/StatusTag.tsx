import { useTranslation } from "react-i18next";

import { Tag, type TagTone } from "../v2/ui";

const TONES: Record<string, TagTone> = {
  ready: "green", succeeded: "green", InService: "green", READY: "green", Running: "green", running: "blue",
  active: "green", Active: "green", ok: "green",
  queued: "gray", pending: "gray", Pending: "gray", creating: "blue", provisioning: "blue", installing: "blue",
  Creating: "blue", Updating: "blue", deploying: "blue", building: "blue", scaling: "blue", deleting: "orange",
  SystemUpdating: "blue", warn: "orange", degraded: "orange", interrupted: "orange", stopping: "orange",
  failed: "red", Failed: "red", fail: "red", cancelled: "gray", stopped: "gray", deleted: "gray",
  delete_failed: "red", CREATE_FAILED: "red", Unavailable: "red",
};

/** Status chip with a localized label when `status.<value>` exists. */
export function StatusTag({ status }: { status: string | null | undefined }) {
  const { t, i18n } = useTranslation();
  const s = status ?? "unknown";
  const key = `status.${s}`;
  return (
    <Tag tone={TONES[s] ?? "gray"} dot>
      {i18n.exists(key) ? t(key) : s}
    </Tag>
  );
}
