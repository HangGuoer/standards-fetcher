from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from . import APP_NAME, __version__
from .catalog import load_collections, search
from .discovery import discover
from .downloader import download_records
from .mohurd import load_index, refresh_index
from .util import default_download_dir, human_size, setup_logging


def cmd_serve(args: argparse.Namespace) -> int:
    from .web import run_app

    run_app(port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    records = search(args.query, args.category)
    if args.json:
        print(json.dumps(records, ensure_ascii=False, indent=2))
        return 0
    if not records:
        print("没有找到匹配的标准。")
        return 1
    print(f"{'标准号':<28} {'实施日期':<12} {'PDF':<5} 名称")
    print("-" * 100)
    for record in records:
        pdf = "可下载" if record.get("pdf_available") else "未公开"
        if (record.get("amendment") or {}).get("pdf_available"):
            pdf = "可下载*"
        print(
            f"{record.get('number',''):<28} {record.get('impl_date',''):<12} {pdf:<6} {record.get('name','')}"
        )
        if record.get("draft"):
            print(f"  ! 有征求意见稿：{record['draft'].get('title','')}")
    print(f"\n共 {len(records)} 项。* 表示含局部修订文件，母标准可能需另行获取。")
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    result = discover(args.query, limit=args.limit)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    meta = result.get("meta", {})
    print(
        f"找到 {meta.get('total_hits', 0)} 条公告，解析 {meta.get('resolved', 0)} 条，"
        f"合并为 {meta.get('groups', 0)} 项标准。"
    )
    if meta.get("truncated"):
        print("公告较多，仅解析相关度最高的一部分；可使用更精确的关键词。")
    print(f"{'标准号':<32} {'实施日期':<12} {'PDF':<6} 名称")
    print("-" * 110)
    for record in result.get("items", []):
        pdf = "可下载" if record.get("pdf_available") else (
            "仅修订" if (record.get("amendment") or {}).get("pdf_available") else "未公开"
        )
        print(
            f"{record.get('number',''):<32} {record.get('impl_date',''):<12} {pdf:<6} "
            f"{record.get('name','')}"
        )
        if record.get("hidden_versions"):
            print(f"  · 已自动合并 {record['hidden_versions']} 个旧版")
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    records = search(args.query, args.category)
    if args.only_available:
        records = [
            record
            for record in records
            if record.get("pdf_available")
            or (record.get("amendment") or {}).get("pdf_available")
        ]
    if not records:
        print("没有找到可下载的标准。")
        return 1
    out_dir = Path(args.out).expanduser() if args.out else default_download_dir()
    print(f"准备下载 {len(records)} 项到：{out_dir}")
    for record in records:
        mark = "PDF" if record.get("pdf_available") else ("仅修订" if (record.get("amendment") or {}).get("pdf_available") else "未公开")
        print(f"  [{mark}] {record.get('number','')} {record.get('name','')}")
    if not args.yes:
        answer = input("继续？[y/N] ").strip().lower()
        if answer not in ("y", "yes", "是"):
            print("已取消。")
            return 0
    result = download_records(records, str(out_dir), force=args.force)
    done = sum(1 for item in result["items"] if item["status"] in ("done", "skipped"))
    print(f"完成：{done}/{len(result['items'])} 个文件已保存或已存在。")
    print(f"文件夹：{out_dir}")
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    print("正在更新住建部标准公告索引…")
    result = refresh_index(pages=args.pages, full=args.full)
    print(f"完成：{result.get('count')} 条公告，更新时间 {result.get('fetched_at')}")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    data = load_collections()
    index = load_index()
    print(f"{APP_NAME} v{__version__}")
    print(f"数据快照：{data.get('snapshot','')}")
    print("数据源：住房和城乡建设部 / 全国标准信息公共服务平台 / 国家工程建设标准化信息网")
    print(f"住建部公告索引：{len(index.get('items', []))} 条（单次最多解析 60 条）")
    print(f"内置标准号回退目录：{len(search(''))} 条")
    print(f"公告索引更新时间：{index.get('fetched_at','')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="standards-fetcher", description=APP_NAME)
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动本地网页界面")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-browser", action="store_true")
    serve.set_defaults(func=cmd_serve)

    list_cmd = sub.add_parser("list", help="列出标准")
    list_cmd.add_argument("query", nargs="?", default="")
    list_cmd.add_argument("--category", default="all")
    list_cmd.add_argument("--json", action="store_true")
    list_cmd.set_defaults(func=cmd_list)

    discover_cmd = sub.add_parser("discover", help="在全部标准公告中检索任意关键词")
    discover_cmd.add_argument("query")
    discover_cmd.add_argument("--limit", type=int, default=60)
    discover_cmd.add_argument("--json", action="store_true")
    discover_cmd.set_defaults(func=cmd_discover)

    download = sub.add_parser("download", help="下载标准 PDF")
    download.add_argument("query", nargs="?", default="")
    download.add_argument("--category", default="all")
    download.add_argument("--out", default="")
    download.add_argument("--force", action="store_true")
    download.add_argument("--yes", action="store_true")
    download.add_argument("--only-available", action="store_true")
    download.set_defaults(func=cmd_download)

    refresh = sub.add_parser("refresh", help="更新公告索引")
    refresh.add_argument("--pages", type=int, default=3)
    refresh.add_argument("--full", action="store_true")
    refresh.set_defaults(func=cmd_refresh)

    info = sub.add_parser("info", help="显示数据信息")
    info.set_defaults(func=cmd_info)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    setup_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
