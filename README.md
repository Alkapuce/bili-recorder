# Bili Recorder

Linux 上的 B 站直播录制程序。每个直播间由独立进程录制，同场断流分段在确认下播后合成一个文件，可自动转码、上传夸克并在云端核验通过后删除本地文件。

- 多房间同时录制、按房间启停、状态查询，重复进程会被目录锁拒绝。
- 同场分段与配置快照持久化；停止和重启继续原场次，查询失败不会被当成下播。
- 默认视频 AV1，音频直接复制。后台转码在各房间之间串行执行，不阻塞其他房间录制。
- 上传支持断点续传；归档模式永久保留云端文件，删除本地前独立回读云端文件核对 SHA256。
- 配置、凭据、录像和运行状态留在本地，不进入 Git 或镜像。

## 安装与配置

需要 Python >=3.10、FFmpeg / FFprobe（含 `libsvtav1`）；NVIDIA AV1 NVENC 可选。

```bash
uv sync --locked
cp config/example.ini config/local.ini
chmod 600 config/local.ini
```

编辑 `config/local.ini`，删除两个禁用的示例房间，添加真实直播间：

```ini
[Room:123456]
Enabled = true

[Room:654321]
Enabled = true
```

公共参数由 `[General]`、`[Recording]`、`[PostProcess]`、`[Maintenance]` 和 `[Upload]` 提供。
每个房间可覆盖 `SaveDir`、`CheckInterval`、`ParentFid`。默认文件保存在 `SaveDir/房间号/`；各房间目录禁止相同或互相嵌套。路径相对于**配置文件所在目录**，例如 `config/local.ini` 中 `SaveDir=../recordings`。

```bash
uv run bili-recorder check
uv run bili-recorder run
```

`run` 前台监督所有已启用房间，异常退出的房间五秒后重启。编辑 `Enabled` 或以下命令可控制正在运行的监督进程：

```bash
uv run bili-recorder stop 123456
uv run bili-recorder start 123456
uv run bili-recorder status
```

停止一个房间时，其 FFmpeg 收尾退出，场次保留，其他房间继续录制。`start/stop` 修改本地配置；须先运行监督进程。若想在独立终端分别管理进程：

```bash
uv run bili-recorder run --room 123456
uv run bili-recorder run --room 654321
```

独立模式用 Ctrl+C 停止；配置里的 `Enabled` 仅控制监督模式。同一房间不能同时被两种模式占用。更改输出目录、状态目录后须停止并重新启动；进行中的场次始终沿用开始录制时的配置快照。

可用 `--config /path/to/local.ini`（放在子命令之前）或 `BILI_CONFIG_PATH` 指定其他配置。旧的单房间 `[General] RoomId` 配置仍可读取，其 SaveDir 布局保持不变。

## 一场直播一个文件

录制阶段直接保存原流；FFmpeg 退出后继续检查是否仍在直播。断流重连、重试次数耗尽、接口暂时出错均保留同一场次，直到成功查询到下播。

后台按顺序合并分段、转码并核验时长与音轨，原子发布 `开始时间_房间号_直播标题.mp4`。只有发布成功后才按 `KeepSource` 清理源分段。状态与恢复记录在各房间目录的 `.sessions/`，未完成文件不会上传或参与保留期清理。

默认优先探测 `av1_nvenc`，不可用时用 `libsvtav1`（CRF 34、preset 6、10 bit、2 个编码线程），音频复制，MP4 索引前置。视频规格变化时统一为较小尺寸、30 fps 的 AV1 再合并；音轨规格变化时保留分段并报错，避免静默损失音频。`TranscodeEnabled=false` 时合并并封装为配置的输出格式。

多个房间共享转码锁，同时仍可录制。每个场次处理失败五分钟后重试；后处理队列积压时要留足磁盘。没有场次清单的历史散落文件不能自动猜测分组。

## 夸克归档与本地删除

在宿主机安装并授权夸克 `quarkclouddrive` CLI，填入本机 Node、CLI 路径与目标目录 FID：

```ini
[Upload]
Enabled = true
Mode = archive
DeleteLocal = true
Node = /path/to/node
QuarkCli = /path/to/quark-drive.cjs
ParentFid = your-target-folder-fid
AlistCredentials = /path/to/private-alist.json
```

