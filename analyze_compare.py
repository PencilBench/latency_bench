"""两次运行之间的解码延迟对比分析（典型用法：Windows d3d11va vs macOS videotoolbox）。

只读取两份已完成的运行结果，不重新解码。

用法::

    python analyze_compare.py --a <run_dir> --b <run_dir> \
        [--label-a AppleM6 --label-b ArcB390]

每轮的逐帧 CSV 优先按 ``summary.json`` 里的 ``round.frames_csv`` 解析（新布局
``<clip>__<hw>.r<N>.frames.csv``）；旧布局没有 ``round`` 字段时回落到
``<clip>__<hw>.frames.csv``，因此新老结果目录都能直接分析。若某片段的 summary
与它引用的 CSV 不是同一轮，第 0 节会显式标出。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

#: 由 configure() 填充的模块级状态（A = 基准侧，B = 对照侧）
RUNS: dict[str, tuple[Path, str]] = {}
SHORT: dict[str, str] = {}
CLIPS: list[str] = []
ORDER: list[str] = []
LABELS: dict[str, str] = {}
_PAYLOADS: dict[str, dict] = {}
_FRAME_PATHS: dict[tuple[str, str], Path] = {}
_TICK_PATHS: dict[tuple[str, str], Path] = {}


def q(s, p):
    return float(np.percentile(s, p))


# ---------------------------------------------------------------------------
# 结果目录解析
# ---------------------------------------------------------------------------
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
    """「AV1 4K 112M/60fps」这类简短标签，用于表格与图例。"""
    src = clip.get("source") or {}
    codec = _codec_of(str(clip.get("clip") or ""))
    res = _resolution_of(src.get("width"))
    rate = src.get("bit_rate")
    mbps = f"{rate / 1e6:.0f}M" if isinstance(rate, (int, float)) and rate else "?"
    fps = src.get("avg_frame_rate")
    fps_txt = f"/{fps:.0f}fps" if isinstance(fps, (int, float)) and fps else ""
    return f"{codec} {res} {mbps}{fps_txt}"


def _resolve(run_dir: Path, rec: dict, hw: str | None, kind: str) -> Path:
    """按 ``round.<kind>_csv`` → 旧命名 的顺序定位某个片段的 CSV。"""
    key = "frames_csv" if kind == "frames" else "ticks_csv"
    name = (rec.get("round") or {}).get(key)
    if name and (run_dir / name).exists():
        return run_dir / name
    stem = str(rec["clip"]).replace(".mp4", "")
    cands = sorted(run_dir.glob(f"{stem}__{hw}*.{kind}.csv"))
    if not cands:
        raise SystemExit(f"找不到 {stem} 的 {kind}.csv（目录：{run_dir}）")
    if len(cands) > 1:
        print(f"提示：{stem} 有 {len(cands)} 份 {kind}.csv，取最后一个 {cands[-1].name}")
    return cands[-1]


def configure(a: Path, b: Path, label_a: str | None, label_b: str | None) -> None:
    global CLIPS, ORDER, SHORT, RUNS, LABELS, _PAYLOADS, _FRAME_PATHS, _TICK_PATHS
    dirs = {"A": a, "B": b}
    _PAYLOADS = {k: json.loads((d / "summary.json").read_text(encoding="utf-8"))
                 for k, d in dirs.items()}
    LABELS = {
        "A": label_a or str(_PAYLOADS["A"].get("run_id") or a.name),
        "B": label_b or str(_PAYLOADS["B"].get("run_id") or b.name),
    }
    recs = {k: {c["clip"]: c for c in _PAYLOADS[k]["clips"]} for k in dirs}
    common = [c for c in recs["A"] if c in recs["B"]]
    if not common:
        raise SystemExit("两份 summary.json 没有共同片段，无法对比")
    common.sort(key=lambda c: (
        _codec_of(c),
        (recs["A"][c].get("source") or {}).get("height") or 0,
        (recs["A"][c].get("source") or {}).get("bit_rate") or 0,
    ))
    CLIPS = common
    ORDER = list(common)
    SHORT = {c: clip_label(recs["A"][c]) for c in common}
    RUNS = {
        k: (dirs[k], recs[k][common[0]].get("hwaccel")
            or _PAYLOADS[k].get("hwaccel") or "")
        for k in dirs
    }
    _FRAME_PATHS = {(k, c): _resolve(dirs[k], recs[k][c], RUNS[k][1], "frames")
                    for k in dirs for c in common}
    _TICK_PATHS = {(k, c): _resolve(dirs[k], recs[k][c], RUNS[k][1], "ticks")
                   for k in dirs for c in common}


def load(run: str, clip: str) -> pd.DataFrame:
    return pd.read_csv(_FRAME_PATHS[(run, clip)])


def load_ticks(run: str, clip: str) -> pd.DataFrame:
    return pd.read_csv(_TICK_PATHS[(run, clip)])


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------
def summarize(df: pd.DataFrame, skip: int = 1) -> dict:
    d = df.iloc[skip:]
    x = d["decode_ms_per_frame"]
    dem = d["demux_ms"]
    med = float(np.median(x))
    trim = float(x[(x >= q(x, 2)) & (x <= q(x, 98))].mean())
    return {
        "n": int(len(x)),
        "mean": float(x.mean()),
        "trim_mean": trim,
        "p50": med,
        "p90": q(x, 90),
        "p95": q(x, 95),
        "p99": q(x, 99),
        "p999": q(x, 99.9),
        "max": float(x.max()),
        "min": float(x.min()),
        "stdev": float(x.std(ddof=1)),
        "mad": float(np.median(np.abs(x - med))),
        "cv": float(x.std(ddof=1) / x.mean()),
        "skew": float(x.skew()),
        "jitter": float(np.median(np.abs(np.diff(x)))),
        "frac_over_5": float((x > 5).mean()),
        "outlier_3mad": float((x > med + 3 * np.median(np.abs(x - med))).mean()),
        "demux_med": float(dem.median()),
        "demux_p99": q(dem, 99),
    }


def periodicity(x: np.ndarray) -> tuple[int, float, float]:
    """用自相关找主周期。返回 (lag, 归一化自相关值, 峰值占比)。"""
    v = x - x.mean()
    if v.std() == 0:
        return 0, 0.0, 0.0
    ac = np.correlate(v, v, mode="full")[len(v) - 1:]
    ac = ac / ac[0]
    lo, hi = 3, min(len(ac) - 1, 200)
    if hi <= lo:
        return 0, 0.0, 0.0
    seg = ac[lo:hi]
    lag = int(np.argmax(seg)) + lo
    return lag, float(ac[lag]), float(ac[lag])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="两次运行的解码延迟对比分析")
    p.add_argument("--a", required=True, type=Path, help="A 侧结果目录（含 summary.json）")
    p.add_argument("--b", required=True, type=Path, help="B 侧结果目录（含 summary.json）")
    p.add_argument("--label-a", default=None, help="A 侧显示名（默认取 run_id）")
    p.add_argument("--label-b", default=None, help="B 侧显示名（默认取 run_id）")
    return p


def main() -> None:
    args = build_parser().parse_args()
    configure(args.a, args.b, args.label_a, args.label_b)
    la, lb = LABELS["A"], LABELS["B"]

    pd.set_option("display.width", 260)
    pd.set_option("display.max_columns", 60)

    raw, summ = {}, {}
    for clip in CLIPS:
        for run in RUNS:
            raw[(run, clip)] = load(run, clip)
            summ[(run, clip)] = summarize(raw[(run, clip)])

    t = pd.DataFrame([{**v, "run": k[0], "clip": SHORT[k[1]]} for k, v in summ.items()])

    print("=" * 140)
    print(f"0. 数据一致性：summary.json 与它引用的 frames.csv —— 应全部一致")
    print(f"   A = {la}   B = {lb}")
    print("=" * 140)
    for run in RUNS:
        for c in _PAYLOADS[run]["clips"]:
            if not c.get("decode"):
                continue
            rec = summ[(run, c["clip"])]
            dm, dp99, dmax = c["decode"]["mean_ms"], c["decode"]["p99_ms"], c["decode"]["max_ms"]
            tag = (c.get("round") or {}).get("file_tag") or "legacy"
            flag = "  <-- 不一致" if abs(dmax - rec["max"]) > 0.001 else ""
            print(f"{run:2s} {c['clip']:24s} [{tag:>6s}] summary(mean/p99/max)="
                  f"{dm:7.3f}/{dp99:7.3f}/{dmax:7.3f}   csv="
                  f"{rec['mean']:7.3f}/{rec['p99']:7.3f}/{rec['max']:7.3f}{flag}")

    print()
    print("=" * 140)
    print("1. 逐片段统计（ms，按工具口径排除第 0 帧；数据源 = summary 引用的那一轮）")
    print("=" * 140)
    cols = ["clip", "run", "n", "mean", "trim_mean", "p50", "p90", "p95", "p99", "p999",
            "max", "stdev", "cv", "jitter", "frac_over_5", "outlier_3mad", "demux_med"]
    out = t[cols].copy()
    out["clip"] = pd.Categorical(out["clip"], categories=[SHORT[c] for c in ORDER], ordered=True)
    print(out.sort_values(["clip", "run"]).to_string(index=False, float_format=lambda v: f"{v:.3f}"))

    print()
    print("=" * 140)
    print(f"2. 跨侧倍数：B / A = {lb} / {la}（<1 => {lb} 更快）")
    print("=" * 140)
    piv = t.pivot(index="clip", columns="run", values=["mean", "trim_mean", "p50", "p95", "p99", "max", "jitter"])
    rows = []
    for clip in [SHORT[c] for c in ORDER]:
        r = {"clip": clip}
        for m in ["mean", "trim_mean", "p50", "p95", "p99", "max", "jitter"]:
            a, b = piv[(m, "A")].get(clip), piv[(m, "B")].get(clip)
            r[m] = b / a
        rows.append(r)
    rt = pd.DataFrame(rows)
    print(rt.to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    print("\n几何均值：", {m: round(float(np.exp(np.log(rt[m]).mean())), 3) for m in ["mean", "trim_mean", "p50", "p95", "p99"]})

    print()
    print("=" * 140)
    print("3. 周期性检验：自相关主周期 与 >p50+3*MAD 的慢帧分布")
    print("=" * 140)
    for clip in ORDER:
        for run in RUNS:
            x = raw[(run, clip)]["decode_ms_per_frame"].to_numpy()[1:]
            lag, acv, _ = periodicity(x)
            med = np.median(x)
            mad = np.median(np.abs(x - med))
            sl = np.where(x > med + 3 * mad)[0]
            gaps = np.diff(sl) if len(sl) > 1 else np.array([])
            gmed = float(np.median(gaps)) if len(gaps) else float("nan")
            print(f"{SHORT[clip]:22s} {run:2s} n={len(x):5d} 自相关峰值lag={lag:4d} r={acv:+.3f}  "
                  f"慢帧数={len(sl):4d} 相邻间隔中位数={gmed:6.1f} 最大={x.max():7.3f}")

    print()
    print("=" * 140)
    print(f"4. B 侧（{lb}）>10 ms 孤立尖峰的上下文（demux_ms vs decode）")
    print("=" * 140)
    for clip in ORDER:
        df = raw[("B", clip)]
        x = df["decode_ms_per_frame"]
        big = x[x > 10]
        if len(big) == 0:
            print(f"\n{SHORT[clip]}: 无 >10 ms 尖峰（max={x.iloc[1:].max():.3f} ms）")
            continue
        for i in big.index:
            print(f"\n{SHORT[clip]} 慢帧 index={i} ({x[i]:.3f} ms)")
            print(df.loc[max(0, i - 2):i + 2, ["index", "demux_ms", "decode_ms_per_frame", "frames_out"]]
                  .to_string(index=False, float_format=lambda v: f"{v:.3f}"))

    print()
    print("=" * 140)
    print("5. ticker 健康度（ticks.csv）")
    print("=" * 140)
    trows = []
    for clip in ORDER:
        for run in RUNS:
            tk = load_ticks(run, clip)
            iv = tk["t_ms"].diff().dropna()
            dr = tk["tick_drift_ms"]
            trows.append({
                "clip": SHORT[clip], "run": run, "ticks": len(tk),
                "interval_med": float(iv.median()), "interval_max": float(iv.max()),
                "drift_max": float(dr.max()), "drift_absmax": float(dr.abs().max()),
                "late_gt1ms": float((dr > 1.0).mean()),
            })
    tt = pd.DataFrame(trows)
    tt["clip"] = pd.Categorical(tt["clip"], categories=[SHORT[c] for c in ORDER], ordered=True)
    print(tt.sort_values(["clip", "run"]).to_string(index=False, float_format=lambda v: f"{v:.3f}"))

    print()
    print("=" * 140)
    print("6. 有效吞吐（按素材比特率折算）")
    print("=" * 140)
    for run in RUNS:
        print(f"-- {run} = {LABELS[run]} --")
        for c in _PAYLOADS[run]["clips"]:
            src = c.get("source") or {}
            br = src.get("bit_rate")
            if not br or not c.get("wall_s"):
                continue
            print(f"   {c['clip']:24s} 比特率={br/1e6:7.1f} Mbps  墙钟={c['wall_s']:6.3f}s  "
                  f"吞吐={c['throughput_fps']:7.1f} fps  {br*8/1e6/c['wall_s']:8.1f} Mbps "
                  f"实时倍率={c['realtime_factor']:.2f}")


if __name__ == "__main__":
    main()
