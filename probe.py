"""Milestone 0 transport probe only. No player data or production API.

Run: python3 site-api/probe.py --host 127.0.0.1 --port 8080
Expose through your HTTPS proxy for a real Creator server round trip.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json


class ProbeHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/v1/capabilities/probe":
            self.reply(404, {"code": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024:
                self.reply(413, {"code": "invalid_size"})
                return
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict) or data.get("challenge") != "obby-transport-v1":
                raise ValueError("invalid probe")
        except (ValueError, UnicodeDecodeError):
            self.reply(400, {"code": "invalid_probe"})
            return
        self.reply(200, {"challenge": "obby-transport-v1",
            "header_received": self.headers.get("X-Obby-Probe") == "transport-v1"})

    def reply(self, status, body):
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    ThreadingHTTPServer((args.host, args.port), ProbeHandler).serve_forever()
