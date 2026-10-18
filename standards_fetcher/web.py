from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import secrets
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from . import APP_NAME, __version__
from .catalog import get_record, load_collections
from .discovery import discover, purchase_url
from .downloader import DownloadCancelled, Job, JobManager, run_download_job
from .mohurd import load_index, refresh_index
from .util import (
    app_data_dir,
    choose_folder,
    default_download_dir,
    ensure_writable_dir,
    load_json,
    log,
    open_path,
    open_url,
    reveal_path,
    save_json,
    setup_logging,
    shorten,
    static_dir,
)
from .downloader import JobItem


APP_CONFIG_FILE = app_data_dir() / "settings.json"
DISCOVERY_FILE = app_data_dir() / "discovered.json"


class AppContext:
    def __init__(self, token: str) -> None:
        self.token = token
        self.jobs = JobManager()
        self.lock = threading.Lock()
        self.shutdown_event = threading.Event()
        self.server: Optional[ThreadingHTTPServer] = None
        self.settings = load_json(APP_CONFIG_FILE, {}) or {}
        self.dynamic: dict[str, dict[str, Any]] = {}
        self.last_discovery: Optional[dict[str, Any]] = None
        self._load_discovery()

    def _load_discovery(self) -> None:
        payload = load_json(DISCOVERY_FILE, {})
        if not isinstance(payload, dict):
            return
        for item in payload.get("items", []) or []:
            if isinstance(item, dict) and item.get("id"):
                self.dynamic[str(item["id"])] = item
        if payload.get("query"):
            self.last_discovery = payload

    def set_discovery(
        self, query: str, items: list[dict[str, Any]], meta: dict[str, Any]
    ) -> None:
        for item in items:
            if item.get("id"):
                self.dynamic[str(item["id"])] = item
        if len(self.dynamic) > 600:
            # 只保留最近检索到的记录，避免缓存无限增长。
            keep = {
                str(item["id"]): item
                for item in items
                if item.get("id")
            }
            self.dynamic = keep
        self.last_discovery = {"query": query, "meta": meta, "items": items}
        try:
            save_json(DISCOVERY_FILE, self.last_discovery)
        except OSError:
            pass

    def find_record(self, record_id: str) -> Optional[dict[str, Any]]:
        return get_record(record_id) or self.dynamic.get(record_id)

    @property
    def out_dir(self) -> str:
        value = self.settings.get("out_dir") or str(default_download_dir())
        return str(Path(value).expanduser())

    def set_out_dir(self, value: str) -> None:
        self.settings["out_dir"] = value
        save_json(APP_CONFIG_FILE, self.settings)


