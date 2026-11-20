"""FastAPI 入口: 上传两期测深 CSV、选区核量、状态查询与静态网页。"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .database import connect, get_data_version, init_db
from .ingest import IngestError, ingest_batch, parse_csv, record_rejection
from .query import QueryError, select_rectangle

STATIC_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = connect()
    init_db(conn)
    conn.close()
    yield


app = FastAPI(
    title="港池疏浚验收计量服务",
    version="1.0.0",
    description="上传前后两期测深 CSV, 按格线矩形核量两期共测面积与净挖方。",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    conn = connect()
    try:
        return {"status": "ok", "data_version": get_data_version(conn)}
    finally:
        conn.close()


@app.post("/api/surveys/{phase}")
async def upload_survey(
    phase: str, file: UploadFile = File(..., description="测深 CSV")
) -> JSONResponse:
    """接收一期测深 CSV 并整批入库; 同号异值整批退回 (409)。"""
    if phase not in ("before", "after"):
        raise HTTPException(status_code=404, detail="phase 仅支持 before / after")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="上传文件为空")

    conn = connect()
    try:
        try:
            readings = parse_csv(content)
        except IngestError as exc:
            # 解析失败: 数据未动, 留痕 (无有效读数时传 None)
            record_rejection(conn, phase, None, str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            result = ingest_batch(conn, phase, readings)
        except IngestError as exc:
            record_rejection(conn, phase, readings, str(exc))
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except sqlite3.OperationalError as exc:
            # 写锁等待超时等: 事务已回滚, 原结果保留
            raise HTTPException(status_code=503, detail=f"数据库繁忙: {exc}") from exc

        return JSONResponse(
            status_code=200,
            content={
                "status": result.status,
                "phase": result.phase,
                "survey_nos": result.survey_nos,
                "rows_received": result.rows_received,
                "rows_applied": result.rows_applied,
                "rows_skipped_stale": result.rows_skipped_stale,
                "blocks_touched": [list(b) for b in result.blocks_touched],
                "data_version": get_data_version(conn),
            },
        )
    finally:
        conn.close()


@app.get("/api/selection")
def selection(
    x0: int = Query(..., description="矩形格坐标 x 起点 (含)"),
    y0: int = Query(..., description="矩形格坐标 y 起点 (含)"),
    x1: int = Query(..., description="矩形格坐标 x 终点 (含)"),
    y1: int = Query(..., description="矩形格坐标 y 终点 (含)"),
    cellwise: bool = Query(False, description="调试: 全部按细格重算"),
) -> dict:
    """框选矩形核量。整块与边缘在同一只读快照内完成。"""
    conn = connect()
    try:
        try:
            result = select_rectangle(conn, x0, y0, x1, y1, cellwise=cellwise)
        except QueryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return asdict(result)
    finally:
        conn.close()


@app.get("/api/extent")
def extent() -> dict:
    """两期读数的坐标范围, 供网页确定画布比例。"""
    conn = connect()
    try:
        out: dict = {}
        for phase in ("before", "after"):
            row = conn.execute(
                "SELECT MIN(x) AS x0, MIN(y) AS y0, MAX(x) AS x1, MAX(y) AS y1, "
                "COUNT(*) AS n FROM readings WHERE phase = ?",
                (phase,),
            ).fetchone()
            out[phase] = dict(row) if row["n"] else None
        out["data_version"] = get_data_version(conn)
        return out
    finally:
        conn.close()


@app.get("/api/cells")
def cells(
    x0: int = Query(...), y0: int = Query(...),
    x1: int = Query(...), y1: int = Query(...),
) -> dict:
    """取矩形范围内各格的前/后测水深 (含空水深 None), 供网页绘格网。"""
    if x1 < x0 or y1 < y0:
        raise HTTPException(status_code=400, detail="坐标范围不合法")
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT x, y, phase, survey_no, depth_cm FROM readings "
            "WHERE x BETWEEN ? AND ? AND y BETWEEN ? AND ? ORDER BY x, y",
            (x0, x1, y0, y1),
        ).fetchall()
        return {"cells": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/api/blocks")
def blocks() -> dict:
    """分块统计全表 (调试与核对用)。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT bx, by, phase, n_cells, n_nonnull, sum_depth_cm, "
            "n_matched, diff_cm FROM block_stats ORDER BY bx, by, phase"
        ).fetchall()
        return {
            "block_size": config.BLOCK_SIZE,
            "data_version": get_data_version(conn),
            "blocks": [dict(r) for r in rows],
        }
    finally:
        conn.close()


@app.get("/api/batches")
def batches(limit: int = Query(50, ge=1, le=500)) -> dict:
    """批次留痕, 含被整批退回的批次。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, phase, survey_no, row_count, status, reject_reason, "
            "blocks_touched, created_at FROM ingest_batches "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return {"batches": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
