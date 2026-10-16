# Moonlight 客户端解码延迟测量工具 — 实现方案

## 1. 目标

在**子目录** `F:\AV1H265\latency_bench\` 新建仓库（`git init` 于此），测量 Moonlight 串流客户端
在目标硬件上的**硬件解码延迟**。素材是上层目录 `F:\AV1H265\clips\` 下由 foundation-sunshine
clip recorder（`F9+F10` 热键录制、`ffmpeg -c copy` 封装，commit `f6db914c`）产出的四段片段：

| 文件 | Codec | 分辨率 | 时长 | 码率 |
|---|---|---|---|---|
| `AV1_126.99Mbps.mp4` | `av01` | 3840x2160 | 9.97 s | 127 Mbps |
| `AV1_30.99Mbps.mp4` | `av01` | 1920x1080 | 9.92 s | 31 Mbps |
| `H265_126.99Mbps.mp4` | `hvc1` | 3840x2160 | 10.0 s | 127 Mbps |
| `H265_30.99Mbps.mp4` | `hvc1` | 1920x1080 | 9.92 s | 31 Mbps |

（H265 两段 `stts` 有 37 个条目 → 变帧率，实时倍率按平均帧率折算。）

目标平台：Windows（Intel Xe3 核显）与 macOS（Apple Silicon / VideoToolbox）。
测量口径对齐 Moonlight 客户端的真实解码路径：解码帧留在 GPU 上供渲染，**不做 CPU 回读**。
延迟结果**每 33ms 落盘一次**。

## 2. 方案概述

- **实现语言**：Python 3.13 + PyAV（`av >= 19`，wheel 自带 FFmpeg，Windows/macOS 共用一套源码）。
- **硬件解码**：
  ```python
  from av.codec.hwaccel import HWAccel
  hw = HWAccel(device_type=..., allow_software_fallback=False, is_hw_owned=True)
  container = av.open(path, hwaccel=hw)
  ```
  - `is_hw_owned=True` → 解码帧留在设备上，**不回读 CPU**。
  - `allow_software_fallback=False` → 强制硬件，失败即报错，**不支持软解**。
  - 断言 `stream.codec_context.is_hwaccel()` 为真，防止静默退化为软解。
- **硬件加速路径**：Windows 固定用 `d3d11va`，macOS 固定用 `videotoolbox`；**不使用 QSV**（见 §3.3）。
- **解码节奏**：尽快解码，不做实时 30fps 节流；每 33ms 把这一窗口的延迟统计写入文件。
- **输出**：逐帧 CSV + 每 33ms 一行的时间窗统计 CSV + 汇总 JSON。
- **仓库布局**：仓库根在子目录 `F:\AV1H265\latency_bench\`（`git init` 于此），源码用 `src\latency_bench\`
  布局；输入素材 `F:\AV1H265\clips\`（约 370 MB）与参考仓库 `F:\AV1H265\foundation-sunshine\`
  都在仓库**之外**，天然不入库。

## 3. 关键技术事实与设计依据

### 3.1 PyAV 硬件解码 API（v19.0.1，要求 Python ≥ 3.12）

- `HWAccel(device_type, device=None, allow_software_fallback=True, options=None, flags=None, is_hw_owned=False)`
  见 `av/codec/hwaccel.py`。`is_hw_owned=True` 时“decoded frames stay on the device … instead of being
  downloaded to system memory”；**默认 `False` 会把帧下载到系统内存**，与本项目口径不符，必须显式置 True。
- `av.codec.hwaccel.hwdevices_available()` 返回当前 FFmpeg 编译期支持的设备类型。
- `Stream.codec_context.is_hwaccel()` 用于确认硬件解码真的生效。
- PyAV **没有** `CodecContext.send_packet()` / `receive_frame()`，只有 `decode()` / `parse()` /
  `flush_buffers()`，因此计时粒度为「每 packet 一次 `Packet.decode()`」。

### 3.2 PyAV wheel 内置 FFmpeg 的硬件加速可用性（Windows 实测）

在 Windows + PyAV 19.0.1（wheel `av-19.0.1-cp312-abi3-win_amd64`，FFmpeg libavcodec 63.1.102）实测：

```
hwdevices_available() -> ['cuda', 'dxva2', 'qsv', 'd3d11va', 'd3d12va', 'amf']
```

即该 wheel 的 FFmpeg **同时**编译进了 `d3d11va`、`d3d12va`、`dxva2`、`qsv`、`cuda`、`amf`。
（注意：PyAV 官方示例 `examples/basics/hw_decode.py` 仍写着 “qsv … requires custom ffmpeg built with
those libraries”，该注释相对 19.0.1 的 Windows wheel **已经过时**。“设备类型被列出”与“解码器可用”
是两件事，不能互相推断。）macOS 侧 wheel 提供 `videotoolbox`；Windows 上不出现该设备类型，属正常。

→ **本项目只用 `d3d11va`（Windows）/ `videotoolbox`（macOS）**，其余设备类型即便被列出也不使用，
理由见 §3.3。

### 3.3 为什么 Windows 只用 D3D11VA（不使用 QSV）

**结论：Windows 侧固定使用 `d3d11va`（原生 `hevc`/`av1` 解码器 + `AV_HWDEVICE_TYPE_D3D11VA`），
不使用 QSV。** 两条理由：

1. **QSV 解码在 FFmpeg 里不是 hwaccel，而是另一套解码器，且自带抬高延迟的流水线。**
   `doc/ffmpeg.texi` 的 `-hwaccel qsv` 条目原文：*"Unlike most other values, this option does not enable
   accelerated decoding (that is used automatically whenever a qsv decoder is selected)"*；源码中搜不到
   `HWACCEL_QSV` / `ff_qsv_hwaccel`（0 命中）；FFmpeg wiki `HWAccelIntro` 的 API 状态表中 `libmfx`
   （QSV 前身）作为**内部 hwaccel 一列为 `-`（不适用）**，而 `Direct3D 11` 为 `Y`。
   即 `d3d11va` 挂在**原生解码器**上，QSV 则必须用独立的 `hevc_qsv` / `av1_qsv`。
   而 `libavcodec/qsvdec.c` + `qsv_internal.h` 显示 QSV 解码器默认开 4 帧深内部流水线：
   ```c
   #define ASYNC_DEPTH_DEFAULT 4       // internal parallelism
   { "async_depth", "Internal parallelization depth, the higher the value the higher the latency.",
     OFFSET(qsv.async_depth), AV_OPT_TYPE_INT, { .i64 = ASYNC_DEPTH_DEFAULT }, 1, INT_MAX, VD },
   param->AsyncDepth = q->async_depth;      /* 传给 oneVPL/MSDK session */
   if ((av_fifo_can_read(q->async_fifo) >= q->async_depth) || (!avpkt->size && ...)) { /* 才吐帧 */ }
   ```
   FFmpeg 自己的选项帮助文本就写明“值越高延迟越高”；默认 4 意味着帧要等队列攒到 4 个才被取出
   （量级为 3 个帧间隔，60fps 下约 50ms）。原生解码器 + `d3d11va` 没有这条 FFmpeg 侧队列。
2. **这是 Moonlight 在同一硬件上的实际做法。** 见 §3.4：moonlight-qt 从不为 Intel 核显启用 QSV，
   源码里明确注释 `case AV_HWDEVICE_TYPE_QSV: // Covered by VAAPI and D3D11VA/DXVA2`。
   本工具的目的是模拟真实串流路径，因此与 Moonlight 保持一致。

