"""GASのpendingリクエストを処理するRecall常駐ワーカー。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

from . import config
from .config import ConfigError, resolve_vault
from .recall import handle_request


class RelayTransportError(RuntimeError):
    """リレー通信の失敗箇所を、URLやトークンを含めずに示す。"""


def _request_json(
    url: str,
    *,
    data: dict[str, object] | None = None,
    timeout: float = config.RECALL_HTTP_TIMEOUT,
) -> object:
    body = None
    headers = {"Accept": "application/json"}
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST" if body else "GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def process_once(endpoint: str, token: str, vault, *, timeout: float = config.RECALL_HTTP_TIMEOUT) -> bool:
    query = urllib.parse.urlencode({"mode": "pending", "token": token})
    try:
        pending = _request_json(f"{endpoint}?{query}", timeout=timeout)
    except Exception as exc:
        raise RelayTransportError(
            f"依頼取得に失敗しました ({type(exc).__name__})"
        ) from exc
    if not isinstance(pending, dict) or not pending.get("request"):
        return False
    request = pending["request"]
    if not isinstance(request, dict) or not isinstance(request.get("id"), str):
        return False
    payload = request.get("payload", {})
    request_type = payload.get("type") if isinstance(payload, dict) else None
    safe_type = request_type if request_type in {"search", "summarize", "capture"} else "unknown"
    print(f"Recall worker: {safe_type} を処理しています", file=sys.stderr)
    result = handle_request(payload if isinstance(payload, dict) else {}, vault)
    try:
        _request_json(
            endpoint,
            data={"mode": "result", "token": token, "id": request["id"], "result": result},
            timeout=timeout,
        )
    except Exception as exc:
        raise RelayTransportError(
            f"結果送信に失敗しました ({type(exc).__name__})"
        ) from exc
    print(f"Recall worker: {safe_type} の結果を送信しました", file=sys.stderr)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recall PWA GAS polling worker")
    parser.add_argument("--endpoint", default=os.environ.get("KH_RECALL_GAS_URL"))
    parser.add_argument("--token", default=os.environ.get("KH_RECALL_TOKEN"))
    parser.add_argument("--vault")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--http-timeout", type=float)
    args = parser.parse_args(argv)
    if not args.endpoint or not args.token:
        print("KH_RECALL_GAS_URL と KH_RECALL_TOKEN を設定してください", file=sys.stderr)
        return 2
    try:
        vault = resolve_vault(args.vault)
        http_timeout = (
            args.http_timeout
            if args.http_timeout is not None
            else config.positive_float_env(
                "KH_RECALL_HTTP_TIMEOUT", config.RECALL_HTTP_TIMEOUT
            )
        )
        if http_timeout <= 0:
            raise ConfigError("--http-timeout は正の数で指定してください")
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    while True:
        try:
            process_once(args.endpoint, args.token, vault, timeout=http_timeout)
        except Exception as exc:
            print(f"Recall worker error: {exc}", file=sys.stderr)
        if args.once:
            return 0
        time.sleep(max(0.5, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
