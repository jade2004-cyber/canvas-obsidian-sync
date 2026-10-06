# Canvas → Obsidian Sync

根据个人课表，在上课前自动将 Canvas 课程资料增量同步到 Obsidian。

> A small, read-only Canvas-to-Obsidian sync tool for macOS.

## 能做什么

- 读取 Canvas 中的课程文件、作业截止日期和最近公告
- 将文件整理到每门课程的 `Canvas/` 目录
- 根据文件名或 Canvas Module 自动归入 `Week 01`、`Week 02` 等目录
- 生成适合 Obsidian 阅读的 `索引.md`
- 只下载新增或更新的文件，未变化的文件会跳过
- 支持断点续传、大小限制、完整性校验和 Canvas 限流重试
- Canvas 中删除的文件不会直接从本地删除，而会移入 `_Archived/`
- 使用 macOS Keychain 保存 API Token
- 通过 LaunchAgent 在课前自动运行
- 支持设置教学期，学期结束后自动跳过
- 只在发现变化或同步失败时发送 macOS 通知

同步工具只发送读取请求，不会提交作业、修改课程或删除 Canvas 内容。

## 目录效果

```text
Your Semester/
└── COURSE101/
    └── Canvas/
        ├── 课程资料/
        ├── Week 01/
        ├── Week 02/
        ├── .canvas-sync-state.json
        └── 索引.md
```

## 使用条件

- macOS
- Python 3.9 或更高版本
- 学校允许创建 Canvas Access Token
- 已建立对应的 Obsidian 课程目录

## 快速安装

### 不熟悉终端：双击安装

