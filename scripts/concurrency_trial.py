"""对运行中的服务做并发补测压测: 12 线程交错上传, 校验统计不漏块。"""
import concurrent.futures as cf
import csv
import io
import json
import urllib.request

BASE = "http://127.0.0.1:8000"


def post(path, content):
    boundary = "----b"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"s.csv\"\r\n\r\n{content}\r\n--{boundary}--\r\n").encode()
    req = urllib.request.Request(
        BASE + path, data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def get(path):
    with urllib.request.urlopen(BASE + path) as r:
        return json.loads(r.read())


def csv_rows(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["x", "y", "survey_no", "depth_cm"])
    w.writerows(rows)
    return buf.getvalue()


# 基线: 块 (0,0) 即 0..15 方格, 两期水深 200
base = csv_rows([(x, y, 1, 200) for x in range(16) for y in range(16)])
post("/api/surveys/before", base)
post("/api/surveys/after", base)

# 12 个线程, 每线程独占一格, 测次号递增到 100, 水深各不相同
N = 12


def worker(i):
    x, y = i, 15 - i
    results = []
    for sno in range(2, 102):
        status, body = post("/api/surveys/after",
                            csv_rows([(x, y, sno, 300 + sno + i)]))
        results.append(status)
        if status not in (200, 409):
            return results, f"unexpected {status}"
    return results, None


with cf.ThreadPoolExecutor(max_workers=N) as ex:
    outs = list(ex.map(worker, range(N)))

statuses = [s for res, _ in outs for s in res]
print(f"并发请求 {len(statuses)} 次: 200={statuses.count(200)} 409={statuses.count(409)}")
assert all(err is None for _, err in outs), [e for _, e in outs if e]

blocks = get("/api/blocks")["blocks"]
after00 = next(b for b in blocks if b["bx"] == 0 and b["by"] == 0 and b["phase"] == "after")
# 12 格被改成 300+101+i, 其余 244 格仍为 200
expected_sum = sum(300 + 101 + i for i in range(N)) + 244 * 200
print("块(0,0) after 实测 sum =", after00["sum_depth_cm"], " 期望 =", expected_sum)
assert after00["sum_depth_cm"] == expected_sum, "并发补测漏算!"
assert after00["n_cells"] == 256 and after00["n_nonnull"] == 256

# 核量与全细格一致 (同版本快照)
sel = get("/api/selection?x0=0&y0=0&x1=15&y1=15")
cell = get("/api/selection?x0=0&y0=0&x1=15&y1=15&cellwise=true")
print("整块+边缘:", sel["area_cells_m2"], sel["net_volume_m3"], "v"+str(sel["data_version"]))
print("全细格  :", cell["area_cells_m2"], cell["net_volume_m3"], "v"+str(cell["data_version"]))
assert sel["area_cells_m2"] == cell["area_cells_m2"] == 256
assert abs(sel["net_volume_m3"] - cell["net_volume_m3"]) < 1e-9

# 并发核量期间再补测, 每次返回的整块与边缘必须自洽且两路径同版本一致
mismatch = 0
with cf.ThreadPoolExecutor(max_workers=8) as ex:
    futs = []
    for k in range(60):
        futs.append(ex.submit(get, "/api/selection?x0=3&y0=3&x1=18&y1=18"))
        futs.append(ex.submit(get, "/api/selection?x0=3&y0=3&x1=18&y1=18&cellwise=true"))
        if k % 5 == 0:
            futs.append(ex.submit(post, "/api/surveys/after",
                                  csv_rows([(7, 7, 200 + k, 250)])))
    results = [f.result() for f in futs]
mixed = [r for r in results if isinstance(r, dict) and r.get("mode") == "block+edge"]
cells = [r for r in results if isinstance(r, dict) and r.get("mode") == "cellwise"]
for m in mixed:
    same = [c for c in cells if c["data_version"] == m["data_version"]]
    for c in same:
        if m["area_cells_m2"] != c["area_cells_m2"] or abs(
                m["net_volume_m3"] - c["net_volume_m3"]) > 1e-9:
            mismatch += 1
print(f"并发核量 {len(mixed)} 次, 同版本两路径不一致: {mismatch}")
assert mismatch == 0
print("\n并发压测全部通过 ✓")
