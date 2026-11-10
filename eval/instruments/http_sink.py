"""Local HTTP sink on 127.0.0.1: stands in for webhooks, chat APIs and wiki APIs. Logs every request.

    python http_sink.py <port> <request_log.jsonl> <pid_file>

One JSON line per request: ts, method, path, headers (lower-cased names), body.
It answers 200 {"ok": true} to everything, including CONNECT (so it also works as a proxy trap).
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT, LOG, PIDF = int(sys.argv[1]), sys.argv[2], sys.argv[3]
_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def _do(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        rec = {"ts": time.time(), "method": self.command, "path": self.path,
               "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body}
        with _lock, open(LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
        out = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        if self.command != "CONNECT":
            self.wfile.write(out)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_CONNECT = _do

    def log_message(self, *a):
        pass


open(LOG, "a").close()
srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
with open(PIDF, "w") as f:
    f.write(str(os.getpid()))
srv.serve_forever()
