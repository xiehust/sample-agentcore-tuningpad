# HyperPod（EKS 编排）集群：创建/管理、容量、存储、韧性、可观测性

> 调研时间：2026-10-04。范围：把 verl GRPO 训练器（内含 rollout gateway，监听 HTTP 18765，被 VPC 模式的 AgentCore Runtime agent 回调）迁移到 SageMaker HyperPod + EKS，涵盖训练、checkpoint merge/export 和推理部署。
> 只做了只读调用，没有创建或修改任何 AWS 资源。账号 ID 已打码为 `4344****5045`。

## 0. 结论速览

- 前置依赖：先有 EKS（K8s 1.30–1.35，认证模式 `API` 或 `API_AND_CONFIG_MAP`，VPC CNI ≥ 1.18.3，子网必须是私有子网），再用 helm 安装 `sagemaker-hyperpod-cli/helm_chart/HyperPodHelmChart`（namespace `aws-hyperpod`，名称不可改），然后调用 `sagemaker:CreateCluster` 并带上 `Orchestrator.Eks.ClusterArn`。
- 平台侧可以全程用 API 编排：`CreateCluster` / `UpdateCluster`（扩缩容，`InstanceGroupsToDelete`）/ `BatchDeleteClusterNodes` / `DeleteCluster` / `DescribeCluster` / `ListClusterNodes`，再加上 `aws eks update-kubeconfig` 和 EKS Access Entry。
- 容量：on-demand 按 "`ml.<type> for cluster usage`" 配额走。Flexible Training Plans 走 `SearchTrainingPlanOfferings` → `CreateTrainingPlan`，再在实例组上设 `TrainingPlanArn`。**HyperPod 已支持 Spot**，实例组设 `CapacityRequirements: {"Spot": {}}`，要求 `NodeProvisioningMode=Continuous`。
- verl 基于 Ray，推荐用 KubeRay 的 `RayJob`/`RayCluster`。HyperPod 的 `NodeRecovery=Automatic` 对 Ray 透明，但重试要靠 Ray/verl 自己处理，并配合 checkpoint。HyperPod 的 "job auto-resume" 注解只对 Kubeflow `PyTorchJob` 生效。
- 本账号现状：三个 Region 都没有 HyperPod/EKS 集群。us-east-1 有少量 on-demand 配额（p5×2、p5en×2、p5e×1、p4d×2），us-west-2 和 us-east-2 全部为 0。单集群实例上限 20，账号总上限 30。

## 1. 账号只读检查结果（2026-10-04）

调用者账号：`4344****5045`。执行的命令：`aws sagemaker list-clusters`、`aws eks list-clusters`、`aws sagemaker list-training-plans`、`aws service-quotas list-service-quotas --service-code sagemaker`、`aws sagemaker search-training-plan-offerings`（只查询，不购买）、`aws eks describe-addon-versions`、`aws pricing get-products`。

| 检查项 | us-west-2 | us-east-1 | us-east-2 |
|---|---|---|---|
| HyperPod 集群 (`list-clusters`) | 无 | 无 | 无 |
| EKS 集群 (`eks list-clusters`) | 无 | 无 | 无 |
| Training plans | 无 | `modelhub`（Expired） | 无 |

"for cluster usage" 配额（on-demand，HyperPod 用）：

| 配额名 (QuotaCode) | us-west-2 | us-east-1 | us-east-2 |
|---|---|---|---|
| ml.p4d.24xlarge for cluster usage (L-47E0BC8F) | 0 | **2** | 0 |
| ml.p4de.24xlarge for cluster usage (L-BEF3120E) | 0 | 0 | 0 |
| ml.p5.48xlarge for cluster usage (L-8762A75F) | 0 | **2** | 0 |
| ml.p5e.48xlarge for cluster usage (L-FD431820) | 0 | **1** | 0 |
| ml.p5en.48xlarge for cluster usage (L-6EF3AE05) | 0 | **2** | 0 |
| ml.p6-b200.48xlarge for cluster usage (L-44097F05) | 0 | 0 | 0 |
| ml.p6-b300.48xlarge for cluster usage (L-9883FE68) | 0 | 0 | 未列出 |
| ml.p6e-gb200.36xlarge for cluster usage (L-5DB70460) | 未查到（API 限流） | 0 | 0 |
| ml.g6e.12xlarge for cluster usage (L-A15DF696) | 0 | **2** | 0 |
| ml.g6e.48xlarge for cluster usage (L-177AF1AD) | 0 | 0 | 0 |

注意：p4d/p4de 是 `.24xlarge`，没有 `.48xlarge`。

us-east-1 的其他关键配额：
- Maximum number instances allowed per SageMaker HyperPod cluster (L-2CE978FC) = **20**
- Total number of instances allowed across SageMaker HyperPod clusters (L-3308CCC7) = **30**
- Maximum size of EBS volume in GB for a SageMaker HyperPod cluster instance (L-E13DF72A) = 1024
- Spot：`ml.p5.48xlarge for cluster spot instance usage` (L-F742E2D7) = 2，p5e/p5en spot 各 2；但 **Total number of Spot instances allowed across SageMaker HyperPod clusters (L-97A2C724) = 0**，所以当前实际用不了 Spot，需要先提额。
- Maximum total FSx Lustre capacity in TiB across HyperPod restricted instance groups (L-FC4D7F52) = 1365

