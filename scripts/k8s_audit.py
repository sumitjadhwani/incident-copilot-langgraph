"""Show Kubernetes API server audit events made through the MCP servers (service account + impersonated human).

uv run python scripts/k8s_audit.py              # last 20 impersonated requests
uv run python scripts/k8s_audit.py --writes     # only writes (patch/update/create/delete)
uv run python scripts/k8s_audit.py --denied     # only refused requests (403)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

NODE = "incident-copilot-control-plane"
LOG = "/var/log/kubernetes/kube-apiserver-audit.log"
WRITE_VERBS = {"patch", "update", "create", "delete"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-n", type=int, default=20)
    parser.add_argument("--writes", action="store_true")
    parser.add_argument("--denied", action="store_true")
    args = parser.parse_args()

    raw = subprocess.run(["docker", "exec", NODE, "cat", LOG], capture_output=True, text=True, check=True).stdout
    events = []
    for line in raw.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "impersonatedUser" not in ev:
            continue
        if args.writes and ev.get("verb") not in WRITE_VERBS:
            continue
        code = (ev.get("responseStatus") or {}).get("code")
        if args.denied and code != 403:
            continue
        events.append(ev)

    print(f"{'time':<20} {'code':<4} {'verb':<6} {'resource':<28} {'service account':<46} impersonated human (groups)")
    for ev in events[-args.n :]:
        ref = ev.get("objectRef") or {}
        resource = f"{ref.get('namespace', '-')}/{ref.get('resource', '')}" + (
            f"/{ref['name']}" if ref.get("name") else ""
        )
        imp = ev["impersonatedUser"]
        print(
            f"{ev['requestReceivedTimestamp'][:19]:<20} {(ev.get('responseStatus') or {}).get('code', ''):<4} "
            f"{ev['verb']:<6} {resource[:28]:<28} {ev['user']['username'][:46]:<46} "
            f"{imp.get('username')} ({', '.join(imp.get('groups', []))})"
        )
    print(f"\n{len(events)} matching events")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
