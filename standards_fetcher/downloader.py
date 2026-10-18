from __future__ import annotations

import csv
import io
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .mohurd import choose_pdf_attachment, fetch_announcement
from .util import (
    DownloadCancelled,
    download_to,
    ensure_writable_dir,
    human_size,
    now_iso,
    safe_filename,
    sha256_file,
    shorten,
)


@dataclass
class JobItem:
    key: str
    number: str
    name: str
    status: str = "pending"  # pending, running, done, skipped, unavailable, error
    message: str = ""
    path: str = ""
    bytes: int = 0
    total_bytes: int = 0
    sha256: str = ""
    source_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "number": self.number,
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "path": self.path,
            "bytes": self.bytes,
            "total_bytes": self.total_bytes,
            "sha256": self.sha256,
            "source_url": self.source_url,
        }


@dataclass
class Job:
    kind: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "queued"  # queued, running, done, error, cancelled
    message: str = "等待开始"
    progress: float = 0.0
    created_at: str = field(default_factory=now_iso)
    finished_at: str = ""
    items: list[JobItem] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def log(self, message: str) -> None:
        with self._lock:
            self.logs.append(f"{time.strftime('%H:%M:%S')} {message}")
            self.logs = self.logs[-120:]

    def cancel(self) -> None:
        self._cancel.set()
        self.log("收到取消请求，正在停止…")

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def to_dict(self, include_logs: bool = True) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "status": self.status,
                "message": self.message,
                "progress": round(self.progress, 1),
                "created_at": self.created_at,
                "finished_at": self.finished_at,
                "error": self.error,
                "items": [item.to_dict() for item in self.items],
                "logs": list(self.logs) if include_logs else [],
                "result": self.result,
            }


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def add(self, job: Job) -> Job:
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def start(self, job: Job, target: Callable[[Job], None]) -> Job:
        self.add(job)

        def runner() -> None:
            job.status = "running"
            job.message = "正在处理…"
            job.log("任务开始")
            try:
                target(job)
            except DownloadCancelled:
                job.status = "cancelled"
                job.message = "已取消"
                job.log("任务已取消")
            except Exception as exc:  # noqa: BLE001
                job.status = "error"
                job.error = str(exc)
                job.message = f"失败：{exc}"
                job.log(f"任务失败：{exc}")
            finally:
                job.finished_at = now_iso()
                if job.status == "running":
                    job.status = "done"
                    if not job.message or job.message in ("正在处理…", "正在启动…"):
                        job.message = "已完成"
                job.progress = 100.0 if job.status == "done" else job.progress
                job.log("任务结束")

        thread = threading.Thread(target=runner, daemon=True, name=f"job-{job.id}")
        thread.start()
        return job


# ---------------------------------------------------------------------------
# download logic
# ---------------------------------------------------------------------------


def _source_filename(
    number: str,
    name: str,
    *,
    amendment_label: str = "",
) -> str:
    number = safe_filename(number, 60)
    name = safe_filename(name, 100)
    label = safe_filename(amendment_label, 30) if amendment_label else ""
    suffix = f"（{label}）" if label else ""
    return safe_filename(f"{number} {name}{suffix}.pdf", 180)


def _record_sources(record: dict[str, Any]) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    if record.get("pdf_available") and record.get("base_announcement_url"):
        sources.append(
            {
                "kind": "base",
                "url": record["base_announcement_url"],
                "number": record.get("base_number") or record.get("number", ""),
                "name": record.get("base_name") or record.get("name", ""),
                "label": "",
            }
        )
    amendment = record.get("amendment") or {}
    if amendment.get("pdf_available") and amendment.get("announcement_url"):
        sources.append(
            {
                "kind": "amendment",
                "url": amendment["announcement_url"],
                "number": amendment.get("number") or record.get("base_number") or record.get("number", ""),
                "name": amendment.get("name") or record.get("name", ""),
                "label": amendment.get("label") or "局部修订",
            }
        )
    return sources


