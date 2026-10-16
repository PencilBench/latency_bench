"""33 ms 输出：tick 线程、逐帧 CSV、按 tick 统计 CSV、汇总 JSON / Markdown。

线程模型（PLAN.md §5）：

* 主解码线程只做计时，把样本按批推给 :class:`SampleSink`；
* ticker 线程用 ``perf_counter`` 对齐 + 漂移补偿，每 ``tick_ms``
  写一行 ``ticks.csv`` 并 ``flush()`` 逐帧 CSV，保证盘上每 33 ms 有新鲜数据。
"""

from __future__ import annotations

import csv
import json
import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, TextIO

from . import stats
from .accel import DecodePlan, PlanAttempt, format_attempts

__all__ = [
    "FrameSample",
    "TickRow",
    "SampleSink",
    "Ticker",
    "RunWriter",
    "TICK_COLUMNS",
    "FRAME_COLUMNS",
    "summarize_ticks",
    "build_clip_summary",
    "render_markdown",
    "render_failure",
]

FRAME_COLUMNS = [
    "index",
    "packet_index",
    "pts",
    "pts_ms",
    "demux_ms",
    "decode_ms",
    "frames_out",
    "decode_ms_per_frame",
    "keyframe",
    "pict_type",
    "format",
    "tail",
]

TICK_COLUMNS = [
    "tick_index",
    "t_ms",
    "t_wall_iso",
    "frames_in_tick",
    "frames_total",
    "packets_in_tick",
    "decode_ms_last",
    "decode_ms_min",
    "decode_ms_max",
    "decode_ms_avg",
    "decode_ms_p50",
    "decode_ms_p95",
    "fps_inst",
    "demux_ms_avg",
    "wall_ms",
    "tick_drift_ms",
]


@dataclass(slots=True)
class FrameSample:
    """一个解出的帧（同一 packet 可能连出多帧）。"""

    index: int
    packet_index: int
    pts: int | None
    pts_ms: float | None
    demux_ns: int
    decode_ns: int
    frames_out: int
    keyframe: bool
    pict_type: str | None
    fmt: str | None
    tail: bool

    @property
    def demux_ms(self) -> float:
        return self.demux_ns / 1e6

    @property
    def decode_ms(self) -> float:
        return self.decode_ns / 1e6

    @property
    def decode_ms_per_frame(self) -> float:
        return self.decode_ns / 1e6 / max(1, self.frames_out)

    def as_row(self) -> list[Any]:
        return [
            self.index,
            self.packet_index,
            self.pts,
            self.pts_ms,
            self.demux_ms,
            self.decode_ms,
            self.frames_out,
            self.decode_ms_per_frame,
            int(self.keyframe),
            self.pict_type,
            self.fmt,
            int(self.tail),
        ]


@dataclass
class TickRow:
    tick_index: int
    t_ms: float
    t_wall_iso: str
    frames_in_tick: int
    frames_total: int
    packets_in_tick: int
    decode_ms_last: float
    decode_ms_min: float
    decode_ms_max: float
    decode_ms_avg: float
    decode_ms_p50: float
    decode_ms_p95: float
    fps_inst: float
    demux_ms_avg: float
    wall_ms: float
    tick_drift_ms: float

    def as_row(self) -> list[Any]:
        return [getattr(self, name) for name in TICK_COLUMNS]


