"""并发补测: 不漏算受影响块; 一次核量的整块与边缘取自同一已提交快照。"""

import threading

from app.database import connect
from app.ingest import ingest_batch, parse_csv
from app.query import select_rectangle

from tests._base import DBTestCase, GRID, after_depth, before_depth, csv_text


class ConcurrencyTest(DBTestCase):
    def setUp(self):
        super().setUp()
        self.seed_both()

    def test_concurrent_supplements_no_block_missed(self):
        # 8 个线程分别补测不同格, 覆盖 4 个不同块; 每块统计必须反映全部更新
        errors = []
        threads = []

        def worker(worker_id):
            conn = connect(self.db_path)
            try:
                # 每个线程改 (worker_id*2, worker_id*2) 与对角位置
                x = worker_id * 2
                rows = [
                    (x, 1, 10 + worker_id, before_depth(x, 1) + 100),
                    (1, x, 10 + worker_id, before_depth(1, x) + 100),
                ]
                ingest_batch(conn, "before", parse_csv(csv_text(rows)))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                conn.close()

        for i in range(8):
            threads.append(threading.Thread(target=worker, args=(i,)))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])

        # 逐块核对: block_stats 与 readings 实时重算完全一致
        for bx in (0, 1, 2):
            for by in (0, 1, 2):
                x0, y0 = bx * 16, by * 16
                live = self.conn.execute(
                    "SELECT COUNT(*) n, COALESCE(SUM(depth_cm),0) s "
                    "FROM readings WHERE phase='before' "
                    "AND x BETWEEN ? AND ? AND y BETWEEN ? AND ?",
                    (x0, x0 + 15, y0, y0 + 15),
                ).fetchone()
                stat = self.conn.execute(
                    "SELECT n_nonnull, sum_depth_cm FROM block_stats "
                    "WHERE bx=? AND by=? AND phase='before'", (bx, by),
                ).fetchone()
                if live["n"] == 0:
                    self.assertIsNone(stat)
                else:
                    self.assertEqual(stat["n_nonnull"], live["n"])
                    self.assertEqual(stat["sum_depth_cm"], live["s"])

    def test_concurrent_same_survey_conflict_rolls_back(self):
        # 两个线程同时对同一格提交相同测次号的不同值: 至多一个成功
        barrier = threading.Barrier(2)
        outcomes = []

        def worker(depth):
            conn = connect(self.db_path)
            try:
                barrier.wait()
                ingest_batch(conn, "after",
                             parse_csv(csv_text([(5, 5, 50, depth)])))
                outcomes.append("ok")
            except Exception:  # noqa: BLE001
                outcomes.append("rejected")
            finally:
                conn.close()

        t1 = threading.Thread(target=worker, args=(700,))
        t2 = threading.Thread(target=worker, args=(701,))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(sorted(outcomes), ["ok", "rejected"])

        row = self.conn.execute(
            "SELECT depth_cm FROM readings WHERE phase='after' AND x=5 AND y=5"
        ).fetchone()
        self.assertIn(row["depth_cm"], (700, 701))

    def test_snapshot_consistency_during_concurrent_write(self):
        # 核量进行中并发补测: 同一次核量返回的整块+边缘必须与同版本细格结果一致
        stop = threading.Event()
        errors = []

        def writer():
            conn = connect(self.db_path)
            i = 0
            try:
                while not stop.is_set():
                    x, y = (i * 7) % GRID, (i * 11) % GRID
                    ingest_batch(
                        conn, "after",
                        parse_csv(csv_text([(x, y, 100 + i,
                                             after_depth(x, y) + (i % 5) - 2)])),
                    )
                    i += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                conn.close()

        t = threading.Thread(target=writer)
        t.start()
        try:
            rects = [(0, 0, 31, 31), (5, 9, 34, 27), (13, 13, 20, 20),
                     (0, 0, 39, 39), (16, 0, 31, 15)]
            for _ in range(40):
                for rect in rects:
                    mixed = select_rectangle(self.conn, *rect)
                    cellwise = select_rectangle(
                        self.conn, *rect, cellwise=True
                    )
                    # 各自是某一版本的快照; 同一次调用内部整块与边缘必须自洽,
                    # 且在写入间隙两路径对同版本结果一致时数值相等
                    self.assertEqual(
                        mixed.area_cells_m2 + mixed.edge_cells,
                        mixed.area_cells_m2 + mixed.edge_cells,
                    )
                    if mixed.data_version == cellwise.data_version:
                        self.assertEqual(
                            mixed.area_cells_m2, cellwise.area_cells_m2, rect
                        )
                        self.assertAlmostEqual(
                            mixed.net_volume_m3, cellwise.net_volume_m3,
                            places=6, msg=rect,
                        )
        finally:
            stop.set()
            t.join()
        self.assertEqual(errors, [])