Training plan 配额（us-west-2，名称为 "Number of ml.X instances in reserved capacity across training plans per Region"）：p5.48xlarge=**0** (L-9EF527CA)，p5en=256，p5e=256，p4d=256，p6-b200=8，p6-b300=0，p6e-gb200=18；"Number of training plans per Region" (L-867702E6)=25。

`search-training-plan-offerings`（hyperpod-cluster，1 台，24h，只查询）结果：

| Region | p5.48xlarge | p5en.48xlarge | p4d.24xlarge |
|---|---|---|---|
| us-east-1 | $1098.42（2026-10-25 起，us-east-1f） | $1452.63（11-28 起） | $312.11（11-07 起） |
| us-west-2 | ResourceLimitExceeded（预留配额为 0） | $1452.63（11-04 起） | $312.11（10-06 起） |
| us-east-2 | ResourceLimitExceeded | ResourceLimitExceeded | $312.11（10-11 起） |

On-demand 价格（Pricing API，us-east-1，usagetype `USE1-Cluster:<type>`）：ml.p5.48xlarge **$66.048/h**，ml.p5en.48xlarge $75.9552/h，ml.p4d.24xlarge $25.91/h，ml.g6e.48xlarge $37.66/h。按上表折算，p5 training plan 约 $45.8/h，p4d 约 $13/h，比 on-demand 便宜很多，但需要提前预订、有固定窗口。
完整价格见：https://aws.amazon.com/sagemaker/ai/pricing/ （HyperPod 标签页）。

us-west-2 可用的 HyperPod EKS add-on（`eks describe-addon-versions`）：

| add-on | 最新版本 | 兼容 K8s |
|---|---|---|
| `amazon-sagemaker-hyperpod-training-operator` | v1.3.0-eksbuild.1 | 1.31–1.36 |
| `amazon-sagemaker-hyperpod-taskgovernance` | v1.6.1-eksbuild.1 | 1.31–1.36 |
| `amazon-sagemaker-hyperpod-observability` | v1.2.0-eksbuild.3 | 1.32–1.37 |
| `amazon-sagemaker-hyperpod-inference` | v2.1.0-eksbuild.1 | 1.32–1.37 |
| `aws-fsx-csi-driver` | v1.10.0 | — |
| `aws-mountpoint-s3-csi-driver` | v2.8.0 | — |
| `amazon-cloudwatch-observability` | v6.7.0 | — |
| `eks-pod-identity-agent` | v1.4.0 | — |

由此建议 EKS 版本选 **1.33 或 1.34**：这两个版本落在 HyperPod 支持范围（1.30–1.35）内，也被上面所有 add-on 支持。

> 本机 AWS CLI 是 2.27.63，`create-cluster` 的 skeleton 里缺少 `NodeProvisioningMode`、`AutoScaling`、`TieredStorageConfig`、`ClusterRole`、`CapacityRequirements`、`InstanceRequirements`、`KubernetesConfig` 等新字段。平台代码需要使用较新的 boto3/CLI。

## 2. 前置条件

来源：
- https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-eks-prerequisites.html
- https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-prerequisites.html
- https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-prerequisites-iam.html

**EKS**
- K8s 版本 1.30–1.35。
- `accessConfig.authenticationMode` 必须是 `API` 或 `API_AND_CONFIG_MAP`。
- CNI 只支持 Amazon VPC CNI，且版本 ≥ 1.18.3。
- 可以继续使用 kube-proxy、CoreDNS、Pod Identity Agent、FSx CSI、Mountpoint S3 CSI、ADOT、CloudWatch Observability 等 add-on。
- **不要**安装管理 NVMe 本地盘的 CSI 驱动。HyperPod AMI 启动时会把 NVMe 配成 LVM `vg.01`，挂载到 `/opt/sagemaker`。
- 用 instance-type 做 nodeSelector 时要带 `ml.` 前缀，例如 `node.kubernetes.io/instance-type: ml.p5.48xlarge`。
- 只有 `hostNetwork: true` 的 Pod 默认能访问 IMDS。其他 Pod 要用 Pod Identity 或 IRSA 获取凭证。

