"""生成两次运行之间的解码延迟对比图（Pillow，输出 PNG）。

用法::

    python make_chart.py --a <summary.json> --b <summary.json> -o out.png \
        [--label-a "Apple M6" --label-b "Intel Arc B390"]

数据源是两次运行的 ``summary.json``（每轮的 CSV 与汇总同轮，见 ``round.frames_csv``）。
``summary.json`` 只提供 min / p50 / p95 / p99 / max / mean / stdev / jitter，
没有 p25 / p75，因此主图用「范围 + 标记」而不是箱线图。
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

CJK = "/System/Library/Fonts/Hiragino Sans GB.ttc"

#: A / B 两侧的配色（蓝 / 橙）。
COLORS = {"a": (37, 99, 235), "b": (234, 88, 12)}

S = 2
W, H = 1240, 950
V_MIN, V_MAX = 0.06, 40.0


def F(sz: int, bold: bool = False):
    return ImageFont.truetype(CJK, sz, index=1 if bold else 0)


def load(path: Path) -> dict:
    d = json.loads(path.read_text(encoding="utf-8"))
    return {c["clip"]: c for c in d["clips"]}


def geo(vals) -> float:
    a = np.asarray(vals, dtype=float)
    return float(np.exp(np.log(a).mean()))


def _codec_of(name: str) -> str:
    """从素材名取编解码器简称：``AV1_126.99Mbps.mp4`` → ``AV1``。"""
    return name.split("_", 1)[0].upper()


def _resolution_of(width: int | None) -> str:
    if not width:
        return "?"
    if width >= 3840:
        return "4K"
    if width >= 2560:
        return "1440p"
    if width >= 1920:
        return "1080p"
    if width >= 1280:
        return "720p"
    return f"{width}px"


def clip_label(clip: dict) -> str:
    """从 summary 的 ``source`` 生成「AV1 4K 112 Mbps / 60fps」这类标签。"""
    src = clip.get("source") or {}
    codec = _codec_of(str(clip.get("clip") or ""))
    res = _resolution_of(src.get("width"))
    rate = src.get("bit_rate")
    mbps = f"{rate / 1e6:.0f} Mbps" if isinstance(rate, (int, float)) and rate else "? Mbps"
    fps = src.get("avg_frame_rate")
    fps_txt = f" / {fps:.0f}fps" if isinstance(fps, (int, float)) and fps else ""
    return f"{codec} {res} {mbps}{fps_txt}"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="生成两次运行之间的解码延迟对比图")
    p.add_argument("--a", required=True, type=Path, help="A 侧的 summary.json")
    p.add_argument("--b", required=True, type=Path, help="B 侧的 summary.json")
    p.add_argument("--label-a", default=None, help="A 侧显示名（默认取 run_id）")
    p.add_argument("--label-b", default=None, help="B 侧显示名（默认取 run_id）")
    p.add_argument("-o", "--out", required=True, type=Path, help="输出 PNG 路径")
    return p


def _side_label(explicit: str | None, path: Path, payload: dict) -> str:
    if explicit:
        return explicit
    return str(payload.get("run_id") or path.parent.name)


def _env_label(payload: dict, clips: dict) -> str:
    """紧凑的环境标签：``macOS / videotoolbox``（完整 platform 串太长，会撞图例）。"""
    env = payload.get("environment") or {}
    platform = str(env.get("platform") or "").split("-")[0] or env.get("sys_platform") or "?"
    hw = next((c.get("hwaccel") for c in clips.values() if c.get("hwaccel")), None)
    return " / ".join(b for b in (platform, hw) if b)


def main() -> None:
    args = build_parser().parse_args()
    raw = {k: json.loads(p.read_text(encoding="utf-8")) for k, p in (("a", args.a), ("b", args.b))}
    D = {k: {c["clip"]: c for c in v["clips"]} for k, v in raw.items()}

    name_a = _side_label(args.label_a, args.a, raw["a"])
    name_b = _side_label(args.label_b, args.b, raw["b"])

    # 片段顺序：编解码器 → 分辨率 → 码率，保证两侧同序
    common = [c for c in D["a"] if c in D["b"]]
    common.sort(key=lambda c: (
        _codec_of(c),
        (D["a"][c].get("source") or {}).get("height") or 0,
        (D["a"][c].get("source") or {}).get("bit_rate") or 0,
    ))
    if not common:
        raise SystemExit("两侧 summary.json 没有共同片段，无法对比")
    ORDER = common
    SHORT = {c: clip_label(D["a"][c]) for c in ORDER}

    img = Image.new("RGB", (W * S, H * S), "white")
    dr = ImageDraw.Draw(img)

    def T(x, y, t, sz=13, fill=(17, 24, 39), anchor="la", bold=False):
        dr.text((x * S, y * S), t, font=F(sz * S, bold), fill=fill, anchor=anchor)

    def rr(box, fill=None, outline=None, width=1, radius=0):
        dr.rounded_rectangle([c * S for c in box], fill=fill, outline=outline,
                             width=width * S, radius=radius * S)

    def ln(xy, fill, w=1):
        dr.line([c * S for c in xy], fill=fill, width=max(1, int(round(w * S))))

    def dot(x, y, r, fill=None, outline=None, ow=1):
        dr.ellipse([(x - r) * S, (y - r) * S, (x + r) * S, (y + r) * S],
                   fill=fill, outline=outline, width=max(1, int(round(ow * S))))

    left, right, top, row_h = 360, 1075, 74, 72
    l0, l1 = math.log10(V_MIN), math.log10(V_MAX)

    def X(v):
        v = min(max(v, V_MIN), V_MAX)
        return left + (math.log10(v) - l0) / (l1 - l0) * (right - left)

    T(40, 22, f"解码延迟对比：{name_a} vs {name_b}", 21, bold=True)

    T(1090, top - 30, "p50", 11, (107, 114, 128), "lm", bold=True)
    T(1173, top - 30, "max", 11, (107, 114, 128), "lm", bold=True)

    grid_bottom = top + row_h * len(ORDER) - 16
    for gv in [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 40]:
        x = X(gv)
        ln((x, top - 18, x, grid_bottom), (229, 231, 235), 1)
        T(x, grid_bottom + 6, f"{gv:g}", 11, (107, 114, 128), "ma")
    T((left + right) / 2, grid_bottom + 27, "decode_ms / packet（对数轴，ms）", 12, (55, 65, 81), "ma")

    for i, clip in enumerate(ORDER):
        y0 = top + i * row_h
        if i % 2 == 0:
            rr((left - 8, y0 - 6, 1225, y0 + row_h - 18), fill=(249, 250, 251))
        T(left - 58, y0 + 14, SHORT[clip], 12.5, anchor="ra", bold=True)
        for j, run in enumerate(["a", "b"]):
            d = D[run][clip]["decode"]
            col = COLORS[run]
            y = y0 + 22 + j * 28
            ln((X(d["min_ms"]), y, X(d["max_ms"]), y), col + (80,), 1.2)
            ln((X(d["median_ms"]), y, X(d["p99_ms"]), y), col, 6)
            ln((X(d["p95_ms"]), y - 6, X(d["p95_ms"]), y + 6), col + (170,), 1.4)
            dot(X(d["max_ms"]), y, 3.0, outline=col, ow=1.4)
            dot(X(d["mean_ms"]), y, 3.8, fill="white", outline=col, ow=1.8)
            T(1097, y, f"{d['median_ms']:.2f}", 10.5, col, "lm", bold=True)
            T(1180, y, f"{d['max_ms']:.2f}", 10.5, col, "lm", bold=True)
        T(left - 16, y0 + 18, "A", 10, COLORS["a"], "ra")
        T(left - 16, y0 + 46, "B", 10, COLORS["b"], "ra")

    ly = grid_bottom + 50
    for k, (run, tag) in enumerate([("a", name_a), ("b", name_b)]):
        col = COLORS[run]
        lx = 360 + k * 330
        rr((lx, ly - 4, lx + 16, ly + 2), fill=col)
        dot(lx + 8, ly - 1, 3.8, fill="white", outline=col, ow=1.8)
        T(lx + 24, ly - 8, f"{'A' if run == 'a' else 'B'} · {tag} / {_env_label(raw[run], D[run])}", 12, (55, 65, 81))
    T(360, ly + 18, "粗线 = p50→p99   细刻度 = p95   白心点 = 均值   空心圈 = 最大值   浅色细线 = min→max",
      11, (107, 114, 128))

    by0 = ly + 50
    T(40, by0, f"{name_b} / {name_a} 延迟倍数（< 1.0 表示 {name_b} 更快）", 15, bold=True)
    metrics = []
    for key, name in [("mean_ms", "均值"), ("median_ms", "p50"), ("p95_ms", "p95"),
                      ("p99_ms", "p99"), ("jitter_ms", "抖动")]:
        metrics.append((name, geo([D["b"][c]["decode"][key] / D["a"][c]["decode"][key]
                                   for c in ORDER])))
    bx, bw, bh = 100, 200, 24
    for k, (name, val) in enumerate(metrics):
        yy = by0 + 24 + k * (bh + 9)
        T(bx - 12, yy + bh / 2, name, 13, (55, 65, 81), "rm")
        rr((bx, yy, bx + bw, yy + bh), fill=(243, 244, 246))
        if val <= 1.0:
            rr((bx, yy, bx + bw * val, yy + bh), fill=COLORS["b"])
            txt = f"{val:.2f}×（{name_b} 快 {(1/val-1)*100:.0f}%）"
        else:
            rr((bx + bw, yy, bx + bw * min(val, 2.0), yy + bh), fill=COLORS["a"])
            txt = f"{val:.2f}×（{name_b} 慢 {(val-1)*100:.0f}%）"
        ln((bx + bw, yy - 4, bx + bw, yy + bh + 4), (156, 163, 175), 1.4)
        T(bx + bw + 10, yy + bh / 2, txt, 12.5, anchor="lm", bold=True)

    mx_b = [D["b"][c]["decode"]["max_ms"] for c in ORDER]
    mx_a = [D["a"][c]["decode"]["max_ms"] for c in ORDER]
    r4k_a = [D["a"][c]["decode"]["mean_ms"] for c in ORDER if "4K" in SHORT[c]]
    r4k_b = [D["b"][c]["decode"]["mean_ms"] for c in ORDER if "4K" in SHORT[c]]
    r1080 = [D["b"][c]["decode"]["mean_ms"] / D["a"][c]["decode"]["mean_ms"]
             for c in ORDER if "1080p" in SHORT[c]]
    r4km = [D["b"][c]["decode"]["mean_ms"] / D["a"][c]["decode"]["mean_ms"]
            for c in ORDER if "4K" in SHORT[c]]
    rt_a = min(D["a"][c]["realtime_factor"] for c in ORDER)
    rt_b = min(D["b"][c]["realtime_factor"] for c in ORDER)
    big_b = sorted(v for v in mx_b if v > 10)

    cx = 600
    T(cx, by0, "要点", 15, bold=True)
    notes: list[str] = []
    notes.append(f"• {name_b} 在全部 {len(ORDER)} 个片段上均值都更快，几何均值 {metrics[0][1]:.2f}×。")
    if r1080 and r4km:
        notes.append(
            f"• 1080p 快约 {1/min(r1080):.1f} 倍（{min(r1080):.2f}×）；"
            f"4K 快 {1/max(r4km):.2f}–{1/min(r4km):.2f} 倍（{min(r4km):.2f}–{max(r4km):.2f}×）。"
        )
    if r4k_a and r4k_b:
        notes.append(
            f"• 4K 均值：{name_a} 为 {min(r4k_a):.2f}–{max(r4k_a):.2f} ms，"
            f"{name_b} 为 {min(r4k_b):.2f}–{max(r4k_b):.2f} ms。"
        )
    notes.append(
        f"• p99 差距收窄到 {metrics[3][1]:.2f}×；抖动几何均值 {name_b} "
        f"{'低' if metrics[4][1] < 1 else '高'} {abs(100*(1-metrics[4][1])):.0f}%"
        f"（{metrics[4][1]:.2f}×）。"
    )
    notes.append(
        f"• 单帧最大值：{name_b} 有 {len(big_b)} 段 >10 ms"
        + (f"（{big_b[0]:.1f}–{big_b[-1]:.1f} ms）；" if big_b else "；")
    )
    notes.append(
        f"  {name_a} 有 {sum(1 for v in mx_a if v > 10)} 段 >10 ms（最大 {max(mx_a):.1f} ms）。"
    )
    notes.append(
        f"• 最重载下均可实时：最小实时倍率 {name_a} {rt_a:.2f}×、{name_b} {rt_b:.2f}×。"
    )
    for k, t in enumerate(notes):
        T(cx, by0 + 26 + k * 19, t, 12, (55, 65, 81))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    img.resize((W, H), Image.LANCZOS).save(args.out)
    print("wrote", args.out, f"{args.out.stat().st_size/1024:.0f} KiB")
    for name, v in metrics:
        print(f"  {name} geo-ratio: {v:.3f}")


if __name__ == "__main__":
    main()
