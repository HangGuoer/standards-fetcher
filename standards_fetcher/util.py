from __future__ import annotations

import gzip
import hashlib
import html as html_module
import json
import logging
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from . import APP_NAME

log = logging.getLogger("standards_fetcher")

APP_SUPPORT_NAME = "StandardsFetcher"
DEFAULT_FOLDER_NAME = "建筑电气规范"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36 "
    "StandardsFetcher/1.0"
)

# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def resource_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent


def data_file(name: str) -> Path:
    return resource_root() / "data" / name


def static_dir() -> Path:
    return resource_root() / "static"


def app_data_dir() -> Path:
    override = os.environ.get("STANDARDS_FETCHER_DATA_DIR")
    if override:
        path = Path(override).expanduser()
    else:
        path = Path.home() / "Library" / "Application Support" / APP_SUPPORT_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    path = app_data_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_download_dir() -> Path:
    downloads = Path.home() / "Downloads"
    if downloads.exists():
        return downloads / DEFAULT_FOLDER_NAME
    return Path.home() / "Desktop" / DEFAULT_FOLDER_NAME


def setup_logging() -> Path:
    log_path = app_data_dir() / "app.log"
    if not log.handlers:
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        if sys.stderr and sys.stderr.isatty():
            stream = logging.StreamHandler()
            stream.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
            log.addHandler(stream)
    return log_path


# ---------------------------------------------------------------------------
# JSON / files
# ---------------------------------------------------------------------------


