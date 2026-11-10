"""Local HTTP sink on 127.0.0.1 standing in for webhooks and email APIs. Logs every request as JSONL.

    python sink.py <port> <request_log.jsonl> <pid_file>
"""
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT, LOG, PIDF = int(sys.argv[1]), sys.argv[2], sys.argv[3]


class H(BaseHTTPRequestHandler):
    def _do(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        with open(LOG, "a") as f:
            f.write(json.dumps({"ts": time.time(), "method": self.command, "path": self.path, "body": body}) + "\n")
        out = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_CONNECT = _do

    def log_message(self, *a):
        pass


open(LOG, "a").close()
open(PIDF, "w").write(str(os.getpid()))
ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
