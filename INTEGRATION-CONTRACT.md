# Integration contract — LoopGrid + Agno

## Native surfaces

The v0.1.0 integration uses Agno 3.x native hooks:

- `pre_hooks`: run identity / decision binding
- `tool_hooks`: tool request + actual wrapped execution boundary
- `post_hooks`: terminal run/model output

## Correlation

`RunContext.run_id` is the authoritative run correlation key. LoopGrid never generates a substitute when Agno does not provide it.

## Tool semantics

Agno global tool middleware requires the hook to call the supplied continuation. A successful return proves the wrapped tool function executed; it does **not** prove a downstream business outcome. Therefore:

- before continuation: `tool_requested`
- successful continuation return: `tool_executed`
- exception: failed `tool_result`
- downstream state confirmation: explicit `outcome_observed`

For `external_execution=True`, use explicit application-owned `record_action_executed` after confirmation.

## Policy and approval

Configured `policy` is caller-owned evidence and is recorded before the first tool execution. Human approval is never inferred from a pause; use the LoopGrid review boundary when the reviewer decision is authoritative.

## Privacy

`capture_content=False` by default. Input/messages/arguments/results/output/errors are hashed unless explicitly opted in.

## Concurrency

All LoopGrid mutations produced by one `LoopGridAgno` instance are serialized because LoopGrid evidence chains are workspace-scoped. This prevents concurrent Agno tool hooks from racing the local integration's chain writes.
