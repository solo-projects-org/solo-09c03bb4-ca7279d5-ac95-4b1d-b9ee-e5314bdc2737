"""两台服务共用一个库、BLOCK_SIZE 不同的多实例场景。

用同一 SQLite 文件 + 运行时切换 ``config.BLOCK_SIZE`` 模拟两台实例:
- 普通核量各按本机块大小读整块汇总, 边缘按细格算;
- 任一台补测, 每种已登记尺寸只更新受影响块, 汇总与读数同事务生效;
- 一台停用期间的补测, 在它重新启动对账时补齐;
- 整批退回时两边原结果都不变。
"""

import os
import tempfile
import unittest
from pathlib import Path

from app import config
from app import query as query_mod
from app import stats as stats_mod
from app.database import (
    connect,
    get_data_version,
    get_stats_version,
    init_db,
)
from app.ingest import Reading, ingest_batch, parse_csv
from app.query import select_rectangle
from app.stats import ensure_block_size

from tests._base import csv_text

SIZE_A = 16
SIZE_B = 10


class MultiInstanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "shared.db"
        os.environ["DB_PATH"] = str(self.db_path)
        self._saved_size = config.BLOCK_SIZE
        # 实例 A 建库并登记尺寸 16
        config.BLOCK_SIZE = SIZE_A
        stats_mod.config.BLOCK_SIZE = SIZE_A
        query_mod.config.BLOCK_SIZE = SIZE_A
        self.conn = connect(self.db_path)
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()
        os.environ.pop("DB_PATH", None)
        config.BLOCK_SIZE = self._saved_size
        stats_mod.config.BLOCK_SIZE = self._saved_size
        query_mod.config.BLOCK_SIZE = self._saved_size

    def _switch(self, size):
        """模拟另一台实例以不同 BLOCK_SIZE 打开同一个库。"""
        config.BLOCK_SIZE = size
        stats_mod.config.BLOCK_SIZE = size
        query_mod.config.BLOCK_SIZE = size

    def _seed(self):
        """两期 20x20 基线: 前测 200cm, 后测 230cm (+30cm)。"""
        cells = [(x, y) for x in range(20) for y in range(20)]
        ingest_batch(
            self.conn, "before",
            [Reading(x, y, 1, 200) for x, y in cells],
        )
        ingest_batch(
            self.conn, "after",
            [Reading(x, y, 1, 230) for x, y in cells],
        )

    def _assert_matches_cellwise(self, size, rect):
        self._switch(size)
        mixed = select_rectangle(self.conn, *rect)
        cellwise = select_rectangle(self.conn, *rect, cellwise=True)
        self.assertEqual(
            mixed.area_cells_m2, cellwise.area_cells_m2,
            f"size={size} rect={rect} 面积不一致",
        )
        self.assertAlmostEqual(
            mixed.net_volume_m3, cellwise.net_volume_m3, places=6,
            msg=f"size={size} rect={rect} 方量不一致",
        )
        return mixed

    def test_two_block_sizes_coexist_without_overwriting(self):
        self._seed()
        # 实例 B (尺寸 10) 首次接入: 全量建自己的汇总
        self._switch(SIZE_B)
        report = ensure_block_size(self.conn, SIZE_B)
        self.assertEqual(report["action"], "rebuild")

        # 两套汇总行数独立: 20x20 网格 -> 尺寸16 有 2x2=4 块, 尺寸10 有 2x2=4 块,
        # 每期各一套
        for size, expected_blocks in ((SIZE_A, 4), (SIZE_B, 4)):
            n = self.conn.execute(
                "SELECT COUNT(*) n FROM block_stats "
                "WHERE block_size=? AND phase='before'", (size,),
            ).fetchone()["n"]
            self.assertEqual(n, expected_blocks, size)

        # 同一数据版本下, 两台各自的整块+边缘核量都与逐格核量一致
        rects = [(0, 0, 15, 15), (0, 0, 19, 19), (3, 5, 17, 14),
                 (10, 10, 19, 19), (0, 0, 9, 9)]
        for rect in rects:
            self._assert_matches_cellwise(SIZE_A, rect)
            self._assert_matches_cellwise(SIZE_B, rect)

    def test_interleaved_supplements_update_only_touched_blocks_per_size(self):
        self._seed()
        self._switch(SIZE_B)
        ensure_block_size(self.conn, SIZE_B)

        # 快照两套汇总, 随后两台交错补测
        def snapshot(size):
            return {
                (r["bx"], r["by"], r["phase"]):
                    (r["n_nonnull"], r["sum_depth_cm"], r["n_matched"], r["diff_cm"])
                for r in self.conn.execute(
                    "SELECT * FROM block_stats WHERE block_size=?", (size,)
                ).fetchall()
            }

        before_a, before_b = snapshot(SIZE_A), snapshot(SIZE_B)

        # A(16) 补测格 (1,1): 只动 16 尺寸的块 (0,0) 与 10 尺寸的块 (0,0)
        self._switch(SIZE_A)
        ingest_batch(self.conn, "after",
                     [parse_csv(csv_text([(1, 1, 2, 260)]))[0]])  # +60 vs before
        # B(10) 补测格 (15,15): 16 尺寸属块 (0,0); 10 尺寸属块 (1,1)
        self._switch(SIZE_B)
        ingest_batch(self.conn, "after",
                     [parse_csv(csv_text([(15, 15, 3, 290)]))[0]])  # +90 vs before

        after_a, after_b = snapshot(SIZE_A), snapshot(SIZE_B)
        changed_a = sorted(k for k in before_a if before_a[k] != after_a.get(k))
        changed_b = sorted(k for k in before_b if before_b[k] != after_b.get(k))
        # 尺寸16: 两格都落在块 (0,0) -> 仅 before/after 两行变化
        self.assertEqual(changed_a, [(0, 0, "after"), (0, 0, "before")])
        # 尺寸10: (1,1) 在块(0,0), (15,15) 在块(1,1) -> 两个块的行变化
        self.assertEqual(
            changed_b,
            [(0, 0, "after"), (0, 0, "before"),
             (1, 1, "after"), (1, 1, "before")],
        )

        # 汇总版本都追平数据版本
        version = get_data_version(self.conn)
        self.assertEqual(get_stats_version(self.conn, SIZE_A), version)
        self.assertEqual(get_stats_version(self.conn, SIZE_B), version)

        # 两台各自核量仍与逐格核量一致 (同一数据版本)
        for rect in [(0, 0, 19, 19), (0, 0, 15, 15), (10, 10, 19, 19),
                     (1, 1, 1, 1), (15, 15, 15, 15), (3, 3, 17, 17)]:
            self._assert_matches_cellwise(SIZE_A, rect)
            self._assert_matches_cellwise(SIZE_B, rect)

        # 手工核对方量: 全选区 400 格, 基线每格 +30cm,
        # (1,1) 改为 +60 (多 30cm), (15,15) 改为 +90 (多 60cm)
        expected_m3 = (400 * 30 + 30 + 60) / 100.0
        cell = select_rectangle(self.conn, 0, 0, 19, 19, cellwise=True)
        self.assertAlmostEqual(cell.net_volume_m3, expected_m3)

    def test_stopped_instance_catches_up_on_restart(self):
        self._seed()
        # 用两条独立连接模拟两台进程; B 注册后关闭连接模拟停用
        conn_b = connect(self.db_path)
        self._switch(SIZE_B)
        ensure_block_size(conn_b, SIZE_B)
        conn_b.close()

        # A 独自运行并补测两次: B 仍在登记表里, A 在同一事务内顺带维护
        # B 尺寸的受影响块 (主路径: 任一台补测, 每种已登记尺寸一起生效)
        self._switch(SIZE_A)
        v_before = get_data_version(self.conn)
        ingest_batch(self.conn, "after",
                     [parse_csv(csv_text([(2, 2, 2, 300)]))[0]])
        ingest_batch(self.conn, "before",
                     [parse_csv(csv_text([(12, 18, 5, 180)]))[0]])
        version_after = get_data_version(self.conn)
        self.assertEqual(
            get_stats_version(self.conn, SIZE_A), version_after
        )
        self.assertEqual(
            get_stats_version(self.conn, SIZE_B), version_after
        )

        # 模拟 B 停用期间错过补测: 把它的汇总版本回退到旧值
        # (等价于历史漏算/旧库汇总未跟进), 汇总行保持旧内容
        self.conn.execute(
            "UPDATE block_size_registry SET stats_version=? WHERE block_size=?",
            (v_before, SIZE_B),
        )

        # B 重新启动: 对账发现落后 -> 全量补算; 读数与数据版本号不变
        conn_b = connect(self.db_path)
        self._switch(SIZE_B)
        report = ensure_block_size(conn_b, SIZE_B)
        self.assertEqual(report["action"], "catch-up")
        conn_b.close()
        self.assertEqual(get_data_version(self.conn), version_after)
        self.assertEqual(get_stats_version(self.conn, SIZE_B), version_after)

        # 补算后 B 的核量与逐格核量一致; A 不受影响
        for rect in [(0, 0, 19, 19), (0, 0, 9, 9), (10, 10, 19, 19),
                     (2, 2, 2, 2), (12, 18, 12, 18)]:
            self._assert_matches_cellwise(SIZE_A, rect)
            self._assert_matches_cellwise(SIZE_B, rect)

        # 再次启动无需重建
        conn_b = connect(self.db_path)
        self.assertEqual(
            ensure_block_size(conn_b, SIZE_B)["action"], "up-to-date"
        )
        conn_b.close()

    def test_rejected_batch_leaves_both_sizes_intact(self):
        self._seed()
        self._switch(SIZE_B)
        ensure_block_size(self.conn, SIZE_B)

        def snapshot(size):
            return {
                (r["bx"], r["by"], r["phase"]):
                    (r["n_nonnull"], r["sum_depth_cm"], r["n_matched"], r["diff_cm"])
                for r in self.conn.execute(
                    "SELECT * FROM block_stats WHERE block_size=?", (size,)
                ).fetchall()
            }

        snap_a, snap_b = snapshot(SIZE_A), snapshot(SIZE_B)
        version = get_data_version(self.conn)

        # B 上传同号异值批次 -> 整批退回 (与 HTTP 路由一致: 回滚后独立留痕)
        self._switch(SIZE_B)
        from app.ingest import IngestError, record_rejection
        bad_batch = parse_csv(csv_text([(0, 0, 1, 999), (5, 5, 1, 888)]))
        with self.assertRaises(IngestError):
            ingest_batch(self.conn, "after", bad_batch)
        record_rejection(self.conn, "after", bad_batch, "整批退回: 测试")

        # 两套汇总、读数、数据版本全部不变
        self.assertEqual(snapshot(SIZE_A), snap_a)
        self.assertEqual(snapshot(SIZE_B), snap_b)
        self.assertEqual(get_data_version(self.conn), version)
        self.assertEqual(get_stats_version(self.conn, SIZE_A), version)
        self.assertEqual(get_stats_version(self.conn, SIZE_B), version)

        # 两边仍查到原结果
        for rect in [(0, 0, 19, 19), (0, 0, 15, 15)]:
            self._assert_matches_cellwise(SIZE_A, rect)
            self._assert_matches_cellwise(SIZE_B, rect)

        # 留痕表记录了退回
        rejected = self.conn.execute(
            "SELECT COUNT(*) n FROM ingest_batches WHERE status='rejected'"
        ).fetchone()["n"]
        self.assertGreaterEqual(rejected, 1)

    def test_new_size_registers_while_other_size_keeps_working(self):
        self._seed()
        # 此时只有尺寸 16 登记; 尺寸 12 的实例首次接入
        self._switch(12)
        report = ensure_block_size(self.conn, 12)
        self.assertEqual(report["action"], "rebuild")
        self.assertEqual(get_stats_version(self.conn, 12),
                         get_data_version(self.conn))
        # 尺寸 16 的汇总原样保留且仍正确
        self._assert_matches_cellwise(SIZE_A, (0, 0, 15, 15))
        self._assert_matches_cellwise(12, (0, 0, 11, 11))

        # 尺寸 10 也接入; 此后三种尺寸都已登记
        self._switch(10)
        self.assertEqual(ensure_block_size(self.conn, 10)["action"], "rebuild")

        # 任一台补测, 三种已登记尺寸各只动受影响块且版本一起追平
        self._switch(SIZE_A)
        ingest_batch(self.conn, "after",
                     [parse_csv(csv_text([(0, 0, 2, 255)]))[0]])
        for size in (SIZE_A, 10, 12):
            self.assertEqual(
                get_stats_version(self.conn, size), get_data_version(self.conn)
            )
            self._assert_matches_cellwise(size, (0, 0, 19, 19))
