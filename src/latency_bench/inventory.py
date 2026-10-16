"""用 PyAV 清点素材元数据（只读容器，不解码）。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import av

__all__ = ["ClipInfo", "inventory_clip", "inventory_clips", "write_inventory"]


@dataclass
class ClipInfo:
    path: str
    name: str
    size_bytes: int
    container_format: str
    codec: str
    """容器里实际选中的解码器名（例如 ``libdav1d`` 或 ``hevc``）。"""

    codec_id: str
    """编解码器名（``av1`` / ``hevc`` …）——由解码器的 ``canonical_name`` 得到。"""
    width: int
    height: int
    pix_fmt: str | None
    profile: str | None
    level: int | None
    duration_s: float | None
    bit_rate: int | None
    frame_count: int | None
    avg_frame_rate: float | None
    r_frame_rate: float | None
    time_base: str | None
    has_b_frames: int | None
    stts_entries: int | None = None
    """``stts`` 条目数——>1 说明是变帧率（PLAN.md §1）。仅 mp4 可读。"""

    error: str | None = None

    @property
    def is_vfr(self) -> bool:
        return bool(self.stts_entries and self.stts_entries > 1)


def _count_stts_entries(path: Path) -> int | None:
    """直接在 MP4 里数 ``stts`` 条目数，用于判定变帧率。

    只做最小化的 box 遍历，任何结构异常都返回 ``None``。
    """
    import struct

    def walk(data: bytes, start: int, end: int) -> int | None:
        pos = start
        while pos + 8 <= end:
            size = struct.unpack_from(">I", data, pos)[0]
            box_type = data[pos + 4 : pos + 8]
            header = 8
            if size == 1:
                if pos + 16 > end:
                    return None
                size = struct.unpack_from(">Q", data, pos + 8)[0]
                header = 16
            elif size == 0:
                size = end - pos
            if size < header or pos + size > end:
                return None
            if box_type == b"stts":
                if pos + header + 8 > end:
                    return None
                return struct.unpack_from(">I", data, pos + header + 4)[0]
            if box_type in (b"moov", b"trak", b"mdia", b"minf", b"stbl"):
                found = walk(data, pos + header, pos + size)
                if found is not None:
                    return found
            pos += size
        return None

    try:
        with path.open("rb") as fh:
            head = fh.read(32)
            if len(head) < 12:
                return None
            offset = 0
            # 跳过 ftyp
            size = struct.unpack_from(">I", head, 0)[0]
            if head[4:8] == b"ftyp" and size >= 8:
                offset = size
            fh.seek(offset)
            data = fh.read()
        return walk(data, 0, len(data))
    except Exception:
        return None


def inventory_clip(path: str | Path) -> ClipInfo:
    """读取单个片段的元数据。"""
    p = Path(path)
    try:
        with av.open(str(p)) as container:
            stream = container.streams.video[0]
            ctx = stream.codec_context
            duration_s = float(container.duration / av.time_base) if container.duration else None
            avg = float(stream.average_rate) if stream.average_rate else None
            r_rate = float(stream.base_rate) if stream.base_rate else None
            return ClipInfo(
                path=str(p),
                name=p.name,
                size_bytes=p.stat().st_size,
                container_format=container.format.name if container.format else "",
                codec=ctx.name,
                codec_id=str(getattr(ctx.codec, "canonical_name", "") or ""),
                width=ctx.width,
                height=ctx.height,
                pix_fmt=ctx.format.name if ctx.format else None,
                profile=str(ctx.profile) if ctx.profile is not None else None,
                level=ctx.level,
                duration_s=duration_s,
                bit_rate=container.bit_rate,
                frame_count=stream.frames or None,
                avg_frame_rate=avg,
                r_frame_rate=r_rate,
                time_base=str(stream.time_base) if stream.time_base else None,
                has_b_frames=ctx.has_b_frames,
                stts_entries=_count_stts_entries(p),
            )
    except Exception as exc:  # pragma: no cover - 依赖真实文件
        return ClipInfo(
            path=str(p),
            name=p.name,
            size_bytes=p.stat().st_size if p.exists() else 0,
            container_format="",
            codec="",
            codec_id="",
            width=0,
            height=0,
            pix_fmt=None,
            profile=None,
            level=None,
            duration_s=None,
            bit_rate=None,
            frame_count=None,
            avg_frame_rate=None,
            r_frame_rate=None,
            time_base=None,
            has_b_frames=None,
            error=f"{type(exc).__name__}: {exc}",
        )


def inventory_clips(paths: list[str | Path]) -> list[ClipInfo]:
    return [inventory_clip(p) for p in paths]


def write_inventory(clips: list[ClipInfo], destination: str | Path) -> Path:
    dest = Path(destination)
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "clips": [asdict(c) for c in clips],
        "vfr_clips": [c.name for c in clips if c.is_vfr],
    }
    dest.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return dest
