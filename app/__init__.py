"""港池疏浚验收计量服务。

模块划分:
- config: 运行配置 (块大小/数据库路径等)
- database: SQLite 连接、建表、事务与并发控制
- ingest: CSV 解析与整批提交/回退逻辑
- stats: 分块统计重算
- query: 选区汇总 (整块 + 边缘细格, 同快照)
- main: FastAPI 路由与静态网页
"""

__version__ = "1.0.0"