`DeleteLocal=true` 需要 AList 的夸克驱动及一个**基本路径严格限定为同一目标目录**的账号。凭据 JSON 格式为 `{"url":"http://127.0.0.1:5244","username":"...","password":"..."}`，权限设为 `600`，不提交 Git。

```bash
uv run bili-recorder upload
# 单个房间或显式加入已整理的历史 MP4
uv run bili-recorder upload --room 123456
uv run bili-recorder upload --room 123456 --enqueue /absolute/path/inside/that/room/archive
```

上传失败时保留本地文件并在下一次调用续传。删除前必须同时满足：

1. 官方 CLI 返回成功、文件 ID 和大小，并保存上传回执。
2. AList 重新查询云端目录，核对文件 ID、大小，并完整读取云端文件核对 SHA256。
3. 再次核对本地 SHA256、大小、修改时间和文件身份。

任何一步失败均保留本地文件。核验回执先落盘再删除，崩溃后可继续收尾；记录保留在 `.quark-upload/manifest.json`。云端完整回读会额外消耗约一份录像大小的下载流量。上传器只自动扫描最终 MP4；其他输出格式须自行归档。

`Mode=archive` 从不删除或改名云端文件。旧的本地镜像同步需显式设置 `Mode=mirror`，并使用独立云目录的单房间配置；此模式会传播本地改名与删除，**禁止同时使用 DeleteLocal**。云端目录应由本项目专用，尤其是镜像模式。

`DeleteLocal=false` 可以只上传并保留本地，不要求 AList；这种情况下仅核对上传回执，不作云端回读核验。官方 CLI 渠道认证所需的环境变量由各自部署提供，不写入公共程序。

## 后台运行

Docker 负责录制与后处理，上传在能访问夸克 CLI 授权信息的宿主机执行：

```bash
./scripts/up.sh                   # 自动探测 GPU，构建并启动
./scripts/up.sh ps
docker compose logs -f bili-recorder
./scripts/up.sh down
```

使用目录挂载配置，宿主机修改 `config/local.ini` 后可热读取。不要复制含个人配置的目录进镜像。`scripts/up.sh` 会叠加可选的本地 `config/compose.local.yml`，可放网络模式等部署专属配置。Docker Compose 通过 `bili-recorder health` 检查已启用房间的心跳；状态在 `.runtime/房间号/status.json`。`init: true` 负责回收子进程。

每五分钟上传一次可使用 `scripts/quark-upload.service` 与 `.timer`：先把 service 的 `/path/to/bili-recorder` 替换为自己的路径，确认 `.venv` 已安装，再安装至 `~/.config/systemd/user/`。需要渠道环境变量时放在本地 `config/upload.env`。

```bash
systemctl --user daemon-reload
systemctl --user enable --now quark-upload.timer
journalctl --user -u quark-upload.service
```

升级旧部署前先确认没有录制或转码任务，再替换容器与宿主机定时器入口。历史顶层录像不会被新房间目录自动扫描；保留原配置可继续以旧单房间布局运行上传，迁移历史文件应单独核对，不直接删除。

## 项目结构与验证

```text
src/bili_recorder/
  cli.py             命令入口与按房间控制
  config.py          本地配置与房间参数
  supervisor.py      进程监督、启停与锁
  recorder.py        开播检测、录制与场次调度
  media.py           合并、转码和媒体校验
  session_store.py   场次持久化与恢复
  quark_upload.py    上传、续传和归档回执
  cloud_sync.py      云端独立校验与显式镜像
  naming.py          文件命名
  processes.py       FFmpeg 中断与收尾
  locking.py         进程互斥
config/example.ini   公共配置模板
config/local.ini     本地个人配置（忽略）
tests/               媒体、并发进程和上传失败路径测试
scripts/             Docker 启动与 systemd 模板
docs/                架构与实施记录
recordings/          录像与场次状态（忽略）
.runtime/            进程状态、共享转码锁（忽略）
```

```bash
uv run python -B -m unittest discover -s tests -v
docker compose config --quiet
bash -n scripts/up.sh
```

测试使用本地 HTTP 流、真实 FFmpeg、独立录制进程及外部云服务替身；不写入真实网盘。并发与归档场景的结果清单保存在 `.runtime/test-results/`。新环境需自行安装 FFmpeg；NVENC 能否工作取决于 GPU、驱动和 FFmpeg 构建。

MIT License。
