from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import importlib.metadata
import inspect
import json
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

try:
    from loopgrid import LoopGrid
except Exception:  # pragma: no cover - dependency is required in real installs
    LoopGrid = None  # type: ignore[assignment]

_JSON_SCALARS = (str, int, float, bool, type(None))
_ALLOWED_POLICY_DECISIONS = {"auto_allowed", "human_approval_required", "blocked", "block"}


def _jsonable(value: Any) -> Any:
    if isinstance(value, _JSON_SCALARS):
        return value
    if isinstance(value, bytes):
        return {"type": "bytes", "length": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if dataclasses.is_dataclass(value):
        try:
            return _jsonable(dataclasses.asdict(value))
        except Exception:
            pass
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _jsonable(model_dump(mode="json"))
        except TypeError:
            return _jsonable(model_dump())
        except Exception:
            pass
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _jsonable(to_dict())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        try:
            data = {k: v for k, v in vars(value).items() if not str(k).startswith("_")}
            if data:
                return _jsonable(data)
        except Exception:
            pass
    return {"type": type(value).__name__, "repr": repr(value)[:500]}


def _sha256(value: Any) -> str:
    raw = json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _agno_version() -> str:
    try:
        return importlib.metadata.version("agno")
    except Exception:
        return "unknown"


def _read(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _model_provenance(actor: Any, run_output: Any = None) -> dict[str, Any]:
    provider = _read(run_output, "model_provider")
    model_name = _read(run_output, "model")
    model = _read(actor, "model")

    if not model_name:
        if isinstance(model, str):
            if ":" in model:
                possible_provider, possible_model = model.split(":", 1)
                provider = provider or possible_provider
                model_name = possible_model
            else:
                model_name = model
        else:
            model_name = _read(model, "id") or _read(model, "model") or _read(model, "name")
            provider = provider or _read(model, "provider")

    if not provider and model is not None:
        module = type(model).__module__
        if "agno.models." in module:
            provider = module.split("agno.models.", 1)[1].split(".", 1)[0]
    return {
        "provider": str(provider or "custom"),
        "name": str(model_name or type(model).__name__ if model is not None else "unknown"),
    }


def _policy_payload(policy: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(_jsonable(dict(policy)))
    decision = payload.get("decision")
    if decision == "block":
        payload["decision"] = "blocked"
        decision = "blocked"
    if decision not in _ALLOWED_POLICY_DECISIONS:
        raise ValueError(
            "LoopGrid policy evidence requires decision to be one of: "
            "auto_allowed, human_approval_required, blocked"
        )
    if not (payload.get("policy_id") or payload.get("version")):
        raise ValueError("LoopGrid policy evidence should include policy_id or version")
    return payload


@dataclass
class _RunState:
    decision_id: str
    run_id: str
    actor_id: str
    actor_name: str
    actor_kind: str
    model: dict[str, Any]
    policy: dict[str, Any] = field(default_factory=dict)
    policy_recorded: bool = False
    model_turn: int = 0
    tool_counter: int = 0
    last_model_observation_sha256: str | None = None
    last_tool_result_sha256: str | None = None
    final_output_sha256: str | None = None


@dataclass(frozen=True)
class LoopGridAgnoSnapshot:
    run_id: str
    decision_id: str
    actor_kind: str
    model_turns: int
    tool_calls: int
    final_output_sha256: str | None = None
    last_tool_result_sha256: str | None = None


class LoopGridAgno:
    """Native Agno hooks for signed, independently verifiable LoopGrid evidence.

    Supported native surfaces in Agno 3.x:
      * ``pre_hooks``: create/bind the decision at run start
      * ``tool_hooks``: wrap the actual tool continuation before and after execution
      * ``post_hooks``: capture the accepted terminal model/run output

    LoopGrid does not decide whether the business action should happen and does not
    independently initiate external side effects. The tool hook calls Agno's supplied
    continuation callback because that is Agno's middleware contract.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        workspace_id: str | None = None,
        service_name: str = "agno",
        agent_version: str = "0.1.0",
        decision_type: str = "agno_agent_run",
        privacy_mode: str = "redacted",
        authority: Mapping[str, Any] | None = None,
        policy: Mapping[str, Any] | None = None,
        proposed_action: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
        capture_content: bool = False,
        fail_open: bool = False,
        client: Any | None = None,
    ) -> None:
        base_url = base_url or os.getenv("LOOPGRID_BASE_URL", "http://127.0.0.1:8000")
        api_key = api_key or os.getenv("LOOPGRID_API_KEY")
        workspace_id = workspace_id or os.getenv("LOOPGRID_WORKSPACE_ID", "default")
        if client is None:
            if LoopGrid is None:
                raise RuntimeError("The 'loopgrid' package is required unless a client is supplied")
            client = LoopGrid(base_url=base_url, api_key=api_key, workspace_id=workspace_id)

        self.grid = client
        self.workspace_id = workspace_id
        self.service_name = service_name
        self.agent_version = agent_version
        self.decision_type = decision_type
        self.privacy_mode = privacy_mode
        self.authority = dict(authority or {})
        self.policy = dict(policy or {})
        self.proposed_action = dict(proposed_action or {})
        self.context = dict(context or {})
        self.capture_content = bool(capture_content)
        self.fail_open = bool(fail_open)
        self._runs: dict[str, _RunState] = {}
        self._state_lock = threading.RLock()
        # LoopGrid chains are workspace-scoped. Serialize mutations made by this
        # integration instance so concurrent Agno tool hooks cannot race chain heads.
        self._mutation_lock = threading.RLock()

    def hooks(self) -> dict[str, list[Callable[..., Any]]]:
        """Return kwargs for a synchronous ``Agent`` or ``Team``."""
        return {
            "pre_hooks": [self.pre_hook],
            "post_hooks": [self.post_hook],
            "tool_hooks": [self.tool_hook],
        }

    def async_hooks(self) -> dict[str, list[Callable[..., Any]]]:
        """Return kwargs for ``Agent.arun`` / ``Team.arun`` with async tools."""
        return {
            "pre_hooks": [self.pre_hook],
            "post_hooks": [self.post_hook],
            "tool_hooks": [self.async_tool_hook],
        }

    def _call(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception:
            if self.fail_open:
                return None
            raise

    def _mutate(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        try:
            with self._mutation_lock:
                return fn(*args, **kwargs)
        except Exception:
            if self.fail_open:
                return None
            raise

    def _run_id(self, run_context: Any) -> str:
        run_id = str(_read(run_context, "run_id") or "").strip()
        if not run_id:
            raise ValueError("Agno RunContext did not provide run_id; LoopGrid will not invent correlation")
        return run_id

    def _state(self, run_id: str) -> _RunState:
        with self._state_lock:
            state = self._runs.get(str(run_id))
        if state is None:
            raise KeyError(f"No LoopGrid decision recorded for Agno run {run_id!r}")
        return state

    def decision_id_for(self, run_id: str) -> str | None:
        with self._state_lock:
            state = self._runs.get(str(run_id))
            return state.decision_id if state else None

    def snapshot_for(self, run_id: str) -> LoopGridAgnoSnapshot | None:
        with self._state_lock:
            state = self._runs.get(str(run_id))
            if state is None:
                return None
            return LoopGridAgnoSnapshot(
                run_id=state.run_id,
                decision_id=state.decision_id,
                actor_kind=state.actor_kind,
                model_turns=state.model_turn,
                tool_calls=state.tool_counter,
                final_output_sha256=state.final_output_sha256,
                last_tool_result_sha256=state.last_tool_result_sha256,
            )

    def get_decision(self, run_id: str) -> dict[str, Any] | None:
        decision_id = self.decision_id_for(run_id)
        return self._call(self.grid.get_decision, decision_id) if decision_id else None

    def verify_workspace(self) -> dict[str, Any] | None:
        return self._call(self.grid.verify_workspace)

    def review_run(self, run_id: str, action: str, reviewer: str, reason: str = "") -> dict[str, Any] | None:
        if action not in {"approve", "reject"}:
            raise ValueError("action must be 'approve' or 'reject'")
        state = self._state(run_id)
        return self._mutate(self.grid.review, state.decision_id, action, reviewer, reason)

    def record_policy(self, run_id: str, policy: Mapping[str, Any]) -> dict[str, Any] | None:
        state = self._state(run_id)
        payload = _policy_payload(policy)
        with self._state_lock:
            if state.policy_recorded:
                raise RuntimeError("Production policy evidence is already recorded for this Agno run")
            state.policy = dict(payload)
        result = self._mutate(
            self.grid.add_event,
            state.decision_id,
            "policy_evaluated",
            payload,
            "policy",
            str(payload.get("policy_id") or "application-policy"),
            idempotency_key=f"agno:{run_id}:policy",
        )
        if result is not None or not self.fail_open:
            with self._state_lock:
                state.policy_recorded = True
        return result

    def record_action_executed(
        self,
        run_id: str,
        action: Mapping[str, Any] | Any,
        *,
        actor_id: str = "application",
        idempotency_key: str | None = None,
    ) -> dict[str, Any] | None:
        """Explicit evidence for actions executed outside Agno's normal tool continuation.

        Use this for ``external_execution=True`` tools or application-owned action
        boundaries. Do not call it merely because a model requested a tool.
        """
        state = self._state(run_id)
        projected = _jsonable(action)
        payload = dict(projected) if isinstance(projected, dict) else {"value": projected}
        payload.setdefault("framework", "agno")
        payload.setdefault("run_id", run_id)
        payload.setdefault("execution_source", "application_confirmed")
        return self._mutate(
            self.grid.add_event,
            state.decision_id,
            "tool_executed",
            payload,
            "system",
            actor_id,
            idempotency_key=idempotency_key or f"agno:{run_id}:external-action:{_sha256(payload)[:24]}",
        )

    def observe_outcome(
        self,
        run_id: str,
        outcome: Mapping[str, Any] | Any,
        *,
        observer_id: str = "application",
        idempotency_key: str | None = None,
    ) -> dict[str, Any] | None:
        state = self._state(run_id)
        projected = _jsonable(outcome)
        payload = dict(projected) if isinstance(projected, dict) else {"value": projected}
        payload.setdefault("framework", "agno")
        payload.setdefault("run_id", run_id)
        if state.final_output_sha256:
            payload.setdefault("agent_output_sha256", state.final_output_sha256)
        if state.last_tool_result_sha256:
            payload.setdefault("last_tool_result_sha256", state.last_tool_result_sha256)
        return self._mutate(
            self.grid.add_event,
            state.decision_id,
            "outcome_observed",
            payload,
            "system",
            observer_id,
            idempotency_key=idempotency_key or f"agno:{run_id}:outcome:{_sha256(payload)[:24]}",
        )

    def _ensure_policy(self, state: _RunState) -> None:
        if state.policy and not state.policy_recorded:
            self.record_policy(state.run_id, state.policy)

    def _context_overrides(self, run_context: Any) -> dict[str, Any]:
        metadata = _read(run_context, "metadata")
        if isinstance(metadata, Mapping):
            lg = metadata.get("loopgrid")
            if isinstance(lg, Mapping):
                return dict(lg)
        return {}

    def _stash_decision_id(self, run_context: Any, decision_id: str) -> None:
        try:
            metadata = _read(run_context, "metadata")
            if metadata is None:
                metadata = {}
                setattr(run_context, "metadata", metadata)
            if isinstance(metadata, dict):
                lg = metadata.setdefault("loopgrid", {})
                if isinstance(lg, dict):
                    lg["decision_id"] = decision_id
        except Exception:
            # The in-memory run map remains authoritative for this integration.
            pass

    def _actor(self, agent: Any = None, team: Any = None) -> tuple[Any, str, str, str]:
        actor = team if team is not None else agent
        kind = "team" if team is not None else "agent"
        actor_id = str(
            _read(actor, "team_id")
            or _read(actor, "agent_id")
            or _read(actor, "id")
            or _read(actor, "name")
            or f"agno-{kind}"
        )
        actor_name = str(_read(actor, "name") or actor_id)
        return actor, actor_id, actor_name, kind

    def pre_hook(
        self,
        run_input: Any,
        run_context: Any,
        agent: Any = None,
        team: Any = None,
        metadata: Any = None,
        **_: Any,
    ) -> None:
        run_id = self._run_id(run_context)
        overrides = self._context_overrides(run_context)
        authority = dict(self.authority)
        authority.update(dict(overrides.get("authority") or {}))
        policy = dict(self.policy)
        policy.update(dict(overrides.get("policy") or {}))
        proposed_action = dict(self.proposed_action)
        proposed_action.update(dict(overrides.get("proposed_action") or {}))
        app_context = dict(self.context)
        app_context.update(dict(overrides.get("context") or {}))

        actor, actor_id, actor_name, actor_kind = self._actor(agent=agent, team=team)
        model = _model_provenance(actor)
        input_projection = _jsonable(run_input)
        input_evidence: dict[str, Any] = {"input_sha256": _sha256(input_projection)}
        if isinstance(input_projection, list):
            input_evidence["item_count"] = len(input_projection)
        if self.capture_content:
            input_evidence["input"] = input_projection

        context = {
            "framework": "agno",
            "framework_version": _agno_version(),
            "run_id": run_id,
            "session_id": str(_read(run_context, "session_id") or ""),
            "actor_kind": actor_kind,
            "actor_name": actor_name,
            "capture_content": self.capture_content,
            "application": app_context,
        }
        result = self._mutate(
            self.grid.record_decision,
            decision_type=self.decision_type,
            service_name=self.service_name,
            workspace_id=self.workspace_id,
            privacy_mode=self.privacy_mode,
            idempotency_key=f"agno:{run_id}:decision",
            agent={"id": actor_id, "name": actor_name, "version": self.agent_version},
            authority=authority,
            model=model,
            context=context,
            input=input_evidence,
            proposed_action=proposed_action,
            metadata={
                "source": "loopgrid-agno",
                "integration_version": "0.1.0",
                "capture_content": self.capture_content,
                "loopgrid_executes_external_action": False,
                "agno_tool_hook_semantics": "wraps_framework_tool_continuation",
            },
        )
        if result is None:
            return
        decision_id = str(result["decision_id"])
        with self._state_lock:
            self._runs[run_id] = _RunState(
                decision_id=decision_id,
                run_id=run_id,
                actor_id=actor_id,
                actor_name=actor_name,
                actor_kind=actor_kind,
                model=model,
                policy=policy,
            )
        self._stash_decision_id(run_context, decision_id)

    def _record_model_boundary(self, state: _RunState, run_context: Any, *, boundary: str) -> None:
        messages = _jsonable(_read(run_context, "messages") or [])
        observation_hash = _sha256(messages)
        with self._state_lock:
            if state.last_model_observation_sha256 == observation_hash:
                return
            # If no messages are exposed, only record one inferred completion boundary
            # before tools. Reaching the tool hook proves a model tool request was accepted,
            # but we do not invent model text/content.
            if messages == [] and state.last_model_observation_sha256 is not None:
                return
            state.model_turn += 1
            turn = state.model_turn
            state.last_model_observation_sha256 = observation_hash
        payload: dict[str, Any] = {
            "framework": "agno",
            "run_id": state.run_id,
            "model_turn": turn,
            "model": state.model,
            "completion_boundary": boundary,
            "messages_sha256": observation_hash,
            "message_count": len(messages) if isinstance(messages, list) else None,
        }
        if self.capture_content:
            payload["messages"] = messages
        self._mutate(
            self.grid.add_event,
            state.decision_id,
            "model_completed",
            payload,
            state.actor_kind,
            state.actor_id,
            idempotency_key=f"agno:{state.run_id}:model:{turn}",
        )

    def _next_tool_id(self, state: _RunState, function_name: str, arguments: Mapping[str, Any]) -> tuple[str, int]:
        with self._state_lock:
            state.tool_counter += 1
            counter = state.tool_counter
        # Agno's global tool_hooks middleware exposes run_context/name/function/args,
        # but not FunctionCall.call_id. Use an explicitly LoopGrid-owned run-scoped id.
        tool_id = f"lg-{state.run_id}-{counter}"
        return tool_id, counter

    def _before_tool(self, state: _RunState, run_context: Any, function_name: str, arguments: Mapping[str, Any]) -> tuple[str, int, str]:
        self._record_model_boundary(state, run_context, boundary="tool_call_ready")
        self._ensure_policy(state)
        tool_id, counter = self._next_tool_id(state, function_name, arguments)
        args_projection = _jsonable(dict(arguments))
        args_hash = _sha256(args_projection)
        payload: dict[str, Any] = {
            "framework": "agno",
            "run_id": state.run_id,
            "tool": function_name,
            "loopgrid_tool_call_id": tool_id,
            "call_index": counter,
            "input_sha256": args_hash,
            "native_call_id_exposed_by_global_hook": False,
        }
        if self.capture_content:
            payload["input"] = args_projection
        self._mutate(
            self.grid.add_event,
            state.decision_id,
            "tool_requested",
            payload,
            state.actor_kind,
            state.actor_id,
            idempotency_key=f"agno:{state.run_id}:tool:{counter}:requested",
        )
        return tool_id, counter, args_hash

    def _tool_success(self, state: _RunState, function_name: str, tool_id: str, counter: int, result: Any) -> Any:
        projected = _jsonable(result)
        result_hash = _sha256(projected)
        with self._state_lock:
            state.last_tool_result_sha256 = result_hash
        payload: dict[str, Any] = {
            "framework": "agno",
            "run_id": state.run_id,
            "tool": function_name,
            "loopgrid_tool_call_id": tool_id,
            "call_index": counter,
            "status": "executed",
            "result_sha256": result_hash,
            "executor": "agno-tool-hook-continuation",
        }
        if self.capture_content:
            payload["result"] = projected
        self._mutate(
            self.grid.add_event,
            state.decision_id,
            "tool_executed",
            payload,
            "tool",
            function_name,
            idempotency_key=f"agno:{state.run_id}:tool:{counter}:executed",
        )
        return result

    def _tool_failure(self, state: _RunState, function_name: str, tool_id: str, counter: int, exc: BaseException) -> None:
        payload = {
            "framework": "agno",
            "run_id": state.run_id,
            "tool": function_name,
            "loopgrid_tool_call_id": tool_id,
            "call_index": counter,
            "status": "failed",
            "error_type": type(exc).__name__,
            "error_sha256": _sha256(str(exc)),
        }
        self._mutate(
            self.grid.add_event,
            state.decision_id,
            "tool_result",
            payload,
            "tool",
            function_name,
            idempotency_key=f"agno:{state.run_id}:tool:{counter}:failed",
        )

    def tool_hook(
        self,
        function_name: str,
        function: Callable[..., Any],
        arguments: dict[str, Any],
        run_context: Any,
        agent: Any = None,
        team: Any = None,
    ) -> Any:
        """Synchronous Agno tool middleware."""
        try:
            run_id = self._run_id(run_context)
            state = self._state(run_id)
            tool_id, counter, _ = self._before_tool(state, run_context, str(function_name), arguments or {})
        except Exception:
            if not self.fail_open:
                raise
            return function(**(arguments or {}))

        try:
            result = function(**(arguments or {}))
            if inspect.isawaitable(result):
                raise RuntimeError("Async Agno tool used with LoopGridAgno.tool_hook; use async_hooks()")
        except Exception as exc:
            self._tool_failure(state, str(function_name), tool_id, counter, exc)
            raise
        return self._tool_success(state, str(function_name), tool_id, counter, result)

    async def async_tool_hook(
        self,
        function_name: str,
        function: Callable[..., Any],
        arguments: dict[str, Any],
        run_context: Any,
        agent: Any = None,
        team: Any = None,
    ) -> Any:
        """Asynchronous Agno tool middleware; supports sync and async tool continuations."""
        try:
            run_id = self._run_id(run_context)
            state = self._state(run_id)
            tool_id, counter, _ = await asyncio.to_thread(
                self._before_tool, state, run_context, str(function_name), arguments or {}
            )
        except Exception:
            if not self.fail_open:
                raise
            result = function(**(arguments or {}))
            return await result if inspect.isawaitable(result) else result

        try:
            result = function(**(arguments or {}))
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            await asyncio.to_thread(self._tool_failure, state, str(function_name), tool_id, counter, exc)
            raise
        return await asyncio.to_thread(self._tool_success, state, str(function_name), tool_id, counter, result)

    def post_hook(
        self,
        run_output: Any,
        run_context: Any,
        agent: Any = None,
        team: Any = None,
        **_: Any,
    ) -> None:
        run_id = self._run_id(run_context)
        state = self._state(run_id)
        status = str(_read(run_output, "status") or "").lower()
        if "error" in status or "cancel" in status:
            payload = {
                "framework": "agno",
                "run_id": run_id,
                "stage": "run",
                "status": status or "error",
                "error_sha256": _sha256(_read(run_output, "error") or status or "run failed"),
            }
            self._mutate(
                self.grid.add_event,
                state.decision_id,
                "incident_flagged",
                payload,
                "integration",
                "loopgrid-agno",
                idempotency_key=f"agno:{run_id}:run-error:{_sha256(payload)[:24]}",
            )
            return
        if "paused" in status:
            # Paused means Agno still awaits confirmation/user input/external execution.
            # Do not invent completion, approval, or action evidence.
            return

        output_projection = _jsonable(_read(run_output, "content"))
        output_hash = _sha256(output_projection)
        model = _model_provenance(team if team is not None else agent, run_output)
        with self._state_lock:
            state.model = model
            state.model_turn += 1
            turn = state.model_turn
            state.final_output_sha256 = output_hash
        payload: dict[str, Any] = {
            "framework": "agno",
            "run_id": run_id,
            "model_turn": turn,
            "model": model,
            "completion_boundary": "run_output",
            "output_sha256": output_hash,
            "status": status or "completed",
        }
        if self.capture_content:
            payload["output"] = output_projection
        self._mutate(
            self.grid.add_event,
            state.decision_id,
            "model_completed",
            payload,
            state.actor_kind,
            state.actor_id,
            idempotency_key=f"agno:{run_id}:model:{turn}",
        )
