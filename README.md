# Filegram Replay Recorder

## 这个项目是干什么的

把 AI agent 操作文件的**行为日志**，变成一段**屏幕录制视频（MP4）**。

具体来说：AI agent（比如 Claude、GPT）在完成文件整理等任务时，会产生一系列操作记录——读了哪个文件、创建了哪个文件夹、把文件移到了哪里。这个项目把这些操作记录**"回放"**出来，像录屏一样生成视频。

## 核心原理

整个流程只有一步：**在 Docker 容器里，一边"演"一边"录"，最终输出 MP4。**

```
你的数据（行为日志 + 初始文件）
        │
        ▼
┌──────────────────────────────────────────────────────┐
│  Docker 容器（全自动，跑完就出视频）                      │
│                                                      │
│  虚拟屏幕（Xvfb）  ← 一个看不见的"显示器"               │
│       │                                              │
│       ▼                                              │
│  浏览器打开 WebUI（长得像桌面文件管理器）                 │
│       │                                              │
│       │  后端按时间顺序发送事件 ──→  前端播放动画         │
│       │  （t=22s 读文件）          （鼠标点击、打开文件） │
│       │  （t=64s 创建文件夹）      （右键→新建文件夹）    │
│       │  （t=80s 移动文件）        （拖拽移动动画）       │
│       │                                              │
│       │     ↑ 这就是 "replay"（回放）                  │
│       │     把日志里的操作按原始时间间隔重新演一遍          │
│       │                                              │
│  ffmpeg 同时在录屏  ← 把上面的动画画面录下来              │
│       │                                              │
│       ▼                                              │
│  输出 recordings/xxx.mp4                              │
└──────────────────────────────────────────────────────┘
```

简单说：**replay = 演，录屏 = 拍**。两件事同时发生在容器里，你只需要跑一个命令，等它结束就能拿到视频。

## 需要准备什么

### 环境
- **Docker Desktop**（Mac/Linux/Windows 都行，Apple Silicon Mac 原生支持）

### 输入数据（2 样东西）

#### 1. 行为日志（Trajectory）

一个 JSON 文件，记录 AI agent 的每一步操作。每个事件有：
- `event_type`：做了什么（读文件、写文件、移动文件……）
- `timestamp`：什么时候做的（秒数，从 0 开始）
- 其他字段：操作的具体信息

示例（`events_clean.json`）：
```json
[
  {
    "event_type": "file_read",
    "timestamp": 22.5,
    "file_path": "birthday_plan.txt"
  },
  {
    "event_type": "dir_create",
    "timestamp": 64.6,
    "dir_path": "01_emails/personal"
  },
  {
    "event_type": "file_move",
    "timestamp": 80.4,
    "old_path": "birthday_plan.txt",
    "new_path": "01_emails/personal"
  },
  {
    "event_type": "file_write",
    "timestamp": 119.7,
    "file_path": "01_emails/README.md",
    "operation": "create"
  }
]
```

支持的 event_type：

| 类型 | 含义 | 视频中的表现 |
|------|------|------------|
| `file_read` | 读/打开文件 | 鼠标点击文件，打开预览，滚动阅读 |
| `file_write` | 创建/写入文件 | 弹出编辑器动画，显示内容 |
| `file_edit` | 编辑已有文件 | 显示 diff 对比 |
| `file_move` | 移动文件到其他目录 | 右键菜单 → 剪切 → 粘贴动画 |
| `file_rename` | 重命名文件 | 右键菜单 → 重命名动画 |
| `file_copy` | 复制文件 | 右键菜单 → 复制 → 粘贴动画 |
| `file_delete` | 删除文件 | 右键菜单 → 删除动画 |
| `file_search` | 搜索文件 | 搜索框动画 |
| `file_browse` | 浏览目录 | 展开文件夹 |
| `dir_create` | 创建文件夹 | 右键 → 新建文件夹动画 |
| `context_switch` | 切换到另一个文件 | 鼠标切换标签 |
| `cross_file_reference` | 两个文件间的关联 | 并排对比视图 |

#### 2. 工作空间（Workspace）

一个文件夹，里面放着 **agent 开始操作之前**的所有文件。这就是"初始状态"。

比如一个文件整理任务，agent 要把散乱的文件分类到文件夹里。那 workspace 就应该是：
```
workspace/
├── birthday_plan.txt      ← 散乱在根目录
├── coffee_promo.eml       ← 散乱在根目录
├── campus_photo.jpg       ← 散乱在根目录
├── resume.pdf             ← 散乱在根目录
└── ...（其他待整理的文件）
```

**不要**放 agent 操作完成后的结果（比如已经分好类的文件夹）。

## 文件放在哪里

```
项目根目录/
├── demo/
│   ├── my_task_name/                          ← 行为日志
│   │   ├── events_clean.json                  ← 必需
│   │   └── media/                             ← 可选，文件内容快照
│   │       ├── blobs/                         ← 用于 file_write 时显示内容
│   │       └── manifest.json
│   └── pilot/sandbox/my_task_name/            ← 工作空间（初始文件）
│       ├── file1.txt
│       ├── file2.pdf
│       └── ...
├── recordings/                                ← 输出目录（自动创建）
│   └── my_task_name.mp4                       ← 生成的视频
├── run_auto_record.sh                         ← 运行脚本
└── docker/                                    ← 容器代码（不用动）
```

## 怎么跑

### 录制单个任务

```bash
./run_auto_record.sh <任务名> [播放速度]
```

任务名 = `demo/` 下面行为日志文件夹的名字，同时也是 `demo/pilot/sandbox/` 下面工作空间文件夹的名字。

```bash
# 正常速度
./run_auto_record.sh my_task_name 1.0

# 2 倍速（视频时长减半）
./run_auto_record.sh my_task_name 2.0
```

脚本会自动：构建 Docker 镜像 → 启动容器 → 回放 → 录屏 → 输出到 `recordings/my_task_name.mp4`

### 批量录制

```bash
for d in demo/p*/; do
  name=$(basename "$d")
  [ "$name" = "pilot" ] && continue
  [ ! -f "$d/events_clean.json" ] && continue
  [ -f "recordings/${name}.mp4" ] && echo "跳过: $name（已存在）" && continue
  ./run_auto_record.sh "$name" 1.0
done
```

## 完整示例：从零开始

```bash
# 1. 克隆项目
git clone https://github.com/KairuiHu/docker_for_filegram.git
cd docker_for_filegram

# 2. 准备数据（假设你有一个叫 my_experiment 的任务）
mkdir -p demo/my_experiment
mkdir -p demo/pilot/sandbox/my_experiment

# 把你的行为日志放进去
cp /path/to/your/events_clean.json demo/my_experiment/

# 把初始文件放进去
cp /path/to/your/initial_files/* demo/pilot/sandbox/my_experiment/

# 3. 录制
./run_auto_record.sh my_experiment 1.0

# 4. 查看结果
open recordings/my_experiment.mp4    # macOS
# 或
xdg-open recordings/my_experiment.mp4  # Linux
```

## 注意事项

- 首次运行会构建 Docker 镜像（下载依赖），需要几分钟，之后会用缓存
- 每个任务录制耗时 ≈ 行为日志的总时长 + 30 秒启动开销
- 输出视频：1920x1080，30fps
- Apple Silicon Mac 原生运行，不需要 x86 模拟
- 日志中的 dbus 错误可以忽略（容器内没有系统总线，不影响功能）
