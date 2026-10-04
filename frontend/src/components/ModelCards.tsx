import { useTranslation } from "react-i18next";

import type { ModelCheck, Plan } from "../lib/api";
import { num } from "../lib/format";
import { Alert, Card, Descriptions, Tag } from "../v2/ui";

export function ModelCheckCard({ check }: { check: ModelCheck }) {
  const { t } = useTranslation();
  return (
    <Card
      title={check.model_id}
      sub={check.compatible ? <Tag tone="green" dot>{t("models.compatible")}</Tag> : <Tag tone="red" dot>{t("models.incompatible")}</Tag>}
      testId="tp-model-check"
    >
      {!check.compatible && check.reason && <Alert tone="error">{check.reason}</Alert>}
      <Descriptions
        items={[
          { label: t("models.schema"), value: check.schema && <code className="tp-mono">{check.schema}</code> },
          { label: t("models.params"), value: check.params_b ? `${num(check.params_b)} B` : "—" },
          { label: t("models.moe"), value: check.is_moe ? t("models.moeValue", { experts: check.num_experts, active: num(check.active_params_b) }) : t("common.no") },
          { label: t("models.context"), value: num(check.max_position_embeddings, 0) },
          { label: t("models.multimodal"), value: check.multimodal ? t("common.yes") : t("common.no") },
          { label: t("models.parsers"), value: check.tool_call_parser ? `${check.tool_call_parser} / ${check.reasoning_parser}` : "—" },
        ]}
      />
    </Card>
  );
}

export function PlanCard({ plan }: { plan: Plan }) {
  const { t } = useTranslation();
  return (
    <Card title={t("models.planTitle")} sub={<Tag tone="blue">{t(`strategy.${plan.strategy}`)}</Tag>} testId="tp-plan">
      {plan.warnings.map((w) => (
        <div key={w} style={{ marginBottom: 8 }}>
          <Alert tone="warn">{w}</Alert>
        </div>
      ))}
      <Descriptions
        items={[
          { label: t("models.profile"), value: plan.profile },
          { label: t("models.parallel"), value: `TP ${plan.tp} · CP ${plan.cp} · EP ${plan.ep} · rollout TP ${plan.rollout_tp}` },
          { label: t("models.lora"), value: plan.lora_rank ? `r${plan.lora_rank} / α${plan.lora_alpha}` : t("common.no") },
          { label: t("models.lr"), value: plan.lr.toExponential(0) },
          { label: t("models.memory"), value: `${plan.train_state_gib_per_gpu} / ${plan.gpu_mem_gib} GiB` },
          { label: t("models.offload"), value: [plan.param_offload && "param", plan.optimizer_offload && "optimizer", plan.grad_offload && "grad"].filter(Boolean).join(", ") || t("common.no") },
          { label: t("models.gpuUtil"), value: plan.gpu_memory_utilization },
          { label: t("models.world"), value: plan.world_size },
        ]}
      />
      {plan.reasons.length > 0 && <p className="v2-muted" style={{ marginTop: 10 }}>{plan.reasons.join(" · ")}</p>}
    </Card>
  );
}

