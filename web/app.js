/* 港池疏浚验收计量网页前端 (原生 JS, 无构建依赖) */
"use strict";

const $ = (id) => document.getElementById(id);
const canvas = $("map");
const ctx = canvas.getContext("2d");

let view = { minX: 0, minY: 0, maxX: 0, maxY: 0, cell: 20 };
let cellData = new Map();   // "x,y" -> {before, after}
let rect = null;             // 当前框选 {x0,y0,x1,y1} (格坐标, 闭区间)
let drag = null;             // 拖选状态

async function api(path, opts) {
  const resp = await fetch(path, opts);
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(body.detail || `HTTP ${resp.status}`);
  return body;
}

function setMsg(id, text, cls) {
  const el = $(id);
  el.textContent = text;
  el.className = "msg " + (cls || "mut");
}

/* ---------------- 上传 ---------------- */
document.querySelectorAll(".up button").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const phase = btn.dataset.phase;
    const input = $("file-" + phase);
    if (!input.files.length) {
      setMsg("up-msg", "请先选择 " + (phase === "before" ? "前测" : "后测") + " CSV 文件", "err");
      return;
    }
    const fd = new FormData();
    fd.append("file", input.files[0]);
    btn.disabled = true;
    try {
      const r = await api(`/api/surveys/${phase}`, { method: "POST", body: fd });
      setMsg(
        "up-msg",
        `${phase === "before" ? "前测" : "后测"}已接收: 收 ${r.rows_received} 行, `
        + `写入 ${r.rows_applied} 格, 旧测次跳过 ${r.rows_skipped_stale} 格, `
        + `重算块 ${r.blocks_touched.length} 个, 数据版本 v${r.data_version}`,
        "ok"
      );
      await refresh();
    } catch (e) {
      setMsg("up-msg", "上传被退回: " + e.message, "err");
    } finally {
      btn.disabled = false;
    }
  });
});

/* ---------------- 数据加载与绘制 ---------------- */
async function refresh() {
  const ext = await api("/api/extent");
  const any = [ext.before, ext.after].filter(Boolean);
  if (!any.length) {
    cellData = new Map();
    draw();
    await loadBatches();
    return;
  }
  const minX = Math.min(...any.map((e) => e.x0));
  const maxX = Math.max(...any.map((e) => e.x1));
  const minY = Math.min(...any.map((e) => e.y0));
  const maxY = Math.max(...any.map((e) => e.y1));
  view = { minX, minY, maxX, maxY, cell: 0 };

  const data = await api(`/api/cells?x0=${minX}&y0=${minY}&x1=${maxX}&y1=${maxY}`);
  cellData = new Map();
  for (const c of data.cells) {
    const key = `${c.x},${c.y}`;
    if (!cellData.has(key)) cellData.set(key, {});
    cellData.get(key)[c.phase] = c.depth_cm; // null = 空水深
  }
  draw();
  await loadBatches();
}

function colorFor(d) {
  if (d.before === undefined || d.after === undefined) return "#e8edf2";
  if (d.before === null || d.after === null) return "#94a3b8";
  const diff = d.after - d.before; // 正: 挖深; 负: 回淤
  if (diff === 0) return "#f1f5f9";
  const mag = Math.min(Math.abs(diff) / 50, 1);
  if (diff > 0) {
    const g = Math.round(185 - mag * 130), b = Math.round(120 - mag * 90);
    return `rgb(185,${g},${b})`;
  }
  const g = Math.round(78 + (1 - mag) * 120), r = Math.round(29 + (1 - mag) * 150);
  return `rgb(${r},${g},216)`;
}

function draw() {
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);
  const nx = view.maxX - view.minX + 1 || 1;
  const ny = view.maxY - view.minY + 1 || 1;
  const cell = Math.max(4, Math.floor(Math.min((W - 40) / nx, (H - 40) / ny)));
  view.cell = cell;
  const ox = 20, oy = 20;
  const px = (x) => ox + (x - view.minX) * cell;
  const py = (y) => oy + (y - view.minY) * cell;

  // 格底色
  for (const [key, d] of cellData) {
    const [x, y] = key.split(",").map(Number);
    ctx.fillStyle = colorFor(d);
    ctx.fillRect(px(x), py(y), cell, cell);
  }

  // 块边界 (每 16 格粗线, 由后端 /api/blocks 给出块大小)
  ctx.strokeStyle = "#475569";
  ctx.lineWidth = 1.5;
  const bs = Number($("bsize").textContent);
  for (let bx = Math.floor(view.minX / bs); bx <= Math.floor(view.maxX / bs); bx++) {
    const x = bx * bs;
    if (x < view.minX || x > view.maxX) continue;
    ctx.beginPath(); ctx.moveTo(px(x), oy); ctx.lineTo(px(x), oy + ny * cell); ctx.stroke();
  }
  for (let by = Math.floor(view.minY / bs); by <= Math.floor(view.maxY / bs); by++) {
    const y = by * bs;
    if (y < view.minY || y > view.maxY) continue;
    ctx.beginPath(); ctx.moveTo(ox, py(y)); ctx.lineTo(ox + nx * cell, py(y)); ctx.stroke();
  }

  // 细格线
  ctx.strokeStyle = "rgba(100,116,139,.25)";
  ctx.lineWidth = 1;
  ctx.strokeRect(ox, oy, nx * cell, ny * cell);

  // 框选区
  if (rect) {
    const r = normalize(rect);
    ctx.fillStyle = "rgba(253,230,138,.45)";
    ctx.strokeStyle = "#b45309";
    ctx.lineWidth = 2;
    const rw = (r.x1 - r.x0 + 1) * cell, rh = (r.y1 - r.y0 + 1) * cell;
    ctx.fillRect(px(r.x0), py(r.y0), rw, rh);
    ctx.strokeRect(px(r.x0), py(r.y0), rw, rh);
  }
}

