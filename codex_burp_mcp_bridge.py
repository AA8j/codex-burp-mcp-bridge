#!/usr/bin/env python3
"""Bridge Codex stdio MCP to Burp Suite's legacy HTTP+SSE MCP endpoint."""

from __future__ import annotations

import argparse
import json
import queue
import signal
import sys
import threading
import time
from pathlib import Path
from socket import timeout as SocketTimeout
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


DEFAULT_UPSTREAM = "http://127.0.0.1:9876/"
CONFIG_PATH = Path(__file__).with_name("codex_burp_mcp_bridge.json")
FALLBACK_METHODS = {
    "resources/list": {"resources": []},
    "resources/templates/list": {"resourceTemplates": []},
    "prompts/list": {"prompts": []},
}


def log(message: str, quiet: bool = False) -> None:
    if quiet:
        return
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", file=sys.stderr, flush=True)


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise RuntimeError(f"Config file must contain a JSON object: {CONFIG_PATH}")
    return data


class BurpSseClient:
    def __init__(
        self,
        upstream_base: str,
        endpoint_timeout: float,
        post_timeout: float,
        rpc_timeout: float,
        reconnect_attempts: int,
        quiet: bool,
    ) -> None:
        self.upstream_base = upstream_base
        self.endpoint_timeout = endpoint_timeout
        self.post_timeout = post_timeout
        self.rpc_timeout = rpc_timeout
        self.reconnect_attempts = reconnect_attempts
        self.quiet = quiet

        self.endpoint_url: str | None = None
        self.reader_started = False
        self.reader_lock = threading.Lock()
        self.endpoint_ready = threading.Event()
        self.pending: dict[Any, queue.Queue[dict[str, Any]]] = {}
        self.pending_lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.closed = False
        self.last_error: str | None = None
        self.stop_event = threading.Event()

    def ensure_reader(self) -> None:
        with self.reader_lock:
            if not self.reader_started:
                self.reader_started = True
                self.endpoint_ready = threading.Event()
                thread = threading.Thread(target=self._reader_loop, daemon=True)
                thread.start()

        if not self.endpoint_ready.wait(timeout=self.endpoint_timeout):
            raise RuntimeError(f"Burp SSE endpoint was not announced: {self.last_error or 'timeout'}")
        if not self.endpoint_url:
            raise RuntimeError(f"Burp SSE endpoint is not ready: {self.last_error or 'unknown error'}")

    def _reader_loop(self) -> None:
        log(f"connecting Burp SSE: {self.upstream_base}", self.quiet)
        req = Request(self.upstream_base, headers={"Accept": "text/event-stream"})
        try:
            with urlopen(req, timeout=None) as resp:
                event = "message"
                data_lines: list[str] = []
                for raw_line in resp:
                    line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
                    if line == "":
                        self._dispatch_event(event, "\n".join(data_lines))
                        event = "message"
                        data_lines = []
                        continue
                    if line.startswith(":"):
                        continue
                    if ":" in line:
                        field, value = line.split(":", 1)
                        value = value.lstrip(" ")
                    else:
                        field, value = line, ""
                    if field == "event":
                        event = value or "message"
                    elif field == "data":
                        data_lines.append(value)
        except SocketTimeout as exc:
            self.last_error = str(exc)
            log(f"Burp SSE timeout: {exc}", self.quiet)
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            if not self.stop_event.is_set():
                log(f"Burp SSE disconnected: {exc}", self.quiet)
        finally:
            self.endpoint_ready.set()
            with self.reader_lock:
                self.reader_started = False
                self.endpoint_url = None
            self._fail_pending("Burp SSE disconnected")

    def _dispatch_event(self, event: str, data: str) -> None:
        if not data:
            return
        if event == "endpoint":
            self.endpoint_url = urljoin(self.upstream_base, data)
            log(f"Burp endpoint ready: {self.endpoint_url}", self.quiet)
            self.endpoint_ready.set()
            return

        try:
            message = json.loads(data)
        except json.JSONDecodeError:
            return

        response_id = message.get("id")
        if response_id is not None:
            with self.pending_lock:
                target_queue = self.pending.get(response_id)
            if target_queue is not None:
                target_queue.put(message)
                return

        self.write_stdout(message)

    def _fail_pending(self, message: str) -> None:
        with self.pending_lock:
            pending = list(self.pending.items())
            self.pending.clear()
        for request_id, response_queue in pending:
            response_queue.put(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32000, "message": message},
                }
            )

    def write_stdout(self, message: dict[str, Any] | list[Any]) -> None:
        with self.write_lock:
            sys.stdout.write(json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n")
            sys.stdout.flush()

    def reset_connection(self) -> None:
        with self.reader_lock:
            self.endpoint_url = None
            self.reader_started = False
            self.endpoint_ready = threading.Event()

    def close(self) -> None:
        self.stop_event.set()
        self._fail_pending("bridge is shutting down")

    def post(self, message: dict[str, Any]) -> dict[str, Any] | None:
        self.ensure_reader()
        assert self.endpoint_url is not None
        req = Request(
            self.endpoint_url,
            data=json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        try:
            with urlopen(req, timeout=self.post_timeout) as resp:
                raw = resp.read()
                content_type = resp.headers.get("Content-Type", "")
                if not raw:
                    return None
                text = raw.decode("utf-8", "replace")
                if "application/json" in content_type:
                    return json.loads(text)
                return None
        except HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"Burp HTTP error {exc.code}: {text}") from exc
        except URLError as exc:
            raise RuntimeError(f"Burp URL error: {exc}") from exc

    def request(self, message: dict[str, Any]) -> dict[str, Any]:
        request_id = message.get("id")
        if request_id is None:
            raise RuntimeError("request message has no id")

        response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self.pending_lock:
            self.pending[request_id] = response_queue
        try:
            direct_response = self.post_with_reconnect(message)
            if direct_response is not None:
                return direct_response
            return response_queue.get(timeout=self.rpc_timeout)
        except queue.Empty as exc:
            raise RuntimeError(f"timed out waiting for Burp response to {message.get('method')}") from exc
        finally:
            with self.pending_lock:
                self.pending.pop(request_id, None)

    def notify(self, message: dict[str, Any]) -> None:
        self.post_with_reconnect(message)

    def post_with_reconnect(self, message: dict[str, Any]) -> dict[str, Any] | None:
        last_error: RuntimeError | None = None
        for attempt in range(self.reconnect_attempts + 1):
            try:
                return self.post(message)
            except RuntimeError as exc:
                last_error = exc
                if attempt >= self.reconnect_attempts:
                    break
                log(f"Burp request failed, reconnecting once: {exc}", self.quiet)
                self.reset_connection()
        assert last_error is not None
        raise last_error


class BridgeServer:
    def __init__(self, client: BurpSseClient) -> None:
        self.client = client

    def run(self) -> None:
        log("Burp stdio bridge started", self.client.quiet)
        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except Exception as exc:  # noqa: BLE001
                    log(f"invalid JSON-RPC input: {exc}", self.client.quiet)
                    self.client.write_stdout(
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {"code": -32700, "message": str(exc)},
                        }
                    )
                    continue

                if self.is_notification_only(message):
                    self.handle_message(message)
                    continue

                if self.is_control_message(message):
                    self._handle_and_write(message)
                    continue

                thread = threading.Thread(target=self._handle_and_write, args=(message,), daemon=True)
                thread.start()
        except KeyboardInterrupt:
            log("Burp stdio bridge interrupted", self.client.quiet)
        finally:
            self.client.close()

    def _handle_and_write(self, message: Any) -> None:
        try:
            response = self.handle_message(message)
            if response is not None:
                self.client.write_stdout(response)
        except Exception as exc:  # noqa: BLE001
            request_id = self.extract_request_id(message)
            log(f"bridge error: {exc}", self.client.quiet)
            self.client.write_stdout(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32603, "message": str(exc)},
                }
            )

    @staticmethod
    def extract_request_id(message: Any) -> Any:
        if isinstance(message, dict):
            return message.get("id")
        return None

    @staticmethod
    def is_notification_only(message: Any) -> bool:
        if isinstance(message, dict):
            return "id" not in message
        if isinstance(message, list):
            return all(isinstance(item, dict) and "id" not in item for item in message)
        return False

    @staticmethod
    def is_control_message(message: Any) -> bool:
        if not isinstance(message, dict):
            return False
        return message.get("method") in {"initialize", "notifications/initialized", "ping"}

    def handle_message(self, message: Any) -> dict[str, Any] | list[Any] | None:
        if isinstance(message, list):
            responses = []
            for item in message:
                response = self.handle_single(item)
                if response is not None:
                    responses.append(response)
            return responses or None
        return self.handle_single(message)

    def handle_single(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid JSON-RPC message"},
            }

        method = message.get("method")
        request_id = message.get("id")

        if request_id is None:
            try:
                self.client.notify(message)
            except RuntimeError as exc:
                log(f"notification failed: method={method} error={exc}", self.client.quiet)
            return None

        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}

        try:
            response = self.client.request(message)
        except RuntimeError as exc:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": str(exc)}}

        error = response.get("error")
        if isinstance(error, dict) and error.get("code") == -32601 and method in FALLBACK_METHODS:
            return {"jsonrpc": "2.0", "id": request_id, "result": FALLBACK_METHODS[method]}
        return response


