# HyperPod EKS 上的工作负载：verl/Ray 训练、Gateway 网络、作业提交、Checkpoint 导出、推理部署

调研时间：2026-10-04。只做了只读操作：`aws sagemaker list-clusters` 和 `aws eks list-clusters`，在 us-west-2、us-east-1、us-east-2 三个区域的返回均为空，说明当前账号（账号 ID 已隐去）还没有 HyperPod 集群，也没有 EKS 集群。

## 0. 结论速览

| 主题 | 推荐方案 | 依据 |
|---|---|---|
| 训练编排 | 用 KubeRay `RayJob`（`ray.io/v1`），每个训练 run 对应一个 RayJob。KubeRay 1.6+ 即可，当前 chart 最新是 1.7.1 | AWS 官方 HyperPod Ray 文档；HyperPod recipes 的 verl-on-EKS 模板本身就是 RayJob |
| 参考样例 | `awslabs/awsome-distributed-training/examples/training/verl/hyperpod-eks/rlvr`，样例为 GRPO/DAPO，已在 4×p5en 上测试，版本为 KubeRay 1.4.2 / verl v0.6.1 / EKS 1.32–1.33 | 仓库源码 |
| ACR 回调 Gateway（18765） | 首选：Pod 直连 IP（VPC CNI 分配的 VPC IP），通过 Downward API 或 Ray 获取后写进调用 payload。备选：用 AWS LBC 建 internal NLB（`target-type: ip`）提供稳定 DNS。不能用 ClusterIP | 见 §1.4 |
| 平台后端提交作业 | 用 Kubernetes Python client 创建 RayJob CR，用 watch 跟踪 `.status`，日志读 submitter Pod 的 log；取消时 delete CR，或在不用 Kueue 时 patch `suspend`。`hyp` CLI 不支持 RayJob，不适合跑 verl | 见 §2 |
| Merge/导出 | 用 k8s `Job`（或 RayJob 的收尾步骤）执行 `python -m verl.model_merger merge --backend fsdp\|megatron --local_dir .../global_step_N/actor --target_dir ...`，再用 `aws s3 sync` 上传 | verl 文档已核实 |
| 推理（评测用） | 推荐 vLLM Deployment 加 internal NLB Service，直接提供 OpenAI 兼容 URL，ACR 用 VPC 模式访问。如果需要 IAM 鉴权、托管 TLS、KEDA 或智能路由，改用 HyperPod Inference Operator 的 `InferenceEndpointConfig`（S3/FSx 模型源，会注册为 SageMaker endpoint） | 见 §4 |

---

## 1. 在 HyperPod EKS 上运行 Ray / verl

### 1.1 安装 KubeRay operator

HyperPod 直接使用开源 KubeRay，没有做任何修改，支持 `RayCluster`、`RayJob`、`RayCronJob`（需要 KubeRay 1.6.0+）和 `RayService`。
来源：https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-ray-install-kuberay.html 、 https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-ray-manage-kubectl.html

```bash
aws eks update-kubeconfig --name <eks-cluster> --region <region>
helm repo add kuberay https://ray-project.github.io/kuberay-helm/ && helm repo update
helm install kuberay-operator kuberay/kuberay-operator --version 1.7.1 -n kuberay --create-namespace   # 2026-09-18 最新 chart=1.7.1
kubectl get pods -n kuberay -l app.kubernetes.io/name=kuberay-operator
```

- AWS 样例固定用 `--version 1.4.2`（`setup/install-kuberay.sh`）。
- 访问权限：给 EKS access entry 关联 `AmazonSagemakerHyperpodTrainingPolicy`（覆盖 RayCluster/RayJob/RayCronJob）或 `AmazonSagemakerHyperpodInferencePolicy`（覆盖 RayCluster/RayService），可以按 namespace 限定范围（来源同上 manage-kubectl 页）。
- head 的 `rayStartParams` 必须设置 `dashboard-host: "0.0.0.0"`，否则 HyperPod Ray Endpoint Operator 和带认证的 dashboard 无法工作。
- HyperPod 在 Ray 之上的增值能力：Studio UI、`sagemaker_ray://` 远程提交（需要 Ray Endpoint Operator 和 `pip install toolkit-for-ray-on-sagemaker-ai`）、Observability add-on、Task governance（基于 Kueue）、节点自动恢复、Managed Tiered Checkpointing（MTC）。来源：https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-ray-getting-started.html

### 1.2 AWS 样例（verl / RL on HyperPod EKS）