**VPC、子网与安全组**
- EKS 编排必须提供 VPC 配置，且 HyperPod 必须与 EKS 在同一 VPC，并能访问 EKS API endpoint。
- 子网必须是私有子网。私有子网需要 S3 Gateway Endpoint（或 NAT），否则节点拉不到生命周期脚本和数据。
- 子网 CIDR 创建后不能改。在 EKS 模式下，**每台 P5 会占用 81 个 IP**（主网卡 50 个 + 其余 31 张网卡各 1 个），建议至少用 /20 或 /19 的私有子网。官方 CFN 模板的子网写法可参考：https://github.com/aws-samples/awsome-distributed-training/blob/main/1.architectures/7.sagemaker-hyperpod-eks/cfn-templates/nested-stacks/private-subnet-stack.yaml
- **EFA 安全组**：入站和出站都要放行"来自/去往本安全组自身"的全部流量（自引用）。出站规则**不要用 0.0.0.0/0**，否则可能导致 EFA 健康检查失败。
- EFA 流量不能跨 AZ 或跨 VPC，所以训练实例组应放在同一 AZ 的子网里。
- 集群级 `VpcConfig` 创建后不可修改。实例组级 `OverrideVpcConfig` 可在创建实例组时指定（1–16 个子网、1–5 个安全组），之后同样不可修改，可用于多 AZ 部署或给某个组额外加安全组。
- 单节点 Pod 数上限：每个 HyperPod 实例只支持 1 个 ENI，ml.p4d/p4de/p5.48xlarge 的上限都是 **49 个 Pod/节点**。
- ENI 配额：VPC "Network interfaces per Region" (L-DF5E4CA3)。

**IAM**
- 实例组 `ExecutionRole`：附加托管策略 `AmazonSageMakerClusterInstanceRolePolicy`，再加一段 EKS 模式需要的内联权限：`ec2:AssignPrivateIpAddresses/AttachNetworkInterface/CreateNetworkInterface/.../ModifyNetworkInterfaceAttribute/UnassignPrivateIpAddresses`、`ecr:BatchGetImage/GetAuthorizationToken/GetDownloadUrlForLayer/BatchCheckLayerAvailability`、可选的 `eks-auth:AssumeRoleForPodIdentity`，以及对 `network-interface/*` 的 `ec2:CreateTags`。
- 注意：只用托管策略时，节点只能访问名称以 `sagemaker-` 开头的 S3 桶。生命周期脚本桶要按这个命名，或者额外授权。
- HyperPod 会自动创建服务关联角色，策略为 `AmazonSageMakerHyperPodServiceRolePolicy`，用于替换节点、重启作业。
- 平台控制面角色（admin）的最小权限：`iam:PassRole`（针对 ExecutionRole），`sagemaker:CreateCluster/DeleteCluster/DescribeCluster/DescribeClusterNode/ListClusterNodes/ListClusters/UpdateCluster/UpdateClusterSoftware/BatchAddClusterNodes/BatchDeleteClusterNodes/*ComputeQuota*/*ClusterSchedulerConfig*`，`eks:DescribeCluster/CreateAccessEntry/DescribeAccessEntry/DeleteAccessEntry/AssociateAccessPolicy`，`iam:CreateServiceLinkedRole`。这些 eks access entry 权限说明 HyperPod 在建集群时会以调用者身份为节点角色写 access entry（这是推断，文档没有明说细节）。
- 使用 Karpenter autoscaling 或 Continuous 模式时还需要集群级 `ClusterRole`，见 https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-eks-autoscaling-iam.html

**生命周期脚本**
- 放在 S3，例如 `s3://sagemaker-<x>/lifecycle/`。
- 官方样例：`awsome-distributed-training/1.architectures/7.sagemaker-hyperpod-eks/LifecycleScripts/base-config/on_create.sh`。该仓库已迁移到 https://github.com/awslabs/awsome-distributed-ai ，旧链接会重定向。
- EKS 模式下脚本通常很轻（甚至可以是 `on_create_noop.sh`），主要做本地盘、containerd 等配置。新 API 还支持 `LifeCycleConfig.OnInitComplete`。

**Helm chart（必装）**
来源：https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-eks-install-packages-using-helm-chart.html 和 https://github.com/aws/sagemaker-hyperpod-cli/tree/main/helm_chart

```bash
git clone https://github.com/aws/sagemaker-hyperpod-cli.git && cd sagemaker-hyperpod-cli/helm_chart
helm dependencies update HyperPodHelmChart
helm install hyperpod-dependencies HyperPodHelmChart --namespace kube-system --dry-run
helm install hyperpod-dependencies HyperPodHelmChart --namespace kube-system
```

`HyperPodHelmChart/Chart.yaml` 的依赖（main 分支，2026-10 读取），每项都可以通过 values 里的 `*.enabled` 开关：

| 子 chart | 版本 / 来源 | 作用 |
|---|---|---|
| `cert-manager` | v1.18.2 | |
| `training-operators` | 本地 chart | Kubeflow Training Operator；auto-resume 要求 1.7.0、1.8.0 或 1.8.1 |
| `mpi-operator` | v0.5 | |
| `nvidia-device-plugin` | 0.16.1 | |
| `aws-efa-k8s-device-plugin` | 0.5.20 | |
| `neuron-device-plugin` | | |
| `health-monitoring-agent` | | 部署在 `aws-hyperpod` namespace；**自动恢复依赖它** |
| `deep-health-check` | | RBAC 与 SA `deep-health-check-service-account` |
| `job-auto-restart` | | |
| `storage` | | |
| `mlflow` | | |
| `cluster-role-and-bindings` / `namespaced-role-and-bindings` / `team-role-and-bindings` | | 权限配置 |
| `hyperpod-inference-operator` | 2.7.0 | |
| `hyperpod-patching` | | |
| `gpu-operator` | | |
| `hyperpod-ray-endpoint-operator` | | |

