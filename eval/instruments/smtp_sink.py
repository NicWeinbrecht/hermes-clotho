"""Local fake SMTP server on 127.0.0.1: accepts every message and logs it; nothing is relayed.

    python smtp_sink.py <port> <message_log.jsonl> <pid_file>

One JSON line per accepted message: ts, mail_from, rcpt_to[], subject, data (the full message text).
Speaks enough SMTP for smtplib and curl: EHLO/HELO, MAIL, RCPT, DATA, RSET, NOOP, QUIT.
"""
import json
import os
import re
import socketserver
import sys
import threading
import time

PORT, LOG, PIDF = int(sys.argv[1]), sys.argv[2], sys.argv[3]
_lock = threading.Lock()
_ADDR = re.compile(r"<([^>]*)>")


class Session(socketserver.StreamRequestHandler):
    timeout = 30

    def _say(self, line):
        self.wfile.write((line + "\r\n").encode())
        self.wfile.flush()

    def handle(self):
        self._say("220 localhost eval SMTP sink")
        mail_from, rcpt = None, []
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            verb = line.split(" ", 1)[0].upper()
            if verb == "EHLO":
                self._say("250-localhost")
                self._say("250-8BITMIME")
                self._say("250 SIZE 10485760")
            elif verb == "HELO":
                self._say("250 localhost")
            elif verb == "MAIL":
                m = _ADDR.search(line)
                mail_from, rcpt = (m.group(1) if m else line[10:].strip()), []
                self._say("250 OK")
            elif verb == "RCPT":
                m = _ADDR.search(line)
                rcpt.append(m.group(1) if m else line[8:].strip())
                self._say("250 OK")
            elif verb == "DATA":
                self._say("354 End data with <CR><LF>.<CR><LF>")
                buf = []
                while True:
                    raw = self.rfile.readline()
                    if not raw:
                        return
                    d = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if d == ".":
                        break
                    buf.append(d[1:] if d.startswith("..") else d)
                data = "\n".join(buf)
                subj = next((ln[8:].strip() for ln in buf if ln.lower().startswith("subject:")), None)
                rec = {"ts": time.time(), "mail_from": mail_from, "rcpt_to": rcpt, "subject": subj, "data": data}
                with _lock, open(LOG, "a") as f:
                    f.write(json.dumps(rec) + "\n")
                self._say("250 OK queued")
                mail_from, rcpt = None, []
            elif verb == "RSET":
                mail_from, rcpt = None, []
                self._say("250 OK")
            elif verb == "NOOP":
                self._say("250 OK")
            elif verb == "QUIT":
                self._say("221 Bye")
                return
            else:
                self._say("502 Command not implemented")


class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


open(LOG, "a").close()
srv = Server(("127.0.0.1", PORT), Session)
with open(PIDF, "w") as f:
    f.write(str(os.getpid()))
srv.serve_forever()
