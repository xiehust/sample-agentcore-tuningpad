// The only typed backend client. Every endpoint goes through `request`, which
// turns the backend envelope {code, message, detail} into an ApiError whose
// message is localized when `apiErrors.<code>` exists.
import i18n from "../i18n";

export class ApiError extends Error {
  code: string;
  detail: unknown;
  status: number;
  constructor(code: string, message: string, detail: unknown, status: number) {
    super(message);
    this.code = code;
    this.detail = detail;
    this.status = status;
  }
}

export const AUTH_UNAUTHORIZED_EVENT = "tuningpad-unauthorized";

export function localizedMessage(code: string, fallback: string): string {
  const key = `apiErrors.${code}`;
  return i18n.exists(key) ? i18n.t(key) : fallback;
}

async function parseResponse<T>(path: string, res: Response): Promise<T> {
  const clone = res.clone();
  const body: unknown = await res.json().catch(() => undefined);
  if (!res.ok) {
    if (res.status === 401 && !path.startsWith("/api/auth/")) {
      window.dispatchEvent(new Event(AUTH_UNAUTHORIZED_EVENT));
    }
    const env = (body ?? {}) as { code?: string; message?: string; detail?: unknown };
    const code = env.code ?? `http.${res.status}`;
    throw new ApiError(code, localizedMessage(code, env.message ?? res.statusText), env.detail, res.status);
  }
  if (body === undefined) {
    const text = await clone.text().catch(() => "");
    throw new ApiError("http.invalid_json", `invalid JSON response from ${path}: ${text.slice(0, 120)}`, null, res.status);
  }
  return body as T;
}

type Init = Omit<RequestInit, "headers" | "body"> & { headers?: Record<string, string>; json?: unknown };

export async function request<T>(path: string, init: Init = {}): Promise<T> {
  const { json, headers, ...rest } = init;
  const res = await fetch(path, {
    ...rest,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...headers },
    body: json === undefined ? undefined : JSON.stringify(json),
  });
  return parseResponse<T>(path, res);
}

export async function requestForm<T>(path: string, form: FormData): Promise<T> {
  const res = await fetch(path, { method: "POST", body: form, credentials: "same-origin" });
  return parseResponse<T>(path, res);
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof TypeError) return i18n.t("apiErrors.network");
  return err instanceof Error ? err.message : String(err);
}

const get = <T,>(path: string) => request<T>(path);
const post = <T,>(path: string, json?: unknown) => request<T>(path, { method: "POST", json: json ?? {} });
const patch = <T,>(path: string, json: unknown) => request<T>(path, { method: "PATCH", json });
const del = <T,>(path: string) => request<T>(path, { method: "DELETE" });
export const http = { get, post, patch, del };

/* ---------- shared shapes ---------- */

export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled" | "interrupted";

export interface JobStage {
  name: string;
  status: "pending" | "running" | "succeeded" | "failed" | "cancelled";
  detail: string | null;
  started_at: string | null;
  ended_at: string | null;
}

export interface Job {
  id: string;
  type: string;
  target_id: string | null;
  status: JobStatus;
  stages: JobStage[];
  error: string | null;
  error_code: string | null;
  created_at: string;
  started_at: string | null;
  ended_at: string | null;
}

export interface LogChunk {
  content: string;
  next_offset: number;
  eof: boolean;
}

export interface AuthStatus {
  auth_required: boolean;
  authenticated: boolean;
}

export interface Meta {
  version: string;
  default_region: string;
  resource_tag: string;
  toolkit_path: string;
  toolkit_present: boolean;
  run_mode: string;
}

export const api = {
  authStatus: () => get<AuthStatus>("/api/auth/status"),
  login: (password: string) => post<{ ok: boolean }>("/api/auth/login", { password }),
  logout: () => post<{ ok: boolean }>("/api/auth/logout"),
  meta: () => get<Meta>("/api/meta"),
  job: (id: string) => get<Job>(`/api/jobs/${id}`),
  jobs: (q: { target_id?: string; type?: string } = {}) =>
    get<Job[]>(`/api/jobs?${new URLSearchParams(q as Record<string, string>)}`),
  jobLog: (id: string, offset: number) => get<LogChunk>(`/api/jobs/${id}/log?offset=${offset}`),
  cancelJob: (id: string) => post(`/api/jobs/${id}/cancel`),
  retryJob: (id: string) => post(`/api/jobs/${id}/retry`),
};

