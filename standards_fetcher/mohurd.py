from __future__ import annotations

import concurrent.futures
import html as html_module
import json
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Optional

from .util import (
    cache_dir,
    data_file,
    decode_html,
    http_get,
    http_get_text,
    load_json,
    log,
    meta_content,
    normalize_number,
    now_iso,
    parse_date,
    safe_filename,
    save_json,
    sha1_text,
    strip_html,
    title_name,
)

BASE_URL = "https://www.mohurd.gov.cn"
INDEX_PAGE_URL = BASE_URL + "/gongkai/fdzdgknr/bzgg/index.html"
INDEX_API = BASE_URL + "/api-gateway/jpaas-publish-server/front/page/build/unit"
SEARCH_API = BASE_URL + "/api-gateway/jpaas-jsearch-web-server/interface/search/info"
SEARCH_SERVICE_ID = "e2f3058e2a3b4f8abc93eb76e739e3e7"
SEARCH_CATE_ID = "6ca0f12c0f0642ab8b1dc17028e12ea1"

INDEX_PARAMS = {
    "parseType": "bulidstatic",
    "webId": "86ca573ec4df405db627fdc2493677f3",
    "tplSetId": "fc259c381af3496d85e61997ea7771cb",
    "pageType": "column",
    "tagId": "栏目-list",
    "editType": "null",
    "pageId": "Y53uSscFTKvrbHxaF9Cvp",
}

_INDEX_CACHE: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# announcement index
# ---------------------------------------------------------------------------


def baseline_index() -> dict[str, Any]:
    value = load_json(data_file("mohurd_index.json"), {})
    if not isinstance(value, dict):
        return {"items": [], "count": 0, "fetched_at": ""}
    value.setdefault("items", [])
    value.setdefault("count", len(value["items"]))
    return value


def index_path() -> Path:
    return cache_dir() / "mohurd_index.json"


def load_index(force_reload: bool = False) -> dict[str, Any]:
    global _INDEX_CACHE
    if _INDEX_CACHE is not None and not force_reload:
        return _INDEX_CACHE
    value = load_json(index_path(), None)
    if not value or not value.get("items"):
        value = baseline_index()
        try:
            save_json(index_path(), value)
        except OSError:
            pass
    _INDEX_CACHE = value
    return value


def fetch_index_page(page: int, page_size: int = 20) -> tuple[list[dict[str, str]], int]:
    params = dict(INDEX_PARAMS)
    params["paramJson"] = json.dumps(
        {"pageNo": page, "pageSize": page_size}, separators=(",", ":")
    )
    url = INDEX_API + "?" + urllib.parse.urlencode(params)
    raw, headers, _ = http_get(
        url,
        timeout=40,
        retries=4,
        referer=INDEX_PAGE_URL,
        headers={"Accept": "application/json, text/javascript, */*; q=0.01"},
    )
    payload = json.loads(decode_html(raw, headers.get("Content-Type", "")))
    html_text = (payload.get("data") or {}).get("html") or ""
    items: list[dict[str, str]] = []
    matches = re.findall(
        r'<a class="fl" href="([^"]+)"[^>]*title="([^"]+)"', html_text
    )
    dates = re.findall(r'<span class="date-info">([^<]+)</span>', html_text)
    for (href, title), date in zip(matches, dates):
        items.append(
            {
                "url": urllib.parse.urljoin(BASE_URL, html_module.unescape(href)),
                "title": html_module.unescape(title).strip(),
                "date": date.strip(),
            }
        )
    count_match = re.search(r'count="(\d+)"', html_text)
    count = int(count_match.group(1)) if count_match else 0
    return items, count


def merge_index_items(
    existing: dict[str, Any], new_items: list[dict[str, str]]
) -> dict[str, Any]:
    by_url = {item.get("url"): item for item in existing.get("items", []) if item.get("url")}
    for item in new_items:
        if item.get("url"):
            by_url[item["url"]] = item
    items = sorted(
        by_url.values(),
        key=lambda item: (item.get("date", ""), item.get("url", "")),
        reverse=True,
    )
    return {
        "source": INDEX_PAGE_URL,
        "source_name": "住房和城乡建设部标准公告",
        "count": len(items),
        "fetched_at": now_iso(),
        "items": items,
    }


