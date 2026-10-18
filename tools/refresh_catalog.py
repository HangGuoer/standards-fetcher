#!/usr/bin/env python3
"""按 standards.json 中的公告地址，重新抓取并打印最新解析结果。

这是维护脚本，不会自动改写目录；确认结果后再人工更新 data/standards.json。
"""
from __future__ import annotations

import json
from pathlib import Path

from standards_fetcher.mohurd import choose_pdf_attachment, fetch_announcement
from standards_fetcher.util import data_file

records = json.loads(data_file("standards.json").read_text(encoding="utf-8"))
for record in records:
    url = record.get("base_announcement_url") or record.get("announcement_url")
    if not url:
        print(f"跳过（无公告）：{record['number']} {record['name']}")
        continue
    try:
        parsed = fetch_announcement(url, force=True)
        attachment = choose_pdf_attachment(parsed.get("attachments", []), record["name"])
        print(
            f"{record['number']:<28} {parsed.get('number',''):<20} "
            f"{parsed.get('impl_date',''):<12} PDF={'有' if attachment else '无'} "
            f"{record['name']}"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"失败：{record['number']} {record['name']}：{exc}")
