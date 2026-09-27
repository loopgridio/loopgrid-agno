from __future__ import annotations

import os
import uuid

from agno.agent import Agent
from agno.models.openai.like import OpenAILike

from loopgrid_agno import LoopGridAgno


def sandbox_refund(amount: float, currency: str) -> dict:
    """Return a sandbox-only refund result."""
    return {
        "status": "succeeded",
        "sandbox": True,
        "real_money_moved": False,
        "amount": amount,
        "currency": currency,
    }


def main() -> None:
    run_id = f"loopgrid-agno-{uuid.uuid4().hex[:12]}"
    evidence = LoopGridAgno(
        base_url=os.getenv("LOOPGRID_BASE_URL", "http://127.0.0.1:8000"),
        workspace_id=os.getenv("LOOPGRID_WORKSPACE_ID", "loopgrid-agno-e2e"),
        authority={"scope": ["refund:create"], "environment": "sandbox"},
        policy={
            "policy_id": "sandbox-refund-policy",
            "version": "1",
            "decision": "auto_allowed",
            "reason": "deterministic release validation",
        },
        proposed_action={"tool": "sandbox_refund", "amount": 25, "currency": "USD"},
    )

    model = OpenAILike(
        id="loopgrid-e2e",
        api_key="local-test-key",
        base_url="http://127.0.0.1:40124/v1",
    )
    agent = Agent(
        name="LoopGrid Agno E2E",
        model=model,
        tools=[sandbox_refund],
        instructions="Always use sandbox_refund with amount 25 USD, then report completion.",
        **evidence.hooks(),
    )

    response = agent.run("Run the deterministic LoopGrid Agno sandbox refund.", run_id=run_id)
    print("run_id:", run_id)
    print("agent_output:", response.content)
    print("decision_id:", evidence.decision_id_for(run_id))

    evidence.observe_outcome(
        run_id,
        {"status": "succeeded", "sandbox": True, "real_money_moved": False},
    )
    decision = evidence.get_decision(run_id)
    verification = evidence.verify_workspace()
    print("decision:", decision)
    print("workspace_verification:", verification)


if __name__ == "__main__":
    main()
