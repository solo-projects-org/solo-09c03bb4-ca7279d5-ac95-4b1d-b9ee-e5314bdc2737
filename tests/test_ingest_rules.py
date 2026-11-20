"""读数入库规则: 最高测次号、空水深覆盖、同号异值整批退回、旧测次跳过。"""

import sqlite3

from app.ingest import IngestError, ingest_batch, parse_csv, record_rejection

from tests._base import DBTestCase, csv_text


class IngestRulesTest(DBTestCase):
    def test_parse_aliases_and_empty_depth(self):
        text = "格X,格Y,测次号,厘米水深\n1,2,3,\n4,5,6,210\n"
        readings = parse_csv(text)
        self.assertEqual(len(readings), 2)
        self.assertIsNone(readings[0].depth_cm)
        self.assertEqual(readings[1].depth_cm, 210)

    def test_higher_survey_overwrites(self):
        ingest_batch(self.conn, "before", [parse_csv(csv_text([(0, 0, 1, 200)]))[0]])
        ingest_batch(self.conn, "before", [parse_csv(csv_text([(0, 0, 2, 215)]))[0]])
        row = self.conn.execute(
            "SELECT survey_no, depth_cm FROM readings WHERE phase='before' AND x=0 AND y=0"
        ).fetchone()
        self.assertEqual((row["survey_no"], row["depth_cm"]), (2, 215))

    def test_empty_depth_overwrites_old_value(self):
        ingest_batch(self.conn, "after", [parse_csv(csv_text([(7, 8, 1, 300)]))[0]])
        # 更高测次 + 空水深: 旧值必须被 NULL 覆盖
        ingest_batch(self.conn, "after", [parse_csv(csv_text([(7, 8, 2, None)]))[0]])
        row = self.conn.execute(
            "SELECT survey_no, depth_cm FROM readings WHERE phase='after' AND x=7 AND y=8"
        ).fetchone()
        self.assertEqual(row["survey_no"], 2)
        self.assertIsNone(row["depth_cm"])

    def test_same_survey_conflict_rejects_whole_batch(self):
        ingest_batch(self.conn, "before", [parse_csv(csv_text([(0, 0, 1, 200)]))[0]])
        before_rows = self.conn.execute("SELECT COUNT(*) n FROM readings").fetchone()["n"]
        before_version = self.conn.execute(
            "SELECT value FROM meta WHERE key='data_version'"
        ).fetchone()["value"]

        # 一批里: 一个新格 + 一个同号异值格, 整批都不能进库
        batch = parse_csv(csv_text([(1, 1, 1, 222), (0, 0, 1, 999)]))
        with self.assertRaises(IngestError):
            ingest_batch(self.conn, "before", batch)

        after_rows = self.conn.execute("SELECT COUNT(*) n FROM readings").fetchone()["n"]
        after_version = self.conn.execute(
            "SELECT value FROM meta WHERE key='data_version'"
        ).fetchone()["value"]
        self.assertEqual(before_rows, after_rows, "退回批次不得写入任何行")
        self.assertEqual(before_version, after_version, "退回批次不得推进数据版本")

        # 留痕表应记录拒绝 (独立事务, 且不推进数据版本)
        record_rejection(self.conn, "before", batch, "测试留痕")
        rej = self.conn.execute(
            "SELECT status FROM ingest_batches ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(rej["status"], "rejected")
        final_version = self.conn.execute(
            "SELECT value FROM meta WHERE key='data_version'"
        ).fetchone()["value"]
        self.assertEqual(before_version, final_version)

    def test_same_survey_same_value_is_idempotent(self):
        r = parse_csv(csv_text([(0, 0, 1, 200)]))[0]
        res1 = ingest_batch(self.conn, "before", [r])
        res2 = ingest_batch(self.conn, "before", [r])
        self.assertEqual(res1.rows_applied, 1)
        self.assertEqual(res2.rows_applied, 0)

    def test_lower_survey_is_skipped(self):
        ingest_batch(self.conn, "after", [parse_csv(csv_text([(3, 3, 5, 400)]))[0]])
        res = ingest_batch(self.conn, "after", [parse_csv(csv_text([(3, 3, 4, 100)]))[0]])
        self.assertEqual(res.rows_skipped_stale, 1)
        row = self.conn.execute(
            "SELECT depth_cm FROM readings WHERE phase='after' AND x=3 AND y=3"
        ).fetchone()
        self.assertEqual(row["depth_cm"], 400)

    def test_duplicate_conflicting_row_in_one_file_rejected(self):
        with self.assertRaises(IngestError):
            parse_csv(csv_text([(0, 0, 1, 200), (0, 0, 1, 201)]))

    def test_malformed_csv_rejected(self):
        with self.assertRaises(IngestError):
            parse_csv("a,b,c\n1,2,3\n")
        with self.assertRaises(IngestError):
            parse_csv(csv_text([("x", 0, 1, 200)]))
