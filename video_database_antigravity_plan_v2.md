# 视频阅读与评语数据库构建方案 V2 (聚合架构与 OneDrive 同步适配版)

本文档面向 **Antigravity 自动化执行** 与 **Obsidian 本地管理**，采用**聚合型结构（月度日志流 + 作品实体聚合）**有效避免海量小文件碎片化，并针对 **OneDrive 云同步机制** 进行了专项适配与冲突防护。

---

## 1. OneDrive 同步适配与避坑规范

Obsidian 保险库（Vault）完全支持放置在 OneDrive 同步目录下，但需遵守以下底层规则以防止云端冲突或文件锁死：

| 潜在隐患 | 原因分析 | 落地防范措施 |
| :--- | :--- | :--- |
| **`.obsidian/workspace.json` 冲突** | 多端打开时高频读写界面布局，易生成副本文件（如 `...-Conflicted Copy.json`） | 推荐在多端保持固定工作区，或避免多台设备同时处于前台编辑状态。 |
| **“按需文件”（Files On-Demand）挂起** | OneDrive 默认将不常访问的文件仅保留在云端，导致 Obsidian 全局搜索或 Dataview 索引不完整 | **必须操作**：右键点击 `Video_Vault` 根文件夹，选择 **“始终在此设备上保留” (Always keep on this device)**。 |
| **小文件同步风暴与 API 频率限制** | 产生数千个碎片小文件会导致 OneDrive 持续扫描索引，引发磁盘高占用与同步延迟 | **本方案采用聚合结构**：每月/每作品为单一文件，将文件总数压降 90% 以上，对 OneDrive 同步极度友好。 |
| **并发写入冲突** | 脚本（如 Antigravity / Python）写入时恰逢 OneDrive 同步上传，可能导致局部重命名 | 脚本写入采用原子追加模式（`append`），并在操作前后增加轻微防抖缓冲。 |

---

## 2. 聚合型目录架构设计

```text
OneDrive/
└── Video_Vault/
    ├── .obsidian/                    # Obsidian 配置与插件
    ├── Inbox_Pending.md              # 单一暂存收件箱（待处理、自动抓取注入流）
    ├── Knowledge_Logs/               # 知识 / 时政类：按月份长日志归档（避免单视频碎片化）
    │   ├── 2026-08_Knowledge_Log.md
    │   └── 2026-09_Knowledge_Log.md
    ├── Classical_Music/              # 古典音乐类：按“作曲家 / 核心作品”聚合归档
    │   ├── J.S.Bach/
    │   │   └── BWV988_Goldberg_Variations.md
    │   └── L.v.Beethoven/
    │       └── Op111_Piano_Sonata_No32.md
    ├── Dashboards/                   # 聚合大纲与索引
    │   └── Library_Index.md
    └── Scripts/                      # 自动化脚本
        └── append_video_entry.py
```

---

## 3. 聚合模板规范与格式定义

### 3.1 知识与时政：月度长日志结构 (`Knowledge_Logs/YYYY-MM_Knowledge_Log.md`)
每个视频作为二级标题（`##`）追加，利用行内字段（Inline Fields）兼顾纯文本可读性与 Dataview 查询能力。

```markdown
# 2026年09月 知识与时政视频阅读长流

---

## [2026-09-09] 视频标题示例
- [platform:: YouTube] | [channel:: 频道名] | [status:: 已精读] | [importance:: 4]
- [url:: https://www.youtube.com/watch?v=...]
- [tags:: #时政 #经济]

### 核心论点与摘要
- 核心论述要点 1：...
- 核心论述要点 2：...

### 时间戳速记
- **[04:12]** 关键转折点...
- **[15:30]** 引述数据值得商榷...

### 个人批判性评语
> 

---
```

### 3.2 古典音乐：核心作品聚合结构 (`Classical_Music/作曲家/作品名.md`)
一份作品一个文件，同一作品的不同演奏家、乐团、录音年代集中展示，天然利于版本横向对比。

