# Bili Recorder

一个面向 Linux 服务器的 B 站直播录制工具，主打长期后台运行和尽量少人工介入。

适合作为 Docker 常驻服务使用，支持开播检测、自动录制、断线恢复、录后转码和基础磁盘清理。

## Features

- 自动轮询直播状态，开播后开始录制
- 录制中断后自动重试，分段文件可自动合并
- 录后自动转码，优先 NVENC，不可用时回退 CPU
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

## Notes

- 公开仓库不包含你的本地 `config.ini`、`recordings/`、`docs/` 和运行状态文件。
- 首选运行方式是 Linux + Docker Compose。
- 首次启动前请确认宿主机已安装 Docker，并能正常拉取基础镜像。

## License

MIT
