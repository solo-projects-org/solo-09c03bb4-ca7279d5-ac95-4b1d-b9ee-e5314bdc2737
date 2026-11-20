"""分块统计与选区核量: 整块/边缘一致性、负值、空水深、只重算受影响块。"""

from app.ingest import ingest_batch, parse_csv
from app.query import select_rectangle
from app.stats import block_of

from tests._base import (
    DBTestCase, GRID, after_depth, before_depth, csv_text,
)


def vol_between(x0, y0, x1, y1):
    """期望净挖方 (m3): 后测减前测, 仅 x<32,y<32 的格有 +30cm 差。"""
    total_cm = 0
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            if x < 32 and y < 32:
                total_cm += 30
    return total_cm / 100.0


class StatsQueryTest(DBTestCase):
    def setUp(self):
        super().setUp()
        self.seed_both()

    def test_block_stats_match_readings(self):
        # 块 (0,0) 覆盖 0..15 x 0..15, 两期各 256 个非空读数
        for phase in ("before", "after"):
            row = self.conn.execute(
                "SELECT * FROM block_stats WHERE bx=0 AND by=0 AND phase=?",
                (phase,),
            ).fetchone()
            self.assertEqual(row["n_cells"], 256)
            self.assertEqual(row["n_nonnull"], 256)
        expected_sum = sum(before_depth(x, y) for x in range(16) for y in range(16))
        b = self.conn.execute(
            "SELECT sum_depth_cm s FROM block_stats WHERE bx=0 AND by=0 AND phase='before'"
        ).fetchone()
        self.assertEqual(b["s"], expected_sum)

    def test_whole_block_selection_uses_stats(self):
        # 恰好 4 个完整块: 0..31 x 0..31, 全部走 block_stats 汇总
        r = select_rectangle(self.conn, 0, 0, 31, 31)
        self.assertEqual(r.whole_blocks, 4)
        self.assertEqual(r.edge_cells, 0)
        self.assertEqual(r.area_cells_m2, 32 * 32)
        self.assertAlmostEqual(r.net_volume_m3, vol_between(0, 0, 31, 31))

    def test_mixed_block_and_edge_equals_cellwise(self):
        # 切开块边界: 选 5..34, 7..29
        for rect in [(5, 7, 34, 29), (0, 0, 39, 39), (3, 3, 17, 17),
                     (16, 16, 31, 31), (0, 0, 0, 0), (20, 25, 38, 39)]:
            mixed = select_rectangle(self.conn, *rect)
            cellwise = select_rectangle(self.conn, *rect, cellwise=True)
            self.assertEqual(mixed.area_cells_m2, cellwise.area_cells_m2, rect)
            self.assertAlmostEqual(
                mixed.net_volume_m3, cellwise.net_volume_m3, places=6, msg=rect
            )
            self.assertAlmostEqual(
                mixed.net_volume_m3, vol_between(*rect), places=6, msg=rect
            )

    def test_negative_depth_change_counts(self):
        # 补测: 后测格 (1,1) 水深减 50cm -> 净挖方为负 0.5 m3
        ingest_batch(self.conn, "after", [parse_csv(csv_text([(1, 1, 2,
                                                               after_depth(1, 1) - 50)]))[0]])
        r = select_rectangle(self.conn, 0, 0, 15, 15)
        # 原 256 格每格 +30cm = 76.8 m3, 其中一格再 -50cm -> 76.3
        self.assertAlmostEqual(r.net_volume_m3, 76.8 - 0.5)
        # 细格路径同样照计负值
        r2 = select_rectangle(self.conn, 0, 0, 15, 15, cellwise=True)
        self.assertAlmostEqual(r2.net_volume_m3, 76.3)

    def test_empty_depth_drops_cell_from_metric(self):
        # 后测补测把 (2,2) 写成空水深, 该格退出计量面积与方量
        ingest_batch(self.conn, "after", [parse_csv(csv_text([(2, 2, 2, None)]))[0]])
        r = select_rectangle(self.conn, 0, 0, 15, 15)
        self.assertEqual(r.area_cells_m2, 255)
        # 该格整格退出计量: 原 76.8 m3 减去它原来的 0.30 m3
        self.assertAlmostEqual(r.net_volume_m3, 76.8 - 0.30)
        # 统计行仍保留读数 (空水深): n_cells 不减, n_nonnull 减 1
        st = self.conn.execute(
            "SELECT * FROM block_stats WHERE bx=0 AND by=0 AND phase='after'"
        ).fetchone()
        self.assertEqual(st["n_cells"], 256)
        self.assertEqual(st["n_nonnull"], 255)

    def test_only_phases_with_both_readings_count(self):
        # 补测只在 before 期新增格 (40,40): after 无读数, 不计量
        ingest_batch(self.conn, "before", [parse_csv(csv_text([(40, 40, 2, 100)]))[0]])
        r = select_rectangle(self.conn, 39, 39, 40, 40)
        self.assertEqual(r.area_cells_m2, 1)   # 仅 (39,39) 两期都有
        self.assertAlmostEqual(r.net_volume_m3, 0.0)

    def test_supplement_recomputes_only_touched_blocks(self):
        before = {
            (r["bx"], r["by"], r["phase"]): (r["n_nonnull"], r["sum_depth_cm"])
            for r in self.conn.execute("SELECT * FROM block_stats").fetchall()
        }
        # 补测只动块 (0,0) 内的格
        ingest_batch(self.conn, "before", [parse_csv(csv_text([(1, 1, 2, 500)]))[0]])
        after = {
            (r["bx"], r["by"], r["phase"]): (r["n_nonnull"], r["sum_depth_cm"])
            for r in self.conn.execute("SELECT * FROM block_stats").fetchall()
        }
        changed = [k for k in before if before[k] != after.get(k)]
        self.assertEqual(changed, [(0, 0, "before")])
        self.assertEqual(block_of(1, 1), (0, 0))

    def test_supplement_removing_all_rows_clears_stats_row(self):
        # 整块格全部用更高测次写成空水深后再无非空读数 -> 统计行仍在但 n_nonnull=0
        rows = [(x, y, 9, None) for x in range(16) for y in range(16)]
        ingest_batch(self.conn, "after", parse_csv(csv_text(rows)))
        st = self.conn.execute(
            "SELECT * FROM block_stats WHERE bx=0 AND by=0 AND phase='after'"
        ).fetchone()
        self.assertEqual(st["n_nonnull"], 0)
        self.assertEqual(st["n_cells"], 256)

    def test_version_bumps_on_commit_only(self):
        v0 = select_rectangle(self.conn, 0, 0, 1, 1).data_version
        ingest_batch(self.conn, "before", [parse_csv(csv_text([(0, 0, 3, 999)]))[0]])
        v1 = select_rectangle(self.conn, 0, 0, 1, 1).data_version
        self.assertEqual(v1, v0 + 1)
