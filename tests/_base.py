"""测试夹具: 临时数据库 + 两期 40x40 格测深数据。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import connect, init_db  # noqa: E402
from app.ingest import Reading, ingest_batch, parse_csv  # noqa: E402

GRID = 40  # 0..39, 含两个完整块 (块大小 16) 与边缘


def csv_text(rows, header="x,y,survey_no,depth_cm"):
    lines = [header]
    for x, y, sno, depth in rows:
        lines.append(f"{x},{y},{sno},{' ' if depth is None else depth}")
    return "\n".join(lines) + "\n"


def full_grid_phase(phase, survey_no, depth_fn):
    return [
        Reading(x, y, survey_no, depth_fn(x, y))
        for x in range(GRID) for y in range(GRID)
    ]


def before_depth(x, y):
    return 200 + x + y          # 200..278 cm


def after_depth(x, y):
    return before_depth(x, y) + 30 if x < 32 and y < 32 else before_depth(x, y)


class DBTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        os.environ["DB_PATH"] = str(self.db_path)
        self.conn = connect(self.db_path)
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()
        os.environ.pop("DB_PATH", None)

    def seed_both(self):
        """灌入两期全网格基线数据, 返回期望差值合计 (厘米)。"""
        b = ingest_batch(self.conn, "before",
                         full_grid_phase("before", 1, before_depth))
        a = ingest_batch(self.conn, "after",
                         full_grid_phase("after", 1, after_depth))
        return b, a
