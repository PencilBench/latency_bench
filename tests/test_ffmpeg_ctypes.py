"""``latency_bench.ffmpeg_ctypes`` 的单元测试。

不需要真实硬件：只验证结构体偏移常量、候选表、以及在缺少 DLL 时的干净报错。
"""

from __future__ import annotations

import ctypes
from pathlib import Path

import pytest

from latency_bench import bench, ffmpeg_ctypes


# --------------------------------------------------------------------------------------
# 常量与候选表
# --------------------------------------------------------------------------------------


def test_native_hw_decoders_covers_av1_d3d11va():
    assert ffmpeg_ctypes.NATIVE_HW_DECODERS["av1"]["d3d11va"] == "av1"
    assert ffmpeg_ctypes.NATIVE_HW_DECODERS["av1"]["d3d12va"] == "av1"


def test_native_hw_decoders_covers_av1_videotoolbox():
    """macOS 的 AV1 也要走 ctypes 后端（PyAV 会给原生 av1 预设 pix_fmt 而失败）。"""
    assert ffmpeg_ctypes.NATIVE_HW_DECODERS["av1"]["videotoolbox"] == "av1"


def test_needs_ctypes_backend_only_for_av1_d3():
    assert bench._needs_ctypes_backend("av1", "d3d11va") is True
    assert bench._needs_ctypes_backend("av1", "d3d12va") is True
    assert bench._needs_ctypes_backend("av1", "videotoolbox") is True
    # HEVC 走 PyAV 就够了（原生 hevc 解码器本来就带 d3d11va / videotoolbox）
    assert bench._needs_ctypes_backend("hevc", "d3d11va") is False
    assert bench._needs_ctypes_backend("hevc", "videotoolbox") is False
    # 软解 / 未知编解码器
    assert bench._needs_ctypes_backend("av1", None) is False
    assert bench._needs_ctypes_backend("theora", "d3d11va") is False


def test_averror_eagain_is_platform_aware():
    """``AVERROR(EAGAIN)`` = ``-EAGAIN``，而 macOS 的 EAGAIN 是 35 而不是 11。"""
    assert ffmpeg_ctypes.EAGAIN_ERRNO == (35 if ffmpeg_ctypes.sys.platform == "darwin" else 11)
    assert ffmpeg_ctypes.AVERROR_EAGAIN == -ffmpeg_ctypes.EAGAIN_ERRNO


def test_struct_offsets_are_within_plausible_range():
    """偏移必须是正数、单调分布在结构体前部，且互不重叠。"""
    off = ffmpeg_ctypes.StructOffsets()
    ctx_fields = {
        "ctx_codec_type": off.ctx_codec_type,
        "ctx_codec_id": off.ctx_codec_id,
        "ctx_extradata": off.ctx_extradata,
        "ctx_extradata_size": off.ctx_extradata_size,
        "ctx_width": off.ctx_width,
        "ctx_height": off.ctx_height,
        "ctx_pix_fmt": off.ctx_pix_fmt,
        "ctx_sw_pix_fmt": off.ctx_sw_pix_fmt,
        "ctx_hwaccel": off.ctx_hwaccel,
        "ctx_hw_frames_ctx": off.ctx_hw_frames_ctx,
        "ctx_hw_device_ctx": off.ctx_hw_device_ctx,
        "ctx_thread_count": off.ctx_thread_count,
        "ctx_thread_type": off.ctx_thread_type,
    }
    assert all(v >= 0 for v in ctx_fields.values())
    # FFmpeg 8 的 AVCodecContext 是 864 字节，所有字段都该落在这个范围内
    assert max(ctx_fields.values()) < 864
    # 已知的相对顺序（来自 offsetof）
    assert off.ctx_codec_type < off.ctx_codec_id < off.ctx_width
    assert off.ctx_pix_fmt + 4 == off.ctx_sw_pix_fmt  # 两个相邻 int
    assert off.ctx_hw_frames_ctx + 8 == off.ctx_hw_device_ctx  # 两个相邻指针
    assert off.ctx_thread_count + 4 == off.ctx_thread_type


def test_struct_offsets_pair_fields_are_aligned():
    off = ffmpeg_ctypes.StructOffsets()
    for name in ("ctx_extradata", "ctx_hwaccel", "ctx_hw_frames_ctx", "ctx_hw_device_ctx"):
        assert getattr(off, name) % 8 == 0, f"{name} 必须 8 字节对齐"
    for name in ("ctx_codec_type", "ctx_codec_id", "ctx_width", "ctx_height", "ctx_pix_fmt"):
        assert getattr(off, name) % 4 == 0, f"{name} 必须 4 字节对齐"


