import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { StatusTag } from "../components/StatusTag";
import { runApi, servingApi } from "../lib/api";
import { dateTime } from "../lib/format";
import { LIST_POLL_MS, usePoll } from "../lib/poll";
import { useLoad } from "../v2/hooks";
import { Alert, Button, Card, Empty, PageHeader, Table } from "../v2/ui";

export default function ExportsPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const list = useLoad(servingApi.exports, "exports");
  const runs = useLoad(runApi.list, "runs");
  usePoll(list.reload, LIST_POLL_MS);
  const runName = (id: string) => runs.data?.find((r) => r.id === id)?.name ?? id;
  return (
    <>
      <PageHeader title={t("pages.exports.title")} desc={t("pages.exports.desc")} />
      <Alert tone="info">{t("exports.how")}</Alert>
      <div style={{ height: 16 }} />
      {list.data?.length === 0 ? (
        <Empty action={<Button onClick={() => navigate("/runs")}>{t("nav.runs")}</Button>}>{t("exports.empty")}</Empty>
      ) : (
        <Card flush>
          <Table
            rowKey={(e) => e.id}
            rows={list.data ?? []}
            loading={list.loading && !list.data}
            error={list.error}
            columns={[
              { key: "r", title: t("nav.runs"), render: (e) => <button type="button" className="v2-link" onClick={() => navigate(`/runs?view=detail&id=${e.run_id}`)}>{runName(e.run_id)}</button> },
              { key: "st", title: t("runs.step"), render: (e) => `global_step_${e.step}` },
              { key: "s", title: t("common.status"), render: (e) => <StatusTag status={e.status} /> },
              { key: "d", title: t("common.detail"), render: (e) => e.error ?? e.job?.stages[0]?.detail ?? "" },
              { key: "fsx", title: "FSx", render: (e) => <span className="tp-mono">{e.fsx_path ?? "—"}</span> },
              { key: "s3", title: "S3", render: (e) => <span className="tp-mono">{e.s3_uri ?? "—"}</span> },
              { key: "a", title: "", render: (e) => e.status === "succeeded" ? <Button size="sm" kind="primary" onClick={() => navigate(`/inference?view=new&export=${e.id}`)}>{t("exports.deploy")}</Button> : null },
              { key: "c", title: t("common.created"), render: (e) => dateTime(e.created_at) },
            ]}
          />
        </Card>
      )}
    </>
  );
}
