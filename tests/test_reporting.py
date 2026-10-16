"""``latency_bench.reporting`` 的单元测试（含假时钟）。"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from latency_bench import reporting
from latency_bench.accel import DecodePlan, PlanAttempt
from latency_bench.reporting import (
    FrameSample,
    SampleSink,
    TickRow,
    Ticker,
    build_clip_summary,
    next_tick_deadline,
    render_failure,
    render_markdown,
    summarize_ticks,
)


def _sample(index: int, decode_us: float = 1000.0, *, tail: bool = False) -> FrameSample:
    return FrameSample(
        index=index,
        packet_index=index,
        pts=index,
        pts_ms=float(index),
        demux_ns=5_000,
        decode_ns=int(decode_us * 1e3),
        frames_out=1,
        keyframe=index == 0,
        pict_type="P",
        fmt="d3d11",
        tail=tail,
    )


# --------------------------------------------------------------------------------------
# 33 ms tick 切分与漂移补偿（假时钟）
# --------------------------------------------------------------------------------------


def test_next_tick_deadline_on_schedule():
    assert next_tick_deadline(0.033, 0.033, 0.033) == pytest.approx(0.066)


def test_next_tick_deadline_absorbs_one_late_tick():
    # 迟了 5 ms：下一个截止时刻仍然贴着原来的时间轴，不带偏。
    assert next_tick_deadline(0.033, 0.038, 0.033) == pytest.approx(0.066)


def test_next_tick_deadline_skips_missed_slots_without_drifting():
    # 迟了 100 ms（约 3 个周期）：直接跳到最近一个未来的截止时刻。
    got = next_tick_deadline(0.033, 0.133, 0.033)
    assert got == pytest.approx(0.165)
    assert got > 0.133


def test_short_late_tick_does_not_accumulate_drift():
    """每个 tick 最多迟到 2 ms（不到一个周期）时，截止时刻不应被持续推后。"""
    interval = 0.033
    late = 0.002
    deadline = interval
    now = 0.0
    for _ in range(20):
        now += interval + late
        deadline = next_tick_deadline(deadline, now, interval)
        # 不变式：截止时刻总是从上一截止时刻整周期推进
        assert deadline > now
        assert deadline <= now + interval
    # 截止时刻始终贴在「周期整数倍」的时间轴上（没有累积迟到），
    # 也不比理想时间轴落后超过一个周期 + 累计迟到。
    ratio = deadline / interval
    assert abs(ratio - round(ratio)) < 1e-6
    assert deadline >= 20 * interval - 1e-9
    assert deadline <= 22 * interval + late


def test_large_late_tick_skips_slots_instead_of_catching_up():
    """迟到超过一个周期时跳到最近一个未来截止时刻，避免追赶式抖动。"""
    interval = 0.033
    # 迟了 100 ms（约 3 个周期）：下一个截止时刻应是 0.165 而不是 0.066
    deadline = next_tick_deadline(interval, interval + 0.100, interval)
    assert deadline == pytest.approx(5 * interval)
    assert deadline > interval + 0.100
    assert deadline - (interval + 0.100) < interval


def test_ticker_real_thread_tick_count_and_interval():
    """真线程冒烟：约 150 ms 内应产生 4~6 个 33 ms tick。"""
    ticks: list[float] = []

    def on_tick(ticker: Ticker, elapsed_s: float) -> None:
        ticks.append(elapsed_s)

    ticker = Ticker(0.033, on_tick)
    ticker.start()
    time.sleep(0.15)
    ticker.stop()

    assert 3 <= len(ticks) <= 7, ticks
    assert ticks == sorted(ticks)
    assert ticker.median_interval_ms == pytest.approx(33.0, abs=6.0)


def test_summarize_ticks_self_check():
    rows = [
        TickRow(
            tick_index=i,
            t_ms=i * 33.0,
            t_wall_iso="",
            frames_in_tick=10,
            frames_total=10 * (i + 1),
            packets_in_tick=10,
            decode_us_last=100.0,
            decode_us_min=90.0,
            decode_us_max=110.0,
            decode_us_avg=100.0,
            decode_us_p50=100.0,
            decode_us_p95=108.0,
            fps_inst=303.0,
            demux_us_avg=20.0,
            wall_ms=i * 33.0,
            tick_drift_ms=0.0,
        )
        for i in range(5)
    ]
    got = summarize_ticks(rows, 33.0, 165.0)
    assert got["count"] == 5
    assert got["expected_count"] == 5
    assert got["median_interval_ms"] == pytest.approx(33.0)
    assert got["late_ticks"] == 0


def test_summarize_ticks_empty():
    got = summarize_ticks([], 33.0, 100.0)
    assert got["count"] == 0
    assert got["expected_count"] == 3


# --------------------------------------------------------------------------------------
# SampleSink 游标语义
# --------------------------------------------------------------------------------------


def test_sample_sink_cursor_returns_only_new_samples():
    sink = SampleSink()
    cursor = 0
    sink.extend([_sample(0), _sample(1)])
    new, cursor = sink.since(cursor)
    assert [s.index for s in new] == [0, 1]
    new, cursor = sink.since(cursor)
    assert new == []
    sink.extend([_sample(2)])
    new, cursor = sink.since(cursor)
    assert [s.index for s in new] == [2]


def test_sample_sink_cursor_survives_trimming():
    sink = SampleSink()
    sink.MAX_WINDOW = 4
    for i in range(10):
        sink.extend([_sample(i)])
    # 只保留最近 4 个样本，但游标是全局序号：不会重复投递旧样本。
    assert len(sink.samples) == 10
    assert sink.trimmed == 6
    new, cursor = sink.since(0)
    assert [s.index for s in new] == [6, 7, 8, 9]
    assert cursor == 10
    new, cursor = sink.since(cursor)
    assert new == []


# --------------------------------------------------------------------------------------
# 汇总与渲染
# --------------------------------------------------------------------------------------


def _summary(**overrides):
    clip = {
        "name": "AV1_30.99Mbps.mp4",
        "path": r"F:\AV1H265\clips\AV1_30.99Mbps.mp4",
        "codec": "av1",
        "width": 1920,
        "height": 1080,
        "pix_fmt": "yuv420p10le",
        "profile": "Main",
        "level": 9,
        "duration_s": 9.92,
        "bit_rate": 30_990_000,
        "avg_frame_rate": 30.0,
        "is_vfr": False,
    }
    clip.update(overrides.pop("clip", {}))
    samples = overrides.pop("samples", [_sample(i, 1000.0 + i * 10) for i in range(300)])
    plan = overrides.pop("plan", DecodePlan(hwaccel="d3d11va", decoder="av1"))
    ticks = overrides.pop(
        "ticks",
        {"count": 1, "expected_count": 1, "median_interval_ms": 33.0,
         "max_drift_ms": 0.5, "late_ticks": 0, "interval_ms": 33.0},
    )
    kwargs = dict(
        clip=clip,
        plan=plan,
        attempts=[PlanAttempt("d3d11va", "av1", "")],
        samples=samples,
        frames_total=300,
        packets_total=300,
        wall_s=0.5,
        hardware_confirmed=True,
        readback_detected=False,
        hw_pix_fmts=["d3d11"],
        ticks=ticks,
    )
    kwargs.update(overrides)
    return build_clip_summary(**kwargs)


def test_build_clip_summary_fields():
    s = _summary()
    assert s["clip"] == "AV1_30.99Mbps.mp4"
    assert s["hwaccel"] == "d3d11va"
    assert s["decoder"] == "av1"
    assert s["frame_count"] == 300
    assert s["flags"]["hardware_decode_confirmed"] is True
    assert s["flags"]["readback_detected"] is False
    # 实时倍率用「帧数 / 容器时长」折算：300 / 9.92 = 30.24 fps，0.5 s 解完
    assert s["average_frame_rate"] == pytest.approx(300 / 9.92)
    assert s["realtime_factor"] == pytest.approx((300 / (300 / 9.92)) / 0.5)
    assert s["decode"]["count"] == 300
    # 样本 decode_us = 1000 + 10 * i（i = 0..299）-> 均值 2495
    assert s["decode"]["mean_us"] == pytest.approx(2495.0)
    assert s["ticks"]["count"] == 1
    json.dumps(s)  # 必须可序列化


def test_build_clip_summary_vfr_uses_average_rate():
    s = _summary(clip={"avg_frame_rate": 30.0, "duration_s": 10.0}, frames_total=250)
    assert s["average_frame_rate"] == pytest.approx(25.0)


def test_render_markdown_contains_table_and_failure():
    ok = _summary()
    bad = {
        "clip": "AV1_126.99Mbps.mp4",
        "hwaccel": "d3d11va",
        "error": "probe-failed\n  已尝试：\n    - av1 + d3d11va: EINVAL",
        "source": {"codec": "libdav1d", "width": 3840, "height": 2160},
        "flags": {"hardware_decode_confirmed": False, "readback_detected": False},
    }
    md = render_markdown({"run_id": "r1", "clips": [ok, bad], "environment": {}})
    assert "## 逐片段对比" in md
    assert "AV1_30.99Mbps.mp4" in md
    assert "## 失败片段" in md
    assert "probe-failed" in md
    assert "## 时序自检" in md


def test_render_markdown_without_clips():
    md = render_markdown({"run_id": "empty", "clips": [], "environment": {}})
    assert "empty" in md
    assert "## 口径说明" in md


def test_render_failure_mentions_attempts_and_no_fallback():
    text = render_failure(
        {"name": "clip.mp4", "codec": "av1"},
        "d3d11va",
        [PlanAttempt("d3d11va", "av1", "get_buffer() failed")],
        {"platform": "Windows", "hwdevices_available": ["d3d11va"]},
        hint="  提示：换构建。",
    )
    assert "无法建立硬件解码会话" in text
    assert "av1" in text and "get_buffer() failed" in text
    assert "提示：换构建。" in text
    assert "不允许回退软解" in text


# --------------------------------------------------------------------------------------
# RunWriter
# --------------------------------------------------------------------------------------


def test_run_writer_writes_all_artifacts(work_dir: Path):
    writer = reporting.RunWriter(
        out_dir=work_dir / "run",
        run_id="r1",
        command="python -m latency_bench",
        environment={"platform": "Windows"},
        started_at="now",
    )
    frame_fh, frame_writer = writer.clip_frame_writer("clip", "d3d11va")
    frame_writer.writerow(_sample(0).as_row())
    frame_fh.close()

    tick_fh, tick_writer = writer.clip_tick_writer("clip", "d3d11va")
    tick_writer.writerow(
        TickRow(0, 33.0, "iso", 1, 1, 1, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 30.0, 1.0, 33.0, 0.0).as_row()
    )
    tick_fh.close()

    writer.write_clip_summary("clip", "d3d11va", _summary())
    writer.write_inventory({"clips": []})
    json_path, md_path = writer.write_summary(extra={"hwaccel": "d3d11va"})

    assert (work_dir / "run" / "clip__d3d11va.frames.csv").exists()
    assert (work_dir / "run" / "clip__d3d11va.ticks.csv").exists()
    assert (work_dir / "run" / "clip__d3d11va.summary.json").exists()
    assert (work_dir / "run" / "inventory.json").exists()
    assert json_path.exists() and md_path.exists()

    header = (work_dir / "run" / "clip__d3d11va.ticks.csv").read_text("utf-8").splitlines()[0]
    assert header.split(",") == reporting.TICK_COLUMNS
    payload = json.loads(json_path.read_text("utf-8"))
    assert payload["hwaccel"] == "d3d11va"
    assert len(payload["clips"]) == 1


def test_json_output_replaces_nan_with_null():
    payload = {"nan": float("nan"), "inf": float("inf"), "nested": [float("nan")]}
    text = reporting._dumps(payload)
    assert "NaN" not in text and "Infinity" not in text
    assert json.loads(text) == {"nan": None, "inf": None, "nested": [None]}
