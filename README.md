# Moonlight 客户端解码延迟测量工具

测量 Moonlight 串流客户端在目标硬件上的**硬件解码延迟**。素材由
foundation-sunshine clip recorder（`F9+F10` 热键、`ffmpeg -c copy` 封装）产出，
放在仓库上层的 `..\clips\`。

测量口径对齐 moonlight-qt 在 Intel 核显上的**真实解码路径**：

| 平台 | hwaccel | 解码器 |
|---|---|---|
| Windows | `d3d11va` | 原生 `hevc` / `av1`（**不用 QSV**） |
| macOS | `videotoolbox` | 原生 `hevc` / `av1` |

* 解码帧留在 GPU 上（`is_hw_owned=True`），**不做 CPU 回读**；
* 不允许软解回退（`allow_software_fallback=False`），硬解不生效即失败；
* 延迟结果**每 33 ms 落盘一次**（`*.ticks.csv`）。

---

## 快速开始

### Windows

```powershell
pwsh -File scripts\setup_windows.ps1
.\.venv\Scripts\python.exe -m latency_bench --clips ..\clips
.\.venv\Scripts\python.exe -m pytest
```

### macOS

```bash
bash scripts/setup_macos.sh
./.venv/bin/python -m latency_bench --clips ../clips
./.venv/bin/python -m pytest
```

> AV1 硬解在 macOS 上需要 Apple M3 及以上；更早的芯片只能软解，本工具会直接报错。

### 直接安装

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"     # Windows: .venv\Scripts\pip.exe
```

要求 Python ≥ 3.12 与 PyAV ≥ 19（wheel 自带 FFmpeg，Windows/macOS 共用一套源码）。

---

## CLI

```
python -m latency_bench [选项]
```

| 选项 | 默认 | 说明 |
|---|---|---|
| `--clips` | 仓库上层 `..\clips` | 素材目录 |
| `--pattern` | `*.mp4` | 文件名匹配模式 |
| `--out` | `results\<时间戳>` | 输出目录 |
| `--hwaccel` | 平台默认值 | **只接受本平台那一个值**；传 `qsv` 等直接报错 |
| `--decoder` | `auto` | `auto` 按平台优先级选原生解码器；也可强制指定解码器名 |
| `--allow-alt-hwaccel` | 关 | 允许备选设备类型（Windows: `cuda`）。**非 Moonlight 路径**，仅供开发机验证 |
| `--allow-software-fallback` | 关 | 允许软解回退。打开后结果不再代表硬解延迟 |
| `--repeat` | 1 | 每片段重复次数（取 p99 更优的一轮） |
| `--warmup` | 0 | 先把片段跑 N 次不计入统计（预热系统缓存） |
| `--tick-ms` | 33 | tick 周期 |
| `--fsync` | 关 | 每次 tick 额外 `fsync`（更慢更稳） |
| `--max-frames` | 整段 | 每片段最多解码 N 帧（冒烟用） |
| `--max-wall-seconds` | 300 | 单片段墙钟上限（防卡死） |
| `--thread-count` | 1 | 解码器线程数；默认同时把 `thread_type` 设为 `NONE` |
| `--json` | 关 | 把本轮汇总 JSON 打到 stdout |

退出码：`0` 全部成功；`1` 有片段无法硬解或检测到回读；`2` 参数/环境错误。

---

## 测量方法

对每个 packet：

```text
pkt  = next(demux)            # t_demux = 取到 packet 的耗时（与解码分开统计）
t0   = perf_counter_ns()
frms = decode(pkt)            # send_packet + receive_frame；硬件帧留在 GPU
t1   = perf_counter_ns()
```

* `decode_us`（packet 级）= `t1 - t0`：交给 libavcodec 的解码耗时，**不含 demux、不含回读**；
* 同一 packet 连出多帧时按 `frames_out` 均摊（`decode_us_per_frame`）；
* 结尾 `decode(None)` 排空流水线，尾部帧在 CSV 里标记 `tail=1`；
* 主解码线程只做计时，样本按批推给 ticker 线程；ticker 每 33 ms 写一行
  `ticks.csv` 并 `flush()` 逐帧 CSV，保证盘上每 33 ms 有新鲜数据。

### 输出文件（每次运行）

