from __future__ import annotations

import html as html_module
import json
import re
import urllib.parse
from typing import Any, Optional

from .util import (
    decode_html,
    http_get_text,
    http_post_form,
    normalize_number,
    parse_date,
    strip_html,
    today_iso,
)

SAMR_SEARCH = "https://std.samr.gov.cn/search/stdPage"
SAMR_HOME = "https://std.samr.gov.cn/search/std"
CCSN_SEARCH = "https://www.ccsn.org.cn/newweb/api/IndexSvr.assx/pageQueryStandardBasicInfoList"
CCSN_HOME = "https://www.ccsn.org.cn/newwebEx/standardSystem"
CCSN_DETAIL = "https://www.ccsn.org.cn/newwebEx/StandardQueryDetail?guid="
OPENSTD = "https://openstd.samr.gov.cn/bzgk/gb/std_list?p.p2="
ENGINEERING_STORE = "https://www.kscecs.com/home"

SOURCE_SHORT = {
    "mohurd": "住建部",
    "samr": "国标平台",
    "ccsn": "工程标准网",
}
SOURCE_LABEL = {
    "mohurd": "住房和城乡建设部标准公告",
    "samr": "全国标准信息公共服务平台",
    "ccsn": "国家工程建设标准化信息网",
}
SOURCE_PRIORITY = {"mohurd": 0, "ccsn": 1, "samr": 2}

CODE_ORDER = [
    "GB",
    "GB/T",
    "GB/Z",
    "JGJ",
    "JGJ/T",
    "CJJ",
    "CJJ/T",
    "JG/T",
    "DB",
    "DG",
    "团体",
    "其他",
]

CODE_LABELS = {
    "GB": "GB 强制性国家标准",
    "GB/T": "GB/T 推荐性国家标准",
    "GB/Z": "GB/Z 国家标准化指导性技术文件",
    "JGJ": "JGJ 建筑行业标准",
    "JGJ/T": "JGJ/T 建筑行业推荐性标准",
    "CJJ": "CJJ 城建行业标准",
    "CJJ/T": "CJJ/T 城建行业推荐性标准",
    "JG/T": "JG/T 建筑行业产品标准",
    "DB": "DB 地方标准",
    "DG": "DG 地方工程建设标准",
    "团体": "团体标准",
    "其他": "其他标准",
}


def classify_code(number: str) -> tuple[str, str]:
    value = normalize_number(number).upper().replace(" ", "")
    if value.startswith("GB/Z"):
        code = "GB/Z"
    elif value.startswith("GB/T"):
        code = "GB/T"
    elif value.startswith("GB"):
        code = "GB"
    elif value.startswith("JGJ/T"):
        code = "JGJ/T"
    elif value.startswith("JGJ"):
        code = "JGJ"
    elif value.startswith("CJJ/T"):
        code = "CJJ/T"
    elif value.startswith("CJJ"):
        code = "CJJ"
    elif value.startswith("JG/T"):
        code = "JG/T"
    elif (
        re.match(r"^DB\d", value)
        or value.startswith("DB/")
        or value.startswith("DBJ")
    ):
        code = "DB"
    elif value.startswith("DG/TJ") or value.startswith("DGJ"):
        code = "DG"
    elif value.startswith("T/") or value.startswith("CECS"):
        code = "团体"
    else:
        code = "其他"
    return code, CODE_LABELS.get(code, "其他标准")



def _status(value: str) -> str:
    value = (value or "").strip()
    if "废止" in value or "失效" in value:
        return "obsolete"
    if "即将" in value or "未实施" in value:
        return "upcoming"
    return "current"


def _iso(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}", value):
        return value[:10]
    return parse_date(value)


def _purchase_url(number: str, source: str = "") -> str:
    number = normalize_number(number)
    if re.match(r"^(GB(?:/T)?\s*5\d{4}|JGJ|CJJ|CJ|JG)\b", number):
        return ENGINEERING_STORE
    if source == "samr" and number.startswith(("GB", "GB/T", "GB/Z")):
        return OPENSTD + urllib.parse.quote(number)
    return ""


# ---------------------------------------------------------------------------
# 全国标准信息公共服务平台 (std.samr.gov.cn)
# ---------------------------------------------------------------------------


def _samr_detail_url(tid: str, pid: str) -> str:
    if tid == "BV_HB":
        return f"https://std.samr.gov.cn/hb/search/stdHBDetailed?id={pid}"
    if tid == "BV_DB":
        return f"https://std.samr.gov.cn/db/search/stdDBDetailed?id={pid}"
    return f"https://std.samr.gov.cn/gb/search/gbDetailed?id={pid}"


