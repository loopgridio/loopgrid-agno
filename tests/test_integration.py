from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from loopgrid_agno import LoopGridAgno


class FakeGrid:
    def __init__(self):
        self.calls = []
        self.n = 0
        self.active_mutations = 0
        self.max_active_mutations = 0
        self.lock = threading.Lock()

    def _enter(self):
        with self.lock:
            self.active_mutations += 1
            self.max_active_mutations = max(self.max_active_mutations, self.active_mutations)
        time.sleep(0.002)

    def _exit(self):
        with self.lock:
            self.active_mutations -= 1

    def record_decision(self, **kwargs):
        self._enter()
        try:
            self.n += 1
            self.calls.append(("decision_created", kwargs))
            return {"decision_id": f"dec_{self.n}"}
        finally:
            self._exit()

    def add_event(self, decision_id, event_type, payload, actor_type="system", actor_id="loopgrid", **kwargs):
        self._enter()
        try:
            self.calls.append((event_type, {"decision_id": decision_id, "payload": payload, "actor_type": actor_type, "actor_id": actor_id, **kwargs}))
            return {"event_type": event_type}
        finally:
            self._exit()

    def get_decision(self, decision_id):
        return {"summary": {"decision_id": decision_id}}

    def verify_workspace(self):
        return {"valid": True, "failures": []}

    def review(self, decision_id, action, reviewer, reason=""):
        self._enter()
        try:
            self.calls.append(("review", {"decision_id": decision_id, "action": action, "reviewer": reviewer, "reason": reason}))
            return {"ok": True}
        finally:
            self._exit()


class FakeModel:
    id = "test-model"
    provider = "test-provider"


class FakeAgent:
    agent_id = "agent-1"
    name = "Test Agno Agent"
    model = FakeModel()


def ctx(run_id="run-1", messages=None, metadata=None):
    return SimpleNamespace(
        run_id=run_id,
        session_id=f"session-{run_id}",
        messages=[] if messages is None else messages,
        metadata={} if metadata is None else metadata,
    )


def output(content="done", status="RunStatus.completed"):
    return SimpleNamespace(content=content, status=status, model="test-model", model_provider="test-provider")


def make_integration(grid=None, **kwargs):
    return LoopGridAgno(
        client=grid or FakeGrid(),
        authority={"scope": ["refund:create"], "environment": "sandbox"},
        policy={"policy_id": "p1", "version": "1", "decision": "auto_allowed", "reason": "test"},
        proposed_action={"tool": "sandbox_refund", "amount": 25, "currency": "USD"},
        **kwargs,
    )


def start(integration, run_context=None):
    rc = run_context or ctx()
    integration.pre_hook("refund $25", rc, agent=FakeAgent())
    return rc


def test_native_hook_bundle_is_small_and_explicit():
    evidence = make_integration()
    hooks = evidence.hooks()
    assert set(hooks) == {"pre_hooks", "post_hooks", "tool_hooks"}
    assert hooks["pre_hooks"] == [evidence.pre_hook]
    assert hooks["post_hooks"] == [evidence.post_hook]
    assert hooks["tool_hooks"] == [evidence.tool_hook]


def test_async_hook_bundle_uses_async_tool_hook():
    evidence = make_integration()
    assert evidence.async_hooks()["tool_hooks"] == [evidence.async_tool_hook]


def test_pre_hook_uses_agno_run_id_and_creates_decision():
    grid = FakeGrid()
    evidence = make_integration(grid)
    rc = start(evidence)
    decision = grid.calls[0][1]
    assert decision["idempotency_key"] == "agno:run-1:decision"
    assert decision["metadata"]["source"] == "loopgrid-agno"
    assert rc.metadata["loopgrid"]["decision_id"] == "dec_1"


def test_missing_run_id_is_not_invented():
    evidence = make_integration()
    with pytest.raises(ValueError):
        evidence.pre_hook("x", SimpleNamespace(run_id=None, session_id="s", messages=[], metadata={}), agent=FakeAgent())