class SampleSink:
    """主解码线程与 ticker 线程之间的共享缓冲。

    逐帧 CSV 的写入放在 ticker 线程里，主线程只做 ``extend()``，
    避免磁盘 I/O 混进解码计时。
    """

    #: tick 窗口超过该长度时裁剪，避免长时间运行内存无界增长。
    MAX_WINDOW = 65536

    def __init__(self, tick_interval_s: float = 0.033) -> None:
        self._lock = threading.Lock()
        self._window: list[FrameSample] = []
        self._closing = False
        self.tick_interval_s = tick_interval_s
        self.samples: list[FrameSample] = []
        self.packets = 0
        self.trimmed = 0
        self._base = 0
        """``_window`` 里第一个样本的全局序号（裁剪后不为 0）。"""

    # -- 主线程 ----------------------------------------------------------------
    def extend(self, samples: list[FrameSample]) -> None:
        if not samples:
            return
        with self._lock:
            self._window.extend(samples)
            self.samples.extend(samples)
            overflow = len(self._window) - self.MAX_WINDOW
            if overflow > 0:
                del self._window[:overflow]
                self._base += overflow
                self.trimmed += overflow

    def count_packet(self) -> None:
        with self._lock:
            self.packets += 1

    def request_close(self) -> None:
        with self._lock:
            self._closing = True

    @property
    def closing(self) -> bool:
        with self._lock:
            return self._closing

    # -- ticker 线程 -----------------------------------------------------------
    def since(self, cursor: int) -> tuple[list[FrameSample], int]:
        """取出全局游标 ``cursor`` 之后的样本，并推进到最新。

        返回值里的新游标是**全局序号**，因此窗口被裁剪后语义仍然正确。
        """
        with self._lock:
            if cursor < self._base:
                cursor = self._base
            start = cursor - self._base
            return list(self._window[start:]), self._base + len(self._window)


def next_tick_deadline(next_at: float, now: float, interval_s: float) -> float:
    """推进下一个 tick 的绝对截止时刻（漂移补偿）。

    每次都从**上一个截止时刻**加上一个周期，而不是从「当前时刻」重新起算，
    因此单次迟到不会把后续所有 tick 一起推迟；迟到超过一个周期时跳过被错过的 tick。
    """
    candidate = next_at + interval_s
    while candidate <= now:
        candidate += interval_s
    return candidate