def parse_args() -> argparse.Namespace:
    config = load_config()
    parser = argparse.ArgumentParser(description="Bridge stdio MCP clients to Burp's legacy SSE MCP server.")
    parser.add_argument("--upstream", default=config.get("upstream", DEFAULT_UPSTREAM), help=f"Burp SSE MCP URL, default: {DEFAULT_UPSTREAM}")
    parser.add_argument("--endpoint-timeout", type=float, default=float(config.get("endpoint_timeout", 10.0)))
    parser.add_argument("--post-timeout", type=float, default=float(config.get("post_timeout", 120.0)))
    parser.add_argument("--rpc-timeout", type=float, default=float(config.get("rpc_timeout", 180.0)))
    parser.add_argument("--reconnect-attempts", type=int, default=int(config.get("reconnect_attempts", 1)))
    parser.add_argument("--quiet", action="store_true", default=bool(config.get("quiet", False)))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    client = BurpSseClient(
        upstream_base=args.upstream,
        endpoint_timeout=args.endpoint_timeout,
        post_timeout=args.post_timeout,
        rpc_timeout=args.rpc_timeout,
        reconnect_attempts=args.reconnect_attempts,
        quiet=args.quiet,
    )
    BridgeServer(client).run()


if __name__ == "__main__":
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    main()