/* ---------- setup ---------- */

export interface Check {
  id: string;
  status: "ok" | "warn" | "fail";
  message: string;
  detail: unknown;
}

export interface Preflight {
  region: string;
  account: string;
  checks: Check[];
  ok: boolean;
}

export interface RegionResources {
  status?: "provisioning" | "ready" | "failed";
  bucket?: string;
  acr_role_arn?: string;
  codebuild_role_arn?: string;
  agent_repo_uri?: string;
  trainer_repo_uri?: string;
  error?: string | null;
}

export interface SetupView {
  account_id: string | null;
  principal_arn: string | null;
  default_region: string;
  regions: Record<string, RegionResources>;
  last_preflight: Partial<Preflight>;
  jobs: Record<string, Job>;
}

/* ---------- catalog ---------- */

export interface InstanceRow {
  type: string;
  ml_type: string;
  gpu: string;
  gpus: number;
  gpu_mem_gib: number;
  vcpus: number;
  mem_gib: number;
  efa: number;
  arch: string;
  multi_node: boolean;
  /** false: inference-only type (vLLM on an EC2 node group) */
  training: boolean;
  price_per_hour: number | null;
  ec2_price_per_hour: number | null;
  quota: { on_demand: number | null; spot: number | null };
}

export interface InstanceCatalog {
  region: string;
  instances: InstanceRow[];
  limits: {
    per_cluster: number | null;
    total: number | null;
    spot_total: number | null;
    ec2_p_vcpus?: { on_demand?: number | null; spot?: number | null };
    ec2_g_vcpus?: { on_demand?: number | null; spot?: number | null };
  };
  errors: string[];
}

export interface ModelCheck {
  model_id: string;
  compatible: boolean;
  schema: string | null;
  reason: string | null;
  template_source: string | null;
  params: number | null;
  params_b: number | null;
  is_moe: boolean;
  num_experts: number | null;
  active_params_b: number | null;
  max_position_embeddings: number | null;
  architectures: string[];
  multimodal: boolean;
  gated: boolean;
  tool_call_parser: string | null;
  reasoning_parser: string | null;
}

export interface ModelPreset {
  id: string;
  verified: string | null;
}

export interface Plan {
  strategy: "fsdp_full" | "fsdp_lora" | "megatron_lora" | "megatron_full";
  profile: "fsdp" | "megatron";
  lora_rank: number | null;
  lora_alpha: number | null;
  lr: number;
  tp: number;
  cp: number;
  ep: number;
  rollout_tp: number;
  gpu_memory_utilization: number;
  param_offload: boolean;
  optimizer_offload: boolean;
  grad_offload: boolean;
  world_size: number;
  train_state_gib_per_gpu: number;
  gpu_mem_gib: number;
  warnings: string[];
  reasons: string[];
}

export interface PlanRequest {
  model_id: string;
  instance_type: string;
  nodes: number;
  max_model_len: number;
  prefer?: string;
  tuning?: string;
}

/* ---------- clusters ---------- */

export interface ComponentState {
  status: "installing" | "ready" | "failed";
  detail: string | null;
}

export interface GroupView {
  name: string;
  instance_type: string;
  target: number;
  current: number;
  status: string;
  training_plan_arn: string | null;
  spot: boolean;
  deep_health_checks: string[];
  price_per_hour?: number | null;
}

export interface Cluster {
  id: string;
  name: string;
  region: string;
  source: "create" | "import";
  status: string;
  cfn_stack: string | null;
  eks_name: string | null;
  hyperpod_name: string | null;
  hyperpod_arn: string | null;
  network: {
    vpc_id?: string;
    private_subnets?: string[];
    azs?: string[];
    az_ids?: string[];
    cluster_sgs?: string[];
    sg_acr?: string;
    sg_nlb?: string;
    fsx_id?: string | null;
    fsx_capacity_gib?: number | null;
  };
  components: Record<string, ComponentState>;
  params: Record<string, unknown>;
  idle_policy: { idle_minutes?: number; budget_usd?: number | null; groups?: Record<string, { idle_minutes?: number; managed?: boolean }> };
  created_at: string;
  job: Job | null;
  live?: {
    status: string;
    failure: string | null;
    node_provisioning_mode?: string | null;
    instance_groups: GroupView[];
    node_groups?: NodeGroupView[];
    node_groups_error?: string;
  };
}

