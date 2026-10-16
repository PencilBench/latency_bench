"""``latency_bench.stats`` 的单元测试。"""

from __future__ import annotations

import math

import pytest

from latency_bench import stats


def test_percentile_matches_linear_interpolation():
    values = [1.0, 2.0, 3.0, 4.0]
    assert stats.percentile(values, 0.0) == pytest.approx(1.0)
    assert stats.percentile(values, 100.0) == pytest.approx(4.0)
    assert stats.percentile(values, 50.0) == pytest.approx(2.5)
    # (n-1) * 0.95 = 2.85 -> 3 + 0.85 * (4 - 3)
    assert stats.percentile(values, 95.0) == pytest.approx(3.85)


def test_percentile_edge_cases():
    assert math.isnan(stats.percentile([], 50.0))
    assert stats.percentile([7.0], 42.0) == 7.0
    with pytest.raises(ValueError):
        stats.percentile([1.0, 2.0], 101.0)


def test_mean_and_stdev():
    assert stats.mean([]) != stats.mean([])  # NaN
    assert stats.mean([1.0, 2.0, 3.0]) == pytest.approx(2.0)
    assert stats.stdev([1.0, 2.0, 3.0]) == pytest.approx(1.0)
    assert stats.stdev([5.0]) == 0.0
    assert stats.stdev([1.0, 3.0], sample=False) == pytest.approx(1.0)


def test_jitter_is_median_of_absolute_differences():
    # 差值：10, 10, 1000 ns -> 中位数 10 ns -> 10e-6 ms
    samples = [0, 10, 20, 1020]
    assert stats.jitter_ns_to_ms(samples) == pytest.approx(10 / 1e6)
    assert stats.jitter_ns_to_ms([1]) == 0.0
    assert stats.jitter_ns_to_ms([]) == 0.0
    # 抖动是「毫秒」：1000 ns 的中位差 = 0.001 ms
    assert stats.jitter_ns_to_ms([1_000, 2_000, 3_000]) == pytest.approx(0.001)


def test_realtime_factor():
    # 300 帧 @ 30fps = 10 s 素材，用 5 s 解完 -> 2x 实时
    assert stats.realtime_factor(300, 5.0, 30.0) == pytest.approx(2.0)
    assert math.isnan(stats.realtime_factor(300, 0.0, 30.0))
    assert math.isnan(stats.realtime_factor(300, 5.0, 0.0))


def test_average_frame_rate_prefers_measured_duration():
    # 变帧率素材：容器时长优先于容器自报的平均帧率
    assert stats.average_frame_rate(300, 10.0, 29.0) == pytest.approx(30.0)
    assert stats.average_frame_rate(0, 10.0, 29.0) == pytest.approx(29.0)
    assert math.isnan(stats.average_frame_rate(0, None, None))


def test_summarize_units_and_percentiles():
    samples = [1_000, 2_000, 3_000, 4_000, 10_000]  # 纳秒
    s = stats.summarize(samples)
    assert s.count == 5
    assert s.total_ns == 20_000
    # 对外单位是毫秒：1000 ns = 0.001 ms
    assert s.mean_ms == pytest.approx(0.004)
    assert s.median_ms == pytest.approx(0.003)
    assert s.min_ms == pytest.approx(0.001)
    assert s.max_ms == pytest.approx(0.010)
    assert s.p95_ms == pytest.approx(0.0088)
    # 相邻差值 1000 ns -> 中位数 1 µs = 0.001 ms
    assert s.jitter_ms == pytest.approx(0.001)

    payload = s.as_dict()
    assert payload["count"] == 5
    assert payload["total_ms"] == pytest.approx(0.020)


def test_summarize_keeps_samples_optional():
    s = stats.summarize([1_500, 2_500], keep_samples=True)
    assert s.samples_ns == [1500, 2500]
    assert stats.summarize([1_500]).samples_ns == []


def test_summarize_empty():
    s = stats.summarize([])
    assert s.count == 0
    assert math.isnan(s.mean_ms)