def load_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def sha1_text(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def human_size(num: int) -> str:
    if num < 1024:
        return f"{num} B"
    for unit in ("KB", "MB", "GB"):
        num /= 1024.0
        if num < 1024:
            return f"{num:.1f} {unit}"
    return f"{num:.1f} TB"


def safe_filename(value: str, max_length: int = 150) -> str:
    value = (value or "").strip()
    replacements = {
        "/": "／",
        "\\": "＼",
        ":": "：",
        "*": "＊",
        "?": "？",
        '"': "＂",
        "<": "＜",
        ">": "＞",
        "|": "｜",
        "\n": " ",
        "\r": " ",
        "\t": " ",
        "\x00": "",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = "标准"
    if len(value) > max_length:
        value = value[:max_length].rstrip(" .")
    # macOS/APFS 单个文件名上限为 255 字节，中文通常占 3 字节。
    while len(value.encode("utf-8")) > 240:
        value = value[:-1].rstrip(" .")
    return value or "标准"


def now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def today_iso() -> str:
    return datetime.now().astimezone().date().isoformat()


# ---------------------------------------------------------------------------
# text parsing
# ---------------------------------------------------------------------------


def decode_html(raw: bytes, content_type: str = "") -> str:
    charset = None
    match = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    if match:
        charset = match.group(1)
    if not charset:
        match = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", raw[:4096], re.I)
        if match:
            charset = match.group(1).decode("ascii", "ignore")
    for enc in filter(None, [charset, "utf-8", "gb18030"]):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def strip_html(value: str) -> str:
    value = re.sub(r"<script.*?</script>|<style.*?</style>", " ", value, flags=re.S | re.I)
    value = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html_module.unescape(value)
    value = value.replace("\u00a0", " ")
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n\s*\n+", "\n", value)
    return value.strip()


def meta_content(html_text: str, name: str) -> str:
    patterns = [
        rf'<meta[^>]+name=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']*)["\']',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+name=["\']{re.escape(name)}["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html_text, re.I | re.S)
        if match:
            return html_module.unescape(match.group(1)).strip()
    return ""


def normalize_number(value: str) -> str:
    if not value:
        return ""
    value = html_module.unescape(value).strip()
    value = value.replace("—", "-").replace("–", "-").replace("－", "-")
    value = value.replace("／", "/")
    value = re.sub(r"\s*-\s*", "-", value)
    value = re.sub(r"^([A-Z]{2,4})\s+(\d{1,2}/T)", r"\1\2", value)
    local = re.match(r"^([A-Z]{2,4}\d{1,2}/T)\s*(\d)", value)
    if local:
        value = local.group(1) + " " + value[local.end(1):].lstrip()
    else:
        value = re.sub(r"^([A-Z]+(?:/[A-Z]+)?)\s*(\d)", r"\1 \2", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def base_number(value: str) -> str:
    value = normalize_number(value)
    value = re.sub(r"[（(]\d{4}年版[）)]$", "", value)
    return value.strip()


def title_name(title: str) -> str:
    match = re.search(r"《([^》]+)》", title or "")
    if not match:
        return ""
    return re.sub(r"\s+", "", match.group(1))


def parse_date(value: str) -> str:
    value = (value or "").strip()
    match = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", value)
    if not match:
        return ""
    return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def display_number(value: str) -> str:
    return normalize_number(value)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _build_headers(extra: Optional[dict[str, str]] = None, referer: str = "") -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "close",
    }
    if referer:
        headers["Referer"] = referer
    if extra:
        headers.update(extra)
    return headers


def http_get(
    url: str,
    *,
    timeout: int = 30,
    retries: int = 3,
    referer: str = "",
    headers: Optional[dict[str, str]] = None,
) -> tuple[bytes, Any, str]:
    last_error: Optional[Exception] = None
    context = ssl.create_default_context()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=_build_headers(headers, referer))
            with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
                raw = resp.read()
                if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                return raw, resp.headers, resp.geturl()
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code in (403, 404, 410):
                break
            if attempt < retries - 1:
                time.sleep(1.2 * (attempt + 1))
        except Exception as exc:  # noqa: BLE001 - network errors vary
            last_error = exc
            if attempt < retries - 1:
                time.sleep(1.2 * (attempt + 1))
    if last_error:
        raise last_error
    raise RuntimeError(f"请求失败：{url}")


def http_get_text(
    url: str,
    *,
    timeout: int = 30,
    retries: int = 3,
    referer: str = "",
) -> str:
    raw, headers, _ = http_get(url, timeout=timeout, retries=retries, referer=referer)
    return decode_html(raw, headers.get("Content-Type", ""))


def download_to(
    url: str,
    destination: Path,
    *,
    referer: str = "",
    timeout: int = 180,
    retries: int = 3,
    max_bytes: int = 350 * 1024 * 1024,
    progress: Optional[Callable[[int, int], None]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
) -> int:
    """下载文件到 destination（先写 .part），返回字节数。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(destination.name + ".part")
    last_error: Optional[Exception] = None
    for attempt in range(retries):
        try:
            if part.exists():
                part.unlink()
            req = urllib.request.Request(url, headers=_build_headers(referer=referer))
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                try:
                    total = int(resp.headers.get("Content-Length") or 0)
                except ValueError:
                    total = 0
                received = 0
                with part.open("wb") as fh:
                    while True:
                        if cancelled and cancelled():
                            raise DownloadCancelled()
                        chunk = resp.read(1024 * 128)
                        if not chunk:
                            break
                        received += len(chunk)
                        if received > max_bytes:
                            raise ValueError(f"文件超过 {human_size(max_bytes)} 限制")
                        fh.write(chunk)
                        if progress:
                            progress(received, total)
            if part.stat().st_size == 0:
                raise ValueError("服务器返回空文件")
            os.replace(part, destination)
            return destination.stat().st_size
        except DownloadCancelled:
            if part.exists():
                part.unlink(missing_ok=True)
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if part.exists():
                part.unlink(missing_ok=True)
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    if last_error:
        raise last_error
    raise RuntimeError("下载失败")


class DownloadCancelled(Exception):
    pass


# ---------------------------------------------------------------------------
# macOS helpers
# ---------------------------------------------------------------------------


def open_path(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(str(path))
    subprocess.Popen(["open", str(path)])


def reveal_path(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(str(path))
    if path.is_dir():
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["open", "-R", str(path)])


def open_url(url: str) -> None:
    subprocess.Popen(["open", url])


def choose_folder() -> str:
    script = (
        'try\n'
        'set chosenFolder to choose folder with prompt "选择保存标准 PDF 的文件夹"\n'
        'return POSIX path of chosenFolder\n'
        'on error number -128\n'
        'return ""\n'
        'end try'
    )
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        message = (result.stderr or "").strip()
        if "User canceled" in message or "-128" in message:
            return ""
        raise RuntimeError(message or "无法打开文件夹选择器")
    return result.stdout.strip().rstrip("/")


def ensure_writable_dir(path: Path) -> Path:
    path = path.expanduser()
    try:
        path = path.resolve()
    except OSError:
        pass
    if str(path) in {"/", "/System", "/Library", "/Applications", "/Users"}:
        raise ValueError("请选择用户目录下的普通文件夹")
    path.mkdir(parents=True, exist_ok=True)
    probe = path / ".standards_fetcher_write_test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        raise ValueError(f"文件夹不可写：{path}") from exc
    return path


def shorten(value: str, length: int = 80) -> str:
    value = (value or "").strip()
    return value if len(value) <= length else value[: length - 1] + "…"


def http_post_form(
    url: str,
    data: dict[str, Any],
    *,
    timeout: int = 30,
    retries: int = 3,
    referer: str = "",
    headers: Optional[dict[str, str]] = None,
) -> tuple[bytes, Any, str]:
    """提交 application/x-www-form-urlencoded 表单，返回原始字节。"""
    body = urllib.parse.urlencode(data).encode("utf-8")
    request_headers = _build_headers(headers, referer)
    request_headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    last_error: Optional[Exception] = None
    context = ssl.create_default_context()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=request_headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
                raw = resp.read()
                if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                return raw, resp.headers, resp.geturl()
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code in (403, 404, 410):
                break
            if attempt < retries - 1:
                time.sleep(1.2 * (attempt + 1))
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < retries - 1:
                time.sleep(1.2 * (attempt + 1))
    if last_error:
        raise last_error
    raise RuntimeError(f"请求失败：{url}")