def refresh_index(
    pages: int = 3,
    *,
    full: bool = False,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> dict[str, Any]:
    """抓取公告索引前若干页；full=True 时抓取全部。"""
    first_items, count = fetch_index_page(1)
    if full:
        total_pages = max(1, (count + 19) // 20)
    else:
        total_pages = max(1, min(pages, max(1, (count + 19) // 20)))
    pages_data: dict[int, list[dict[str, str]]] = {1: first_items}
    if progress:
        progress(1, total_pages, f"读取公告索引 1/{total_pages}")

    if total_pages > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            futures = {
                executor.submit(fetch_index_page, page): page
                for page in range(2, total_pages + 1)
            }
            done = 1
            for future in concurrent.futures.as_completed(futures):
                page = futures[future]
                try:
                    items, _ = future.result()
                except Exception as exc:  # noqa: BLE001
                    log.warning("公告索引第 %s 页读取失败：%s", page, exc)
                    items = []
                pages_data[page] = items
                done += 1
                if progress:
                    progress(done, total_pages, f"读取公告索引 {done}/{total_pages}")

    new_items: list[dict[str, str]] = []
    for page in sorted(pages_data):
        new_items.extend(pages_data[page])
    merged = merge_index_items(load_index(), new_items)
    save_json(index_path(), merged)
    global _INDEX_CACHE
    _INDEX_CACHE = merged
    return merged


def index_search(
    query: str,
    *,
    category: str = "all",
    include_drafts: bool = False,
    include_products: bool = False,
    require_all: bool = False,
    limit: int = 80,
) -> list[dict[str, str]]:
    """在本地公告索引中检索标题，返回按相关度和日期排序的公告。"""
    query = (query or "").strip()
    tokens = [token for token in re.split(r"\s+", query) if token]
    normalized_query = re.sub(r"\s+", "", query).lower()
    results: list[tuple[int, dict[str, str]]] = []
    for item in load_index().get("items", []):
        title = item.get("title", "")
        plain_title = re.sub(r"\s+", "", title).lower()
        if not include_drafts and any(
            word in title for word in ("征求意见", "英文版", "废止公告")
        ):
            continue
        if not include_products and "产品标准" in title:
            continue
        if any(word in title for word in ("标准图集", "标准设计图", "废止公告")):
            continue
        if not normalized_query:
            score = 1
        else:
            score = 0
            if normalized_query in plain_title:
                score += 30
            if plain_title.startswith(normalized_query):
                score += 15
            for token in tokens:
                if token.lower() in plain_title:
                    score += 7
            if require_all and not all(token.lower() in plain_title for token in tokens):
                continue
            if score == 0:
                continue
            # 让设计规范排在产品标准、图集和事务性公告之前。
            if "规范" in title:
                score += 12
            elif "标准" in title:
                score += 6
            if "局部修订" in title:
                score += 2
            if title.startswith(("住房城乡建设部关于发布", "住房和城乡建设部关于发布", "关于发布")):
                score += 5
        results.append((score, item))
    results.sort(key=lambda pair: (pair[0], pair[1].get("date", "")), reverse=True)
    return [item for _, item in results[:limit]]


# ---------------------------------------------------------------------------
# announcement page parsing
# ---------------------------------------------------------------------------

_NUMBER_RE = re.compile(
    r"([A-Z]{1,5}(?:/[A-Z])?)\s*(\d+(?:\.\d+)?)\s*[-—–－]\s*(\d{4})"
)
_IMPL_RE = re.compile(r"自\s*(\d{4}年\d{1,2}月\d{1,2}日)\s*起(?:实施|施行)")
_SUPERSEDE_RE = re.compile(
    r"原(?:国家标准|行业标准|地方标准|团体标准|规范|标准|行业产品标准)?\s*《([^》]+)》"
    r"\s*[（(]?\s*([A-Z]{1,5}(?:/[A-Z])?\s*\d+(?:\.\d+)?\s*[-—–－]\s*\d{4})",
    re.S,
)


def _attachment_from_match(match: re.Match[str]) -> Optional[dict[str, str]]:
    href = html_module.unescape(match.group(1)).strip()
    label = strip_html(match.group(2) or "").strip()
    if not href:
        return None
    url = urllib.parse.urljoin(BASE_URL, href)
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    filename = ""
    if query.get("fileName"):
        filename = urllib.parse.unquote(query["fileName"][0])
    if not filename:
        filename = urllib.parse.unquote(Path(parsed.path).name)
    return {"url": url, "label": label, "filename": filename}


def parse_announcement_html(html_text: str, url: str = "") -> dict[str, Any]:
    title = meta_content(html_text, "ArticleTitle")
    if not title:
        match = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.S | re.I)
        title = strip_html(match.group(1)) if match else ""
    title = re.sub(r"\s+", "", title)
    text = strip_html(html_text)
    flat = re.sub(r"\s+", " ", text)

    number = ""
    number_match = re.search(
        r"编号为\s*([A-Z]{1,5}(?:/[A-Z])?\s*\d+(?:\.\d+)?\s*[-—–－]\s*\d{4})",
        flat,
    )
    if number_match:
        number = normalize_number(number_match.group(1))
    else:
        for match in _NUMBER_RE.finditer(flat):
            candidate = normalize_number(match.group(0))
            if candidate.startswith(("GB", "JGJ", "CJJ", "CJ", "JG")):
                number = candidate
                break

    impl = ""
    impl_match = _IMPL_RE.search(flat)
    if impl_match:
        impl = parse_date(impl_match.group(1))
    if not impl:
        meta_impl = re.search(r"实施日期[：:]\s*(\d{4}-\d{1,2}-\d{1,2})", flat)
        if meta_impl:
            impl = parse_date(meta_impl.group(1))

    supersedes: list[dict[str, str]] = []
    for match in _SUPERSEDE_RE.finditer(flat):
        supersedes.append(
            {
                "name": re.sub(r"\s+", "", html_module.unescape(match.group(1))),
                "number": normalize_number(match.group(2)),
            }
        )

    attachments: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    link_re = re.compile(
        r'<a\s+[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S
    )
    for match in link_re.finditer(html_text):
        href = html_module.unescape(match.group(1))
        if not (
            "document/download" in href
            or re.search(r"\.pdf(?:$|[?#])", href, re.I)
            or "/attach/" in href
        ):
            continue
        attachment = _attachment_from_match(match)
        if not attachment or attachment["url"] in seen_urls:
            continue
        seen_urls.add(attachment["url"])
        attachments.append(attachment)

    name = title_name(title)
    is_draft = "征求意见" in title
    is_revision = "局部修订" in title
    is_english = "英文版" in title
    is_product = "产品标准" in title
    kind = "draft" if is_draft else "revision" if is_revision else "english" if is_english else "product" if is_product else "standard"

    return {
        "url": url,
        "title": title,
        "name": name,
        "number": number,
        "impl_date": impl,
        "supersedes": supersedes,
        "attachments": attachments,
        "kind": kind,
        "is_revision": is_revision,
        "is_draft": is_draft,
        "is_english": is_english,
        "text": flat,
        "fetched_at": now_iso(),
    }


def choose_pdf_attachment(
    attachments: list[dict[str, str]], standard_name: str = ""
) -> Optional[dict[str, str]]:
    if not attachments:
        return None
    normalized_name = re.sub(r"[\s《》]", "", standard_name or "")
    scored: list[tuple[int, dict[str, str]]] = []
    for attachment in attachments:
        label = re.sub(r"[\s《》]", "", attachment.get("label", ""))
        filename = attachment.get("filename", "")
        url = attachment.get("url", "")
        score = 0
        if normalized_name and label and (
            normalized_name in label or label in normalized_name
        ):
            score += 10
        if filename.lower().endswith(".pdf"):
            score += 4
        if "document/download" in url:
            score += 3
        if label:
            score += 1
        if any(word in label for word in ("废止", "说明", "前言", "目录", "公告")):
            score -= 20
        if any(word in label for word in ("条文", "修订")):
            score += 1
        scored.append((score, attachment))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    best_score, best = scored[0]
    return best if best_score > -10 else None


def fetch_announcement(
    url: str,
    *,
    force: bool = False,
    ttl_hours: int = 24 * 7,
) -> dict[str, Any]:
    if not url:
        raise ValueError("公告地址为空")
    cache_file = cache_dir() / "announcements" / (sha1_text(url) + ".json")
    cached = load_json(cache_file, None)
    if cached and not force:
        try:
            fetched_at = cached.get("_cached_at", "")
            age = time.time() - time.mktime(time.strptime(fetched_at[:19], "%Y-%m-%dT%H:%M:%S"))
            if age < ttl_hours * 3600:
                return cached
        except (ValueError, TypeError, OverflowError):
            pass
    try:
        html_text = http_get_text(url, timeout=40, retries=3, referer=INDEX_PAGE_URL)
        parsed = parse_announcement_html(html_text, url)
        parsed["_cached_at"] = now_iso()
        save_json(cache_file, parsed)
        return parsed
    except Exception:
        if cached:
            cached["_stale"] = True
            return cached
        raise


# ---------------------------------------------------------------------------
# site search (for revision drafts / broader discovery)
# ---------------------------------------------------------------------------


def search_site(query: str, page: int = 1) -> list[dict[str, str]]:
    params = {
        "websiteid": "",
        "q": query,
        "pg": page,
        "cateid": SEARCH_CATE_ID,
        "serviceId": SEARCH_SERVICE_ID,
    }
    url = SEARCH_API + "?" + urllib.parse.urlencode(params)
    raw, headers, _ = http_get(
        url,
        timeout=30,
        retries=3,
        referer=INDEX_PAGE_URL,
        headers={"Accept": "application/json, text/javascript, */*; q=0.01"},
    )
    payload = json.loads(decode_html(raw, headers.get("Content-Type", "")))
    result = ((payload.get("data") or {}).get("searchResult") or {})
    items: list[dict[str, str]] = []
    for block in result.get("result", []) or []:
        match = re.search(
            r'<a[^>]*href="([^"]+)"[^>]*class="textTitle"[^>]*>(.*?)</a>',
            block,
            re.S,
        )
        if not match:
            continue
        title = strip_html(match.group(2))
        items.append(
            {
                "url": html_module.unescape(match.group(1)),
                "title": title,
            }
        )
    return items


def find_revision_draft(name: str) -> Optional[dict[str, str]]:
    if not name:
        return None
    try:
        results = search_site(name + " 征求意见")
    except Exception as exc:  # noqa: BLE001
        log.info("查询征求意见稿失败：%s", exc)
        return None
    plain_name = re.sub(r"\s+", "", name)
    for item in results:
        title = re.sub(r"\s+", "", item.get("title", ""))
        if plain_name in title and "征求意见" in title:
            return item
    return None
