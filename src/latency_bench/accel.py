"""硬件加速与解码器选择。

设计依据见 PLAN.md §3：

* Windows 固定 ``d3d11va``，macOS 固定 ``videotoolbox``；**不使用 QSV**。
* 必须落在 FFmpeg **原生解码器**上（``hevc`` / ``av1`` / ``h264`` …），
  这正是 moonlight-qt 在 Intel 核显上的路径（``ffmpeg.cpp`` 的 pass 0 分支）。
* ``is_hw_owned=True`` 让解码帧留在设备上；``allow_software_fallback=False`` 禁止软解回退。
* 断言 ``stream.codec_context.is_hwaccel`` 为真，且解码帧格式必须是硬件格式，
  否则视为静默软解 / 隐式回读，直接失败。
"""

from __future__ import annotations

import platform as _platform
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Mapping

import av
from av.codec import Codec
from av.codec.hwaccel import HWAccel, HWDeviceType, hwdevices_available

if TYPE_CHECKING:  # pragma: no cover
    from av.codec.context import CodecContext
    from av.stream import Stream

    HWAccelLike = HWAccel
    StreamLike = Stream
    CodecContextLike = CodecContext
else:  # 运行期只做类型别名，避免额外 import 开销
    HWAccelLike = HWAccel
    StreamLike = object
    CodecContextLike = object

__all__ = [
    "PLATFORM_HWACCEL",
    "EXCLUDED_HWACCELS",
    "ALT_HWACCELS",
    "DECODER_PREFERENCES",
    "SOFTWARE_DECODERS",
    "AccelError",
    "AltHwaccelNotEnabled",
    "HardwareUnavailable",
    "DecodePlan",
    "PlanAttempt",
    "available_device_types",
    "device_type_available",
    "platform_default_hwaccel",
    "allowed_hwaccels",
    "validate_hwaccel",
    "hardware_pix_fmts",
    "supports_hwaccel",
    "decoder_candidates",
    "make_hwaccel",
    "is_software_pix_fmt",
    "plan_for_codec",
    "describe_environment",
    "format_attempts",
]

# --------------------------------------------------------------------------------------
# 平台映射（PLAN.md §6）
# --------------------------------------------------------------------------------------

PLATFORM_HWACCEL: dict[str, str] = {
    "win32": "d3d11va",
    "darwin": "videotoolbox",
}

#: 主动排除的设备类型——即使 ``hwdevices_available()`` 列出了它们也不使用（PLAN.md §3.3 / §6）。
EXCLUDED_HWACCELS: tuple[str, ...] = ("qsv", "d3d12va", "dxva2", "amf")

#: ``--allow-alt-hwaccel`` 打开后才允许的备选设备类型（仅用于开发机验证，非 Moonlight 路径）。
ALT_HWACCELS: dict[str, tuple[str, ...]] = {
    "win32": ("cuda",),
    "darwin": (),
}

#: 每种「编解码器 × 设备类型」下按优先级排列的解码器名。
#: 原生解码器在前——只有原生解码器才带 FFmpeg 的 hwaccel 支持
#: （``libdav1d`` 这类外部解码器的 ``hardware_configs`` 为空）。
DECODER_PREFERENCES: dict[str, dict[str, tuple[str, ...]]] = {
    "av1": {
        "d3d11va": ("av1",),
        "d3d12va": ("av1",),
        "videotoolbox": ("av1",),
        "cuda": ("av1_cuvid",),
    },
    "hevc": {
        "d3d11va": ("hevc",),
        "d3d12va": ("hevc",),
        "videotoolbox": ("hevc",),
        "cuda": ("hevc_cuvid",),
    },
    "h264": {
        "d3d11va": ("h264",),
        "d3d12va": ("h264",),
        "videotoolbox": ("h264",),
        "cuda": ("h264_cuvid",),
    },
    "vp9": {
        "d3d11va": ("vp9",),
        "videotoolbox": ("vp9",),
        "cuda": ("vp9_cuvid",),
    },
}