| 样例 | 路径 / URL | 要点 |
|---|---|---|
| verl RLVR（GRPO/DAPO） | https://github.com/awslabs/awsome-distributed-training/tree/main/examples/training/verl/hyperpod-eks/rlvr （旧名 aws-samples/awsome-distributed-training 已迁到 awslabs） | `setup/raycluster.yaml`（EFA + FSx）、`recipe/run_grpo_configurable.sh`（`ray job submit --no-wait --working-dir ... -- python3 -m verl.trainer.main_ppo ...`）、`job-stop.sh`（`ray job stop`）、`managed-tiered-checkpointing/`（MTC + verl fork）、`setup/setup-irsa.sh`（S3 IRSA） |
| verl on 通用 EKS | `examples/training/verl/kubernetes/rlvr`（含 Qwen3-235B Megatron、Qwen3-VL 脚本） | 带 `download-model-job.sh` |
| 其他 RL 框架 | `examples/training/{openrlhf/hyperpod-eks, nemo-rl/grpo (RayJob), slime, miles, trl/*-grpo}` | nemo-rl 提供 `kubernetes/rayjob-grpo.yaml`，可直接参考 RayJob 写法 |
| HyperPod recipes（官方） | https://github.com/aws/sagemaker-hyperpod-recipes | 提供 VERL GRPO（RLVR/RLAIF）和 DPO recipe，支持 EKS 和 SMTJ（不支持 Slurm）。EKS 端的 launcher 模板 `launcher/nemo/k8s_templates/ray/training.yaml` 是 RayJob：`submissionMode: K8sJobMode`、`shutdownAfterJobFinishes: true`、`ttlSecondsAfterFinished: 600`。镜像示例：`327873000638.dkr.ecr.us-east-1.amazonaws.com/hyperpod-recipes:verl-v1.0.0-eks` |
| SkyRL 多模态 GRPO（博客） | https://aws.amazon.com/blogs/machine-learning/accelerate-multimodal-rl-training-with-skyrl-on-amazon-sagemaker-hyperpod | CPU head 加 GPU worker；FSDP 和 vLLM 共置；FSx 共享；用 `sagemaker_ray://` 提交；最后用 RayService（`ray.serve.llm:build_openai_app`）托管 LoRA |
| Ray 官方 verl on KubeRay | https://docs.ray.io/en/latest/cluster/kubernetes/examples/verl-post-training.html | 单节点示例 |

AWS 样例 README 里记录的坑（直接影响平台设计）：
- head Pod 不申请 GPU/EFA 时，`trainer.nnodes` 只能填 worker 数。如果把 head 也算进 NCCL 通信组，会出现 `fi_av_insert failed` 或 NCCL hang（EFA 和 Socket 混用导致）。
- 每个 checkpoint 很大，20B 模型、12 GPU 时约 117GB。建议 `trainer.save_freq=20 trainer.max_actor_ckpt_to_keep=3 trainer.resume_mode=auto`，用 `lfs df -h /fsx` 查看 Lustre 剩余容量。
- g5（24GB 显存）上要用 `actor.strategy=fsdp2` 加 `offload_policy=True`，同时设置 `rollout.enforce_eager=True` 和 `gpu_memory_utilization≥0.6`。

### 1.3 RayCluster / RayJob 的 GPU、EFA、NCCL 和存储配置（最小示例）

下面的配置综合自 AWS 样例的 `raycluster.yaml` 和 KubeRay RayJob 文档（https://docs.ray.io/en/latest/cluster/kubernetes/getting-started/rayjob-quick-start.html ）。

