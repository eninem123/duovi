import argparse
import asyncio
import json
import os
import sys

from main import QuantTradingSystem, run_async
from mcp_agent import MCPAgent


BRIDGE_PREFIX = "__BRIDGE_JSON__"


def emit(payload):
    print(f"{BRIDGE_PREFIX}{json.dumps(payload, ensure_ascii=False)}", flush=True)


def read_payload():
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command")
    args = parser.parse_args()
    payload = read_payload()

    try:
        if args.command == "notebook-status":
            result = run_async(MCPAgent(config_path="config.yaml").get_notebooklm_status())
        elif args.command == "setup-auth":
            result = run_async(MCPAgent(config_path="config.yaml").setup_notebooklm_auth())
        elif args.command == "ask-mda":
            result = run_async(
                MCPAgent(config_path="config.yaml").ask_multi_domain_foresight(
                    payload.get("message", ""),
                    history=payload.get("history") or [],
                )
            )
        elif args.command == "why-not-buy":
            result = run_async(
                MCPAgent(config_path="config.yaml").explain_why_not_buy(
                    payload.get("symbol", ""),
                    history=payload.get("history") or [],
                )
            )
        elif args.command == "manual-refresh":
            result = QuantTradingSystem().run_manual_refresh()
        else:
            result = {"success": False, "error": f"未知 bridge 命令: {args.command}"}
    except Exception as exc:
        result = {"success": False, "error": str(exc)}

    emit(result)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        os._exit(0)


if __name__ == "__main__":
    main()
