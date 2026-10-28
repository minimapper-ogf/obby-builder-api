"""Dependency-free HTTP adapter. Run one instance behind your HTTPS proxy."""
import argparse
from collections import defaultdict, deque
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import sqlite3
import threading
import time

from service import Service, ApiError


def reject_constant(_):
    raise ValueError("Non-finite JSON number")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def make_handler(service):
    buckets, lock = defaultdict(deque), threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "ObbyAPI"

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_):
            # Never log bearer headers, query identifiers, or request bodies.
            pass

        def run_request(self):
            try:
                authorization = self.headers.get("Authorization", "")
                if len(authorization) > 2048:
                    raise ApiError(400, "invalid_header", "Invalid authorization header")
                # Bounded request budgets, in addition to the registration SQL limit.
                identity = hashlib.sha256((authorization or self.client_address[0]).encode()).hexdigest()
                now = time.monotonic()
                with lock:
                    if len(buckets) > 2048:
                        for key in list(buckets):
                            if not buckets[key] or buckets[key][-1] < now-60:
                                del buckets[key]
                        if len(buckets) > 2048:
                            raise ApiError(429, "rate_limited", "Please retry later")
                    bucket = buckets[identity]
                    while bucket and bucket[0] <= now-60:
                        bucket.popleft()
                    if len(bucket) >= 120:
                        raise ApiError(429, "rate_limited", "Please retry later")
                    bucket.append(now)
                if self.headers.get("Transfer-Encoding"):
                    raise ApiError(400, "invalid_encoding", "Content-Length is required")
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > 150000:
                    raise ApiError(413, "body_too_large", "Request snapshot is too large")
                if length and self.headers.get_content_type() != "application/json":
                    raise ApiError(400, "content_type", "JSON content type is required")
                raw = self.rfile.read(length) if length else b""
                if len(raw) != length:
                    raise ApiError(400, "invalid_body", "Incomplete request")
                data = json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique_object) if raw else {}
                result = service.dispatch(self.command, self.path, authorization, data)
                self.reply(200, result)
            except ApiError as error:
                self.reply(error.status, {"code": error.code, "message": error.message})
            except (ValueError, TypeError, UnicodeError, RecursionError):
                self.reply(400, {"code": "invalid_json", "message": "Invalid request data"})
            except sqlite3.Error:
                self.reply(503, {"code": "storage_unavailable", "message": "Draft storage temporarily unavailable"})
            except Exception:
                logging.error("API request failed (internal error)")
                self.reply(503, {"code": "service_unavailable", "message": "Service temporarily unavailable"})

        def reply(self, status, data):
            encoded = json.dumps(data, allow_nan=False, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(encoded)))
            if status == 429:
                self.send_header("Retry-After", "60")
            self.end_headers()
            self.wfile.write(encoded)

        do_GET = do_POST = do_PUT = do_DELETE = run_request

    return Handler


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8127)
    parser.add_argument("--database", default=os.environ.get("OBBY_DATABASE", "obby.sqlite3"))
    args = parser.parse_args()
    secret = os.environ.get("OBBY_BOOTSTRAP_SECRET", "")
    previous = os.environ.get("OBBY_BOOTSTRAP_SECRET_PREVIOUS")
    game_id = os.environ.get("OBBY_GAME_ID")
    if len(secret) < 32 or game_id is None or not game_id.isdecimal():
        parser.error("Set OBBY_BOOTSTRAP_SECRET (32+ characters) and OBBY_GAME_ID")
    api = Service(args.database, [secret]+([previous] if previous else []), int(game_id))
    print(f"Obby persistence API listening on {args.host}:{args.port}")
    ThreadingHTTPServer((args.host, args.port), make_handler(api)).serve_forever()