export interface ClusterNode {
  id: string;
  group: string;
  type: string;
  status: string;
  message: string | null;
  launch_time: string | null;
}

export interface CreateClusterBody {
  name: string;
  region: string;
  az_ids: string[];
  fsx_capacity_gib: number;
  vpc_cidr?: string;
  idle_minutes: number;
  budget_usd: number | null;
}

export interface ClusterPreview {
  stack_name: string;
  template_url: string;
  parameters: { ParameterKey: string; ParameterValue: string }[];
  system_instance: string;
  system_price_per_hour: number | null;
  fsx_monthly_usd_estimate: number;
  notes: string[];
}

/** EKS managed node group (plain EC2) — the second compute pool next to HyperPod groups. */
export interface NodeGroupView {
  name: string;
  provider: "ec2";
  instance_type: string;
  capacity: "on_demand" | "spot";
  efa: boolean;
  status: string;
  target: number;
  max: number;
  current: number;
  running: number;
  subnets: string[];
  failure: string | null;
  price_per_hour: number | null;
}

export interface NodeGroupBody {
  name: string;
  instance_type?: string;
  capacity: "on_demand" | "spot";
  count: number;
  max_size?: number | null;
  efa?: boolean;
  confirm_cost?: boolean;
}

export interface GroupBody {
  group: string;
  instance_type?: string;
  count: number;
  capacity: "on_demand" | "training_plan" | "spot";
  training_plan_arn?: string | null;
  deep_health_checks?: boolean;
  confirm_cost?: boolean;
}

export interface Ledger {
  groups: Record<string, { node_hours: number; cost_usd: number; idle_since: number | null; current?: number }>;
  total_cost_usd: number;
  actions: { at: number; group: string; action: string; reason: string }[];
  alert?: string;
  updated_at_epoch?: number;
}

export interface PlanOffering {
  id: string;
  upfront_fee: number;
  currency: string;
  duration_hours: number;
  start: string;
  end: string;
  az: string;
  instance_type: string;
  count: number;
}

export interface TrainingPlan {
  arn: string;
  name: string;
  status: string;
  start: string;
  end: string;
  instances: number;
  available: number | null;
  in_use: number | null;
  upfront_fee: string | number;
  currency: string | null;
  targets: string[];
  instance_type: string | null;
  az: string | null;
  az_id: string | null;
}

const q = (o: Record<string, string | number | boolean | undefined | null>) =>
  new URLSearchParams(
    Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== "").map(([k, v]) => [k, String(v)]),
  ).toString();

export const setupApi = {
  get: () => get<SetupView>("/api/setup"),
  preflight: (region: string) => post<Preflight>("/api/setup/preflight", { region }),
  setupRegion: (region: string) => post<{ job_id: string }>("/api/setup/region", { region }),
};

export const catalogApi = {
  instances: (region: string) => get<InstanceCatalog>(`/api/catalog/instances?${q({ region })}`),
  presets: () => get<ModelPreset[]>("/api/catalog/models/presets"),
  checkModel: (model_id: string) => post<ModelCheck>("/api/catalog/models/check", { model_id }),
  plan: (body: PlanRequest) => post<{ model: ModelCheck; instance: string; plan: Plan }>("/api/catalog/plan", body),
};

export const clusterApi = {
  list: () => get<Cluster[]>("/api/clusters"),
  get: (id: string) => get<Cluster>(`/api/clusters/${id}`),
  azs: (region: string) => get<{ name: string; id: string }[]>(`/api/clusters/azs?${q({ region })}`),
  discoverable: (region: string) =>
    get<{ name: string; arn: string; status: string }[]>(`/api/clusters/discoverable?${q({ region })}`),
  preview: (body: CreateClusterBody) => post<ClusterPreview>("/api/clusters/preview", body),
  create: (body: CreateClusterBody) => post<{ id: string; job_id: string }>("/api/clusters", body),
  import: (body: { region: string; hyperpod_name: string; idle_minutes: number; budget_usd: number | null }) =>
    post<{ id: string; job_id: string }>("/api/clusters/import", body),
  nodes: (id: string) => get<ClusterNode[]>(`/api/clusters/${id}/nodes`),
  repair: (id: string) => post<{ job_id: string }>(`/api/clusters/${id}/components`),
  scale: (id: string, body: GroupBody) => post<{ job_id: string }>(`/api/clusters/${id}/groups`, body),
  deleteGroup: (id: string, group: string) => del<{ job_id: string }>(`/api/clusters/${id}/groups/${group}`),
  applyNodegroup: (id: string, body: NodeGroupBody) => post<{ job_id: string }>(`/api/clusters/${id}/nodegroups`, body),
  deleteNodegroup: (id: string, name: string) => del<{ job_id: string }>(`/api/clusters/${id}/nodegroups/${name}`),
  setPolicy: (id: string, body: { idle_minutes: number; budget_usd: number | null; groups?: Record<string, unknown> }) =>
    request<{ ok: boolean; applied_to_cluster: boolean }>(`/api/clusters/${id}/policy`, { method: "PUT", json: body }),
  ledger: (id: string) => get<Ledger>(`/api/clusters/${id}/ledger`),
  remove: (id: string, confirm: string) => del<{ job_id: string }>(`/api/clusters/${id}?${q({ confirm })}`),
};