class Ticker:
    """对齐 ``perf_counter`` 的周期线程（带漂移补偿）。

    等待策略：先 ``time.sleep`` 到距截止时刻 ``spin_margin_s``，再用忙等贴住截止时刻。
    在本机实测中 ``Event.wait(0.033)`` 的唤醒延迟约 14 ms（Windows），
    ``time.sleep`` 只有约 0.5 ms —— 前者会把 33 ms 的 tick 拖成 47 ms。
    """

    def __init__(
        self,
        interval_s: float,
        on_tick: Any,
        *,
        fsync: bool = False,
        flush: bool = True,
        spin_margin_s: float = 0.002,
    ) -> None:
        self.interval_s = interval_s
        self.on_tick = on_tick
        self.fsync = fsync
        self.flush = flush
        self.spin_margin_s = spin_margin_s
        self.tick_index = 0
        self.drift_ms = 0.0
        self.late_ticks = 0
        self.actual_intervals_ms: list[float] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.errors: list[str] = []
        """回调抛出的异常。ticker 是后台线程，异常必须先记下来再由主线程重抛。"""

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="ticker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        if self.errors:
            raise RuntimeError(
                "tick 回调抛出异常（后台线程异常，已在此重抛）：\n  " + "\n  ".join(self.errors)
            )

    def _run(self) -> None:
        start = time.perf_counter()
        next_at = start + self.interval_s
        last = start
        while not self._stop.is_set():
            now = time.perf_counter()
            remaining = next_at - now
            if remaining > self.spin_margin_s:
                time.sleep(remaining - self.spin_margin_s)
            while not self._stop.is_set() and time.perf_counter() < next_at:
                pass
            if self._stop.is_set():
                break
            now = time.perf_counter()
            self.drift_ms = (now - next_at) * 1e3
            if self.drift_ms > 1.0:
                self.late_ticks += 1
            self.actual_intervals_ms.append((now - last) * 1e3)
            last = now
            try:
                self.on_tick(self, now - start)
            except Exception as exc:  # noqa: BLE001 - 后台线程异常必须带回去重抛
                import traceback

                self.errors.append(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
                self._stop.set()
                break
            finally:
                self.tick_index += 1
            # 漂移补偿：按绝对时间轴推进，而不是每次在「当前时刻」上加 interval。
            next_at = next_tick_deadline(next_at, now, self.interval_s)

    @property
    def median_interval_ms(self) -> float:
        values = sorted(self.actual_intervals_ms)
        if not values:
            return float("nan")
        return stats.percentile(values, 50.0)


@dataclass
class RunWriter:
    """``results/<run_id>/`` 的落地写入器。"""

    out_dir: Path
    run_id: str
    command: str
    environment: dict[str, Any]
    clips: list[dict[str, Any]] = field(default_factory=list)
    started_at: str = ""

    def __post_init__(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)

    # -- 单个片段 --------------------------------------------------------------
    def clip_frame_writer(self, clip_stem: str, hwaccel: str) -> tuple[TextIO, Any]:
        path = self.out_dir / f"{clip_stem}__{hwaccel}.frames.csv"
        fh = path.open("w", newline="", encoding="utf-8")
        writer = csv.writer(fh)
        writer.writerow(FRAME_COLUMNS)
        return fh, writer

    def clip_tick_writer(self, clip_stem: str, hwaccel: str) -> tuple[TextIO, Any]:
        path = self.out_dir / f"{clip_stem}__{hwaccel}.ticks.csv"
        fh = path.open("w", newline="", encoding="utf-8")
        writer = csv.writer(fh)
        writer.writerow(TICK_COLUMNS)
        return fh, writer

    def write_clip_summary(self, clip_stem: str, hwaccel: str, payload: dict[str, Any]) -> Path:
        path = self.out_dir / f"{clip_stem}__{hwaccel}.summary.json"
        path.write_text(_dumps(payload), encoding="utf-8")
        self.clips.append(payload)
        return path

    def write_inventory(self, payload: dict[str, Any]) -> Path:
        path = self.out_dir / "inventory.json"
        path.write_text(_dumps(payload), encoding="utf-8")
        return path

    # -- 整轮 ------------------------------------------------------------------
    def write_summary(self, extra: dict[str, Any] | None = None) -> tuple[Path, Path]:
        payload: dict[str, Any] = {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "command": self.command,
            "environment": self.environment,
            "clips": self.clips,
        }
        if extra:
            payload.update(extra)
        json_path = self.out_dir / "summary.json"
        json_path.write_text(_dumps(payload), encoding="utf-8")

        md_path = self.out_dir / "summary.md"
        md_path.write_text(render_markdown(payload), encoding="utf-8")
        return json_path, md_path


def _sanitize(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value]
    return value


def _dumps(payload: Any) -> str:
    """``json.dumps`` 但把 NaN/Infinity 写成 ``null``（合法 JSON）。"""
    return json.dumps(_sanitize(payload), indent=2, ensure_ascii=False, default=str)


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        if not math.isfinite(value):
            return "—"
        return f"{value:.{digits}f}"
    return str(value)


def render_markdown(payload: dict[str, Any]) -> str:
    """把汇总渲染成可贴进 issue / 文档的对比表。"""
    lines: list[str] = []
    lines.append(f"# 解码延迟测量 — `{payload.get('run_id', '')}`")
    lines.append("")
    lines.append(f"- 开始时间：{payload.get('started_at') or '—'}")
    lines.append(f"- 命令：`{payload.get('command', '')}`")
    env = payload.get("environment", {})
    lines.append(f"- 平台：{env.get('platform', '—')}（{env.get('machine', '—')}）")
    lines.append(f"- Python {env.get('python', '—')} / PyAV {env.get('pyav', '—')}")
    lines.append(f"- 可用硬件设备类型：{', '.join(env.get('hwdevices_available', []) or []) or '—'}")
    lines.append("")

    clips = payload.get("clips") or []
    if clips:
        lines.append("## 逐片段对比")
        lines.append("")
        lines.append(
            "| 片段 | Codec | 分辨率 | Mbps | hwaccel | 解码器 | 硬解 | 回读 | "
            "帧数 | 均值 ms | p50 ms | p95 ms | p99 ms | max ms | 抖动 ms | 实时倍率 |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for c in clips:
            if c.get("error"):
                lines.append(
                    f"| {c.get('clip')} | — | — | — | {c.get('hwaccel', '—')} | — | **失败** |"
                    " — | — | — | — | — | — | — | — | — |"
                )
                continue
            dec = c.get("decode", {})
            src = c.get("source", {})
            flags = c.get("flags", {})
            raw_rate = src.get("bit_rate")
            mbps = (raw_rate / 1e6) if isinstance(raw_rate, (int, float)) and raw_rate else None
            lines.append(
                "| {clip} | {codec} | {w}x{h} | {mbps} | {hw} | {decoder} | {hw_ok} | {readback} | "
                "{frames} | {mean} | {p50} | {p95} | {p99} | {mx} | {jit} | {rt} |".format(
                    clip=c.get("clip", "—"),
                    codec=src.get("codec", "—"),
                    w=src.get("width", "—"),
                    h=src.get("height", "—"),
                    mbps=_fmt(mbps, 1),
                    hw=c.get("hwaccel", "—"),
                    decoder=c.get("decoder") or "container-default",
                    hw_ok="是" if flags.get("hardware_decode_confirmed") else "**否**",
                    readback="**是**" if flags.get("readback_detected") else "否",
                    frames=c.get("frame_count", "—"),
                    mean=_fmt(dec.get("mean_ms"), 3),
                    p50=_fmt(dec.get("median_ms"), 3),
                    p95=_fmt(dec.get("p95_ms"), 3),
                    p99=_fmt(dec.get("p99_ms"), 3),
                    mx=_fmt(dec.get("max_ms"), 3),
                    jit=_fmt(dec.get("jitter_ms"), 3),
                    rt=_fmt(c.get("realtime_factor"), 2),
                )
            )
        lines.append("")

        failures = [c for c in clips if c.get("error")]
        if failures:
            lines.append("## 失败片段")
            lines.append("")
            for c in failures:
                lines.append(f"### {c.get('clip')}（{c.get('hwaccel')}）")
                lines.append("")
                lines.append("```")
                lines.append(str(c.get("error")))
                lines.append("```")
                lines.append("")

        lines.append("## 时序自检")
        lines.append("")
        lines.append("| 片段 | tick 数 | 期望 tick 数 | tick 间隔中位数 ms | 最大漂移 ms | 迟到 tick |")
        lines.append("|---|---|---|---|---|---|")
        for c in clips:
            t = c.get("ticks") or {}
            lines.append(
                "| {clip} | {n} | {exp} | {med} | {drift} | {late} |".format(
                    clip=c.get("clip", "—"),
                    n=t.get("count", "—"),
                    exp=t.get("expected_count", "—"),
                    med=_fmt(t.get("median_interval_ms")),
                    drift=_fmt(t.get("max_drift_ms")),
                    late=t.get("late_ticks", "—"),
                )
            )
        lines.append("")

        slow_rows = [(c, c.get("slow_frames")) for c in clips if c.get("slow_frames")]
        if slow_rows:
            lines.append("## 偏慢的帧（> 5 ms）")
            lines.append("")
            lines.append(
                "| 片段 | 数量 | 首个 | 最大 ms | 相邻间隔中位数（帧） | 判定 |"
            )
            lines.append("|---|---|---|---|---|---|")
            for c, s in slow_rows:
                gap = s.get("median_gap_frames")
                periodic = bool(gap) and s["count"] >= 3
                lines.append(
                    "| {clip} | {n} | 第 {first} 帧（{fms} ms） | {mx} | {gap} | {verdict} |".format(
                        clip=c.get("clip", "—"),
                        n=s["count"],
                        first=s["first_index"],
                        fms=_fmt(s["first_ms"], 3),
                        mx=_fmt(s["max_ms"], 3),
                        gap=_fmt(gap, 1),
                        verdict="疑似周期性（见 README 已知现象）" if periodic else "偶发",
                    )
                )
            lines.append("")

    lines.append("## 口径说明")
    lines.append("")
    lines.append(
        "- `decode_ms` 是**每 packet** 交给 libavcodec 的耗时（`send_packet` + 取帧），"
        "不含 demux、不含 CPU 回读。"
    )
    skipped = [c for c in clips if c.get("skipped_first_frames")]
    if skipped:
        detail = "、".join(
            f"{c.get('clip')} {c['skipped_first_frames']} 帧" for c in skipped
        )
        lines.append(
            f"- **开头若干帧只从统计口径中排除**（含解码器初始化开销的第 0 帧）：{detail}。"
            "这些帧照常解码、照常写进 `frames.csv`、照常计入 `frame_count`，"
            "但不参与均值/分位数/抖动/实时倍率，也不出现在「偏慢的帧」里（"
            "因此 `decode.count` 会比 `frame_count` 少）。"
        )
    lines.append("- `p50/p95/p99` 用线性插值分位数；`抖动` 是相邻样本绝对差的中位数。")
    lines.append(
        "- `实时倍率 = (帧数 / 平均帧率) / 解码墙钟`；变帧率素材按平均帧率折算，"
        "> 1 表示解码快于实时播放。"
    )
    lines.append(
        "- per-packet 计时受解码器流水线深度影响，"
        "**不完全等同「packet 到帧就绪」的端到端延迟**（PLAN.md §3.5）。"
    )
    lines.append("")
    return "\n".join(lines)


def summarize_ticks(rows: list[TickRow], interval_ms: float, wall_ms: float) -> dict[str, Any]:
    late = sum(1 for r in rows if abs(r.tick_drift_ms) > 1.0)
    if not rows:
        return {
            "count": 0,
            "expected_count": int(wall_ms / interval_ms) if interval_ms else 0,
            "median_interval_ms": float("nan"),
            "max_drift_ms": float("nan"),
            "late_ticks": late,
            "interval_ms": interval_ms,
        }
    intervals = [b.t_ms - a.t_ms for a, b in zip(rows, rows[1:])]
    return {
        "count": len(rows),
        "expected_count": int(wall_ms / interval_ms) if interval_ms else 0,
        "median_interval_ms": (
            stats.percentile(sorted(intervals), 50.0) if intervals else float("nan")
        ),
        "max_drift_ms": max(abs(r.tick_drift_ms) for r in rows),
        "late_ticks": late,
        "interval_ms": interval_ms,
    }


def slow_frame_report(
    samples: list[FrameSample],
    threshold_ms: float = 5.0,
    *,
    index_offset: int = 0,
) -> dict[str, Any] | None:
    """挑出明显偏慢的帧并给出它们的间隔中位数。

    本机实测到某类「周期性慢帧」现象（见 README「已知现象」），
    把间隔中位数一起记下来，读者一眼就能判断它是周期性的还是随机的。

    :param index_offset: 给帧号加的回填量，用于 ``samples`` 里的 ``index``
        不携带全局帧号（例如全为 0）的场合；正常调用传 0 即可，
        此时直接用 ``FrameSample.index``，报出的就是与 ``frames.csv``
        一致的原始帧号。
    """
    slow = [
        (s.index + index_offset, s.decode_ms)
        for s in samples
        if s.decode_ms > threshold_ms
    ]
    if not slow:
        return None
    gaps = [b[0] - a[0] for a, b in zip(slow, slow[1:])]
    return {
        "threshold_ms": threshold_ms,
        "count": len(slow),
        "indices": [i for i, _ in slow][:64],
        "gaps": gaps[:64],
        "median_gap_frames": stats.percentile(sorted(gaps), 50.0) if gaps else None,
        "first_index": slow[0][0],
        "first_ms": slow[0][1],
        "max_ms": max(v for _, v in slow),
    }


def build_clip_summary(
    *,
    clip: dict[str, Any],
    plan: DecodePlan,
    attempts: Iterable[PlanAttempt],
    samples: list[FrameSample],
    frames_total: int,
    packets_total: int,
    wall_s: float,
    hardware_confirmed: bool,
    readback_detected: bool,
    hw_pix_fmts: list[str],
    ticks: dict[str, Any],
    keyframe_count: int | None = None,
    tail_frames: int = 0,
    stop_reason: str | None = None,
    expected_frames: int | None = None,
    backend: dict[str, Any] | None = None,
    skip_first_frames: int = 0,
) -> dict[str, Any]:
    """组装单个片段的 ``<clip>__<hwaccel>.summary.json`` 内容。

    ``skip_first_frames`` 只在**统计口径**上生效：开头 N 个帧不参与
    均值/分位数/抖动/实时倍率，也不出现在「偏慢的帧」里；它们照样写进
    ``frames.csv``、照样计入 ``frame_count``。因此当 N>0 时
    ``decode.count`` 会比 ``frame_count`` 少 N。
    """
    decode_ns = [s.decode_ns for s in samples]
    per_frame_ns = [s.decode_ns / max(1, s.frames_out) for s in samples]
    demux_ns = [s.demux_ns for s in samples]

    # 统计口径：丢掉开头 N 帧（通常是带解码器初始化开销的第 0 帧）。
    skipped = min(max(0, skip_first_frames), len(samples))
    stat_samples = samples[skipped:]
    stat_decode_ns = decode_ns[skipped:]
    stat_per_frame_ns = per_frame_ns[skipped:]
    stat_demux_ns = demux_ns[skipped:]

    fps = stats.average_frame_rate(
        frames_total,
        clip.get("duration_s"),
        clip.get("avg_frame_rate"),
    )
    return {
        "clip": clip.get("name"),
        "path": clip.get("path"),
        "hwaccel": plan.hwaccel,
        "decoder": plan.decoder,
        "backend": backend,
        "hwaccel_owned": True,
        "allow_software_fallback": plan.allow_software_fallback,
        "source": {
            "codec": clip.get("codec"),
            "width": clip.get("width"),
            "height": clip.get("height"),
            "pix_fmt": clip.get("pix_fmt"),
            "profile": clip.get("profile"),
            "level": clip.get("level"),
            "duration_s": clip.get("duration_s"),
            "bit_rate": clip.get("bit_rate"),
            "avg_frame_rate": clip.get("avg_frame_rate"),
            "is_vfr": clip.get("is_vfr", False),
        },
        "flags": {
            "hardware_decode_confirmed": hardware_confirmed,
            "readback_detected": readback_detected,
        },
        "hw_pix_fmts": hw_pix_fmts,
        "packets_total": packets_total,
        "frame_count": frames_total,
        "skipped_first_frames": skipped,
        "tail_frames": tail_frames,
        "keyframe_count": keyframe_count,
        "stop_reason": stop_reason,
        "expected_frames": expected_frames,
        "wall_s": wall_s,
        "throughput_fps": (frames_total / wall_s) if wall_s > 0 else float("nan"),
        "average_frame_rate": fps,
        "realtime_factor": stats.realtime_factor(frames_total, wall_s, fps),
        "decode": stats.summarize(stat_decode_ns).as_dict(),
        "decode_per_frame_ms": {
            **stats.summarize(stat_per_frame_ns).as_dict(),
            "note": "= decode_ms / frames_out",
        },
        "demux": stats.summarize(stat_demux_ns).as_dict(),
        "slow_frames": slow_frame_report(stat_samples),
        "ticks": ticks,
        "attempts": [
            {"hwaccel": a.hwaccel, "decoder": a.decoder, "error": a.error} for a in attempts
        ],
    }


def render_failure(
    clip: dict[str, Any],
    hwaccel: str,
    attempts: Iterable[PlanAttempt],
    environment: dict[str, Any],
    hint: str = "",
) -> str:
    """硬解无法建立时的可读报错。"""
    lines = [
        f"片段 {clip.get('name')}（codec={clip.get('codec')}）无法建立硬件解码会话。",
        f"  平台：{environment.get('platform')}  设备类型：{hwaccel}",
        f"  可用设备类型：{', '.join(environment.get('hwdevices_available', []) or []) or '（无）'}",
        "  已尝试：",
        format_attempts(attempts),
    ]
    if hint:
        lines.append(hint)
    lines.append(
        "  本项目**不允许回退软解**（allow_software_fallback=False）。"
        "如需临时绕过，请显式加 --allow-software-fallback（结果不能当作硬解延迟）。"
    )
    return "\n".join(lines)