```yaml
apiVersion: ray.io/v1
kind: RayJob
metadata:
  name: rl-run-{{runId}}
  namespace: rl-team-a
  labels:
    kueue.x-k8s.io/queue-name: hyperpod-ns-rl-team-a-localqueue   # 启用 HyperPod task governance 时才需要（命名规则请到集群上核实）
spec:
  submissionMode: K8sJobMode          # submitter Job 的日志就是 driver 日志，便于平台采集
  shutdownAfterJobFinishes: true
  ttlSecondsAfterFinished: 600
  activeDeadlineSeconds: 172800       # 兜底超时
  backoffLimit: 0
  entrypoint: >-
    python3 -m verl.trainer.main_ppo
    algorithm.adv_estimator=grpo
    trainer.nnodes=2 trainer.n_gpus_per_node=8
    trainer.default_local_dir=/fsx/runs/{{runId}}/ckpt
    trainer.save_freq=20 trainer.max_actor_ckpt_to_keep=3 trainer.resume_mode=auto
  runtimeEnvYAML: |
    env_vars: {ROLLOUT_GATEWAY_PORT: "18765"}
  rayClusterSpec:
    rayVersion: "2.55.1"
    headGroupSpec:
      rayStartParams:
        dashboard-host: "0.0.0.0"
        resources: '"{\"gateway\": 1}"'     # 自定义资源，用来把 gateway actor 钉在 head 上（见 1.4）
      template:
        spec:
          nodeSelector:
            sagemaker.amazonaws.com/node-health-status: Schedulable
          containers:
          - name: ray-head
            image: <acct>.dkr.ecr.<region>.amazonaws.com/verl:<tag>
            ports:
            - {containerPort: 6379, name: gcs-server}
            - {containerPort: 8265, name: dashboard}
            - {containerPort: 10001, name: client}
            - {containerPort: 18765, name: rollout-gw}
            env:
            - name: POD_IP
              valueFrom: {fieldRef: {fieldPath: status.podIP}}
            resources:
              requests: {cpu: "16", memory: 64Gi}
              limits:   {cpu: "16", memory: 64Gi}
            volumeMounts:
            - {name: fsx, mountPath: /fsx}
          volumes:
          - name: fsx
            persistentVolumeClaim: {claimName: fsx-claim}
    workerGroupSpecs:
    - groupName: gpu
      replicas: 2
      minReplicas: 2
      maxReplicas: 2
      rayStartParams: {num-gpus: "8"}
      template:
        spec:
          nodeSelector:
            node.kubernetes.io/instance-type: ml.p5.48xlarge
            sagemaker.amazonaws.com/node-health-status: Schedulable
          containers:
          - name: ray-worker
            image: <acct>.dkr.ecr.<region>.amazonaws.com/verl:<tag>
            env:
            - {name: FI_PROVIDER, value: efa}
            - {name: FI_EFA_USE_DEVICE_RDMA, value: "1"}     # p5/p5en 设为 1；g5 必须设为 0
            - {name: FI_EFA_FORK_SAFE, value: "1"}
            - {name: NCCL_SOCKET_IFNAME, value: "^docker,lo,veth"}
            - {name: NCCL_DEBUG, value: WARN}
            - {name: TORCH_NCCL_ASYNC_ERROR_HANDLING, value: "1"}
            # 没有 GPUDirect RDMA 的机型（g5）还要加 NCCL_PROTO=simple
            resources:
              limits:   {nvidia.com/gpu: 8, vpc.amazonaws.com/efa: 32, cpu: "180", memory: 1800Gi}
              requests: {nvidia.com/gpu: 8, vpc.amazonaws.com/efa: 32, cpu: "180", memory: 1800Gi}
            volumeMounts:
            - {name: fsx, mountPath: /fsx}
            - {name: dshm, mountPath: /dev/shm}
          volumes:
          - name: fsx
            persistentVolumeClaim: {claimName: fsx-claim}
          - name: dshm
            emptyDir: {medium: Memory, sizeLimit: 512Gi}   # vLLM/NCCL 需要较大的 shm；AWS 样例没有这一项，属于建议补充
```

- EFA 设备数：p5.48xlarge 为 32，p5en.48xlarge 为 16（样例 env 中写明），g5.12xlarge 为 1，p4d.24xlarge 为 4。p6-b200 等新机型请以 EC2 网络规格为准（https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html#ac_network ）。HyperPod 的 AMI 已预装 EFA device plugin 和 NVIDIA device plugin。
- 共享存储：
  - FSx for Lustre：用 CSI 静态 PV 加 `fsx-claim` PVC，与样例一致。数据集、checkpoint 和 merge 产物都放在这里，必须与 HyperPod 节点在同一 AZ。
  - S3：可以用 Mountpoint for S3 CSI，也可以用 `aws s3 sync` 做导出。
  - Pod 访问 S3 需要 IRSA（样例 `setup/setup-irsa.sh`）。
- `RayJob` 的取消和清理字段：`suspend`（会删除 RayCluster 和 submitter；用 Kueue 时不要手动改这个字段）、`deletionStrategy.deletionRules`（beta，需要打开 feature gate）、`preRunningDeadlineSeconds`。
- `submissionMode` 可选 `K8sJobMode`（默认）、`HTTPMode`、`InteractiveMode`、`SidecarMode`。

### 1.4 Gateway（18765）的网络可达性：ACR VPC 模式回调