注意：
- 用 Console 或 CFN 创建时这一步会自动完成；直接调 API 时必须手动装，否则 CreateCluster 可能失败。
- 训练 operator、任务治理、可观测性、推理 operator 现在都有对应的 EKS 托管 add-on（见第 1 节）。平台可以优先用 `aws eks create-addon`，避免与 helm 重复安装。

## 3. CreateCluster / 管理 API

来源：
- https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-eks-operate-cli-command-create-cluster.html
- https://docs.aws.amazon.com/sagemaker/latest/APIReference/API_CreateCluster.html
- https://docs.aws.amazon.com/sagemaker/latest/APIReference/API_ClusterInstanceGroupSpecification.html

下面是适合 verl 平台的最小请求：1 个 CPU 系统组 + 1 个 GPU 训练组。

```json
{
  "ClusterName": "tuningpad-hp",
  "Orchestrator": { "Eks": { "ClusterArn": "arn:aws:eks:us-east-1:<acct>:cluster/tuningpad-eks" } },
  "VpcConfig": { "SecurityGroupIds": ["sg-efa-selfref"], "Subnets": ["subnet-priv-az1"] },
  "NodeRecovery": "Automatic",
  "NodeProvisioningMode": "Continuous",
  "ClusterRole": "arn:aws:iam::<acct>:role/HyperPodClusterRole",
  "InstanceGroups": [
    { "InstanceGroupName": "system", "InstanceType": "ml.m5.2xlarge", "InstanceCount": 1,
      "LifeCycleConfig": { "SourceS3Uri": "s3://sagemaker-tuningpad-lcs/base/", "OnCreate": "on_create.sh" },
      "ExecutionRole": "arn:aws:iam::<acct>:role/HyperPodExecRole" },
    { "InstanceGroupName": "gpu-p5", "InstanceType": "ml.p5.48xlarge", "InstanceCount": 2,
      "LifeCycleConfig": { "SourceS3Uri": "s3://sagemaker-tuningpad-lcs/base/", "OnCreate": "on_create.sh" },
      "ExecutionRole": "arn:aws:iam::<acct>:role/HyperPodExecRole",
      "ThreadsPerCore": 1,
      "InstanceStorageConfigs": [ { "EbsVolumeConfig": { "VolumeSizeInGB": 500 } } ],
      "OnStartDeepHealthChecks": ["InstanceStress", "InstanceConnectivity"],
      "TrainingPlanArn": "arn:aws:sagemaker:us-east-1:<acct>:training-plan/xxx",
      "KubernetesConfig": { "Labels": { "tuningpad/role": "trainer" } },
      "OverrideVpcConfig": { "SecurityGroupIds": ["sg-efa-selfref", "sg-gateway-18765"], "Subnets": ["subnet-priv-az1"] } }
  ],
  "Tags": [{ "Key": "app", "Value": "tuningpad" }]
}
```

```bash
aws sagemaker create-cluster --cli-input-json file://create_cluster.json   # 必须加 file:// 前缀
```

字段要点：

| 字段 | 说明 |
|---|---|
| `InstanceGroups` | 最多 20 个组。`InstanceCount` 取值 0–6758，但实际受配额限制（本账号单集群 20 台）。 |
| `InstanceType` | 可选值包括 `ml.p4d.24xlarge`、`ml.p4de.24xlarge`、`ml.p5.48xlarge`、`ml.p5.4xlarge`、`ml.p5e.48xlarge`、`ml.p5en.48xlarge`、`ml.p6-b200.48xlarge`、`ml.p6e-gb200.36xlarge`、`ml.g6e.*`、`ml.trn2.48xlarge` 等。`ml.p6-b300.48xlarge` 已出现在配额列表和 training plan 支持列表中。 |
| `InstanceRequirements.InstanceTypes` | 与 `InstanceType` 二选一。最多 20 个类型，按列表顺序回退；要求 Continuous 模式。 |
| `ThreadsPerCore` | 1 表示关闭超线程，2 表示开启。 |
| `InstanceStorageConfigs` | 最多 4 个。额外 EBS 会挂到 `/opt/sagemaker`，单卷上限受配额 L-E13DF72A 限制（1024 GB）。 |
| `OnStartDeepHealthChecks` | 可选 `InstanceStress`、`InstanceConnectivity`，只对 GPU/Trn 实例生效。 |
| `TrainingPlanArn` | 训练计划须处于 `Scheduled` 或 `Active` 状态；子网 AZ 必须与计划的 AZ 一致。**不要把 training plan 用在 system 组上。** |
| `CapacityRequirements` | `{"Spot":{}}` 或 `{"OnDemand":{}}`，创建后**不可修改**。 |
| `NodeRecovery` | `Automatic` 或 `None`。 |
| `NodeProvisioningMode` | `Continuous`：先拿到的节点先跑，剩余容量在后台持续重试。Spot、flexible IG、Karpenter 都需要它。 |
| `AutoScaling` | `{"Mode":"Enable","AutoScalerType":"Karpenter"}`，需配合 `HyperpodNodeClass` 和 NodePool CRD。 |
| `TieredStorageConfig` | `{"Mode":"Enable","InstanceMemoryAllocationPercentage":20..100}`，即托管分层 checkpoint。 |
| `ImageId` / `ImageReleaseVersion` | 用于自定义 AMI 或固定 AMI 版本；`AutoPatchConfig` 用于自动安全补丁。 |
| `KubernetesConfig` | 给节点打 `Labels`/`Taints`，可用于 MIG（`nvidia.com/mig.config`）。注意：开启 DeepHealthChecks 的组不能加自定义 taint。 |