> **决策记录（主动排除，非能力缺失）**：`hwdevices_available()` 在 Windows 上确实会返回 `qsv`（见 §3.2），
> 但本项目主动排除它，属设计取舍。同理也不使用 `d3d12va` / `dxva2` / `cuda` / `amf`——即便它们可用，
> 也不是 Moonlight 在 Intel 核显上的真实路径。若将来要重开 QSV 对比，需额外解决“按名字构造
> `hevc_qsv` / `av1_qsv` 独立解码器”的问题，并同时记录 `async_depth=1` 与默认 4 两组结果。

> **证据性质**：§3.2 为**本机实测**（Windows + PyAV 19.0.1）；§3.1 / §3.3 / §3.4 为**源码与官方文档级**
> 结论（FFmpeg 主分支源码、moonlight-qt 源码、FFmpeg wiki 的 API 状态表），属与硬件世代无关的确定性
> 事实。本项目**未引用第三方实测帧率对比**——那类数据多为**吞吐**指标，不能外推为延迟。
> Xe3 / Apple Silicon 上的最终数字以本工具实测为准（见 §9）。

### 3.4 Moonlight 客户端的真实解码路径（按 moonlight-qt 源码核实）

这决定了“模拟真实串流”应当测哪条路径。

- moonlight-qt 全仓库搜 `qsv` / `mfx` / `MFXVideoDECODE` → **0 命中**。解码器目录
  `app/streaming/video/ffmpeg-renderers/` 仅含 `cuda`、`d3d11va`、`dxva2`、`vaapi`、`vdpau`、
  `vt_*.mm`(VideoToolbox)、`drm`、`mmal`、`plvk`(Vulkan Video)、`swframemapper`——**没有 QSV 实现**。
