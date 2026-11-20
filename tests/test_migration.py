"""接入未记录块大小的已有库: 保留读数、批次留痕与数据版本, 块汇总重建。"""

from app.database import connect, get_data_version, get_stats_version, init_db
from app.ingest import ingest_batch, parse_csv
from app.query import select_rectangle
from app.stats import ensure_block_size

from tests._base import DBTestCase, csv_text


class LegacyLibraryMigrationTest(DBTestCase):
    def _build_legacy_db(self):
        """手工构造真实旧版库: readings/ingest_batches/meta 齐全,
        block_stats 为无 block_size 列的旧结构, 且不存在 block_size_registry。"""
        self.conn.execute("DROP TABLE block_size_registry")
        self.conn.execute("DROP TABLE block_stats")
        self.conn.execute(
            "CREATE TABLE block_stats (bx INTEGER, by INTEGER, phase TEXT, "
            "n_cells INTEGER, n_nonnull INTEGER, sum_depth_cm INTEGER, "
            "n_matched INTEGER NOT NULL DEFAULT 0, "
            "diff_cm INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY(bx, by, phase))"
        )
        # 旧汇总的几何含义未知 (块大小没记录), 内容不可信
        self.conn.execute(
            "INSERT INTO block_stats VALUES (0, 0, 'before', 1, 1, 200, 0, 0)"
        )
        self.conn.execute(
            "INSERT INTO block_stats VALUES (9, 9, 'after', 7, 7, 99999, 0, 0)"
        )
        # 直接写读数/留痕, 模拟历史积累数据
        self.conn.executemany(
            "INSERT INTO readings(phase, x, y, survey_no, depth_cm) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ("before", 0, 0, 1, 200), ("before", 1, 1, 1, 210),
                ("after", 0, 0, 1, 240), ("after", 1, 1, 1, 230),
            ],
        )
        self.conn.execute(
            "INSERT INTO ingest_batches(phase, survey_no, row_count, status, "
            "reject_reason, blocks_touched) VALUES "
            "('before', 1, 2, 'accepted', NULL, '[[0,0]]'),"
            "('after', 1, 99, 'rejected', '整批退回: 测试', NULL)"
        )
        self.conn.execute(
            "UPDATE meta SET value='7' WHERE key='data_version'"
        )
        return (
            7,
            self.conn.execute("SELECT COUNT(*) n FROM ingest_batches").fetchone()["n"],
            self.conn.execute("SELECT COUNT(*) n FROM readings").fetchone()["n"],
        )

    def test_legacy_library_preserves_readings_batches_version(self):
        version_before, batches_before, readings_before = self._build_legacy_db()

        # 重新初始化: 旧 block_stats 按新结构重建, 其它表原样保留
        init_db(self.conn)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) n FROM readings").fetchone()["n"],
            readings_before,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) n FROM ingest_batches"
            ).fetchone()["n"],
            batches_before,
        )
        rejected = self.conn.execute(
            "SELECT reject_reason FROM ingest_batches WHERE status='rejected'"
        ).fetchone()
        self.assertEqual(rejected["reject_reason"], "整批退回: 测试")
        self.assertEqual(get_data_version(self.conn), version_before)
        # 旧汇总行已清空, 且尚未登记任何块大小
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) n FROM block_stats").fetchone()["n"],
            0,
        )
        self.assertIsNone(get_stats_version(self.conn, 16))

        # 实例启动对账: 按本机块大小全量重建汇总, 数据版本不推进
        report = ensure_block_size(self.conn, 16)
        self.assertEqual(report["action"], "rebuild")
        self.assertEqual(get_data_version(self.conn), version_before)
        self.assertEqual(get_stats_version(self.conn, 16), version_before)

        row = self.conn.execute(
            "SELECT * FROM block_stats WHERE block_size=16 AND bx=0 AND by=0 "
            "AND phase='before'"
        ).fetchone()
        self.assertEqual(row["n_cells"], 2)
        self.assertEqual(row["sum_depth_cm"], 410)  # 200 + 210, 而非旧值 200
        self.assertEqual(row["n_matched"], 2)
        self.assertEqual(row["diff_cm"], (240 - 200) + (230 - 210))

        # 重建后核量与逐格核量一致
        mixed = select_rectangle(self.conn, 0, 0, 1, 1)
        cellwise = select_rectangle(self.conn, 0, 0, 1, 1, cellwise=True)
        self.assertEqual(mixed.area_cells_m2, cellwise.area_cells_m2)
        self.assertAlmostEqual(mixed.net_volume_m3, cellwise.net_volume_m3)

        # 重建后补测可正常重算, 并把数据版本从 7 推进到 8
        ingest_batch(
            self.conn, "before",
            [parse_csv(csv_text([(0, 0, 2, 195)]))[0]],
        )
        self.assertEqual(get_data_version(self.conn), version_before + 1)
        r = select_rectangle(self.conn, 0, 0, 0, 0)
        self.assertEqual(r.area_cells_m2, 1)
        self.assertAlmostEqual(r.net_volume_m3, (240 - 195) / 100.0)

        # 新连接走完整迁移+对账路径也不报错, 且无需再重建
        conn2 = connect(self.db_path)
        init_db(conn2)
        self.assertEqual(ensure_block_size(conn2, 16)["action"], "up-to-date")
        conn2.close()
