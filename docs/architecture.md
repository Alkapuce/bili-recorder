# 录制与归档调用链

```mermaid
flowchart TD
    Config[本地配置 config/local.ini] --> CLI[CLI / 监督进程]
    CLI --> A[房间 A 进程 + 独占锁]
    CLI --> B[房间 B 进程 + 独占锁]
    A --> AS[A 的分段与场次记录]
    B --> BS[B 的分段与场次记录]
    AS --> Lock[共享转码锁]
    BS --> Lock
    Lock --> Media[整场合并 / AV1 / 音频复制 / 媒体校验]
    Media --> Final[原子发布单个最终 MP4]
    Final --> Upload[宿主机上传定时器 / 官方 CLI 续传]
    Upload --> Cloud[夸克文件]
    Cloud --> Verify[AList 独立查询与完整回读 SHA256]
    Verify --> LocalCheck[再次核对本地身份与内容]
    LocalCheck --> Receipt[持久化删除核验回执]
    Receipt --> Delete[删除本地最终 MP4]
```

房间进程只写自己的录像、场次状态和心跳；转码锁限制重任务并发，录制互不等待。停止进程会通知 FFmpeg 收尾，保留未结束场次，重新启动后接续分段编号。

云端归档与本地镜像是显式互斥的策略。归档模式没有云端删除动作；镜像模式必须由用户在本地配置启用，并禁止上传后删除本地。回执先持久化再执行本地删除，允许处理崩溃后的重复调用。

源文件、完整性校验失败的文件和未知状态的场次均应保留。历史录像未登记场次时，不按标题推测合并。