function normalize(r) {
  return {
    x0: Math.min(r.x0, r.x1), x1: Math.max(r.x0, r.x1),
    y0: Math.min(r.y0, r.y1), y1: Math.max(r.y0, r.y1),
  };
}

/* ---------------- 鼠标沿格线框选 ---------------- */
function eventCell(ev) {
  const box = canvas.getBoundingClientRect();
  const sx = canvas.width / box.width, sy = canvas.height / box.height;
  const cx = (ev.clientX - box.left) * sx - 20;
  const cy = (ev.clientY - box.top) * sy - 20;
  const x = view.minX + Math.floor(cx / view.cell);
  const y = view.minY + Math.floor(cy / view.cell);
  return { x, y };
}

canvas.addEventListener("mousedown", (ev) => {
  const c = eventCell(ev);
  if (c.x < view.minX || c.x > view.maxX || c.y < view.minY || c.y > view.maxY) return;
  drag = { x0: c.x, y0: c.y };
  rect = { x0: c.x, y0: c.y, x1: c.x, y1: c.y };
  draw();
});
canvas.addEventListener("mousemove", (ev) => {
  if (!drag) return;
  const c = eventCell(ev);
  rect = { x0: drag.x0, y0: drag.y0, x1: c.x, y1: c.y };
  syncRectInputs();
  draw();
});
window.addEventListener("mouseup", () => { drag = null; });

function syncRectInputs() {
  if (!rect) return;
  const r = normalize(rect);
  $("r-x0").value = r.x0; $("r-y0").value = r.y0;
  $("r-x1").value = r.x1; $("r-y1").value = r.y1;
}

/* ---------------- 核量 ---------------- */
async function runSelection(cellwise) {
  const r = {
    x0: Number($("r-x0").value), y0: Number($("r-y0").value),
    x1: Number($("r-x1").value), y1: Number($("r-y1").value),
  };
  if ([r.x0, r.y0, r.x1, r.y1].some((v) => Number.isNaN(v))) {
    setMsg("sel-msg", "请在画布拖选或填写完整的整数格坐标", "err");
    return;
  }
  rect = r;
  draw();
  try {
    const q = new URLSearchParams({ ...r, cellwise: String(cellwise) });
    const res = await api("/api/selection?" + q);
    $("r-area").textContent = `${res.area_cells_m2} m²`;
    $("r-vol").textContent = `${res.net_volume_m3.toFixed(3)}`;
    $("r-blocks").textContent = res.whole_blocks;
    $("r-edge").textContent =
      `${res.edge_cells} 格 (其中可计量 ${res.edge_matched_cells})`;
    $("r-ver").textContent = "v" + res.data_version;
    $("r-mode").textContent = res.mode === "cellwise" ? "全部细格(校验)" : "整块+边缘";
    setMsg(
      "sel-msg",
      `选区 [${res.x0},${res.y0}]–[${res.x1},${res.y1}] 共 `
      + `${(res.x1-res.x0+1)*(res.y1-res.y0+1)} 格; `
      + `整块与边缘取自同一快照 v${res.data_version}`,
      "ok"
    );
  } catch (e) {
    setMsg("sel-msg", "核量失败: " + e.message, "err");
  }
}

$("btn-select").addEventListener("click", () => runSelection(false));
$("btn-cellwise").addEventListener("click", () => runSelection(true));
$("btn-clear").addEventListener("click", () => {
  rect = null; draw();
  ["r-x0", "r-y0", "r-x1", "r-y1"].forEach((id) => ($(id).value = ""));
  setMsg("sel-msg", "", "mut");
});

/* ---------------- 批次留痕 ---------------- */
async function loadBatches() {
  const data = await api("/api/batches?limit=20");
  const tb = document.querySelector("#batch-table tbody");
  tb.innerHTML = "";
  for (const b of data.batches) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${b.id}</td><td>${b.phase === "before" ? "前" : "后"}</td>`
      + `<td>${b.survey_no}</td><td>${b.row_count}</td>`
      + `<td class="tag-${b.status}">${b.status === "accepted" ? "接收" : "退回"}</td>`
      + `<td style="text-align:left">${b.reject_reason || ""}</td>`;
    tb.appendChild(tr);
  }
}

(async function init() {
  try {
    const blk = await api("/api/blocks");
    $("bsize").textContent = blk.block_size;
    $("bsize2").textContent = blk.block_size;
    await refresh();
  } catch (e) {
    setMsg("up-msg", "初始化失败: " + e.message, "err");
  }
})();
