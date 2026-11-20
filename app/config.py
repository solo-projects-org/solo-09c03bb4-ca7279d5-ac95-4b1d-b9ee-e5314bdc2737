"""运行配置。全部可通过环境变量覆盖。"""

from __future__ import annotations

import os
from pathlib import Path


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"环境变量 {name} 必须是正整数, 实际: {raw!r}")
    return value


# 每个统计块的边长 (格), 块 = BLOCK_SIZE x BLOCK_SIZE 个 1 米方格
BLOCK_SIZE: int = _int_env("BLOCK_SIZE", 16)

# SQLite 数据库文件默认路径 (容器内 /data 为挂载卷);
# 实际路径在 database.connect() 中动态解析 DB_PATH 环境变量
DB_PATH: Path = Path("/data/dredging.db")

# 写事务拿锁的等待时间 (秒); 并发补测撞锁时在此期间重试, 超时返回 503
BUSY_TIMEOUT_S: float = float(os.environ.get("BUSY_TIMEOUT_S", "10"))

# 单次上传允许的最大行数, 防止异常文件打满内存
MAX_ROWS_PER_BATCH: int = _int_env("MAX_ROWS_PER_BATCH", 5_000_000)

# 单格深度合理范围 (厘米), 仅做输入校验, 不参与计量
MIN_DEPTH_CM: int = _int_env("MIN_DEPTH_CM", 0)
MAX_DEPTH_CM: int = _int_env("MAX_DEPTH_CM", 100_000)
