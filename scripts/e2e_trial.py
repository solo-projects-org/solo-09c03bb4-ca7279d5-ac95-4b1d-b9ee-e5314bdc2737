#!/usr/bin/env python3
"""端到端试算: 通过 HTTP 验证补测前后选区核量与整批退回。"""
import csv
import io
import json
import urllib.request

BASE = "http://127.0.0.1:8000"


def call(method, path, data=None, files=None):
    if files is not None:
        boundary = "----dredgeboundary"
        body = b""
        for field, (filename, content) in files.items():
            body += f"--{boundary}\r\n".encode()
            body += (f'Content-Disposition: form-data; name="{field}"; '
                     f'filename="{filename}"\r\n\r\n').encode()
            body += content.encode() + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            BASE + path, data=body, method=method,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(BASE + path, method=method,
                                     data=json.dumps(data).encode() if data else None,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def make_csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["x", "y", "survey_no", "depth_cm"])
    for r in rows:
        w.writerow(["" if v is None else v for v in r])
    return buf.getvalue()


def show(title, status, body):
    print(f"\n== {title} -> HTTP {status}")
    print(json.dumps(body, ensure_ascii=False, indent=2))


# 40x40 格, 前测 200+x+y cm; 后测在 x<32,y<32 区域挖深 30cm
GRID = 40
before = [(x, y, 1, 200 + x + y) for x in range(GRID) for y in range(GRID)]
after = [(x, y, 1, 200 + x + y + (30 if x < 32 and y < 32 else 0))
         for x in range(GRID) for y in range(GRID)]

print("########## 1. 健康检查 ##########")
show("health", *call("GET", "/health"))

print("########## 2. 上传两期 ##########")
show("上传前测", *call("POST", "/api/surveys/before",
                       files={"file": ("before.csv", make_csv(before))}))
show("上传后测", *call("POST", "/api/surveys/after",
                       files={"file": ("after.csv", make_csv(after))}))

print("########## 3. 补测前选区核量 ##########")
# 整块区: 0..31 (4 个完整块)
show("整块选区 [0,0]-[31,31]", *call(
    "GET", "/api/selection?x0=0&y0=0&x1=31&y1=31"))
# 切开块的选区
show("混合选区 [5,7]-[34,29]", *call(
    "GET", "/api/selection?x0=5&y0=7&x1=34&y1=29"))
show("同选区细格校验", *call(
    "GET", "/api/selection?x0=5&y0=7&x1=34&y1=29&cellwise=true"))

print("########## 4. 整批退回: 同号异值 ##########")
show("同号异值 (应 409)", *call("POST", "/api/surveys/after",
                                files={"file": ("bad.csv", make_csv([(0, 0, 1, 999)]))}))
# 退回后方量不变
show("退回后再核量", *call("GET", "/api/selection?x0=0&y0=0&x1=31&y1=31"))

print("########## 5. 补测: 更高测次, 两格变浅 (负值) + 一格空水深 ##########")
supp = [(1, 1, 2, 200 + 1 + 1 + 30 - 50),   # 比前测浅 20cm -> -0.20 m3
        (2, 2, 2, None)]                    # 空水深覆盖, 该格退出计量
show("补测上传", *call("POST", "/api/surveys/after",
                      files={"file": ("supp.csv", make_csv(supp))}))

print("########## 6. 补测后选区核量 ##########")
show("整块选区(补测后)", *call("GET", "/api/selection?x0=0&y0=0&x1=31&y1=31"))
show("混合选区(补测后)", *call("GET", "/api/selection?x0=5&y0=7&x1=34&y1=29"))
show("混合选区细格校验", *call(
    "GET", "/api/selection?x0=5&y0=7&x1=34&y1=29&cellwise=true"))

print("########## 7. 批次留痕 / 块统计 ##########")
show("批次留痕", *call("GET", "/api/batches?limit=10"))
blk = call("GET", "/api/blocks")[1]
print(f"\n块大小={blk['block_size']}, 统计行数={len(blk['blocks'])}, "
      f"数据版本=v{blk['data_version']}")
for b in blk["blocks"][:4]:
    print(" ", b)

print("\n所有调用完成。")
