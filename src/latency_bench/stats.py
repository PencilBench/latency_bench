"""纯 Python 的统计工具。

刻意不依赖 numpy：测量结果的数值必须与 PyAV 解出的原始样本逐位一致，
而这里用到的都是「排序 + 线性插值」这类可精确复现的小算子。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

__all__ = [
    "percentile",
    "mean",
    "stdev",
    "jitter_ms",
    "summarize",
    "DecodeSummary",
    "realtime_factor",
    "fps_from_frame_count",
    "average_frame_rate",
]


def percentile(sorted_values: Sequence[float], q: float) -> float:
    """线性插值分位数（与 numpy 默认 ``linear`` 方法一致）。

    :param sorted_values: **已升序排序**的样本序列。
    :param q: 0..100 之间的百分位。
    """
    n = len(sorted_values)
    if n == 0:
        return float("nan")
    if n == 1:
        return float(sorted_values[0])
    if not 0.0 <= q <= 100.0:
        raise ValueError(f"q must be within [0, 100], got {q!r}")

    pos = (n - 1) * (q / 100.0)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(sorted_values[lo])
    frac = pos - lo
    return float(sorted_values[lo]) * (1.0 - frac) + float(sorted_values[hi]) * frac


def mean(values: Sequence[float]) -> float:
    if not values:
        return float("nan")
    return math.fsum(values) / len(values)


def stdev(values: Sequence[float], *, sample: bool = True) -> float:
    """标准差。``sample=True`` 时用无偏（n-1）估计。"""
    n = len(values)
    if n == 0:
        return float("nan")
    if n == 1:
        return 0.0
    mu = mean(values)
    acc = math.fsum((v - mu) ** 2 for v in values)
    denom = (n - 1) if sample else n
    return math.sqrt(acc / denom)


def jitter_ms(values_ns: Sequence[float]) -> float:
    """抖动：相邻样本绝对差的中位数。

    用中位数而不是平均数，避免单次调度抖动（GC、缺页）把整段统计拉歪。
    :param values_ns: 以**纳秒**为单位的样本序列（按解码先后顺序，不排序）。
    """
    if len(values_ns) < 2:
        return 0.0
    diffs = sorted(abs(b - a) for a, b in zip(values_ns, values_ns[1:]))
    return percentile(diffs, 50.0) / 1e6


def realtime_factor(frame_count: int, wall_s: float, fps: float) -> float:
    """实时倍率：``(帧数 / 平均帧率) / 解码墙钟``。

    > 1 表示解码快于实时播放。变帧率素材按平均帧率折算。
    """
    if wall_s <= 0 or fps <= 0:
        return float("nan")
    return (frame_count / fps) / wall_s


def fps_from_frame_count(frame_count: int, duration_s: float) -> float:
    if duration_s <= 0:
        return float("nan")
    return frame_count / duration_s


def average_frame_rate(
    frame_count: int,
    container_duration_s: float | None,
    stream_average_rate: float | None,
) -> float:
    """平均帧率。

    优先用「帧数 / 容器时长」（对变帧率素材也成立）；容器时长不可用时
    退回 FFmpeg 给出的 ``stream.average_rate``。
    """
    if container_duration_s and container_duration_s > 0 and frame_count > 0:
        return frame_count / container_duration_s
    if stream_average_rate and stream_average_rate > 0:
        return float(stream_average_rate)
    return float("nan")


@dataclass
class DecodeSummary:
    """一组解码耗时样本（纳秒）的汇总统计，对外单位为微秒。"""

    count: int = 0
    total_ns: int = 0
    mean_us: float = float("nan")
    median_us: float = float("nan")
    p95_us: float = float("nan")
    p99_us: float = float("nan")
    min_us: float = float("nan")
    max_us: float = float("nan")
    stdev_us: float = float("nan")
    jitter_us: float = 0.0
    samples_ns: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, float | int]:
        return {
            "count": self.count,
            "total_us": self.total_ns / 1e3,
            "mean_us": self.mean_us,
            "median_us": self.median_us,
            "p95_us": self.p95_us,
            "p99_us": self.p99_us,
            "min_us": self.min_us,
            "max_us": self.max_us,
            "stdev_us": self.stdev_us,
            "jitter_us": self.jitter_us,
        }


def summarize(samples_ns: Iterable[float], *, keep_samples: bool = False) -> DecodeSummary:
    """把纳秒样本汇成 :class:`DecodeSummary`。"""
    values = [float(v) for v in samples_ns]
    if not values:
        return DecodeSummary()

    ordered = sorted(values)
    s = DecodeSummary(
        count=len(values),
        total_ns=int(math.fsum(values)),
        mean_us=mean(values) / 1e3,
        median_us=percentile(ordered, 50.0) / 1e3,
        p95_us=percentile(ordered, 95.0) / 1e3,
        p99_us=percentile(ordered, 99.0) / 1e3,
        min_us=ordered[0] / 1e3,
        max_us=ordered[-1] / 1e3,
        stdev_us=stdev(values) / 1e3,
        jitter_us=jitter_ms(values),
    )
    if keep_samples:
        s.samples_ns = [int(v) for v in values]
    return s
