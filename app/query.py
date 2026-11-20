"""选区核量: 整块只读汇总 + 切开的块按细格算, 两者取自同一快照。

矩形 (沿格线框选) 覆盖的格为 x0..x1, y0..y1 的闭区间。
内部完整块直接读 block_stats; 落在矩形边缘、被矩形切开的块退回 readings
逐格计算。两条查询在同一个只读事务中执行, WAL 保证它们看到同一份
已提交数据, 期间任何补测提交都不会改变本次结果。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import config
from .database import get_data_version, read_transaction


class QueryError(ValueError):
    """选区参数不合法。"""


@dataclass
class SelectionResult:
    x0: int
    y0: int
    x1: int
    y1: int
    area_cells_m2: int          # 前后两期都有非空水深的格数, 每格 1 平方米
    net_volume_m3: float        # 后测减前测累计, 负值照计
    whole_blocks: int           # 整块参与汇总的块数
    edge_cells: int             # 边缘按细格计算的格数 (有读数的)
    edge_matched_cells: int     # 边缘中前后都有非空水深的格数
    data_version: int
    mode: str                   # "block+edge" 或调试用的 "cellwise"


def _normalize_rect(x0: int, y0: int, x1: int, y1: int) -> tuple[int, int, int, int]:
    lo_x, hi_x = sorted((int(x0), int(x1)))
    lo_y, hi_y = sorted((int(y0), int(y1)))
    if hi_x - lo_x + 1 <= 0 or hi_y - lo_y + 1 <= 0:
        raise QueryError("选区必须至少覆盖一个方格")
    return lo_x, lo_y, hi_x, hi_y


def _core_rect(
    x0: int, y0: int, x1: int, y1: int
) -> tuple[int, int, int, int] | None:
    """完整落在选区内的块所覆盖的格范围; 没有完整块则返回 None。

    完整块序号区间: ceil(x0/s) .. floor((x1+1)/s)-1
    """
    s = config.BLOCK_SIZE
    cx0 = -((-x0) // s) * s                      # ceil(x0/s)*s
    cy0 = -((-y0) // s) * s
    cx1 = ((x1 + 1) // s) * s - 1                # 最后一个完整块的末端格
    cy1 = ((y1 + 1) // s) * s - 1
    if cx0 > cx1 or cy0 > cy1:
        return None
    return cx0, cy0, cx1, cy1


def _whole_aggregate(
    conn: sqlite3.Connection, x0: int, y0: int, x1: int, y1: int
) -> tuple[int, int, int]:
    """完整块汇总, 返回 (配对计量格数, 后减前厘米差合计, 参与块数)。

    配对指标 n_matched / diff_cm 在块重算时已按格 JOIN 得出,
    任一期空水深或缺测都不会污染结果。
    """
    core = _core_rect(x0, y0, x1, y1)
    if core is None:
        return 0, 0, 0
    cx0, cy0, cx1, cy1 = core
    s = config.BLOCK_SIZE
    bx0, by0, bx1, by1 = cx0 // s, cy0 // s, cx1 // s, cy1 // s

    # 只取 before 行即可: before/after 两行的配对指标完全相同
    rows = conn.execute(
        "SELECT bx, by, n_matched, diff_cm FROM block_stats "
        "WHERE phase = 'before' "
        "AND bx BETWEEN ? AND ? AND by BETWEEN ? AND ?",
        (bx0, bx1, by0, by1),
    ).fetchall()

    return (
        sum(int(r["n_matched"]) for r in rows),
        sum(int(r["diff_cm"]) for r in rows),
        len(rows),
    )


def _edge_aggregate(
    conn: sqlite3.Connection, x0: int, y0: int, x1: int, y1: int
) -> tuple[int, int, int]:
    """边缘细格汇总, 返回 (计量格数, 厘米差合计, 边缘有读数的格总数)。"""
    core = _core_rect(x0, y0, x1, y1)

    # 边缘 = 选区内、核心区之外的格。t 为表别名前缀。
    if core is None:
        edge_filter = (
            "{t}.x BETWEEN :x0 AND :x1 AND {t}.y BETWEEN :y0 AND :y1"
        )
    else:
        cx0, cy0, cx1, cy1 = core
        edge_filter = (
            "{t}.x BETWEEN :x0 AND :x1 AND {t}.y BETWEEN :y0 AND :y1 "
            "AND ({t}.x NOT BETWEEN :cx0 AND :cx1 "
            "     OR {t}.y NOT BETWEEN :cy0 AND :cy1)"
        )
    params = {
        "x0": x0, "x1": x1, "y0": y0, "y1": y1,
        "cx0": core[0] if core else 0, "cx1": core[2] if core else 0,
        "cy0": core[1] if core else 0, "cy1": core[3] if core else 0,
    }

    # 任一期在该边缘格有读数 (含空水深) 都计入边缘格数
    total_edge = conn.execute(
        "SELECT COUNT(*) AS n FROM ("
        f"SELECT r.x AS x, r.y AS y FROM readings r WHERE {edge_filter.format(t='r')} "
        "UNION "
        f"SELECT r.x AS x, r.y AS y FROM readings r WHERE {edge_filter.format(t='r')}"
        ")",
        params,
    ).fetchone()["n"]

    matched = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(a.depth_cm - b.depth_cm), 0) AS d "
        "FROM readings b "
        "JOIN readings a ON a.x = b.x AND a.y = b.y AND a.phase = 'after' "
        "WHERE b.phase = 'before' AND b.depth_cm IS NOT NULL "
        "AND a.depth_cm IS NOT NULL "
        f"AND {edge_filter.format(t='b')}",
        params,
    ).fetchone()
    return int(matched["n"]), int(matched["d"]), int(total_edge)


def _cellwise_aggregate(
    conn: sqlite3.Connection, x0: int, y0: int, x1: int, y1: int
) -> tuple[int, int]:
    """调试/校验用: 整个选区全部逐格计算。"""
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


def select_rectangle(
    conn: sqlite3.Connection,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    cellwise: bool = False,
) -> SelectionResult:
    lo_x, lo_y, hi_x, hi_y = _normalize_rect(x0, y0, x1, y1)
    with read_transaction(conn) as tx:
        version = get_data_version(tx)
        if cellwise:
            cells, diff_cm = _cellwise_aggregate(tx, lo_x, lo_y, hi_x, hi_y)
            return SelectionResult(
                lo_x, lo_y, hi_x, hi_y,
                area_cells_m2=cells,
                net_volume_m3=round(diff_cm / 100.0, 3),
                whole_blocks=0,
                edge_cells=(hi_x - lo_x + 1) * (hi_y - lo_y + 1),
                edge_matched_cells=cells,
                data_version=version,
                mode="cellwise",
            )

        whole_cells, whole_diff, n_blocks = _whole_aggregate(
            tx, lo_x, lo_y, hi_x, hi_y
        )
        edge_cells, edge_diff, edge_total = _edge_aggregate(
            tx, lo_x, lo_y, hi_x, hi_y
        )
        return SelectionResult(
            lo_x, lo_y, hi_x, hi_y,
            area_cells_m2=whole_cells + edge_cells,
            net_volume_m3=round((whole_diff + edge_diff) / 100.0, 3),
            whole_blocks=n_blocks,
            edge_cells=edge_total,
            edge_matched_cells=edge_cells,
            data_version=version,
            mode="block+edge",
        )
