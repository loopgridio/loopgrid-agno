from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = 40124


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        messages = body.get("messages") or []
        has_tool_result = any(m.get("role") == "tool" for m in messages if isinstance(m, dict))

        if not has_tool_result:
            print("MOCK: requesting sandbox_refund(amount=25, currency=USD)", flush=True)
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "agno_refund_call_1",
                        "type": "function",
                        "function": {
                            "name": "sandbox_refund",
                            "arguments": json.dumps({"amount": 25, "currency": "USD"}),
                        },
                    }
                ],
            }
            finish_reason = "tool_calls"
        else:
            print("MOCK: tool result received; returning final answer", flush=True)
            message = {"role": "assistant", "content": "LoopGrid Agno runtime test complete."}
            finish_reason = "stop"

        payload = {
            "id": "chatcmpl-loopgrid-agno",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "loopgrid-e2e",
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"LoopGrid deterministic Agno model listening on http://{HOST}:{PORT}/v1", flush=True)
    server.serve_forever()
