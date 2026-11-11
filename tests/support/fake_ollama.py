"""A loopback stand-in for Ollama's HTTP API, for transport tests.

It binds 127.0.0.1 on an ephemeral port and records every request. Each path
can be scripted to misbehave: a wrong status, content type or size, a missing
Content-Length, a redirect, or a body sent one byte at a time. No model runs.

It can also be started by hand as a hostile endpoint for a manual check:
    python -m tests.support.fake_ollama --mode oversize
"""
import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "fixture-model:latest"
DIGEST = "sha256:" + "ab" * 32


def chat_reply(content, **extra):
    """An Ollama /api/chat reply whose message content is the given text."""
    if not isinstance(content, str):
        content = json.dumps(content)
    return {"model": MODEL, "message": {"role": "assistant", "content": content}, "done": True,
            "done_reason": "stop", "prompt_eval_count": 10, "eval_count": 5,
            "load_duration": 250000000, "prompt_eval_duration": 1000000, "eval_duration": 2000000, **extra}


class Reply:
    """One scripted HTTP response.

    body: a JSON-serialisable value or raw bytes. length: True sends the real
    Content-Length, False sends none, an int declares that value. drip: seconds
    between single bytes of the body.
    """

    def __init__(self, body=None, *, status=200, content_type="application/json",
                 length=True, drip=None, headers=None):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.status, self.content_type, self.length = status, content_type, length
        self.drip, self.headers = drip, headers or {}


class FakeOllama:
    def __init__(self, models=(MODEL,)):
        self.requests = []
        self.models = list(models)
        self.routes = {}
        self._stop = threading.Event()
        self.chat = self.default_chat
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                owner._handle(self, None)

            def do_POST(self):
                size = int(self.headers.get("Content-Length") or 0)
                owner._handle(self, self.rfile.read(size))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .05},
                                       name="fake-ollama", daemon=True)

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @staticmethod
    def default_chat(body):
        """Answer a warm-up with ready and any other chat with an empty object."""
        schema = body.get("format") if isinstance(body, dict) else None
        ready = isinstance(schema, dict) and "ready" in schema.get("properties", {})
        return Reply(chat_reply({"ready": True} if ready else {}))

    def paths(self, path=None):
        return [r["path"] for r in self.requests if path is None or r["path"] == path]

    def default(self, path):
        if path == "/api/chat":
            return self.chat
        if path == "/api/tags":
            return Reply({"models": [{"name": m, "model": m, "digest": DIGEST, "size": 1000} for m in self.models]})
        if path == "/api/version":
            return Reply({"version": "0.0.0-fixture"})
        if path == "/api/show":
            return Reply({"license": "x" * 50, "template": "{{ .Prompt }}",
                          "details": {"family": "fixture", "parameter_size": "1B", "quantization_level": "Q4_K_M"},
                          "model_info": {"general.architecture": "fixture", "fixture.context_length": 40960}})
        if path == "/api/ps":
            return Reply({"models": [{"name": m, "model": m, "digest": DIGEST, "size": 1000, "size_vram": 750,
                                      "context_length": 16384, "expires_at": "2030-01-01T00:00:00Z"}
                                     for m in self.models]})
        return Reply({"error": "not found"}, status=404)

    def _handle(self, handler, raw):
        body = None
        if raw:
            try:
                body = json.loads(raw)
            except ValueError:
                body = raw
        self.requests.append({"method": handler.command, "path": handler.path, "body": body})
        reply = self.routes.get(handler.path) or self.default(handler.path)
        if callable(reply):
            reply = reply(body)
        try:
            handler.send_response(reply.status)
            if reply.content_type:
                handler.send_header("Content-Type", reply.content_type)
            if reply.length is True:
                handler.send_header("Content-Length", str(len(reply.body)))
            elif reply.length is not False:
                handler.send_header("Content-Length", str(reply.length))
            for key, value in reply.headers.items():
                handler.send_header(key, value)
            handler.send_header("Connection", "close")
            handler.end_headers()
            if reply.drip is None:
                handler.wfile.write(reply.body)
                return
            for index in range(len(reply.body)):
                if self._stop.wait(reply.drip):
                    return
                handler.wfile.write(reply.body[index:index + 1])
                handler.wfile.flush()
        except OSError:
            pass  # The client gave up, which is what several tests expect.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["normal", "oversize", "drip", "redirect"], default="normal")
    args = parser.parse_args()
    with FakeOllama() as server:
        if args.mode == "oversize":
            server.chat = Reply(chat_reply({}, padding="x" * (16 * 1024 * 1024)), length=False)
        elif args.mode == "drip":
            server.chat = Reply(chat_reply({}), drip=1)
        elif args.mode == "redirect":
            server.chat = Reply(b"", status=302, headers={"Location": "http://127.0.0.1:9/api/chat"})
        print("Fake Ollama (" + args.mode + ") at " + server.url, flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