def test_privacy_default_hashes_input_without_content():
    grid = FakeGrid()
    evidence = make_integration(grid)
    start(evidence)
    inp = grid.calls[0][1]["input"]
    assert inp["input_sha256"]
    assert "input" not in inp


def test_capture_content_is_explicit_opt_in():
    grid = FakeGrid()
    evidence = make_integration(grid, capture_content=True)
    start(evidence)
    assert grid.calls[0][1]["input"]["input"] == "refund $25"


def test_run_context_loopgrid_overrides_defaults():
    grid = FakeGrid()
    evidence = make_integration(grid)
    rc = ctx(metadata={"loopgrid": {"authority": {"limit_usd": 50}, "context": {"case": "A-1"}}})
    start(evidence, rc)
    decision = grid.calls[0][1]
    assert decision["authority"]["limit_usd"] == 50
    assert decision["context"]["application"]["case"] == "A-1"


def test_sync_tool_hook_maps_model_policy_request_execution_then_final_model():
    grid = FakeGrid()
    evidence = make_integration(grid)
    rc = start(evidence, ctx(messages=[{"role": "assistant", "tool_calls": [{"name": "sandbox_refund"}]}]))
    called = []

    def refund(amount, currency):
        called.append((amount, currency))
        return {"status": "succeeded", "sandbox": True, "real_money_moved": False}

    result = evidence.tool_hook("sandbox_refund", refund, {"amount": 25, "currency": "USD"}, rc, agent=FakeAgent())
    evidence.post_hook(output(), rc, agent=FakeAgent())
    evidence.observe_outcome("run-1", {"status": "succeeded", "sandbox": True, "real_money_moved": False})

    assert called == [(25, "USD")]
    assert result["status"] == "succeeded"
    assert [n for n, _ in grid.calls] == [
        "decision_created",
        "model_completed",
        "policy_evaluated",
        "tool_requested",
        "tool_executed",
        "model_completed",
        "outcome_observed",
    ]


def test_tool_is_invoked_exactly_once():
    evidence = make_integration()
    rc = start(evidence)
    count = {"n": 0}
    def f():
        count["n"] += 1
        return "ok"
    assert evidence.tool_hook("f", f, {}, rc, agent=FakeAgent()) == "ok"
    assert count["n"] == 1


def test_failed_tool_records_tool_result_and_reraises_without_raw_error():
    grid = FakeGrid()
    evidence = make_integration(grid)
    rc = start(evidence)
    def boom():
        raise RuntimeError("private failure text")
    with pytest.raises(RuntimeError):
        evidence.tool_hook("boom", boom, {}, rc, agent=FakeAgent())
    assert grid.calls[-1][0] == "tool_result"
    payload = grid.calls[-1][1]["payload"]
    assert payload["status"] == "failed"
    assert payload["error_type"] == "RuntimeError"
    assert "private failure text" not in str(payload)


def test_tool_ids_are_run_scoped_and_unique():
    grid = FakeGrid()
    evidence = make_integration(grid)
    rc = start(evidence)
    evidence.tool_hook("a", lambda: 1, {}, rc, agent=FakeAgent())
    evidence.tool_hook("a", lambda: 2, {}, rc, agent=FakeAgent())
    reqs = [v["payload"] for n, v in grid.calls if n == "tool_requested"]
    assert reqs[0]["loopgrid_tool_call_id"] == "lg-run-1-1"
    assert reqs[1]["loopgrid_tool_call_id"] == "lg-run-1-2"


def test_global_hook_does_not_claim_native_call_id():
    grid = FakeGrid()
    evidence = make_integration(grid)
    rc = start(evidence)
    evidence.tool_hook("a", lambda: 1, {}, rc, agent=FakeAgent())
    req = next(v["payload"] for n, v in grid.calls if n == "tool_requested")
    assert req["native_call_id_exposed_by_global_hook"] is False


