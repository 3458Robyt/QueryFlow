from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .review import _read_sample_receipt, build_review_model, render_review_model
from .task import baseline_file, preview_notebook_task
from .workspace import read_manifest


def load_review_model(task: Path) -> dict[str, Any]:
    manifest = read_manifest(task)
    filename = str(manifest["filename"])
    before = baseline_file(task, filename)
    if manifest.get("resource", {}).get("kind") == "notebook":
        after = preview_notebook_task(task)
    else:
        after = (task / filename).read_bytes()
    validation_path = task / "validation.json"
    if validation_path.exists():
        validation = json.loads(validation_path.read_text(encoding="utf-8"))
    else:
        validation = {"status": "pending", "method": "pending", "publishable": False}
    return build_review_model(manifest, validation, before, after, _read_sample_receipt(task))


def _handler_factory(task: Path) -> type[BaseHTTPRequestHandler]:
    class ReviewHandler(BaseHTTPRequestHandler):
        server_version = "QueryFlowReview/1"

        def _send(self, body: bytes, content_type: str, status: int = HTTPStatus.OK) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            path = urlsplit(self.path).path
            if path == "/api/review":
                try:
                    body = json.dumps(load_review_model(task), ensure_ascii=False).encode("utf-8")
                except (OSError, ValueError, KeyError, RuntimeError) as error:
                    self._send(
                        json.dumps({"error": str(error)}, ensure_ascii=False).encode("utf-8"),
                        "application/json; charset=utf-8",
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                    return
                self._send(body, "application/json; charset=utf-8")
                return
            if path in {"/", "/review.html"}:
                try:
                    model = load_review_model(task)
                    body = render_review_model(model, live=True).encode("utf-8")
                except (OSError, ValueError, KeyError, RuntimeError) as error:
                    self._send(str(error).encode("utf-8"), "text/plain; charset=utf-8", HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                self._send(body, "text/html; charset=utf-8")
                return
            self._send(b"Not found\n", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return ReviewHandler


def serve_review(task: Path, port: int) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("0.0.0.0", port), _handler_factory(task))
    host = os.environ.get("WEB_HOST", "localhost")
    actual_port = int(server.server_address[1])
    return server, f"https://{actual_port}-{host}/review.html"