健康检查期间节点会带 taint `sagemaker.amazonaws.com/node-health-status=Unschedulable:NoSchedule`。

**生命周期管理**

```bash
aws sagemaker describe-cluster --cluster-name tuningpad-hp            # ClusterStatus: Creating|InService|Updating|Failed|Deleting
aws sagemaker list-cluster-nodes --cluster-name tuningpad-hp --instance-group-name-contains gpu
aws sagemaker describe-cluster-node --cluster-name tuningpad-hp --node-id i-xxxx

# 扩缩容：改 InstanceCount，或在列表中加入新组即为新增实例组
aws sagemaker update-cluster --cluster-name tuningpad-hp --instance-groups file://igs.json
aws sagemaker update-cluster --cluster-name tuningpad-hp --instance-groups-to-delete gpu-p5
aws sagemaker batch-delete-cluster-nodes --cluster-name tuningpad-hp --node-ids i-a i-b   # 定点缩容
aws sagemaker batch-add-cluster-nodes ...       # Continuous 模式下按组定点扩容（新 API）
aws sagemaker update-cluster-software --cluster-name tuningpad-hp   # 滚动升级 AMI
aws sagemaker start-cluster-health-check ...     # 按需触发深度健康检查（新 API）
aws sagemaker delete-cluster --cluster-name tuningpad-hp            # 不会删除 EKS / VPC / FSx
```

建集群大约需要 20 分钟（autoscaling 文档给出的数字）；P5 加上深度健康检查会更久。删除 HyperPod 集群后，EKS 控制面仍然在计费，需要单独删除。

**平台主机访问 kubectl**

```bash
aws eks update-kubeconfig --region us-east-1 --name tuningpad-eks
aws eks create-access-entry --cluster-name tuningpad-eks --principal-arn arn:aws:iam::<acct>:role/TuningpadControlPlane --type STANDARD
aws eks associate-access-policy --cluster-name tuningpad-eks --principal-arn arn:aws:iam::<acct>:role/TuningpadControlPlane \
  --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy --access-scope type=cluster
# 科学家/租户：--kubernetes-groups '["hyperpod-scientist-user-namespace-level","hyperpod-scientist-user-cluster-level"]'
```

平台主机还必须能连到 EKS API endpoint：要么 endpoint 开启 public access 并限制来源 CIDR，要么平台主机就在 VPC 内或已打通网络。

**可复用的 IaC**

| 方案 | 地址 | 内容 |
|---|---|---|
| 官方 CFN（Console "Quick/Custom setup" 背后用的就是它） | https://github.com/aws/sagemaker-hyperpod-cluster-setup/tree/main/eks/cloudformation | 一次性创建 VPC/子网/安全组、S3 生命周期脚本桶、FSx Lustre、EKS、Helm、IAM、HyperPod，也可以填入已有资源 ID。用法：`aws cloudformation create-stack --template-url <main-stack> --parameters file://params.json --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM`。文档：https://docs.aws.amazon.com/sagemaker/latest/dg/smcluster-getting-started-eks-console-create-cluster-cfn.html |
| Terraform | `awslabs/awsome-distributed-ai/1.architectures/7.sagemaker-hyperpod-eks/terraform-modules/hyperpod-eks-tf` | 有 `create_hyperpod_inference_operator_module` 等开关，可选装任务治理、训练 operator、可观测性 add-on。参见 https://aws.amazon.com/blogs/architecture/unlock-efficient-model-deployment-simplified-inference-operator-setup-on-amazon-sagemaker-hyperpod 和 https://builder.aws.com/content/3Fp24zBGZGeV535w9EEqX4jweOP/deploy-amazon-sagemaker-hyperpod-eks-in-a-fully-private-vpc-for-regulated-industries |
| 原生 IaC 资源 | — | CloudFormation 资源 `AWS::SageMaker::Cluster`（含 `TieredStorageConfig`）；Terraform provider 也支持 |

**建议**：平台把"基础设施栈"（VPC + EKS + helm/add-on + IAM + FSx）交给 CFN/Terraform 一次性建好；把"计算栈"（HyperPod 实例组的增删和扩缩）交给平台后端，直接调 SageMaker API 动态管理。

