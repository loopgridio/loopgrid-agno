# Security

`loopgrid-agno` is evidence middleware. It does not grant authority and must not be used as a substitute for application authorization.

Defaults:

- `capture_content=False`
- raw exception messages are hashed
- raw tool arguments/results are hashed
- `fail_open=False`

For sensitive workloads, use a dedicated LoopGrid workspace and keep LoopGrid Core authentication enabled outside local development.