#: 纯软解解码器，永远不满足本项目的硬解要求；仅用于给出更清楚的报错信息。
SOFTWARE_DECODERS: Mapping[str, str] = {
    "libdav1d": "dav1d 软件 AV1 解码器（FFmpeg 在 PyAV wheel 中默认优先选它）",
    "libaom-av1": "libaom 软件 AV1 解码器",
    "libvpx-vp9": "libvpx 软件 VP9 解码器",
}


class AccelError(RuntimeError):
    """硬件加速相关的致命错误——按 PLAN.md 的口径，一律不许回退软解。"""


class AltHwaccelNotEnabled(AccelError):
    """请求了备选设备类型但没有显式打开开关。"""


class HardwareUnavailable(AccelError):
    """本平台 / 本 FFmpeg 构建缺少所需的硬件设备类型。"""


# --------------------------------------------------------------------------------------
# 设备类型与解码器能力
# --------------------------------------------------------------------------------------


def available_device_types() -> list[str]:
    """当前 FFmpeg 构建支持的硬件设备类型名。"""
    return list(hwdevices_available())


def device_type_available(name: str) -> bool:
    return name in available_device_types()


def hardware_pix_fmts(decoder_name: str) -> tuple[str, ...]:
    """解码器声明的硬件像素格式名（用于判断该解码器是否真的支持硬解）。"""
    try:
        codec = Codec(decoder_name, "r")
    except Exception:
        return ()
    return tuple(cfg.format.name for cfg in codec.hardware_configs if cfg.is_supported)


def supports_hwaccel(decoder_name: str, hwaccel: str) -> bool:
    """该解码器是否为指定设备类型声明了 *可用* 的硬件配置。"""
    try:
        codec = Codec(decoder_name, "r")
    except Exception:
        return False
    return any(
        cfg.is_supported
        and cfg.device_type != HWDeviceType.none
        and cfg.device_type.name == hwaccel
        for cfg in codec.hardware_configs
    )


# --------------------------------------------------------------------------------------
# 平台 / 参数校验
# --------------------------------------------------------------------------------------


def host_platform() -> str:
    """``win32`` / ``darwin`` / 其它 ``sys.platform`` 值。"""
    return sys.platform


def platform_default_hwaccel(platform: str | None = None) -> str:
    plat = platform or host_platform()
    try:
        return PLATFORM_HWACCEL[plat]
    except KeyError:
        raise HardwareUnavailable(
            f"不支持的平台 {plat!r}：本项目只实现 Windows（d3d11va）与 macOS（videotoolbox）。"
        ) from None


def allowed_hwaccels(allow_alt: bool = False, platform: str | None = None) -> tuple[str, ...]:
    """本平台允许传入的 ``--hwaccel`` 取值。"""
    plat = platform or host_platform()
    primary = (platform_default_hwaccel(plat),)
    if allow_alt:
        return primary + ALT_HWACCELS.get(plat, ())
    return primary


def validate_hwaccel(
    requested: str | None,
    *,
    allow_alt: bool = False,
    platform: str | None = None,
) -> str:
    """校验 ``--hwaccel``：只接受本平台的那一个值（备选需显式开关）。"""
    plat = platform or host_platform()
    if requested in (None, "", "auto", "default"):
        requested = platform_default_hwaccel(plat)

    allowed = allowed_hwaccels(allow_alt, plat)
    if requested not in allowed:
        hint = ""
        if not allow_alt and requested in allowed_hwaccels(True, plat):
            hint = (
                f"\n  {requested!r} 属备选设备类型，仅在显式加上 --allow-alt-hwaccel 时才可用"
                "（该路径不是 Moonlight 的真实路径，只用于开发机验证）。"
            )
        raise AccelError(
            f"--hwaccel {requested!r} 在当前平台（{plat}）不被接受。\n"
            f"  本平台允许的取值：{', '.join(allowed)}。{hint}"
        )

    if not device_type_available(requested):
        raise HardwareUnavailable(
            f"当前 FFmpeg/PyAV 构建不支持设备类型 {requested!r}。\n"
            f"  可用设备类型：{', '.join(available_device_types()) or '（无）'}"
        )
    return requested