def _write_manifest(out_dir: Path, records: list[dict[str, Any]], items: list[JobItem]) -> None:
    by_key = {item.key: item for item in items}
    json_path = out_dir / "_标准清单.json"
    existing: list[dict[str, Any]] = []
    try:
        loaded = json.loads(json_path.read_text(encoding="utf-8"))
        if isinstance(loaded, list):
            existing = loaded
    except (OSError, ValueError):
        existing = []
    merged: dict[str, dict[str, Any]] = {
        str(row.get("id")): row for row in existing if row.get("id")
    }
    for record in records:
        item = by_key.get(record.get("id"))
        row = {
            "id": record.get("id"),
            "标准号": record.get("number", ""),
            "名称": record.get("name", ""),
            "实施日期": record.get("impl_date", ""),
            "状态": "现行" if record.get("status") != "upcoming" else "即将实施",
            "是否含局部修订": "是" if record.get("amendment") else "否",
            "官方PDF": "可下载" if record.get("pdf_available") else "未公开",
            "保存状态": item.status if item else "未处理",
            "保存路径": item.path if item else "",
            "文件大小": human_size(item.bytes) if item and item.bytes else "",
            "SHA256": item.sha256 if item else "",
            "公告来源": record.get("announcement_url") or record.get("official_url", ""),
            "备注": record.get("pdf_note") or record.get("note", ""),
        }
        merged[str(record.get("id"))] = row
    manifest = list(merged.values())
    try:
        json_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        csv_path = out_dir / "_标准清单.csv"
        with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(manifest[0].keys()) if manifest else [])
            if manifest:
                writer.writeheader()
                writer.writerows(manifest)
        _write_report(out_dir, records, items)
    except OSError:
        pass


def _write_report(out_dir: Path, records: list[dict[str, Any]], items: list[JobItem]) -> None:
    done = sum(1 for item in items if item.status in ("done", "skipped"))
    unavailable = sum(1 for item in items if item.status == "unavailable")
    errors = sum(1 for item in items if item.status == "error")
    lines = [
        "# 标准下载报告",
        "",
        f"- 生成时间：{now_iso()}",
        f"- 目标文件夹：`{out_dir}`",
        f"- 目录条目：{len(records)}",
        f"- 已保存/已存在：{done}",
        f"- 官方未公开：{unavailable}",
        f"- 失败：{errors}",
        "",
        "## 来源说明",
        "",
        "本工具只从住房和城乡建设部门户网站公开的“标准公告”附件下载 PDF。",
        "版权归标准出版单位所有，下载文件仅供个人学习、研究或项目内部查阅，请勿用于商业分发。",
        "官方未公开全文的标准，请通过中国建筑出版传媒、国家标准馆等正规渠道购买。",
        "",
        "## 明细",
        "",
        "| 标准号 | 名称 | 状态 | 文件 |",
        "| --- | --- | --- | --- |",
    ]
    by_key = {item.key: item for item in items}
    for record in records:
        item = by_key.get(record.get("id"))
        if not item:
            continue
        status = {
            "done": "已下载",
            "skipped": "已存在",
            "unavailable": "未公开",
            "error": "失败",
        }.get(item.status, item.status)
        path = f"`{Path(item.path).name}`" if item.path else item.message
        lines.append(
            f"| {record.get('number','')} | {record.get('name','')} | {status} | {path} |"
        )
    (out_dir / "_下载报告.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def download_records(
    records: list[dict[str, Any]],
    out_dir: str,
    *,
    force: bool = False,
    progress: Optional[Callable[[float, str], None]] = None,
) -> dict[str, Any]:
    """同步下载，供 CLI 使用；Web 端用 Job 包装。"""
    out = ensure_writable_dir(Path(out_dir))
    items: list[JobItem] = []
    for record in records:
        sources = _record_sources(record)
        if not sources:
            item = JobItem(
                key=record.get("id", ""),
                number=record.get("number", ""),
                name=record.get("name", ""),
                status="unavailable",
                message=record.get("pdf_note") or "官方未公开 PDF 全文",
                source_url=record.get("official_url") or record.get("announcement_url", ""),
            )
            items.append(item)
            continue
        for source in sources:
            item = JobItem(
                key=record.get("id", ""),
                number=record.get("number", ""),
                name=record.get("name", ""),
                status="running",
            )
            items.append(item)
            try:
                _download_one(source, out, item, force=force)
            except Exception as exc:  # noqa: BLE001
                item.status = "error"
                item.message = shorten(str(exc), 160)
    _write_manifest(out, records, items)
    return {"items": [item.to_dict() for item in items], "out_dir": str(out)}


