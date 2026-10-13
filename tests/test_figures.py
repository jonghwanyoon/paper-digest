import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

from digest import db, export, figures
from digest.engines import EngineError, QuotaExhausted
from digest.net import HttpError
from tests.helpers import TempEnv
from tests.test_summarize import GOOD, ScriptedEngine

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("site_build", ROOT / "site" / "build.py")
site_build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(site_build)

CELL_URL = "https://www.cell.com/cell/fulltext/S0092-8674(26)01119-0"


def png_bytes(width=2000, height=800) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, "PNG")
    return buf.getvalue()


class FakeClient:
    def __init__(self, answers):
        self.answers = answers
        self.urls = []

    def image_exists(self, url):
        self.urls.append(url)
        out = self.answers.get(url, False)
        if isinstance(out, Exception):
            raise out
        return out


class FiguresTest(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.cfg = self.env.cfg
        self.es = self.cfg.engine_settings("codex")

    def tearDown(self):
        self.env.close()

    def add(self, doi, relevance, journal="nature", url="", **extra):
        self.env.add_paper(doi, status="summarized", journal=journal, url=url, relevance=relevance,
                           summary_json=json.dumps(GOOD), **extra)

    def test_elsevier_pii_from_cell_link(self):
        self.assertEqual(figures.elsevier_pii(CELL_URL), "S0092867426011190")
        self.assertIsNone(figures.elsevier_pii("https://www.nature.com/articles/s41586-026-1"))

    def test_ga_lookup_only_for_journals_with_ga_source(self):
        self.add("10.1016/j.cell.1", 3, journal="cell", url=CELL_URL)
        self.add("10.1038/n1", 9)
        ga = figures.ELSEVIER_GA.format(pii="S0092867426011190")
        client = FakeClient({ga: True})
        self.assertEqual(figures.check_graphical_abstracts(self.cfg, self.env.conn, client),
                         {"found": 1, "none": 1, "error": 0})
        self.assertEqual(client.urls, [ga])  # Nature 는 요청하지 않음
        self.assertEqual(db.get(self.env.conn, "10.1016/j.cell.1")["ga_url"], ga)
        self.assertEqual(db.get(self.env.conn, "10.1038/n1")["ga_checked"], 1)

    def test_ga_network_error_retries_next_run(self):
        self.add("10.1016/j.cell.1", 9, journal="cell", url=CELL_URL)
        ga = figures.ELSEVIER_GA.format(pii="S0092867426011190")
        figures.check_graphical_abstracts(self.cfg, self.env.conn, FakeClient({ga: HttpError(ga, 503, "x")}))
        self.assertEqual(db.get(self.env.conn, "10.1016/j.cell.1")["ga_checked"], 0)

    def test_ai_figure_only_for_relevant_papers_without_ga(self):
        self.add("10.1/high", 9, ga_checked=1)
        self.add("10.1/low", 6, ga_checked=1)
        self.add("10.1/ga", 10, ga_checked=1, ga_url="https://ars.els-cdn.com/x-ga1.jpg")
        self.add("10.1/unchecked", 10)  # GA 확인 전이면 아직 그리지 않음
        engine = ScriptedEngine(png_bytes())
        res = figures.generate_ai_figures(self.cfg, self.env.conn, lambda p: engine(p, None), self.es)
        self.assertEqual((res.done, res.failed), (1, 0))
        row = db.get(self.env.conn, "10.1/high")
        self.assertEqual(row["figure_path"], "figures/10.1_high.webp")
        webp = self.cfg.public_data_dir / row["figure_path"]
        self.assertEqual(webp.read_bytes()[8:12], b"WEBP")
        from PIL import Image
        with Image.open(webp) as im:
            self.assertEqual(im.width, self.cfg.figures.max_width)
        self.assertIn("<summary>", engine.prompts[0])

    def test_quota_stops_and_keeps_candidates(self):
        self.add("10.1/a", 9, ga_checked=1)
        self.add("10.1/b", 8, ga_checked=1)
        engine = ScriptedEngine(QuotaExhausted("You've hit your usage limit"))
        res = figures.generate_ai_figures(self.cfg, self.env.conn, lambda p: engine(p, None), self.es)
        self.assertEqual((res.stopped, res.remaining), ("quota", 2))
        self.assertEqual(len(figures.ai_candidates(self.cfg, self.env.conn)), 2)

    def test_failures_stop_after_max_attempts(self):
        self.add("10.1/a", 9, ga_checked=1)
        for _ in range(self.cfg.figures.max_attempts):
            engine = ScriptedEngine(EngineError("no image"))
            figures.generate_ai_figures(self.cfg, self.env.conn, lambda p: engine(p, None), self.es)
        self.assertEqual(figures.ai_candidates(self.cfg, self.env.conn), [])

    def test_site_shows_ga_link_and_labels_ai_figure(self):
        self.add("10.1/ga", 9, ga_checked=1, ga_url=figures.ELSEVIER_GA.format(pii="S0092867426011190"))
        self.add("10.1/ai", 8, ga_checked=1, figure_path="figures/10.1_ai.webp", figure_model="m")
        self.add("10.1/bad", 7, ga_checked=1, figure_path="../../etc/passwd")
        (self.cfg.public_data_dir / "figures").mkdir(parents=True)
        (self.cfg.public_data_dir / "figures" / "10.1_ai.webp").write_bytes(b"x")
        export.export(self.cfg, self.env.conn)
        with tempfile.TemporaryDirectory() as out:
            site_build.build(self.cfg.public_data_dir, Path(out))
            index = (Path(out) / "index.html").read_text()
            ga_page = (Path(out) / "paper-10.1_ga.html").read_text()
            ai_page = (Path(out) / "paper-10.1_ai.html").read_text()
            bad_page = (Path(out) / "paper-10.1_bad.html").read_text()
            self.assertTrue((Path(out) / "figures" / "10.1_ai.webp").exists())
        ga = 'src="https://ars.els-cdn.com/content/image/1-s2.0-S0092867426011190-ga1.jpg"'
        self.assertIn(ga, index)  # 카드 썸네일
        self.assertIn(ga, ga_page)
        self.assertIn("1-s2.0-S0092867426011190-ga1_lrg.jpg", ga_page)
        self.assertIn('src="figures/10.1_ai.webp"', index)
        self.assertIn("AI 생성 개요도", ai_page)
        self.assertNotIn("etc/passwd", index + bad_page)


if __name__ == "__main__":
    unittest.main()
