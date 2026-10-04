# TuningPad trainer image (linux/amd64): verl + agentcore-rl-toolkit from the platform's
# checkout, installed with the toolkit's own uv.lock (`--frozen`) so the trainer, the
# rollout gateway and the agents speak the same contract.
#   PROFILE=fsdp      uv sync --extra verl
#   PROFILE=megatron  uv sync --extra verl --group verl-megatron  (+ megatron-bridge CP clamp)
# CUDA 13 needs NVIDIA driver >= 580 (HyperPod EKS AMI ships 580.126.09).
FROM nvidia/cuda:13.0.2-cudnn-devel-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-dev git curl ca-certificates build-essential ninja-build patch unzip \
      libnuma1 pciutils iproute2 \
    && rm -rf /var/lib/apt/lists/*

# EFA user space (libfabric + aws-ofi-nccl); the kernel module comes from the host AMI.
ARG EFA_INSTALLER_VERSION=1.45.0
RUN apt-get update \
    && curl -fsSL https://efa-installer.amazonaws.com/aws-efa-installer-${EFA_INSTALLER_VERSION}.tar.gz \
      | tar xz -C /tmp \
    && cd /tmp/aws-efa-installer && ./efa_installer.sh -y --skip-kmod --skip-limit-conf --no-verify \
    && rm -rf /tmp/aws-efa-installer /var/lib/apt/lists/*

# AWS CLI v2 (data staging, log/ckpt sync, exports)
RUN curl -fsSL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o /tmp/awscli.zip \
    && unzip -q /tmp/awscli.zip -d /tmp && /tmp/aws/install && rm -rf /tmp/awscli.zip /tmp/aws

COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv
COPY toolkit/ /opt/toolkit/
WORKDIR /opt/toolkit
ARG PROFILE=fsdp
ENV UV_PROJECT_ENVIRONMENT=/opt/toolkit/.venv UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=automatic
RUN if [ "$PROFILE" = "megatron" ]; then \
      uv sync --frozen --python 3.12 --extra verl --group verl-megatron; \
    else \
      uv sync --frozen --python 3.12 --extra verl; \
    fi \
    && rm -rf /root/.cache/uv
RUN if [ "$PROFILE" = "megatron" ]; then VENV=/opt/toolkit/.venv bash patches/apply-megatron-bridge-cp-clamp.sh; fi

ENV PATH=/opt/toolkit/.venv/bin:/usr/local/cuda/bin:/opt/amazon/efa/bin:$PATH \
    LD_LIBRARY_PATH=/opt/amazon/ofi-nccl/lib/x86_64-linux-gnu:/opt/amazon/ofi-nccl/lib:/opt/amazon/efa/lib:/usr/local/cuda/lib64 \
    CUDA_HOME=/usr/local/cuda \
    RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 \
    TUNINGPAD_PROFILE=$PROFILE
RUN python -c "import verl, vllm, agentcore_rl_toolkit.backends.verl.trainer; print('verl', verl.__version__, 'vllm', vllm.__version__)"
