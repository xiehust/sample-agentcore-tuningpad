import { useState } from "react";
import { useTranslation } from "react-i18next";

import { JobPanel } from "../components/JobPanel";
import { StatusTag } from "../components/StatusTag";
import { errorMessage, setupApi, type Preflight } from "../lib/api";
import { regionOptions } from "../lib/regions";
import { useLoad, useV2Toast } from "../v2/hooks";
import { Alert, Button, Card, Descriptions, Field, PageHeader, Select, Spin, Table } from "../v2/ui";

export default function SettingsPage() {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const setup = useLoad(setupApi.get, "setup");
  const [region, setRegion] = useState<string | null>(null);
  const [preflight, setPreflight] = useState<Preflight | null>(null);
  const [busy, setBusy] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);

  const current = region ?? setup.data?.default_region ?? "us-east-1";
  const res = setup.data?.regions[current];
  const lastJob = setup.data?.jobs[current];

  const runPreflight = async () => {
    setBusy(true);
    try {
      setPreflight(await setupApi.preflight(current));
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const runSetup = async () => {
    setBusy(true);
    try {
      const r = await setupApi.setupRegion(current);
      setJobId(r.job_id);
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const checks = preflight?.checks ?? (setup.data?.last_preflight.region === current ? setup.data?.last_preflight.checks : undefined);

  return (
    <>
      <PageHeader title={t("pages.settings.title")} desc={t("pages.settings.desc")} />
      {setup.loading && !setup.data ? (
        <Spin />
      ) : (
        <div className="tp-stack">
          <Card title={t("settings.region")}>
            <div className="v2-form cols-2">
              <Field label={t("settings.region")} hint={t("settings.regionHint")}>
                <Select value={current} options={regionOptions} onChange={setRegion} testId="tp-settings-region" />
              </Field>
              <Field label={t("settings.account")}>
                <span className="tp-mono">{setup.data?.account_id ?? "—"}</span>
              </Field>
            </div>
          </Card>

          <Card
            title={t("settings.preflight")}
            sub={t("settings.preflightDesc")}
            end={
              <Button kind="primary" size="sm" disabled={busy} onClick={() => void runPreflight()} testId="tp-run-preflight">
                {t("settings.runPreflight")}
              </Button>
            }
          >
            {checks ? (
              <Table
                rowKey={(c) => c.id}
                rows={checks}
                columns={[
                  { key: "s", title: t("common.status"), width: 110, render: (c) => <StatusTag status={c.status} /> },
                  { key: "id", title: t("settings.check"), width: 160, render: (c) => <code className="tp-mono">{c.id}</code> },
                  { key: "m", title: t("common.detail"), render: (c) => c.message },
                ]}
              />
            ) : (
              <p className="v2-muted">{t("settings.preflightNever")}</p>
            )}
          </Card>

          <Card
            title={t("settings.project")}
            sub={t("settings.projectDesc")}
            end={
              <div className="tp-row">
                {res?.status && <StatusTag status={res.status} />}
                <Button kind="primary" size="sm" disabled={busy || res?.status === "provisioning"} onClick={() => void runSetup()} testId="tp-run-setup">
                  {res?.status === "ready" ? t("settings.reapply") : t("settings.setup")}
                </Button>
              </div>
            }
          >
            {res?.error && <Alert tone="error">{res.error}</Alert>}
            <Descriptions
              items={[
                { label: t("settings.bucket"), value: res?.bucket && <span className="tp-mono">s3://{res.bucket}</span> },
                { label: t("settings.acrRole"), value: res?.acr_role_arn && <span className="tp-mono">{res.acr_role_arn}</span> },
                { label: t("settings.codebuildRole"), value: res?.codebuild_role_arn && <span className="tp-mono">{res.codebuild_role_arn}</span> },
                { label: t("settings.agentRepo"), value: res?.agent_repo_uri && <span className="tp-mono">{res.agent_repo_uri}</span> },
                { label: t("settings.trainerRepo"), value: res?.trainer_repo_uri && <span className="tp-mono">{res.trainer_repo_uri}</span> },
              ]}
            />
          </Card>
          {(jobId ?? lastJob?.id) && (
            <JobPanel jobId={(jobId ?? lastJob?.id) as string} title={t("settings.setupJob")} onDone={() => setup.reload()} />
          )}
        </div>
      )}
    </>
  );
}
