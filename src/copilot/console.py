"""Human-readable rendering of timeline events for terminal scripts."""

from __future__ import annotations

import json
from typing import Any

from copilot.graph.common import preview


def print_event(event: dict[str, Any]) -> None:
    t = event.get("type")
    who = event.get("agent", "")
    if event.get("label"):
        who = f"{who}<{preview(event['label'], 40)}>"
    if t == "route":
        print(f"\n== supervisor -> {event['to']}: {event['reason']}")
    elif t == "agent_step":
        if event.get("thought"):
            print(f"[{who}] {preview(event['thought'], 220)}")
        for call in event["tool_calls"]:
            print(f"[{who}] -> {call['tool']}({json.dumps(call['args'])})")
    elif t == "tool_result":
        print(f"[{who}] <- {event['tool']}: {preview(event['preview'], 160)}")
    elif t == "tool_error":
        print(f"[{who}] !! {event['tool']}: {event['error']} (retrying)")
    elif t == "hypotheses":
        for h in event["hypotheses"]:
            print(f"   hypothesis {h['id']} [{h['category']}]: {h['title']}")
    elif t == "agent_result":
        r = event["result"]
        if who.startswith("investigator"):
            print(f"[{who}] verdict: {r['verdict']}")
        elif who == "triage":
            print(f"[triage] actionable={r['actionable']} severity={r['severity']}: {preview(r['summary'], 200)}")
        elif who == "diagnosis":
            print(
                f"[diagnosis] {r['category']} ({r['confidence']}), culprit={r['culprit_deployment']}: "
                f"{preview(r['root_cause'], 200)}"
            )
        elif who == "remediation":
            print(f"[remediation] action={json.dumps(r['action'])}")
    elif t == "memory":
        print(f"[memory] loaded {event['kind']} {event.get('category') or event.get('count')}")
    elif t == "approval":
        print(f"[gate] auto-approved by policy ({', '.join(event['reasons'])})")
    elif t == "execution":
        print(f"[executor] {event['status']} {preview(event.get('result') or event.get('error') or '', 160)}")
    elif t == "verification":
        print(f"[verify] resolved={event['resolved']}: {'; '.join(event['checks'])}")
    elif t == "token":
        print(event["text"], end="", flush=True)
    elif t == "status":
        print(f"\n\n== final status: {event['status']}")
