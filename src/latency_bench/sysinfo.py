"""系统信息采集：OS / CPU / GPU / Python / PyAV / FFmpeg。

全部为尽力而为——任何一项失败都只记录 ``None``，绝不影响测量本身。
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from typing import Any

import av

__all__ = ["collect_system_info", "collect_gpu_info", "json_default"]


def _run(cmd: list[str], timeout: float = 15.0) -> str | None:
    exe = shutil.which(cmd[0])
    if exe is None:
        return None
    try:
        proc = subprocess.run(
            [exe, *cmd[1:]],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception:
        return None
    out = (proc.stdout or "").strip()
    return out or None


def collect_gpu_info() -> list[dict[str, Any]] | None:
    """Windows 上通过 CIM 列出显卡；其它平台尽力而为。"""
    if sys.platform == "win32":
        raw = _run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-CimInstance Win32_VideoController | "
                "Select-Object Name,DriverVersion,AdapterRAM | ConvertTo-Json -Compress",
            ]
        )
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            parsed = [parsed]
        return [
            {
                "name": item.get("Name"),
                "driver_version": item.get("DriverVersion"),
                "adapter_ram_bytes": item.get("AdapterRAM"),
            }
            for item in parsed
            if isinstance(item, dict)
        ]

    if sys.platform == "darwin":
        raw = _run(["system_profiler", "SPDisplaysDataType", "-json"])
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        out = []
        for item in parsed.get("SPDisplaysDataType", []):
            out.append(
                {
                    "name": item.get("sppci_model") or item.get("_name"),
                    "vendor": item.get("spdisplays_vendor"),
                    "metal": item.get("spdisplays_metal"),
                }
            )
        return out or None

    return None


def collect_system_info() -> dict[str, Any]:
    """一次性采集全部环境信息。"""
    return {
        "os": {
            "platform": platform.platform(),
            "sys_platform": sys.platform,
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
        },
        "python": {
            "version": sys.version.split()[0],
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
            "bits": 64 if sys.maxsize > 2**32 else 32,
        },
        "cpu": {
            "processor": platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER"),
            "identifier": os.environ.get("PROCESSOR_IDENTIFIER"),
            "logical_cores": os.cpu_count(),
        },
        "gpus": collect_gpu_info(),
        "pyav": {
            "version": av.__version__,
            "ffmpeg_libraries": {
                name: ".".join(str(part) for part in version)
                for name, version in av.library_versions.items()
            },
        },
    }


def json_default(value: Any) -> Any:
    """给 ``json.dump`` 用的兜底序列化器。"""
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if isinstance(value, bytes):
        return value.hex()
    return str(value)
