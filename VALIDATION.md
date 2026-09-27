# Validation

## Release gates

1. `python -m pytest -v`
2. `python -m compileall -q src examples tests`
3. Install Agno 3.0.11 and run the deterministic local model E2E.
4. Run against LoopGrid Core 0.8.1-design-partner in a fresh workspace.
5. Require `evidence_complete`, coverage 100%, `verify.valid=true`, and no failures.
6. `python -m build`
7. `python -m twine check dist/*`
8. Fresh wheel install in a clean venv.

## Semantic assertions

- one Agno `run_id` -> one LoopGrid decision
- no raw content by default
- policy is explicit caller-owned evidence
- normal tool hook records request before continuation and execution only after successful return
- exceptions never become `tool_executed`
- external execution remains explicit
- observed outcome remains explicit
- concurrent mutations are serialized
- fail-open mode never prevents Agno tool execution solely because LoopGrid is unavailable
