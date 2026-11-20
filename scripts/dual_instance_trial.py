#!/usr/bin/env python3
"""双实例端到端自测: 两个 uvicorn 子进程共用一个 SQLite, BLOCK_SIZE 16 vs 10。

不依赖 Docker, 直接在本机起两个进程, 模拟 docker-compose 的双实例部署, 覆盖:

1. A 建库灌基线, B 启动自动重建自己的块汇总 (rebuild)
2. 两台交错补测, 各自整块+边缘核量 == 逐格核量 (同数据版本)
3. B 停用期间 A 补测; 模拟汇总版本落后后 B 重启对账补算 (catch-up)
4. 同号异值整批退回, 两边原结果不变, 留痕保留
5. 两台网页静态资源均可访问

用法:
    python scripts/dual_instance_trial.py
可选环境变量 WORK_DIR (默认临时目录)。
"""
from __future__ import annotations

import csv
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SIZE_A, SIZE_B = 16, 10
PORT_A, PORT_B = 8016, 8010


def _wait_health(port: int, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as r:
                return json.loads(r.read())
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"service on {port} not healthy")


def _start(size: int, port: int, db_path: Path, work: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({
        "DB_PATH": str(db_path),
        "BLOCK_SIZE": str(size),
        "BUSY_TIMEOUT_S": "30",
        "INSTANCE_NAME": f"{'A' if size == SIZE_A else 'B'}({size})",
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONSAFEPATH": "1",
    })
    log = open(work / f"svc_{size}.log", "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO_ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    _wait_health(port)
    return proc


def _stop(proc: subprocess.Popen) -> None:
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=15)


def _call(port: int, method: str, path: str, files: dict | None = None):
    if files is not None:
        boundary = "----dredgeboundary"
        body = b""
        for field, (filename, content) in files.items():
            body += f"--{boundary}\r\n".encode()
            body += (
                f'Content-Disposition: form-data; name="{field}"; '
                f'filename="{filename}"\r\n\r\n'
            ).encode()
            body += content.encode() + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=body, method=method,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _make_csv(rows: list[tuple]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["x", "y", "survey_no", "depth_cm"])
    for row in rows:
        writer.writerow(["" if value is None else value for value in row])
    return buf.getvalue()


def _check(port: int, rect: tuple, label: str) -> dict:
    q = "/api/selection?x0=%d&y0=%d&x1=%d&y1=%d" % rect
    _, mixed = _call(port, "GET", q)
    _, cellwise = _call(port, "GET", q + "&cellwise=true")
    ok = (
        mixed["area_cells_m2"] == cellwise["area_cells_m2"]
        and abs(mixed["net_volume_m3"] - cellwise["net_volume_m3"]) < 1e-9
        and mixed["data_version"] == cellwise["data_version"]
    )
    print(f"  [{'OK' if ok else 'FAIL'}] {label}: "
          f"{mixed['area_cells_m2']}格/{mixed['net_volume_m3']}m3 "
          f"v{mixed['data_version']} (整块{mixed['whole_blocks']}/"
          f"边缘{mixed['edge_cells']})")
    assert ok, (mixed, cellwise)
    return mixed


def main() -> None:
    work = Path(os.environ.get("WORK_DIR") or tempfile.mkdtemp(prefix="dual-trial-"))
    work.mkdir(parents=True, exist_ok=True)
    db_path = work / "dredging.db"
    procs: dict[int, subprocess.Popen] = {}
    try:
        print("== 1. 启动实例 A (BLOCK_SIZE=16) ==")
        procs[SIZE_A] = _start(SIZE_A, PORT_A, db_path, work)
        print("  health:", _wait_health(PORT_A))

        print("== 2. A 灌入两期 20x20 基线 (前 200 / 后 230) ==")
        grid = [(x, y) for x in range(20) for y in range(20)]
        s1, _ = _call(PORT_A, "POST", "/api/surveys/before",
                      files={"file": ("b.csv", _make_csv([(x, y, 1, 200) for x, y in grid]))})
        s2, body = _call(PORT_A, "POST", "/api/surveys/after",
                         files={"file": ("a.csv", _make_csv([(x, y, 1, 230) for x, y in grid]))})
        assert (s1, s2) == (200, 200)
        print("  基线数据版本 v", body["data_version"])

        print("== 3. 启动实例 B (BLOCK_SIZE=10), 自动 rebuild 自己的汇总 ==")
        procs[SIZE_B] = _start(SIZE_B, PORT_B, db_path, work)
        print("  health:", _wait_health(PORT_B))
        _, blk = _call(PORT_B, "GET", "/api/blocks")
        assert blk["block_size"] == SIZE_B and len(blk["blocks"]) == 8
        print("  B 块大小", blk["block_size"], "统计行", len(blk["blocks"]))

        print("== 4. 两台交错补测 ==")
        s, r = _call(PORT_A, "POST", "/api/surveys/after",
                     files={"file": ("s.csv", _make_csv([(1, 1, 2, 260)]))})
        assert s == 200, r
        print("  A 补 (1,1)->260, 受影响块:", r["blocks_touched"], "v", r["data_version"])
        s, r = _call(PORT_B, "POST", "/api/surveys/after",
                     files={"file": ("s.csv", _make_csv([(15, 15, 3, 290)]))})
        assert s == 200, r
        print("  B 补 (15,15)->290, 受影响块:", r["blocks_touched"], "v", r["data_version"])
        s, r = _call(PORT_A, "POST", "/api/surveys/after",
                     files={"file": ("s.csv", _make_csv([(7, 7, 4, None)]))})
        assert s == 200, r
        print("  A 补 (7,7)->空水深 v", r["data_version"])

        print("== 5. 两台各自核量, 与逐格核量交叉核对 ==")
        rects = [(0, 0, 19, 19), (0, 0, 15, 15), (0, 0, 9, 9), (3, 5, 17, 14),
                 (10, 10, 19, 19), (1, 1, 1, 1), (15, 15, 15, 15)]
        for rect in rects:
            _check(PORT_A, rect, f"A(16) {rect}")
            _check(PORT_B, rect, f"B(10) {rect}")

        _, ma = _call(PORT_A, "GET", "/api/selection?x0=0&y0=0&x1=19&y1=19")
        _, mb = _call(PORT_B, "GET", "/api/selection?x0=0&y0=0&x1=19&y1=19")
        assert ma["data_version"] == mb["data_version"]
        assert ma["area_cells_m2"] == mb["area_cells_m2"] == 399
        assert abs(ma["net_volume_m3"] - mb["net_volume_m3"]) < 1e-9
        expected = (399 * 30 + 30 + 60) / 100.0  # 基线 +30, (1,1) 多 30, (15,15) 多 60
        assert abs(ma["net_volume_m3"] - expected) < 1e-9
        print(f"  跨实例同版本一致: {ma['net_volume_m3']} m3, 手算 {expected} m3")

        print("== 6. B 停用, A 补测; 模拟版本落后后 B 重启 catch-up ==")
        _stop(procs.pop(SIZE_B))
        s, r = _call(PORT_A, "POST", "/api/surveys/before",
                     files={"file": ("s.csv", _make_csv([(12, 18, 5, 180)]))})
        assert s == 200
        s, r = _call(PORT_A, "POST", "/api/surveys/after",
                     files={"file": ("s.csv", _make_csv([(0, 19, 6, 255)]))})
        assert s == 200
        print("  B 停用期间 A 补测 2 次, 当前 v", r["data_version"])

        # 人为回退 B 的汇总版本, 模拟停用漏算/旧库未跟进
        import sqlite3
        conn = sqlite3.connect(f"file:{db_path}?mode=rw", uri=True, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute(
            "UPDATE block_size_registry SET stats_version = "
            "(SELECT CAST(value AS INTEGER) - 1 FROM meta WHERE key='data_version') "
            "WHERE block_size = ?", (SIZE_B,))
        conn.commit()
        conn.close()

        procs[SIZE_B] = _start(SIZE_B, PORT_B, db_path, work)
        caught = [
            line for line in (work / f"svc_{SIZE_B}.log").read_text().splitlines()
            if "catch-up" in line
        ]
        assert caught, "B 重启未触发 catch-up"
        print(" ", caught[0])
        for rect in rects + [(12, 18, 12, 18), (0, 19, 0, 19)]:
            _check(PORT_A, rect, f"A(16) {rect}")
            _check(PORT_B, rect, f"B(10)重启后 {rect}")

        print("== 7. 整批退回: B 发同号异值, 两边结果不变 ==")
        _, before_sel = _call(PORT_B, "GET", "/api/selection?x0=0&y0=0&x1=19&y1=19")
        status, resp = _call(PORT_B, "POST", "/api/surveys/after",
                             files={"file": ("bad.csv", _make_csv([(0, 0, 1, 999)]))})
        assert status == 409, resp
        print("  退回 HTTP 409:", resp["detail"][:36])
        _, after_b = _call(PORT_B, "GET", "/api/selection?x0=0&y0=0&x1=19&y1=19")
        _, after_a = _call(PORT_A, "GET", "/api/selection?x0=0&y0=0&x1=19&y1=19")
        assert after_b["net_volume_m3"] == before_sel["net_volume_m3"]
        assert after_a["net_volume_m3"] == before_sel["net_volume_m3"]
        assert after_b["data_version"] == before_sel["data_version"]
        _, batches = _call(PORT_B, "GET", "/api/batches?limit=10")
        assert any(b["status"] == "rejected" for b in batches["batches"])
        print("  退回后两边方量不变 v", after_b["data_version"], ", 留痕含 rejected")

        print("== 8. 网页静态资源两台均可访问 ==")
        for port in (PORT_A, PORT_B):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as resp:
                assert resp.status == 200 and "港池疏浚" in resp.read().decode()
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/static/app.js") as resp:
                assert resp.status == 200
        print("  两台 / 与 /static/app.js 均 200")

        print("\n双实例端到端自测全部通过 ✓")
    finally:
        for proc in procs.values():
            _stop(proc)


if __name__ == "__main__":
    main()