def test_policy_is_explicit_and_recorded_once():
    grid = FakeGrid()
    evidence = LoopGridAgno(client=grid, authority={"scope": ["x"]})
    rc = start(evidence)
    evidence.record_policy("run-1", {"policy_id": "later", "version": "1", "decision": "auto_allowed"})
    with pytest.raises(RuntimeError):
        evidence.record_policy("run-1", {"policy_id": "later", "version": "1", "decision": "auto_allowed"})


def test_invalid_policy_is_rejected():
    evidence = LoopGridAgno(client=FakeGrid())
    start(evidence)
    with pytest.raises(ValueError):
        evidence.record_policy("run-1", {"policy_id": "x", "decision": "invented"})


def test_review_uses_loopgrid_review_endpoint():
    grid = FakeGrid()
    evidence = make_integration(grid)
    start(evidence)
    evidence.review_run("run-1", "approve", "reviewer@example.com", "approved")
    assert grid.calls[-1][0] == "review"


def test_external_execution_is_explicit_not_inferred():
    grid = FakeGrid()
    evidence = make_integration(grid)
    start(evidence)
    evidence.record_action_executed("run-1", {"tool": "external_charge", "status": "confirmed"})
    assert grid.calls[-1][0] == "tool_executed"
    assert grid.calls[-1][1]["payload"]["execution_source"] == "application_confirmed"


def test_paused_run_does_not_invent_completion():
    grid = FakeGrid()
    evidence = make_integration(grid)
    rc = start(evidence)
    evidence.post_hook(output(status="RunStatus.paused"), rc, agent=FakeAgent())
    assert [n for n, _ in grid.calls] == ["decision_created"]


def test_cancelled_run_records_incident_not_model_completion():
    grid = FakeGrid()
    evidence = make_integration(grid)
    rc = start(evidence)
    evidence.post_hook(SimpleNamespace(content=None, status="RunStatus.cancelled", error="private"), rc, agent=FakeAgent())
    assert grid.calls[-1][0] == "incident_flagged"
    assert "private" not in str(grid.calls[-1][1]["payload"])


def test_fail_open_executes_tool_when_loopgrid_is_unavailable():
    class BrokenGrid(FakeGrid):
        def record_decision(self, **kwargs):
            raise RuntimeError("down")
    evidence = LoopGridAgno(client=BrokenGrid(), fail_open=True)
    rc = ctx()
    evidence.pre_hook("x", rc, agent=FakeAgent())
    assert evidence.tool_hook("echo", lambda value: value, {"value": 7}, rc, agent=FakeAgent()) == 7


def test_mutations_are_serialized_across_concurrent_tools():
    grid = FakeGrid()
    evidence = make_integration(grid)
    rc = start(evidence)

    threads = [
        threading.Thread(target=lambda i=i: evidence.tool_hook(f"t{i}", lambda: i, {}, rc, agent=FakeAgent()))
        for i in range(6)
    ]
    for t in threads: t.start()
    for t in threads: t.join()
    assert grid.max_active_mutations == 1


@pytest.mark.asyncio
async def test_async_tool_hook_supports_async_tool():
    grid = FakeGrid()
    evidence = make_integration(grid)
    rc = start(evidence)
    async def afunc(x):
        await asyncio.sleep(0)
        return {"x": x}
    result = await evidence.async_tool_hook("afunc", afunc, {"x": 9}, rc, agent=FakeAgent())
    assert result == {"x": 9}
    assert grid.calls[-1][0] == "tool_executed"


def test_snapshot_reports_run_evidence_state():
    evidence = make_integration()
    rc = start(evidence)
    evidence.tool_hook("x", lambda: "ok", {}, rc, agent=FakeAgent())
    evidence.post_hook(output(), rc, agent=FakeAgent())
    snap = evidence.snapshot_for("run-1")
    assert snap is not None
    assert snap.tool_calls == 1
    assert snap.model_turns == 2
    assert snap.final_output_sha256
    assert snap.last_tool_result_sha256
