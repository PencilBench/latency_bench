"""命令行入口：参数解析、批处理编排、汇总输出。"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from . import __version__, accel, bench
from .accel import AccelError
from .inventory import inventory_clip, write_inventory
from .reporting import RunWriter, render_failure
from .sysinfo import collect_system_info

__all__ = ["build_parser", "main", "run"]

DEFAULT_PATTERN = "*.mp4"


def default_clips_dir() -> Path:
    """默认素材目录：源码树里是仓库上层的 ``..\\clips``；安装后是当前目录下的 ``clips``。"""
    repo_root = Path(__file__).resolve().parents[2]
    candidate = repo_root.parent / "clips"
    if candidate.is_dir():
        return candidate
    return Path.cwd() / "clips"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="latency_bench",
        description=(
            "Moonlight 客户端硬件解码延迟测量。"
            "Windows 固定 d3d11va，macOS 固定 videotoolbox；"
            "解码帧留在 GPU 上，不回读 CPU，不允许软解回退。"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--clips",
        type=Path,
        default=None,
        help="素材目录（默认：仓库上层的 ../clips）",
    )
    parser.add_argument("--pattern", default=DEFAULT_PATTERN, help="素材文件名匹配模式")
    parser.add_argument("--out", type=Path, default=None, help="输出目录（默认 results/<时间戳>）")
    parser.add_argument(
        "--hwaccel",
        default=None,
        help="硬件加速设备类型；默认取平台默认值（Windows d3d11va / macOS videotoolbox）",
    )
    parser.add_argument(
        "--decoder",
        default="auto",
        help="强制指定解码器名（默认 auto：按平台优先级选原生解码器）",
    )
    parser.add_argument(
        "--allow-alt-hwaccel",
        action="store_true",
        help="允许备选设备类型（Windows: cuda）。非 Moonlight 路径，仅用于开发机验证。",
    )
    parser.add_argument(
        "--allow-software-fallback",
        action="store_true",
        help="允许软解回退（默认禁止）。打开后结果不再代表硬件解码延迟。",
    )
    parser.add_argument(
        "--repeat", type=int, default=1, help="每个片段重复次数（取最优一轮）"
    )
    parser.add_argument(
        "--skip-first-frames",
        type=int,
        default=1,
        help=(
            "只在统计口径上排除开头 N 个帧（默认 1，即排除含解码器初始化开销的第 0 帧）；"
            "帧照常解码、照常写 CSV，填 0 表示不排除"
        ),
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=0,
        help="每个片段先跑 N 次不计入统计（预热系统缓存）",
    )
    parser.add_argument("--tick-ms", type=float, default=33.0, help="输出 tick 周期（毫秒）")
    parser.add_argument("--fsync", action="store_true", help="每次 tick 额外 fsync（更慢，更稳）")
    parser.add_argument(
        "--max-frames",
        type=int,
        default=None,
        help="每个片段最多解码 N 帧（冒烟用；默认整段）",
    )
    parser.add_argument(
        "--max-wall-seconds",
        type=float,
        default=300.0,
        help="单个片段的墙钟上限；超过即停止该片段（防止卡死）",
    )
    parser.add_argument("--json", action="store_true", help="把本轮汇总 JSON 打到 stdout")
    parser.add_argument(
        "--thread-count",
        type=int,
        default=1,
        help="解码器线程数；默认 1 且 thread_type=NONE，避免额外流水线延迟",
    )
    parser.add_argument("--version", action="version", version=f"latency_bench {__version__}")
    return parser


def discover_clips(clips_dir: Path, pattern: str) -> list[Path]:
    if not clips_dir.is_dir():
        raise AccelError(f"素材目录不存在：{clips_dir}")
    found = sorted(p for p in clips_dir.glob(pattern) if p.is_file())
    if not found:
        raise AccelError(f"素材目录 {clips_dir} 下没有匹配 {pattern!r} 的文件")
    return found


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return "—"
        return f"{value:.{digits}f}"
    return str(value)


def _print_clip_line(summary: dict[str, Any]) -> None:
    dec = summary.get("decode", {})
    src = summary.get("source", {})
    flags = summary.get("flags", {})
    print(
        "  {name:<24} {w}x{h} {codec:<10} hw={hw:<12} dec={decoder:<15} "
        "frames={frames:<5} mean={mean:>8}ms p95={p95:>8}ms p99={p99:>8}ms "
        "jitter={jit:>8}ms rt={rt:>6}x hw_ok={hw_ok} readback={rb}".format(
            name=str(summary.get("clip"))[:24],
            w=src.get("width"),
            h=src.get("height"),
            codec=str(src.get("codec"))[:10],
            hw=str(summary.get("hwaccel"))[:12],
            decoder=str(summary.get("decoder") or "container")[:15],
            frames=summary.get("frame_count"),
            mean=_fmt(dec.get("mean_ms"), 3),
            p95=_fmt(dec.get("p95_ms"), 3),
            p99=_fmt(dec.get("p99_ms"), 3),
            jit=_fmt(dec.get("jitter_ms"), 3),
            rt=_fmt(summary.get("realtime_factor"), 2),
            hw_ok="yes" if flags.get("hardware_decode_confirmed") else "NO",
            rb="YES" if flags.get("readback_detected") else "no",
        )
    )
    backend = summary.get("backend") or {}
    if backend.get("backend") == "ctypes-ffmpeg":
        print(
            f"      后端 ctypes-ffmpeg：按名字选中原生 {backend.get('decoder')} 解码器"
            f"（PyAV 会选中 libdav1d，故走本后端）；"
            f"出帧格式 {', '.join(backend.get('output_formats') or []) or '—'}"
        )


def _better(a: dict[str, Any] | None, b: dict[str, Any] | None) -> dict[str, Any] | None:
    """重复测量时取「p99 更小」的一轮，同时要求帧数不更少。"""
    if a is None:
        return b
    if b is None:
        return a
    a_frames = a.get("frame_count", 0)
    b_frames = b.get("frame_count", 0)
    if b_frames < a_frames:
        return a
    if b_frames > a_frames:
        return b
    a_p99 = a.get("decode", {}).get("p99_ms") or float("inf")
    b_p99 = b.get("decode", {}).get("p99_ms") or float("inf")
    return b if b_p99 < a_p99 else a


def _configure_console() -> None:
    """Windows 控制台默认是 GBK，直接输出中文/符号会 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        for kwargs in (
            {"encoding": "utf-8", "errors": "replace"},
            {"errors": "replace"},
        ):
            try:
                reconfigure(**kwargs)
                break
            except Exception:
                continue