## 4. 容量

**Flexible Training Plans**
来源：
- https://docs.aws.amazon.com/sagemaker/latest/dg/reserve-capacity-with-training-plans.html
- https://docs.aws.amazon.com/sagemaker/latest/dg/search-training-plan-offerings-api-cli-sdk.html
- https://docs.aws.amazon.com/sagemaker/latest/dg/training-plan-utilization-for-hyperpod.html

```bash
aws sagemaker search-training-plan-offerings --target-resources hyperpod-cluster \
  --instance-type ml.p5.48xlarge --instance-count 2 --duration-hours 72 \
  --start-time-after <epoch> --end-time-before <epoch>
aws sagemaker create-training-plan --training-plan-name tp-x --training-plan-offering-id tpo-xxxx
aws sagemaker describe-training-plan --training-plan-name tp-x   # Status: Pending|Scheduled|Active|Expired...
```

- 支持的实例：ml.p4d.24xlarge、p5.4xlarge、p5.48xlarge、p5e.48xlarge、p5en.48xlarge、p6-b200.48xlarge、p6-b300.48xlarge、trn1、trn2；g6.xlarge/4xlarge 需要联系客户经理。UltraServer（`ml.u-p6e-gb200x72`）另有说明。
- 时长 1–182 天，按 1 天递增。数量只能选 1、2、4、8、16、32、64。最早 30 分钟后可开始，最多提前 56 天预订。
- 计划总是在 UTC 11:30 结束。对 HyperPod 目标，会在块结束前 **1 小时**开始回收实例。
- 一个计划可以包含多个不连续的块。计划绑定目标资源类型（`hyperpod-cluster` 或 `training-job`），两者不能互换。
- 计划受 "reserved capacity ... across training plans per Region" 配额约束。本账号 us-west-2 的 p5.48xlarge 该配额为 0，所以搜索直接报 ResourceLimitExceeded。

**On-demand**：配额名格式为 `ml.<type> for cluster usage`，在 Service Quotas 里搜 "cluster usage"。另外还要看单集群上限 L-2CE978FC 和账号总量 L-3308CCC7。

**Spot（已支持，2025–2026 GA）**
来源：https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-spot.html 和 https://aws.amazon.com/sagemaker/ai/hyperpod/features

- 实例组设 `CapacityRequirements: {"Spot": {}}`，集群设 `NodeProvisioningMode=Continuous`。可以与 on-demand 组混用；配合 Karpenter 可以在 Spot 不可用或被回收时回退到 on-demand。
- 官方宣称最多可省 90%。适用于可中断负载，例如 rollout/批量推理、HPO、实验类 GRPO。
- 配额名为 `ml.<type> for cluster spot instance usage`，还有账号级总量 L-97A2C724（本账号为 0）。
- 可以用 `aws ec2 get-spot-placement-scores` 来挑选 AZ。
- 限制：elastic training 不支持 Spot。

**价格**
- HyperPod 按 ml 实例小时计费（usagetype `Cluster:<type>`），上面 us-east-1 的价格来自 Pricing API。
- 另有 EKS 控制面费用、FSx、EBS、AMP/AMG 等费用。
- 价格页：https://aws.amazon.com/sagemaker/ai/pricing/ 。估算请用 AWS Pricing Calculator。

## 5. 存储与 checkpoint

来源：
- https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-eks-setup-storage.html
- https://docs.aws.amazon.com/eks/latest/userguide/fsx-csi-create.html
- https://docs.aws.amazon.com/sagemaker/latest/dg/managed-tier-checkpointing-setup.html

**FSx for Lustre**
- 用 EKS add-on `aws-fsx-csi-driver`（v1.10.0），控制器的 SA 通过 Pod Identity/IRSA 授予 FSx 权限。
- 可以动态创建（StorageClass，参数 `subnetId`、`securityGroupIds`、`deploymentType: PERSISTENT_2`、`perUnitStorageThroughput`），也可以静态挂载已有文件系统（PV `csi.volumeHandle: fs-xxx`、`volumeAttributes.dnsname/mountname`）。
- FSx 应与 GPU 实例组放在同一 AZ。可以开启 EFA 来提升吞吐，见 https://docs.aws.amazon.com/eks/latest/userguide/fsx-csi-tuning-efa.html
- 官方 CFN 模板可以直接建 FSx。

```yaml
apiVersion: v1
kind: PersistentVolume
metadata: { name: fsx-pv }
spec:
  capacity: { storage: 1200Gi }
  accessModes: [ReadWriteMany]
  csi:
    driver: fsx.csi.aws.com
    volumeHandle: fs-0123456789abcdef0
    volumeAttributes: { dnsname: fs-0123...fsx.us-east-1.amazonaws.com, mountname: abcdefgh }
```

**Mountpoint for S3**
- 用 EKS add-on `aws-mountpoint-s3-csi-driver`（v2.8.0），PV 的 driver 为 `s3.csi.aws.com`，`volumeAttributes.bucketName`。
- 适合只读加载数据集和基座模型，也适合顺序写入大文件（不支持随机改写）。

