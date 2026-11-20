"""SQLite 连接管理、建表与并发控制。

设计要点
--------
- 开启 WAL, 读不阻塞写、写不阻塞读, 保证一次核量的整块查询和边缘查询
  能在同一个读事务 (同一份已提交快照) 内完成。
- 进程内写锁 ``_write_lock`` 串行化补测写入; SQLite 层再以 BEGIN IMMEDIATE
  抢占写锁, 配合 busy timeout, 保证并发补测不会漏算受影响的块。
- 读数 (readings) 与分块统计 (block_stats) 在同一个事务内提交,
  任一步失败整批回滚, 保留原结果。
- 多实例共用一个库且 BLOCK_SIZE 不同时, ``block_stats`` 以
  ``(block_size, bx, by, phase)`` 为主键, 每种块大小各存各的汇总,
  互不覆盖; ``block_size_registry`` 记录每种尺寸的汇总停在哪个数据版本,
  实例启动时对账, 把停用期间错过的补测一次性补齐。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config

# 进程内写锁: 同一应用进程内的补测事务严格串行
_write_lock = threading.RLock()


def _resolve_db_path(db_path: Path | str | None) -> Path:
    if db_path is not None:
        return Path(db_path)
    # 动态读取环境变量, 便于测试在导入后切换数据库
    raw = os.environ.get("DB_PATH")
    return Path(raw) if raw else config.DB_PATH


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """打开一个配置好的连接 (每个请求/线程独立连接)。"""
    path = _resolve_db_path(db_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        # 容器外本机调试且默认 /data 不可写时, 回退到临时目录
        if db_path is None and not os.environ.get("DB_PATH"):
            path = Path(tempfile.gettempdir()) / "dredging" / "dredging.db"
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            raise
    conn = sqlite3.connect(
        str(path),
        timeout=config.BUSY_TIMEOUT_S,
        isolation_level=None,  # 事务完全由代码显式控制
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(config.BUSY_TIMEOUT_S * 1000)}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
-- 现行读数: 同一期同一格只有一行, 由最高测次号决定
CREATE TABLE IF NOT EXISTS readings (
    phase      TEXT NOT NULL CHECK (phase IN ('before', 'after')),
    x          INTEGER NOT NULL,
    y          INTEGER NOT NULL,
    survey_no  INTEGER NOT NULL,
    depth_cm   INTEGER,              -- NULL = 空水深 (测过但无有效水深)
    PRIMARY KEY (phase, x, y)
);

-- 分块统计: 每种块大小、每块每期一行, 补测只重算受影响的块。
-- block_size 是块边长 (格): 两台服务 BLOCK_SIZE 不同也各存各的, 互不覆盖。
CREATE TABLE IF NOT EXISTS block_stats (
    block_size      INTEGER NOT NULL,
    bx              INTEGER NOT NULL,
    by              INTEGER NOT NULL,
    phase           TEXT NOT NULL CHECK (phase IN ('before', 'after')),
    n_cells         INTEGER NOT NULL,  -- 该期该块内有读数 (含空水深) 的格数
    n_nonnull       INTEGER NOT NULL,  -- 其中水深非空的格数
    sum_depth_cm    INTEGER NOT NULL,  -- 非空水深之和 (厘米)
    -- 两期配对指标 (before/after 行存相同值, 便于任一时期补测后整行重写):
    -- 前后都有非空水深的格数, 以及这些格的 (后测-前测) 厘米差之和
    n_matched       INTEGER NOT NULL DEFAULT 0,
    diff_cm         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (block_size, bx, by, phase)
);

-- 已接收批次留痕 (成功与失败都记录, 失败批次不改读数/统计)
CREATE TABLE IF NOT EXISTS ingest_batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    phase           TEXT NOT NULL,
    survey_no       INTEGER NOT NULL,
    row_count       INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('accepted', 'rejected')),
    reject_reason   TEXT,
    blocks_touched  TEXT,               -- JSON: 受影响块坐标列表
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 数据版本: 每提交一次补测 +1, 便于网页/调用方确认快照
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 块尺寸登记表: 每种 block_size 的分块汇总停在哪个数据版本。
-- stats_version == 数据版本表示汇总已追平; 更小表示该尺寸有补测漏算,
-- 实例启动 (或下次补测) 时按受影响块/全量补齐。
CREATE TABLE IF NOT EXISTS block_size_registry (
    block_size    INTEGER PRIMARY KEY,
    stats_version INTEGER NOT NULL
);
"""

