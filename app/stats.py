"""分块统计: 块坐标换算与受影响块重算。

块 (bx, by) 覆盖的格坐标范围::

    bx*BLOCK_SIZE <= x < (bx+1)*BLOCK_SIZE
    by*BLOCK_SIZE <= y < (by+1)*BLOCK_SIZE

每块每期保存单期指标:
- n_cells:      该期在该块有读数的格数 (空水深也算有读数)
- n_nonnull:    水深非空的格数
- sum_depth_cm: 非空水深之和 (厘米)

以及两期配对指标 (before/after 两行冗余存同一份, 补测任一期都可整行重写):
- n_matched: 前后两期都有非空水深的格数, 即参与计量的格数 (每格 1 平方米)
- diff_cm:    这些格的 (后测水深 - 前测水深) 厘米差之和, 负值照计

配对指标必须用 readings 按 (x,y) JOIN 计算, 不能用两期总和直接相减,
否则任一期出现空水深/缺测时格集合不一致会算错。
"""

from __future__ import annotations

import sqlite3

from . import config


def block_of(x: int, y: int) -> tuple[int, int]:
    """格坐标 -> 所属块坐标 (支持负坐标, Python // 为向下取整)。"""
    return x // config.BLOCK_SIZE, y // config.BLOCK_SIZE


def block_cells(bx: int, by: int) -> tuple[int, int, int, int]:
    """块覆盖的格坐标闭区间 (x0, y0, x1, y1)。"""
    s = config.BLOCK_SIZE
    return bx * s, by * s, (bx + 1) * s - 1, (by + 1) * s - 1


def _matched_aggregate(
    conn: sqlite3.Connection, x0: int, y0: int, x1: int, y1: int
) -> tuple[int, int]:
    """块范围内前后两期配对格: (配对格数, 后减前厘米差合计)。"""
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(a.depth_cm - b.depth_cm), 0) AS d "
        "FROM readings b "
        "JOIN readings a ON a.x = b.x AND a.y = b.y AND a.phase = 'after' "
        "WHERE b.phase = 'before' AND b.depth_cm IS NOT NULL "
        "AND a.depth_cm IS NOT NULL "
        "AND b.x BETWEEN ? AND ? AND b.y BETWEEN ? AND ?",
        (x0, x1, y0, y1),
    ).fetchone()
    return int(row["n"]), int(row["d"])


def recompute_blocks(conn: sqlite3.Connection, blocks: set[tuple[int, int]]) -> None:
    """在当前事务内全量重算给定块的两期统计 (可重复、可纠正)。"""
    for bx, by in blocks:
        x0, y0, x1, y1 = block_cells(bx, by)
        n_matched, diff_cm = _matched_aggregate(conn, x0, y0, x1, y1)
        for phase in ("before", "after"):
            agg = conn.execute(
                "SELECT COUNT(*) AS n_cells, "
                "SUM(CASE WHEN depth_cm IS NOT NULL THEN 1 ELSE 0 END) AS n_nonnull, "
                "COALESCE(SUM(depth_cm), 0) AS sum_depth_cm "
                "FROM readings "
                "WHERE phase = ? AND x BETWEEN ? AND ? AND y BETWEEN ? AND ?",
                (phase, x0, x1, y0, y1),
            ).fetchone()
            n_cells = int(agg["n_cells"])
            if n_cells == 0:
                # 该期在该块已无任何读数, 删掉统计行
                conn.execute(
                    "DELETE FROM block_stats WHERE bx = ? AND by = ? AND phase = ?",
                    (bx, by, phase),
                )
            else:
                conn.execute(
                    "INSERT INTO block_stats(bx, by, phase, n_cells, n_nonnull, "
                    "sum_depth_cm, n_matched, diff_cm) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(bx, by, phase) DO UPDATE SET "
                    "n_cells = excluded.n_cells, "
                    "n_nonnull = excluded.n_nonnull, "
                    "sum_depth_cm = excluded.sum_depth_cm, "
                    "n_matched = excluded.n_matched, "
                    "diff_cm = excluded.diff_cm",
                    (
                        bx, by, phase,
                        n_cells, int(agg["n_nonnull"]), int(agg["sum_depth_cm"]),
                        n_matched, diff_cm,
                    ),
                )
