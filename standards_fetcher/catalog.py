from __future__ import annotations

import re
from typing import Any, Optional

from .util import (
    app_data_dir,
    data_file,
    load_json,
    normalize_number,
    now_iso,
    save_json,
    today_iso,
)

_CATALOG_CACHE: Optional[dict[str, Any]] = None
_RECORDS_CACHE: Optional[list[dict[str, Any]]] = None


def _user_records_path():
    return app_data_dir() / "standards.json"


def _bundled_records() -> list[dict[str, Any]]:
    value = load_json(data_file("standards.json"), [])
    return value if isinstance(value, list) else []


def _merge_records(
    bundled: list[dict[str, Any]], overlay: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_id = {str(record.get("id")): record for record in bundled if record.get("id")}
    for record in overlay:
        if record.get("id"):
            by_id[str(record["id"])] = record
    order = {str(record.get("id")): index for index, record in enumerate(bundled)}
    result = list(by_id.values())
    result.sort(key=lambda record: order.get(str(record.get("id")), 10_000))
    return result


def reload_records() -> list[dict[str, Any]]:
    global _RECORDS_CACHE
    _RECORDS_CACHE = None
    return load_records()


def load_collections() -> dict[str, Any]:
    global _CATALOG_CACHE
    if _CATALOG_CACHE is None:
        value = load_json(data_file("collections.json"), {})
        if not isinstance(value, dict):
            value = {}
        _CATALOG_CACHE = value
    return _CATALOG_CACHE


def load_records() -> list[dict[str, Any]]:
    global _RECORDS_CACHE
    if _RECORDS_CACHE is None:
        bundled = _bundled_records()
        overlay = load_json(_user_records_path(), [])
        if not isinstance(overlay, list):
            overlay = []
        value = _merge_records(bundled, overlay)
        for record in value:
            record.setdefault("status", "current")
            record.setdefault("supersedes", [])
            record.setdefault("amendment", None)
            record.setdefault("draft", None)
            record.setdefault("pdf_available", False)
            record.setdefault("verified_at", load_collections().get("snapshot", ""))
            if record.get("impl_date", "") > today_iso():
                record["status"] = "upcoming"
        _RECORDS_CACHE = value
    return _RECORDS_CACHE


def categories() -> list[dict[str, Any]]:
    return load_collections().get("categories", [])


def stats() -> dict[str, int]:
    records = load_records()
    return {
        "total": len(records),
        "downloadable": sum(
            1
            for record in records
            if record.get("pdf_available")
            or (record.get("amendment") or {}).get("pdf_available")
        ),
        "unavailable": sum(
            1
            for record in records
            if not record.get("pdf_available")
            and not (record.get("amendment") or {}).get("pdf_available")
        ),
        "with_amendment": sum(1 for record in records if record.get("amendment")),
        "with_draft": sum(1 for record in records if record.get("draft")),
    }


def _search_blob(record: dict[str, Any]) -> str:
    values = [
        record.get("number", ""),
        record.get("base_number", ""),
        record.get("name", ""),
        record.get("base_name", ""),
        record.get("family", ""),
        record.get("category_name", ""),
        record.get("note", ""),
    ]
    amendment = record.get("amendment") or {}
    values.extend([amendment.get("number", ""), amendment.get("name", "")])
    return re.sub(r"\s+", "", " ".join(str(value) for value in values if value)).lower()


def search(
    query: str = "",
    category: str = "all",
    *,
    include_unavailable: bool = True,
) -> list[dict[str, Any]]:
    records = load_records()
    if category and category != "all":
        records = [record for record in records if record.get("category") == category]
    query = (query or "").strip()
    if query:
        normalized = re.sub(r"\s+", "", query).lower()
        tokens = [token.lower() for token in re.split(r"\s+", query) if token]
        digits = re.findall(r"\d+", normalized)
        scored: list[tuple[int, dict[str, Any]]] = []
        for record in records:
            blob = _search_blob(record)
            number = re.sub(r"\s+", "", normalize_number(record.get("number", ""))).lower()
            base = re.sub(r"\s+", "", normalize_number(record.get("base_number", ""))).lower()
            score = 0
            if digits:
                # 标准号检索：查询中的数字必须出现在标准号里，避免“GB”把全部结果带出来。
                if not all(digit in (number + base) for digit in digits):
                    continue
                if normalized in (number + base):
                    score += 100
                elif number.startswith(normalized) or base.startswith(normalized):
                    score += 60
                else:
                    score += 20
            else:
                if normalized and normalized in blob:
                    score += 30
                specific = [token for token in tokens if token not in {"gb", "gbt", "jgj", "cjj", "标准", "规范", "t"}]
                if not specific:
                    specific = tokens
                if not any(token in blob for token in specific):
                    continue
                for token in specific:
                    if token in blob:
                        score += 6
            scored.append((score, record))
        scored.sort(key=lambda pair: (pair[0], pair[1].get("impl_date", "")), reverse=True)
        records = [record for _, record in scored]
    if not include_unavailable:
        records = [
            record
            for record in records
            if record.get("pdf_available")
            or (record.get("amendment") or {}).get("pdf_available")
        ]
    return records


def get_record(record_id: str) -> Optional[dict[str, Any]]:
    for record in load_records():
        if record.get("id") == record_id:
            return record
    return None


def current_records_with_drafts() -> list[dict[str, Any]]:
    return [record for record in load_records() if record.get("draft")]


def latest_by_category() -> dict[str, int]:
    result: dict[str, int] = {}
    for record in load_records():
        key = record.get("category", "all")
        result[key] = result.get(key, 0) + 1
    return result


# ---------------------------------------------------------------------------
# reconcile catalog with newly fetched announcement index
# ---------------------------------------------------------------------------


def _norm(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def _year_of(number: str) -> int:
    match = re.search(r"-(\d{4})", number or "")
    return int(match.group(1)) if match else 0


def _attachment_available(parsed: dict[str, Any], name: str) -> bool:
    from .mohurd import choose_pdf_attachment

    return choose_pdf_attachment(parsed.get("attachments", []), name) is not None


def reconcile_with_index(
    index: dict[str, Any],
    *,
    progress: Optional[Any] = None,
) -> dict[str, int]:
    """把公告索引里比目录更新的版本合并到用户目录。

    只处理同名标准，并且要求公告正文能解析出标准号/实施日期，避免把新闻或
    征求意见稿误当成正式版本。更新写入 Application Support，不修改应用包内数据。
    """
    from .mohurd import fetch_announcement

    records = load_records()
    items = index.get("items", []) if isinstance(index, dict) else []
    changed: list[dict[str, Any]] = []
    updated = 0
    checked = 0
    for record in records:
        if progress:
            progress(checked, len(records), f"核对版本 {checked + 1}/{len(records)}")
        checked += 1
        base_name = _norm(record.get("base_name") or record.get("name", ""))
        if not base_name:
            continue
        known_urls = {
            record.get("announcement_url", ""),
            record.get("base_announcement_url", ""),
            (record.get("amendment") or {}).get("announcement_url", ""),
        }
        candidates = []
        for item in items:
            title = _norm(item.get("title", ""))
            if base_name not in title:
                continue
            if any(word in title for word in ("征求意见", "英文版", "产品标准", "废止公告")):
                continue
            if item.get("url") in known_urls:
                continue
            if (item.get("date") or "") <= (record.get("announce_date") or ""):
                continue
            candidates.append(item)
        for item in sorted(candidates, key=lambda value: value.get("date", ""))[-3:]:
            try:
                parsed = fetch_announcement(item["url"], force=False)
            except Exception:
                continue
            if parsed.get("is_draft") or parsed.get("is_english"):
                continue
            impl_date = parsed.get("impl_date") or ""
            if not impl_date:
                continue
            if parsed.get("is_revision"):
                if impl_date <= (record.get("impl_date") or ""):
                    continue
                attachment_ok = _attachment_available(parsed, record.get("name", ""))
                number = parsed.get("number") or record.get("base_number") or record.get("number", "")
                label = f"{impl_date[:4]}局部修订"
                record["amendment"] = {
                    "label": label,
                    "number": number,
                    "name": parsed.get("name") or record.get("base_name") or record.get("name", ""),
                    "impl_date": impl_date,
                    "announcement_url": item["url"],
                    "pdf_available": attachment_ok,
                    "partial": True,
                    "note": "根据住建部新公告自动识别；通常仅包含局部修订条文。",
                }
                record["impl_date"] = impl_date
                record["status"] = "upcoming" if impl_date > today_iso() else "current"
                record["announcement_url"] = item["url"]
                record["note"] = "目录已根据新公告自动加入局部修订。"
                record["verified_at"] = today_iso()
                changed.append(record)
                updated += 1
                break
            new_number = parsed.get("number") or ""
            if not new_number:
                continue
            if _year_of(new_number) <= _year_of(record.get("base_number") or record.get("number", "")):
                continue
            old_number = record.get("number", "")
            record.update(
                {
                    "number": new_number,
                    "base_number": new_number,
                    "name": parsed.get("name") or record.get("name", ""),
                    "base_name": parsed.get("name") or record.get("base_name", ""),
                    "impl_date": impl_date,
                    "status": "upcoming" if impl_date > today_iso() else "current",
                    "announce_date": item.get("date", ""),
                    "announcement_url": item["url"],
                    "base_announcement_url": item["url"],
                    "pdf_available": _attachment_available(parsed, parsed.get("name", "")),
                    "amendment": None,
                    "draft": None,
                    "supersedes": [old_number] if old_number else [],
                    "note": "目录已根据住建部新公告自动更新。",
                    "verified_at": today_iso(),
                }
            )
            changed.append(record)
            updated += 1
            break
    if changed:
        existing = load_json(_user_records_path(), [])
        if not isinstance(existing, list):
            existing = []
        save_json(_user_records_path(), _merge_records(existing, changed))
        reload_records()
    return {"checked": checked, "updated": updated}


def reset_user_catalog() -> None:
    path = _user_records_path()
    if path.exists():
        path.unlink()
    reload_records()
