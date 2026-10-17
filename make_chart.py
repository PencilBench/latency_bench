"""生成多次运行之间的解码延迟对比图（Pillow，输出 PNG）。

用法::

    python make_chart.py \
        --run "Apple M6=results/<runA>/summary.json" \
        --run "Intel Arc B390=/Volumes/GM7/MacShare/<runB>/summary.json" \
        --run "RTX 5090=/Volumes/SDEX/<runC>/summary.json" \
        --colors orange,blue,green -o compare.png

``--run`` 可重复 2 次以上，**第一个是基线**（倍数与"更快"的比较基准）。
兼容旧写法 ``--a … --b …``。

数据源是各次运行的 ``summary.json``。它只提供 min / p50 / p95 / p99 / max / mean /
stdev / jitter，没有 p25 / p75，因此主图用「范围 + 标记」而不是箱线图。

**可靠度标记（*）**：当某行的 ``p50`` 远小于均值（``p50 < 0.2 ms`` 且
``mean > 5×p50``）时，说明该片段的每包计时被解码器的**异步返回**主导 ——
绝大多数包 ``send_packet`` 立即返回、真正的解码开销只在少数包上体现，
于是 p50/p99 反映的是轮询节奏而不是帧延迟。这种行的**均值仍可用，分位数不可比**，
图上会加 * 并在图例中说明。
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

CJK = "/System/Library/Fonts/Hiragino Sans GB.ttc"

#: 可用的命名配色（也可直接给 ``#RRGGBB``）。
PALETTE = {
    "blue": (37, 99, 235),
    "orange": (234, 88, 12),
    "green": (22, 163, 74),
    "red": (220, 38, 38),
    "purple": (147, 51, 234),
    "teal": (13, 148, 136),
    "gray": (100, 116, 139),
    "brown": (146, 64, 14),
}

#: 自动分配颜色的顺序。
DEFAULT_COLORS = ["blue", "orange", "green", "purple", "teal", "red", "brown", "gray"]

S = 2
W = 1240
V_MIN, V_MAX = 0.03, 60.0


def F(sz: int, bold: bool = False):
    return ImageFont.truetype(CJK, sz, index=1 if bold else 0)


def parse_color(spec: str) -> tuple[int, int, int]:
    key = spec.strip().lower()
    if key in PALETTE:
        return PALETTE[key]
    hexed = key.lstrip("#")
    if len(hexed) == 6:
        try:
            return tuple(int(hexed[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
        except ValueError:
            pass
    raise SystemExit(f"无法解析颜色 {spec!r}；可用：{', '.join(PALETTE)}，或 #RRGGBB")


def _codec_of(name: str) -> str:
    return name.split("_", 1)[0].upper()


def _resolution_of(width: int | None) -> str:
    if not width:
        return "?"
    for limit, tag in ((3840, "4K"), (2560, "1440p"), (1920, "1080p"), (1280, "720p")):
        if width >= limit:
            return tag
    return f"{width}px"


def clip_label(clip: dict) -> str:
    src = clip.get("source") or {}
    codec = _codec_of(str(clip.get("clip") or ""))
    rate = src.get("bit_rate")
    mbps = f"{rate / 1e6:.0f} Mbps" if isinstance(rate, (int, float)) and rate else "? Mbps"
    fps = src.get("avg_frame_rate")
    fps_txt = f" / {fps:.0f}fps" if isinstance(fps, (int, float)) and fps else ""
    return f"{codec} {_resolution_of(src.get('width'))} {mbps}{fps_txt}"


def is_async_polluted(dec: dict) -> bool:
    """p50 远小于均值 → 每包计时被异步返回主导，分位数不可比。"""
    p50 = dec.get("median_ms")
    mean = dec.get("mean_ms")
    if not isinstance(p50, (int, float)) or not isinstance(mean, (int, float)):
        return False
    return p50 < 0.2 and mean > 5 * p50


def _env_label(payload: dict) -> str:
    env = payload.get("environment") or {}
    platform = str(env.get("platform") or "").split("-")[0] or env.get("sys_platform") or "?"
    hw = payload.get("hwaccel")
    return " / ".join(b for b in (platform, hw) if b)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="多次运行的解码延迟对比图")
    p.add_argument("--run", action="append", metavar="LABEL=PATH",
                   help="一次运行：显示名=summary.json，可重复；第一个是基线")
    p.add_argument("--a", type=Path, help="(旧写法) A 侧 summary.json")
    p.add_argument("--b", type=Path, help="(旧写法) B 侧 summary.json")
    p.add_argument("--label-a", default=None, help="(旧写法) A 侧显示名")
    p.add_argument("--label-b", default=None, help="(旧写法) B 侧显示名")
    p.add_argument("--color-a", default=None, help="(旧写法) A 侧颜色")
    p.add_argument("--color-b", default=None, help="(旧写法) B 侧颜色")
    p.add_argument("--colors", default=None,
                   help=f"逗号分隔的配色，默认 {','.join(DEFAULT_COLORS[:3])}…；"
                        f"可用：{', '.join(PALETTE)} 或 #RRGGBB")
    p.add_argument("-o", "--out", required=True, type=Path, help="输出 PNG 路径")
    return p


def resolve_runs(args) -> list[dict]:
    """把 CLI 归一成 [{label, path, color}] 列表。"""
    runs: list[dict] = []
    if args.run:
        for item in args.run:
            if "=" not in item:
                raise SystemExit(f"--run 需要 LABEL=PATH 形式，收到 {item!r}")
            label, _, path = item.partition("=")
            runs.append({"label": label.strip(), "path": Path(path.strip()).expanduser()})
    elif args.a and args.b:
        runs.append({"label": (args.label_a or args.a.parent.name), "path": args.a})
        runs.append({"label": (args.label_b or args.b.parent.name), "path": args.b})
    else:
        raise SystemExit("请用 --run LABEL=PATH（可重复，至少 2 次）指定要对比的运行")
    if len(runs) < 2:
        raise SystemExit("至少需要两次运行才能对比")
    specs = ([s.strip() for s in args.colors.split(",")] if args.colors else []) or DEFAULT_COLORS
    for i, r in enumerate(runs):
        r["path"] = r["path"].resolve()
        if not r["path"].exists():
            raise SystemExit(f"找不到 {r['path']}")
        r["payload"] = json.loads(r["path"].read_text(encoding="utf-8"))
        r["clips"] = {c["clip"]: c for c in r["payload"]["clips"]}
        spec = specs[i % len(specs)]
        r["color"] = parse_color(spec)
    # 旧写法的 --color-a/--color-b 覆盖
    if not args.run and not args.colors:
        if args.color_a:
            runs[0]["color"] = parse_color(args.color_a)
        if args.color_b:
            runs[1]["color"] = parse_color(args.color_b)
    return runs


def main() -> None:
    args = build_parser().parse_args()
    runs = resolve_runs(args)
    base = runs[0]
    n = len(runs)

    # 片段顺序：编解码器 → 分辨率 → 码率
    common = [c for c in base["clips"] if all(c in r["clips"] for r in runs)]
    if not common:
        raise SystemExit("这些运行之间没有共同片段")
    common.sort(key=lambda c: (
        _codec_of(c),
        (base["clips"][c].get("source") or {}).get("height") or 0,
        (base["clips"][c].get("source") or {}).get("bit_rate") or 0,
    ))
    ORDER = common
    SHORT = {c: clip_label(base["clips"][c]) for c in ORDER}

    step = 20
    row_h = 24 + n * step
    top = 74
    left, right = 380, 1055
    grid_bottom = top + row_h * len(ORDER) - 12
    legend_y = grid_bottom + 62
    by0 = legend_y + 46
    bar_block = 5 * 33
    H = by0 + 24 + max(bar_block, 7 * 19) + 34

    l0, l1 = math.log10(V_MIN), math.log10(V_MAX)
    img = Image.new("RGB", (W * S, H * S), "white")
    dr = ImageDraw.Draw(img)

    def T(x, y, t, sz=13, fill=(17, 24, 39), anchor="la", bold=False):
        dr.text((x * S, y * S), t, font=F(sz * S, bold), fill=fill, anchor=anchor)

    def rr(box, fill=None, outline=None, width=1):
        dr.rounded_rectangle([c * S for c in box], fill=fill, outline=outline,
                             width=width * S, radius=2 * S)

    def ln(xy, fill, w=1):
        dr.line([c * S for c in xy], fill=fill, width=max(1, int(round(w * S))))

    def dot(x, y, r, fill=None, outline=None, ow=1):
        dr.ellipse([(x - r) * S, (y - r) * S, (x + r) * S, (y + r) * S],
                   fill=fill, outline=outline, width=max(1, int(round(ow * S))))

    def X(v):
        v = min(max(v, V_MIN), V_MAX)
        return left + (math.log10(v) - l0) / (l1 - l0) * (right - left)

    T(40, 22, "解码延迟对比：" + " vs ".join(r["label"] for r in runs), 21, bold=True)

    T(1063, top - 30, "均值", 11, (107, 114, 128), "lm", bold=True)
    T(1146, top - 30, "p99", 11, (107, 114, 128), "lm", bold=True)

    for gv in [0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50]:
        if not (V_MIN <= gv <= V_MAX):
            continue
        x = X(gv)
        ln((x, top - 18, x, grid_bottom), (229, 231, 235), 1)
        T(x, grid_bottom + 6, f"{gv:g}", 11, (107, 114, 128), "ma")
    T((left + right) / 2, grid_bottom + 27, "decode_ms / packet（对数轴，ms）", 12, (55, 65, 81), "ma")

    for i, clip in enumerate(ORDER):
        y0 = top + i * row_h
        if i % 2 == 0:
            rr((left - 8, y0 - 6, 1215, y0 + row_h - 16), fill=(249, 250, 251))
        T(left - 66, y0 + 14, SHORT[clip], 12.5, anchor="ra", bold=True)
        for j, run in enumerate(runs):
            d = run["clips"][clip]["decode"]
            polluted = is_async_polluted(d)
            col = run["color"]
            y = y0 + 18 + j * step
            ln((X(d["min_ms"]), y, X(d["max_ms"]), y), col + (80,), 1.2)
            if not polluted:
                ln((X(d["median_ms"]), y, X(d["p99_ms"]), y), col, 6)
                ln((X(d["p95_ms"]), y - 5, X(d["p95_ms"]), y + 5), col + (170,), 1.4)
            dot(X(d["max_ms"]), y, 2.8, outline=col, ow=1.3)
            dot(X(d["mean_ms"]), y, 3.4, fill="white", outline=col, ow=1.7)
            T(1070, y, f"{d['mean_ms']:.2f}", 10, col, "lm", bold=True)
            T(1153, y, f"{d['p99_ms']:.2f}{'*' if polluted else ''}", 10, col, "lm", bold=True)
        # 左侧的短标签（首字母/序号，颜色对应图例）
        for j, run in enumerate(runs):
            T(left - 18, y0 + 18 + j * step, chr(ord("A") + j), 10, run["color"], "ra", bold=True)

    # 图例
    for k, run in enumerate(runs):
        lx = 40 + (k % 3) * 330
        ly = legend_y + (k // 3) * 18
        rr((lx, ly - 4, lx + 14, ly + 2), fill=run["color"])
        dot(lx + 7, ly - 1, 3.4, fill="white", outline=run["color"], ow=1.6)
        T(lx + 20, ly - 8, f"{chr(ord('A') + k)} · {run['label']} / {_env_label(run['payload'])}",
          11.5, (55, 65, 81))
    T(40, legend_y + 22, "粗线 = p50→p99   细刻度 = p95   白心点 = 均值   空心圈 = 最大值   "
                                "浅色细线 = min→max   * = p50/p99 受异步返回影响，不可比（均值仍可用）",
      10.5, (107, 114, 128))

    # 倍数条：相对基线
    others = runs[1:]
    T(40, by0, f"延迟倍数（相对 {base['label']}；< 1.0 表示更快）", 15, bold=True)
    # 均值是「总解码时间 / 包数」，即使该行被异步返回"污染"也仍然有效，
    # 因此均值用全部片段；分位数与抖动才需要剔除污染行，且所有平台统一用同一子集。
    clean = [c for c in ORDER
             if not any(is_async_polluted(r["clips"][c]["decode"]) for r in runs)]
    metrics = []
    for key, name, subset in [("mean_ms", "均值", ORDER), ("median_ms", "p50", clean),
                              ("p95_ms", "p95", clean), ("p99_ms", "p99", clean),
                              ("jitter_ms", "抖动", clean)]:
        vals = []
        for r in others:
            ratios = [r["clips"][c]["decode"][key] / base["clips"][c]["decode"][key]
                      for c in subset]
            vals.append((r, float(np.exp(np.log(ratios).mean())) if ratios else float("nan")))
        metrics.append((name, vals))
    print(f"  分位数/抖动只用 {len(clean)}/{len(ORDER)} 个未受污染的片段："
          + "、".join(SHORT[c].split(' (')[0] for c in clean if c not in ORDER[:0]) if False else
          f"  分位数/抖动统计子集：{len(clean)}/{len(ORDER)} 个片段")
    bx, bw, bh = 118, 210, 24
    slot = (bw - 6 * (len(others) - 1)) / len(others)
    for k, (name, vals) in enumerate(metrics):
        yy = by0 + 24 + k * (bh + 9)
        T(bx - 12, yy + bh / 2, name, 13, (55, 65, 81), "rm")
        rr((bx, yy, bx + bw, yy + bh), fill=(243, 244, 246))
        parts = []
        for m, (run, val) in enumerate(vals):
            x0 = bx + m * (slot + 6)
            if not math.isfinite(val):
                parts.append(f"{chr(ord('A') + m + 1)} 不可比")
                continue
            rr((x0, yy, x0 + slot * min(val, 1.0), yy + bh), fill=run["color"])
            if val > 1.0:
                rr((x0 + slot, yy, x0 + slot * min(val, 2.0), yy + bh), fill=run["color"] + (60,))
            parts.append(f"{chr(ord('A') + m + 1)} {val:.2f}×")
        T(bx + bw + 10, yy + bh / 2, " · ".join(parts), 11, (55, 65, 81), "lm", bold=True)
        ln((bx + bw, yy - 4, bx + bw, yy + bh + 4), (156, 163, 175), 1.4)

    # 右侧要点
    cx = 640
    T(cx, by0, "要点", 15, bold=True)
    lines: list[str] = []
    for run, val in metrics[0][1]:
        if math.isfinite(val):
            lines.append(f"• 均值：{run['label']} 是基线的 {val:.2f}×"
                         f"（{'快' if val < 1 else '慢'} {abs(1 - val) * 100:.0f}%）")
    best = {}
    for c in ORDER:
        vals = [(r["clips"][c]["decode"]["mean_ms"], r["label"]) for r in runs]
        best[min(vals)[1]] = best.get(min(vals)[1], 0) + 1
    lines.append("• 逐片段均值最快：" + "、".join(f"{k} {v} 个" for k, v in best.items()))
    for name, vals in metrics:
        if name in ("均值", "p50"):
            continue
        seg = [f"{r['label']} {v:.2f}×" for r, v in vals if math.isfinite(v)]
        if seg:
            lines.append(f"• {name}：" + "、".join(seg))
    mx = {r["label"]: max(r["clips"][c]["decode"]["max_ms"] for c in ORDER) for r in runs}
    lines.append("• 最大单帧：" + "、".join(f"{k} {v:.1f} ms" for k, v in mx.items()))
    pol_counts = {r["label"]: sum(1 for c in ORDER if is_async_polluted(r["clips"][c]["decode"]))
                  for r in runs}
    pol_txt = "、".join(f"{k} {v} 行" for k, v in pol_counts.items() if v)
    if pol_txt:
        lines.append(f"• * 分位数不可比：{pol_txt}（均值仍可用）")
    for k, t in enumerate(lines):
        T(cx, by0 + 26 + k * 19, t, 11.5, (55, 65, 81))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    img.resize((W, H), Image.LANCZOS).save(args.out)
    print("wrote", args.out, f"{args.out.stat().st_size/1024:.0f} KiB")
    for name, vals in metrics:
        print(f"  {name}: " + ", ".join(f"{r['label']}={v:.3f}" for r, v in vals))


if __name__ == "__main__":
    main()