def _parse_samr_page(text: str) -> tuple[list[dict[str, Any]], int, int]:
    items: list[dict[str, Any]] = []
    total = 0
    total_pages = 1
    match_total = re.search(r'Number\("(\d+)"\)\s*>\s*0', text)
    if match_total:
        total = int(match_total.group(1))
    match_pages = re.search(r"totalPages:\s*(\d+)", text)
    if match_pages:
        total_pages = int(match_pages.group(1))
    for block in re.split(r'<div class="panel panel-default post">', text)[1:]:
        match = re.search(
            r'<a href="#" tid="([^"]+)" pid="([^"]+)"[^>]*>'
            r'<span class="en-code">([^<]+)</span>(.*?)</a>',
            block,
            re.S,
        )
        if not match:
            continue
        tid, pid, number, name_html = match.groups()
        name_html = re.sub(r"</?sacinfo[^>]*>", "", name_html, flags=re.I)
        name = html_module.unescape(strip_html(name_html)).strip()
        name = re.sub(r"\s+", " ", name)
        number = normalize_number(html_module.unescape(number))
        if not number or not name:
            continue
        status_match = re.search(
            r'<span class="s-status label label-(?:info|success|danger|default)">([^<]*)</span>',
            block,
        )
        status_text = status_match.group(1).strip() if status_match else ""
        dates = re.findall(r'<time class="post-date"[^>]*>([^<]+)</time>', block)
        logo = re.search(r'<span class="line11">(.*?)</span>', block, re.S)
        standard_type = strip_html(logo.group(1)).replace("\n", "") if logo else "国家标准"
        impl_date = _iso(dates[1]) if len(dates) > 1 else ""
        publish_date = _iso(dates[0]) if dates else ""
        detail_url = _samr_detail_url(tid, pid)
        items.append(
            {
                "id": "samr-" + pid,
                "number": number,
                "base_number": number,
                "name": name,
                "base_name": name,
                "status": _status(status_text),
                "impl_date": impl_date,
                "publish_date": publish_date,
                "announce_date": publish_date,
                "standard_type": standard_type,
                "source": "samr",
                "source_label": SOURCE_LABEL["samr"],
                "source_short": SOURCE_SHORT["samr"],
                "detail_url": detail_url,
                "announcement_url": "",
                "official_url": detail_url,
                "pdf_available": False,
                "amendment": None,
                "supersedes": [],
                "purchase_url": _purchase_url(number, "samr"),
                "note": f"来源：{SOURCE_LABEL['samr']}",
            }
        )
    return items, total, total_pages


def samr_search(query: str, pages: int = 2) -> dict[str, Any]:
    query = (query or "").strip()
    result: dict[str, Any] = {
        "items": [],
        "total": 0,
        "fetched": 0,
        "error": "",
    }
    if not query:
        return result
    try:
        page_items: list[dict[str, Any]] = []
        total_pages = 1
        total = 0
        for page in range(1, pages + 1):
            url = SAMR_SEARCH + "?" + urllib.parse.urlencode(
                {"q": query, "pageNo": page, "tid": ""}
            )
            text = http_get_text(url, timeout=35, retries=2, referer=SAMR_HOME)
            items, page_total, pages_count = _parse_samr_page(text)
            if page == 1:
                total = page_total
                total_pages = pages_count
            page_items.extend(items)
            if page >= total_pages:
                break
        result.update(
            {
                "items": page_items,
                "total": total or len(page_items),
                "fetched": len(page_items),
            }
        )
        return result
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
        return result


# ---------------------------------------------------------------------------
# 国家工程建设标准化信息网 (ccsn.org.cn)
# ---------------------------------------------------------------------------


def _ccsn_query(query: str) -> str:
    compact = re.sub(r"\s+", "", query)
    if re.match(r"^[A-Z]{1,5}(?:/[A-Z])?\d", compact, re.I):
        return compact
    return query