事实：
1. HyperPod EKS 必须提供 `VpcConfig`，包含 Subnets 和 1–5 个 SecurityGroupIds，创建后不能修改。实例组可以用 `OverrideVpcConfig` 覆盖。HyperPod 节点的 ENI 位于客户 VPC 子网内，Pod 由 VPC CNI 分配 VPC 内可路由的 IP。文档原文写到，在 EKS 编排下每台 P5 可占用 81 个 IP（主网卡 50 个，其余 31 张网卡各 1 个），所以要预留足够大的子网 CIDR。来源：https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-prerequisites.html
2. 对 EFA 实例，安全组必须对自身放通全部入站和出站流量，文档还提醒出站不要用 `0.0.0.0/0`，否则 EFA 健康检查可能失败（同上来源）。
3. 默认情况下，所有实例都部署在单个 AZ。EFA 流量不能跨 AZ。
4. Pod 使用 secondary IP，实际生效的是节点 ENI 上的 SG，也就是 HyperPod `VpcConfig` 里的 SG。EKS Security Groups for Pods 依赖 trunk ENI 和 VPC Resource Controller（https://docs.aws.amazon.com/eks/latest/userguide/security-groups-for-pods.html ）。HyperPod 文档中没有找到支持 SGP 的说明，因此本文按“不可用”来设计（未验证）。

几种暴露方式的对比：

| 方式 | ACR ENI 能否访问 | 说明 |
|---|---|---|
| `ClusterIP` Service | 不能 | 虚拟 IP 只能在集群节点的 kube-proxy 规则内解析，VPC 中其他 ENI 无法路由到它 |
| Pod IP 直连 `http://<podIP>:18765` | 能 | 延迟最低，不需要 LB。缺点是 Pod 重建后 IP 会变，但每个 run 本来就会重新下发 URL，所以影响不大。推荐 |
| headless Service 加 CoreDNS | 不能 | ACR 用不了集群 DNS |
| internal NLB（AWS LBC，`target-type: ip`） | 能 | 提供稳定 DNS，可以挂独立 SG。代价是每个 run 建一个 NLB，耗时数十秒到几分钟，还有额外费用。NLB TCP idle timeout 默认 350s，长连接或流式调用需要注意 |
| NodePort | 理论上能 | 不推荐，节点 IP 不稳定，而且要放开端口范围 |

推荐落地方案：
- 安全组规划：HyperPod 集群在创建时就挂两个 SG。
  - `sg-hp-efa`：对自身放通全部流量，满足 EFA 要求。
  - `sg-hp-gw`：入站 TCP 18765，来源为 `sg-acr`。之后只改规则，不需要改 SG 成员关系；成员关系在集群创建后不能修改。
- ACR 侧：`networkMode: VPC`，子网与 HyperPod 在同一 VPC（最好同 AZ，可以避免跨 AZ 流量费和延迟）。`sg-acr` 出站放行 TCP 18765 到 `sg-hp-gw`。ACR 自身访问 ECR、Logs、Bedrock 所需的 NAT 或 VPC endpoint 另行规划。
- Gateway 地址发现：在 verl 中，`main_ppo` 的 `TaskRunner` 是一个 Ray actor，不一定调度到 head Pod 上，可能落在某个 worker Pod。有两种处理方式：
  - (a) 在 head 上声明自定义资源 `gateway`，gateway actor 用 `resources={"gateway": 0.01}` 钉在 head 上，再通过 Downward API 拿到的 `POD_IP` 拼出 URL；
  - (b) 不钉节点，gateway 启动时调用 `ray.util.get_node_ip_address()`（结果就是所在 Pod 的 IP）得到地址，再写进 ACR invoke payload 的 callback URL。
- 如果必须用稳定 DNS，就用下面的 Service。前提是安装 AWS Load Balancer Controller；HyperPod Inference add-on 会把它作为依赖一起安装。

```yaml
apiVersion: v1
kind: Service
metadata:
  name: rl-run-{{runId}}-gw
  annotations:
    service.beta.kubernetes.io/aws-load-balancer-type: external
    service.beta.kubernetes.io/aws-load-balancer-scheme: internal
    service.beta.kubernetes.io/aws-load-balancer-nlb-target-type: ip
    service.beta.kubernetes.io/aws-load-balancer-security-groups: sg-nlb-gw   # NLB SG 入站只允许 sg-acr；sg-hp-gw 要允许 sg-nlb-gw
    service.beta.kubernetes.io/aws-load-balancer-subnets: subnet-a,subnet-b
spec:
  type: LoadBalancer
  selector: {ray.io/cluster: <raycluster-name>, ray.io/node-type: head}
  ports: [{name: gw, port: 18765, targetPort: 18765, protocol: TCP}]
```

安全提醒：18765 上的 gateway 是 HTTP 明文，默认没有鉴权。至少要做到 SG 最小化，并给每个 run 生成一次性的 bearer token，由 ACR 调用时携带。

