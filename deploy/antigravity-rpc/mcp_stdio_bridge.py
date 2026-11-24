"""Minimal concurrent stdio MCP server forwarding client_tools calls to the local bridge."""

import json
import os
import socket
import sys
import threading
from pathlib import Path

WRITE_LOCK = threading.Lock()


def send_message(payload: dict) -> None:
    encoded = json.dumps(payload, ensure_ascii=False) + "\n"
    with WRITE_LOCK:
        sys.stdout.write(encoded)
        sys.stdout.flush()


def invoke_bridge_callback(port: int, token: str, name: str, arguments: object) -> tuple[bool, str]:
    request_line = (
        json.dumps(
            {"token": token, "name": name, "arguments": arguments},
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")
    with socket.create_connection(("127.0.0.1", port), timeout=300) as sock:
        sock.sendall(request_line)
        reader = sock.makefile("rb")
        raw = reader.readline(8 * 1024 * 1024)
        if not raw:
            return False, "Bridge closed tool callback connection"
        reply = json.loads(raw.decode("utf-8"))
        return bool(reply.get("ok")), str(reply.get("content", ""))


def handle_tool_call(msg_id, params: dict, port: int, token: str) -> None:
    name = params.get("name", "")
    arguments = params.get("arguments")
    try:
        ok, text = invoke_bridge_callback(port, token, name, arguments)
    except Exception as exc:
        ok, text = False, f"Tool bridge error: {type(exc).__name__}"
    send_message(
        {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "content": [{"type": "text", "text": text}],
                "isError": not ok,
            },
        }
    )


def main() -> None:
    tools_path = Path(sys.argv[1])
    callback_port = int(sys.argv[2])
    token = os.environ.get("AGY_BRIDGE_TOKEN", "")
    tools = json.loads(tools_path.read_text(encoding="utf-8"))

    active_threads: list[threading.Thread] = []
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        msg_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

        if method == "initialize" and msg_id is not None:
            pv = params.get("protocolVersion") or "2024-11-05"
            send_message(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": pv,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "client_tools", "version": "1.0.0"},
                    },
                }
            )
        elif method == "tools/list" and msg_id is not None:
            send_message(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"tools": tools},
                }
            )
        elif method == "tools/call" and msg_id is not None:
            worker = threading.Thread(
                target=handle_tool_call,
                args=(msg_id, params, callback_port, token),
                daemon=True,
            )
            worker.start()
            active_threads.append(worker)
        elif msg_id is not None:
            send_message({"jsonrpc": "2.0", "id": msg_id, "result": {}})


if __name__ == "__main__":
    main()
