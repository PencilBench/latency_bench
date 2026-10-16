"""Moonlight 客户端硬件解码延迟测量工具。

测量口径与 Moonlight 客户端的真实解码路径对齐：

* Windows 固定使用 ``d3d11va``（原生 ``hevc`` / ``av1`` 解码器 + ``AV_HWDEVICE_TYPE_D3D11VA``）；
* macOS 固定使用 ``videotoolbox``；
* 解码帧留在 GPU 上（``is_hw_owned=True``），**不做 CPU 回读**；
* 不允许软解回退（``allow_software_fallback=False``），硬解不生效即失败。
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
