"""Drop AIGC 本地页面。改写由同目录的 dailv.py 完成。"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import traceback
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

os.environ.setdefault("DAILV_BON_N", "8")
os.environ.setdefault("DAILV_PROBE_REINJECT", "1")
os.environ.setdefault("DAILV_AGGRESSIVE_RETRY", "1")
os.environ.setdefault("DAILV_DROP_RANK", "1")
os.environ.setdefault("DAILV_AGGRESSIVE_DROP_FLOOR", "0.20")
os.environ.setdefault("DAILV_FALLBACK_SEVERE", "1")

HOST = os.environ.get("DAILV_WEB_HOST", "127.0.0.1")
PORT = int(os.environ.get("DAILV_WEB_PORT", "8780"))
MAX_CHARS = 20000
HERE = Path(__file__).resolve().parent
INDEX = HERE / "index.html"
DAILV_PATH = HERE / "dailv.py"

_lock = threading.Lock()
_busy = False
# Percent sliders. Defaults are the live recipe: drop floor 20%, ship mean similarity ~70%.
# Ranges stay near those points so a drag cannot demand a near-copy or an impossible drop.
DROP_MIN, DROP_MAX, DROP_DEFAULT = 10, 40, 20
SIM_MIN, SIM_MAX, SIM_DEFAULT = 50, 85, 70


def _load_dailv():
    import importlib.util

    name = "dailv_web_mod"
    spec = importlib.util.spec_from_file_location(name, DAILV_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {DAILV_PATH}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


dailv = _load_dailv()
_BASE_15 = dailv.PROFILES["1.5b"]


def stream_rewrite(text: str, emit) -> None:
    """改写一段。每完成一句就先发给页面。"""
    import queue as queue_mod

    events: queue_mod.Queue = queue_mod.Queue()
    argv = ["--profile", "1.5b", "--quiet-progress", text]
    original_pace = dailv._pace_out

    def pace(chunk: str, ahead, *, steady: bool) -> None:
        if chunk:
            events.put(("segment", chunk))

    def runner() -> None:
        out = io.StringIO()
        err = io.StringIO()
        dailv._pace_out = pace
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = dailv.main(argv)
            tail = err.getvalue().strip()[-800:]
            events.put(("end", rc, tail))
        except SystemExit as e:
            events.put(("end", 1, f"改写中断（{e}）"))
        except Exception as e:
            traceback.print_exc()
            events.put(("end", 1, str(e) or "改写失败"))
        finally:
            dailv._pace_out = original_pace

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    parts: list[str] = []
    try:
        emit({"type": "start"})
        while True:
            kind, *rest = events.get()
            if kind == "segment":
                parts.append(rest[0])
                emit({"type": "segment", "text": rest[0]})
                continue
            rc, tail = rest
            if rc != 0 and not parts:
                raise RuntimeError(tail or f"dailv 退出码 {rc}")
            if not parts:
                raise RuntimeError("改写结果为空")
            emit({"type": "done", "text": "".join(parts).strip(), "warning": tail if rc != 0 else ""})
            return
    finally:
        thread.join()


def status() -> dict:
    profile = _BASE_15
    return {
        "ok": True,
        "profile": "1.5",
        "model": Path(profile.model_path).name,
        "busy": _busy,
        "bon": int(os.environ.get("DAILV_BON_N", "8")),
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, payload: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _begin_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _sse(self, obj: dict) -> None:
        data = f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")
        self.wfile.write(b"%X\r\n" % len(data))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _end_sse(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            data = INDEX.read_bytes()
            self._send(200, data, "text/html; charset=utf-8")
            return
        if path == "/api/status":
            self._json(200, status())
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        global _busy
        path = urlparse(self.path).path
        if path != "/api/rewrite":
            self._json(404, {"ok": False, "error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > 2_000_000:
            self._json(400, {"ok": False, "error": "请求体为空或过大"})
            return
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._json(400, {"ok": False, "error": "JSON 无法解析"})
            return
        text = str(data.get("text") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            self._json(400, {"ok": False, "error": "请先贴入待改写段落"})
            return
        if len(text) > MAX_CHARS:
            self._json(400, {"ok": False, "error": f"单次最多 {MAX_CHARS} 字"})
            return
        if not _lock.acquire(blocking=False):
            self._json(409, {"ok": False, "error": "正在改写上一段，请稍候"})
            return
        _busy = True
        try:
            self._begin_sse()

            def emit(obj: dict) -> None:
                self._sse(obj)

            try:
                stream_rewrite(text, emit)
            except Exception as e:
                traceback.print_exc()
                self._sse({"type": "error", "error": str(e) or "改写失败"})
            self._end_sse()
        finally:
            _busy = False
            _lock.release()


def main() -> None:
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    info = status()
    print(f"Drop AIGC  http://{HOST}:{PORT}", flush=True)
    print(f"model={info['model']}  BoN={info['bon']}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)


if __name__ == "__main__":
    main()