- `app/streaming/video/ffmpeg.cpp` 的 `createHwAccelRenderer(..., pass)` 三级选择：
  - **pass 0（最高优先）**：Windows 上 `case AV_HWDEVICE_TYPE_D3D11VA: return new D3D11VARenderer(pass);`
    并附注释 *"DXVA2 appears in the hwaccel list before D3D11VA, so we only check for D3D11VA on the
    first pass to ensure we prefer D3D11VA over DXVA2."*
  - **pass 1**：CUDA（仅 NVIDIA + Wayland）/ DXVA2 / VAAPI / VDPAU / VideoToolbox。
  - **pass 2（兜底）**：通用 `GenericHwAccelRenderer`，构造时打印 *"Using generic FFmpeg hwaccel
    backend … Performance may not be optimal!"*，且只支持回读渲染（`isDirectRenderingSupported()`
    返回 false）。该分支中有一行注释直接点题：
    `case AV_HWDEVICE_TYPE_QSV: // Covered by VAAPI and D3D11VA/DXVA2`
    → Moonlight 作者认定 QSV 已被 D3D11VA/DXVA2（Windows）与 VAAPI（Linux）覆盖，因此从不主动使用。
- `ffmpeg-renderers/d3d11va.cpp` 的 Intel 专属行为：
  - `adapterDesc.VendorId == 0x8086 && featureLevel <= D3D_FEATURE_LEVEL_11_0` → 跳过 D3D11VA
    （日志 *"Avoiding D3D11VA on old pre-FL11.1 Intel GPU. Set D3D11VA_ENABLED=1 to override."*）；
    **Xe/Xe2/Xe3 为 FL 12_x，正常启用**。
  - Intel 使用**独立解码/渲染色设备**：`separateDevices = adapterDesc.VendorId == 0x8086 || …`（为性能）。
  - Intel 使用 **bind 而非 copy**：`m_BindDecoderOutputTextures = adapterDesc.VendorId == 0x8086 || separateDevices;`
    注释引用 issue #1304 称额外拷贝有 *"significant performance impact"* → 解码输出纹理零拷贝直接交给渲染器，
    **不回读 CPU**。
  - 解码能力探测用 DXVA profile GUID，含 HEVC RExt（`MAIN_444` / `MAIN10_444`）与
    `D3D11_DECODER_PROFILE_AV1_VLD_PROFILE0`（`DXGI_FORMAT_NV12`，失败时报 *"GPU doesn't support AV1 decoding"*）。
