#!/bin/sh
# Container entrypoint. Training runtimes run the agent as before; the eval-only runtime
# (TuningPad sets TP_OBSERVABILITY=1 on it) runs it under ADOT so Strands' spans reach
# CloudWatch Transaction Search (aws/spans) for the eval trace view.
if [ "${TP_OBSERVABILITY:-}" = "1" ]; then
  exec opentelemetry-instrument python -m rl_app
fi
exec python -m rl_app
