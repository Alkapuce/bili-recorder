# Bili Recorder

一个面向 Linux 服务器的 B 站直播录制工具，主打长期后台运行和尽量少人工介入。

适合作为 Docker 常驻服务使用，支持开播检测、自动录制、断线恢复、录后转码和基础磁盘清理。

## Features

- 自动轮询直播状态，开播后开始录制
- 同场断流、重试耗尽后继续收集分段，确认下播后自动合并
- 后台串行转码为 AV1，音频直接复制，继续监听下一场直播
- 周期写入 `status.json`，配合 Docker healthcheck 做存活检测
- 支持运行中修改 `config.ini`，无需重启即可切换 `RoomId`
- 支持按磁盘剩余空间、文件数量、保留天数做基础清理

## Tech Stack

- Python 3.10
- `bilibili-api-python`
- FFmpeg / FFprobe
- Docker Compose
- Optional NVIDIA NVENC

## Quick Start

1. 准备配置文件

```bash
cp config.example.ini config.ini
```

2. 编辑 `config.ini`，至少填写真实的 `RoomId`

3. 启动服务

```bash
./scripts/up.sh
```

查看状态：

```bash
./scripts/up.sh ps
docker compose logs -f bili-recorder
```

停止服务：

```bash
./scripts/up.sh down
```

`./scripts/up.sh` 会自动探测宿主机 GPU。可用时叠加 `docker-compose.gpu.yml`，不可用时自动回退 CPU 模式。

## Configuration

配置文件示例见 `config.example.ini`。

### `[General]`

- `RoomId`: 要监听的直播间号
- `CheckInterval`: 轮询间隔，单位秒
- `SaveDir`: 录制输出目录
- `MinFreeSpaceGB`: 最低剩余磁盘阈值，小于该值时跳过新录制

### `[Recording]`

- `Format`: 原始录制格式，支持 `flv` / `mp4` / `mkv`
- `VideoEncoder` / `AudioEncoder`: 原始录制阶段的编码方式
- `StallTimeoutSeconds`: 文件长时间不增长时判定为卡死
- `MaxRecoverRetries`: 录制异常后的自动恢复次数

### `[PostProcess]`

- `TranscodeEnabled`: 是否启用录后转码
- `AutoPreferNvenc`: 自动优先 NVENC
- `VideoEncoder` / `FallbackVideoEncoder`: CPU 转码主编码器与回退编码器
- `OutputFormat`: 转码输出格式
- `KeepSource`: 转码成功后是否保留源文件

默认录制策略是视频、音频均 `copy`，原流码率由直播源决定。
后处理默认使用 AV1：可用时 `av1_nvenc` CQ 34 / p5，否则
`libsvtav1` CRF 34 / preset 6 / 10-bit，`CpuThreads = 2` 限制 SVT 的并行度。
默认输出 MP4，索引前置，关键帧间隔 60 帧。分辨率和帧率保留，音频始终 `copy`。CRF 控制质量，输出码率和大小随画面变化。
AV1 编码失败保留源文件，不自动改成 H.264/HEVC。

录制进程退出后重新检查直播状态；查询失败继续保留当前场次，确认下播后再合并。
视频规格一致时先无损合并；H.264/HEVC、尺寸或帧率变化时先统一为 AV1 再合并，
此时使用较小分段的尺寸、30 fps，音频仍直接复制。音轨格式变化时保留分段并报错，避免损失音质。
合并和转码都检查时长及音轨参数，校验通过后按 `KeepSource` 删除对应源文件。
未正常关闭的 FLV 从数据包时间戳恢复时长；仍无法确认完整性时保留源文件。
后处理在后台串行运行，待处理场次存在时暂停自动清理，避免删除仍在处理的录制文件。
场次及配置快照保存在 `recordings/.sessions/`，录制开始前登记分段；重启后恢复同场录制，
已下播的任务继续后处理，失败每五分钟重试。最终文件校验通过后才清理源分段。
没有场次清单的历史散落文件仍需人工核对，不能仅按标题猜测分组。

### 自动上传夸克

宿主机安装并授权夸克官方 `quarkclouddrive` 后，设置 `[Upload]`：
`Enabled = true`、`QuarkCli` 和 `Node` 的实际路径。`ParentFid` 留空使用工具默认目录。
安装用户定时器（项目位于 `/path/to/bili-recorder`；其他位置请调整 service）：
所附 service 使用 本地私有环境文件 声明 CLI 渠道，须与官方 CLI 已授权的渠道一致。

```bash
mkdir -p ~/.config/systemd/user
cp scripts/quark-upload.service scripts/quark-upload.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now quark-upload.timer
```

每五分钟检查一次顶层 `*.transcoded.mp4`。转码先写临时文件，校验后原子更名，
上传器只看到完整产物。上传成功确认文件大小并记录 SHA256；已上传文件跳过，
失败任务使用官方断点续传重试；进程中断后找回 CLI 任务，已完成任务核对云端后补记成功。
保留本地文件，未上传的最终文件不会被自动清理。CLI 的配置及任务记录须保留在其运行时目录。
上传状态在 `recordings/.quark-upload/manifest.json`，日志用
`journalctl --user -u quark-upload.service` 查看。已整理的旧 MP4 可显式加入队列，保留相对路径记录：

```bash
python3 -B quark_upload.py --enqueue recordings/某场直播
systemctl --user start quark-upload.service
```

自动清理只处理顶层已完成且已上传的 MP4；原始分段、临时文件和场次子目录不参与保留期清理。

### `[Maintenance]`

- `CleanupEnabled`: 是否启用清理
- `KeepDays`: 保留天数
- `KeepCount`: 保留文件数
- `CleanupExtensions`: 参与清理的扩展名

## Common Commands

```bash
# 启动或更新
./scripts/up.sh

# 查看容器状态
./scripts/up.sh ps

# 实时日志
docker compose logs -f bili-recorder

# 查看健康检查
docker inspect --format='{{json .State.Health}}' bili-recorder
```

运行中切换直播间：

```bash
sed -i 's/^RoomId = .*/RoomId = 123456/' config.ini
```
## License

MIT