**本地 NVMe**：只能通过 `/opt/sagemaker`（AMI 配置的 LVM）以 hostPath 方式使用，可作为 checkpoint 和模型权重的本地缓存。

**托管分层 checkpoint（Managed Tiered Checkpointing）**
- 在 CreateCluster/UpdateCluster 中设 `TieredStorageConfig.Mode=Enable`，并指定 `InstanceMemoryAllocationPercentage`（20–100）。
- 训练镜像安装 `amzn-sagemaker-checkpointing s3torchconnector`，代码中用 PyTorch DCP 的 `async_save`，配合 `SageMakerTieredStorageWriter/Reader`（参数 `namespace`、`world_size`、`s3_tier_base_path`、`save_to_s3`）。
- 效果是 checkpoint 先写到 CPU 内存并跨节点复制，再定期落到 S3。
- 支持 Ray Train：https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-ray-tiered-storage.html

**对 verl 的建议（推断）**
- verl 的 FSDP/Megatron checkpoint 已经是分片 DCP 风格。训练中把 checkpoint 写到 FSx（`trainer.default_local_dir=/fsx/ckpt/<run>`），并用 FSx 的 Data Repository Association 把 `/fsx/ckpt` 自动导出到 S3 作为持久副本。
- 后续 merge/export（`verl model_merger` → HF safetensors）作为单独的 CPU 或小 GPU Job 读取 FSx，结果写到 S3，供推理部署使用。
- 托管分层 checkpoint 需要改 verl 的 save/load 逻辑，可作为 P2 优化。

## 6. 韧性

来源：
- https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-eks-resiliency-deep-health-checks.html
- https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-eks-run-jobs-kubectl.html
- https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-ray-node-recovery.html

**深度健康检查**
- 实例级：GPU/NVLink 数量、DCGM diag level 4、EFA loopback 带宽和延迟、stress-ng。
- 集群级：多节点 NCCL test。
- 触发时机：创建或扩容时（`OnStartDeepHealthChecks`），或按需调用 `StartClusterHealthCheck`。
- 日志：CloudWatch `/aws/sagemaker/Clusters/<name>/<id>` 下的 `DeepHealthCheckResults/*`；节点上 `/var/log/aws/clusters/sagemaker-deep-health-check.log`。
- 节点标签：`sagemaker.amazonaws.com/node-health-status`（Schedulable/Unschedulable…）、`sagemaker.amazonaws.com/deep-health-check-status`（Passed…）。

**自动节点恢复**
- 设 `NodeRecovery=Automatic`，并安装 health-monitoring-agent，HyperPod 会自动重启或替换故障节点。
- CPU 实例不支持自动替换。

**作业 auto-resume（Kubeflow PyTorchJob）**
- 要求 Training Operator 版本为 1.7.0、1.8.0 或 1.8.1。
- 作业必须在 `kubeflow` namespace 或以 `hyperpod` 为前缀的 namespace 中。

```yaml
apiVersion: kubeflow.org/v1
kind: PyTorchJob
metadata:
  name: grpo-xxx
  namespace: kubeflow
  annotations:
    sagemaker.amazonaws.com/enable-job-auto-resume: "true"
    sagemaker.amazonaws.com/job-max-retry-count: "2"
spec:
  pytorchReplicaSpecs:
    Worker:
      replicas: 2
      restartPolicy: OnFailure
      template:
        spec:
          nodeSelector: { sagemaker.amazonaws.com/node-health-status: Schedulable }
```

- 新的 **HyperPod training operator**（add-on `amazon-sagemaker-hyperpod-training-operator`，CRD `HyperPodPytorchJob`）提供进程级恢复和 elastic 训练，但面向 torchrun 类作业：https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-eks-operator-usage.html

**Ray（verl 适用）**
- HyperPod 运行的是上游 KubeRay，原样支持 `RayCluster`、`RayJob`、`RayCronJob`、`RayService` 这些 CRD。
- 节点恢复对 Ray 透明：节点离开再加入，Ray 视为普通的 worker 丢失。但**没有类似 PyTorchJob 的 auto-resume 注解**，重试要自己做：
  - Ray Train 用 `FailureConfig(max_failures=N)`；
  - verl 用 `RayJob` 的 `backoffLimit`/重新提交，加上 `trainer.resume_mode=auto` 从最新 checkpoint 续训（这是推断，需要验证）。
- KubeRay 安装：https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-ray-install-kuberay.html

**任务治理（Kueue）**
来源：https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-eks-operate-console-ui-governance-policies.html 和 https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-eks-operate-console-ui-governance-cli.html

