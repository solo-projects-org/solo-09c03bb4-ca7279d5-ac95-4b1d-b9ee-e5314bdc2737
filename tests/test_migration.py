"""旧版本数据库 (无配对指标列) 的升级迁移。"""

from app.database import connect, init_db
from app.ingest import ingest_batch, parse_csv
from app.query import select_rectangle

from tests._base import DBTestCase, csv_text


class MigrationTest(DBTestCase):
    def test_old_schema_gets_matched_columns(self):
        # 手工构造旧版 block_stats (只有单期指标)
        self.conn.execute(
            "CREATE TABLE old_stats AS SELECT 1 AS bx, 1 AS by, 'x' AS phase, "
            "0 AS n_cells, 0 AS n_nonnull, 0 AS sum_depth_cm WHERE 0"
        )
        self.conn.execute("DROP TABLE block_stats")
        self.conn.execute(
            "CREATE TABLE block_stats (bx INTEGER, by INTEGER, phase TEXT, "
            "n_cells INTEGER, n_nonnull INTEGER, sum_depth_cm INTEGER, "
            "PRIMARY KEY(bx, by, phase))"
        )
        self.conn.execute(
            "INSERT INTO block_stats VALUES (0, 0, 'before', 1, 1, 200)"
        )
        self.conn.commit()

        # 重新初始化应自动补列且不丢数据
        init_db(self.conn)
        row = self.conn.execute(
            "SELECT * FROM block_stats WHERE bx=0 AND by=0"
        ).fetchone()
        self.assertEqual(row["sum_depth_cm"], 200)
        self.assertEqual(row["n_matched"], 0)
        self.assertEqual(row["diff_cm"], 0)

        # 迁移后补测能正常重算配对指标
        ingest_batch(self.conn, "before",
                     [parse_csv(csv_text([(0, 0, 1, 200), (1, 1, 1, 210)]))[0]])
        # 新连接走完整迁移路径也不报错
        conn2 = connect(self.db_path)
        init_db(conn2)
        conn2.close()
        r = select_rectangle(self.conn, 0, 0, 0, 0)
        self.assertEqual(r.area_cells_m2, 0)  # after 期尚无数据
