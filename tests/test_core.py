import unittest

from standards_fetcher.catalog import get_record, load_records, search, stats
from standards_fetcher.downloader import _record_sources, _source_filename
from standards_fetcher.mohurd import choose_pdf_attachment, parse_announcement_html
from standards_fetcher.util import normalize_number, parse_date, safe_filename


SAMPLE = """
<html><head>
<title>住房城乡建设部关于发布国家标准《建筑照明设计标准》的公告</title>
<meta name="ArticleTitle" content="住房城乡建设部关于发布国家标准《建筑照明设计标准》的公告">
</head><body>
<p>现批准《建筑照明设计标准》为国家标准，编号为GB/T50034-2024，自2024年8月1日起实施。
原国家标准《建筑照明设计标准》GB50034-2013同时废止。</p>
<p>本标准在住房城乡建设部门户网站公开。</p>
<a href="/api-gateway/jpaas-web-server/front/document/download?fileUrl=abc&amp;fileName=%E5%BB%BA%E7%AD%91%E7%85%A7%E6%98%8E%E8%AE%BE%E8%AE%A1%E6%A0%87%E5%87%86.pdf">《建筑照明设计标准》</a>
</body></html>
"""

REVISION = """
<html><head><title>关于发布国家标准《建筑设计防火规范》局部修订的公告</title>
<meta name="ArticleTitle" content="关于发布国家标准《建筑设计防火规范》局部修订的公告"></head>
<body><p>现批准国家标准《建筑设计防火规范》GB50016-2014局部修订的条文，自2018年10月1日起实施。</p>
<a href="/api-gateway/jpaas-web-server/front/document/download?fileUrl=xyz&amp;fileName=rev.pdf">建筑设计防火规范</a>
</body></html>
"""


class TestParsing(unittest.TestCase):
    def test_normalize_number(self):
        self.assertEqual(normalize_number("GB/T50034—2024"), "GB/T 50034-2024")
        self.assertEqual(normalize_number("JGJ/T334-2014"), "JGJ/T 334-2014")
        self.assertEqual(normalize_number("GB 55024-2022"), "GB 55024-2022")

    def test_parse_date(self):
        self.assertEqual(parse_date("自2024年8月1日起实施"), "2024-08-01")
        self.assertEqual(parse_date("实施日期：2022-04-01"), "2022-04-01")
        self.assertEqual(parse_date("不是日期"), "")

    def test_safe_filename(self):
        self.assertEqual(safe_filename('GB/T 50034:2024 "x"'), "GB／T 50034：2024 ＂x＂")
        self.assertLessEqual(len(safe_filename("长" * 300)), 150)

    def test_parse_announcement(self):
        parsed = parse_announcement_html(SAMPLE, "https://example.test/a")
        self.assertEqual(parsed["number"], "GB/T 50034-2024")
        self.assertEqual(parsed["impl_date"], "2024-08-01")
        self.assertEqual(parsed["name"], "建筑照明设计标准")
        self.assertEqual(parsed["supersedes"][0]["number"], "GB 50034-2013")
        self.assertEqual(len(parsed["attachments"]), 1)
        self.assertEqual(parsed["attachments"][0]["filename"], "建筑照明设计标准.pdf")

    def test_parse_revision(self):
        parsed = parse_announcement_html(REVISION, "https://example.test/b")
        self.assertEqual(parsed["number"], "GB 50016-2014")
        self.assertEqual(parsed["impl_date"], "2018-10-01")
        self.assertTrue(parsed["is_revision"])

    def test_choose_attachment_skips_repeal(self):
        attachments = [
            {"url": "https://x/1", "label": "废止的现行工程建设标准相关强制性条文", "filename": "1.pdf"},
            {"url": "https://x/2", "label": "建筑环境通用规范", "filename": "2.pdf"},
        ]
        chosen = choose_pdf_attachment(attachments, "建筑环境通用规范")
        self.assertEqual(chosen["url"], "https://x/2")


