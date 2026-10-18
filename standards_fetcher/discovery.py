from __future__ import annotations

import concurrent.futures
import re
import urllib.parse
from typing import Any, Callable, Optional

from .mohurd import (
    choose_pdf_attachment,
    fetch_announcement,
    index_search,
    search_site,
)
from .sources import SOURCE_SHORT, ccsn_search, merge_sources, samr_search
from .util import normalize_number, now_iso, sha1_text, today_iso

ENGINEERING_STORE = "https://www.kscecs.com/home"
OPENSTD = "https://openstd.samr.gov.cn/bzgk/gb/std_list?p.p2="
DEFAULT_LIMIT = 60


def _norm(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def _group_key(name: str) -> str:
    value = _norm(name)
    return re.sub(r"(规范|标准|规程)$", "", value)


def _year(value: str) -> int:
    match = re.search(r"-(\d{4})", value or "")
    return int(match.group(1)) if match else 0


def purchase_url(number: str) -> str:
    number = normalize_number(number)
    if re.match(r"^(GB(?:/T)?\s*5\d{4}|JGJ|CJJ|CJ|JG)\b", number):
        return ENGINEERING_STORE
    return OPENSTD + urllib.parse.quote(number)


def _version_key(record: dict[str, Any]) -> tuple[int, str, str]:
    return (
        _year(record.get("base_number") or record.get("number", "")),
        record.get("impl_date", ""),
        record.get("announce_date", ""),
    )


def _record_from_announcement(
    item: dict[str, str], parsed: dict[str, Any]
) -> Optional[dict[str, Any]]:
    title = parsed.get("title") or item.get("title", "")
    if any(word in title for word in ("征求意见", "英文版", "产品标准", "标准图集")):
        return None
    name = parsed.get("name") or ""
    number = normalize_number(parsed.get("number") or "")
    if not name or not number:
        return None
    impl_date = parsed.get("impl_date") or ""
    attachment = choose_pdf_attachment(parsed.get("attachments", []), name)
    is_revision = bool(parsed.get("is_revision"))
    record = {
        "id": "disc-" + sha1_text(item["url"])[:16],
        "family": "discovered",
        "number": number,
        "base_number": number,
        "name": name,
        "base_name": name,
        "category": "discovered",
        "category_name": "公告检索",
        "status": "upcoming" if impl_date and impl_date > today_iso() else "current",
        "impl_date": impl_date,
        "announce_date": item.get("date", ""),
        "announcement_url": item["url"],
        "base_announcement_url": item["url"],
        "pdf_available": bool(attachment) and not is_revision,
        "pdf_note": "" if attachment else "该公告未附 PDF 全文；请查看官方公告或通过正规渠道购买。",
        "amendment": None,
        "supersedes": [
            normalize_number(value.get("number", ""))
            for value in parsed.get("supersedes", [])
            if value.get("number")
        ],
        "draft": None,
        "source": "mohurd",
        "source_label": "住房和城乡建设部",
        "official_url": item["url"],
        "purchase_url": purchase_url(number),
        "verified_at": today_iso(),
        "note": "按住建部公告标题检索",
        "is_revision": is_revision,
        "is_product": "产品标准" in title,
        "attachment_count": len(parsed.get("attachments", [])),
    }
    if is_revision:
        record["pdf_available"] = False
        record["pdf_note"] = "该公告仅公开局部修订条文；母标准全文可能需另行购买。"
    return record


def _merge_group(group: list[dict[str, Any]]) -> dict[str, Any]:
    bases = [record for record in group if not record.get("is_revision")]
    revisions = [record for record in group if record.get("is_revision")]
    if bases:
        base = max(bases, key=_version_key)
        record = dict(base)
        record.pop("is_revision", None)
        if revisions:
            revision = max(revisions, key=_version_key)
            if (revision.get("impl_date") or "") >= (base.get("impl_date") or ""):
                year = (revision.get("impl_date") or "")[:4]
                display_number = base["number"]
                if revision.get("number") and revision["number"] != base["number"]:
                    display_number = revision["number"]
                record.update(
                    {
                        "number": f"{display_number}（{year}局部修订）" if year else display_number,
                        "name": revision.get("name") or base["name"],
                        "impl_date": revision.get("impl_date") or base["impl_date"],
                        "announcement_url": revision["announcement_url"],
                        "status": "upcoming"
                        if (revision.get("impl_date") or "") > today_iso()
                        else "current",
                        "amendment": {
                            "label": f"{year}局部修订" if year else "局部修订",
                            "number": revision.get("number") or base["number"],
                            "name": revision.get("name") or base["name"],
                            "impl_date": revision.get("impl_date", ""),
                            "announcement_url": revision["announcement_url"],
                            "pdf_available": bool(revision.get("attachment_count")),
                            "partial": True,
                            "note": "局部修订条文；需与母标准配套使用。",
                        },
                        "note": "已自动配对母标准与局部修订条文。",
                    }
                )
        record["hidden_versions"] = max(0, len(group) - 1)
        if record["hidden_versions"]:
            record["note"] = (
                record.get("note", "")
                + f" 自动隐藏 {record['hidden_versions']} 个旧版/重复公告。"
            ).strip()
        return record

    # 只检索到局部修订公告：保留它，但明确标注母标准缺失。
    revision = max(revisions, key=_version_key)
    record = dict(revision)
    record["is_revision"] = False
    original_pdf = bool(revision.get("attachment_count"))
    record.update(
        {
            "pdf_available": False,
            "pdf_note": "仅检索到局部修订条文，母标准全文未在公告库公开。",
            "amendment": {
                "label": (revision.get("impl_date") or "")[:4] + "局部修订"
                if revision.get("impl_date")
                else "局部修订",
                "number": revision.get("number", ""),
                "name": revision.get("name", ""),
                "impl_date": revision.get("impl_date", ""),
                "announcement_url": revision["announcement_url"],
                "pdf_available": original_pdf,
                "partial": True,
                "note": "仅局部修订条文，母标准需另行获取。",
            },
            "note": "仅检索到局部修订公告；已保留可下载的修订条文。",
        }
    )
    return record


def _mohurd_records(
    query: str,
    *,
    limit: int,
    number_lookup: bool,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> tuple[list[dict[str, Any]], int, int, str]:
    hits = index_search(
        query,
        include_products=False,
        require_all=False,
        limit=1000,
    )
    if not hits and number_lookup:
        compact = re.sub(r"\s+", "", query)
        try:
            site_hits = search_site(compact)
        except Exception:
            site_hits = []
        hits = [
            item
            for item in site_hits
            if "发布" in item.get("title", "")
            and "标准" in item.get("title", "")
            and not any(
                word in item.get("title", "")
                for word in ("声明", "答复", "征求意见", "英文版")
            )
        ]
    if not hits:
        try:
            from .catalog import search as catalog_search

            curated = catalog_search(re.sub(r"\s+", "", query))
        except Exception:
            curated = []
        if curated:
            items = [dict(record) for record in curated]
            for record in items:
                record["source"] = "mohurd"
                record["source_label"] = "住房和城乡建设部"
                record["source_short"] = SOURCE_SHORT["mohurd"]
            return items, len(items), len(items), ""

    total_hits = len(hits)
    candidates = hits[:limit]
    parsed_records: list[dict[str, Any]] = []
    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(fetch_announcement, item["url"], force=False): item
            for item in candidates
        }
        for future in concurrent.futures.as_completed(futures):
            item = futures[future]
            completed += 1
            try:
                parsed = future.result()
                if not parsed.get("is_draft") and not parsed.get("is_english"):
                    record = _record_from_announcement(item, parsed)
                    if record:
                        if number_lookup:
                            query_digits = re.findall(r"\d+", query)
                            if not all(
                                digit in record.get("number", "")
                                for digit in query_digits
                            ):
                                continue
                        record["source_short"] = SOURCE_SHORT["mohurd"]
                        parsed_records.append(record)
            except Exception:
                pass
            if progress:
                progress(
                    completed,
                    max(len(candidates), 1),
                    f"解析住建部公告 {completed}/{max(len(candidates), 1)}",
                )

    position = {item["url"]: index for index, item in enumerate(candidates)}
    parsed_records.sort(key=lambda record: position.get(record.get("announcement_url", ""), 10_000))

    groups: dict[str, list[dict[str, Any]]] = {}
    order: dict[str, int] = {}
    for record in parsed_records:
        key = _group_key(record.get("base_name") or record.get("name", ""))
        if not key:
            continue
        if key not in groups:
            groups[key] = []
            order[key] = len(order)
        groups[key].append(record)

    items: list[dict[str, Any]] = []
    for key, group in groups.items():
        merged = _merge_group(group)
        merged["id"] = merged.get("id") or ("disc-" + sha1_text(key)[:16])
        merged.setdefault("source", "mohurd")
        merged.setdefault("source_label", "住房和城乡建设部")
        merged["source_short"] = SOURCE_SHORT["mohurd"]
        items.append(merged)
    items.sort(
        key=lambda record: order.get(
            _group_key(record.get("base_name") or record.get("name", "")), 10_000
        )
    )
    return items, total_hits, len(candidates), ""


def discover(
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> dict[str, Any]:
    query = (query or "").strip()
    if not query:
        raise ValueError("请输入检索关键词")
    limit = max(1, min(int(limit), 80))
    compact = re.sub(r"\s+", "", query)
    number_lookup = bool(re.match(r"^[A-Z]{1,5}(?:/[A-Z])?\d", compact, re.I))

    def mohurd_progress(done: int, total: int, message: str) -> None:
        if progress:
            progress(int(done / max(total, 1) * 50), 100, message)

    mohurd_items, mohurd_total, mohurd_resolved, mohurd_error = _mohurd_records(
        query,
        limit=limit,
        number_lookup=number_lookup,
        progress=mohurd_progress,
    )

    if progress:
        progress(55, 100, "检索全国标准信息公共服务平台…")
    samr_result = samr_search(query, pages=2)
    if progress:
        progress(72, 100, "检索国家工程建设标准化信息网…")
    ccsn_result = ccsn_search(query, pages=2)
    if progress:
        progress(88, 100, "合并多源检索结果…")

    merged = merge_sources(
        mohurd_items + ccsn_result.get("items", []) + samr_result.get("items", [])
    )
    # 可直接下载的条目优先展示，其余保持原相关度顺序。
    merged.sort(
        key=lambda record: 0
        if record.get("pdf_available")
        or (record.get("amendment") or {}).get("pdf_available")
        else 1
    )
    downloadable_count = sum(
        1
        for record in merged
        if record.get("pdf_available")
        or (record.get("amendment") or {}).get("pdf_available")
    )
    source_counts = {
        "mohurd": mohurd_total,
        "ccsn": ccsn_result.get("total", 0),
        "samr": samr_result.get("total", 0),
    }
    source_fetched = {
        "mohurd": mohurd_resolved,
        "ccsn": ccsn_result.get("fetched", 0),
        "samr": samr_result.get("fetched", 0),
    }
    total_hits = sum(source_counts.values())
    resolved = sum(source_fetched.values())
    errors = [
        error
        for error in (mohurd_error, ccsn_result.get("error", ""), samr_result.get("error", ""))
        if error
    ]
    meta = {
        "query": query,
        "total_hits": total_hits,
        "resolved": resolved,
        "groups": len(merged),
        "downloadable": downloadable_count,
        "shown": min(len(merged), limit),
        "truncated": any(source_counts[key] > source_fetched[key] for key in source_counts),
        "source_counts": source_counts,
        "source_fetched": source_fetched,
        "source": "住房和城乡建设部 / 全国标准信息公共服务平台 / 国家工程建设标准化信息网",
        "errors": errors,
        "generated_at": now_iso(),
    }
    return {"items": merged[:limit], "meta": meta}


def build_purchase_links(records: list[dict[str, Any]]) -> None:
    for record in records:
        if not record.get("purchase_url"):
            record["purchase_url"] = purchase_url(record.get("number", ""))