# 旧版 block_stats (无 block_size 列) 的建表形态, 用于识别未记录块大小的已有库
_LEGACY_BLOCK_STATS_SQL = """
CREATE TABLE block_stats (
    block_size      INTEGER NOT NULL,
    bx              INTEGER NOT NULL,
    by              INTEGER NOT NULL,
    phase           TEXT NOT NULL CHECK (phase IN ('before', 'after')),
    n_cells         INTEGER NOT NULL,
    n_nonnull       INTEGER NOT NULL,
    sum_depth_cm    INTEGER NOT NULL,
    n_matched       INTEGER NOT NULL DEFAULT 0,
    diff_cm         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (block_size, bx, by, phase)
)
"""


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def init_db(conn: sqlite3.Connection) -> None:
    """建表并初始化数据版本。幂等, 含旧库迁移。

    迁移策略
    --------
    - 库中已有 readings / ingest_batches / meta 一律保留 (读数、批次留痕、
      数据版本不丢)。
    - 旧版 ``block_stats`` 没有 block_size 列, 其块大小无记录、几何含义
      已不可信, 直接按新表结构重建; 重建后由 stats.ensure_block_size
      依据 readings 全量重算当前实例尺寸的汇总。
    - 全新库 (无任何读数) 直接登记本机 BLOCK_SIZE, 使不经 Web 启动流程
      的直接调用方 (测试/脚本) 也能立即写块汇总。
    """
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)

    legacy_stats = bool(_table_columns(conn, "block_stats")) and (
        "block_size" not in _table_columns(conn, "block_stats")
    )
    if legacy_stats:
        conn.execute("DROP TABLE block_stats")
        conn.execute(_LEGACY_BLOCK_STATS_SQL)

    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('data_version', '0')"
    )

    # 全新库: 登记本机块尺寸, 初始汇总版本 0 与数据版本一致, 无块可算。
    # 旧库不在此登记 —— 其块大小未记录, 交由 ensure_block_size 全量重建。
    has_readings = conn.execute(
        "SELECT 1 FROM readings LIMIT 1"
    ).fetchone() is not None
    if not legacy_stats and not has_readings:
        conn.execute(
            "INSERT OR IGNORE INTO block_size_registry(block_size, stats_version) "
            "VALUES (?, 0)",
            (config.BLOCK_SIZE,),
        )


def get_data_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'data_version'").fetchone()
    return int(row["value"]) if row else 0


def get_stats_version(conn: sqlite3.Connection, block_size: int) -> int | None:
    """某块尺寸的汇总版本; 未登记返回 None。"""
    row = conn.execute(
        "SELECT stats_version FROM block_size_registry WHERE block_size = ?",
        (block_size,),
    ).fetchone()
    return None if row is None else int(row["stats_version"])


@contextmanager
def read_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """只读快照事务: WAL 下读到的是事务开始时的已提交版本。"""
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.execute("COMMIT")


@contextmanager
def write_transaction(
    conn: sqlite3.Connection, bump_version: bool = True
) -> Iterator[sqlite3.Connection]:
    """写事务: 进程锁 + BEGIN IMMEDIATE, 串行化所有补测。

    读数更新、分块重算、版本号递增在同一事务内, 异常即整体回滚。
    版本号在事务体执行前先递增, 业务代码可在同事务内把各块尺寸的
    汇总版本对齐到新版本; 回滚时版本与汇总一并撤销。
    仅写留痕 (如批次退回) 时传 bump_version=False, 不推进数据版本。
    """
    with _write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if bump_version:
                conn.execute(
                    "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) "
                    "WHERE key = 'data_version'"
                )
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
