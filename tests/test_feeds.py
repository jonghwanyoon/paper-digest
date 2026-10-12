import unittest

from digest import db, feeds
from tests.helpers import TempEnv

NATURE_RSS = b"""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
 xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/">
 <item rdf:about="https://www.nature.com/articles/s41586-026-00001-1">
  <title>A study of &lt;sup&gt;229&lt;/sup&gt;Th clocks</title>
  <link>https://www.nature.com/articles/s41586-026-00001-1</link>
  <dc:creator>Kim A</dc:creator><dc:creator>Lee B</dc:creator>
  <dc:date>2026-10-09</dc:date>
  <prism:doi>10.1038/s41586-026-00001-1</prism:doi>
 </item>
 <item rdf:about="https://www.nature.com/articles/s41586-026-00002-2">
  <title>Author Correction: Something earlier</title>
  <prism:doi>10.1038/s41586-026-00002-2</prism:doi>
 </item>
 <item rdf:about="https://www.nature.com/articles/d41586-026-00003-3">
  <title>News about mice</title>
  <prism:doi>10.1038/d41586-026-00003-3</prism:doi>
 </item>
</rdf:RDF>"""

CELL_RSS = b"""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
 xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:prism="http://prismstandard.org/namespaces/1.2/basic/">
 <item rdf:about="https://www.cell.com/cell/fulltext/S0092-8674(26)01119-0?rss=yes">
  <title>Mapping responses</title>
  <link>https://www.cell.com/cell/fulltext/S0092-8674(26)01119-0?rss=yes</link>
  <dc:identifier>10.1016/j.cell.2026.09.017</dc:identifier>
  <dc:date>2026-10-09</dc:date>
  <prism:section>Resource</prism:section>
 </item>
 <item rdf:about="x"><title>Correction: Old</title>
  <dc:identifier>10.1016/j.cell.2026.09.099</dc:identifier><prism:section>Correction</prism:section></item>
 <item rdf:about="y"><title>Preview piece</title>
  <dc:identifier>10.1016/j.cell.2026.09.002</dc:identifier><prism:section>Preview</prism:section></item>
</rdf:RDF>"""


class ClassificationTest(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()

    def tearDown(self):
        self.env.close()

    def statuses(self):
        return {r["doi"]: r["status"] for r in self.env.conn.execute("SELECT doi, status FROM papers")}

    def test_nature_research_only_s41586_without_corrections(self):
        items = feeds.parse_feed(NATURE_RSS)
        self.assertEqual(items[0].title, "A study of 229Th clocks")
        self.assertEqual(items[0].authors, "Kim A, Lee B")
        feeds.store(self.env.conn, self.env.cfg.journals["nature"], items, "2026-10-12")
        self.assertEqual(self.statuses(), {
            "10.1038/s41586-026-00001-1": "new",
            "10.1038/s41586-026-00002-2": "skipped",
            "10.1038/d41586-026-00003-3": "skipped",
        })

    def test_cell_section_rules_and_bare_doi_identifier(self):
        items = feeds.parse_feed(CELL_RSS)
        self.assertEqual(items[0].url, "https://www.cell.com/cell/fulltext/S0092-8674(26)01119-0")
        feeds.store(self.env.conn, self.env.cfg.journals["cell"], items, "2026-10-12")
        self.assertEqual(self.statuses(), {
            "10.1016/j.cell.2026.09.017": "new",
            "10.1016/j.cell.2026.09.099": "skipped",
            "10.1016/j.cell.2026.09.002": "skipped",
        })

    def test_refetch_does_not_duplicate_or_reset(self):
        journal = self.env.cfg.journals["cell"]
        items = feeds.parse_feed(CELL_RSS)
        self.assertEqual(feeds.store(self.env.conn, journal, items, "2026-10-12"), (1, 2))
        db.update(self.env.conn, "10.1016/j.cell.2026.09.017", status="summarized")
        # 몇 주 뒤에도 피드에 남아 있는 같은 논문
        self.assertEqual(feeds.store(self.env.conn, journal, items, "2026-10-30"), (0, 0))
        row = db.get(self.env.conn, "10.1016/j.cell.2026.09.017")
        self.assertEqual(row["status"], "summarized")
        self.assertEqual(row["digest_date"], "2026-10-12")

    def test_normalize_doi(self):
        self.assertEqual(feeds.normalize_doi("doi:10.1126/Science.AEH6251"), "10.1126/science.aeh6251")
        self.assertEqual(feeds.normalize_doi("https://www.science.org/doi/abs/10.1126/science.x1?af=R"),
                         "10.1126/science.x1")
        self.assertIsNone(feeds.normalize_doi("no doi here"))


if __name__ == "__main__":
    unittest.main()
