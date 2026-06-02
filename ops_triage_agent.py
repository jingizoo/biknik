"""
ops_triage_agent.py
-------------------
A minimal but complete *agentic* AI example using the Anthropic API.

What "agentic" means here: instead of a single request->response, we give the
model a GOAL and a set of TOOLS, then run a LOOP. On each turn the model decides
whether it has enough information to answer, or whether it needs to call a tool.
We execute the tool, feed the result back, and let it decide again. It drives
itself until the goal is met. That self-directed loop is the whole idea.

This example simulates a "Finance Platforms ops triage" agent. The tools are
mocked (in-memory data) so the script runs end-to-end with nothing but an API
key. In production you'd swap the tool *bodies* for real SQL queries, metrics
APIs, ticketing calls, etc. The agent loop itself does not change.

Run:
    pip install anthropic
    export ANTHROPIC_API_KEY=sk-ant-...
    python ops_triage_agent.py
"""

import json
import anthropic

client = anthropic.Anthropic()

MODEL = "claude-opus-4-8"   # swappable: claude-sonnet-4-6 is cheaper/faster
MAX_TURNS = 10              # hard guardrail so the agent can never loop forever


# ----------------------------------------------------------------------------
# 1. MOCK BACKEND  (in a real system these are your databases / metrics / APIs)
# ----------------------------------------------------------------------------

SERVICE_HEALTH = {
    "tubs-batch":   {"status": "degraded", "error_rate_pct": 14.2, "p95_ms": 8200},
    "ofam-pipeline":{"status": "healthy",  "error_rate_pct": 0.3,  "p95_ms": 410},
    "coupa-sync":   {"status": "healthy",  "error_rate_pct": 0.1,  "p95_ms": 220},
}

# Pretend this is a row set you'd get from a SQL query against a staging table.
RECENT_BATCH_RUNS = [
    {"run_id": "R-9001", "service": "tubs-batch", "rows": 1_240_000, "duration_s": 5400, "status": "FAILED"},
    {"run_id": "R-9000", "service": "tubs-batch", "rows": 1_180_000, "duration_s": 1900, "status": "OK"},
    {"run_id": "R-8999", "service": "tubs-batch", "rows": 1_205_000, "duration_s": 1850, "status": "OK"},
]


# ----------------------------------------------------------------------------
# 2. TOOL IMPLEMENTATIONS  (plain Python functions — the "hands" of the agent)
# ----------------------------------------------------------------------------

def get_service_health(service: str) -> dict:
    return SERVICE_HEALTH.get(service, {"error": f"unknown service '{service}'"})


def query_recent_runs(service: str, limit: int = 5) -> list:
    rows = [r for r in RECENT_BATCH_RUNS if r["service"] == service]
    return rows[:limit]


def create_incident_ticket(service: str, severity: str, summary: str) -> dict:
    # A SIDE-EFFECTING action. We never let the agent fire this unsupervised.
    print(f"\n  >>> Agent wants to OPEN A TICKET:")
    print(f"      service={service}  severity={severity}")
    print(f"      summary={summary}")
    approve = input("      Approve this action? [y/N] ").strip().lower()
    if approve != "y":
        return {"created": False, "reason": "human declined the action"}
    return {"created": True, "ticket_id": "INC-4471", "service": service}


# Map tool names -> functions so the loop can dispatch by name.
TOOL_IMPL = {
    "get_service_health": get_service_health,
    "query_recent_runs": query_recent_runs,
    "create_incident_ticket": create_incident_ticket,
}


# ----------------------------------------------------------------------------
# 3. TOOL SCHEMAS  (what the model SEES — name + description + input shape)
#    The descriptions are the model's only instructions for *when* to use each
#    tool, so write them like you're briefing a new analyst. Be specific.
# ----------------------------------------------------------------------------

TOOLS = [
    {
        "name": "get_service_health",
        "description": "Get current health (status, error rate, p95 latency) for a "
                       "single service. Use this first to see what is wrong.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Service name, e.g. 'tubs-batch'"}
            },
            "required": ["service"],
        },
    },
    {
        "name": "query_recent_runs",
        "description": "Return the most recent batch runs for a service, including "
                       "row counts, durations, and pass/fail status. Use this to "
                       "find anomalies such as a run that took far longer than usual.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "limit": {"type": "integer", "description": "Max rows to return (default 5)"},
            },
            "required": ["service"],
        },
    },
    {
        "name": "create_incident_ticket",
        "description": "Open an incident ticket. Only call this once you have "
                       "evidence of a real problem and can write a clear summary "
                       "with severity (SEV1/SEV2/SEV3). This is a real action.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "severity": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["service", "severity", "summary"],
        },
    },
]


# ----------------------------------------------------------------------------
# 4. THE AGENTIC LOOP  (this is the part that makes it an "agent")
# ----------------------------------------------------------------------------

SYSTEM = (
    "You are an SRE triage agent for a Finance Platforms team. Given an alert, "
    "investigate using the available tools, form a root-cause hypothesis from the "
    "evidence, and if a real incident exists, open a ticket. Be concise and "
    "evidence-driven. Do not open a ticket without supporting data."
)


def run_agent(goal: str):
    messages = [{"role": "user", "content": goal}]

    for turn in range(MAX_TURNS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        # Show any text the model produced this turn (its reasoning / answer).
        for block in response.content:
            if block.type == "text":
                print(f"\n[agent] {block.text}")

        # No tool requested -> the model is done. Exit the loop.
        if response.stop_reason != "tool_use":
            print("\n[done] Agent finished.")
            return

        # The model must keep its own turn in history *including* the tool_use
        # blocks, or the follow-up tool_result won't line up.
        messages.append({"role": "assistant", "content": response.content})

        # Execute every tool the model asked for this turn (it may batch several).
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"[tool ] {block.name}({json.dumps(block.input)})")
                fn = TOOL_IMPL[block.name]
                try:
                    result = fn(**block.input)
                    content, is_error = json.dumps(result), False
                except Exception as e:
                    content, is_error = f"Tool error: {e}", True
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,
                    "is_error": is_error,
                })

        # Feed results back as a user turn; loop continues, model decides next.
        messages.append({"role": "user", "content": tool_results})

    print("\n[halt] Hit MAX_TURNS guardrail without finishing.")


if __name__ == "__main__":
    run_agent("Alert fired: tubs-batch is degraded. Investigate and act.")