---

## 2. 平台后端的作业提交、状态、日志与取消

| 方式 | 适合 verl/Ray 吗 | 提交 | 状态 | 日志 | 取消 |
|---|---|---|---|---|---|
| Kubernetes Python client（`kubernetes==36.0.3`） | 适合，推荐 | `CustomObjectsApi.create_namespaced_custom_object("ray.io","v1",ns,"rayjobs",body)` | `watch.Watch().stream(list_namespaced_custom_object, ...)`，读取 `.status.jobDeploymentStatus`（Initializing/Running/Complete/Failed/Suspended/Retrying…）和 `.status.jobStatus`（PENDING/RUNNING/SUCCEEDED/FAILED/STOPPED） | 在 K8sJobMode 下，submitter Pod（label `job-name=<rayjob>`）的日志就是 `ray job logs --follow` 的输出，用 `CoreV1Api.read_namespaced_pod_log(follow=True, _preload_content=False)` 流式读取 | 不用 Kueue 时 `patch spec.suspend=true`；用 Kueue 时直接 `delete_namespaced_custom_object` |
| `kubectl` | 适合，偏运维 | `kubectl apply -f rayjob.yaml` | `kubectl get rayjob X -o jsonpath='{.status.jobStatus}'` | `kubectl logs -f -l job-name=X` | `kubectl delete rayjob X` |
| Ray Jobs API（`JobSubmissionClient`） | 适合，用于往常驻 RayCluster 提交 | `submit_job(entrypoint, runtime_env)`，地址为 `http://<head-svc>:8265`（需要网络通路），或 `sagemaker_ray://<cluster>/<ns>`（IAM 认证，需要 Ray Endpoint Operator） | `get_job_status` | `tail_job_logs()`（async 迭代器） | `stop_job()`（优雅停止） |
| `hyp` CLI / SDK（`pip install sagemaker-hyperpod`，当前 3.11.0） | 不适合 | 只支持 `hyp-pytorch-job`（HyperPodPyTorchJob，基于 torchrun 和 HyperPod Training Operator）、`hyp-recipe-job`、`hyp-jumpstart-endpoint`、`hyp-custom-endpoint`、`hyp-space`，没有 RayJob 子命令 | `hyp describe/list` | `hyp get-logs hyp-pytorch-job --job-name J --pod-name P` | `hyp delete hyp-pytorch-job --job-name J` |

`hyp` 参考：https://github.com/aws/sagemaker-hyperpod-cli 。示例：`hyp create hyp-pytorch-job --job-name j --image ... --instance-type ml.p5.48xlarge --node-count 2 --tasks-per-node 8 --queue-name q --deep-health-check-passed-nodes-only true --volume name=fsx,type=pvc,mount_path=/fsx,claim_name=fsx-claim`。这条路适合 merge、SFT 这类非 Ray 作业，或者推理 endpoint 管理（`hyp create hyp-custom-endpoint --model-source-type s3 ...`）。

后端接入要点：
- 认证：后端的 IAM 角色需要 EKS access entry，按 namespace 关联上面提到的 HyperPod 访问策略。Python client 需要 bearer token：可以调用 `aws eks get-token`，也可以自己生成 STS presigned `GetCallerIdentity`（带 `x-k8s-aws-id` header）。
- 日志持久化：Ray 日志默认写在 `/tmp/ray` 的 emptyDir 里，`shutdownAfterJobFinishes` 删除集群后就没有了。建议把 verl metrics 和 rollout trace 写到 `/fsx/runs/<id>/` 或直接写 S3；需要集中采集时用 Fluent Bit / CloudWatch Container Insights，或 HyperPod Observability add-on（AMP/AMG）。
- 排队：HyperPod Task governance 基于 Kueue。RayJob 加上 `kueue.x-k8s.io/queue-name` 和 `kueue.x-k8s.io/priority-class` label 就能进入队列。启用 Kueue 时，`suspend` 字段由 Kueue 控制，平台不要去改它。
- 弹性和恢复：HyperPod 节点故障时会自动替换节点，KubeRay 会重建 worker Pod。verl 通过 `resume_mode=auto` 从最近的 checkpoint 继续。head 挂掉会导致整个 job 失败（GCS FT 需要外部 Redis），平台可以用 `backoffLimit` 重试，重试时会新建一个 RayCluster。

---

## 3. Checkpoint merge/export（k8s Job）

verl 文档核实结果（https://verl.readthedocs.io/en/latest/advance/checkpoint.html ，PyPI 当前版本 verl 0.9.1）：