export const plansApi = {
  list: (region: string) => get<TrainingPlan[]>(`/api/training-plans?${q({ region })}`),
  check: (p: { arn: string; cluster_id: string; instance_type: string; count: number }) =>
    get<TrainingPlan>(`/api/training-plans/check?${q(p)}`),
  search: (body: { region: string; instance_type: string; count: number; duration_hours: number }) =>
    post<PlanOffering[]>("/api/training-plans/search", body),
  buy: (body: { region: string; offering_id: string; name: string; confirm_upfront_fee: number }) =>
    post<{ arn: string }>("/api/training-plans", body),
};

export { q as queryString };

/* ---------- agents / templates ---------- */

export type Localized = { en: string; "zh-CN": string };

export interface TemplateParam {
  key: string;
  type: "text" | "select" | "number";
  label: Localized;
  default: string | number;
  options?: string[];
  min?: number;
  max?: number;
}

export interface TemplatePreset {
  id: string;
  label: Localized;
  model_id: string;
  instance_type: string;
  nodes: number;
  params: Record<string, number | boolean>;
}

export interface Template {
  id: string;
  name: Localized;
  description: Localized;
  contract: string;
  verified: boolean;
  params: TemplateParam[];
  payload: { prompt_field: string | null; required: string[]; explicit_prompt_column?: boolean };
  dataset?: { builtin?: string };
  agent_loop: Record<string, unknown>;
  presets: TemplatePreset[];
}

export type PlatformChoice = "auto" | "V1" | "V2";

export interface AgentRuntimeView {
  id: string;
  cluster_id: string | null;
  region: string;
  network_mode: "VPC" | "PUBLIC";
  runtime_id: string | null;
  runtime_arn: string | null;
  image_uri: string | null;
  platform_version: "V1" | "V2" | null;
  status: string;
  last_smoke: { ok?: boolean; passed?: number; total?: number; model_id?: string; results?: { index: number; ok: boolean; rewards?: unknown; error?: string | null }[] };
  job: Job | null;
}

export interface AgentView {
  id: string;
  name: string;
  source: "template" | "upload" | "image";
  template_id: string | null;
  contract: string;
  config: { region?: string; params?: Record<string, unknown>; smoke_payloads?: unknown[]; filename?: string };
  image_uri: string | null;
  status: string;
  checks: { static?: { errors: string[]; warnings: string[] }; probe?: { ping: number }; image?: { size_bytes: number; architectures: string[] } };
  created_at: string;
  job: Job | null;
  runtimes: AgentRuntimeView[];
}

export const agentApi = {
  templates: () => get<Template[]>("/api/templates"),
  list: () => get<AgentView[]>("/api/agents"),
  get: (id: string) => get<AgentView>(`/api/agents/${id}`),
  create: (body: { name: string; source: "template" | "image"; template_id?: string; params?: Record<string, unknown>; image_uri?: string; smoke_payloads?: unknown[]; region: string }) =>
    post<{ id: string; job_id: string }>("/api/agents", body),
  upload: (form: FormData) => requestForm<{ id: string; job_id: string }>("/api/agents/upload", form),
  rebuild: (id: string) => post<{ job_id: string }>(`/api/agents/${id}/rebuild`),
  deploy: (id: string, body: { cluster_id: string | null; smoke_payloads?: unknown[] | null; skip_smoke?: boolean; platform_version?: PlatformChoice }) =>
    post<{ id: string; job_id: string }>(`/api/agents/${id}/runtimes`, body),
  deleteRuntime: (id: string, rt: string) => del<{ ok: boolean }>(`/api/agents/${id}/runtimes/${rt}`),
  remove: (id: string) => del<{ ok: boolean }>(`/api/agents/${id}`),
};

