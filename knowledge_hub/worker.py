"""GASのpendingリクエストを処理するRecall常駐ワーカー。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

from .config import ConfigError, resolve_vault
from .recall import handle_request


def _request_json(url: str, *, data: dict[str, object] | None = None, timeout: float = 15) -> object:
    body = None
    headers = {"Accept": "application/json"}
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST" if body else "GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def process_once(endpoint: str, token: str, vault) -> bool:
    query = urllib.parse.urlencode({"mode": "pending", "token": token})
    pending = _request_json(f"{endpoint}?{query}")
    if not isinstance(pending, dict) or not pending.get("request"):
        return False
    request = pending["request"]
    if not isinstance(request, dict) or not isinstance(request.get("id"), str):
        return False
    payload = request.get("payload", {})
    result = handle_request(payload if isinstance(payload, dict) else {}, vault)
    _request_json(endpoint, data={"mode": "result", "token": token,
                                  "id": request["id"], "result": result})
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recall PWA GAS polling worker")
    parser.add_argument("--endpoint", default=os.environ.get("KH_RECALL_GAS_URL"))
    parser.add_argument("--token", default=os.environ.get("KH_RECALL_TOKEN"))
    parser.add_argument("--vault")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=3.0)
    args = parser.parse_args(argv)
    if not args.endpoint or not args.token:
        print("KH_RECALL_GAS_URL と KH_RECALL_TOKEN を設定してください", file=sys.stderr)
        return 2
    try:
        vault = resolve_vault(args.vault)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    while True:
        try:
            process_once(args.endpoint, args.token, vault)
        except Exception as exc:
            print(f"Recall worker error: {exc}", file=sys.stderr)
        if args.once:
            return 0
        time.sleep(max(0.5, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