- `ffmpeg-renderers/plvk.cpp`：Vulkan Video 在 **Intel Windows 驱动上被主动跳过**
  （`vendorID == 0x8086 && !PLVK_ALLOW_INTEL` → *"Skipping Intel GPU for Vulkan Video due to broken drivers"*，
  以 FFmpeg 7.0.1 为准），理由之一是 Intel 的 HEVC RExt 已可通过 D3D11VA 使用。
- 客户端平台 → 实际解码：

  | 客户端平台 + Intel 核显 | 实际解码 API |
  |---|---|
  | **Windows（Xe3 目标平台）** | **D3D11VA**（原生 `hevc`/`av1` 解码器 + `AV_HWDEVICE_TYPE_D3D11VA`），零拷贝 bind |
  | Linux | VAAPI（Intel iHD 驱动） |
  | macOS | VideoToolbox |

- 因此本项目的 `d3d11va` 主路径与 Moonlight 在 Intel 核显上的真实行为一致：同一 FFmpeg 原生解码器、
  同一 `AV_HWDEVICE_TYPE_D3D11VA`、同一驱动 API（AV1 同为 `AV1_VLD_PROFILE0`）、同样不回读 CPU。
  差异仅在 Moonlight 额外的渲染/呈现环节，不属解码延迟测量范围。

### 3.5 计时精度限制

PyAV 无 `send_packet` / `receive_frame` 分离接口，`decode_ms` 是**稳态下每 packet 的解码耗时**，
受解码器流水线深度影响，不完全等同「packet 到帧就绪」的端到端延迟。该限制需在文档中显式说明。

## 4. 目录结构

```
F:\AV1H265\                        <- 工作目录（不是仓库）
├─ .vscode\
├─ clips\                          <- 输入素材（约 370 MB，在仓库外）
├─ foundation-sunshine\            <- 参考仓库（独立 git，在仓库外）
└─ latency_bench\                  <- 仓库根（git init 于此）
   ├─ .gitignore                   <- results/ .venv/ __pycache__/ 等
   ├─ README.md                    <- 目标、快速开始(Win/macOS)、方法学、结果解读
   ├─ PLAN.md                      <- 本文件
   ├─ pyproject.toml               <- deps: av>=19 ; extras: dev=[pytest]
   ├─ results\                     <- 运行输出（不跟踪）
   ├─ src\latency_bench\
   │  ├─ __init__.py
   │  ├─ __main__.py               <- python -m latency_bench
   │  ├─ cli.py                    <- 参数解析、批处理编排、汇总输出
   │  ├─ accel.py                  <- 平台映射 + 可用性校验 + 硬解断言
   │  ├─ bench.py                  <- 解码循环 + 逐 packet 计时
   │  ├─ reporting.py              <- 33ms ticker、CSV/JSON 写出
   │  ├─ stats.py                  <- 分位数 / 抖动 / 实时倍率
   │  ├─ inventory.py              <- 用 PyAV 清点 clips 元数据
   │  └─ sysinfo.py                <- OS/CPU/GPU/PyAV/FFmpeg 版本采集
   ├─ tests\
   │  ├─ test_stats.py
   │  └─ test_reporting.py
   └─ scripts\
      ├─ setup_windows.ps1         <- 装 Python 3.13 + venv + pip install
      └─ setup_macos.sh            <- brew python + venv + pip install
```

## 5. 测量方法

对每个片段、每个 packet：

```
pkt  = next(demux)            # t_demux = 取到 packet 的耗时（与解码分开统计）
t0   = perf_counter_ns()
frms = pkt.decode()           # 含 send_packet + receive_frame；硬件帧留在 GPU
t1   = perf_counter_ns()
```

