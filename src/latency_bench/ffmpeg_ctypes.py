"""用 ctypes 直接驱动 PyAV wheel 自带的 FFmpeg —— 补上 PyAV 缺失的「按名字选解码器」。

为什么要这么做（本机实测，见 README「已知现象」）：

* FFmpeg 的 AV1 解码器列表里 ``libdav1d`` 排在原生 ``av1`` 前面，而 ``libdav1d``
  不带 hwaccel；PyAV 用 ``avcodec_find_decoder()`` 选解码器，没有「按名字选」的入口。
* 原生 ``av1`` 解码器在这套构建里**确实带 D3D11VA**（avcodec.dll 里有
  ``av1_d3d11va`` / ``av1_d3d11va2`` / ``av1_dxva2``），但 PyAV 在建上下文时
  会把 ``avctx->pix_fmt`` 预设成硬件格式 ``d3d11``，这会让 AV1 解码器的
  ``ff_get_format()`` 协商失败，最终在 ``get_buffer()`` 报 EINVAL。

修法是两条：**按名字选解码器**，以及**不要预设 pix_fmt**（让 FFmpeg 自己协商）。
这样做之后 AV1 + D3D11VA 正常工作：出帧格式为 ``d3d11``，1080p 约 936 fps、4K 约 256 fps。
macOS 上的 AV1 + VideoToolbox 同理：PyAV 会把 ``avctx->pix_fmt`` 预设成
``videotoolbox`` 并让 ``avcodec_send_packet()`` 返回 EINVAL(22)，走本后端则能正常
协商出 ``videotoolbox_vld`` 帧（实测 M6，1080p 约 716 fps）。

实现要点：解复用仍交给 PyAV（它工作正常），只把「解码」这一步换成 ctypes 调用，
因此本项目其余部分（33 ms ticker、CSV/JSON、统计）完全不用改。
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = [
    "CtypesFfmpegUnavailable",
    "FfmpegLibraries",
    "StructOffsets",
    "CtypesDecoder",
    "load_libraries",
    "ffmpeg_shared_dir",
    "NATIVE_HW_DECODERS",
]

# 这些「编解码器 × 设备类型」组合需要 ctypes 后端（PyAV 选不到原生解码器）
NATIVE_HW_DECODERS: dict[str, dict[str, str]] = {
    "av1": {"d3d11va": "av1", "d3d12va": "av1", "videotoolbox": "av1"},
}


class CtypesFfmpegUnavailable(RuntimeError):
    """ctypes 后端不可用（找不到 DLL、ABI 校验失败等）。"""


# --------------------------------------------------------------------------------------
# 结构体偏移
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StructOffsets:
    """FFmpeg 结构体字段偏移。

    这些值来自 FFmpeg 8.0 公开头文件的编译期 ``offsetof``，
    并在运行期用「已知字段哨兵值」校验（见 :meth:`CtypesDecoder.verify`）。
    """

    # AVCodecContext
    ctx_width: int = 112
    ctx_height: int = 116
    ctx_pix_fmt: int = 136
    ctx_sw_pix_fmt: int = 140
    ctx_extradata: int = 72
    ctx_extradata_size: int = 80
    ctx_codec_type: int = 12
    ctx_codec_id: int = 24
    ctx_hwaccel: int = 536
    ctx_hw_frames_ctx: int = 552
    ctx_hw_device_ctx: int = 560
    ctx_thread_count: int = 656
    ctx_thread_type: int = 660
    # AVPacket
    pkt_buf: int = 0
    pkt_pts: int = 8
    pkt_dts: int = 16
    pkt_data: int = 24
    pkt_size: int = 32
    pkt_flags: int = 40
    # AVFrame
    frame_width: int = 104
    frame_height: int = 108
    frame_format: int = 116
    frame_pts: int = 136
    frame_flags: int = 276


AVMEDIA_TYPE_VIDEO = 0
AV_PIX_FMT_D3D11 = 171
AVERROR_EOF = -541478725

#: ``AVERROR(EAGAIN)`` 就是 ``-EAGAIN``，而 ``EAGAIN`` 是**平台相关**的：
#: Linux/Windows 上是 11，macOS（BSD errno）上是 35。写死 -11 会让 macOS 上
#: 「解码器暂时没有输出帧」这一正常情况被当成致命错误（实测
#: ``avcodec_receive_frame()`` 在 VideoToolbox 上返回 -35）。
EAGAIN_ERRNO: int = 35 if sys.platform == "darwin" else 11
AVERROR_EAGAIN: int = -EAGAIN_ERRNO
AV_PKT_FLAG_KEY = 1
AV_INPUT_BUFFER_PADDING_SIZE = 64

#: 每个解码器的 (codec_type, codec_id) 哨兵值，用于校验结构体偏移是否正确。
_CODEC_SENTINELS: dict[str, int] = {
    "av1": 222,
    "hevc": 172,
    "h264": 27,
}


# --------------------------------------------------------------------------------------
# 库加载
# --------------------------------------------------------------------------------------


@dataclass
class FfmpegLibraries:
    """已加载的 FFmpeg 动态库句柄。"""

    directory: Path
    avutil: ctypes.CDLL
    avcodec: ctypes.CDLL
    files: dict[str, str] = field(default_factory=dict)

    def version(self, which: str = "avcodec") -> tuple[int, int, int]:
        fn = getattr(self, which).__getattr__(f"{which}_version")
        v = fn()
        return v >> 16, (v >> 8) & 0xFF, v & 0xFF


def ffmpeg_shared_dir() -> Path:
    """PyAV wheel 自带的 FFmpeg 动态库目录。

    Windows（delvewheel）放在 ``av.libs``；macOS（delocate）放在 ``av/.dylibs``。
    """
    import av as _av

    pkg_dir = Path(_av.__file__).resolve().parent
    for candidate in (
        pkg_dir / ".dylibs",
        pkg_dir / ".." / "av.libs",
        pkg_dir.parent / "av.libs",
    ):
        if candidate.is_dir():
            return candidate.resolve()
    raise CtypesFfmpegUnavailable(
        f"找不到 PyAV 的 FFmpeg 动态库目录，已查：{pkg_dir / '.dylibs'}、"
        f"{pkg_dir.parent / 'av.libs'}"
    )


def _find_library(directory: Path, stem: str) -> Path:
    """按平台命名约定找单个 FFmpeg 动态库（Windows ``avcodec-*.dll`` / macOS ``libavcodec*.dylib``）。"""
    patterns = (
        (f"{stem}-*.dll", f"{stem}*.dll")
        if sys.platform == "win32"
        else (f"lib{stem}-*.dylib", f"lib{stem}*.dylib", f"{stem}*.dylib")
    )
    for pattern in patterns:
        matches = sorted(directory.glob(pattern))
        if matches:
            return matches[0]
    raise CtypesFfmpegUnavailable(
        f"在 {directory} 里找不到 {stem} 动态库（试过：{', '.join(patterns)}）"
    )


def load_libraries(directory: Path | None = None) -> FfmpegLibraries:
    """加载 avutil / avcodec（Windows 的 .dll 与 macOS 的 .dylib）。"""
    if sys.platform not in ("win32", "darwin"):
        raise CtypesFfmpegUnavailable(
            f"ctypes FFmpeg 后端只实现 Windows（d3d11va）与 macOS（videotoolbox）路径，"
            f"当前平台 {sys.platform!r} 不受支持"
        )
    directory = directory or ffmpeg_shared_dir()
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(str(directory))

    avutil_path = _find_library(directory, "avutil")
    avcodec_path = _find_library(directory, "avcodec")
    # avcodec 依赖 avutil，先把 avutil 加载进来
    avutil = ctypes.CDLL(str(avutil_path))
    avcodec = ctypes.CDLL(str(avcodec_path))
    return FfmpegLibraries(
        directory=directory,
        avutil=avutil,
        avcodec=avcodec,
        files={"avutil": avutil_path.name, "avcodec": avcodec_path.name},
    )


# --------------------------------------------------------------------------------------
# ctypes 结构
# --------------------------------------------------------------------------------------


class _AVBufferRef(ctypes.Structure):
    _fields_ = [
        ("buffer", ctypes.c_void_p),
        ("data", ctypes.POINTER(ctypes.c_uint8)),
        ("size", ctypes.c_size_t),
    ]


class _AVPacket(ctypes.Structure):
    """字段顺序与 FFmpeg 8 一致；实际读写用显式偏移，避免依赖这里。"""

    _fields_ = [
        ("buf", ctypes.POINTER(_AVBufferRef)),
        ("pts", ctypes.c_int64),
        ("dts", ctypes.c_int64),
        ("data", ctypes.POINTER(ctypes.c_uint8)),
        ("size", ctypes.c_int),
        ("stream_index", ctypes.c_int),
        ("flags", ctypes.c_int),
        ("side_data", ctypes.c_void_p),
        ("side_data_elems", ctypes.c_int),
    ]


def _proto(lib: ctypes.CDLL, name: str, restype: Any, argtypes: list[Any]) -> Any:
    try:
        fn = getattr(lib, name)
    except AttributeError as exc:
        raise CtypesFfmpegUnavailable(f"FFmpeg 库里没有符号 {name}") from exc
    fn.restype = restype
    fn.argtypes = argtypes
    return fn


# --------------------------------------------------------------------------------------
# 解码器
# --------------------------------------------------------------------------------------


class CtypesDecoder:
    """按名字选解码器的 FFmpeg 解码上下文。

    接口与 PyAV 的 ``CodecContext`` 对齐：``decode(packet)`` / ``decode(None)`` /
    ``is_hwaccel`` / ``format``，因此可以无缝替换进 :mod:`latency_bench.bench`。
    """

    def __init__(
        self,
        libs: FfmpegLibraries,
        decoder_name: str,
        *,
        width: int,
        height: int,
        extradata: bytes | None = None,
        hwaccel: str | None = "d3d11va",
        thread_count: int = 1,
        offsets: StructOffsets | None = None,
        set_pix_fmt: bool = False,
    ) -> None:
        self.libs = libs
        self.offsets = offsets or StructOffsets()
        self.decoder_name = decoder_name
        self.hwaccel = hwaccel
        self.frames_decoded = 0
        self.decode_calls = 0
        self.output_formats: list[str] = []
        self.last_error: str | None = None
        self._bind()  # 必须先绑定，_validate_offsets 会用到库函数
        self._validate_offsets(decoder_name)

        self._first_frame_info: tuple[str, int, int] | None = None

        decoder = self._find_decoder(decoder_name.encode())
        if not decoder:
            raise CtypesFfmpegUnavailable(f"FFmpeg 里找不到解码器 {decoder_name!r}")
        self._decoder = decoder

        ctx = self._alloc_context(decoder)
        if not ctx:
            raise CtypesFfmpegUnavailable("avcodec_alloc_context3 失败")
        self._ctx = ctx
        self._ctx_ref = ctypes.c_void_p(ctx)
        self._hw_device = None
        self._extradata_buf = None

        try:
            self._configure(width, height, extradata, hwaccel, thread_count)
            rc = self._open2(ctx, decoder, None)
            if rc < 0:
                raise CtypesFfmpegUnavailable(
                    f"avcodec_open2({decoder_name}) 失败: {self._err(rc)}"
                )
        except Exception:
            self.close()
            raise

        self._frame = self._frame_alloc()
        if not self._frame:
            self.close()
            raise CtypesFfmpegUnavailable("av_frame_alloc 失败")
        self._live_packet: Any = None

    # -- 生命周期 -------------------------------------------------------------
    def close(self) -> None:
        if getattr(self, "_live_packet", None) is not None:
            self._packet_free(ctypes.byref(self._live_packet))
            self._live_packet = None
        if getattr(self, "_frame", None):
            ptr = ctypes.c_void_p(self._frame)
            self._frame_free(ctypes.byref(ptr))
            self._frame = None
        if getattr(self, "_ctx", None):
            self._free_context(ctypes.byref(self._ctx_ref))
            self._ctx = None

    def __del__(self):  # pragma: no cover - 尽力而为
        try:
            self.close()
        except Exception:
            pass

    # -- 属性 -----------------------------------------------------------------
    @property
    def is_hwaccel(self) -> bool:
        """解码器确实拿到了硬件加速上下文。"""
        return bool(self._hw_device) and self.hwaccel is not None

    @property
    def is_open(self) -> bool:
        return bool(self._ctx) and bool(self._is_open(self._ctx))

    @property
    def format(self) -> Any:
        return _FormatName(self.output_formats[-1] if self.output_formats else None)

    @property
    def name(self) -> str:
        return self.decoder_name

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend": "ctypes-ffmpeg",
            "decoder": self.decoder_name,
            "hwaccel": self.hwaccel,
            "ffmpeg_files": self.libs.files,
            "output_formats": sorted(set(self.output_formats)),
            "first_frame": self._first_frame_info,
        }

    # -- 解码 -----------------------------------------------------------------
    def decode(self, packet: Any | None) -> list[Any]:
        """``packet`` 可以是 PyAV 的 ``Packet``，也可以是 ``None``（排空）。"""
        self.decode_calls += 1
        out: list[_DecodedFrame] = []

        if packet is None:
            # 排空：发一个空包
            rc = self._send_packet(self._ctx, None)
            if rc < 0 and rc not in (AVERROR_EOF, AVERROR_EAGAIN):
                self.last_error = self._err(rc)
                return []
            out.extend(self._drain())
            return out

        payload = bytes(packet) if not isinstance(packet, (bytes, bytearray)) else bytes(packet)
        pts = getattr(packet, "pts", None)
        dts = getattr(packet, "dts", None)
        key = bool(getattr(packet, "is_keyframe", False))

        if getattr(self, "_live_packet", None) is not None:
            self._packet_free(ctypes.byref(self._live_packet))
            self._live_packet = None
        pkt = self._make_packet(payload, pts, dts, key)
        self._live_packet = pkt

        rc = self._send_packet(self._ctx, pkt)
        if rc < 0:
            self.last_error = self._err(rc)
            raise RuntimeError(f"avcodec_send_packet 失败: {self.last_error}")
        return self._drain()

    def _drain(self) -> list[Any]:
        out: list[Any] = []
        while True:
            rc = self._receive_frame(self._ctx, self._frame)
            if rc == AVERROR_EAGAIN or rc == AVERROR_EOF:
                break
            if rc < 0:
                self.last_error = self._err(rc)
                raise RuntimeError(f"avcodec_receive_frame 失败: {self.last_error}")
            base = self._frame
            fmt = self._read_int(base, self.offsets.frame_format)
            name = self._pix_name(fmt)
            width = self._read_int(base, self.offsets.frame_width)
            height = self._read_int(base, self.offsets.frame_height)
            pts = ctypes.c_int64.from_address(base + self.offsets.frame_pts).value
            self.output_formats.append(name)
            if self._first_frame_info is None:
                self._first_frame_info = (name, width, height)
            out.append(_DecodedFrame(format_name=name, width=width, height=height, pts=pts))
            self.frames_decoded += 1
            self._frame_unref(self._frame)
        return out

    # -- 内部 -----------------------------------------------------------------
    def _bind(self) -> None:
        libs = self.libs
        avu, avc = libs.avutil, libs.avcodec
        void_p, int_, i64 = ctypes.c_void_p, ctypes.c_int, ctypes.c_int64
        buf_ref_p = ctypes.POINTER(_AVBufferRef)
        c_bool = ctypes.c_int
        self._find_decoder = _proto(avc, "avcodec_find_decoder_by_name", void_p, [ctypes.c_char_p])
        self._alloc_context = _proto(avc, "avcodec_alloc_context3", void_p, [void_p])
        self._free_context = _proto(avc, "avcodec_free_context", None, [ctypes.POINTER(void_p)])
        self._open2 = _proto(avc, "avcodec_open2", int_, [void_p, void_p, void_p])
        self._send_packet = _proto(
            avc, "avcodec_send_packet", int_, [void_p, ctypes.POINTER(_AVPacket)]
        )
        self._receive_frame = _proto(avc, "avcodec_receive_frame", int_, [void_p, void_p])
        self._packet_alloc = _proto(avc, "av_packet_alloc", ctypes.POINTER(_AVPacket), [])
        self._packet_free = _proto(
            avc, "av_packet_free", None, [ctypes.POINTER(ctypes.POINTER(_AVPacket))]
        )
        self._get_name = _proto(avc, "avcodec_get_name", ctypes.c_char_p, [int_])
        self._is_open = _proto(avc, "avcodec_is_open", c_bool, [void_p])

        self._hwdevice_create = _proto(
            avu,
            "av_hwdevice_ctx_create",
            int_,
            [ctypes.POINTER(buf_ref_p), int_, ctypes.c_char_p, void_p, int_],
        )
        self._hwdevice_find = _proto(
            avu, "av_hwdevice_find_type_by_name", int_, [ctypes.c_char_p]
        )
        self._buffer_alloc = _proto(avu, "av_buffer_alloc", buf_ref_p, [ctypes.c_size_t])
        self._frame_alloc = _proto(avu, "av_frame_alloc", void_p, [])
        self._frame_unref = _proto(avu, "av_frame_unref", None, [void_p])
        self._frame_free = _proto(avu, "av_frame_free", None, [ctypes.POINTER(void_p)])
        self._pix_name = self._pix_fmt_name
        self._strerror = _proto(avu, "av_strerror", int_, [int_, ctypes.c_char_p, ctypes.c_size_t])
        avu.av_log_set_level(16)  # AV_LOG_ERROR

    def _pix_fmt_name(self, fmt: int) -> str:
        fn = getattr(self.libs.avutil, "av_get_pix_fmt_name")
        fn.restype = ctypes.c_char_p
        fn.argtypes = [ctypes.c_int]
        name = fn(fmt)
        return name.decode("utf-8", "replace") if name else str(fmt)

    def _err(self, code: int) -> str:
        buf = ctypes.create_string_buffer(256)
        self._strerror(code, buf, 256)
        return f"{code} ({buf.value.decode('utf-8', 'replace')})"

    def _read_int(self, base: int, offset: int) -> int:
        return ctypes.c_int.from_address(base + offset).value

    def _write_int(self, base: int, offset: int, value: int) -> None:
        ctypes.c_int.from_address(base + offset).value = value

    def _read_ptr(self, base: int, offset: int) -> int:
        return ctypes.c_void_p.from_address(base + offset).value or 0

    def _write_ptr(self, base: int, offset: int, value: int) -> None:
        ctypes.c_void_p.from_address(base + offset).value = value

    def _validate_offsets(self, decoder_name: str) -> None:
        """用解码器的 (codec_type, codec_id) 哨兵值校验偏移是否与运行库一致。

        ``avcodec_alloc_context3`` 会把这两个字段设成已知值，所以只要它们读出来
        正确，同一区域的其它偏移（pix_fmt / hw_device_ctx …）也是可信的。
        """
        codec_id = _CODEC_SENTINELS.get(decoder_name)
        if codec_id is None:
            return
        libraries = getattr(self, "libs")
        alloc = _proto(libraries.avcodec, "avcodec_alloc_context3", ctypes.c_void_p, [ctypes.c_void_p])
        free = _proto(
            libraries.avcodec, "avcodec_free_context", None, [ctypes.POINTER(ctypes.c_void_p)]
        )
        find = _proto(
            libraries.avcodec, "avcodec_find_decoder_by_name", ctypes.c_void_p, [ctypes.c_char_p]
        )
        decoder = find(decoder_name.encode())
        if not decoder:
            return
        ctx = alloc(decoder)
        if not ctx:
            return
        try:
            ctype = self._read_int(ctx, self.offsets.ctx_codec_type)
            cid = self._read_int(ctx, self.offsets.ctx_codec_id)
        finally:
            ref = ctypes.c_void_p(ctx)
            free(ctypes.byref(ref))
        if ctype != AVMEDIA_TYPE_VIDEO or cid != codec_id:
            raise CtypesFfmpegUnavailable(
                "FFmpeg 结构体偏移与本机运行库不匹配"
                f"（期望 codec_type={AVMEDIA_TYPE_VIDEO} codec_id={codec_id}，"
                f"实得 codec_type={ctype} codec_id={cid}）。"
                "请更新 latency_bench.ffmpeg_ctypes.StructOffsets。"
            )

    def _configure(
        self,
        width: int,
        height: int,
        extradata: bytes | None,
        hwaccel: str | None,
        thread_count: int,
    ) -> None:
        ctx = self._ctx
        off = self.offsets
        self._write_int(ctx, off.ctx_width, width)
        self._write_int(ctx, off.ctx_height, height)
        self._write_int(ctx, off.ctx_thread_count, thread_count)
        self._write_int(ctx, off.ctx_thread_type, 0)  # FF_THREAD_NONE

        # 注意：这里**故意不设置 avctx->pix_fmt**。
        # 预设成硬件格式会让 AV1 解码器的 ff_get_format() 协商失败（get_buffer EINVAL）；
        # 留空则由 FFmpeg 自己协商到 d3d11。
        if extradata:
            buf = self._buffer_alloc(len(extradata) + AV_INPUT_BUFFER_PADDING_SIZE)
            if not buf:
                raise CtypesFfmpegUnavailable("av_buffer_alloc(extradata) 失败")
            ctypes.memmove(buf.contents.data, extradata, len(extradata))
            ctypes.memset(
                ctypes.addressof(buf.contents.data.contents) + len(extradata),
                0,
                AV_INPUT_BUFFER_PADDING_SIZE,
            )
            self._extradata_buf = buf
            self._write_ptr(ctx, off.ctx_extradata, ctypes.cast(buf.contents.data, ctypes.c_void_p).value)
            self._write_int(ctx, off.ctx_extradata_size, len(extradata))

        if hwaccel:
            hw_type = self._hwdevice_find(hwaccel.encode())
            if hw_type < 0:
                raise CtypesFfmpegUnavailable(f"本构建不支持 hwaccel {hwaccel!r}")
            device = ctypes.POINTER(_AVBufferRef)()
            rc = self._hwdevice_create(ctypes.byref(device), hw_type, None, None, 0)
            if rc < 0:
                raise CtypesFfmpegUnavailable(
                    f"av_hwdevice_ctx_create({hwaccel}) 失败: {self._err(rc)}"
                )
            self._hw_device = device
            self._write_ptr(
                ctx, off.ctx_hw_device_ctx, ctypes.cast(device, ctypes.c_void_p).value
            )

    def _make_packet(self, payload: bytes, pts: Any, dts: Any, key: bool) -> Any:
        pkt = self._packet_alloc()
        if not pkt:
            raise CtypesFfmpegUnavailable("av_packet_alloc 失败")
        addr = ctypes.addressof(pkt.contents)
        buf = self._buffer_alloc(len(payload) + AV_INPUT_BUFFER_PADDING_SIZE)
        if not buf:
            self._packet_free(ctypes.byref(pkt))
            raise CtypesFfmpegUnavailable("av_buffer_alloc(packet) 失败")
        ctypes.memmove(buf.contents.data, payload, len(payload))
        ctypes.memset(
            ctypes.addressof(buf.contents.data.contents) + len(payload),
            0,
            AV_INPUT_BUFFER_PADDING_SIZE,
        )
        self._write_ptr(addr, self.offsets.pkt_buf, ctypes.cast(buf, ctypes.c_void_p).value)
        self._write_ptr(
            addr, self.offsets.pkt_data, ctypes.cast(buf.contents.data, ctypes.c_void_p).value
        )
        self._write_int(addr, self.offsets.pkt_size, len(payload))
        self._write_int(addr, self.offsets.pkt_flags, AV_PKT_FLAG_KEY if key else 0)
        ctypes.c_int64.from_address(addr + self.offsets.pkt_pts).value = (
            pts if pts is not None else -1
        )
        ctypes.c_int64.from_address(addr + self.offsets.pkt_dts).value = (
            dts if dts is not None else -1
        )
        return pkt


@dataclass(slots=True)
class _DecodedFrame:
    """最小帧描述：本项目只关心「出了几帧、什么格式、多少像素」。"""

    format_name: str
    width: int
    height: int
    pts: int | None
    key_frame: bool = False
    pict_type: str | None = None

    @property
    def format(self) -> "_FormatName":
        return _FormatName(self.format_name)


class _FormatName:
    __slots__ = ("name",)

    def __init__(self, name: str | None) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Format {self.name}>"


def make_decoder(
    decoder_name: str,
    *,
    width: int,
    height: int,
    extradata: bytes | None = None,
    hwaccel: str | None = "d3d11va",
    thread_count: int = 1,
    libs: FfmpegLibraries | None = None,
) -> CtypesDecoder:
    """便捷入口：加载库并构造解码器。"""
    return CtypesDecoder(
        libs or load_libraries(),
        decoder_name,
        width=width,
        height=height,
        extradata=extradata,
        hwaccel=hwaccel,
        thread_count=thread_count,
    )