# --------------------------------------------------------------------------------------
# 解码计划
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DecodePlan:
    """一次「用什么解码」的完整决定。"""

    hwaccel: str
    decoder: str | None
    """``None`` 表示用容器自己选出的解码器。"""

    force_hardware: bool = True
    allow_software_fallback: bool = False
    note: str = ""

    @property
    def label(self) -> str:
        return f"{self.decoder or 'container-default'}+{self.hwaccel}"


@dataclass
class PlanAttempt:
    """一次尝试的记录，用于在全部失败时给出可读的报错。"""

    hwaccel: str
    decoder: str | None
    error: str


def decoder_candidates(codec_name: str, hwaccel: str) -> tuple[str | None, ...]:
    """按优先级给出该编解码器在此设备类型下值得尝试的解码器名。

    最后一项固定是 ``None``（让容器自己选），这样即使原生解码器建不起来，
    报错信息里也会同时记录「容器默认解码器」的失败原因，便于定位。
    """
    table = DECODER_PREFERENCES.get(codec_name)
    names = table.get(hwaccel, ()) if table else ()
    if not names:
        return (None,)
    return (*names, None)


def make_hwaccel(
    hwaccel: str,
    *,
    device: str | None = None,
    allow_software_fallback: bool = False,
    is_hw_owned: bool = True,
) -> HWAccel:
    """构造 :class:`av.codec.hwaccel.HWAccel`。"""
    return HWAccel(
        device_type=hwaccel,
        device=device,
        allow_software_fallback=allow_software_fallback,
        is_hw_owned=is_hw_owned,
    )


_SOFTWARE_PIX_FMT_MARKERS = ("yuv", "nv12", "nv21", "rgb", "bgr", "gray", "p010", "p012")


def is_software_pix_fmt(name: str | None) -> bool:
    """判断像素格式名是否为**软件**格式（即发生了隐式回读）。

    D3D11VA 的硬件格式是 ``d3d11``，VideoToolbox 是 ``videotoolbox`` /
    ``videotoolbox_vld``；凡是 ``yuv*`` / ``nv12`` 之类都属于软件格式。
    """
    if not name:
        return True
    lowered = name.lower()
    return lowered.startswith(_SOFTWARE_PIX_FMT_MARKERS)


def plan_for_codec(
    codec_name: str,
    hwaccel: str,
    *,
    requested_decoder: str | None = None,
    force_hardware: bool = True,
) -> DecodePlan:
    """构造单次解码计划（可行性探测在 :mod:`latency_bench.bench` 里做）。"""
    if requested_decoder in ("auto", "", None):
        candidates = decoder_candidates(codec_name, hwaccel)
        decoder = candidates[0] if candidates else None
    else:
        decoder = requested_decoder
        if decoder in SOFTWARE_DECODERS and force_hardware:
            raise AccelError(
                f"--decoder {decoder!r} 是软件解码器（{SOFTWARE_DECODERS[decoder]}），"
                "与本项目「必须硬解、不回读」的口径冲突。"
            )
    return DecodePlan(
        hwaccel=hwaccel,
        decoder=decoder,
        force_hardware=force_hardware,
    )


def describe_environment() -> dict[str, object]:
    """环境摘要，写进 summary.json 与报错信息。"""
    return {
        "platform": _platform.platform(),
        "sys_platform": sys.platform,
        "machine": _platform.machine(),
        "python": sys.version.split()[0],
        "pyav": av.__version__,
        "ffmpeg_libraries": {k: ".".join(str(x) for x in v) for k, v in av.library_versions.items()},
        "hwdevices_available": available_device_types(),
        "platform_default_hwaccel": PLATFORM_HWACCEL.get(sys.platform),
        "alt_hwaccels": list(ALT_HWACCELS.get(sys.platform, ())),
        "excluded_hwaccels": list(EXCLUDED_HWACCELS),
    }


def format_attempts(attempts: Iterable[PlanAttempt]) -> str:
    lines = []
    for a in attempts:
        dec = a.decoder or "（容器默认）"
        lines.append(f"    - {dec} + {a.hwaccel}: {a.error}")
    return "\n".join(lines)