def _json_response(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _error(handler: BaseHTTPRequestHandler, message: str, status: int = 400) -> None:
    _json_response(handler, {"ok": False, "error": message}, status)


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    if length > 1024 * 1024:
        raise ValueError("请求体过大")
    raw = handler.rfile.read(length)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("请求格式错误") from exc
    return value if isinstance(value, dict) else {}


def _record_for_web(record: dict[str, Any]) -> dict[str, Any]:
    amendment = record.get("amendment") or {}
    draft = record.get("draft") or {}
    return {
        "id": record.get("id"),
        "number": record.get("number"),
        "name": record.get("name"),
        "category": record.get("category"),
        "category_name": record.get("category_name"),
        "code_type": record.get("code_type", "其他"),
        "code_label": record.get("code_label", "其他标准"),
        "status": record.get("status"),
        "impl_date": record.get("impl_date"),
        "announce_date": record.get("announce_date"),
        "pdf_available": bool(record.get("pdf_available")),
        "pdf_note": record.get("pdf_note", ""),
        "has_amendment": bool(amendment),
        "amendment": {
            "label": amendment.get("label", ""),
            "number": amendment.get("number", ""),
            "name": amendment.get("name", ""),
            "impl_date": amendment.get("impl_date", ""),
            "pdf_available": bool(amendment.get("pdf_available")),
            "note": amendment.get("note", ""),
        }
        if amendment
        else None,
        "has_draft": bool(draft),
        "draft": {
            "title": draft.get("title", ""),
            "url": draft.get("url", ""),
            "kind": draft.get("kind", ""),
        }
        if draft
        else None,
        "supersedes": record.get("supersedes", []),
        "hidden_versions": int(record.get("hidden_versions") or 0),
        "note": record.get("note", ""),
        "announcement_url": record.get("announcement_url", ""),
        "detail_url": record.get("detail_url", ""),
        "official_url": record.get("official_url", ""),
        "purchase_url": record.get("purchase_url")
        or purchase_url(record.get("number", "")),
        "source": record.get("source", ""),
        "source_label": record.get("source_label", ""),
        "source_labels": record.get("source_labels", []),
        "sources": record.get("sources", []),
        "source_count": int(record.get("source_count") or 0),
        "verified_at": record.get("verified_at", ""),
        "dynamic": record.get("category") == "discovered",
        "downloadable": bool(record.get("pdf_available"))
        or bool(amendment.get("pdf_available")),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "StandardsFetcher/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> AppContext:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    # -- helpers ---------------------------------------------------------
    def _authorized(self) -> bool:
        header = self.headers.get("X-App-Token", "")
        return secrets.compare_digest(header, self.app.token)

    def _serve_file(self, path: Path) -> None:
        try:
            raw = path.read_bytes()
        except OSError:
            _error(self, "文件不存在", 404)
            return
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if path.suffix == ".js":
            content_type = "application/javascript; charset=utf-8"
        elif path.suffix in (".html", ".css"):
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(raw)

    # -- GET -------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path in ("/", "/index.html"):
                index = static_dir() / "index.html"
                html_text = index.read_text(encoding="utf-8")
                html_text = html_text.replace("__APP_TOKEN__", self.app.token)
                raw = html_text.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)
                return
            if path.startswith("/static/"):
                relative = posixpath.normpath(path[len("/static/") :])
                if relative.startswith("..") or relative.startswith("/"):
                    _error(self, "非法路径", 400)
                    return
                target = (static_dir() / relative).resolve()
                if not str(target).startswith(str(static_dir().resolve()) + os.sep):
                    _error(self, "非法路径", 400)
                    return
                self._serve_file(target)
                return
            if path == "/api/health":
                _json_response(self, {"ok": True, "version": __version__})
                return
            if not self._authorized():
                _error(self, "未授权：请从应用窗口访问", 403)
                return
            if path == "/api/config":
                collections = load_collections()
                _json_response(
                    self,
                    {
                        "ok": True,
                        "app_name": APP_NAME,
                        "version": __version__,
                        "snapshot": collections.get("snapshot", ""),
                        "source_name": collections.get("source_name", ""),
                        "source_url": collections.get("source_url", ""),
                        "description": collections.get("description", ""),
                        "out_dir": self.app.out_dir,
                        "data_dir": str(app_data_dir()),
                        "index_count": len(load_index().get("items", [])),
                        "discovery_limit": 60,
                    },
                )
                return
            if path == "/api/discovered":
                payload = self.app.last_discovery
                if not payload:
                    _json_response(
                        self,
                        {"ok": True, "query": "", "meta": {}, "items": []},
                    )
                    return
                _json_response(
                    self,
                    {
                        "ok": True,
                        "query": payload.get("query", ""),
                        "meta": payload.get("meta", {}),
                        "total": len(payload.get("items", [])),
                        "items": [
                            _record_for_web(record)
                            for record in payload.get("items", [])
                        ],
                    },
                )
                return
            if path.startswith("/api/jobs/"):
                job_id = path.split("/")[-1]
                job = self.app.jobs.get(job_id)
                if not job:
                    _error(self, "任务不存在", 404)
                    return
                _json_response(self, {"ok": True, "job": job.to_dict()})
                return
            _error(self, "接口不存在", 404)
        except Exception as exc:  # noqa: BLE001
            log.error("GET %s 失败：%s\n%s", self.path, exc, traceback.format_exc())
            _error(self, f"服务器错误：{exc}", 500)

    # -- POST ------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if not self._authorized():
                _error(self, "未授权：请从应用窗口访问", 403)
                return
            body = _read_json(self)
            if path.startswith("/api/jobs/") and path.endswith("/cancel"):
                job_id = path.split("/")[-2]
                job = self.app.jobs.get(job_id)
                if not job:
                    _error(self, "任务不存在", 404)
                    return
                job.cancel()
                _json_response(self, {"ok": True})
                return
            if path == "/api/discover":
                self._start_discover(body)
                return
            if path == "/api/download":
                self._start_download(body)
                return
            if path == "/api/refresh":
                self._start_refresh(body)
                return
            if path == "/api/choose-folder":
                try:
                    chosen = choose_folder()
                except Exception as exc:  # noqa: BLE001
                    _error(self, f"无法选择文件夹：{exc}", 500)
                    return
                if chosen:
                    self.app.set_out_dir(chosen)
                _json_response(self, {"ok": True, "path": chosen or self.app.out_dir})
                return
            if path == "/api/reveal":
                target = str(body.get("path") or self.app.out_dir)
                try:
                    reveal_path(Path(target).expanduser())
                except Exception as exc:  # noqa: BLE001
                    _error(self, f"无法打开：{exc}", 400)
                    return
                _json_response(self, {"ok": True})
                return
            if path == "/api/open":
                url = str(body.get("url") or "")
                if not url.startswith(
                    (
                        "https://www.mohurd.gov.cn/",
                        "https://www.ndls.org.cn/",
                        "https://www.kscecs.com/",
                        "https://openstd.samr.gov.cn/",
                        "https://std.samr.gov.cn/",
                        "https://www.ccsn.org.cn/",
                        "https://ebook.chinabuilding.com.cn/",
                    )
                ):
                    _error(self, "只允许打开官方来源链接", 400)
                    return
                try:
                    open_url(url)
                except Exception as exc:  # noqa: BLE001
                    _error(self, f"无法打开链接：{exc}", 400)
                    return
                _json_response(self, {"ok": True})
                return
            if path == "/api/check-drafts":
                self._start_drafts(body)
                return
            if path == "/api/quit":
                _json_response(self, {"ok": True})
                threading.Thread(target=self._shutdown, daemon=True).start()
                return
            _error(self, "接口不存在", 404)
        except Exception as exc:  # noqa: BLE001
            log.error("POST %s 失败：%s\n%s", self.path, exc, traceback.format_exc())
            _error(self, f"服务器错误：{exc}", 500)

    def _shutdown(self) -> None:
        time.sleep(0.3)
        self.app.shutdown_event.set()
        if self.app.server:
            self.app.server.shutdown()

    def _start_discover(self, body: dict[str, Any]) -> None:
        query = str(body.get("q") or "").strip()
        if not query:
            _error(self, "请输入检索关键词")
            return
        try:
            limit = int(body.get("limit") or 60)
        except (TypeError, ValueError):
            limit = 60
        limit = max(1, min(limit, 80))
        job = Job(kind="discover")
        job.items = []

        def work(current: Job) -> None:
            def on_progress(done: int, total: int, message: str) -> None:
                current.progress = (done / max(total, 1)) * 100
                current.message = message

            result = discover(query, limit=limit, progress=on_progress)
            meta = result.get("meta", {})
            self.app.set_discovery(query, result.get("items", []), meta)
            current.result = meta
            current.message = (
                f"三站命中 {meta.get('total_hits', 0)} 条记录，"
                f"解析 {meta.get('resolved', 0)} 条，合并为 {meta.get('groups', 0)} 项标准"
            )

        self.app.jobs.start(job, work)
        _json_response(self, {"ok": True, "job_id": job.id})

    def _start_download(self, body: dict[str, Any]) -> None:
        ids = body.get("ids") or []
        if not isinstance(ids, list) or not ids:
            _error(self, "请先选择要下载的标准")
            return
        records = []
        for record_id in ids:
            record = self.app.find_record(str(record_id))
            if record:
                records.append(record)
        if not records:
            _error(self, "没有找到选中的标准")
            return
        out_dir = str(body.get("out_dir") or self.app.out_dir)
        try:
            ensure_writable_dir(Path(out_dir))
        except Exception as exc:  # noqa: BLE001
            _error(self, str(exc))
            return
        self.app.set_out_dir(out_dir)
        job = Job(kind="download")
        self.app.jobs.start(
            job,
            lambda current: run_download_job(
                current,
                records,
                out_dir,
                force=bool(body.get("force")),
            ),
        )
        _json_response(self, {"ok": True, "job_id": job.id})

    def _start_refresh(self, body: dict[str, Any]) -> None:
        full = bool(body.get("full"))
        job = Job(kind="refresh")
        job.items = []

        def work(current: Job) -> None:
            def on_progress(done: int, total: int, message: str) -> None:
                current.progress = (done / max(total, 1)) * 60
                current.message = message

            result = refresh_index(pages=3, full=full, progress=on_progress)
            current.result = {
                "count": result.get("count", 0),
                "fetched_at": result.get("fetched_at", ""),
            }
            current.message = f"公告库已更新，共 {result.get('count', 0)} 条公告"
            current.log(current.message)

        self.app.jobs.start(job, work)
        _json_response(self, {"ok": True, "job_id": job.id})

    def _start_drafts(self, body: dict[str, Any]) -> None:
        ids = body.get("ids") or []
        records = [get_record(str(record_id)) for record_id in ids]
        records = [record for record in records if record]
        if not records:
            _error(self, "请选择要核对的标准")
            return
        from .mohurd import find_revision_draft

        job = Job(kind="check")
        job.items = []

        def work(current: Job) -> None:
            total = len(records)
            found = 0
            for index, record in enumerate(records, 1):
                if current.cancelled:
                    raise DownloadCancelled()
                item = JobItem(
                    key=record.get("id", ""),
                    number=record.get("number", ""),
                    name=record.get("name", ""),
                    status="running",
                )
                current.items.append(item)
                current.message = f"核对征求意见稿 {index}/{total}"
                draft = find_revision_draft(record.get("name", ""))
                if draft:
                    found += 1
                    item.status = "done"
                    item.message = f"发现征求意见稿：{shorten(draft['title'], 80)}"
                    item.source_url = draft.get("url", "")
                else:
                    item.status = "skipped"
                    item.message = "未发现新的征求意见稿"
                current.progress = index / total * 100
            current.result = {"found": found, "total": total}
            current.message = f"核对完成：发现 {found} 个征求意见稿"

        self.app.jobs.start(job, work)
        _json_response(self, {"ok": True, "job_id": job.id})


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: AppContext):
        self.app = app
        super().__init__(address, Handler)


def _find_port(preferred: int = 8765) -> int:
    for port in [preferred] + list(range(preferred + 1, preferred + 30)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run_app(port: int = 8765, open_browser: bool = True) -> None:
    setup_logging()
    token = secrets.token_urlsafe(24)
    app = AppContext(token)
    actual_port = _find_port(port)
    server = AppServer(("127.0.0.1", actual_port), app)
    app.server = server
    url = f"http://127.0.0.1:{actual_port}/"
    log.info("启动 %s v%s：%s", APP_NAME, __version__, url)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            subprocess.Popen(["open", url])
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        log.info("程序退出")