```
results/<run_id>/
├─ inventory.json                       片段元数据
├─ <clip>__<hwaccel>.frames.csv         逐帧
├─ <clip>__<hwaccel>.ticks.csv          每 33 ms 一行
├─ <clip>__<hwaccel>.summary.json       总数/均值/中位/p95/p99/max/标准差/抖动/吞吐/实时倍率/硬解/回读
├─ summary.json                         全部片段汇总 + 环境信息
└─ summary.md                           便于贴进 issue 的对比表
```

---

## 结果解读

* **`decode_us`** 是稳态下**每 packet** 的解码耗时。PyAV 没有
  `send_packet()` / `receive_frame()` 分离接口，因此该数值受解码器流水线深度影响，
  **不完全等同「packet 到帧就绪」的端到端延迟**。
* **`实时倍率`** = `(帧数 / 平均帧率) / 解码墙钟`；变帧率素材（H265 两段 `stts` 有
  37 个条目）按平均帧率折算。`> 1` 表示解码快于实时播放。
* **`抖动`** 是相邻样本绝对差的**中位数**（不是平均数，避免单次调度抖动把统计拉歪）。
* **硬解判定**：`stream.codec_context.is_hwaccel` 为真 **且** 出帧格式是硬件格式
  （`d3d11` / `videotoolbox*`）**且** 未检测到软件格式帧。任何一条不满足都会
  置 `hardware_decode_confirmed=false` 并以退出码 1 结束。
* **回读判定**：出现 `yuv*` / `nv12` 等软件格式帧即置 `readback_detected=true`，
  该结果无效（PLAN.md §5 的「不回读校验」）。

---

## 为什么 Windows 只用 D3D11VA

1. **QSV 在 FFmpeg 里不是 hwaccel，而是另一套解码器，且自带抬高延迟的流水线。**
   `libavcodec/qsvdec.c` 默认 `ASYNC_DEPTH_DEFAULT = 4`，FFmpeg 自己的选项帮助就写着
   「值越高延迟越高」；原生解码器 + `d3d11va` 没有这条 FFmpeg 侧队列。
2. **这是 moonlight-qt 在同一硬件上的实际做法。** 全仓库搜 `qsv` / `mfx` 零命中；
   `ffmpeg.cpp` 里 `case AV_HWDEVICE_TYPE_QSV: // Covered by VAAPI and D3D11VA/DXVA2`，
   从不主动使用。本工具的目的是模拟真实串流路径，因此与 Moonlight 保持一致。

同理也不使用 `d3d12va` / `dxva2` / `cuda` / `amf`——即便 `hwdevices_available()`
把它们列出来（本机实测：`cuda, dxva2, qsv, d3d11va, d3d12va, amf`）。

---

## 已知现象与限制

### 1. AV1 + D3D11VA 在 PyAV 官方 Windows wheel 上建不起来（**本机实测**）

在 Windows + PyAV 19.0.1（`av-19.0.1-cp312-abi3-win_amd64`）上：

* FFmpeg 的 AV1 解码器列表里 `libdav1d` 排在原生 `av1` **前面**，而
  `libdav1d.hardware_configs == []`（外部解码器不带 hwaccel）；
* 原生 `av1` 解码器**确实**声明了 `d3d11va`（`format=d3d11`），但 PyAV 没有
  「按解码器名解复用」的入口（`av.open()` 无 `codec_name` 参数），
  手工 `CodecContext.create("av1", hwaccel=...)` 喂包时在 `get_buffer()` 处失败
  （`avcodec_send_packet()` 返回 `EINVAL`）——软解路径更直接：
  FFmpeg 的 AV1 解码器已不再支持纯软件解码，`ff_get_format()` 找不到
  `avctx->hwaccel` 时返回 `ENOSYS`（`av1dec.c`：*"Your platform doesn't support
  hardware accelerated AV1 decoding"*）；
* 作为对照，**HEVC + D3D11VA 完全正常**（出帧格式 `d3d11`，无回读），
  `av1_cuvid` + `cuda` 也能正常解 AV1——说明 GPU 与 PyAV 的硬件路径本身没问题，
  是**这个 wheel 的原生 AV1 + D3D11VA 组合**不可用。

因此本工具在 Windows 上跑 AV1 片段时会**明确失败并给出上述诊断**，而不是悄悄软解。
可选出路：

