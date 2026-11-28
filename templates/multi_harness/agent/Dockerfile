# multi_harness rollout Agent (linux/arm64).
# The SmolDataEnvs Task environment (savatar101/env-data-agent-train:base, amd64-only, unpinned
# packages) rebuilt for arm64 with pinned versions, plus the Harnesses and the toolkit wheel.
# Nothing is installed at rollout time: training runtimes may have no internet access.
FROM python:3.12-slim-trixie
ARG NODE_VERSION=26.13.1
ARG NODE_SHA256=89e1853138db51e8b82d3e98ba9c0d3f2956a28e5e55ea1b7e12d25aae92d5dd
ARG CLAUDE_CODE_VERSION=2.1.332
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git xz-utils libatomic1 \
    && rm -rf /var/lib/apt/lists/*
# Node only installs Claude Code (its npm package ships a native linux-arm64 binary)
RUN curl -fsSLo /tmp/node.tar.xz \
      "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-arm64.tar.xz" \
    && echo "${NODE_SHA256}  /tmp/node.tar.xz" | sha256sum -c - \
    && tar -xJf /tmp/node.tar.xz -C /opt \
    && mv "/opt/node-v${NODE_VERSION}-linux-arm64" /opt/node && rm /tmp/node.tar.xz
ENV PATH=/opt/node/bin:$PATH
RUN npm install -g --no-fund --no-audit "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && claude --version
# The Task environment (plotly is named in the Task instructions, so it is included)
RUN pip install pandas==3.1.1 numpy==2.5.5 matplotlib==3.11.2 seaborn==0.13.2 \
      scipy==1.18.1 scikit-learn==1.9.1 statsmodels==0.15.0 tabulate==0.10.0 \
      plotly==7.2.0 math-verify==0.9.0
COPY dist/agentcore_rl_toolkit-*.whl /tmp/
# [server] = bedrock-agentcore (AgentCoreRuntimeApp, boto3)
RUN pip install "$(ls /tmp/agentcore_rl_toolkit-*.whl)[server]" && rm -f /tmp/*.whl
WORKDIR /app
COPY rl_app.py tp_config.json /app/
COPY harness_agent /app/harness_agent
# Claude Code refuses --dangerously-skip-permissions as root: run as an ordinary user
RUN useradd -m -u 1000 -d /home/user user \
    && mkdir -p /workdir /home/user/input /tests /logs \
    && chown -R user:user /workdir /home/user /tests /logs /app
USER user
ENV HOME=/home/user
EXPOSE 8080
CMD ["python", "-m", "rl_app"]
