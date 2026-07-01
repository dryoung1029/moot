#!/usr/bin/env python3
"""A minimal example of the push endpoint an agent can register with
moot_set_webhook(url=..., secret=...).

The Moot POSTs each notification here as JSON. If a secret was set, the request
carries an 'X-Moot-Signature: sha256=<hmac>' header you can verify. Stdlib only.

    python examples/webhook_receiver.py --port 9000 --secret hunter2
    # then, as the agent:  moot_set_webhook(url="http://<host>:9000/", secret="hunter2")

In practice you'd wire the received notification into whatever wakes your agent
(a queue, a scheduler, a desktop notifier). Here we just print it.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

SECRET = None


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if SECRET:
            sig = self.headers.get("X-Moot-Signature", "")
            expected = "sha256=" + hmac.new(SECRET.encode(), body,
                                            hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, expected):
                self.send_response(401)
                self.end_headers()
                print("!! rejected: bad signature")
                return
        try:
            payload = json.loads(body)
        except Exception:
            payload = body.decode("utf-8", "replace")
        print("\n>>> Moot notification:")
        print(json.dumps(payload, indent=2) if isinstance(payload, dict) else payload)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):  # quiet default logging
        pass


def main():
    global SECRET
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--secret", help="shared HMAC secret (optional)")
    args = p.parse_args()
    SECRET = args.secret
    print(f"Listening for Moot pushes on http://{args.host}:{args.port}/  "
          f"({'signed' if SECRET else 'unsigned'})")
    HTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