1. 换用带原生 AV1 D3D11VA 的 FFmpeg 构建（本项目只用 PyAV 的公开 API，
   换 wheel 即可）；
2. 临时加 `--allow-alt-hwaccel` 用 `cuda`（仅 NVIDIA，且**不是** Moonlight 路径）；
3. 在目标机（Intel Xe3）上按同样方式复核。

> `hwdevices_available()` 列出了 `qsv` 也不代表可用：实测 `HWDevice("qsv")` 直接
> 抛 `UnknownError`。「设备类型被列出」与「解码器可用」是两件事。

### 2. 周期性慢帧（1080p HEVC，本机实测）

在本机（NVIDIA RTX 5090 D v2）上，1080p HEVC 片段每约 **26 帧**会出现一个
14–30 ms 的慢帧；4K 片段没有这个现象（只有第 0 帧因为解码器初始化而偏慢）。

* 与 ticker 线程无关（关掉 ticker 依然出现）；
* 与 `thread_count` / `thread_type` 无关（1/0 × NONE/FRAME/AUTO 都一样）；
* `frame.close()` 释放解码帧也不消除；
* `is_hw_owned=False`（下载到系统内存）路径下**不会**出现。

推测与 D3D11 输出纹理池的周期性回收有关。工具会把这类偏慢帧单独汇总到
`summary.md` 的「偏慢的帧」一节，并给出**相邻间隔中位数**，便于判断是周期性还是偶发。
在目标机（Intel Xe3）上采集时应重新观察是否存在同样现象。

### 3. PyAV 的 demux 生成器在部分 MP4 上到 EOF 后不再返回哨兵值

实测本机全部四个素材：解完最后一帧后 `next(container.demux(stream), None)`
会永久阻塞而不是返回 `None`。因此测量循环把「预期帧数」（`stream.frames`，
否则 `时长 × 平均帧率`）作为**主退出条件**，`demux` 返回 `None` 只是备用出口，
并额外提供 `--max-wall-seconds` 兜底。

### 4. ticker 等待策略

Windows 上 `threading.Event.wait(0.033)` 的实测唤醒延迟约 **14 ms**（把 33 ms 的
tick 拖成 47 ms）。因此 ticker 采用「`time.sleep` 到截止前 2 ms + 忙等收尾」，
本机实测 tick 间隔中位数 **33.000 ms**、漂移 < 0.1 ms。

### 5. 其它

* **未在本机验证的路径**：VideoToolbox（没有 macOS 机器）。macOS 侧靠 API 探测 +
  在 Mac 上运行 `scripts/setup_macos.sh` 自查。
* **本机没有 Intel 核显**，开发机只能用 D3D11VA 做端到端管线验证
  （NVIDIA 同样支持 AV1/HEVC 的 D3D11VA）。**Xe3 的真实数据必须在
  Panther Lake 机器上采集。**
* **4K 127 Mbps 读盘带宽**：demux 可能成为瓶颈，故 demux 与 decode 分列统计，
  `--repeat` 可预热系统缓存。
* **范围外**：不实现实时 30 fps 节流播放；不做编码 / 回读 / 渲染耗时测量；
  不修改 `foundation-sunshine`（只读参考）。

---

## 目录结构

```
latency_bench/
├─ pyproject.toml
├─ PLAN.md                     实现方案（含设计依据与证据）
├─ README.md
├─ scripts/
│  ├─ setup_windows.ps1
│  └─ setup_macos.sh
├─ src/latency_bench/
│  ├─ __main__.py              python -m latency_bench
│  ├─ cli.py                   参数解析、批处理编排、汇总
│  ├─ accel.py                 平台映射、解码器选择、硬解断言
│  ├─ bench.py                 解码循环 + 逐 packet 计时 + 回读检测
│  ├─ reporting.py             33 ms ticker、CSV/JSON/Markdown 写出
│  ├─ stats.py                 分位数 / 抖动 / 实时倍率
│  ├─ inventory.py             用 PyAV 清点素材元数据
│  └─ sysinfo.py               OS/CPU/GPU/PyAV/FFmpeg 版本采集
└─ tests/
   ├─ test_stats.py
   ├─ test_accel.py
   └─ test_reporting.py
```

设计依据（QSV 排除、moonlight-qt 源码核实、计时精度限制）详见 [PLAN.md](PLAN.md) §3。
