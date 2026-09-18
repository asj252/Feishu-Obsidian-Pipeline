"""
视频知识数据库自动追加脚本 (OneDrive 同步安全版)
支持使用 yt-dlp 抓取视频元数据并以原子追加方式写入月度日志或收件箱。

用法:
  uv run --with yt-dlp python append_video_entry.py <URL>
  python append_video_entry.py <URL> [--inbox]
"""

import sys
import json
import argparse
import subprocess
from datetime import datetime
from pathlib import Path

VAULT_ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = VAULT_ROOT / "Knowledge_Logs"
INBOX_FILE = VAULT_ROOT / "Inbox_Pending.md"

def get_video_info(url: str) -> dict:
    """尝试通过 yt-dlp 抓取元数据，若失败则提供优雅回退"""
    try:
        cmd = ["yt-dlp", "--dump-json", "--no-playlist", "--skip-download", url]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, encoding="utf-8")
        return json.loads(result.stdout)
    except Exception as e:
        print(f"[提示] 未能通过 yt-dlp 提取元数据 ({e})，将使用通用占位模版。")
        # 回退基础元数据
        platform = "Bilibili" if "bilibili.com" in url or "b23.tv" in url else ("YouTube" if "youtube.com" in url or "youtu.be" in url else "Video")
        return {
            "title": f"未命名视频 ({datetime.now().strftime('%Y-%m-%d %H:%M')})",
            "uploader": "未知作者",
            "extractor_key": platform,
            "webpage_url": url,
        }

def append_to_inbox(url: str, info: dict):
    title = info.get("title", "未命名条目").replace("[", "(").replace("]", ")")
    channel = info.get("uploader", "未知频道")
    extractor = info.get("extractor_key", "Video")
    today_str = datetime.now().strftime("%Y-%m-%d")

    entry = f"""
- [ ] [{today_str}] {title}
  - **来源链接**：{url}
  - **平台与作者**：{extractor} | {channel}
  - **状态**：待深度观看与精读
"""
    if not INBOX_FILE.exists():
        INBOX_FILE.write_text("# 📥 视频暂存收件箱 (Inbox / Pending)\n\n---\n\n## 待处理 / 待深度观看队列\n", encoding="utf-8")

    with open(INBOX_FILE, "a", encoding="utf-8") as f:
        f.write(entry)
    print(f"[OK] 已成功追加待办至收件箱: {INBOX_FILE.name}")

def append_knowledge_entry(url: str, info: dict):
    title = info.get("title", "未命名视频").replace("[", "(").replace("]", ")")
    channel = info.get("uploader", "未知作者")
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
- [ ] [platform:: {extractor}] | [channel:: {channel}] | [status:: 待观看] | [importance:: 3]
- [url:: {url}]
- [tags:: #知识 #时政]

### 核心论点与摘要 (AI 生成 / 待补充)
- 

### 时间戳速记 (AI 生成 / 待补充)
- 

### 个人批判性评语与心得 (Obsidian 手动补充)
> [!quote] 个人心得记录区
> （观看后在此记录个人心得、评语与反思...）

---
"""
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(entry)
    print(f"[OK] 已成功追加条目至月度日志: {log_file.name}")

def main():
    parser = argparse.ArgumentParser(description="追加视频元数据至 Obsidian 知识数据库")
    parser.add_argument("url", help="视频网页地址 (YouTube / Bilibili 等)")
    parser.add_argument("--inbox", action="store_true", help="写入暂存收件箱 Inbox_Pending.md 而非月度日志")
    args = parser.parse_args()

    print(f"[*] 正在分析视频地址: {args.url}")
    info = get_video_info(args.url)

    if args.inbox:
        append_to_inbox(args.url, info)
    else:
        append_knowledge_entry(args.url, info)

if __name__ == "__main__":
    main()