- `decode_ms`（packet 级）= `t1 - t0`：本次交给 libavcodec 的解码耗时，**不含 demux、不含回读**。
- `frames_out` = `len(frms)`；`decode_ms_per_frame` = `decode_ms / max(1, frames_out)`。
- 每解出一帧写一行 `frames.csv`，并用 `frame.pts` 记录 PTS。
- 结尾 `flush_buffers()` / `decode(None)` 排空流水线，尾部帧单独标记。
- **不做软解**：`allow_software_fallback=False`，并断言 `stream.codec_context.is_hwaccel()` 为真，否则直接失败。
- **不回读校验**：检查 `frame.format.name` 是否为硬件格式（本项目即 `d3d11`（Windows）/
  `videotoolbox*`（macOS））；若出现 `yuv420p` / `nv12` 等软件格式，说明发生了隐式回读 →
  在 summary 中标记 `readback_detected=true` 并报警。
- 线程：`thread_count=1`、`thread_type=NONE`（避免 FFmpeg 帧级多线程额外引入延迟）。

### 33ms 输出

- 主解码线程只做计时，把样本推入带锁的 list。
- 独立 **ticker 线程**（`perf_counter` 对齐 + 漂移补偿）每 33ms：
  1. 写一行 `ticks.csv`：`tick_index, t_ms, frames_in_tick, decode_us_last, min, max, avg, p50, p95, fps_inst`
  2. `flush()` 逐帧 CSV，保证文件每 33ms 有新鲜数据
- 默认只 `flush()`，`--fsync` 可选开 `os.fsync`（避免磁盘抖动污染测量）。

### 输出文件（每次运行）

`results/<run_id>/`

- `inventory.json` — 片段元数据
- `<clip>__<hwaccel>.frames.csv` — 逐帧
- `<clip>__<hwaccel>.ticks.csv` — 每 33ms 一行
- `<clip>__<hwaccel>.summary.json` — 总数/均值/中位/p95/p99/max/标准差/抖动/吞吐 fps/实时倍率/
  hw 像素格式/是否回读/环境信息
- `summary.json` — 全部片段汇总 + 实际使用的 hwaccel 与设备/环境信息
- `summary.md` — 便于贴入 issue/文档的对比表

## 6. 硬件加速选择

平台映射固定，**不做候选探测择优**：

| 平台 | hwaccel |
|---|---|
| Windows | `d3d11va` |
| macOS | `videotoolbox` |

1. 启动时用 `hwdevices_available()` 校验目标设备类型存在；缺失即报错退出。
2. 解码过程中断言 `stream.codec_context.is_hwaccel()` 为真，否则报错退出（**不回退软解**）。
3. 不使用 QSV（见 §3.3），也不使用 `d3d12va` / `dxva2` / `cuda` / `amf`——即使它们出现在
   `hwdevices_available()` 里。
4. `--hwaccel` 默认取平台默认值；允许传入的**只有本平台的那一个值**，传入其它值（含 `qsv`）直接报错，
   以免误用未经本方案验证的路径。

### CLI 参数

- `--clips`（默认 `..\clips`，即仓库上层的素材目录）、`--pattern`（默认 `*.mp4`）、
  `--out`（默认 `results\<时间戳>`）、`--hwaccel`（默认平台默认值：Windows `d3d11va` /
  macOS `videotoolbox`）、`--repeat`、`--warmup`、`--tick-ms`（默认 `33`）、`--fsync`、`--json`

## 7. 实施步骤