def _parse_ccsn_item(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    number = normalize_number(str(item.get("StandardCode") or ""))
    name = html_module.unescape(str(item.get("StandardCNName") or "")).strip()
    if not number or not name:
        return None
    guid = str(item.get("Guid") or "")
    if not guid:
        return None
    impl_date = _iso(str(item.get("PerformDate") or ""))
    publish_date = _iso(str(item.get("PublishDate") or ""))
    detail_url = CCSN_DETAIL + urllib.parse.quote(guid)
    supersedes = []
    replace = item.get("ReplaceStandardCode")
    if isinstance(replace, str) and replace.strip():
        supersedes = [normalize_number(replace)]
    return {
        "id": "ccsn-" + guid,
        "number": number,
        "base_number": number,
        "name": name,
        "base_name": name,
        "status": _status(str(item.get("StandardState") or "")),
        "impl_date": impl_date,
        "publish_date": publish_date,
        "announce_date": publish_date,
        "standard_type": str(item.get("StandardType") or "工程建设标准"),
        "source": "ccsn",
        "source_label": SOURCE_LABEL["ccsn"],
        "source_short": SOURCE_SHORT["ccsn"],
        "detail_url": detail_url,
        "announcement_url": "",
        "official_url": detail_url,
        "pdf_available": False,
        "amendment": None,
        "supersedes": supersedes,
        "purchase_url": ENGINEERING_STORE,
        "note": f"来源：{SOURCE_LABEL['ccsn']}",
    }


def ccsn_search(query: str, pages: int = 2, page_size: int = 20) -> dict[str, Any]:
    query = (query or "").strip()
    result: dict[str, Any] = {
        "items": [],
        "total": 0,
        "fetched": 0,
        "error": "",
    }
    if not query:
        return result
    try:
        page_items: list[dict[str, Any]] = []
        total = 0
        for page in range(1, pages + 1):
            raw, headers, _ = http_post_form(
                CCSN_SEARCH,
                {
                    "pageIndex": page,
                    "pageSize": page_size,
                    "keyWordGuids": "",
                    "keyWord": _ccsn_query(query),
                },
                timeout=35,
                retries=2,
                referer=CCSN_HOME,
            )
            payload = json.loads(decode_html(raw, headers.get("Content-Type", "")))
            if page == 1:
                total = int(payload.get("Total") or 0)
            records = payload.get("List") or []
            for item in records:
                parsed = _parse_ccsn_item(item)
                if parsed:
                    page_items.append(parsed)
            if len(records) < page_size:
                break
        result.update(
            {
                "items": page_items,
                "total": total or len(page_items),
                "fetched": len(page_items),
            }
        )
        return result
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
        return result


# ---------------------------------------------------------------------------
# merge across sources
# ---------------------------------------------------------------------------


def _number_key(record: dict[str, Any]) -> str:
    value = normalize_number(record.get("base_number") or record.get("number") or "")
    value = re.sub(r"[（(].*?[）)]", "", value)
    value = re.sub(r"-\d{4}$", "", value)
    return value.upper()


def _year(record: dict[str, Any]) -> int:
    match = re.search(r"-(\d{4})", record.get("base_number") or record.get("number") or "")
    return int(match.group(1)) if match else 0


def _version_key(record: dict[str, Any]) -> tuple[int, str, int]:
    return (
        _year(record),
        record.get("impl_date", ""),
        -SOURCE_PRIORITY.get(record.get("source", ""), 9),
    )


def merge_sources(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    order: dict[str, int] = {}
    for record in records:
        key = _number_key(record)
        if not key:
            key = re.sub(r"\s+", "", record.get("name", "")).upper()
        if not key:
            continue
        if key not in groups:
            groups[key] = []
            order[key] = len(order)
        groups[key].append(record)

    merged_items: list[dict[str, Any]] = []
    for key, group in groups.items():
        latest = max(group, key=_version_key)
        merged = dict(latest)
        latest_year = _year(latest)

        mohurd_same = [
            record
            for record in group
            if record.get("source") == "mohurd"
            and _year(record) == latest_year
            and (
                record.get("pdf_available")
                or (record.get("amendment") or {}).get("pdf_available")
            )
        ]
        donor = max(mohurd_same, key=_version_key) if mohurd_same else None
        if donor and donor is not latest:
            merged.update(
                {
                    "base_number": donor.get("base_number", merged.get("base_number")),
                    "base_name": donor.get("base_name", merged.get("base_name")),
                    "base_announcement_url": donor.get("base_announcement_url", ""),
                    "amendment": donor.get("amendment"),
                    "pdf_available": bool(donor.get("pdf_available")),
                    "pdf_note": donor.get("pdf_note", ""),
                    "announcement_url": donor.get("announcement_url", ""),
                }
            )

        sources = []
        seen = set()
        for record in group:
            source_key = (
                record.get("source"),
                record.get("detail_url") or record.get("announcement_url") or record.get("number"),
            )
            if source_key in seen:
                continue
            seen.add(source_key)
            sources.append(
                {
                    "source": record.get("source"),
                    "label": record.get("source_label"),
                    "short": record.get("source_short"),
                    "url": record.get("detail_url") or record.get("announcement_url", ""),
                    "type": record.get("standard_type", ""),
                    "status": record.get("status", ""),
                    "pdf": bool(record.get("pdf_available")),
                }
            )
        merged["sources"] = sources
        merged["source_labels"] = list(
            dict.fromkeys(
                source["short"] for source in sources if source.get("short")
            )
        )
        merged["source_count"] = len(sources)
        merged["source"] = "multi" if len(sources) > 1 else latest.get("source", "")
        merged["source_label"] = " · ".join(merged["source_labels"])
        merged["code_type"], merged["code_label"] = classify_code(merged.get("number", ""))
        other_detail = next(
            (
                source.get("url", "")
                for source in sources
                if source.get("source") != "mohurd" and source.get("url")
            ),
            "",
        )
        merged["detail_url"] = (
            other_detail
            or latest.get("detail_url")
            or latest.get("official_url", "")
        )
        merged["official_url"] = merged["detail_url"]
        merged["purchase_url"] = (
            latest.get("purchase_url")
            or next((record.get("purchase_url") for record in group if record.get("purchase_url")), "")
        )
        if len(sources) > 1:
            merged["note"] = (
                (merged.get("note", "") + "；" if merged.get("note") else "")
                + "多源核对：" + " · ".join(merged["source_labels"])
            )
        versions = {
            (_year(record), record.get("impl_date", ""))
            for record in group
        }
        merged["hidden_versions"] = max(0, len(versions) - 1)
        merged_items.append(merged)

    merged_items.sort(key=lambda record: order.get(_number_key(record), 10_000))
    return merged_items
