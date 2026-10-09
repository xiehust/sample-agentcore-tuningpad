# OfficeBench rollout agent (linux/arm64): LibreOffice / Tesseract / ImageMagick and the
# OfficeBench app scripts, plus the toolkit wheel from the platform's checkout.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
WORKDIR /app
ENV UV_SYSTEM_PYTHON=1 UV_COMPILE_BYTECODE=1 UV_NO_PROGRESS=1 PYTHONUNBUFFERED=1 \
    DOCKER_CONTAINER=1 BYPASS_TOOL_CONSENT=true STRANDS_NON_INTERACTIVE=true
RUN apt-get update && apt-get install -y --no-install-recommends \
      libreoffice-writer libreoffice-calc tesseract-ocr imagemagick git ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
# pinned commit of the OfficeBench app scripts
ARG OFFICEBENCH_REF=main
RUN git clone https://github.com/zlwang-cs/OfficeBench.git /tmp/officebench \
    && git -C /tmp/officebench checkout "$OFFICEBENCH_REF" \
    && cp -r /tmp/officebench/apps /apps && rm -rf /tmp/officebench
RUN mkdir -p /testbed/data /testbed/calendar /testbed/emails
COPY dist/agentcore_rl_toolkit-*.whl /tmp/
RUN uv pip install /tmp/agentcore_rl_toolkit-*.whl \
      "strands-agents[openai,otel]==1.18.0" "strands-agents-tools==0.2.16" python-dotenv \
      "aws-opentelemetry-distro==0.21.0" \
      openpyxl python-docx PyMuPDF pdf2docx pytesseract icalendar Pillow pytz fire \
    && rm -f /tmp/*.whl
COPY rl_app.py reward.py models.py utils.py tools.py tp_entry.sh /app/
RUN useradd -m -u 1000 bedrock_agentcore && chown -R bedrock_agentcore:bedrock_agentcore /testbed /app
USER bedrock_agentcore
EXPOSE 8080
# ADOT only when TP_OBSERVABILITY=1 (the eval-only runtime); training keeps the plain process
CMD ["sh", "/app/tp_entry.sh"]