def _download_one(
    source: dict[str, str],
    out_dir: Path,
    item: JobItem,
    *,
    force: bool,
    job: Optional[Job] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> None:
    if job and job.cancelled:
        raise DownloadCancelled()
    announcement = fetch_announcement(source["url"], force=True)
    attachment = choose_pdf_attachment(announcement.get("attachments", []), source["name"])
    if not attachment:
        item.status = "unavailable"
        item.message = "公告页未找到可下载的 PDF 附件"
        return
    filename = _source_filename(
        source["number"], source["name"], amendment_label=source.get("label", "")
    )
    destination = out_dir / filename
    item.path = str(destination)
    item.source_url = source["url"]
    if destination.exists() and not force:
        item.status = "skipped"
        item.bytes = destination.stat().st_size
        item.message = "文件已存在，已跳过"
        if job:
            job.log(f"跳过：{filename}")
        return
    if job:
        job.log(f"下载：{filename}")
    started = time.time()

    def on_progress(received: int, total: int) -> None:
        item.bytes = received
        item.total_bytes = total
        if progress_cb:
            progress_cb(received, total)

    download_to(
        attachment["url"],
        destination,
        referer=source["url"],
        timeout=240,
        retries=3,
        max_bytes=350 * 1024 * 1024,
        progress=on_progress,
        cancelled=(lambda: job.cancelled) if job else None,
    )
    with destination.open("rb") as fh:
        magic = fh.read(8)
    size = destination.stat().st_size
    if not magic.startswith(b"%PDF"):
        destination.unlink(missing_ok=True)
        raise ValueError("下载内容不是 PDF（可能是登录页或错误页面）")
    if size < 8 * 1024:
        destination.unlink(missing_ok=True)
        raise ValueError(f"PDF 文件异常（仅 {human_size(size)}）")
    item.status = "done"
    item.bytes = size
    item.sha256 = sha256_file(destination)
    item.message = f"已保存（{human_size(size)}，{time.time() - started:.1f}s）"
    if job:
        job.log(f"完成：{filename}（{human_size(size)}）")


def run_download_job(
    job: Job,
    records: list[dict[str, Any]],
    out_dir: str,
    *,
    force: bool = False,
) -> None:
    out = ensure_writable_dir(Path(out_dir))
    job.result["out_dir"] = str(out)
    items: list[JobItem] = []
    source_total = sum(len(_record_sources(record)) for record in records)
    completed = 0
    for record in records:
        if job.cancelled:
            raise DownloadCancelled()
        sources = _record_sources(record)
        if not sources:
            item = JobItem(
                key=record.get("id", ""),
                number=record.get("number", ""),
                name=record.get("name", ""),
                status="unavailable",
                message=record.get("pdf_note") or "官方未公开 PDF 全文",
                source_url=record.get("official_url") or record.get("announcement_url", ""),
            )
            items.append(item)
            job.items.append(item)
            job.log(f"未公开：{record.get('number','')} {record.get('name','')}")
            continue
        for source in sources:
            if job.cancelled:
                raise DownloadCancelled()
            item = JobItem(
                key=record.get("id", ""),
                number=record.get("number", ""),
                name=record.get("name", ""),
                status="running",
            )
            items.append(item)
            job.items.append(item)
            job.message = f"正在下载：{record.get('number','')} {record.get('name','')}"
            try:
                _download_one(
                    source,
                    out,
                    item,
                    force=force,
                    job=job,
                )
            except DownloadCancelled:
                item.status = "cancelled"
                item.message = "已取消"
                raise
            except Exception as exc:  # noqa: BLE001
                item.status = "error"
                item.message = shorten(str(exc), 200)
                job.log(f"失败：{record.get('number','')} {record.get('name','')}：{exc}")
            completed += 1
            job.progress = (completed / max(source_total, 1)) * 100
    _write_manifest(out, records, items)
    job.result.update(
        {
            "done": sum(1 for item in items if item.status in ("done", "skipped")),
            "unavailable": sum(1 for item in items if item.status == "unavailable"),
            "errors": sum(1 for item in items if item.status == "error"),
            "total": len(items),
        }
    )
    job.message = (
        f"完成：保存 {job.result['done']} 个，未公开 {job.result['unavailable']} 个，"
        f"失败 {job.result['errors']} 个"
    )
