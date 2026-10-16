"""解码循环与逐 packet 计时（PLAN.md §5）。

每条 packet 的测量口径：

.. code-block:: text

    pkt  = next(demux)          # demux_ns = 取到 packet 的耗时（与解码分开统计）
    t0   = perf_counter_ns()
    frms = decode(pkt)          # send_packet + receive_frame；硬件帧留在 GPU
    t1   = perf_counter_ns()

* ``decode_ms``（packet 级）= ``t1 - t0``，不含 demux、不含 CPU 回读；
* 每解出一帧写一行 ``frames.csv``；同一 packet 连出多帧时按 ``frames_out`` 均摊；
* 结尾 ``decode(None)`` 排空流水线，尾部帧标记 ``tail=1``。
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

import av

from . import accel, ffmpeg_ctypes, stats
from .accel import AccelError, DecodePlan, PlanAttempt
from .reporting import (
    FrameSample,
    SampleSink,
    TickRow,
    Ticker,
    build_clip_summary,
    summarize_ticks,
)

__all__ = [
    "DecodeSession",
    "ProbeOutcome",
    "ClipRunResult",
    "probe_plan",
    "run_clip",
]

#: 保住 PyAV 的 HWAccel 对象引用（解码期间不能被 GC 回收）。
_HWACCEL_REFS: dict[int, Any] = {}


@dataclass
class DecodeSession:
    """一个可用的解码会话：容器 + 目标视频流 + 解码上下文。"""

    container: Any
    stream: Any
    decoder: Any
    plan: DecodePlan
    clip: dict[str, Any]

    def close(self) -> None:
        try:
            self.container.close()
        except Exception:
            pass
        _HWACCEL_REFS.pop(id(self.decoder), None)
        closer = getattr(self.decoder, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass


@dataclass
class ProbeOutcome:
    """探测结果：成功时带上一个可用的会话与已解码的包。"""

    session: DecodeSession | None
    attempts: list[PlanAttempt] = field(default_factory=list)
    pending: list[tuple[Any, int, int, list[Any]]] = field(default_factory=list)
    """探测阶段已消费的 packet：``(packet, demux_ns, decode_ns, frames)``。"""

    @property
    def ok(self) -> bool:
        return self.session is not None


def clip_metadata(path: str) -> dict[str, Any]:
    """只读容器拿到测量所需的基本信息（不在这里做完整 inventory）。"""
    with av.open(path) as container:
        stream = container.streams.video[0]
        ctx = stream.codec_context
        duration_s = float(container.duration / av.time_base) if container.duration else None
        return {
            "name": path.replace("\\", "/").rsplit("/", 1)[-1],
            "path": path,
            "codec": ctx.name,
            "codec_id": str(getattr(ctx.codec, "canonical_name", "") or ""),
            "width": ctx.width,
            "height": ctx.height,
            "pix_fmt": ctx.format.name if ctx.format else None,
            "profile": str(ctx.profile) if ctx.profile is not None else None,
            "level": ctx.level,
            "duration_s": duration_s,
            "bit_rate": container.bit_rate,
            "avg_frame_rate": float(stream.average_rate) if stream.average_rate else None,
            "frame_count_hint": stream.frames or None,
            "is_vfr": False,
        }


def _container_default_decoder(stream: Any, hwaccel: accel.HWAccelLike | None) -> Any:
    """让容器自己选解码器（``libdav1d`` 等），仅在没有更优候选时使用。"""
    context = stream.codec_context
    if hwaccel is not None and not context.is_hwaccel:
        raise AccelError(
            f"容器为 codec {stream.codec_context.name!r} 选择的解码器不支持硬件加速"
            "（hardware_configs 为空），无法用于本项目。"
        )
    return context


def _needs_ctypes_backend(codec_name: str, hwaccel: str | None) -> bool:
    """这个「编解码器 × 设备类型」是否必须走 ctypes 后端。

    目前只有 AV1 + D3D11VA：PyAV 会用 ``avcodec_find_decoder()`` 选中不带 hwaccel 的
    ``libdav1d``，而它没有「按名字选解码器」的入口。
    """
    if not hwaccel:
        return False
    return hwaccel in ffmpeg_ctypes.NATIVE_HW_DECODERS.get(codec_name, {})


def _manual_decoder(
    stream: Any,
    decoder_name: str,
    hwaccel: accel.HWAccelLike | None,
    *,
    thread_count: int = 1,
) -> tuple[Any, Any]:
    """按名字新建解码上下文（PLAN.md §3.4：原生解码器 + hwaccel）。"""
    source = stream.codec_context
    kwargs = {"hwaccel": hwaccel} if hwaccel is not None else {}
    context = av.CodecContext.create(decoder_name, "r", **kwargs)

    # 等价于 avcodec_parameters_to_context 里我们能在 PyAV 层做到的部分。
    if source.extradata:
        context.extradata = bytes(source.extradata)
    if source.pkt_timebase:
        context.pkt_timebase = source.pkt_timebase
    for attr in ("profile", "level"):
        value = getattr(source, attr, None)
        if value is not None:
            try:
                setattr(context, attr, value)
            except Exception:
                pass
    for attr in ("width", "height"):
        value = getattr(source, attr, None)
        if value:
            try:
                setattr(context, attr, value)
            except Exception:
                pass

    if thread_count is not None:
        try:
            context.thread_count = thread_count
        except Exception:
            pass
    try:
        # 避免 FFmpeg 帧级多线程额外引入延迟（PLAN.md §5）。
        context.thread_type = "NONE" if thread_count == 1 else "FRAME"
    except Exception:
        pass

    context.open()
    return context, hwaccel


def _ctypes_decoder(
    stream: Any,
    decoder_name: str,
    hwaccel: str,
    *,
    thread_count: int = 1,
) -> Any:
    """用 ctypes 直接驱动 wheel 的 FFmpeg（补上 PyAV 缺失的按名字选解码器）。

    关键差异：**不预设 ``avctx->pix_fmt``**，让 FFmpeg 自己协商到 ``d3d11`` /
    ``videotoolbox_vld``。
    """
    source = stream.codec_context
    try:
        return ffmpeg_ctypes.make_decoder(
            decoder_name,
            width=source.width,
            height=source.height,
            extradata=bytes(source.extradata) if source.extradata else None,
            hwaccel=hwaccel,
            thread_count=thread_count,
        )
    except ffmpeg_ctypes.CtypesFfmpegUnavailable as exc:
        raise AccelError(f"ctypes FFmpeg 后端不可用：{exc}") from exc


def open_session(
    path: str,
    plan: DecodePlan,
    *,
    allow_software_fallback: bool = False,
    thread_count: int = 1,
) -> DecodeSession:
    """按计划打开容器与解码器。设备类型不可用时抛 :class:`accel.AccelError`。"""
    clip = clip_metadata(path)

    hwaccel: Any | None = None
    if plan.hwaccel:
        if not accel.device_type_available(plan.hwaccel):
            raise AccelError(
                f"FFmpeg 构建不支持设备类型 {plan.hwaccel!r}；"
                f"可用：{', '.join(accel.available_device_types()) or '（无）'}"
            )
        hwaccel = accel.make_hwaccel(
            plan.hwaccel,
            allow_software_fallback=allow_software_fallback,
            is_hw_owned=True,
        )

    container = av.open(path)
    try:
        stream = container.streams.video[0]
        codec_name = (clip.get("codec_id") or clip.get("codec") or "").lower()
        if plan.decoder and _needs_ctypes_backend(codec_name, plan.hwaccel):
            decoder_ctx = _ctypes_decoder(
                stream, plan.decoder, plan.hwaccel, thread_count=thread_count
            )
            hwaccel_ref = hwaccel
        elif plan.decoder:
            decoder_ctx, hwaccel_ref = _manual_decoder(
                stream, plan.decoder, hwaccel, thread_count=thread_count
            )
        else:
            decoder_ctx = _container_default_decoder(stream, hwaccel)
            hwaccel_ref = hwaccel
    except Exception:
        container.close()
        raise

    # 保留 hwaccel 引用，避免被 GC 提前回收（PyAV 的上下文不持有它，
    # 而 PyAV 的 Cython 类不允许挂自定义属性）。
    _HWACCEL_REFS[id(decoder_ctx)] = hwaccel_ref

    return DecodeSession(
        container=container,
        stream=stream,
        decoder=decoder_ctx,
        plan=plan,
        clip=clip,
    )


def _decode_packet(session: DecodeSession, packet: Any) -> list[Any]:
    return session.decoder.decode(packet)


def probe_plan(
    path: str,
    *,
    hwaccel: str,
    requested_decoder: str | None = None,
    allow_alt_hwaccel: bool = False,
    allow_software_fallback: bool = False,
    max_probe_packets: int = 4,
) -> ProbeOutcome:
    """按优先级尝试候选解码器，返回第一个真正解出帧的方案。

    这里**用真实素材探测**，而不是只看 ``Codec.hardware_configs`` 是否为空：
    「设备类型被列出」与「解码器真的能硬解」是两件事（PLAN.md §3.2）。
    """
    outcome = ProbeOutcome(session=None)
    clip = clip_metadata(path)
    # 注意：要用「编解码器」（av1/hevc/…），而不是容器默认选出的解码器名
    # （可能是 libdav1d 这类外部软解解码器）。
    codec_name = (clip.get("codec_id") or clip.get("codec") or "").lower()

    hwaccels: list[str] = [hwaccel]
    if allow_alt_hwaccel:
        for alt in accel.ALT_HWACCELS.get(accel.host_platform(), ()):
            if alt != hwaccel and accel.device_type_available(alt):
                hwaccels.append(alt)

    candidates: list[tuple[str, str | None]] = []
    native = ffmpeg_ctypes.NATIVE_HW_DECODERS.get(codec_name, {})
    for hw in hwaccels:
        # 需要 ctypes 后端的组合优先：它按名字选原生解码器，AV1 + D3D11VA 只有它能建起来。
        if requested_decoder in (None, "auto", "") and hw in native:
            candidates.append((hw, native[hw]))
        if requested_decoder in (None, "auto", ""):
            for name in accel.decoder_candidates(codec_name, hw):
                if (hw, name) not in candidates:
                    candidates.append((hw, name))
        else:
            if (hw, requested_decoder) not in candidates:
                candidates.append((hw, requested_decoder))

    for hw, decoder_name in candidates:
        if not accel.device_type_available(hw):
            outcome.attempts.append(
                PlanAttempt(hw, decoder_name, f"设备类型不可用（可用：{accel.available_device_types()}）")
            )
            continue
        plan = DecodePlan(
            hwaccel=hw,
            decoder=decoder_name,
            allow_software_fallback=allow_software_fallback,
        )
        if decoder_name in accel.SOFTWARE_DECODERS and not allow_software_fallback:
            # 与 accel.plan_for_codec 保持一致的把关：软解解码器不能当硬解用。
            outcome.attempts.append(
                PlanAttempt(
                    hw,
                    decoder_name,
                    f"软件解码器（{accel.SOFTWARE_DECODERS[decoder_name]}），"
                    "本项目要求硬解、不回读",
                )
            )
            continue
        try:
            session = open_session(
                path, plan, allow_software_fallback=allow_software_fallback
            )
        except Exception as exc:
            outcome.attempts.append(
                PlanAttempt(hw, decoder_name, f"{type(exc).__name__}: {exc}")
            )
            continue

        try:
            pending = _probe_decode(session, max_probe_packets)
        except Exception as exc:
            session.close()
            outcome.attempts.append(
                PlanAttempt(hw, decoder_name, f"解码首帧失败：{type(exc).__name__}: {exc}")
            )
            continue

        outcome.session = session
        outcome.pending = pending
        return outcome

    return outcome


def _probe_decode(
    session: DecodeSession, max_packets: int
) -> list[tuple[Any, int, int, list[Any]]]:
    """喂入至多 ``max_packets`` 个真实 packet，直到解出第一帧。"""
    pending: list[tuple[Any, int, int, list[Any]]] = []
    for _ in range(max_packets):
        t0 = time.perf_counter_ns()
        packet = next(session.container.demux(session.stream), None)
        t1 = time.perf_counter_ns()
        if packet is None:
            break
        if not packet.size:
            continue
        t2 = time.perf_counter_ns()
        frames = _decode_packet(session, packet)
        t3 = time.perf_counter_ns()
        pending.append((packet, t1 - t0, t3 - t2, frames))
        if frames:
            return pending
    if not pending or not any(entry[3] for entry in pending):
        if not pending:
            raise AccelError("容器里没有可解码的视频 packet")
        raise AccelError(f"连喂 {len(pending)} 个 packet 仍未解出任何帧（疑为解码器不可用）")
    return pending


@dataclass
class ClipRunResult:
    summary: dict[str, Any] | None
    error: str | None
    attempts: list[PlanAttempt]
    frames: int = 0
    readback_detected: bool = False
    hardware_confirmed: bool = False
    hw_pix_fmts: list[str] = field(default_factory=list)


def _hardware_confirmed(decoder_ctx: Any, hw_formats: Sequence[str], readback: bool) -> bool:
    """硬解生效判定：上下文自报硬解 + 出帧格式是硬件格式 + 未检测到回读。"""
    flag = bool(getattr(decoder_ctx, "is_hwaccel", False))
    has_hw_format = any(not accel.is_software_pix_fmt(name) for name in hw_formats)
    return flag and has_hw_format and not readback


def _percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return float("nan")
    return stats.percentile(sorted(values), q)


def _pts_ms(pts: int | None, time_base: Any) -> float | None:
    if pts is None or not time_base:
        return None
    return pts * float(time_base) * 1e3


def expected_frame_count(clip: Mapping[str, Any]) -> int | None:
    """尽力给出「这个片段有多少帧」。

    优先用容器自报的 ``nb_frames``（``stream.frames``），否则用
    ``时长 × 平均帧率``。变帧率素材用平均帧率折算——这里只用来判断
    「是不是已经解完了」，不参与延迟统计。
    """
    hint = clip.get("frame_count_hint")
    if isinstance(hint, int) and hint > 1:
        return hint
    duration = clip.get("duration_s")
    fps = clip.get("avg_frame_rate")
    if isinstance(duration, (int, float)) and duration > 0 and isinstance(fps, (int, float)) and fps > 0:
        return int(duration * fps) + 2
    return None


def run_clip(
    path: str,
    *,
    hwaccel: str,
    out_dir: Any,
    requested_decoder: str | None = None,
    allow_alt_hwaccel: bool = False,
    allow_software_fallback: bool = False,
    tick_ms: float = 33.0,
    fsync: bool = False,
    max_frames: int | None = None,
    max_wall_s: float | None = None,
    thread_count: int = 1,
    skip_first_frames: int = 1,
    on_tick: Any = None,
) -> ClipRunResult:
    """完整跑一个片段：探测 → 解码计时 → 33 ms tick → 写 CSV/JSON。

    :param skip_first_frames: **只在统计口径上**把开头 N 个帧排除（默认 1，
        即排除第 0 帧）。解码流程本身完全不变：帧照解、CSV 照写、帧数照记，
        只是这些帧不参与均值/分位数/抖动/实时倍率。第 0 帧带着解码器初始化
        开销（实测 18~26 ms，稳态均值只有 1~6 ms），留在统计里会把 p99/max
        显著抬高。
    """
    outcome = probe_plan(
        path,
        hwaccel=hwaccel,
        requested_decoder=requested_decoder,
        allow_alt_hwaccel=allow_alt_hwaccel,
        allow_software_fallback=allow_software_fallback,
    )
    if not outcome.ok or outcome.session is None:
        return ClipRunResult(summary=None, error="probe-failed", attempts=outcome.attempts)

    session = outcome.session
    plan = session.plan
    clip = session.clip
    clip_stem = clip["name"].rsplit(".", 1)[0]
    # PyAV 的 demux 生成器在若干 MP4 上到 EOF 后不会再吐出哨兵值（实测本机所有素材），
    # 因此用「预期帧数」作为主循环的正常退出条件；demux 返回 None 只是备用出口。
    expected_frames = max_frames if max_frames is not None else expected_frame_count(clip)
    wall_guard = False

    frame_fh, frame_writer = out_dir.clip_frame_writer(clip_stem, plan.hwaccel)
    tick_fh, tick_writer = out_dir.clip_tick_writer(clip_stem, plan.hwaccel)

    sink = SampleSink(tick_ms / 1e3)
    tick_rows: list[TickRow] = []
    state: dict[str, Any] = {
        "frame_cursor": 0,
        "tick_cursor": 0,
        "tick_index": 0,
        "hw_formats": [],
        "packets_in_tick": 0,
        "frames_total": 0,
    }
    frame_index = 0
    packets = 0
    keyframes = 0
    readback = False

    def note_format(fmt: str | None) -> None:
        if fmt and fmt not in state["hw_formats"]:
            state["hw_formats"].append(fmt)

    def emit_tick(ticker: Any, elapsed_s: float) -> None:
        nonlocal readback
        new, state["frame_cursor"] = sink.since(state["frame_cursor"])
        if new:
            frame_writer.writerows(s.as_row() for s in new)
        frame_fh.flush()
        if fsync:
            os.fsync(frame_fh.fileno())

        window_frames = [s for s in sink.samples[state["tick_cursor"] :] if not s.tail]
        state["tick_cursor"] = len(sink.samples)
        for sample in new:
            note_format(sample.fmt)
            if accel.is_software_pix_fmt(sample.fmt):
                readback = True

        dec = [s.decode_ms for s in window_frames]
        demux = [s.demux_ms for s in window_frames]
        n_packets = state["packets_in_tick"]
        state["packets_in_tick"] = 0
        row = TickRow(
            tick_index=state["tick_index"],
            t_ms=elapsed_s * 1e3,
            t_wall_iso=datetime.now().astimezone().isoformat(timespec="milliseconds"),
            frames_in_tick=len(window_frames),
            frames_total=state["frames_total"],
            packets_in_tick=n_packets,
            decode_ms_last=dec[-1] if dec else float("nan"),
            decode_ms_min=min(dec) if dec else float("nan"),
            decode_ms_max=max(dec) if dec else float("nan"),
            decode_ms_avg=(math.fsum(dec) / len(dec)) if dec else float("nan"),
            decode_ms_p50=_percentile(dec, 50.0),
            decode_ms_p95=_percentile(dec, 95.0),
            fps_inst=(len(window_frames) / (tick_ms / 1e3)) if window_frames else 0.0,
            demux_ms_avg=(math.fsum(demux) / len(demux)) if demux else float("nan"),
            wall_ms=elapsed_s * 1e3,
            tick_drift_ms=ticker.drift_ms,
        )
        tick_writer.writerow(row.as_row())
        tick_fh.flush()
        if fsync:
            os.fsync(tick_fh.fileno())
        tick_rows.append(row)
        state["tick_index"] += 1
        if on_tick is not None:
            on_tick(row)

    ticker = Ticker(tick_ms / 1e3, emit_tick, fsync=fsync)
    wall_start = time.perf_counter()
    ticker.start()

    pending = list(outcome.pending)
    tail_frames = 0
    decoder_ctx = session.decoder
    try:
        while True:
            if pending:
                _packet, demux_ns, decode_ns, frames = pending.pop(0)
            else:
                t0 = time.perf_counter_ns()
                packet = next(session.container.demux(session.stream), None)
                t1 = time.perf_counter_ns()
                if packet is None:
                    break
                if not packet.size:
                    continue
                demux_ns = t1 - t0
                t2 = time.perf_counter_ns()
                frames = _decode_packet(session, packet)
                t3 = time.perf_counter_ns()
                decode_ns = t3 - t2

            packets += 1
            state["packets_in_tick"] += 1
            sink.count_packet()
            time_base = session.stream.time_base
            samples: list[FrameSample] = []
            for frame in frames:
                fmt = frame.format.name if frame.format else None
                note_format(fmt)
                is_key = bool(getattr(frame, "key_frame", False))
                if is_key:
                    keyframes += 1
                samples.append(
                    FrameSample(
                        index=frame_index,
                        packet_index=packets - 1,
                        pts=frame.pts,
                        pts_ms=_pts_ms(frame.pts, time_base),
                        demux_ns=demux_ns,
                        decode_ns=decode_ns,
                        frames_out=len(frames),
                        keyframe=is_key,
                        pict_type=str(frame.pict_type) if frame.pict_type else None,
                        fmt=fmt,
                        tail=False,
                    )
                )
                frame_index += 1
            state["frames_total"] = frame_index
            sink.extend(samples)
            if expected_frames is not None and frame_index >= expected_frames:
                break
            if max_wall_s is not None and time.perf_counter() - wall_start > max_wall_s:
                wall_guard = True
                break

        # 排空流水线：尾部帧单独标记（PLAN.md §5）。
        t2 = time.perf_counter_ns()
        tail_list = decoder_ctx.decode(None)
        t3 = time.perf_counter_ns()
        tail_samples: list[FrameSample] = []
        time_base = session.stream.time_base
        for frame in tail_list:
            fmt = frame.format.name if frame.format else None
            note_format(fmt)
            tail_samples.append(
                FrameSample(
                    index=frame_index,
                    packet_index=packets,
                    pts=frame.pts,
                    pts_ms=_pts_ms(frame.pts, time_base),
                    demux_ns=0,
                    decode_ns=t3 - t2,
                    frames_out=max(1, len(tail_list)),
                    keyframe=bool(getattr(frame, "key_frame", False)),
                    pict_type=str(frame.pict_type) if frame.pict_type else None,
                    fmt=fmt,
                    tail=True,
                )
            )
            frame_index += 1
        tail_frames = len(tail_samples)
        state["frames_total"] = frame_index
        sink.extend(tail_samples)
    finally:
        wall_s = time.perf_counter() - wall_start
        ticker.stop()
        sink.request_close()
        # 收尾：把最后一个 tick 之后产生的样本也写进 CSV。
        new, state["frame_cursor"] = sink.since(state["frame_cursor"])
        if new:
            frame_writer.writerows(s.as_row() for s in new)
        frame_fh.flush()
        if fsync:
            os.fsync(frame_fh.fileno())
        frame_fh.close()
        tick_fh.close()
        session.close()

    hw_formats = list(state["hw_formats"])
    for name in hw_formats:
        if accel.is_software_pix_fmt(name):
            readback = True
    confirmed = _hardware_confirmed(decoder_ctx, hw_formats, readback)
    backend_info = decoder_ctx.as_dict() if hasattr(decoder_ctx, "as_dict") else None

    ticks = summarize_ticks(tick_rows, tick_ms, wall_s * 1e3)
    summary = build_clip_summary(
        clip=clip,
        plan=plan,
        attempts=outcome.attempts,
        samples=sink.samples,
        frames_total=frame_index,
        packets_total=packets,
        wall_s=wall_s,
        hardware_confirmed=confirmed,
        readback_detected=readback,
        hw_pix_fmts=hw_formats,
        ticks=ticks,
        keyframe_count=keyframes,
        tail_frames=tail_frames,
        stop_reason=(
            "wall-time-guard"
            if wall_guard
            else ("max-frames" if max_frames is not None else "expected-frame-count")
        ),
        expected_frames=expected_frames,
        backend=backend_info,
        skip_first_frames=skip_first_frames,
    )
    summary["sample_trimmed"] = sink.trimmed
    return ClipRunResult(
        summary=summary,
        error=None,
        attempts=outcome.attempts,
        frames=frame_index,
        readback_detected=readback,
        hardware_confirmed=confirmed,
        hw_pix_fmts=hw_formats,
    )