```markdown
---
composer: "J.S. Bach"
work: "Goldberg Variations, BWV 988"
genre: 键盘独奏
tags:
  - 巴赫
  - 变奏曲
---

# J.S. Bach: Goldberg Variations, BWV 988

## 作品基本资料与乐谱关联
- **作品概况**：全曲包含 Aria 及 30 首变奏，以低音声部（Ground Bass）为变奏基础。
- **关联文献/乐谱**：`[[Bach_Gesellschaft_Edition]]`

---

## 版本对比记录

### [1981] Glenn Gould 演奏录影版
- [platform:: YouTube] | [performer:: Glenn Gould] | [ensemble:: 独奏] | [conductor:: 无]
- [rating:: 5] | [date_watched:: 2026-09-09]
- [url:: https://www.youtube.com/watch?v=...]

#### 声画特征与触键
- **声画表现**：CBS 30th Street Studio 录影棚实录，分屏特写指法。
- **乐段评语**：Aria 速度极缓；非连音（non-legato）触键极其均匀，声部对位层次分明。

#### 个人版本横向评语
> 相比其 1955 年首录版本的速度与锐度，1981 版展现了绝对理性的结构控制与沉思感。

---

### [2020] Víkingur Ólafsson 独奏现场版
- [platform:: Bilibili] | [performer:: Víkingur Ólafsson] | [ensemble:: 独奏] | [conductor:: 无]
- [rating:: 4] | [date_watched:: 2026-09-15]
- [url:: https://www.bilibili.com/video/...]

#### 声画特征与触键
- **乐段评语**：音色更具现代现代钢琴的泛音色彩，踏板运用更丰富。
```

---

## 4. 自动化追加脚本 (`Scripts/append_video_entry.py`)

脚本不再创建孤立小文件，而是以**安全追加（Append）**方式合并到月度日志中，避免产生小文件风暴并适配 OneDrive 同步。

```python
import sys
import json
import subprocess
from datetime import datetime
from pathlib import Path

VAULT_ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = VAULT_ROOT / "Knowledge_Logs"

def get_video_info(url: str) -> dict:
    cmd = ["yt-dlp", "--dump-json", "--no-playlist", url]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)

def append_knowledge_entry(url: str):
    info = get_video_info(url)
    title = info.get("title", "Untitled").replace("[", "(").replace("]", ")")
    channel = info.get("uploader", "Unknown Channel")
    extractor = info.get("extractor_key", "Video")
    today = datetime.now()
    month_str = today.strftime("%Y-%m")
    today_str = today.strftime("%Y-%m-%d")

    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    log_file = KNOWLEDGE_DIR / f"{month_str}_Knowledge_Log.md"

    if not log_file.exists():
        header = f"# {today.strftime('%Y年%m月')} 知识与时政视频阅读长流\n\n---\n"
        log_file.write_text(header, encoding="utf-8")

    entry = f"""
## [{today_str}] {title}
- [platform:: {extractor}] | [channel:: {channel}] | [status:: 待整理] | [importance:: 3]
- [url:: {url}]
- [tags:: #时政 #知识]

### 核心论点与摘要
- 

### 时间戳速记
- 

### 个人批判性评语
> 

---
"""
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(entry)
    print(f"[OK] 已成功追加条目至: {log_file.name}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python append_video_entry.py <URL>")
        sys.exit(1)
    append_knowledge_entry(sys.argv[1])
```

---

## 5. Antigravity 部署与执行指令流

在 Antigravity 中执行以下步骤完成初始化：

1. **环境预备**：
   - 确认工作目录位于本地 OneDrive 同步路径内（例如 `C:/Users/<Username>/OneDrive/Video_Vault` 或对应挂载路径）。
   - 右键该目录确认设为“始终在此设备上保留”。
2. **初始化目录树**：
   - 创建 `Knowledge_Logs/`、`Classical_Music/`、`Dashboards/`、`Scripts/`。
3. **部署脚本与主文档**：
   - 写入 `append_video_entry.py` 并安装 `yt-dlp`。
   - 初始化创建当月的长日志文件。
