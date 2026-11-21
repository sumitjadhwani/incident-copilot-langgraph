"""End-to-end smoke test against the running docker-compose stack (real Keycloak, real LLM).

    uv run python scripts/smoke_e2e.py [--scenario oomkilled_loop]

Logs in as alice (sre, acme), bob (sre-approver, acme) and carol (sre-approver, globex) through the
dev-only `copilot-cli` client, then checks: alice starts an incident; carol cannot see or approve it
(404); alice cannot approve (403); a tampered hash is refused (409); bob approves; the incident
resolves; the audit chain verifies; a second approval is refused (409).
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time

import httpx

KEYCLOAK = "http://localhost:8080/realms/incident-copilot/protocol/openid-connect/token"
API = "http://localhost:8000"


def login(user: str) -> str:
    r = httpx.post(
        KEYCLOAK,
        data={
            "grant_type": "password",
            "client_id": "copilot-cli",
            "username": user,
            "password": user,
            "scope": "openid",
        },
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def claims(token: str) -> dict:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' - ' + detail) if detail else ''}")
    if not ok:
        sys.exit(1)


def wait_for(client: httpx.Client, thread_id: str, token: str, until: set[str], timeout: float = 600) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        inc = client.get(f"/api/incidents/{thread_id}", headers={"Authorization": f"Bearer {token}"}).json()
        if inc["thread"]["status"] in until and not inc["running"]:
            return inc
        time.sleep(2)
    raise TimeoutError(f"incident did not reach {until}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="oomkilled_loop")
    args = parser.parse_args()

    alice, bob, carol = login("alice"), login("bob"), login("carol")
    c = claims(bob)
    print(
        f"token claims (bob): sub={c['sub'][:8]}... tenant={c.get('tenant')} groups={c.get('groups')} "
        f"roles={[r for r in c['realm_access']['roles'] if r.startswith('sre')]} aud={c['aud']}"
    )
    h = lambda t: {"Authorization": f"Bearer {t}"}  # noqa: E731

    with httpx.Client(base_url=API, timeout=30) as client:
        check("no token is rejected", client.get("/api/me").status_code == 401)
        me = client.get("/api/me", headers=h(alice)).json()
        check("alice is resolved from the JWT", me["tenant_id"] == "acme" and not me["can_approve"], str(me))

        r = client.post("/api/incidents", json={"scenario_id": args.scenario}, headers=h(alice))
        check("alice starts an incident", r.status_code == 201, r.text)
        thread_id = r.json()["thread_id"]
        print(f"  ... investigating {thread_id} (real LLM, takes a minute or two)")
        inc = wait_for(client, thread_id, alice, {"awaiting_approval", "completed", "failed"})
        check("run pauses for approval", inc["thread"]["status"] == "awaiting_approval", inc["thread"]["status"])
        pending = inc["pending_approval"]
        print(f"  proposed: {json.dumps(pending['action'])} (risk={pending['risk']})")
        body = {"decision": "approve", "action_hash": pending["action_hash"], "comment": "smoke test"}
        url = f"/api/incidents/{thread_id}/approvals"

        check(
            "carol (other tenant) cannot see it",
            client.get(f"/api/incidents/{thread_id}", headers=h(carol)).status_code == 404,
        )
        check("carol cannot approve it", client.post(url, json=body, headers=h(carol)).status_code == 404)
        check(
            "alice (no approver role) cannot approve", client.post(url, json=body, headers=h(alice)).status_code == 403
        )
        bad = {**body, "action_hash": "0" * 64}
        check("tampered action hash is refused", client.post(url, json=bad, headers=h(bob)).status_code == 409)
        r = client.post(url, json=body, headers=h(bob))
        check("bob approves", r.status_code == 202, r.text)

        inc = wait_for(client, thread_id, bob, {"completed", "failed"})
        state = inc["state"]
        check("incident resolved", state.get("status") == "resolved", str(state.get("status")))
        check("action executed as bob", (state.get("execution") or {}).get("executed_as") == "bob")
        check("second approval is refused", client.post(url, json=body, headers=h(bob)).status_code == 409)

        audit = client.get(f"/api/audit?thread_id={thread_id}", headers=h(alice)).json()
        check("audit rows approved -> executed", [r["status"] for r in audit["rows"]] == ["approved", "executed"])
        check("audit hash chain verifies", audit["chain"]["ok"], str(audit["chain"]))
    print(f"\nall checks passed. Open {API} and log in as alice / bob / carol to see it in the UI.")


if __name__ == "__main__":
    main()