```text
python -m verl.model_merger merge --backend {fsdp,megatron} [--local_dir LOCAL_DIR] [--tie-word-embedding]
  [--is-value-model] [--use_cpu_initialization] [--target_dir TARGET_DIR] [--hf_upload_path ...] [--private]
示例：python -m verl.model_merger merge --backend fsdp \
        --local_dir checkpoints/<proj>/<exp>/global_step_1/actor --target_dir /path/to/merged_hf_model
Megatron 分布式合并：torchrun --nproc_per_node 1 --nnodes 8 --node_rank ${RANK} -m verl.model_merger merge --backend megatron ...
另有子命令 test（与参考模型比对）；老版本 checkpoint 中没有 fsdp_config.json 时，改用 verl/scripts/legacy_model_merger.py
```

- Megatron 后端：如果启用 megatron-bridge（`use_mbridge=True`），`save_contents` 加上 `hf_model` 就会直接产出 `model/huggingface/`，可以跳过 merge。只有纯 `dist_ckpt` 才需要 merger。
- FSDP 后端：merger 运行在 CPU 上即可（读取 `actor/` 下所有 rank 的分片，再从 `actor/huggingface/` 复制 config 和 tokenizer）。内存需求约为模型 bf16 大小的 2 倍或更多。也可以在训练时把 `hf_model` 加进 `save_contents`，直接导出 HF 权重；该行为是否与 merger 等价，需要实测。
- 多模态 Qwen3.5/3.6：merge 完成后要确认 `target_dir` 里有 `chat_template`、`preprocessor_config.json` 等 processor 文件，没有就从基座模型补齐。

最小 Job（CPU 节点、共享 FSx、通过 IRSA 写 S3）：

```yaml
apiVersion: batch/v1
kind: Job
metadata: {name: merge-{{runId}}-step{{N}}, namespace: rl-team-a}
spec:
  backoffLimit: 1
  ttlSecondsAfterFinished: 3600
  template:
    spec:
      serviceAccountName: rl-s3-writer          # IRSA → s3:PutObject on s3://<bucket>/models/*
      restartPolicy: Never
      nodeSelector: {sagemaker.amazonaws.com/node-health-status: Schedulable}
      containers:
      - name: merge
        image: <acct>.dkr.ecr.<region>.amazonaws.com/verl:<tag>   # 需要包含 awscli 或 s5cmd
        command: ["bash","-lc"]
        args:
        - |
          set -euo pipefail
          SRC=/fsx/runs/{{runId}}/ckpt/global_step_{{N}}/actor
          DST=/fsx/runs/{{runId}}/hf/global_step_{{N}}
          python -m verl.model_merger merge --backend fsdp --local_dir "$SRC" --target_dir "$DST"
          aws s3 sync "$DST" s3://<bucket>/models/{{runId}}/step{{N}}/ --only-show-errors
          echo '{"status":"ok"}' > "$DST/_EXPORT_DONE"
        resources:
          requests: {cpu: "16", memory: 256Gi}
          limits:   {cpu: "16", memory: 256Gi}
        volumeMounts: [{name: fsx, mountPath: /fsx}]
      volumes: [{name: fsx, persistentVolumeClaim: {claimName: fsx-claim}}]
```

也可以不单独起 Job，把 merge 和 sync 直接串在 RayJob `entrypoint` 末尾（`python -m verl.trainer.main_ppo ... && python -m verl.model_merger ...`），省掉一次调度。缺点是这段时间 GPU 节点处于空闲占用状态。

---

## 4. 在同一 HyperPod EKS 集群上部署推理

### 4.1 HyperPod Inference Operator