1. 打开 [Releases](https://github.com/jade2004-cyber/canvas-obsidian-sync/releases/latest)
2. 下载并解压 `canvas-obsidian-sync-v*.zip`
3. 双击 `Install.command`
4. 按中文提示完成设置

如果 macOS 首次阻止运行，请在 Finder 中右键点击 `Install.command`，选择“打开”。不要运行来源不明或被他人修改过的副本。

安装包还提供：

| 文件 | 用途 |
| --- | --- |
| `Sync Now.command` | 立即同步全部课程 |
| `Status.command` | 查看上次结果和下次同步时间 |
| `Configure.command` | 修改教学期、课表、目录或课程 |
| `Diagnostics.command` | 生成不含 Token 的诊断信息 |
| `Update.command` | 下载新版并更新后台程序 |
| `Uninstall.command` | 移除定时任务，保留课程资料 |

### 熟悉终端：克隆仓库

```bash
git clone https://github.com/jade2004-cyber/canvas-obsidian-sync.git
cd canvas-obsidian-sync
./Install.command
```

安装器会依次询问：

- Canvas 地址与课程 ID
- Obsidian 学期目录和课程目录
- 教学期开始、结束日期
- 每门课程的上课时间
- 课前多少分钟同步
- 单个文件的大小上限

然后自动完成：

- 将 Token 保存到 macOS Keychain
- 生成配置文件
- 对全部课程执行第一次只读测试；测试失败时不会加载后台任务
- 生成并加载 LaunchAgent
- 按上课时间减去提前量计算同步时间
- 在加载后台任务前提示完成必要的 macOS 权限设置

例如，输入：

```text
COURSE101 class times: Mon 09:00, Wed 09:00
Minutes before class to sync: 45
```

系统会在周一和周三 `08:15` 同步 `COURSE101`。

## 查看状态与立即同步

双击 `Status.command` 可以看到每门课的上次同步时间、结果和下一次计划时间。首次定时同步尚未发生时会显示“尚未运行”。

双击 `Sync Now.command` 可以立即同步全部课程，不必等待下一节课。命令行用户也可以运行：

```bash
python3 manager.py status
python3 canvas_sync.py
```

修改配置时双击 `Configure.command`。安装器会读取现有设置作为默认值；选择保留现有课程时，无需重新输入每一个课程 ID。

## 手动配置

如果不使用安装器，可以复制并编辑示例配置：

```bash
cp config.example.json canvas_courses.json
```

配置格式：

```json
{
  "base_url": "https://canvas.example.edu",
  "keychain_service": "canvas-obsidian-sync",
  "vault_path": "/Users/YOU/Documents/Obsidian Vault/2026 Semester A",
  "max_file_size_mb": 500,
  "archive_removed": true,
  "courses": {
    "COURSE101": {
      "canvas_id": 123456,
      "vault_directory": "COURSE101"
    }
  }
}
```

Canvas 课程 ID 通常可以从课程网址中找到：

```text
https://canvas.example.edu/courses/123456
                                     └─ course ID
```

`vault_directory` 必须是 `vault_path` 下已经存在的课程目录。

### 保存 Canvas Token

在 Canvas 的个人设置中创建 Access Token。不要把 Token 写进 JSON、脚本、README 或截图。

将 Token 保存到 macOS Keychain：

```bash
security add-generic-password \
  -U \
  -a "$USER" \
  -s "canvas-obsidian-sync" \
  -w
```

命令会提示你输入 Token，输入内容不会写入终端历史。`-s` 后的名称要与配置中的 `keychain_service` 一致。

### 手动测试

先执行只读检查：

```bash
python3 canvas_sync.py --course COURSE101 --dry-run
```

确认结果后进行第一次同步：

```bash
python3 canvas_sync.py --course COURSE101
```

同步全部已配置课程：

```bash
python3 canvas_sync.py
```

### 手动设置课前同步

复制模板：

```bash
cp examples/com.example.canvas-sync.plist \
  ~/Library/LaunchAgents/com.example.canvas-sync.course101.plist
```

编辑 plist 中的以下内容：

- Python 路径
- 本仓库中 `canvas_scheduled_sync.py` 的绝对路径
- 课程代码
- 教学期开始和结束日期
- `Weekday`、`Hour`、`Minute`
- 日志保存位置

`Weekday` 使用 `0–7`，其中 `0` 和 `7` 都表示星期日，`1` 表示星期一。

加载任务：

```bash
launchctl bootstrap gui/$(id -u) \
  ~/Library/LaunchAgents/com.example.canvas-sync.course101.plist
```

立即试运行：

```bash
launchctl kickstart -k \
  gui/$(id -u)/com.example.canvas-sync.course101
```

查看状态：

```bash
launchctl print \
  gui/$(id -u)/com.example.canvas-sync.course101
```

## macOS 权限问题

如果 Obsidian Vault 位于 `Documents`，后台任务可能出现：

```text
PermissionError: [Errno 1] Operation not permitted
```

可在以下位置为 plist 中实际使用的 `python3` 开启文件访问权限：

```text
系统设置 → 隐私与安全性 → 完全磁盘访问权限
```

这是范围较大的权限。开启前请检查脚本内容，不要使用来源不明的版本。更谨慎的选择是将 Vault 放到不受该权限限制的位置。

## 卸载自动任务

双击 `Uninstall.command`，或运行：

```bash
python3 setup.py --uninstall
```

卸载器只移除定时任务，不会删除配置、Keychain Token、日志或已经同步到 Obsidian 的文件。

## 通知策略

后台同步会保持安静，以下情况才发送通知：

- 发现新增、更新、移动或归档的文件
- 作业截止日期或公告发生变化
- 文件超过配置的大小限制
- 同步失败，需要查看日志

如果所有文件都没有变化，则不会打扰你。手动编辑 plist，在参数中加入 `--no-notify` 可以关闭通知。

## 文件更新与删除

- 文件改名或调整分类时，会在本地安全移动，不重复下载
- 文件内容更新时，会下载新版本并校验大小与 SHA-256
- 下载中断后会从临时文件继续
- Canvas 中消失的文件会移动到 `Canvas/_Archived/YYYY-MM-DD/`
- 工具不会自动永久删除课程资料

## 安全说明

- 不要提交 `canvas_courses.json`
- 不要提交 `.canvas-sync-state.json`、日志或课程资料
- 不要在 Issue 中粘贴 Token、学号或受限课程链接
- 本项目不是 Canvas 官方产品
- 使用前请遵守学校及 Instructure Canvas 的相关规定

## 已知限制

- 这是定时检查，不是 Canvas 实时推送
- 电脑关机时不会执行；睡眠期间错过的任务通常会在唤醒后补跑
- 不同学校可能限制课程文件 API，此时工具会尝试读取 Modules 中可见的文件
- 只同步教师已在 Canvas 中开放给当前账户的内容

## License

[MIT](LICENSE)
