import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { errorMessage, plansApi, type TrainingPlan } from "../lib/api";
import { dateTime } from "../lib/format";
import { useLoad } from "../v2/hooks";
import { Alert, Field, Select } from "../v2/ui";

const USABLE = new Set(["Active", "Scheduled"]);

/**
 * Flexible Training Plan (FTP) for a HyperPod instance group: pick one of the region's
 * HyperPod plans that match the instance type, or paste an ARN (e.g. a plan shared from
 * another team). The selection is validated against the cluster AZs and group size.
 */
export function TrainingPlanField({
  region,
  clusterId,
  instanceType,
  count,
  value,
  onChange,
  onValid,
  disabled,
}: {
  region: string;
  clusterId: string;
  instanceType: string;
  count: number;
  value: string;
  onChange: (arn: string) => void;
  onValid?: (plan: TrainingPlan | null) => void;
  disabled?: boolean;
}) {
  const { t } = useTranslation();
  const plans = useLoad(() => plansApi.list(region), `plans-${region}`);
  const [check, setCheck] = useState<{ plan: TrainingPlan | null; error: string | null; busy: boolean }>({
    plan: null, error: null, busy: false,
  });
  const arn = value.trim();
  const matching = (plans.data ?? []).filter(
    (p) => USABLE.has(p.status) && p.targets.includes("hyperpod-cluster") && (!p.instance_type || p.instance_type === instanceType),
  );
  const listed = matching.some((p) => p.arn === arn);

  useEffect(() => {
    if (!arn || !clusterId) {
      setCheck({ plan: null, error: null, busy: false });
      onValid?.(null);
      return;
    }
    let live = true;
    setCheck((c) => ({ ...c, busy: true }));
    const timer = setTimeout(() => {
      plansApi
        .check({ arn, cluster_id: clusterId, instance_type: instanceType, count: Math.max(1, count) })
        .then((p) => {
          if (!live) return;
          setCheck({ plan: p, error: null, busy: false });
          onValid?.(p);
        })
        .catch((err) => {
          if (!live) return;
          setCheck({ plan: null, error: errorMessage(err), busy: false });
          onValid?.(null);
        });
    }, 400);
    return () => {
      live = false;
      clearTimeout(timer);
    };
    // onValid is a callback prop; re-checking only depends on the inputs
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [arn, clusterId, instanceType, count]);

  const p = check.plan;
  return (
    <>
      <Field label={t("plans.pick")} hint={plans.data && !matching.length ? t("plans.noneMatching", { type: instanceType }) : t("plans.pickHint")} full>
        <Select
          value={listed ? arn : ""}
          disabled={disabled}
          onChange={onChange}
          placeholder={t("clusters.pickPlan")}
          options={matching.map((x) => ({
            value: x.arn,
            label: `${x.name} · ${x.status} · ${x.available ?? "?"}/${x.instances} · ${x.az ?? "?"} · ${dateTime(x.start)} → ${dateTime(x.end)}`,
          }))}
        />
      </Field>
      <Field label={t("plans.arn")} hint={t("plans.arnHint")} error={check.error} full>
        <input
          className="v2-input mono"
          value={value}
          disabled={disabled}
          placeholder="arn:aws:sagemaker:<region>:<account>:training-plan/<name>"
          onChange={(e) => onChange(e.target.value)}
          data-testid="tp-plan-arn"
        />
      </Field>
      {p && (
        <div className="v2-field full">
          <Alert tone="success">
            {t("plans.valid", {
              name: p.name, status: p.status, type: p.instance_type ?? instanceType,
              available: p.available ?? "?", total: p.instances, az: p.az ?? p.az_id ?? "?",
              start: dateTime(p.start), end: dateTime(p.end),
            })}
          </Alert>
        </div>
      )}
      {check.busy && arn && !p && !check.error && <div className="v2-field full"><span className="hint">{t("plans.checking")}</span></div>}
    </>
  );
}
