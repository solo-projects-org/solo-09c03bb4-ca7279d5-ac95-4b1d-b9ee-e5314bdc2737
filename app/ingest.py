"""CSV 解析与整批提交/回退。

业务规则
--------
1. 同一期同一格只保留最高测次号 (survey_no) 的读数。
2. 新测次号更高 -> 覆盖旧值; 空水深 (depth_cm 留空) 也照样覆盖。
3. 测次号相同但水深不同 -> 整批退回 (本批一行不写)。
4. 测次号更低 -> 该格跳过 (旧读数保留)。
5. 读数写入与分块统计重算在同一事务内提交, 失败回滚保留原结果。
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from dataclasses import dataclass, field

from . import config
from .database import get_data_version, write_transaction
from .stats import block_of, recompute_blocks, registered_block_sizes

# 表头别名 -> 标准字段
_COLUMN_ALIASES = {
    "x": "x",
    "格x": "x",
    "格线x": "x",
    "y": "y",
    "格y": "y",
    "格线y": "y",
    "survey_no": "survey_no",
    "surveyno": "survey_no",
    "测次号": "survey_no",
    "测次": "survey_no",
    "depth_cm": "depth_cm",
    "depth": "depth_cm",
    "水深": "depth_cm",
    "厘米水深": "depth_cm",
    "水深厘米": "depth_cm",
}

REQUIRED_COLUMNS = ("x", "y", "survey_no", "depth_cm")


class IngestError(ValueError):
    """输入数据不合法或触发整批退回, 路由层映射为 HTTP 400/409。"""


@dataclass(frozen=True)
class Reading:
    x: int
    y: int
    survey_no: int
    depth_cm: int | None  # None 表示空水深


@dataclass
class IngestResult:
    phase: str
    survey_nos: list[int]
    rows_received: int
    rows_applied: int = 0          # 实际插入/覆盖的格数
    rows_skipped_stale: int = 0    # 测次号更低被跳过
    blocks_touched: list[tuple[int, int]] = field(default_factory=list)
    status: str = "accepted"
    reject_reason: str | None = None


def _parse_int(raw: str, field_name: str, line_no: int) -> int:
    raw = raw.strip()
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise IngestError(f"第 {line_no} 行字段 {field_name} 不是整数: {raw!r}") from exc


def parse_csv(content: bytes | str) -> list[Reading]:
    """解析上传的测深 CSV。空水深 -> depth_cm=None。"""
    if isinstance(content, bytes):
        # 兼容带 BOM 的 UTF-8
        text = content.decode("utf-8-sig")
    else:
        text = content
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise IngestError("CSV 为空, 缺少表头") from exc

    columns: list[str] = []
    for name in header:
        key = name.strip().lower().replace(" ", "")
        columns.append(_COLUMN_ALIASES.get(key, key))
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        raise IngestError(f"CSV 缺少必需列: {', '.join(missing)}; 实际表头: {header}")
    idx = {c: columns.index(c) for c in REQUIRED_COLUMNS}

    readings: list[Reading] = []
    seen: dict[tuple[int, int], Reading] = {}
    for line_no, row in enumerate(reader, start=2):
        if not row or all(not cell.strip() for cell in row):
            continue  # 跳过空行
        if len(row) < len(header):
            raise IngestError(f"第 {line_no} 行列数不足: {row}")

        x = _parse_int(row[idx["x"]], "x", line_no)
        y = _parse_int(row[idx["y"]], "y", line_no)
        survey_no = _parse_int(row[idx["survey_no"]], "survey_no", line_no)
        if survey_no <= 0:
            raise IngestError(f"第 {line_no} 行测次号必须为正整数: {survey_no}")

        depth_raw = row[idx["depth_cm"]].strip()
        depth_cm: int | None
        if depth_raw == "":
            depth_cm = None  # 空水深: 合法, 且会覆盖旧值
        else:
            depth_cm = _parse_int(depth_raw, "depth_cm", line_no)
            if not (config.MIN_DEPTH_CM <= depth_cm <= config.MAX_DEPTH_CM):
                raise IngestError(
                    f"第 {line_no} 行水深 {depth_cm}cm 超出允许范围 "
                    f"[{config.MIN_DEPTH_CM}, {config.MAX_DEPTH_CM}]"
                )

        reading = Reading(x=x, y=y, survey_no=survey_no, depth_cm=depth_cm)
        if (x, y) in seen:
            prev = seen[(x, y)]
            # 同批同格: 完全一致则去重, 否则拒收
            if (prev.survey_no, prev.depth_cm) != (survey_no, depth_cm):
                raise IngestError(
                    f"第 {line_no} 行格 ({x},{y}) 在本批中重复且内容不一致"
                )
            continue
        seen[(x, y)] = reading
        readings.append(reading)

    if len(readings) > config.MAX_ROWS_PER_BATCH:
        raise IngestError(
            f"单批行数 {len(readings)} 超过上限 {config.MAX_ROWS_PER_BATCH}"
        )
    if not readings:
        raise IngestError("CSV 没有任何有效数据行")
    return readings


def _record_batch(
    conn: sqlite3.Connection, result: IngestResult
) -> None:
    conn.execute(
        "INSERT INTO ingest_batches(phase, survey_no, row_count, status, "
        "reject_reason, blocks_touched) VALUES (?, ?, ?, ?, ?, ?)",
        (
            result.phase,
            result.survey_nos[0] if len(result.survey_nos) == 1 else -1,
            result.rows_received,
            result.status,
            result.reject_reason,
            json.dumps(result.blocks_touched),
        ),
    )


def ingest_batch(
    conn: sqlite3.Connection, phase: str, readings: list[Reading]
) -> IngestResult:
    """在一个写事务内应用整批读数并重算受影响块。

    冲突 (同号异值) 时抛 IngestError, 事务回滚, 原读数与原统计保持不变。
    """
    if phase not in ("before", "after"):
        raise IngestError(f"phase 必须是 before 或 after, 实际: {phase!r}")

    result = IngestResult(
        phase=phase,
        survey_nos=sorted({r.survey_no for r in readings}),
        rows_received=len(readings),
    )
    touched_cells: set[tuple[int, int]] = set()

    with write_transaction(conn) as tx:
        for r in readings:
            cur = tx.execute(
                "SELECT survey_no, depth_cm FROM readings "
                "WHERE phase = ? AND x = ? AND y = ?",
                (phase, r.x, r.y),
            ).fetchone()

            if cur is not None:
                if cur["survey_no"] > r.survey_no:
                    result.rows_skipped_stale += 1
                    continue
                if cur["survey_no"] == r.survey_no:
                    if cur["depth_cm"] != r.depth_cm:
                        raise IngestError(
                            f"整批退回: 格 ({r.x},{r.y}) 已有测次号 "
                            f"{r.survey_no} 的读数 (水深 "
                            f"{cur['depth_cm']}cm), 本批同号异值 "
                            f"({r.depth_cm}cm)"
                        )
                    # 同号同值: 幂等, 无需重写
                    continue

            tx.execute(
                "INSERT INTO readings(phase, x, y, survey_no, depth_cm) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(phase, x, y) DO UPDATE SET "
                "survey_no = excluded.survey_no, depth_cm = excluded.depth_cm",
                (phase, r.x, r.y, r.survey_no, r.depth_cm),
            )
            result.rows_applied += 1
            touched_cells.add((r.x, r.y))

        if touched_cells:
            # 共用库的每台服务可能有不同块大小: 对每种已登记尺寸,
            # 只重算本批写入格落在其中的块; 未登记的尺寸不代算,
            # 由该尺寸的实例启动对账时补齐。
            blocks_by_size: dict[int, set[tuple[int, int]]] = {}
            for size in registered_block_sizes(tx):
                blocks_by_size[size] = {
                    block_of(x, y, size) for x, y in touched_cells
                }
            recompute_blocks(tx, blocks_by_size)
            new_version = get_data_version(tx)
            tx.executemany(
                "UPDATE block_size_registry SET stats_version = ? "
                "WHERE block_size = ?",
                [(new_version, size) for size in blocks_by_size],
            )
            # 响应里的 blocks_touched 按本机块大小报告, 网页展示不变
            result.blocks_touched = sorted(
                blocks_by_size.get(config.BLOCK_SIZE, set())
            )
        _record_batch(tx, result)

    return result


def record_rejection(
    conn: sqlite3.Connection, phase: str, readings: list[Reading] | None,
    reason: str,
) -> None:
    """拒收留痕: 独立事务写入, 不触碰 readings / block_stats。"""
    survey_nos = sorted({r.survey_no for r in (readings or [])})
    row = IngestResult(
        phase=phase,
        survey_nos=survey_nos or [-1],
        rows_received=len(readings or []),
        status="rejected",
        reject_reason=reason,
    )
    with write_transaction(conn, bump_version=False) as tx:
        _record_batch(tx, row)