1. **仓库骨架**：在子目录 `F:\AV1H265\latency_bench\` 内 `git init`、`.gitignore`（`results/`、
   `.venv/`、`__pycache__/` 等）、`README.md`。
2. **环境**：装 Python 3.13（优先 `winget install -e --id Python.Python.3.13 --scope user`；失败则用
   `curl` 从 python.org 下载最新 3.13.x amd64 安装包并 `/quiet InstallAllUsers=0 PrependPath=1`），
   在仓库根建 `.venv`（即 `F:\AV1H265\latency_bench\.venv`），`pip install av==19.*` + `pip install -e .`。
3. **能力确认**：打印 `hwdevices_available()`、确认 `HWAccel(..., is_hw_owned=True)` 可用、
   `is_hwaccel()` 为真、`frame.format.name` 是硬格式（而非 `yuv420p`/`nv12` 等软格式）、
   `av.library_versions`。结论落实到 `accel.py` 的实现方式。
4. `inventory.py`：清点 4 个片段（codec/分辨率/帧率/时长/码率）→ `inventory.json`。
5. `accel.py`：平台映射 + 设备类型可用性校验 + 硬解断言。
6. `bench.py`：解码循环与逐 packet 计时（demux 与 decode 分开）。
7. `reporting.py` + `stats.py`：33ms ticker、逐帧/按 tick CSV、汇总 JSON、回读检测。
8. `cli.py`：批处理全部片段 + 汇总表。
9. `tests/`：pytest 覆盖分位数、33ms tick 切分与漂移补偿（假时钟）、回读检测判定。
10. `report.py` / `summary.md`：结果渲染为对比表（AV1 vs H265 × 4K vs 1080p × 31/127Mbps × 平台）。
11. `scripts/setup_windows.ps1` 与 `scripts/setup_macos.sh`（macOS：`brew install python@3.13`）。
12. **本机验证**：4 个片段全跑通；确认硬解生效、无回读、CSV/JSON 完整、tick 数量 ≈ 解码墙钟 / 33ms、
    传入非法的 `--hwaccel`（如 `qsv`）能干净失败并给出清晰提示。
13. 文档：根 `README.md`（快速开始 / 平台矩阵 / 方法学与局限）+ `latency_bench/README.md`。

## 8. 风险与已知局限

- **QSV 被主动排除**（见 §3.3）：这不是能力缺失——PyAV 19.0.1 的 Windows wheel 实测已列出 `qsv`
  （§3.2），排除它是设计取舍；同理也不使用 `d3d12va` / `dxva2` / `cuda` / `amf`。
- **本机无 Intel 核显**：开发机是 NVIDIA RTX 5090 D v2 + AMD Ryzen 9 9950X。本机只能用 `d3d11va`
  做端到端管线验证（NVIDIA 同样支持 AV1/HEVC 的 D3D11VA 解码）；**Xe3 的真实数据必须在
  Panther Lake 机器上采集**。
- **无 macOS 机器**：VideoToolbox 路径无法在本机验证，靠 API 探测 + 在 Mac 上运行 setup 脚本自查。
  Apple M6 尚未发布，按 Apple Silicon + VideoToolbox 通用路径实现（AV1 硬解需 M3 及以上，运行时显式报告）。
- **解码器流水线延迟**：per-packet 计时 ≠ 端到端延迟，见 §3.5。
- **4K 127Mbps 读盘带宽**：demux 可能成为瓶颈，故 demux 与 decode 分列统计；`--repeat` 可预热系统缓存。
- **VFR 片段**：实时倍率按平均帧率折算。

## 9. 验证方式

- `pytest`：stats / reporting 单元测试。
- 本机执行 `python -m latency_bench --clips ..\clips --hwaccel auto`，4 个片段全跑通，产出 `results/` 下
  全部 4 类文件；硬解断言与无回读校验通过。
- 时序自检：`ticks.csv` 行数 ≈ `wall_ms / 33`；相邻 tick 间隔中位数 ≈ 33ms。
- 失败路径：`--hwaccel qsv` 与 `--hwaccel videotoolbox`（Windows 上均为非法值）都清晰报错。

## 10. 范围外

- 不实现实时 30fps 节流播放模式。
- 不做编码 / 回读 / 渲染耗时测量。
- 不修改 `foundation-sunshine`（仅作只读参考）。
- 不配置 git remote（仅本地 `git init` + 首次提交）。
- `.vscode\settings.json` 保持不动。
