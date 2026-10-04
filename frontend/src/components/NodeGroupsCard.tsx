import { useState } from "react";
import { useTranslation } from "react-i18next";
import { catalogApi, clusterApi, errorMessage, type Cluster, type NodeGroupBody, type NodeGroupView } from "../lib/api";
import { money } from "../lib/format";
import { useLoad, useV2Toast } from "../v2/hooks";
import { Alert, Button, Card, Confirm, Field, Modal, Select, Table, Tag } from "../v2/ui";
import { StatusTag } from "./StatusTag";

/** EKS managed node groups (plain EC2 nodes) on the HyperPod cluster's EKS cluster. */
export function NodeGroupsCard({ cluster, onJob }: { cluster: Cluster; onJob: (jobId: string) => void }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [form, setForm] = useState<{ open: boolean; ng?: NodeGroupView }>({ open: false });
  const [del, setDel] = useState<string | null>(null);
  const rows = cluster.live?.node_groups ?? [];
  const pluginsReady = cluster.components?.gpu_plugins?.status === "ready";
  const remove = async () => {
    if (!del) return;
    try {
      onJob((await clusterApi.deleteNodegroup(cluster.id, del)).job_id);
    } catch (err) {
      toast("error", errorMessage(err));
    }
    setDel(null);
  };
  return (
    <Card
      title={t("nodegroups.title")}
      sub={t("nodegroups.desc")}
      end={
        <Button kind="primary" size="sm" disabled={cluster.status !== "ready" || !pluginsReady} onClick={() => setForm({ open: true })} testId="tp-add-nodegroup">
          {t("nodegroups.add")}
        </Button>
      }
      flush
    >
      {!pluginsReady && (
        <div style={{ padding: "0 16px 12px" }}>
          <Alert tone="warn">{t("nodegroups.needsComponents")}</Alert>
        </div>
      )}
      {cluster.live?.node_groups_error && (
        <div style={{ padding: "0 16px 12px" }}>
          <Alert tone="error">{cluster.live.node_groups_error}</Alert>
        </div>
      )}
      <Table
        rowKey={(g) => g.name}
        rows={rows}
        empty={t("nodegroups.empty")}
        columns={[
          { key: "n", title: t("common.name"), render: (g) => <b>{g.name}</b> },
          { key: "t", title: t("models.instanceType"), render: (g) => <span className="tp-mono">{g.instance_type}</span> },
          {
            key: "cap",
            title: t("clusters.capacity"),
            render: (g) => (
              <span className="tp-row">
                {t(`nodegroups.capacity_${g.capacity}`)}
                {g.efa && <Tag>EFA</Tag>}
              </span>
            ),
          },
          { key: "c", title: t("clusters.nodesCol"), render: (g) => `${g.current} / ${g.target}` },
          {
            key: "s",
            title: t("common.status"),
            render: (g) => (
              <div>
                <StatusTag status={g.status} />
                {g.failure && <div className="v2-muted" style={{ fontSize: 12, maxWidth: 360 }}>{g.failure}</div>}
              </div>
            ),
          },
          { key: "p", title: t("clusters.hourly"), render: (g) => money((g.price_per_hour ?? 0) * g.running) },
          {
            key: "a",
            title: "",
            render: (g) => (
              <div className="tp-row">
                <Button size="sm" disabled={g.status !== "ACTIVE" && g.status !== "DEGRADED"} onClick={() => setForm({ open: true, ng: g })}>{t("clusters.scale")}</Button>
                <Button size="sm" kind="danger" disabled={g.status === "DELETING"} onClick={() => setDel(g.name)}>{t("common.delete")}</Button>
              </div>
            ),
          },
        ]}
      />
      {form.open && <NodeGroupForm cluster={cluster} initial={form.ng} onClose={() => setForm({ open: false })} onStarted={onJob} />}
      <Confirm
        open={!!del}
        title={t("nodegroups.deleteTitle")}
        body={t("nodegroups.deleteBody", { name: del })}
        confirmLabel={t("common.delete")}
        danger
        onConfirm={() => void remove()}
        onClose={() => setDel(null)}
      />
    </Card>
  );
}

