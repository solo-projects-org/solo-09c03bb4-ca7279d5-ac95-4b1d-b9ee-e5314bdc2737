"""随机矩形 + 负坐标 + 跨块场景下, 整块+边缘 与 全细格 结果必须完全一致。"""

import random

from app.database import connect, init_db
from app.ingest import Reading, ingest_batch
from app.query import select_rectangle


def main():
    conn = connect("/tmp/dredge-run/random_check.db")
    init_db(conn)

    rng = random.Random(42)
    # 含负坐标的两期数据, 后测随机增减, 部分空水深
    coords = [(x, y) for x in range(-20, 21) for y in range(-20, 21)]
    before, after = [], []
    for x, y in coords:
        before.append(Reading(x, y, 1, 200 + rng.randint(0, 50)))
        d = 200 + rng.randint(0, 50)
        after.append(Reading(x, y, 1, None if rng.random() < 0.05 else d))
    ingest_batch(conn, "before", before)
    ingest_batch(conn, "after", after)

    bad = 0
    for _ in range(500):
        x0, x1 = sorted(rng.sample(range(-20, 21), 2))
        y0, y1 = sorted(rng.sample(range(-20, 21), 2))
        mixed = select_rectangle(conn, x0, y0, x1, y1)
        cell = select_rectangle(conn, x0, y0, x1, y1, cellwise=True)
        if (mixed.area_cells_m2 != cell.area_cells_m2
                or abs(mixed.net_volume_m3 - cell.net_volume_m3) > 1e-9):
            bad += 1
            print("MISMATCH", (x0, y0, x1, y1), mixed, cell)
    print(f"500 个随机矩形比对完成, 不一致数: {bad}")

    # 确认确实有选区走到了整块路径
    used_whole = 0
    for _ in range(200):
        x0, x1 = sorted(rng.sample(range(-20, 21), 2))
        y0, y1 = sorted(rng.sample(range(-20, 21), 2))
        if select_rectangle(conn, x0, y0, x1, y1).whole_blocks > 0:
            used_whole += 1
    print(f"其中 {used_whole}/200 个选区包含完整块 (走 block_stats 汇总)")

    # 手工核对一个含完整块+边缘的矩形
    r1 = select_rectangle(conn, -17, -17, 18, 19)
    r2 = select_rectangle(conn, -17, -17, 18, 19, cellwise=True)
    print("样例选区:", r1)
    assert r1.whole_blocks > 0
    assert r1.area_cells_m2 == r2.area_cells_m2
    assert r1.net_volume_m3 == r2.net_volume_m3
    print("样例选区两路径一致 ✓")
    conn.close()


if __name__ == "__main__":
    main()
