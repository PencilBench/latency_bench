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


def _sample(index: int, decode_ms: float = 1.0, *, tail: bool = False) -> FrameSample:
    return FrameSample(
        index=index,
        packet_index=index,
        pts=index,
        pts_ms=float(index),
        demux_ns=5_000,
        decode_ns=int(decode_ms * 1e6),
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


def _sleep_overshoot_ms(probe_s: float = 0.031, samples: int = 5) -> float:
    """当前系统 ``time.sleep`` 的超出量中位数（ms）。用 31 ms 探测，与实际 tick 周期一致。"""
    over = []
    for _ in range(samples):
        t0 = time.perf_counter()
        time.sleep(probe_s)
        over.append((time.perf_counter() - t0 - probe_s) * 1e3)
    return sorted(over)[len(over) // 2]


def test_ticker_real_thread_tick_count_and_interval():
    """真线程冒烟：约 150 ms 内应产生 4~6 个 33 ms tick。

    Ticker 的等待策略是「``time.sleep`` 到截止前 ``spin_margin_s``（2 ms）
    + 忙等收尾」，前提是 sleep 超出量远小于 2 ms。刚重启或定时器被系统合并的
    macOS 上实测超出量可达 60–100 ms（忙等本身仍是精确的），此时 ticker 会
    **按设计**跳过错过的 tick，本断言必然失败 —— 这是环境问题而非代码缺陷。
    """
    overshoot = _sleep_overshoot_ms()
    if overshoot > 10.0:
        pytest.skip(
            f"当前系统 time.sleep 粒度太差（31 ms 的 sleep 超出 {overshoot:.0f} ms，疑似定时器合并），"
            "无法验证实时 ticker"
        )
    ticks: list[float] = []

    def on_tick(ticker: Ticker, elapsed_s: float) -> None:
        ticks.append(elapsed_s)

    ticker = Ticker(0.033, on_tick)
    ticker.start()
    time.sleep(0.15)
    ticker.stop()

    if len(ticks) < 3:
        # 粒度会波动：跑完再探一次，确认是系统定时器被合并而不是 ticker 逻辑有问题
        again = _sleep_overshoot_ms(samples=6)
        if again > 10.0:
            pytest.skip(f"ticker 运行期间系统定时器被合并（sleep 超出 {again:.0f} ms）")
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
            decode_ms_last=0.1,
            decode_ms_min=0.09,
            decode_ms_max=0.11,
            decode_ms_avg=0.1,
            decode_ms_p50=0.1,
            decode_ms_p95=0.108,
            fps_inst=303.0,
            demux_ms_avg=0.02,
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
    samples = overrides.pop("samples", [_sample(i, 1.0 + i * 0.01) for i in range(300)])
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
    # 样本 decode_ms = 1.0 + 0.01 * i（i = 0..299）-> 均值 2.495 ms
    assert s["decode"]["mean_ms"] == pytest.approx(2.495)
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


# --------------------------------------------------------------------------------------
# 只在统计口径上排除第 0 帧
# --------------------------------------------------------------------------------------


def test_skip_first_frames_default_is_one():
    """默认排除第 0 帧：它带着解码器初始化开销，会把 p99/max 显著抬高。"""
    from latency_bench.cli import build_parser

    assert build_parser().parse_args([]).skip_first_frames == 1
    assert build_parser().parse_args(["--skip-first-frames", "0"]).skip_first_frames == 0


def test_skip_first_frames_affects_stats_only():
    """排除只作用在统计口径：帧数照记，只是统计样本少一个。"""
    s = _summary(skip_first_frames=1)
    assert s["skipped_first_frames"] == 1
    assert s["frame_count"] == 300          # 帧数照记
    assert s["decode"]["count"] == 299      # 统计样本少一个
    assert s["demux"]["count"] == 299
    assert s["decode_per_frame_ms"]["count"] == 299


def test_skip_first_frames_removes_first_frame_outlier():
    """第 0 帧若是异常慢帧，排除后不应再影响统计，也不该出现在偏慢帧里。"""
    samples = [_sample(0, 25.0)] + [_sample(i, 1.0) for i in range(1, 50)]
    without = _summary(samples=samples, skip_first_frames=1)
    with_all = _summary(samples=samples, skip_first_frames=0)

    assert without["decode"]["max_ms"] == pytest.approx(1.0)
    assert with_all["decode"]["max_ms"] == pytest.approx(25.0)
    assert without["slow_frames"] is None
    assert with_all["slow_frames"]["count"] == 1
    assert with_all["slow_frames"]["first_index"] == 0


def test_slow_frame_indices_keep_original_frame_numbers():
    """排除开头帧后，偏慢帧报出的仍应是原始帧号（与 frames.csv 对齐）。"""
    # _summary 固定用 300 帧，覆盖传入的 samples，因此这里直接用 build_clip_summary
    # 构造一个 10 帧的小样本：第 5 帧 9 ms 是唯一慢帧。
    samples = [_sample(i, 1.0) for i in range(5)] + [_sample(5, 9.0)] + [
        _sample(i, 1.0) for i in range(6, 10)
    ]
    s = build_clip_summary(
        clip={"name": "c.mp4", "duration_s": 10.0, "avg_frame_rate": 30.0},
        plan=DecodePlan(hwaccel="d3d11va", decoder="av1"),
        attempts=[],
        samples=samples,
        frames_total=10,
        packets_total=10,
        wall_s=1.0,
        hardware_confirmed=True,
        readback_detected=False,
        hw_pix_fmts=["d3d11"],
        ticks={"count": 0},
        skip_first_frames=2,
    )
    # 第 5 帧是原始帧号；相对剩余样本的序号是 5-2=3，加回偏移后仍是 5
    assert s["slow_frames"]["first_index"] == 5
    assert s["slow_frames"]["indices"] == [5]
    # 0、1 号帧被排除，统计样本从第 2 帧算起
    assert s["decode"]["count"] == 8
    assert s["skipped_first_frames"] == 2


def test_summary_skip_defaults_to_zero_for_callers():
    """构造 summary 的默认值保持 0，避免调用方出现意外口径。"""
    s = _summary()
    assert s["skipped_first_frames"] == 0
    assert s["decode"]["count"] == 300


def test_render_markdown_notes_skipped_frames():
    ok = _summary(skip_first_frames=1)
    md = render_markdown({"run_id": "r1", "clips": [ok], "environment": {}})
    assert "开头若干帧只从统计口径中排除" in md
    assert "1 帧" in md


def test_render_markdown_omits_note_without_skip():
    ok = _summary()
    md = render_markdown({"run_id": "r1", "clips": [ok], "environment": {}})
    assert "开头若干帧只从统计口径中排除" not in md


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
    ctx = reporting.RoundContext(index=2, total=4, warmup=2, counted=2)
    frame_fh, frame_writer = writer.clip_frame_writer("clip", "d3d11va", ctx)
    frame_writer.writerow(_sample(0).as_row())
    frame_fh.close()

    tick_fh, tick_writer = writer.clip_tick_writer("clip", "d3d11va", ctx)
    tick_writer.writerow(
        TickRow(0, 33.0, "iso", 1, 1, 1, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 30.0, 1.0, 33.0, 0.0).as_row()
    )
    tick_fh.close()

    writer.write_clip_summary("clip", "d3d11va", _summary())
    writer.write_inventory({"clips": []})
    json_path, md_path = writer.write_summary(extra={"hwaccel": "d3d11va"})

    assert (work_dir / "run" / "clip__d3d11va.r3.frames.csv").exists()
    assert (work_dir / "run" / "clip__d3d11va.r3.ticks.csv").exists()
    assert (work_dir / "run" / "clip__d3d11va.summary.json").exists()
    assert (work_dir / "run" / "inventory.json").exists()
    assert json_path.exists() and md_path.exists()

    header = (work_dir / "run" / "clip__d3d11va.r3.ticks.csv").read_text("utf-8").splitlines()[0]
    assert header.split(",") == reporting.TICK_COLUMNS
    payload = json.loads(json_path.read_text("utf-8"))
    assert payload["hwaccel"] == "d3d11va"
    assert len(payload["clips"]) == 1


def test_round_context_names_and_tags():
    """轮次标签既是文件名中缀，也写进 summary 的 round 字段。"""
    # 默认：单轮 r1
    plain = reporting.RoundContext()
    assert plain.number == 1
    assert plain.file_tag() == "r1"
    assert plain.is_warmup is False

    warm = reporting.RoundContext(index=1, total=4, warmup=2, counted=2)
    assert warm.number == 2 and warm.file_tag() == "r2" and warm.is_warmup is True

    last = reporting.RoundContext(index=3, total=4, warmup=2, counted=2)
    assert last.file_tag() == "r4" and last.is_warmup is False
    meta = last.metadata(frames_csv="clip__d3d11va.r4.frames.csv",
                         ticks_csv="clip__d3d11va.r4.ticks.csv")
    assert meta["file_tag"] == "r4" and meta["number"] == 4 and meta["index"] == 3
    assert meta["total_runs"] == 4 and meta["warmup_runs"] == 2 and meta["counted_runs"] == 2
    assert meta["frames_csv"].endswith(".r4.frames.csv")

    with pytest.raises(ValueError):
        reporting.round_file_tag(0)


def test_csv_paths_carry_round_tag(work_dir: Path):
    """同一片段的不同轮必须落到不同文件，否则旧行为会互相覆盖。"""
    writer = reporting.RunWriter(
        out_dir=work_dir / "run", run_id="x", command="c", environment={}, started_at="now"
    )
    seen = set()
    for i in range(4):
        ctx = reporting.RoundContext(index=i, total=4, warmup=2, counted=2)
        p = writer.clip_frames_path("clip", "d3d11va", ctx)
        q = writer.clip_ticks_path("clip", "d3d11va", ctx)
        assert p.name == f"clip__d3d11va.r{i + 1}.frames.csv"
        assert q.name == f"clip__d3d11va.r{i + 1}.ticks.csv"
        seen.add(p.name)
    assert len(seen) == 4


def test_build_clip_summary_records_round():
    """summary 必须自报 CSV 文件名，读取方不必猜命名规则。"""
    ctx = reporting.RoundContext(index=3, total=4, warmup=2, counted=2)
    summary = reporting.build_clip_summary(
        clip={"name": "clip.mp4", "duration_s": 1.0, "avg_frame_rate": 60.0},
        plan=DecodePlan(hwaccel="d3d11va", decoder="hevc"),
        attempts=[],
        samples=[_sample(i) for i in range(4)],
        frames_total=4,
        packets_total=4,
        wall_s=1.0,
        hardware_confirmed=True,
        readback_detected=False,
        hw_pix_fmts=["d3d11"],
        ticks={},
        round_info=ctx.metadata("clip__d3d11va.r4.frames.csv", "clip__d3d11va.r4.ticks.csv"),
    )
    assert summary["round"]["file_tag"] == "r4"
    assert summary["round"]["frames_csv"] == "clip__d3d11va.r4.frames.csv"


def test_json_output_replaces_nan_with_null():
    payload = {"nan": float("nan"), "inf": float("inf"), "nested": [float("nan")]}
    text = reporting._dumps(payload)
    assert "NaN" not in text and "Infinity" not in text
    assert json.loads(text) == {"nan": None, "inf": None, "nested": [None]}
