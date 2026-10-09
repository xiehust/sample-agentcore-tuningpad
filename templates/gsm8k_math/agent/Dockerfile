# ACR rollout agent (linux/arm64). Toolkit from the platform's local checkout wheel
# so the agent contract matches the trainer. ADOT is installed but only the eval-only
# runtime (TP_OBSERVABILITY=1) starts under it; training runtimes keep the plain process.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
WORKDIR /app
ENV UV_SYSTEM_PYTHON=1 UV_COMPILE_BYTECODE=1 UV_NO_PROGRESS=1 PYTHONUNBUFFERED=1 DOCKER_CONTAINER=1
COPY dist/agentcore_rl_toolkit-*.whl /tmp/
RUN uv pip install /tmp/agentcore_rl_toolkit-*.whl \
      "strands-agents[openai,otel]==1.18.0" "strands-agents-tools==0.2.16" \
      "aws-opentelemetry-distro==0.21.0" \
    && rm -f /tmp/*.whl
COPY rl_app.py reward.py models.py tp_config.json tp_entry.sh /app/
RUN useradd -m -u 1000 bedrock_agentcore
USER bedrock_agentcore
EXPOSE 8080
CMD ["sh", "/app/tp_entry.sh"]