class TestCatalog(unittest.TestCase):
    def test_counts(self):
        records = load_records()
        self.assertGreaterEqual(len(records), 50)
        self.assertEqual(stats()["total"], len(records))
        self.assertGreater(stats()["downloadable"], 30)

    def test_exact_number_search(self):
        records = search("GB 50034")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["number"], "GB/T 50034-2024")

    def test_search_name(self):
        records = search("照明")
        self.assertTrue(any("照明" in record["name"] for record in records))
        self.assertTrue(all("照明" in (record["name"] + record["number"]) for record in records))

    def test_amendment_pair(self):
        record = get_record("gb-50016-2014-2018")
        self.assertIsNotNone(record)
        self.assertTrue(record["pdf_available"])
        self.assertTrue(record["amendment"]["pdf_available"])
        sources = _record_sources(record)
        self.assertEqual(len(sources), 2)
        self.assertEqual({source["kind"] for source in sources}, {"base", "amendment"})

    def test_only_amendment(self):
        record = get_record("gb-t-50720-2011-2025")
        self.assertIsNotNone(record)
        self.assertFalse(record["pdf_available"])
        sources = _record_sources(record)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["kind"], "amendment")

    def test_source_filename(self):
        name = _source_filename("GB/T 50034-2024", "建筑照明设计标准")
        self.assertTrue(name.endswith(".pdf"))
        self.assertIn("建筑照明设计标准", name)
        self.assertNotIn("/", name)


if __name__ == "__main__":
    unittest.main()


class TestDiscovery(unittest.TestCase):
    def test_group_key_merges_standard_suffixes(self):
        from standards_fetcher.discovery import _group_key

        self.assertEqual(_group_key("石油化工企业设计防火规范"), _group_key("石油化工企业设计防火标准"))
        self.assertNotEqual(_group_key("建筑设计防火规范"), _group_key("建筑防火通用规范"))

    def test_purchase_url(self):
        from standards_fetcher.discovery import purchase_url

        self.assertIn("kscecs", purchase_url("GB 50016-2014"))
        self.assertIn("kscecs", purchase_url("GB/T 51368-2019"))
        self.assertIn("openstd", purchase_url("GB/T 19001-2016"))

    def test_merge_base_and_revision(self):
        from standards_fetcher.discovery import _merge_group

        base = {
            "id": "b1",
            "number": "GB 50160-2008",
            "base_number": "GB 50160-2008",
            "name": "石油化工企业设计防火规范",
            "base_name": "石油化工企业设计防火规范",
            "impl_date": "2009-07-01",
            "announce_date": "2008-12-01",
            "announcement_url": "https://example.test/base",
            "pdf_available": True,
            "attachment_count": 1,
            "is_revision": False,
        }
        revision = {
            "id": "r1",
            "number": "GB 50160-2008",
            "base_number": "GB 50160-2008",
            "name": "石油化工企业设计防火标准",
            "base_name": "石油化工企业设计防火标准",
            "impl_date": "2019-04-01",
            "announce_date": "2019-01-01",
            "announcement_url": "https://example.test/rev",
            "pdf_available": False,
            "attachment_count": 1,
            "is_revision": True,
        }
        merged = _merge_group([base, revision])
        self.assertTrue(merged["pdf_available"])
        self.assertIn("2019局部修订", merged["number"])
        self.assertTrue(merged["amendment"]["pdf_available"])
        self.assertEqual(merged["announcement_url"], "https://example.test/rev")

    def test_merge_revision_only(self):
        from standards_fetcher.discovery import _merge_group

        revision = {
            "id": "r1",
            "number": "GB/T 50720-2011",
            "base_number": "GB/T 50720-2011",
            "name": "建设工程施工现场消防安全技术标准",
            "base_name": "建设工程施工现场消防安全技术规范",
            "impl_date": "2025-11-01",
            "announce_date": "2025-07-24",
            "announcement_url": "https://example.test/rev",
            "pdf_available": False,
            "attachment_count": 1,
            "is_revision": True,
        }
        merged = _merge_group([revision])
        self.assertFalse(merged["pdf_available"])
        self.assertTrue(merged["amendment"]["pdf_available"])
        self.assertIn("局部修订", merged["amendment"]["label"])