/* ---------- datasets ---------- */

export interface DatasetView {
  id: string;
  name: string;
  region: string;
  source: string;
  template_id: string | null;
  status: string;
  splits: Record<string, { s3_key: string; s3_uri: string; rows: number }>;
  prompt_field: string;
  has_prompt_column: boolean;
  stats: Record<string, number>;
  created_at: string;
  job: Job | null;
}

export const datasetApi = {
  list: () => get<DatasetView[]>("/api/datasets"),
  get: (id: string) => get<DatasetView>(`/api/datasets/${id}`),
  preview: (id: string, split: string) => get<Record<string, unknown>[]>(`/api/datasets/${id}/preview?${q({ split })}`),
  builtin: (body: { template_id: string; name?: string; region: string; limit?: number | null }) =>
    post<{ id: string; job_id: string }>("/api/datasets/builtin", body),
  upload: (form: FormData) => requestForm<{ id: string; job_id: string }>("/api/datasets/upload", form),
  remove: (id: string) => del<{ ok: boolean }>(`/api/datasets/${id}`),
};

/* ---------- runs ---------- */

export interface RunCompute {
  instance_group: string;
  instance_type: string;
  nodes: number;
  provider?: "hyperpod" | "ec2";
  capacity: "on_demand" | "training_plan" | "spot";
  training_plan_arn?: string | null;
  scale_up: boolean;
  scale_down_after: boolean;
  max_hours: number | null;
  budget_usd: number | null;
  max_retries: number;
}

export interface CreateRunBody {
  name: string;
  agent_runtime_id: string;
  train_dataset_id: string;
  val_dataset_id?: string | null;
  val_split: string;
  model_id: string;
  prefer: string;
  tuning: string;
  params: Record<string, unknown>;
  compute: RunCompute;
  confirm_cost?: boolean;
}

export interface RunEstimate {
  prepaid: boolean;
  price_per_node_hour: number;
  nodes: number;
  hourly_usd: number;
  max_hours: number | null;
  max_cost_usd: number | null;
}

export interface RunSummary {
  last_step: number | null;
  val_reward?: number | null;
  best_val_reward?: number | null;
  best_step?: number | null;
  baseline_val_reward?: number | null;
  train_score?: number | null;
  sec_per_step?: number | null;
}

export interface RunView {
  id: string;
  name: string;
  agent_runtime_id: string;
  cluster_id: string;
  train_dataset_id: string;
  val_dataset_id: string | null;
  model_id: string;
  spec: { plan: Plan; params: Record<string, unknown>; val_split: string; estimate?: RunEstimate };
  compute: RunCompute;
  status: string;
  rayjob_name: string | null;
  retries: number;
  started_at: string | null;
  ended_at: string | null;
  node_hours: number;
  est_cost_usd: number;
  progress: { step?: number; attempt?: number; rayjob?: { job?: string; deployment?: string; message?: string } | null; ckpt_steps?: number[]; hydra?: string[] };
  error: string | null;
  created_at: string;
  job: Job | null;
  summary: RunSummary;
  series?: Record<string, [number, number][]>;
  metric_keys?: string[];
}

export interface RunPod {
  name: string;
  type: string;
  phase: string;
  node: string | null;
  ip: string | null;
  reason: string | null;
}

export interface TrainerImageView {
  id: string;
  profile: string;
  region: string;
  toolkit_sha: string;
  image_uri: string;
  status: string;
  build_id: string | null;
  created_at: string;
  job: Job | null;
}

