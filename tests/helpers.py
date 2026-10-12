import dataclasses
import os
import tempfile
from pathlib import Path

from digest import config, db


class TempEnv:
    """임시 데이터 디렉터리·임시 repo 루트를 쓰는 Config 와 DB."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        old = os.environ.get("PAPER_DIGEST_HOME")
        os.environ["PAPER_DIGEST_HOME"] = str(root / "home")
        try:
            cfg = config.load()
        finally:
            if old is None:
                os.environ.pop("PAPER_DIGEST_HOME")
            else:
                os.environ["PAPER_DIGEST_HOME"] = old
        (root / "repo").mkdir()
        self.cfg = dataclasses.replace(cfg, repo_root=root / "repo")
        self.conn = db.connect(self.cfg.db_path)

    def add_paper(self, doi, status="ready", text="Some abstract text.", journal="nature", **extra):
        now = db.utcnow()
        path = self.cfg.raw_dir / doi.replace("/", "_") / "abstract.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        fields = dict(doi=doi, journal=journal, title=f"Title {doi}", authors="A, B", url="",
                      article_type="Article", published="2026-10-10", teaser=None,
                      first_seen=now, last_seen=now, digest_date="2026-10-12", status=status,
                      source_level="abstract", source_origin="pubmed", source_path=str(path))
        fields.update(extra)
        cols = ",".join(fields)
        self.conn.execute(f"INSERT INTO papers ({cols}) VALUES ({','.join('?' * len(fields))})",
                          tuple(fields.values()))
        self.conn.commit()

    def close(self):
        self.conn.close()
        self.tmp.cleanup()