- 安装：EKS add-on `amazon-sagemaker-hyperpod-inference`，2026-09-21 发布的版本为 `v2.1.0-eksbuild.1`，对应 Operator v3.7。也可以用 Helm 安装：`sagemaker-hyperpod-cli/helm_chart/HyperPodHelmChart/charts/inference-operator`（chart 2.7.0）。依赖组件有 AWS LBC、KEDA、cert-manager、S3/FSx CSI 和 metrics-server，安装后运行在 `hyperpod-inference-system` 命名空间。TLS 证书使用名称前缀为 `hyperpod-tls-*` 的 S3 桶。来源：https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-model-deployment-setup.html 、 https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-inference-release-notes.html
- CRD：`InferenceEndpointConfig`（`inference.sagemaker.aws.amazon.com/v1`，用于自定义模型）和 `JumpStartModel`；还有派生资源 `SageMakerEndpointRegistration`。v1 spec 字段（取自 CRD 源码）：`modelName, instanceType/instanceTypes, invocationEndpoint, replicas, modelSourceConfig{modelSourceType: s3|fsx|huggingface, s3Storage, fsxStorage, huggingFaceModel, modelLocation, prefetchEnabled}, worker, loadBalancer{healthCheckPath(默认 /ping), routingAlgorithm, idleTimeoutSeconds}, autoScalingSpec{min/maxReplicaCount, cloudWatchTrigger(List), prometheusTrigger(List), cooldownPeriod...}（KEDA）, tlsConfig, dnsConfig, intelligentRoutingSpec, kvCacheSpec, pdSpec, inferenceGateway, kubernetes{tolerations...}, nodeAffinity, dataCapture, endpointName, tags`。
- 访问路径：
  - (1) Operator 为每个部署创建 ALB Ingress `alb-<name>`，TLS 默认使用自签证书（公钥上传到 S3），可以通过 `tlsConfig.customCertificateConfig` 换成 ACM 证书。CRD 里没有 ALB scheme（internal 还是 internet-facing）字段，部署前需要实测确认。来源：https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-model-deployment-custom-certs.html
  - (2) Operator 会注册一个 SageMaker endpoint，可以用 `aws sagemaker-runtime invoke-endpoint --endpoint-name <name>` 调用（SigV4 鉴权），body 会转发到 `invocationEndpoint`（例如 `v1/chat/completions`）。

S3 上的微调模型示例（基于官方 YAML 精简，来源：https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-model-deployment-deploy-ftm.html ）：

```yaml
apiVersion: inference.sagemaker.aws.amazon.com/v1
kind: InferenceEndpointConfig
metadata: {name: rl-{{runId}}-eval, namespace: rl-team-a}
spec:
  modelName: rl-{{runId}}
  endpointName: rl-{{runId}}-eval
  instanceType: ml.g6e.12xlarge
  invocationEndpoint: v1/chat/completions
  replicas: 1
  modelSourceConfig:
    modelSourceType: s3
    s3Storage: {bucketName: <bucket>, region: us-west-2}
    modelLocation: models/{{runId}}/step{{N}}
    prefetchEnabled: true
  loadBalancer: {healthCheckPath: /health}
  worker:
    image: vllm/vllm-openai:v0.19.1
    args: ["--model","/opt/ml/model","--port","8000","--served-model-name","rl-{{runId}}",
           "--tensor-parallel-size","4","--reasoning-parser","qwen3",
           "--enable-auto-tool-choice","--tool-call-parser","qwen3_coder"]
    modelInvocationPort: {containerPort: 8000, name: http}
    modelVolumeMount: {name: model-weights, mountPath: /opt/ml/model}
    resources:
      requests: {nvidia.com/gpu: 4, cpu: "24", memory: 160Gi}
      limits:   {nvidia.com/gpu: 4}
```

### 4.2 直接部署 vLLM（Deployment 加 internal NLB）

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: {name: eval-{{runId}}, namespace: rl-team-a}
spec:
  replicas: 1
  selector: {matchLabels: {app: eval-{{runId}}}}
  template:
    metadata: {labels: {app: eval-{{runId}}}}
    spec:
      serviceAccountName: rl-s3-reader
      nodeSelector: {node.kubernetes.io/instance-type: ml.g6e.12xlarge}
      initContainers:
      - name: fetch
        image: amazon/aws-cli:2.17.0
        args: ["s3","sync","s3://<bucket>/models/{{runId}}/step{{N}}/","/model/"]
        volumeMounts: [{name: model, mountPath: /model}]
      containers:
      - name: vllm
        image: vllm/vllm-openai:v0.19.1
        args: ["--model","/model","--served-model-name","rl-{{runId}}","--port","8000",
               "--tensor-parallel-size","4","--api-key","$(VLLM_API_KEY)",
               "--reasoning-parser","qwen3","--enable-auto-tool-choice","--tool-call-parser","qwen3_coder"]
        env: [{name: VLLM_API_KEY, valueFrom: {secretKeyRef: {name: eval-key, key: k}}}]
        ports: [{containerPort: 8000}]
        readinessProbe: {httpGet: {path: /health, port: 8000}, periodSeconds: 10, failureThreshold: 60}
        resources: {limits: {nvidia.com/gpu: 4}}
        volumeMounts: [{name: model, mountPath: /model}, {name: dshm, mountPath: /dev/shm}]
      volumes:
      - {name: model, emptyDir: {sizeLimit: 200Gi}}   # 也可以直接挂 fsx-claim，免去从 S3 下载
      - {name: dshm, emptyDir: {medium: Memory, sizeLimit: 32Gi}}