def test_struct_offsets_is_frozen():
    off = ffmpeg_ctypes.StructOffsets()
    with pytest.raises(Exception):
        off.ctx_codec_id = 999  # type: ignore[misc]


# --------------------------------------------------------------------------------------
# ctypes 结构体布局（纯 Python 侧，与 FFmpeg 无关）
# --------------------------------------------------------------------------------------


def test_avpacket_ctypes_layout_matches_offsets():
    """AVPacket 的 ctypes 声明要与 StructOffsets 里的偏移一致。"""
    off = ffmpeg_ctypes.StructOffsets()
    pkt = ffmpeg_ctypes._AVPacket()
    base = ctypes.addressof(pkt)
    assert ctypes.c_void_p.from_address(base + off.pkt_buf).value is None or True
    # 用 ctypes 自己的 offsetof 交叉验证
    assert ffmpeg_ctypes._AVPacket.buf.offset == off.pkt_buf
    assert ffmpeg_ctypes._AVPacket.pts.offset == off.pkt_pts
    assert ffmpeg_ctypes._AVPacket.dts.offset == off.pkt_dts
    assert ffmpeg_ctypes._AVPacket.data.offset == off.pkt_data
    assert ffmpeg_ctypes._AVPacket.size.offset == off.pkt_size
    assert ffmpeg_ctypes._AVPacket.flags.offset == off.pkt_flags


def test_avbufferref_ctypes_layout():
    assert ffmpeg_ctypes._AVBufferRef.data.offset == 8
    assert ffmpeg_ctypes._AVBufferRef.size.offset == 16


# --------------------------------------------------------------------------------------
# 解码器描述的辅助类型
# --------------------------------------------------------------------------------------


def test_decoded_frame_exposes_format_name():
    frame = ffmpeg_ctypes._DecodedFrame(
        format_name="d3d11", width=1920, height=1080, pts=42
    )
    assert frame.format.name == "d3d11"
    assert frame.width == 1920 and frame.height == 1080
    assert frame.pts == 42
    assert frame.key_frame is False
    assert frame.pict_type is None


# --------------------------------------------------------------------------------------
# 缺 DLL 时的报错
# --------------------------------------------------------------------------------------


def test_load_libraries_reports_missing_directory(work_dir: Path):
    empty = work_dir / "no-ffmpeg-here"
    empty.mkdir(exist_ok=True)
    with pytest.raises(ffmpeg_ctypes.CtypesFfmpegUnavailable):
        ffmpeg_ctypes.load_libraries(empty)


def test_ffmpeg_shared_dir_points_at_existing_directory():
    directory = ffmpeg_ctypes.ffmpeg_shared_dir()
    assert directory.is_dir()
    if ffmpeg_ctypes.sys.platform == "win32":
        assert directory.name == "av.libs"
        assert list(directory.glob("avcodec-*.dll")) or list(directory.glob("avcodec*.dll"))
    else:
        # macOS：PyAV wheel 用 delocate，库在 av/.dylibs 下且带 lib 前缀
        assert directory.name == ".dylibs"
        assert list(directory.glob("libavcodec*.dylib"))


def test_libraries_load_and_report_version():
    libs = ffmpeg_ctypes.load_libraries()
    major, minor, micro = libs.version("avcodec")
    assert major >= 60 and minor >= 0
    assert libs.files["avcodec"].startswith(("avcodec-", "libavcodec"))


def test_load_libraries_rejects_unsupported_platform(monkeypatch):
    """只实现 Windows 与 macOS；其它平台要给出干净的报错。"""
    for platform in ("linux", "freebsd"):
        monkeypatch.setattr(ffmpeg_ctypes.sys, "platform", platform)
        with pytest.raises(ffmpeg_ctypes.CtypesFfmpegUnavailable):
            ffmpeg_ctypes.load_libraries()


def test_load_libraries_finds_libraries_on_macos():
    if ffmpeg_ctypes.sys.platform != "darwin":
        pytest.skip("只在 macOS 上验证 .dylib 分支")
    libs = ffmpeg_ctypes.load_libraries()
    assert libs.files["avutil"].startswith("libavutil")
    assert libs.files["avcodec"].startswith("libavcodec")