def run(argv: Sequence[str] | None = None) -> int:
    _configure_console()
    parser = build_parser()
    args = parser.parse_args(argv)

    environment = accel.describe_environment()
    environment["system"] = collect_system_info()

    try:
        hwaccel = accel.validate_hwaccel(args.hwaccel, allow_alt=args.allow_alt_hwaccel)
        clips_dir = (args.clips or default_clips_dir()).expanduser().resolve()
        clips = discover_clips(clips_dir, args.pattern)
    except AccelError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    if args.repeat < 1:
        print("错误：--repeat 必须 >= 1", file=sys.stderr)
        return 2
    if args.tick_ms <= 0:
        print("错误：--tick-ms 必须 > 0", file=sys.stderr)
        return 2

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = (args.out or (Path("results") / run_id)).expanduser().resolve()
    command = "python -m latency_bench " + " ".join(shlex.quote(a) for a in (argv or sys.argv[1:]))

    print(f"latency_bench {__version__}")
    print(f"  平台          : {environment['platform']} ({environment['machine']})")
    print(f"  Python / PyAV : {environment['python']} / {environment['pyav']}")
    print(f"  设备类型      : {hwaccel}  (可用: {', '.join(environment['hwdevices_available'])})")
    print(f"  素材目录      : {clips_dir}  ({len(clips)} 个文件)")
    print(f"  输出目录      : {out_dir}")
    print()

    writer = RunWriter(
        out_dir=out_dir,
        run_id=run_id,
        command=command,
        environment=environment,
        started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )

    # 元数据清点（只读容器）。
    infos = [inventory_clip(p) for p in clips]
    inventory_payload = {
        "clips": [asdict(c) for c in infos],
        "vfr_clips": [c.name for c in infos if c.is_vfr],
    }
    write_inventory(infos, out_dir / "inventory.json")

    exit_code = 0
    for info in infos:
        clip_meta = {
            **asdict(info),
            "is_vfr": info.is_vfr,
        }
        print(f"[{info.name}] codec={info.codec} {info.width}x{info.height} "
              f"{_fmt((info.bit_rate or 0) / 1e6, 1)} Mbps"
              f"{' VFR' if info.is_vfr else ''}")

        attempts_all = []
        best: dict[str, Any] | None = None
        runs = args.warmup + args.repeat
        for round_index in range(runs):
            is_warmup = round_index < args.warmup
            result = bench.run_clip(
                str(info.path),
                hwaccel=hwaccel,
                out_dir=writer,
                requested_decoder=args.decoder,
                allow_alt_hwaccel=args.allow_alt_hwaccel,
                allow_software_fallback=args.allow_software_fallback,
                tick_ms=args.tick_ms,
                fsync=args.fsync,
                max_frames=args.max_frames,
                max_wall_s=args.max_wall_seconds,
                thread_count=args.thread_count,
                skip_first_frames=args.skip_first_frames,
            )
            attempts_all.extend(result.attempts)
            if result.error or result.summary is None:
                continue
            if is_warmup:
                continue
            best = _better(best, result.summary)

        if best is None:
            # 探测失败：给出可读报错，并写一份 error summary 供汇总表引用。
            hint = _failure_hint(info.codec, hwaccel, environment)
            message = render_failure(clip_meta, hwaccel, attempts_all, environment, hint)
            print("  x 无法硬解")
            for line in message.splitlines():
                print(f"      {line}")
            exit_code = 1
            error_summary = {
                "clip": info.name,
                "path": str(info.path),
                "hwaccel": hwaccel,
                "decoder": None,
                "error": message,
                "source": {
                    "codec": info.codec,
                    "width": info.width,
                    "height": info.height,
                    "bit_rate": info.bit_rate,
                },
                "flags": {
                    "hardware_decode_confirmed": False,
                    "readback_detected": False,
                },
                "frame_count": 0,
                "attempts": [
                    {"hwaccel": a.hwaccel, "decoder": a.decoder, "error": a.error}
                    for a in attempts_all
                ],
            }
            writer.write_clip_summary(info.name.rsplit(".", 1)[0], hwaccel, error_summary)
            continue

        if not best.get("flags", {}).get("hardware_decode_confirmed"):
            exit_code = 1
        if best.get("flags", {}).get("readback_detected"):
            exit_code = 1
        _print_clip_line(best)
        writer.write_clip_summary(info.name.rsplit(".", 1)[0], hwaccel, best)

    json_path, md_path = writer.write_summary(extra={"hwaccel": hwaccel})
    print()
    print("汇总：")
    print(f"  {json_path}")
    print(f"  {md_path}")

    if args.json:
        print(json.dumps(writer.clips, ensure_ascii=False, indent=2, default=str))

    failures = [c for c in writer.clips if c.get("error")]
    readbacks = [c for c in writer.clips if c.get("flags", {}).get("readback_detected")]
    if failures:
        print(f"\n警告：{len(failures)} 个片段未能建立硬件解码会话（见 summary.md）。", file=sys.stderr)
    if readbacks:
        print(
            f"\n警告：{len(readbacks)} 个片段检测到 CPU 回读（帧格式为软件格式），结果无效。",
            file=sys.stderr,
        )
    return exit_code


