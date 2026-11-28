# TuningPad

TuningPad is a console for training agents with reinforcement learning on AWS. These terms describe what it trains and what runs a rollout.

## Language

**Agent**:
The deployable program TuningPad builds from a template and runs on AgentCore Runtime; one **Agent** can contain several **Harnesses**.
_Avoid_: runtime (when meaning the program), agent program

**Harness**:
A third-party agent program, such as Claude Code or OpenCode, that runs inside an **Agent** unmodified and owns its own loop, tools and context.
_Avoid_: scaffold, framework, agent (when meaning the third-party program)

**Task**:
One unit of training or evaluation work, made of an instruction, input files and a **Verifier**; it names no **Harness**.
_Avoid_: problem, sample, environment

**Verifier**:
The grading program a **Task** ships with, run after the **Harness** exits, that produces the reward.
_Avoid_: grader, judge, tests (when meaning the whole grading step)