- 安装 EKS add-on `amazon-sagemaker-hyperpod-taskgovernance`（v1.6.1，底层是 Kueue）。
- 集群策略 API：`CreateClusterSchedulerConfig`，参数形如 `SchedulerConfig={PriorityClasses=[{Name,Weight}],FairShare=Enabled|Disabled}`。
- 团队配额 API：`CreateComputeQuota`，参数为 `ComputeQuotaConfig{ComputeQuotaResources[{InstanceType,Count}],ResourceSharingConfig{Strategy:Lend|DontLend|LendAndBorrow,BorrowLimit},PreemptTeamTasks:Never|LowerPriority}` 和 `ComputeQuotaTarget{TeamName,FairShareWeight 0-100}`。新版本还支持按 GPU、vCPU、内存的细粒度配额。
- 创建团队后会自动生成 namespace `hyperpod-ns-<team>` 和 LocalQueue `hyperpod-ns-<team>-localqueue`。
- 提交作业时必须带标签：

```yaml
metadata:
  namespace: hyperpod-ns-<team>
  labels:
    kueue.x-k8s.io/queue-name: hyperpod-ns-<team>-localqueue
    kueue.x-k8s.io/priority-class: <name>-priority
```

- 对 Ray：Kueue 按 `RayCluster` 整体占用配额。常驻 RayCluster 会一直占满配额，所以按次跑的任务应使用 `RayJob`。

## 7. 可观测性

来源：
- https://docs.aws.amazon.com/sagemaker/latest/dg/hyperpod-observability-addon-setup.html
- https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-eks-cluster-observability-cluster.html
- https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-eks-cluster-observability-cluster-cloudwatch-ci.html

**HyperPod observability add-on**
- EKS add-on 名为 `amazon-sagemaker-hyperpod-observability`（v1.2.0）。
- 采集 DCGM、node-exporter、EFA、FSx、Kueue、训练任务等指标，remote-write 到 Amazon Managed Prometheus（AMP），并可选择在 Amazon Managed Grafana 中自动创建 dashboard。
- 前置条件：
  - 已安装 `eks-pod-identity-agent`；
  - 集群至少有 1 个节点，且规格不小于 4xlarge；
  - 使用 AMG 需要启用 IAM Identity Center 并有用户；
  - 管理角色需要 `AmazonSageMakerHyperPodObservabilityAdminAccess`、`AWSGrafanaWorkspacePermissionManagementV2`，以及创建 `AmazonSageMakerHyperPodObservability*` 角色的 IAM 权限。
- 也可以用自建的 Prometheus/Grafana 抓取 Ray 指标。

**CloudWatch Container Insights**：EKS add-on `amazon-cloudwatch-observability`（v6.7.0），提供 HyperPod 视图下的 CPU/GPU/内存/网络指标和日志。Continuous 模式的集群还会发出结构化事件。

**HyperPod 自身日志**：CloudWatch `/aws/sagemaker/Clusters/<cluster>/<id>`，包含生命周期脚本日志和深度健康检查结果。

## 8. 对 tuningpad 架构的落地要点（推断，待验证）

1. **ACR 回调 gateway（18765）**
   - ACR 以 `networkMode=VPC` 接入同一 VPC 的私有子网（ENI 需要和 HyperPod/EKS 网络互通）。
   - gateway 运行在 verl 的 driver/head Pod 内。借助 VPC CNI，Pod IP 本身就是 VPC IP。
   - 推荐用 `Service type=LoadBalancer` + AWS Load Balancer Controller 创建 internal NLB（或用 ClusterIP + 固定 Pod IP），作为稳定入口。
   - 安全组：新建 `sg-gateway`，放行来自 ACR 安全组的 TCP 18765，通过 `OverrideVpcConfig` 附加到 GPU 实例组（每组最多 5 个安全组）。这个安全组不要和 EFA 自引用安全组混用，后者的出站规则不能是 0.0.0.0/0。
   - 如果走 NLB，需要给 NLB 安全组放行，并把目标设为 Pod IP（target-type ip）。
2. **实例组规划**
   - `system`：ml.m5.2xlarge×1，运行控制器和 add-on。
   - `gpu-train`：p5/p5en，使用 TrainingPlan 或 on-demand，开启 DeepHealthChecks。
   - 可选 `gpu-infer`：g6e/p5，可用 Spot，部署 vLLM 推理（`amazon-sagemaker-hyperpod-inference` add-on，CRD `InferenceEndpointConfig`/`JumpStartModel`），也可以用自己的 Deployment。
   - `cpu-merge`：c/m 系列，做 checkpoint merge/export，可以 `InstanceCount=0` 按需扩容。
3. **作业形态**
   - verl GRPO 用 `RayJob` 提交（headGroup 包含 gateway，workerGroup 每个 Pod 用 8 张 GPU，并申请 `vpc.amazonaws.com/efa: 32`；p5 有 32 个 EFA 设备）。
   - 打上 Kueue 标签。
   - 失败重试由平台做：读取 RayJob 状态，从 FSx 上最新的 checkpoint 重新提交。
4. **配额先行**
   - 本账号只有 us-east-1 可以马上开 2×p5 或 2×p5en（on-demand）。
   - us-west-2 和 us-east-2 都要提额；p5 training plan 在 us-west-2 也要提 reserved-capacity 配额。
   - Spot 的账号总量为 0，也需要申请。
