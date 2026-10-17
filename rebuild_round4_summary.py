"""按 CSV 重算某个结果目录的 summary，使其与 `frames.csv` / `ticks.csv` 同轮。

背景
----
旧版 `bench.run_clip()` 每轮都用 `path.open("w")` 截断重写
`<clip>__<hw>.frames.csv` / `.ticks.csv`，所以盘上的 CSV 是**最后一轮**；而
`cli.run()` 用 `_better()` 在计入轮里取 p99 更小的一轮写进 summary。两者因此
经常不是同一轮（16 个「片段×平台」组合里有 10 个对不上）。

本脚本从 CSV（含逐帧与逐 tick）重算 summary，直接复用工具自己的
`FrameSample` / `TickRow` / `summarize_ticks` / `build_clip_summary` /
`render_markdown`，保证统计口径与工具逐位一致。两种用途：

* **修正旧结果**：把「第 3/4 轮取优」的 summary 重基到盘上 CSV 那一轮；
* **校验新结果**：新布局已按轮命名 CSV，summary 里记了 `round.frames_csv`，
  此时重算结果应与原文件完全一致（`--check` 会全部显示「同轮」）。

用法
----
    python rebuild_round4_summary.py --check --run results/<run_id> [--run ...]
    python rebuild_round4_summary.py --write --run results/<run_id> [--run ...]

需要能 import 本工具（会连带 import PyAV），因此用本仓库的虚拟环境跑：
``./.venv/bin/python rebuild_round4_summary.py ...``。

CSV 定位顺序：`round.frames_csv` / `round.ticks_csv` → 旧命名
`<clip>__<hw>.frames.csv`。因此新老结果目录都能直接处理。
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "src"))

from latency_bench import reporting as R  # noqa: E402
from latency_bench.accel import DecodePlan, PlanAttempt  # noqa: E402

#: 写盘前把原文件备份到这里（相对每个结果目录）。
#: 早期手工处理 M6/B390 两份结果时用的是 `_bestof_r3r4_backup`，已存在则不重复备份。
BACKUP_DIRNAME = "_summary_backup"
LEGACY_BACKUP_DIRNAME = "_bestof_r3r4_backup"

# summary.json 里由「轮次」决定、需要重算的字段
ROUND_FIELDS = ("wall_s", "throughput_fps", "realtime_factor", "decode", "decode_per_frame_ms",
                "demux", "slow_frames", "ticks", "rounds", "round")


def read_frames(path: Path) -> list[R.FrameSample]:
    out: list[R.FrameSample] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            pts = row["pts"]
            pts_ms = row["pts_ms"]
            out.append(
                R.FrameSample(
                    index=int(row["index"]),
                    packet_index=int(row["packet_index"]),
                    pts=int(pts) if pts not in ("", "None") else None,
                    pts_ms=float(pts_ms) if pts_ms not in ("", "None") else None,
                    demux_ns=int(round(float(row["demux_ms"]) * 1e6)),
                    decode_ns=int(round(float(row["decode_ms"]) * 1e6)),
                    frames_out=int(row["frames_out"]),
                    keyframe=bool(int(row["keyframe"] or 0)),
                    pict_type=row["pict_type"] or None,
                    fmt=row["format"] or None,
                    tail=bool(int(row["tail"] or 0)),
                )
            )
    return out


def read_ticks(path: Path) -> list[R.TickRow]:
    out: list[R.TickRow] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            out.append(
                R.TickRow(
                    tick_index=int(row["tick_index"]),
                    t_ms=float(row["t_ms"]),
                    t_wall_iso=row["t_wall_iso"],
                    frames_in_tick=int(row["frames_in_tick"]),
                    frames_total=int(row["frames_total"]),
                    packets_in_tick=int(row["packets_in_tick"]),
                    decode_ms_last=float(row["decode_ms_last"]),
                    decode_ms_min=float(row["decode_ms_min"]),
                    decode_ms_max=float(row["decode_ms_max"]),
                    decode_ms_avg=float(row["decode_ms_avg"]),
                    decode_ms_p50=float(row["decode_ms_p50"]),
                    decode_ms_p95=float(row["decode_ms_p95"]),
                    fps_inst=float(row["fps_inst"]),
                    demux_ms_avg=float(row["demux_ms_avg"]),
                    wall_ms=float(row["wall_ms"]),
                    tick_drift_ms=float(row["tick_drift_ms"]),
                )
            )
    return out


def resolve_csvs(d: Path, orig: dict, hw: str | None) -> tuple[Path, Path]:
    """定位某个片段的 frames.csv / ticks.csv：优先 summary 自报的轮次文件，其次旧命名。"""
    stem = orig["clip"].replace(".mp4", "")
    rnd = orig.get("round") or {}
    out: list[Path] = []
    for key, kind in (("frames_csv", "frames"), ("ticks_csv", "ticks")):
        name = rnd.get(key)
        if name and (d / name).exists():
            out.append(d / name)
            continue
        cands = sorted(d.glob(f"{stem}__{hw}*.{kind}.csv")) if hw else []
        cands = cands or sorted(d.glob(f"{stem}__*.{kind}.csv"))
        if not cands:
            raise SystemExit(f"找不到 {stem} 的 {kind}.csv（目录：{d}）")
        out.append(cands[-1])
    return out[0], out[1]


def rebuild_clip(orig: dict, d: Path, hw: str | None) -> dict:
    frames_csv, ticks_csv = resolve_csvs(d, orig, hw)
    frames = read_frames(frames_csv)
    tick_rows = read_ticks(ticks_csv)

    interval = (orig.get("ticks") or {}).get("interval_ms") or 33.0

    # wall_s 无法从 CSV 精确恢复（CSV 不存墙钟），用「全部帧的 demux+decode 之和」估计，
    # 再用 ticker 的物理约束收边界：ticker 在最后一次 tick 之后、下一次 tick 之前被停止，
    # 因此 wall_ms ∈ [last_tick.t_ms, last_tick.t_ms + interval)。
    # 在「最优轮 == 第 4 轮」的 6 个组合上，该估计与原 wall_s 的偏差 < 0.5%。
    est_ms = (sum(s.demux_ms for s in frames) + sum(s.decode_ms for s in frames))
    last_ms = tick_rows[-1].t_ms if tick_rows else est_ms
    est_ms = min(max(est_ms, last_ms), last_ms + interval - 1e-6)
    wall_s = est_ms / 1000.0

    ticks = R.summarize_ticks(tick_rows, interval, wall_s * 1000.0)

    src = orig["source"]
    clip = {
        "name": orig["clip"],
        "path": orig["path"],
        "is_vfr": src.get("is_vfr", False),
        **{k: src.get(k) for k in ("codec", "width", "height", "pix_fmt", "profile",
                                   "level", "duration_s", "bit_rate", "avg_frame_rate")},
    }
    plan = DecodePlan(
        hwaccel=orig["hwaccel"],
        decoder=orig["decoder"],
        force_hardware=True,
        allow_software_fallback=bool(orig.get("allow_software_fallback")),
    )
    attempts = [PlanAttempt(**a) for a in (orig.get("attempts") or [])]

    out = R.build_clip_summary(
        clip=clip,
        plan=plan,
        attempts=attempts,
        samples=frames,
        frames_total=orig["frame_count"],
        packets_total=orig["packets_total"],
        wall_s=wall_s,
        hardware_confirmed=orig["flags"]["hardware_decode_confirmed"],
        readback_detected=orig["flags"]["readback_detected"],
        hw_pix_fmts=orig["hw_pix_fmts"],
        ticks=ticks,
        keyframe_count=orig["keyframe_count"],
        tail_frames=orig["tail_frames"],
        stop_reason=orig["stop_reason"],
        expected_frames=orig["expected_frames"],
        backend=orig["backend"],
        skip_first_frames=orig["skipped_first_frames"],
        round_info=orig.get("round"),
    )
    # build_clip_summary 不产出这两个字段：`sample_trimmed` 由 bench.py 收尾时补上且与
    # 轮次无关；`rounds[]` 是 cli.py 选出最优轮后追加的审计表，重算时原样保留。
    out["sample_trimmed"] = orig.get("sample_trimmed", 0)
    if "rounds" in orig:
        out["rounds"] = orig["rounds"]
    return out


def diff_report(run: str, orig: dict, new: dict) -> tuple[bool, str]:
    """比较非轮次字段是否完全一致，并报告轮次字段的变化幅度。"""
    bad = []
    for k in orig:
        if k in ROUND_FIELDS:
            continue
        if orig[k] != new.get(k):
            bad.append(k)
    same_round = abs(orig["decode"]["max_ms"] - new["decode"]["max_ms"]) < 1e-9
    dm = (new["decode"]["mean_ms"] / orig["decode"]["mean_ms"] - 1) * 100
    dp = (new["decode"]["p99_ms"] / orig["decode"]["p99_ms"] - 1) * 100
    dmax = (new["decode"]["max_ms"] - orig["decode"]["max_ms"])
    tag = "同轮" if same_round else "跨轮"
    flag = "" if not bad else f"  ⚠ 非轮次字段不一致: {bad}"
    return not bad, (f"{run:5s} {orig['clip']:24s} [{tag}] "
                     f"mean {dm:+6.2f}%  p99 {dp:+6.2f}%  max {dmax:+8.3f} ms"
                     f"  ticks {orig['ticks']['count']}→{new['ticks']['count']}{flag}")


def main() -> int:
    ap = argparse.ArgumentParser(description="按 CSV 重算某个结果目录的 summary")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="只校验，不写盘")
    g.add_argument("--write", action="store_true", help="备份原文件后写盘")
    ap.add_argument("--run", action="append", required=True, type=Path, metavar="DIR",
                    help="结果目录（含 summary.json），可重复指定")
    args = ap.parse_args()

    ok_all = True
    for d in args.run:
        if not (d / "summary.json").exists():
            raise SystemExit(f"{d} 下没有 summary.json")
        top = json.loads((d / "summary.json").read_text(encoding="utf-8"))
        top_hw = top.get("hwaccel")
        print(f"\n=== {d} ===")
        new_clips = []
        rebased = False
        for orig in top["clips"]:
            hw = orig.get("hwaccel") or top_hw
            new = rebuild_clip(orig, d, hw)
            new_clips.append(new)
            ok, line = diff_report(str(top.get("run_id") or d.name)[:10], orig, new)
            ok_all &= ok
            print("  " + line)
            rebased = rebased or not (orig.get("round") or {}).get("file_tag")
            # ticks 自检：期望 tick 数应等于实际 tick 数
            t = new["ticks"]
            if t["expected_count"] != t["count"]:
                print(f"      ⚠ tick 自检不符：expected={t['expected_count']} actual={t['count']}")
                ok_all = False
        if not rebased:
            print("  （该目录的 summary 已与 round.frames_csv 同轮，重算结果应与原文件一致）")

        if not args.write:
            continue

        # 备份原文件（老目录已备份到 LEGACY_BACKUP_DIRNAME 的，不重复备份）
        legacy = d / LEGACY_BACKUP_DIRNAME
        bdir = d / BACKUP_DIRNAME
        if not (legacy.exists() and not bdir.exists()):
            bdir.mkdir(exist_ok=True)
            for name in ("summary.json", "summary.md"):
                if (d / name).exists() and not (bdir / name).exists():
                    shutil.copy2(d / name, bdir / name)
            for orig in top["clips"]:
                stem = orig["clip"].replace(".mp4", "")
                for hw in {orig.get("hwaccel") or top_hw}:
                    name = f"{stem}__{hw}.summary.json"
                    if (d / name).exists() and not (bdir / name).exists():
                        shutil.copy2(d / name, bdir / name)

        payload = {
            "run_id": top["run_id"],
            "started_at": top["started_at"],
            "command": top["command"],
            "environment": top["environment"],
            "clips": new_clips,
        }
        if rebased:
            payload["summary_rebase"] = {
                "policy": "csv-on-disk",
                "note": (
                    "该目录是旧布局（各轮共用同一个 CSV 文件名，盘上只剩最后一轮），"
                    "summary.json / summary.md / <clip>.summary.json 已按盘上 CSV 重算，"
                    "使其与 frames.csv / ticks.csv 同轮。原文件备份在 "
                    f"{LEGACY_BACKUP_DIRNAME if (d / LEGACY_BACKUP_DIRNAME).exists() else BACKUP_DIRNAME}/。"
                ),
            }
        for c in new_clips:
            stem = c["clip"].replace(".mp4", "")
            hw = c.get("hwaccel") or top_hw
            (d / f"{stem}__{hw}.summary.json").write_text(R._dumps(c), encoding="utf-8")
        (d / "summary.json").write_text(R._dumps(payload), encoding="utf-8")
        md = R.render_markdown(payload)
        if rebased and "- 统计口径：" not in md:
            md = md.replace(
                "- 可用硬件设备类型：",
                "- 统计口径：按盘上 frames.csv / ticks.csv 重算（旧布局各轮共用文件名，"
                "盘上只剩最后一轮）\n- 可用硬件设备类型：",
                1,
            )
        (d / "summary.md").write_text(md, encoding="utf-8")
        where = LEGACY_BACKUP_DIRNAME if (d / LEGACY_BACKUP_DIRNAME).exists() else BACKUP_DIRNAME
        print(f"  → 已写入；原文件备份在 {where}/")

    print("\n校验结果：" + ("全部通过" if ok_all else "存在不一致，见上方 ⚠"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