export const runApi = {
  list: () => get<RunView[]>("/api/runs"),
  get: (id: string) => get<RunView>(`/api/runs/${id}`),
  preview: (body: CreateRunBody) =>
    post<{ plan: Plan; params: Record<string, unknown>; model: ModelCheck; estimate: RunEstimate; training_plan: TrainingPlan | null }>("/api/runs/preview", body),
  create: (body: CreateRunBody) => post<{ id: string; job_id: string }>("/api/runs", body),
  series: (id: string, keys: string[]) => get<Record<string, [number, number][]>>(`/api/runs/${id}/series?${q({ keys: keys.join(",") })}`),
  log: (id: string, offset: number) => get<LogChunk>(`/api/runs/${id}/log?offset=${offset}`),
  pods: (id: string) => get<RunPod[]>(`/api/runs/${id}/pods`),
  checkpoints: (id: string) => get<{ steps: number[]; fsx_dir: string }>(`/api/runs/${id}/checkpoints`),
  stop: (id: string) => post<{ ok: boolean }>(`/api/runs/${id}/stop`),
  resume: (id: string) => post<{ job_id: string }>(`/api/runs/${id}/resume`),
  remove: (id: string) => del<{ ok: boolean }>(`/api/runs/${id}`),
  images: () => get<TrainerImageView[]>("/api/trainer-images"),
  buildImage: (body: { region: string; profile: string }) => post<{ id: string; ready: boolean }>("/api/trainer-images", body),
};

/* ---------- exports / inference / evals ---------- */

export interface ExportView {
  id: string;
  run_id: string;
  step: number;
  status: string;
  fsx_path: string | null;
  s3_uri: string | null;
  error: string | null;
  created_at: string;
  job: Job | null;
}

export interface EndpointView {
  id: string;
  name: string;
  cluster_id: string;
  model_source: { kind: "hf" | "export"; model_id?: string; export_id?: string; base_model_id?: string };
  served_model_name: string;
  instance_group: string;
  replicas: number;
  tp: number;
  url: string | null;
  status: string;
  error: string | null;
  created_at: string;
  job: Job | null;
}

export interface EvalView {
  id: string;
  name: string;
  endpoint_id: string;
  agent_runtime_id: string;
  dataset_id: string;
  split: string;
  limit: number;
  status: string;
  summary: { n?: number; scored?: number; failed?: number; mean_reward?: number | null; mean_reward_scored?: number | null; acr_failed_rate?: number | null };
  results_s3: string | null;
  error: string | null;
  created_at: string;
  job: Job | null;
}

export const servingApi = {
  exports: () => get<ExportView[]>("/api/exports"),
  createExport: (body: { run_id: string; step: number }) => post<{ id: string; job_id: string }>("/api/exports", body),
  endpoints: () => get<EndpointView[]>("/api/inference"),
  createEndpoint: (body: { name: string; cluster_id: string; instance_group: string; source: "hf" | "export"; model_id?: string; export_id?: string; tp: number; replicas: number; max_model_len?: number | null }) =>
    post<{ id: string; job_id: string }>("/api/inference", body),
  deleteEndpoint: (id: string) => del<{ job_id: string }>(`/api/inference/${id}`),
  evals: () => get<EvalView[]>("/api/evals"),
  createEval: (body: { name: string; endpoint_id: string; agent_runtime_id: string; dataset_id: string; split: string; limit: number }) =>
    post<{ id: string; job_id: string }>("/api/evals", body),
};

/* ---------- resources / overview ---------- */

export interface ResourcesView {
  region: string;
  project: RegionResources;
  clusters: { id: string; name: string; status: string; source: string; cfn_stack: string | null; gpu_cost_usd: number | null }[];
  runtimes: { id: string; agent_id: string; runtime_id: string | null; network_mode: string; cluster_id: string | null; status: string }[];
  endpoints: { id: string; name: string; status: string; url: string | null; cluster_id: string }[];
  trainer_images: { id: string; profile: string; status: string; image_uri: string }[];
  storage: { prefix: string; objects?: number; bytes?: number; monthly_usd?: number; purgeable?: boolean; error?: string }[];
  ecr: { repository: string; images: number; bytes: number; monthly_usd: number }[];
}

export interface OverviewView {
  counts: Record<string, number>;
  run_cost_usd: number;
  node_hours: number;
  active_runs: { id: string; name: string; status: string; step: number | null; est_cost_usd: number }[];
  clusters: { id: string; name: string; status: string; region: string }[];
  setup: Record<string, RegionResources>;
}

export const resourceApi = {
  get: (region: string) => get<ResourcesView>(`/api/resources?${q({ region })}`),
  purge: (region: string, prefix: string) => post<{ deleted: number }>("/api/resources/purge", { region, prefix }),
  overview: () => get<OverviewView>("/api/overview"),
};
