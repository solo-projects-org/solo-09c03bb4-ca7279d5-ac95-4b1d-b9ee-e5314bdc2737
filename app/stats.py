"""分块统计: 块坐标换算、受影响块重算与多块尺寸对账。

块 (bx, by) 覆盖的格坐标范围 (s = block_size)::

    bx*s <= x < (bx+1)*s
    by*s <= y < (by+1)*s

``block_stats`` 以 (block_size, bx, by, phase) 为主键: 多台服务共用一个库、
各自 BLOCK_SIZE 不同时, 每种尺寸维护自己的一套汇总, 补测互不覆盖。

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
from .database import get_data_version, get_stats_version


def block_of(x: int, y: int, block_size: int | None = None) -> tuple[int, int]:
    """格坐标 -> 所属块坐标 (支持负坐标, Python // 为向下取整)。"""
    s = config.BLOCK_SIZE if block_size is None else block_size
    return x // s, y // s


def block_cells(
    bx: int, by: int, block_size: int | None = None
) -> tuple[int, int, int, int]:
    """块覆盖的格坐标闭区间 (x0, y0, x1, y1)。"""
    s = config.BLOCK_SIZE if block_size is None else block_size
    return bx * s, by * s, (bx + 1) * s - 1, (by + 1) * s - 1


def registered_block_sizes(conn: sqlite3.Connection) -> list[int]:
    """库中已登记的所有块尺寸 (升序)。"""
    rows = conn.execute(
        "SELECT block_size FROM block_size_registry ORDER BY block_size"
    ).fetchall()
    return [int(r["block_size"]) for r in rows]


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


def _recompute_one_block(
    conn: sqlite3.Connection, block_size: int, bx: int, by: int
) -> None:
    """全量重算单个块 (指定尺寸) 的两期统计行。"""
    x0, y0, x1, y1 = block_cells(bx, by, block_size)
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
            # 该期在该块已无任何读数, 删掉统计行 (只删本尺寸)
            conn.execute(
                "DELETE FROM block_stats "
                "WHERE block_size = ? AND bx = ? AND by = ? AND phase = ?",
                (block_size, bx, by, phase),
            )
        else:
            conn.execute(
                "INSERT INTO block_stats(block_size, bx, by, phase, n_cells, "
                "n_nonnull, sum_depth_cm, n_matched, diff_cm) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(block_size, bx, by, phase) DO UPDATE SET "
                "n_cells = excluded.n_cells, "
                "n_nonnull = excluded.n_nonnull, "
                "sum_depth_cm = excluded.sum_depth_cm, "
                "n_matched = excluded.n_matched, "
                "diff_cm = excluded.diff_cm",
                (
                    block_size, bx, by, phase,
                    n_cells, int(agg["n_nonnull"]), int(agg["sum_depth_cm"]),
                    n_matched, diff_cm,
                ),
            )


def recompute_blocks(
    conn: sqlite3.Connection,
    blocks_by_size: dict[int, set[tuple[int, int]]],
) -> None:
    """在当前事务内重算给定块 (按尺寸分组), 可重复、可纠正。

    只重算调用方列出的受影响块, 其它块/其它尺寸的行不动。
    """
    for block_size, blocks in blocks_by_size.items():
        for bx, by in blocks:
            _recompute_one_block(conn, block_size, bx, by)


def _blocks_with_readings(
    conn: sqlite3.Connection, block_size: int
) -> set[tuple[int, int]]:
    """readings 中任一期出现过的、按该尺寸切分的全部块坐标。"""
    rows = conn.execute(
        "SELECT DISTINCT (x / ?) AS bx, (y / ?) AS by FROM readings",
        (block_size, block_size),
    ).fetchall()
    return {(int(r["bx"]), int(r["by"])) for r in rows}


def recompute_all_blocks(conn: sqlite3.Connection, block_size: int) -> int:
    """按 readings 全量重建某尺寸的全部块汇总, 返回重算块数。

    用于: 新尺寸首次登记、实例重启后补算停用期间错过的补测、
    接入未记录块大小的旧库后的首次重建。先清掉该尺寸的旧行,
    再逐块重算, 调用方须包在写事务内。
    """
    conn.execute(
        "DELETE FROM block_stats WHERE block_size = ?", (block_size,)
    )
    blocks = _blocks_with_readings(conn, block_size)
    for bx, by in sorted(blocks):
        _recompute_one_block(conn, block_size, bx, by)
    return len(blocks)


def ensure_block_size(conn: sqlite3.Connection, block_size: int) -> dict:
    """登记/对账本机块尺寸, 须在实例启动时调用。

    - 新尺寸 (含旧库迁移后首次启动): 全量重建该尺寸汇总并登记。
    - 已登记但汇总版本落后于数据版本 (实例停用期间其它实例做了补测):
      全量补算到当前数据版本。
    - 已追平: 不动数据。

    读数、批次留痕、数据版本始终保留; 整个对账在一个写事务内完成,
    失败整体回滚, 库内原结果不受影响。返回对账情况供启动日志使用。
    """
    from .database import write_transaction  # 局部导入避免循环依赖

    with write_transaction(conn, bump_version=False) as tx:
        data_version = get_data_version(tx)
        stats_version = get_stats_version(tx, block_size)
        if stats_version is not None and stats_version == data_version:
            return {
                "block_size": block_size,
                "action": "up-to-date",
                "blocks": 0,
                "data_version": data_version,
            }

        action = "rebuild" if stats_version is None else "catch-up"
        n_blocks = recompute_all_blocks(tx, block_size)
        tx.execute(
            "INSERT INTO block_size_registry(block_size, stats_version) "
            "VALUES (?, ?) "
            "ON CONFLICT(block_size) DO UPDATE SET stats_version = excluded.stats_version",
            (block_size, data_version),
        )
        return {
            "block_size": block_size,
            "action": action,
            "blocks": n_blocks,
            "data_version": data_version,
        }