---
apiVersion: v1
kind: Service
metadata:
  name: eval-{{runId}}
  annotations:
    service.beta.kubernetes.io/aws-load-balancer-type: external
    service.beta.kubernetes.io/aws-load-balancer-scheme: internal
    service.beta.kubernetes.io/aws-load-balancer-nlb-target-type: ip
spec:
  type: LoadBalancer
  selector: {app: eval-{{runId}}}
  ports: [{port: 8000, targetPort: 8000}]
```

### 4.3 选型：用于评测的微调 Qwen3.5/3.6 HF checkpoint

| 维度 | Inference Operator（`InferenceEndpointConfig`） | 直接部署 vLLM（Deployment 加 NLB） |
|---|---|---|
| 从 S3/FSx 加载模型 | 内置（`prefetchEnabled`） | 需要自己写 initContainer，或直接挂 FSx |
| 给 AgentCore agent 用的 OpenAI 兼容 URL | ALB 的 `https://<alb>/v1/...` 可用，但默认是自签证书，客户端要信任 S3 上的证书或换 ACM 加自有域名；ALB scheme 需要核实 | `http://<internal-nlb>:8000/v1` 加 `--api-key`，最简单 |
| IAM 鉴权和不依赖网络通路 | 支持。通过 SageMaker endpoint 调用（SigV4），ACR 甚至可以用 PUBLIC 网络模式。但 OpenAI SDK 不能直接用，要用 boto3 `invoke_endpoint(_with_response_stream)`，或者 Agent 框架自带的 SageMaker model provider（例如 Strands 的 SageMaker provider，需核实） | 不支持，靠 SG 和 api-key |
| 扩缩容和路由 | KEDA（CloudWatch/Prometheus trigger）、智能路由（prefix/kv-aware）、KV cache（LMCache）、PD 分离 | 要自己配 HPA 或 KEDA |
| 上线速度和排障 | 依赖组件多（ALB、证书、注册），首次部署较慢 | Pod Ready 即可用 |
| 适用场景 | 长期在线、多租户、需要对外暴露的服务 | 一次性评测，按 run 创建、评测完删除 |

结论：
- 训练后的评测阶段，优先选直接部署 vLLM，按 run 创建、评测完删除。ACR agent 以 VPC 模式访问，`base_url=http://<nlb-dns>:8000/v1`，`model=rl-<runId>`，api-key 存在 Secrets Manager 中，以环境变量形式注入 agent。
- 如果平台要求不打通网络，或者需要 IAM 级访问控制，就选 Operator 并通过 SageMaker endpoint 调用。这时 agent 端要用 SageMaker provider，或者封装一层 OpenAI→InvokeEndpoint 适配。
- 模型兼容性：Qwen3.5/3.6 要求较新的 vLLM。vLLM 0.13 不认识 `qwen3_5_moe`；AWS 文档示例用的是 `vllm/vllm-openai:v0.19.1`，0.19 已能服务 Qwen3.5。推荐参数为 `--reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder`，纯文本场景可以加 `--language-model-only`。来源：https://docs.vllm.ai/projects/recipes/en/stable/Qwen/Qwen3.5.html 。已知 vLLM 0.19 有一个问题：tool_call 嵌在 `<think>` 里时会丢失（https://github.com/vllm-project/vllm/issues/39056 ），评测前请用 agent 场景回归一遍。
- 资源复用：推理 Pod 与训练争抢同一批 GPU。建议给推理单独建一个实例组（例如 g6e），用 taint 和 toleration 隔离（Operator v3.7 已支持 `spec.kubernetes.tolerations`），或者用 Task governance 配额来控制。

---

## 5. 待验证 / 风险

1. HyperPod 节点是否支持 EKS Security Groups for Pods：文档未提及，本文按“不支持”设计，SG 统一配在 `VpcConfig` 上。
2. Inference Operator 创建的 ALB 是 internal 还是 internet-facing，以及能否指定子网：需要在集群上实测 `kubectl get ingress alb-<name> -o yaml` 来确认。
3. verl 0.9.x 中 TaskRunner 和 agent-loop/gateway actor 的实际调度位置会决定 callback IP 的取法：方案 (a) 钉 head，方案 (b) 自报 node IP，见 §1.4。
4. 在 Qwen3.5 MoE / 多模态模型上，FSDP 的 `hf_model` 直接保存与 `model_merger` 的结果是否一致，以及 processor 文件是否完整。
5. Kueue LocalQueue 的命名规则（`hyperpod-ns-<team>-localqueue`）需要在启用 Task governance 后实际查看确认。
