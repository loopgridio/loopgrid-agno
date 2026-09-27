# LoopGrid for Agno

Signed, independently verifiable decision evidence for **Agno 3.x** Agents and Teams.

`loopgrid-agno` uses Agno's native `pre_hooks`, `post_hooks`, and `tool_hooks` surfaces. It does not replace Agno tracing and it does not execute business actions on its own.

## Why this integration exists

Agno tells your application how an agent ran. LoopGrid preserves cryptographically verifiable evidence of consequential decisions, policy/authority context, tool execution, and observed outcomes.

```text
Agno run
  -> decision_created
  -> model_completed (tool-call boundary)
  -> policy_evaluated
  -> tool_requested
  -> Agno tool continuation executes
  -> tool_executed
  -> model_completed (terminal output)
  -> application observes real outcome
  -> outcome_observed
  -> verify
```

## Install

```bash
pip install loopgrid-agno
```

## Native Agno usage

```python
from agno.agent import Agent
from loopgrid_agno import LoopGridAgno

evidence = LoopGridAgno(
    authority={"scope": ["refund:create"], "environment": "sandbox"},
    policy={
        "policy_id": "refund-policy",
        "version": "1",
        "decision": "auto_allowed",
    },
    proposed_action={"tool": "sandbox_refund"},
)

agent = Agent(
    model="openai:gpt-5.4-mini",
    tools=[sandbox_refund],
    **evidence.hooks(),
)

run_id = "refund-demo-001"
result = agent.run("Refund $25", run_id=run_id)

# Only after your application actually observes the downstream result:
evidence.observe_outcome(
    run_id,
    {"status": "succeeded", "sandbox": True, "real_money_moved": False},
)
```

For `Agent.arun()` / async tools, use `**evidence.async_hooks()`.

## Evidence semantics

### What is automatic

- `pre_hook` binds one LoopGrid decision to `RunContext.run_id`.
- At the first wrapped tool boundary, LoopGrid records a model-completion observation without storing raw messages by default.
- `tool_hook` records `tool_requested`, calls Agno's continuation exactly once, then records `tool_executed` if it returned successfully.
- Tool exceptions are recorded as failed `tool_result` evidence and re-raised.
- `post_hook` records the terminal model/run output as `model_completed` unless the run is paused/cancelled/errored.

### What is never invented

LoopGrid does not infer:

- delegated authority,
- application policy,
- human approval,
- an external action for `external_execution=True`,
- a downstream business outcome.

Use `record_policy`, `review_run`, `record_action_executed`, and `observe_outcome` at the authoritative application boundary.

## Agno approvals and external execution

Agno supports `requires_confirmation=True`, `requires_user_input=True`, and `external_execution=True`. These are mutually exclusive tool modes. LoopGrid preserves evidence but does not convert a pause or model request into proof of approval/execution.

For `external_execution=True`, call `record_action_executed(...)` only after your application confirms the action happened.

## Privacy

Default: `capture_content=False`.

LoopGrid stores hashes and structural metadata instead of raw run input, messages, tool arguments, tool results, model output, or exception text. Raw content is captured only when explicitly enabled.

## Agno tool-call IDs

Agno's global `tool_hooks` middleware exposes the run context, tool name, continuation, and arguments. It does not expose `FunctionCall.call_id` through that global middleware signature. `loopgrid-agno` therefore creates a LoopGrid-owned, run-scoped evidence call ID (`lg-<run_id>-<index>`) and does **not** claim it is Agno's native call ID.

Agno's per-tool pre/post hook surface can expose `FunctionCall` (`fc`) and its native `call_id`, but requiring every tool to be individually decorated would make the integration much less drop-in. The global hook is the v0.1.0 default.

## Scope of v0.1.0

Validated target: Agno `3.0.11` direct Agent/Team runs.

AgentOS persisted `continue_run` / cross-process HITL continuation is intentionally not claimed until independently validated. The direct runtime keeps `RunContext.run_id` stable and is the release target for v0.1.0.

## Verification target

Release E2E gate:

```text
lifecycle = evidence_complete
applicable coverage = 100%
verify.valid = true
failures = []
```

See `VALIDATION.md` and `TESTING-WINDOWS.md`.