class TestSources(unittest.TestCase):
    def test_parse_samr_page(self):
        from standards_fetcher.sources import _parse_samr_page

        html = """
        <div class="panel panel-default post">
        <span class="line11">国家<br/>标准</span>
        <a href="#" tid="BV_GB" pid="PID123" target="_blank"><span class="en-code">GB 12345-2026</span>&nbsp;&nbsp;建筑<sacinfo>防火</sacinfo>门</a>
        <span class="s-status label label-info">即将实施</span>
        <time class="post-date" datetime="">2026-01-01</time>
        <time class="post-date" datetime="">2027-01-01</time>
        </div>
        <script>totalPages:3</script><script>if(Number("25")>0){}</script>
        """
        items, total, pages = _parse_samr_page(html)
        self.assertEqual(total, 25)
        self.assertEqual(pages, 3)
        self.assertEqual(items[0]["number"], "GB 12345-2026")
        self.assertEqual(items[0]["name"], "建筑防火门")
        self.assertEqual(items[0]["status"], "upcoming")
        self.assertEqual(items[0]["impl_date"], "2027-01-01")
        self.assertIn("gbDetailed", items[0]["detail_url"])

    def test_parse_ccsn_item(self):
        from standards_fetcher.sources import _parse_ccsn_item

        record = _parse_ccsn_item(
            {
                "Guid": "abc-123",
                "StandardCode": "GB50054-2011",
                "StandardCNName": "低压配电设计规范",
                "StandardState": "现行",
                "StandardType": "工程国标",
                "PublishDate": "2011-07-26T00:00:00",
                "PerformDate": "2012-06-01T00:00:00",
                "ReplaceStandardCode": "GB50054-95",
            }
        )
        self.assertEqual(record["number"], "GB 50054-2011")
        self.assertEqual(record["impl_date"], "2012-06-01")
        self.assertEqual(record["source"], "ccsn")
        self.assertIn("StandardQueryDetail", record["detail_url"])
        self.assertEqual(record["supersedes"], ["GB 50054-95"])

    def test_merge_sources_keeps_mohurd_pdf(self):
        from standards_fetcher.sources import merge_sources

        mohurd = {
            "id": "m1",
            "number": "GB 50016-2014（2018局部修订）",
            "base_number": "GB 50016-2014",
            "name": "建筑设计防火规范",
            "base_name": "建筑设计防火规范",
            "source": "mohurd",
            "source_label": "住房和城乡建设部",
            "source_short": "住建部",
            "status": "current",
            "impl_date": "2018-10-01",
            "announcement_url": "https://mohurd.test/a",
            "base_announcement_url": "https://mohurd.test/base",
            "official_url": "https://mohurd.test/a",
            "pdf_available": True,
            "amendment": {"pdf_available": True},
            "sources": [],
            "source_labels": [],
        }
        ccsn = {
            "id": "c1",
            "number": "GB50016-2014（2018年版）",
            "base_number": "GB50016-2014",
            "name": "建筑设计防火规范",
            "base_name": "建筑设计防火规范",
            "source": "ccsn",
            "source_label": "国家工程建设标准化信息网",
            "source_short": "工程标准网",
            "status": "current",
            "impl_date": "2015-05-01",
            "detail_url": "https://ccsn.test/d",
            "official_url": "https://ccsn.test/d",
            "pdf_available": False,
        }
        merged = merge_sources([mohurd, ccsn])
        self.assertEqual(len(merged), 1)
        self.assertTrue(merged[0]["pdf_available"])
        self.assertEqual(merged[0]["source_count"], 2)
        self.assertEqual(merged[0]["detail_url"], "https://ccsn.test/d")
        self.assertEqual(merged[0]["announcement_url"], "https://mohurd.test/a")


class TestCodeClassification(unittest.TestCase):
    def test_code_types(self):
        from standards_fetcher.sources import classify_code

        cases = {
            "GB 50016-2014": "GB",
            "GB/T 50034-2024": "GB/T",
            "GB/Z 5169.50-2026": "GB/Z",
            "JGJ 57-2016": "JGJ",
            "JGJ/T 334-2014": "JGJ/T",
            "CJJ 45-2015": "CJJ",
            "CJJ/T 120-2018": "CJJ/T",
            "JG/T 577-2022": "JG/T",
            "DB32/T 5275-2025": "DB",
            "DBJ/T 15-110-2015": "DB",
            "DG/TJ 08-2347-2021": "DG",
            "T/CECS 123-2020": "团体",
        }
        for number, expected in cases.items():
            self.assertEqual(classify_code(number)[0], expected, number)