function NodeGroupForm({
  cluster,
  initial,
  onClose,
  onStarted,
}: {
  cluster: Cluster;
  initial?: NodeGroupView;
  onClose: () => void;
  onStarted: (jobId: string) => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const instances = useLoad(() => catalogApi.instances(cluster.region), `inst-${cluster.region}`);
  const [name, setName] = useState(initial?.name ?? "ec2-p5-spot");
  const [itype, setItype] = useState(initial?.instance_type ?? "p5.48xlarge");
  const [capacity, setCapacity] = useState<NodeGroupBody["capacity"]>(initial?.capacity ?? "spot");
  const [count, setCount] = useState(initial ? initial.target : 1);
  const [efa, setEfa] = useState(initial?.efa ?? false);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const row = instances.data?.instances.find((i) => i.type === itype);
  const vcpu = instances.data?.limits.ec2_p_vcpus?.[capacity];
  const price = initial ? (initial.price_per_hour ?? 0) : capacity === "on_demand" ? (row?.ec2_price_per_hour ?? 0) : 0;
  const hourly = price * count;

  const submit = async () => {
    setBusy(true);
    try {
      const r = await clusterApi.applyNodegroup(cluster.id, {
        name, instance_type: initial ? undefined : itype, capacity, count, max_size: Math.max(count, initial?.max ?? 1), efa, confirm_cost: true,
      });
      onStarted(r.job_id);
      onClose();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
      setConfirm(false);
    }
  };

  return (
    <>
      <Modal
        open
        title={initial ? t("nodegroups.scaleTitle", { name: initial.name }) : t("nodegroups.add")}
        onClose={onClose}
        footer={
          <>
            <Button onClick={onClose}>{t("v2.common.cancel")}</Button>
            <Button kind="primary" disabled={busy || !name} onClick={() => (count > 0 ? setConfirm(true) : void submit())} testId="tp-nodegroup-submit">
              {t("common.apply")}
            </Button>
          </>
        }
      >
        <div className="v2-form cols-2">
          <Field label={t("common.name")} required hint={t("nodegroups.nameHint")}>
            <input className="v2-input" value={name} disabled={!!initial} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label={t("models.instanceType")} required>
            <Select
              value={itype}
              disabled={!!initial}
              onChange={setItype}
              options={(instances.data?.instances ?? []).map((i) => ({
                value: i.type,
                label: `${i.type} · ${i.gpus}×${i.gpu} · ${t("nodegroups.odPrice", { price: money(i.ec2_price_per_hour) })}`,
              }))}
            />
          </Field>
          <Field label={t("clusters.capacity")} hint={vcpu != null ? t("nodegroups.vcpuQuota", { n: vcpu }) : undefined}>
            <Select
              value={capacity}
              disabled={!!initial}
              onChange={(v) => setCapacity(v as NodeGroupBody["capacity"])}
              options={[
                { value: "spot", label: t("nodegroups.capacity_spot") },
                { value: "on_demand", label: t("nodegroups.capacity_on_demand") },
              ]}
            />
          </Field>
          <Field label={t("clusters.count")} required hint={t("clusters.countHint")}>
            <input className="v2-input" type="number" min={0} max={20} value={count} onChange={(e) => setCount(Math.max(0, Number(e.target.value) || 0))} />
          </Field>
          {!initial && (
            <Field label="EFA" hint={row && !row.multi_node ? t("runs.singleNodeOnly") : t("nodegroups.efaHint")} full>
              <label className="tp-row">
                <input type="checkbox" checked={efa} disabled={!row?.multi_node} onChange={(e) => setEfa(e.target.checked)} />
                {t("nodegroups.efaOn")}
              </label>
            </Field>
          )}
        </div>
        {count > 0 && (
          <div style={{ marginTop: 12 }}>
            <Alert tone="warn">
              {capacity === "spot" && !initial ? t("nodegroups.spotWarn") : t("clusters.costWarn", { cost: money(hourly) })}
            </Alert>
          </div>
        )}
      </Modal>
      <Confirm
        open={confirm}
        title={t("clusters.confirmCostTitle")}
        body={capacity === "spot" && !initial ? t("nodegroups.confirmSpot", { count, type: itype }) : t("clusters.confirmCostBody", { count, type: itype, cost: money(hourly) })}
        confirmLabel={t("clusters.confirmCost")}
        danger
        busy={busy}
        onConfirm={() => void submit()}
        onClose={() => setConfirm(false)}
      />
    </>
  );
}