def _failure_hint(codec: str, hwaccel: str, environment: dict[str, Any]) -> str:
    """针对「本机已确认过」的坑，给出具体一点的下一步提示。"""
    hints: list[str] = []
    codec_l = (codec or "").lower()
    if environment.get("sys_platform") == "win32" and hwaccel == "d3d11va":
        if codec_l in ("libdav1d", "libaom-av1", "av1"):
            hints.append(
                "  提示：PyAV 官方 Windows wheel 里 FFmpeg 默认用 libdav1d 解 AV1，"
                "而 libdav1d 不带 hwaccel；原生 av1 解码器虽然声明了 d3d11va，"
                "但在该 wheel 上无法通过 PyAV 建立硬解会话（实测 get_buffer 失败）。"
            )
            hints.append(
                "  可选：(a) 换用带原生 AV1 D3D11VA 的 FFmpeg 构建；"
                "(b) 临时加 --allow-alt-hwaccel 用 cuda（NVIDIA 才有，且不是 Moonlight 路径）。"
            )
    if hwaccel == "videotoolbox":
        hints.append("  提示：macOS 上 AV1 硬解需要 M3 及以上；更早的芯片只能软解。")
    return "\n".join(hints)


def main(argv: Sequence[str] | None = None) -> int:
    started = time.perf_counter()
    try:
        code = run(argv)
    except AccelError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已中断。", file=sys.stderr)
        return 130
    elapsed = time.perf_counter() - started
    print(f"耗时 {elapsed:.1f}s")
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
