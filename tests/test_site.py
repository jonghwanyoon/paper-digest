import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from digest import export
from digest.textclean import jats_abstract, markup_to_text
from tests.helpers import TempEnv

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("site_build", ROOT / "site" / "build.py")
site_build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(site_build)

EVIL = '<script>alert(1)</script><img src=x onerror=alert(2)>'
ABSTRACT = "SECRET-ABSTRACT-SENTENCE that must stay local."


class ExportAndBuildTest(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        summary = {"title_ko": EVIL, "tldr": EVIL, "key_findings": [EVIL], "methods": EVIL,
                   "limitations": None, "keywords": [EVIL], "relevance": 9, "relevance_reason": EVIL}
        self.env.add_paper("10.1/evil", status="summarized", text=ABSTRACT, teaser=ABSTRACT,
                           title=EVIL, summary_json=json.dumps(summary), summary_level="abstract",
                           relevance=9, flags_json=json.dumps({"unverified_numbers": [EVIL]}))
        self.env.add_paper("10.1/waiting", status="needs_pdf", text=ABSTRACT, teaser=ABSTRACT)
        self.env.add_paper("10.1/news", status="skipped", text=ABSTRACT)
        export.export(self.env.cfg, self.env.conn)
        self.data = self.env.cfg.public_data_dir

    def tearDown(self):
        self.env.close()

    def test_public_json_has_no_source_text_and_no_skipped(self):
        blob = "".join(p.read_text() for p in self.data.rglob("*.json"))
        self.assertNotIn("SECRET-ABSTRACT", blob)
        records = json.loads((self.data / "papers" / "2026-10.json").read_text())
        self.assertEqual(sorted(r["doi"] for r in records), ["10.1/evil", "10.1/waiting"])

    def test_model_output_is_escaped(self):
        with tempfile.TemporaryDirectory() as out:
            site_build.build(self.data, Path(out))
            page = (Path(out) / "index.html").read_text()
        self.assertNotIn("<script>", page)
        self.assertNotIn("<img", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertIn("근거: 초록", page)
        self.assertIn("PDF 필요", page)

    def test_relevance_order(self):
        records = json.loads((self.data / "papers" / "2026-10.json").read_text())
        ordered = sorted(records, key=site_build.sort_key)
        self.assertEqual(ordered[0]["doi"], "10.1/evil")


class TextCleanTest(unittest.TestCase):
    def test_citation_superscripts_dropped_exponents_kept(self):
        jats = ("<jats:title>Abstract</jats:title><jats:p>Cancer is rarely curable<jats:sup>1,2</jats:sup>"
                " with 10<jats:sup>6</jats:sup> cells and BATF<jats:sup>+</jats:sup> Treg.</jats:p>")
        self.assertEqual(jats_abstract(jats),
                         "Cancer is rarely curable with 10^6 cells and BATF^+ Treg.")

    def test_europepmc_bibr_refs_and_ref_list_removed(self):
        xml = ('<sec><title>Intro</title><p>Known<xref ref-type="bibr" rid="r1">12</xref> fact.</p></sec>'
               '<ref-list><ref>Smith 2020</ref></ref-list>')
        text = markup_to_text(xml)
        self.assertIn("## Intro", text)
        self.assertIn("Known fact.", text)
        self.assertNotIn("Smith", text)

    def test_jats_title_markup_and_isotopes(self):
        xml = '<sec><title>Bond of <sup>259</sup>Sg(CO)<sub>6</sub></title><p>We saw <sup>229</sup>Th.</p></sec>'
        self.assertEqual(markup_to_text(xml), "## Bond of ^259Sg(CO)6\n\nWe saw ^229Th.")


if __name__ == "__main__":
    unittest.main()
