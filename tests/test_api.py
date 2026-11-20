"""HTTP 端到端: 上传 -> 核量 -> 补测 -> 退回 -> 留痕。"""

import os
import tempfile
import unittest
from pathlib import Path

import httpx

from app.database import connect, init_db
from app.main import app

from tests._base import csv_text


class ApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "api.db"
        os.environ["DB_PATH"] = str(self.db_path)
        conn = connect(self.db_path)
        init_db(conn)
        conn.close()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        self.tmp.cleanup()
        os.environ.pop("DB_PATH", None)

    async def _upload(self, phase, rows):
        return await self.client.post(
            f"/api/surveys/{phase}",
            files={"file": ("s.csv", csv_text(rows), "text/csv")},
        )

    async def test_full_workflow_and_rejection(self):
        # 前测: 16x16 整块, 水深 200cm
        before_rows = [(x, y, 1, 200) for x in range(16) for y in range(16)]
        r = await self._upload("before", before_rows)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["rows_applied"], 256)
        self.assertEqual(r.json()["blocks_touched"], [[0, 0]])

        # 后测: 同样格, 水深 240cm (+40cm)
        after_rows = [(x, y, 1, 240) for x in range(16) for y in range(16)]
        r = await self._upload("after", after_rows)
        self.assertEqual(r.status_code, 200)

        # 核量整块: 256 m2, 256*40/100 = 102.4 m3
        r = await self.client.get("/api/selection",
                                  params={"x0": 0, "y0": 0, "x1": 15, "y1": 15})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["area_cells_m2"], 256)
        self.assertAlmostEqual(body["net_volume_m3"], 102.4)
        self.assertEqual(body["whole_blocks"], 1)
        self.assertEqual(body["edge_cells"], 0)

        # 切开块: 0..9 走边缘细格, 数值应与全细格一致
        r1 = (await self.client.get(
            "/api/selection",
            params={"x0": 0, "y0": 0, "x1": 9, "y1": 9})).json()
        r2 = (await self.client.get(
            "/api/selection",
            params={"x0": 0, "y0": 0, "x1": 9, "y1": 9, "cellwise": True})).json()
        self.assertEqual(r1["area_cells_m2"], 100)
        self.assertAlmostEqual(r1["net_volume_m3"], r2["net_volume_m3"])

        # 同号异值 -> 409 整批退回
        r = await self._upload("after", [(0, 0, 1, 999)])
        self.assertEqual(r.status_code, 409)
        self.assertIn("整批退回", r.json()["detail"])

        # 退回不影响原结果
        r = (await self.client.get(
            "/api/selection",
            params={"x0": 0, "y0": 0, "x1": 15, "y1": 15})).json()
        self.assertAlmostEqual(r["net_volume_m3"], 102.4)

        # 更高测次补测一格, 块统计随之更新
        r = await self._upload("after", [(0, 0, 2, 190)])  # 比前测浅 10cm -> 负值
        self.assertEqual(r.status_code, 200)
        r = (await self.client.get(
            "/api/selection",
            params={"x0": 0, "y0": 0, "x1": 15, "y1": 15})).json()
        # 原 102.4, 该格从 +40cm 变成 -10cm, 差 -50cm = -0.5 m3
        self.assertAlmostEqual(r["net_volume_m3"], 101.9)

        # 留痕: 含 rejected
        batches = (await self.client.get("/api/batches")).json()["batches"]
        statuses = [b["status"] for b in batches]
        self.assertIn("rejected", statuses)
        self.assertIn("accepted", statuses)

        # 空水深补测覆盖
        r = await self._upload("after", [(1, 1, 3, None)])
        self.assertEqual(r.status_code, 200)
        r = (await self.client.get(
            "/api/selection",
            params={"x0": 0, "y0": 0, "x1": 15, "y1": 15})).json()
        self.assertEqual(r["area_cells_m2"], 255)

    async def test_bad_phase_and_bad_csv(self):
        self.assertEqual((await self._upload("middle", [(0, 0, 1, 1)])).status_code, 404)
        r = await self.client.post(
            "/api/surveys/before",
            files={"file": ("bad.csv", "a,b,c\n1,2,3\n", "text/csv")},
        )
        self.assertEqual(r.status_code, 400)

    async def test_health_extent_blocks(self):
        self.assertEqual((await self.client.get("/health")).json()["status"], "ok")
        await self._upload("before", [(0, 0, 1, 200)])
        ext = (await self.client.get("/api/extent")).json()
        self.assertEqual(ext["before"]["x0"], 0)
        self.assertIsNone(ext["after"])
        blocks = (await self.client.get("/api/blocks")).json()
        self.assertEqual(blocks["block_size"], 16)
        self.assertEqual(len(blocks["blocks"]), 1)

    async def test_index_page_served(self):
        r = await self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("港池疏浚", r.text)
        r = await self.client.get("/static/app.js")
        self.assertEqual(r.status_code, 200)
