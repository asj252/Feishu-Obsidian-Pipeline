"""
飛書影片知識庫機器人服務 (Feishu Obsidian Video Bot - Smart Multimodal Pipeline)
支援：
1. Level 2 字幕軌優先解析
2. Rule 5 B站長視頻跨平台 YouTube 同源原片反向檢索與字幕補全
3. Level 3 原生語音多模態抽取（無字幕時直接聽音）
4. Level 1 結構化元數據知識圖譜兜底
5. 長視頻 (>=20分鐘) 逐字稿獨立存儲 (Rule 3)
6. 智能分流代理（Gemini 走代理，飛書長連接直連）
"""

import os
import re
import json
import logging
import sys
import base64
import tempfile
import urllib.parse
from datetime import datetime
from pathlib import Path
import time
import requests
from concurrent.futures import ThreadPoolExecutor
import warnings
warnings.filterwarnings("ignore")

# Windows 控制台 UTF-8 與無緩衝
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:
        pass

import lark_oapi as lark
from lark_oapi.ws import Client
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
import yt_dlp

# 基礎路徑設定
VAULT_ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = VAULT_ROOT / "Knowledge_Logs"
WEEKLY_DIR = KNOWLEDGE_DIR / "Weekly"
MONTHLY_DIR = KNOWLEDGE_DIR / "Monthly"
TRANSCRIPTS_DIR = VAULT_ROOT / "Transcripts"
INBOX_FILE = VAULT_ROOT / "Inbox_Pending.md"
ENV_FILE = VAULT_ROOT / ".env"
COOKIES_FILE = VAULT_ROOT / "cookies.txt"

def get_current_week_info(dt: datetime = None) -> tuple[str, str, str]:
    """回傳 (week_str, start_date_str, end_date_str)，如 ('2026-W39', '2026-09-21', '2026-09-27')"""
    if dt is None:
        dt = datetime.now()
    iso_year, iso_week, iso_weekday = dt.isocalendar()
    week_str = f"{iso_year}-W{iso_week:02d}"
    from datetime import timedelta
    start_of_week = dt - timedelta(days=iso_weekday - 1)
    end_of_week = start_of_week + timedelta(days=6)
    return week_str, start_of_week.strftime("%Y-%m-%d"), end_of_week.strftime("%Y-%m-%d")

# 載入 .env
def load_env():
    env_vars = {}
    if ENV_FILE.exists():
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env_vars[k.strip()] = v.strip()
    return env_vars

ENV = load_env()
APP_ID = ENV.get("FEISHU_APP_ID", "")
APP_SECRET = ENV.get("FEISHU_APP_SECRET", "")
GEMINI_KEY = ENV.get("GEMINI_API_KEY", "")

if not APP_ID or not APP_SECRET:
    logging.warning("尚未設定 FEISHU_APP_ID 或 FEISHU_APP_SECRET，請於 .env 文件中填入飛書憑證。")
if not GEMINI_KEY:
    logging.warning("尚未設定 GEMINI_API_KEY，請於 .env 文件中填入 Gemini API 金鑰。")

# 代理設定
PROXY_URL = ENV.get("HTTPS_PROXY") or ENV.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# 建立飛書 API 客戶端（用於回覆訊息）
feishu_client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()

def reply_feishu_message(chat_id: str, text: str):
    """向指定飛書會話回傳訊息"""
    try:
        req = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .build()) \
            .build()
        resp = feishu_client.im.v1.message.create(req)
        if not resp.success():
            logging.error(f"回傳飛書訊息失敗: {resp.code} - {resp.msg}")
        else:
            logging.info("已成功回覆飛書訊息")
    except Exception as e:
        logging.error(f"發送飛書回覆異常: {e}")

def resolve_short_url(url: str) -> str:
    """若是 b23.tv 或短鏈接，自動解析獲取真實跳轉網址"""
    if "b23.tv" in url:
        try:
            resp = requests.get(url, allow_redirects=True, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200 and "bilibili.com" in resp.url:
                clean_target = resp.url.split("?")[0]
                logging.info(f"b23.tv 短鏈接已解析為真實地址: {clean_target}")
                return clean_target
        except Exception as e:
            logging.warning(f"短鏈接解析警告: {e}")
    return url

def extract_video_items(text: str) -> list:
    """
    提取文字中的影片項目，包含 (url, title_hint, user_comment)。
    支援將兩個 link 之間的文字輸入提取為前一個 link 的評語。
    """
    url_pattern = re.compile(r'(?:https?://|www\.)[a-zA-Z0-9\.\-_/~\?#=%&:;+@!*]+')
    url_matches = list(url_pattern.finditer(text))
    if not url_matches:
        return []

    extracted_items = []
    for i, u_match in enumerate(url_matches):
        raw_url = u_match.group(0).rstrip(').,;!?\'"')
        clean_url = resolve_short_url(raw_url)
        
        prefix = text[:u_match.start()]
        title_hint = None
        entity_start = u_match.start()
        entity_end = u_match.end()
        
        # 1. 檢查 Markdown 格式: [title](url)
        m_md = re.search(r'\[([^\]\r\n]+)\]\(\s*$', prefix)
        # 2. 檢查 B站格式: 【title】 url
        m_bili = re.search(r'【+([^\r\n]+)】+\s*$', prefix)
        
        if m_md:
            title_hint = m_md.group(1).strip()
            entity_start = m_md.start()
            if text[u_match.end():].startswith(')'):
                entity_end = u_match.end() + 1
        elif m_bili:
            title_hint = re.sub(r'^[【\[]+|[】\]]+$', '', m_bili.group(1).strip()).strip()
            entity_start = m_bili.start()

        extracted_items.append({
            "url": clean_url,
            "title_hint": title_hint,
            "entity_start": entity_start,
            "entity_end": entity_end
        })

    results = []
    for i, item in enumerate(extracted_items):
        cur_end = item["entity_end"]
        if i + 1 < len(extracted_items):
            next_start = extracted_items[i + 1]["entity_start"]
            raw_comment = text[cur_end:next_start]
        else:
            raw_comment = text[cur_end:]
            
        clean_comment = raw_comment.strip()
        clean_comment = re.sub(r'^\s*[\)\]\}\-–—\s]+\s*', '', clean_comment).strip()
        
        results.append((item["url"], item["title_hint"], clean_comment))
        
    return results

def parse_duration_string(dur_str: str) -> int:
    """將 1:15:08 或 52:14 或 3:45 等字串轉換為秒數"""
    if not dur_str:
        return 0
    parts = dur_str.strip().split(':')
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 1:
            return int(parts[0])
    except Exception:
        pass
    return 0

def get_youtube_fallback_info(url: str, proxies: dict = None) -> dict:
    """
    當 yt-dlp 在 YouTube 遇到機器人驗證或反爬時，
    透過官方免登入 oEmbed API 與 YouTube 搜尋解析補全作者、真實標題與時長。
    """
    result = {"title": None, "uploader": None, "duration": 0, "duration_string": ""}
    vid_match = re.search(r'(?:v=|youtu\.be/|shorts/)([a-zA-Z0-9_-]{11})', url)
    video_id = vid_match.group(1) if vid_match else None
    
    # 1. 官方 oEmbed 獲取標題與作者頻道
    try:
        oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
        r = requests.get(oembed_url, proxies=proxies, timeout=8)
        if r.status_code == 200:
            d = r.json()
            result["title"] = d.get("title")
            result["uploader"] = d.get("author_name")
    except Exception as e:
        logging.warning(f"YouTube oEmbed 回退失敗: {e}")

    # 2. YouTube 搜尋結果解析時長
    if video_id:
        try:
            search_url = f"https://www.youtube.com/results?search_query={video_id}"
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            r = requests.get(search_url, headers=headers, proxies=proxies, timeout=8)
            if r.status_code == 200:
                match = re.search(r'var ytInitialData = ({.*?});</script>', r.text)
                if match:
                    data = json.loads(match.group(1))
                    sections = data.get('contents', {}).get('twoColumnSearchResultsRenderer', {}).get('primaryContents', {}).get('sectionListRenderer', {}).get('contents', [])
                    for sec in sections:
                        for item in sec.get('itemSectionRenderer', {}).get('contents', []):
                            v = item.get('videoRenderer')
                            if v and v.get('videoId') == video_id:
                                dur_text = v.get('lengthText', {}).get('simpleText', '')
                                if dur_text:
                                    result["duration_string"] = dur_text
                                    result["duration"] = parse_duration_string(dur_text)
                                if not result["title"]:
                                    result["title"] = "".join(run.get('text', '') for run in v.get('title', {}).get('runs', []))
                                if not result["uploader"]:
                                    result["uploader"] = "".join(run.get('text', '') for run in v.get('ownerText', {}).get('runs', []))
                                break
        except Exception as e:
            logging.warning(f"YouTube 搜尋解析時長回退失敗: {e}")
            
    return result

def get_video_info(url: str, title_hint: str = None) -> dict:
    """利用 yt_dlp 抓取影片元數據，若遇阻斷則自動啟動多級回退機制"""
    platform = "Bilibili" if "bilibili.com" in url or "b23.tv" in url else ("YouTube" if "youtube.com" in url or "youtu.be" in url else "Video")
    
    default_title = title_hint if title_hint else f"未命名影片 ({datetime.now().strftime('%Y-%m-%d %H:%M')})"
    if default_title and default_title.endswith(" - YouTube"):
        default_title = default_title[:-10].strip()
        
    info = {
        "title": default_title.replace("[", "(").replace("]", ")"),
        "uploader": "未知作者",
        "extractor_key": platform,
        "webpage_url": url,
        "description": "",
        "duration": 0,
        "duration_string": "",
        "original_url": None
    }
    
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'no_playlist': True,
            'extract_flat': False
        }
        if COOKIES_FILE.exists():
            ydl_opts['cookiefile'] = str(COOKIES_FILE)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            raw = ydl.extract_info(url, download=False)
            if raw:
                raw_title = raw.get("title")
                if raw_title:
                    info["title"] = raw_title.replace("[", "(").replace("]", ")")
                info["uploader"] = raw.get("uploader") or raw.get("channel") or info["uploader"]
                info["description"] = (raw.get("description") or "")[:3000]
                info["duration"] = raw.get("duration") or 0
                info["duration_string"] = raw.get("duration_string") or ""
    except Exception as e:
        logging.warning(f"yt_dlp 提取元數據提示: {e}，啟動回退機制。")
        
    # YouTube 多級元數據補全回退
    if platform == "YouTube" and (info["uploader"] == "未知作者" or info["duration"] == 0 or not info.get("title")):
        fb = get_youtube_fallback_info(url, proxies=PROXIES)
        if fb.get("title") and (not info.get("title") or info.get("title").startswith("未命名影片")):
            info["title"] = fb["title"].replace("[", "(").replace("]", ")")
        if fb.get("uploader") and info["uploader"] == "未知作者":
            info["uploader"] = fb["uploader"]
        if fb.get("duration") and info["duration"] == 0:
            info["duration"] = fb["duration"]
            info["duration_string"] = fb["duration_string"]
            
    return info

FOREIGN_REPOST_KEYWORDS = [
    'stanford', '斯坦福', '史丹佛', 'cs229', 'cs329', 'cs224', 'cs106',
    'mit', '麻省理工', 'harvard', '哈佛', 'berkeley', '伯克利', 'cmu', '卡耐基梅隆',
    'lex fridman', 'joe rogan', 'dwarkesh', 'huberman', 'y combinator', 'ted',
    'economist', '经济学人', 'bloomberg', 'cnbc', 'wsj', 'ft', 'bbc',
    '3blue1brown', 'veritasium', 'kurzgesagt', 'numberphile',
    'karpathy', 'yann lecun', 'hinton', 'andrew ng', '吴恩达', 'sutskever', 'altman', 'amodei',
    'openai', 'anthropic', 'deepmind', 'google deepmind', 'nvidia',
    '中英字幕', '双语', '中英双语', '熟肉', '生肉', '搬运', '英文字幕'
]

def search_youtube_counterpart(title: str, duration_sec: int = 0, proxies: dict = None) -> dict:
    """
    【Rule 5】依據 B 站標題在 YouTube 檢索最匹配的同源原片。
    必須通過雙重校驗：
    1. 海外轉載特徵檢測（避免國內本土原創視頻誤匹配）
    2. 時長誤差必須在 ±15% 以內
    """
    title_lower = title.lower()
    
    # 1. 檢測是否具備海外轉載特徵關鍵詞
    is_foreign = any(kw in title_lower for kw in FOREIGN_REPOST_KEYWORDS)
    if not is_foreign:
        logging.info(f"--> [Rule 5] 《{title}》未命中海外轉載特徵，判定為國內本土原創內容，跳過跨平台關聯。")
        return {'found': False}
        
    clean_title = re.sub(r'【.*?】|\[.*?\]|（.*?）|\(.*?\)|稍后再看|哔哩哔哩视频|谁为李白|不靠谱博士', ' ', title).strip()
    query = clean_title
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9,zh-TW;q=0.8,zh;q=0.7'
    }
    
    search_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}"
    try:
        r = requests.get(search_url, headers=headers, proxies=proxies, timeout=10)
        if r.status_code == 200:
            match = re.search(r'var ytInitialData = ({.*?});</script>', r.text)
            if match:
                data = json.loads(match.group(1))
                sections = data.get('contents', {}).get('twoColumnSearchResultsRenderer', {}).get('primaryContents', {}).get('sectionListRenderer', {}).get('contents', [])
                for sec in sections:
                    item_sec = sec.get('itemSectionRenderer', {}).get('contents', [])
                    for item in item_sec:
                        v = item.get('videoRenderer')
                        if v:
                            vid_id = v.get('videoId')
                            v_title = "".join(run.get('text', '') for run in v.get('title', {}).get('runs', []))
                            dur_text = v.get('lengthText', {}).get('simpleText', '')
                            cand_dur = parse_duration_string(dur_text)
                            
                            # 2. 時長誤差校驗（±15%）
                            if duration_sec > 0 and cand_dur > 0:
                                diff_ratio = abs(cand_dur - duration_sec) / duration_sec
                                if diff_ratio > 0.15:
                                    logging.info(f"--> [Rule 5] 時長差異過大 ({cand_dur}s vs {duration_sec}s, 誤差 {diff_ratio:.1%})，放棄候選: {v_title}")
                                    continue
                                    
                            return {
                                'found': True,
                                'url': f"https://www.youtube.com/watch?v={vid_id}",
                                'title': v_title,
                                'duration_text': dur_text
                            }
    except Exception as e:
        logging.warning(f"YouTube 反向檢索出錯: {e}")
    return {'found': False}

def extract_subtitles_text(url: str) -> tuple[str, str]:
    """嘗試提取影片外掛字幕（繁簡中文、英文）"""
    try:
        ydl_opts = {
            'skip_download': True,
            'quiet': True,
            'no_warnings': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': ['zh-Hans', 'zh-Hant', 'zh', 'en', 'en-US'],
        }
        if COOKIES_FILE.exists():
            ydl_opts['cookiefile'] = str(COOKIES_FILE)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            raw = ydl.extract_info(url, download=False)
            subs = raw.get('subtitles') or {}
            auto_subs = raw.get('automatic_captions') or {}
            
            target_langs = ['zh-Hans', 'zh-Hant', 'zh', 'en', 'en-US']
            chosen = None
            for lang in target_langs:
                if lang in subs and subs[lang]:
                    chosen = (subs[lang], f"{lang} (手動字幕)")
                    break
            if not chosen:
                for lang in target_langs:
                    if lang in auto_subs and auto_subs[lang]:
                        chosen = (auto_subs[lang], f"{lang} (自動字幕)")
                        break
                        
            if chosen:
                sub_list, lang_tag = chosen
                fmt_url = None
                for ext in ['json3', 'vtt', 'srt']:
                    for it in sub_list:
                        if it.get('ext') == ext:
                            fmt_url = it.get('url')
                            break
                    if fmt_url:
                        break
                if not fmt_url and sub_list:
                    fmt_url = sub_list[0].get('url')
                    
                if fmt_url:
                    r = requests.get(fmt_url, proxies=PROXIES, timeout=12)
                    if r.status_code == 200:
                        content = r.text
                        if 'events' in content:
                            data = json.loads(content)
                            lines = []
                            for ev in data.get('events', []):
                                piece = "".join(s.get('utf8', '') for s in ev.get('segs', [])).strip()
                                if piece:
                                    t_ms = ev.get('tStartMs', 0)
                                    lines.append(f"[{int(t_ms//60000):02d}:{int((t_ms%60000)//1000):02d}] {piece}")
                            return "\n".join(lines), lang_tag
                        else:
                            clean = re.sub(r'<[^>]+>', '', content)
                            clean = re.sub(r'\d{2}:\d{2}:\d{2}\.\d{3} --> \d{2}:\d{2}:\d{2}\.\d{3}.*\n', '', clean)
                            return clean[:20000], lang_tag
    except Exception as e:
        logging.warning(f"字幕提取跳過: {e}")
    return "", ""

def download_audio_stream(url: str, output_dir: Path) -> Path:
    """快速抽取輕量音訊流（限時長 <= 30分鐘或檔案 <= 25MB）"""
    out_tmpl = str(output_dir / "%(id)s.%(ext)s")
    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'outtmpl': out_tmpl,
        'quiet': True,
        'no_warnings': True,
        'max_filesize': 25 * 1024 * 1024
    }
    if COOKIES_FILE.exists():
        ydl_opts['cookiefile'] = str(COOKIES_FILE)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            r = ydl.extract_info(url, download=True)
            if r:
                vid_id = r.get('id')
                for f in output_dir.glob(f"{vid_id}.*"):
                    if f.suffix.lower() in ['.m4a', '.mp3', '.aac', '.webm', '.ogg']:
                        return f
    except Exception as e:
        logging.warning(f"音訊抽取跳過: {e}")
    return None

def process_video_pipeline(url: str, title_hint: str = None) -> tuple[dict, str, str, str]:
    """
    智慧多級精讀流水線核心調度
    返回 (info, summary, mode_name, transcript_content)
    """
    # 1. 抓取元數據
    info = get_video_info(url, title_hint=title_hint)
    duration = info.get("duration") or 0
    title = info.get("title", "")
    
    # 2. Level 2: 嘗試抓取原網址字幕
    sub_text, sub_lang = extract_subtitles_text(url)
    if sub_text and len(sub_text.strip()) > 100:
        logging.info(f"--> [流水線] 命中影片原生字幕軌 ({sub_lang})，啟動字幕逐字稿精讀...")
        prompt = f"""請以資深學者與分析師的角度，認真精讀以下影片的完整字幕逐字稿，為 Obsidian 知識庫生成專業、深刻的繁體中文精讀條目。
必須完全使用繁體中文（台灣正體）。

影片標題：{title}
影片網址：{url}
時長：{info.get('duration_string', '')} | 來源頻道：{info.get('uploader', '')}

完整逐字稿內容：
{sub_text[:30000]}

請按以下結構輸出（請勿包含代碼塊標記）：

### 核心論點與摘要 (Gemini 逐字稿深度精讀)
- **1. 關鍵論述**：...
- **2. 關鍵論述**：...
- **3. 關鍵論述**：...

### 時間戳與關鍵章節速記 (AI 生成)
- **[00:00]** ...
- **[00:00]** ...

### 深度洞察與分析 (AI 生成)
- **底層架構與關鍵啟發**：...
"""
        api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key={GEMINI_KEY}"
        resp = requests.post(api_url, json={"contents": [{"parts": [{"text": prompt}]}]}, proxies=PROXIES, timeout=35)
        if resp.status_code == 200:
            summary = resp.json()['candidates'][0]['content']['parts'][0]['text'].strip()
            return info, summary, f"字幕逐字稿深度精讀 ({sub_lang})", sub_text

    # 3. Rule 5: 若為 B 站影片且無字幕，啟動跨平台 YouTube 同源原片反向檢索
    if info.get("extractor_key") == "Bilibili" and not sub_text:
        logging.info("--> [Rule 5] B 站影片無字幕，啟動跨平台 YouTube 同源檢索...")
        yt_match = search_youtube_counterpart(title, duration_sec=duration, proxies=PROXIES)
        if yt_match.get("found"):
            yt_url = yt_match.get("url")
            yt_title = yt_match.get("title")
            logging.info(f"--> [Rule 5] 找到 YouTube 同源原片: {yt_url} ({yt_title})")
            info["original_url"] = yt_url
            
            # 嘗試抓取 YouTube 原片字幕
            yt_sub, yt_lang = extract_subtitles_text(yt_url)
            if yt_sub and len(yt_sub.strip()) > 100:
                logging.info(f"--> [Rule 5] 成功提取 YouTube 原片字幕 ({yt_lang})，啟動同源精讀...")
                prompt = f"""請以資深學者與分析師的角度，認真精讀以下 YouTube 同源原片的完整字幕逐字稿，為 Obsidian 知識庫生成專業、深刻的繁體中文精讀條目。
必須完全使用繁體中文（台灣正體）。

影片標題：{title}
B站轉載連結：{url}
YouTube原片連結：{yt_url}（原片標題：{yt_title}）
時長：{info.get('duration_string', '')} | 頻道/作者：{info.get('uploader', '')}

原片完整字幕逐字稿：
{yt_sub[:30000]}

請按以下結構輸出（請勿包含代碼塊標記）：

### 核心論點與摘要 (Gemini 跨平台同源原片精讀)
- **1. 關鍵論述**：...
- **2. 關鍵論述**：...
- **3. 關鍵論述**：...

### 時間戳與關鍵章節速記 (AI 生成)
- **[00:00]** ...
- **[00:00]** ...

### 深度洞察與分析 (AI 生成)
- **底層架構與關鍵啟發**：...
"""
                api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key={GEMINI_KEY}"
                resp = requests.post(api_url, json={"contents": [{"parts": [{"text": prompt}]}]}, proxies=PROXIES, timeout=35)
                if resp.status_code == 200:
                    summary = resp.json()['candidates'][0]['content']['parts'][0]['text'].strip()
                    return info, summary, f"跨平台 YouTube 同源字幕補全 ({yt_lang})", yt_sub

    # 4. Level 3: 若無字幕且時長 <= 30分鐘，執行語音抽取 + Gemini 原生語音多模態分析
    if 0 < duration <= 1800:
        logging.info("--> [流水線] 無外掛字幕，但時長在 30 分鐘內，啟動輕量語音抽取...")
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = download_audio_stream(url, Path(tmpdir))
            if audio_path and audio_path.exists():
                file_size_mb = round(audio_path.stat().st_size / 1024 / 1024, 2)
                logging.info(f"--> [流水線] 音訊抽取完成 ({file_size_mb} MB)，傳遞至 Gemini Flash 進行多模態聽音解析...")
                
                b64_audio = base64.b64encode(audio_path.read_bytes()).decode('utf-8')
                mime = "audio/mp4" if audio_path.suffix == '.m4a' else f"audio/{audio_path.suffix.replace('.', '')}"
                
                prompt = f"""請以資深專業分析師的角度，直接聽取這段完整的音訊，為本影片生成極致深刻、客觀的 Obsidian 筆記條目。
必須全部使用繁體中文（台灣正體）。

影片標題：{title}
時長：{info.get('duration_string', '')} | 頻道/作者：{info.get('uploader', '')}

請按以下結構輸出（請勿包含外部代碼塊標記）：

### 核心論點與摘要 (Gemini 原生語音多模態精讀)
- **1. 關鍵論述**：...
- **2. 關鍵論述**：...
- **3. 關鍵論述**：...

### 時間戳與關鍵章節速記 (AI 語音生成)
- **[00:00]** 開篇主題引入...
- **[00:00]** 核心轉折或案例剖析...
- **[00:00]** 總結與核心價值...

### 深度洞察與分析 (AI 生成)
- **底層邏輯與啟示**：...
"""
                payload = {
                    "contents": [{
                        "parts": [
                            {"inline_data": {"mime_type": mime, "data": b64_audio}},
                            {"text": prompt}
                        ]
                    }]
                }
                api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key={GEMINI_KEY}"
                resp = requests.post(api_url, json=payload, proxies=PROXIES, timeout=60)
                if resp.status_code == 200:
                    summary = resp.json()['candidates'][0]['content']['parts'][0]['text'].strip()
                    return info, summary, "Gemini 原生語音多模態精讀 (Level 3)", ""

    # 5. Level 1: 結構化元數據與知識圖譜兜底 (適用於超長視頻或音訊跳過)
    logging.info("--> [流水線] 啟動結構化元數據與大模型知識圖譜精讀...")
    prompt = f"""請為以下影片生成專業、深刻的 Obsidian 筆記條目，必須全部使用標準繁體中文（台灣正體）。

影片標題：{title}
影片網址：{url}
時長：{info.get('duration_string', '')} | 作者：{info.get('uploader', '')}
簡介內容：
{info.get('description', '')}

請按以下結構輸出（請勿包含代碼塊標記）：

### 核心論點與摘要 (Gemini 生成繁體中文精讀)
- **1. 關鍵論述**：...
- **2. 關鍵論述**：...
- **3. 關鍵論述**：...

### 時間戳與關鍵章節速記 (AI 生成)
- **[00:00]** ...
- **[00:00]** ...

### 深度洞察與分析 (AI 生成)
- **底層架構與啟發**：...
"""
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key={GEMINI_KEY}"
    try:
        resp = requests.post(api_url, json={"contents": [{"parts": [{"text": prompt}]}]}, proxies=PROXIES, timeout=30)
        if resp.status_code == 200:
            summary = resp.json()['candidates'][0]['content']['parts'][0]['text'].strip()
            long_transcript = ""
            if duration >= 1200:
                logging.info(f"--> [Level 1] 長視頻 ({info.get('duration_string', '')}) 同步生成精修結構化演講逐字稿與筆記...")
                t_prompt = f"""請以資深學者與專業速記員的角度，為以下這場長達 {info.get('duration_string', '')} 的重要學術/專業演講，生成一份結構嚴謹、極其深刻的「精修整理逐字稿與全篇深度筆記」。
必須全部使用繁體中文（台灣正體）。

影片標題：{title}
主講機構/頻道：{info.get('uploader', '')}
影片時長：{info.get('duration_string', '')}
簡介內容：
{info.get('description', '')}

請按照以下規範生成（請勿包含外部 Markdown 代碼塊標記）：
## 目錄導航
1. [章節一：...]
2. [章節二：...]
...

### 章節一：... (00:00 - ...)
（以口語化、生動且不失學術深度的第一人稱演講視角，忠實還原並深度展開主講人的核心論據、經典案例、政治/哲學/經濟邏輯推導...）

### 章節二：...
...
"""
                try:
                    t_resp = requests.post(api_url, json={"contents": [{"parts": [{"text": t_prompt}]}]}, proxies=PROXIES, timeout=60)
                    if t_resp.status_code == 200:
                        long_transcript = t_resp.json()['candidates'][0]['content']['parts'][0]['text'].strip()
                except Exception as ex:
                    logging.warning(f"生成長視頻擴展逐字稿提示: {ex}")
            return info, summary, "結構化元數據精讀 (Level 1)", long_transcript
        else:
            logging.error(f"Level 1 Gemini API 失敗 (HTTP {resp.status_code}): {resp.text[:300]}")
    except Exception as e:
        logging.error(f"Level 1 Gemini 請求異常: {e}")

    # 降級兜底
    fallback_summary = f"""### 核心論點與摘要 (待分析)
- 標題：{title}
- 來源簡介：{info.get('description', '')[:200]}

### 時間戳與關鍵章節速記
- **[00:00]** 導言與全篇展開
"""
    return info, fallback_summary, "基礎待看條目", ""

def append_to_vault(url: str, info: dict, ai_summary: str, mode_name: str, transcript_text: str = "", user_comment: str = "") -> str:
    """原子寫入 Obsidian 週度日誌流 (Rule 7)，並強制執行長視頻獨立逐字稿規範 (Rule 3) 與評語記錄 (Rule 6)"""
    today = datetime.now()
    week_str, start_date_str, end_date_str = get_current_week_info(today)
    today_str = today.strftime("%Y-%m-%d")
    month_str = today.strftime("%Y-%m")
    
    WEEKLY_DIR.mkdir(parents=True, exist_ok=True)
    log_file = WEEKLY_DIR / f"{week_str}_Knowledge_Log.md"
    
    if not log_file.exists():
        header = f"""# {week_str[:4]}年第{week_str[-2:]}週 ({start_date_str} ~ {end_date_str}) 知識與時政影片閱讀週度流

- [status:: 收集進行中] | [organized:: false]
- [week:: {week_str}]

> **使用說明**：
> 1. 本文檔為 {week_str} 之週度影片精讀長流（每週單一檔案線性聚合，防範小檔案同步風暴）。
> 2. 由飛書機器人自動分析並即時追加於文末，支援隨筆評語自動記錄。
> 3. 在飛書發送「**zl，整理**」指令時，本週之前的未整理週度日誌將全自動歸檔為「月度分類檔案」，並生成好中壞評價與統計報表。

---
"""
        log_file.write_text(header, encoding="utf-8")
        
    title = info.get("title", "未命名影片")
    platform = info.get("extractor_key", "Video")
    channel = info.get("uploader", "未知作者")
    duration = info.get("duration") or 0
    duration_str = info.get("duration_string") or ""
    
    # 處理 YouTube 原片雙鏈 (Rule 5)
    original_url_line = f"- [original_url:: {info['original_url']}]\n" if info.get("original_url") else ""
    
    # 執行系統規範 3：長視頻（>=20分鐘，即 1200 秒）獨立歸檔逐字稿
    transcript_backlink = ""
    if duration >= 1200 or transcript_text:
        month_transcript_dir = TRANSCRIPTS_DIR / month_str
        month_transcript_dir.mkdir(parents=True, exist_ok=True)
        
        safe_title = re.sub(r'[\\/:*?"<>|]', '_', title)[:35].strip()
        t_filename = f"{today_str}_{safe_title}_逐字稿.md"
        t_file = month_transcript_dir / t_filename
        
        body_text = transcript_text if transcript_text else f"> ℹ️ 本影片時長為 {duration_str}（超長視頻），已按系統規範預留獨立歸檔檔案。"
        t_content = f"""# 📜 整理逐字稿與深度筆記：{title}

> **關聯導航**：
> - 📑 返回週度日誌精讀條目：[[Knowledge_Logs/Weekly/{week_str}_Knowledge_Log#{today_str} {title}|{week_str}週度日誌]]
> - 🔗 原始影片連結：[{platform}]({url})
> - ⏱️ 影片時長：{duration_str} | 頻道/作者：{channel}
> - 模式標籤：{mode_name}
> - 規範依據：遵循 [[Dashboards/System_Rules#3. 長視頻逐字稿獨立歸檔規範 (Long Video Transcripts)|長視頻逐字稿獨立存儲規範]]

---

### 完整整理逐字稿 / 筆記
{body_text}
"""
        if not t_file.exists() or transcript_text:
            t_file.write_text(t_content, encoding="utf-8")
            
        transcript_backlink = f"- 📄 [[Transcripts/{month_str}/{t_filename[:-3]}|查看完整整理逐字稿 (Transcript)]]\n"

    # 執行規範 6：心得評語區塊處理
    if user_comment and user_comment.strip():
        formatted_comment_lines = "\n".join(f"> {line}" for line in user_comment.strip().splitlines())
        comment_block = f"""### 個人批判性評語與心得 (飛書隨筆記錄)
> [!quote] 筆記與心得記錄區
{formatted_comment_lines}"""
    else:
        comment_block = """### 個人批判性評語與心得 (Obsidian 手動補充)
> [!quote] 筆記與心得記錄區
> （觀看後在此記錄個人心得、反思與批判性評語...）"""

    entry = f"""
## [{today_str}] {title}
- [ ] [platform:: {platform}] | [channel:: {channel}] | [status:: 待觀看] | [importance:: 3]
- [url:: {url}]
{original_url_line}- [tags:: #AI #科技 #知識]
- [精讀模式:: {mode_name}]
{transcript_backlink}
{ai_summary}

{comment_block}

---
"""
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(entry)
        
    return log_file.name

def append_user_comment_to_latest_entry(new_comment: str) -> tuple[bool, str]:
    """
    在 Obsidian 週度知識日誌中定位最後一個影片條目，並將心得評語原子寫入或追加。
    回傳: (是否成功, 影片標題)
    """
    today = datetime.now()
    week_str, _, _ = get_current_week_info(today)
    
    # 依序尋找目標日誌檔案：當週週度日誌 -> 最近週度日誌 -> 根目錄日誌
    target_files = []
    current_weekly_log = WEEKLY_DIR / f"{week_str}_Knowledge_Log.md"
    if current_weekly_log.exists():
        target_files.append(current_weekly_log)
        
    target_files.extend(sorted(WEEKLY_DIR.glob("*_Knowledge_Log.md"), reverse=True))
    target_files.extend(sorted(KNOWLEDGE_DIR.glob("*_Knowledge_Log.md"), reverse=True))
    
    log_file = None
    for f in target_files:
        if f.exists():
            log_file = f
            break
            
    if not log_file:
        return False, ""
            
    try:
        content = log_file.read_text(encoding="utf-8")
        entry_pattern = re.compile(r'(?m)^##\s*\[\d{4}-\d{2}-\d{2}\]\s*(.+)$')
        matches = list(entry_pattern.finditer(content))
        if not matches:
            return False, ""
            
        last_match = matches[-1]
        title = last_match.group(1).strip()
        last_entry_start = last_match.start()
        last_entry_text = content[last_entry_start:]
        
        comment_header_pattern = re.compile(r'(###\s*個人批判性評語與心得.*?\n)(>.*?)(?=\n---\n|\Z)', re.DOTALL)
        m_comment = comment_header_pattern.search(last_entry_text)
        
        formatted_comment_lines = "\n".join(f"> {line}" for line in new_comment.strip().splitlines())
        
        if m_comment:
            header_str = m_comment.group(1)
            new_header = re.sub(r'###\s*個人批判性評語與心得.*', '### 個人批判性評語與心得 (飛書隨筆記錄)\n', header_str)
            existing_quotes = m_comment.group(2).strip()
            
            if "觀看後在此記錄" in existing_quotes or "观看后在此记录" in existing_quotes:
                new_quote_block = f"""> [!quote] 筆記與心得記錄區\n{formatted_comment_lines}"""
            else:
                new_quote_block = existing_quotes + "\n" + formatted_comment_lines
                
            updated_entry = last_entry_text[:m_comment.start()] + new_header + new_quote_block + "\n" + last_entry_text[m_comment.end():]
        else:
            new_block = f"""\n### 個人批判性評語與心得 (飛書隨筆記錄)\n> [!quote] 筆記與心得記錄區\n{formatted_comment_lines}\n"""
            if "\n---\n" in last_entry_text:
                idx = last_entry_text.rfind("\n---\n")
                updated_entry = last_entry_text[:idx] + new_block + last_entry_text[idx:]
            else:
                updated_entry = last_entry_text + new_block
                
        updated_content = content[:last_entry_start] + updated_entry
        log_file.write_text(updated_content, encoding="utf-8")
        return True, title
    except Exception as e:
        logging.error(f"寫入評語至日誌出錯: {e}")
        return False, ""

def classify_and_evaluate_entry(title: str, tags: list, summary: str, user_comment: str, existing_categories: list[str]) -> dict:
    """
    利用 Gemini API 進行語意分類與評語好中壞評價判定。
    回傳: {"category": str, "is_new": bool, "evaluation": "好"|"中"|"壞"}
    """
    if not GEMINI_KEY:
        cat = "AI與大模型" if any("ai" in t.lower() for t in tags) or "ai" in title.lower() else "綜合知識"
        is_new = cat not in existing_categories
        eval_res = "好" if any(k in user_comment for k in ["好", "讚", "棒", "推薦", "精彩"]) else ("壞" if any(k in user_comment for k in ["差", "爛", "不嚴謹", "浪費"]) else "中")
        return {"category": cat, "is_new": is_new, "evaluation": eval_res}
        
    prompt = f"""請針對以下影片知識庫條目進行【主題分類】與【評語評價判定】：

影片標題：{title}
影片標籤：{", ".join(tags) if tags else "無"}
核心摘要片段：{summary[:500]}
使用者個人心得/評語：{user_comment if user_comment else "（無使用者評語）"}

現有類別庫：{json.dumps(existing_categories, ensure_ascii=False) if existing_categories else "[]"}

請嚴格遵循以下要求：
1. 【主題分類】(category)：
   - 若現有類別庫中有語意合適的類別，請優先選用現有類別（必須精確匹配現有名稱）；
   - 若現有類別庫為空，或現有類別皆無法合適涵蓋該影片主題，請提出一個精準簡練的繁體中文新類別名稱（2~6 個字，例如：「AI與大模型」、「軟體架構與程式開發」、「全球經濟與金融」、「地緣政治與歷史」、「人文社科」、「生物醫藥」等）。
2. 【評語評價判定】(evaluation)：
   - 根據使用者個人心得/評語的情感傾向判定：
     - 若評語表達讚賞、收穫大、強烈推薦、高價值、深刻、論述嚴謹 -> "好"
     - 若評語指出錯誤、批判論點不嚴謹、內容水、浪費時間、強烈質疑 -> "壞"
     - 若評語為客觀筆記、中性描述、正反皆有、或使用者無評語（如只有預設佔位符） -> 預設為 "中"

請以純 JSON 格式輸出（不得包含任何 markdown 代碼塊標記）：
{{"category": "類別名稱", "is_new": true或false, "evaluation": "好或中或壞"}}
"""
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key={GEMINI_KEY}"
    try:
        resp = requests.post(api_url, json={"contents": [{"parts": [{"text": prompt}]}]}, proxies=PROXIES, timeout=25)
        if resp.status_code == 200:
            text = resp.json()['candidates'][0]['content']['parts'][0]['text']
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if m:
                res = json.loads(m.group(0))
                cat = res.get("category", "").strip().strip('"\'')
                eval_val = res.get("evaluation", "中").strip()
                if eval_val not in ["好", "中", "壞"]:
                    eval_val = "中"
                is_new = res.get("is_new", cat not in existing_categories)
                if not cat:
                    cat = "綜合知識"
                return {"category": cat, "is_new": is_new, "evaluation": eval_val}
    except Exception as e:
        logging.warning(f"Gemini 歸檔分類請求異常: {e}")

    cat = "AI與大模型" if any("ai" in t.lower() for t in tags) or "ai" in title.lower() else "綜合知識"
    is_new = cat not in existing_categories
    eval_res = "好" if any(k in user_comment for k in ["好", "讚", "棒", "推薦", "精彩"]) else ("壞" if any(k in user_comment for k in ["差", "爛", "不嚴謹", "浪費"]) else "中")
    return {"category": cat, "is_new": is_new, "evaluation": eval_res}

def organize_weekly_logs_worker(chat_id: str) -> None:
    """
    執行 'zl，整理' 流程：
    1. 掃描未整理過的週度日誌（排除本週，排除已整理過）
    2. 按月份將條目分類歸檔至月度分檔案日誌中
    3. 動態識別或新建類別，並統計各類別加入數量
    4. 依評語標記 [evaluation:: 好/中/壞]（無評語預設中等）
    5. 回報詳細統計至飛書
    """
    try:
        today = datetime.now()
        current_week, _, _ = get_current_week_info(today)
        
        WEEKLY_DIR.mkdir(parents=True, exist_ok=True)
        MONTHLY_DIR.mkdir(parents=True, exist_ok=True)
        
        weekly_files = sorted(WEEKLY_DIR.glob("*_Knowledge_Log.md"))
        eligible_files = []
        for f in weekly_files:
            match = re.match(r'^(\d{4}-W\d{2})_Knowledge_Log\.md$', f.name)
            if match:
                w_str = match.group(1)
                # 排除本週進行中日誌
                if w_str >= current_week:
                    continue
                content = f.read_text(encoding="utf-8")
                # 排除已整理過的日誌
                if "[organized:: true]" in content or "[status:: 已整理歸檔]" in content:
                    continue
                eligible_files.append((w_str, f, content))
                
        if not eligible_files:
            reply_feishu_message(
                chat_id,
                f"ℹ️ 掃描完成：目前沒有過去待整理的週度日誌。\n"
                f"👉 本週進行中的日誌（{current_week}）依規則保留收集，不予提前歸檔；其餘歷史週日誌皆已完成整理。"
            )
            return

        total_processed_videos = 0
        processed_weeks = []
        category_stats = {}      # category -> count
        new_category_stats = {}  # category -> count
        eval_stats = {"好": 0, "中": 0, "壞": 0}
        
        for w_str, file_path, content in eligible_files:
            processed_weeks.append(w_str)
            
            # 切分各個條目
            entry_pattern = re.compile(r'(?m)^##\s*\[(\d{4}-\d{2}-\d{2})\]\s*(.+)$')
            matches = list(entry_pattern.finditer(content))
            if not matches:
                continue
                
            for i, m in enumerate(matches):
                date_str = m.group(1)
                title = m.group(2).strip()
                start_idx = m.start()
                end_idx = matches[i + 1].start() if i + 1 < len(matches) else len(content)
                raw_entry = content[start_idx:end_idx].strip()
                
                # 移除尾部 ---
                if raw_entry.endswith("---"):
                    raw_entry = raw_entry[:-3].strip()
                    
                target_month = date_str[:7]
                month_dir = MONTHLY_DIR / target_month
                month_dir.mkdir(parents=True, exist_ok=True)
                
                # 讀取該月份已存在的類別檔案名稱（不含 .md）
                existing_cats = [cf.stem for cf in month_dir.glob("*.md")]
                
                # 提取摘要與標籤
                tags_match = re.search(r'-\s*\[tags::\s*([^\]]+)\]', raw_entry)
                tags = [t.strip() for t in tags_match.group(1).split()] if tags_match else []
                
                # 提取評語內容
                user_comment = ""
                m_comment = re.search(r'###\s*個人批判性評語與心得.*?\n(>.*?)(?=\n---\n|\Z)', raw_entry, re.DOTALL)
                if m_comment:
                    lines = [line.lstrip('> ').strip() for line in m_comment.group(1).splitlines() if line.strip() and not line.startswith('> [!')]
                    cmt = "\n".join(lines).strip()
                    if cmt and "觀看後在此記錄" not in cmt and "观看后在此记录" not in cmt:
                        user_comment = cmt
                        
                # 取得核心論點摘要
                m_summary = re.search(r'###\s*核心論點與摘要.*?\n(.*?)(?=###|\Z)', raw_entry, re.DOTALL)
                summary_text = m_summary.group(1).strip() if m_summary else ""
                
                # 調用分類與好中壞評價
                classify_res = classify_and_evaluate_entry(title, tags, summary_text, user_comment, existing_cats)
                cat_name = classify_res["category"]
                is_new = classify_res["is_new"]
                eval_val = classify_res["evaluation"]
                
                # 統計
                total_processed_videos += 1
                eval_stats[eval_val] = eval_stats.get(eval_val, 0) + 1
                if is_new and cat_name not in existing_cats:
                    new_category_stats[cat_name] = new_category_stats.get(cat_name, 0) + 1
                else:
                    category_stats[cat_name] = category_stats.get(cat_name, 0) + 1
                    
                # 在 entry 中注入 evaluation 與 category
                # 1. 在狀態行最後加上 | [evaluation:: {eval_val}]
                updated_entry = re.sub(
                    r'(-\s*\[\s*\]\s*\[platform::[^\]]+\]\s*\|\s*\[channel::[^\]]+\]\s*\|\s*\[status::[^\]]+\]\s*\|\s*\[importance::\s*\d+\])',
                    r'\1 | [evaluation:: ' + eval_val + ']',
                    raw_entry
                )
                if '[evaluation::' not in updated_entry:
                    updated_entry = re.sub(
                        r'^(##\s*\[\d{4}-\d{2}-\d{2}\][^\n]+\n-\s*\[.*?)$',
                        r'\1 | [evaluation:: ' + eval_val + ']',
                        updated_entry,
                        flags=re.M
                    )
                    
                # 2. 在 tags 行注入 | [category:: {cat_name}]
                if '[category::' not in updated_entry:
                    updated_entry = re.sub(
                        r'(-\s*\[tags::[^\]]+\])',
                        r'\1 | [category:: ' + cat_name + ']',
                        updated_entry
                    )

                # 寫入目標月度分類檔案
                cat_file = month_dir / f"{cat_name}.md"
                if not cat_file.exists():
                    year_val, month_val = target_month.split("-")
                    cat_header = f"""# {year_val}年{month_val}月 分類精讀：{cat_name}

- [month:: {target_month}]
- [category:: {cat_name}]
- [type:: 月度分類知識庫]

> **導航與說明**：
> - 本文檔為 {target_month} 月份【{cat_name}】主題之沉澱歸檔。
> - 來源：由各週度日誌透過「**zl，整理**」指令動態聚類生成，並依據隨筆評語自動標記 `[evaluation:: 好/中/壞]`。

---

"""
                    cat_file.write_text(cat_header, encoding="utf-8")
                    
                with open(cat_file, "a", encoding="utf-8") as f_cat:
                    f_cat.write(f"\n{updated_entry}\n\n---\n")

            # 標記週度檔案為已整理
            new_header = re.sub(
                r'-\s*\[status::\s*收集進行中\]\s*\|\s*\[organized::\s*false\]',
                f'- [status:: 已整理歸檔] | [organized:: true] | [organized_at:: {datetime.now().strftime("%Y-%m-%d %H:%M")}]',
                content
            )
            archive_notice = f"\n> [!success] 整理歸檔完成\n> 本週日誌已於 {datetime.now().strftime('%Y-%m-%d %H:%M')} 完成月度分類歸檔。\n> 歸檔目標目錄：[[Knowledge_Logs/Monthly/]]\n"
            if "\n---\n" in new_header:
                idx = new_header.find("\n---\n")
                updated_weekly = new_header[:idx] + archive_notice + new_header[idx:]
            else:
                updated_weekly = new_header + archive_notice
                
            file_path.write_text(updated_weekly, encoding="utf-8")

        # 組裝飛書回報統計訊息
        weeks_display = ", ".join(processed_weeks)
        msg_lines = [
            "📊 【週度日誌歸檔與月度分類整理完成】",
            f"📅 已處理歷史週次：{weeks_display}（共 {total_processed_videos} 部影片）",
            "━━━━━━━━━━━━━━━━━━━━━━"
        ]
        
        if category_stats:
            msg_lines.append("📂 現有類別歸檔：")
            for c_name, c_cnt in sorted(category_stats.items(), key=lambda x: -x[1]):
                msg_lines.append(f"  • {c_name}：+{c_cnt} 部")
                
        if new_category_stats:
            msg_lines.append("✨ 本次新成立類別：")
            for c_name, c_cnt in sorted(new_category_stats.items(), key=lambda x: -x[1]):
                msg_lines.append(f"  • {c_name}：+{c_cnt} 部")
                
        msg_lines.extend([
            "━━━━━━━━━━━━━━━━━━━━━━",
            "⭐ 評語評價分佈：",
            f"  • 🌟 好評：{eval_stats.get('好', 0)} 部",
            f"  • ⚖️ 中等：{eval_stats.get('中', 0)} 部（含無評語預設）",
            f"  • 👎 差評：{eval_stats.get('壞', 0)} 部",
            "━━━━━━━━━━━━━━━━━━━━━━",
            "📁 歸檔目錄：`Knowledge_Logs/Monthly/`",
            "OneDrive 正在實時同步至雲端與 Obsidian 保險庫。"
        ])
        
        final_msg = "\n".join(msg_lines)
        reply_feishu_message(chat_id, final_msg)
        logging.info(f"成功完成週度日誌歸檔並回傳飛書統計 (共 {total_processed_videos} 部影片)")
        
    except Exception as e:
        logging.error(f"執行週度整理歸檔管線失敗: {e}")
        reply_feishu_message(chat_id, f"⚠️ 整理歸檔過程出錯: {e}")

bot_executor = ThreadPoolExecutor(max_workers=3)
processed_msg_ids = set()
pending_playlist_sessions: dict[str, dict] = {}
active_processing_sessions: dict[str, dict] = {}

def check_bilibili_playlist(url: str) -> dict:
    """檢測 Bilibili 是否為多 P 列表，若已自帶 ?p= 則視為已指定單集，不觸發選單"""
    match = re.search(r'(BV[a-zA-Z0-9]+)', url, re.IGNORECASE)
    if not match:
        return None
    bvid = match.group(1)
    
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    if 'p' in params:
        return None
        
    try:
        api_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        r = requests.get(api_url, headers=headers, timeout=6)
        if r.status_code == 200:
            d = r.json().get('data', {})
            videos = d.get('videos', 1)
            if videos > 1:
                return {
                    "bvid": bvid,
                    "title": d.get("title", "未命名系列"),
                    "total": videos,
                    "pages": d.get("pages", []),
                    "base_url": f"https://www.bilibili.com/video/{bvid}",
                    "created_at": time.time()
                }
    except Exception as e:
        logging.warning(f"檢測 B 站分 P 失敗: {e}")
    return None

def format_playlist_menu(playlist: dict) -> str:
    """生成友好的分 P 選擇清單與提示"""
    title = playlist['title']
    total = playlist['total']
    pages = playlist['pages']
    
    total_sec = sum(p.get('duration', 0) for p in pages)
    total_hours = round(total_sec / 3600, 1)
    
    lines = [
        f"📚 檢測到此影片為【多 P 系列課程 / 影片清單】！",
        f"🎬 課程名稱：《{title}》",
        f"📺 總集數：共 {total} 講（總時長約 {total_hours} 小時）\n",
        "📋 部分章節預覽："
    ]
    
    preview_count = min(8, len(pages))
    for p in pages[:preview_count]:
        dur = p.get('duration', 0)
        dur_str = f"{dur//60:02d}:{dur%60:02d}"
        lines.append(f"  • P{p.get('page'):02d}: {p.get('part')} ({dur_str})")
        
    if total > preview_count:
        lines.append(f"  • ... 以及其餘 {total - preview_count} 講\n")
    else:
        lines.append("")
        
    lines.extend([
        "━━━━━━━━━━━━━━━━━━━━━━",
        "❓ 請問您想精讀第幾講？請直接私聊回覆：",
        "👉 輸入集數（如「2」或「p2」）：精讀該單講",
        "👉 輸入範圍（如「1-3」或「p1-p3」）：依序精讀前 3 講",
        f"👉 輸入「全部」或「all」：全數 {total} 講依序深度精讀",
        "👉 輸入「取消」放棄本次處理"
    ])
    return "\n".join(lines)

def parse_part_selection(text: str, total: int) -> list[int]:
    """解析使用者回覆的集數選擇"""
    raw = text.strip().lower()
    if raw in ["all", "全部", "全数", "全數", "全", "都要", "全部精讀", "全部精读"]:
        return list(range(1, total + 1))
        
    # 匹配範圍，如 1-3, p1-p3, 1~5, 1至3, 1到3
    range_match = re.match(r'^[pP]?(\d+)\s*[-~至到]\s*[pP]?(\d+)$', raw)
    if range_match:
        s, e = int(range_match.group(1)), int(range_match.group(2))
        s, e = min(s, e), max(s, e)
        s = max(1, s)
        e = min(total, e)
        return list(range(s, e + 1))
        
    # 匹配分隔列表，如 1, 3, 5 或 p1, p3
    tokens = re.split(r'[,，\s]+', raw)
    if len(tokens) > 1:
        parts = []
        for tok in tokens:
            m = re.search(r'\d+', tok)
            if m:
                v = int(m.group(0))
                if 1 <= v <= total and v not in parts:
                    parts.append(v)
        if parts:
            return sorted(parts)
            
    # 匹配單集，如 2, p2, 第2講, 第2集
    single_match = re.match(r'^(?:第)?\s*[pP]?(\d+)\s*(?:講|讲|集)?$', raw)
    if single_match:
        val = int(single_match.group(1))
        if 1 <= val <= total:
            return [val]
            
    return []

def process_video_items_worker(chat_id: str, items: list) -> None:
    """後台異步處理影片精讀流水線，不阻塞 WebSocket 長連接"""
    try:
        total_count = len(items)
        for idx, item in enumerate(items, 1):
            url = item[0]
            title_hint = item[1] if len(item) > 1 else None
            user_comment = item[2] if len(item) > 2 else ""
            
            # 記錄當前正在處理的影片會話狀態
            active_processing_sessions[chat_id] = {
                "url": url,
                "title_hint": title_hint,
                "user_comment": user_comment,
                "status": "processing",
                "started_at": time.time()
            }
            
            progress_prefix = f"({idx}/{total_count}) " if total_count > 1 else ""
            display_title = title_hint if title_hint else url
            logging.info(f"開始啟動智慧流水線: {progress_prefix}{url} (提示標題: {title_hint})")
            reply_feishu_message(chat_id, f"🔍 正在為您啟動多模態流水線分析：\n{progress_prefix}{display_title}\n請稍候...")
            
            info, summary, mode_name, transcript_content = process_video_pipeline(url, title_hint=title_hint)
            
            # 獲取可能在處理過程中由使用者即時發送的追加評語
            final_comment = active_processing_sessions.get(chat_id, {}).get("user_comment", user_comment)
            
            logging.info(f"--> 寫入 Obsidian (模式: {mode_name})...")
            file_name = append_to_vault(url, info, summary, mode_name, transcript_content, user_comment=final_comment)
            
            active_processing_sessions[chat_id] = {
                "url": url,
                "title": info.get("title"),
                "status": "completed",
                "completed_at": time.time()
            }
            
            orig_msg = f"\n🔗 原片溯源：{info['original_url']}" if info.get('original_url') else ""
            comment_msg = f"\n✍️ 心得評語：已記錄至個人心得區" if final_comment else ""
            reply_msg = f"✅ {progress_prefix}影片《{info.get('title')}》精讀成功！\n🎯 精讀模式：【{mode_name}】{orig_msg}{comment_msg}\n📂 已存入 Obsidian：`{file_name}`\nOneDrive 正在實時同步中。"
            reply_feishu_message(chat_id, reply_msg)
            logging.info(f"--> [完成] {progress_prefix}回傳飛書成功！")
    except Exception as e:
        logging.error(f"後台處理影片流水線出錯: {e}")

def handle_incoming_message(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    """處理飛書接收到的訊息（非阻塞快速 ACK + 多 P 互動選單 + 隨筆評語智能關聯）"""
    try:
        event = data.event
        msg = event.message
        chat_id = msg.chat_id
        msg_id = getattr(msg, "message_id", None)
        
        # 消息去重：防止飛書超時重推導致重複處理
        if msg_id:
            if msg_id in processed_msg_ids:
                logging.info(f"忽略飛書重複推送訊息: {msg_id}")
                return
            processed_msg_ids.add(msg_id)
            if len(processed_msg_ids) > 1000:
                processed_msg_ids.pop()
        
        if msg.message_type != "text":
            return
            
        content_json = json.loads(msg.content)
        raw_text = content_json.get("text", "").strip()
        
        logging.info(f"收到飛書訊息: {raw_text}")
        
        # 0. 整理指令觸發 (zl / 整理 / zl，整理)
        clean_cmd = re.sub(r'[\s,，、/!！]+', '', raw_text.strip().lower())
        if clean_cmd in ["zl", "整理", "zl整理", "整理zl", "organize", "歸檔", "归档"]:
            reply_feishu_message(chat_id, "⏳ 收到整理指令！正在為您掃描過往未整理的週度日誌，並啟動月度智慧分類與評價歸檔...")
            bot_executor.submit(organize_weekly_logs_worker, chat_id)
            return
        
        # 1. 檢查是否有該用戶正在等待的多 P 選擇對話 (10 分鐘內有效)
        session = pending_playlist_sessions.get(chat_id)
        if session and (time.time() - session.get("created_at", 0) < 600):
            # 若用戶回覆取消
            if raw_text.lower() in ["取消", "cancel", "不要了", "算了"]:
                del pending_playlist_sessions[chat_id]
                reply_feishu_message(chat_id, "👌 已為您取消該系列的分 P 精讀任務。")
                return
                
            # 若用戶發送了新的影片網址，則自動放棄上一輪對話，轉而處理新網址
            new_urls = extract_video_items(raw_text)
            if not new_urls:
                # 解析集數選擇
                parts = parse_part_selection(raw_text, session['total'])
                if parts:
                    del pending_playlist_sessions[chat_id]
                    bvid = session['bvid']
                    title = session['title']
                    pages_dict = {p['page']: p['part'] for p in session['pages']}
                    
                    items = []
                    for p in parts:
                        part_title = pages_dict.get(p, f"第{p}講")
                        items.append((
                            f"https://www.bilibili.com/video/{bvid}?p={p}",
                            f"{title} (P{p:02d} {part_title})",
                            ""
                        ))
                        
                    parts_desc = f"{parts[0]}~{parts[-1]}" if len(parts) > 2 and parts == list(range(parts[0], parts[-1]+1)) else ",".join(map(str, parts))
                    reply_feishu_message(chat_id, f"🎯 已確認！已為您將【第 {parts_desc} 講】（共 {len(items)} 講）加入精讀隊列，將在後台依序精讀並存入 Obsidian！")
                    bot_executor.submit(process_video_items_worker, chat_id, items)
                    return
                else:
                    # 提示未能識別
                    reply_feishu_message(chat_id, f"⚠️ 未能識別您的選擇。該課程共 {session['total']} 講，請直接輸入：\n👉 輸入集數（如「2」或「p2」）\n👉 輸入範圍（如「1-3」）\n👉 輸入「全部」全量精讀\n👉 或輸入「取消」")
                    return
        elif session:
            # 已過期，清理快取
            del pending_playlist_sessions[chat_id]

        # 2. 正常提取網址（包含兩個 link 之間的隨筆評語）
        items = extract_video_items(raw_text)
        if not items:
            # 檢查是否為針對影片的心得評語
            # 情況 A: 當前 chat_id 正在後台分析影片，將評語附加到處理隊列中
            if chat_id in active_processing_sessions and active_processing_sessions[chat_id].get("status") == "processing":
                existing_c = active_processing_sessions[chat_id].get("user_comment", "")
                new_c = f"{existing_c}\n{raw_text}".strip() if existing_c else raw_text
                active_processing_sessions[chat_id]["user_comment"] = new_c
                reply_feishu_message(chat_id, f"✍️ 已收到您的心得評語！將於當前影片精讀完成後一併寫入 Obsidian 筆記中：\n> {raw_text}")
                logging.info(f"用戶針對處理中影片追加評語: {raw_text}")
                return
                
            # 情況 B: 嘗試追加至 Obsidian 最近完成的影片條目中
            success, title = append_user_comment_to_latest_entry(raw_text)
            if success:
                reply_feishu_message(chat_id, f"✍️ 已為您將心得評語追加至最新影片《{title}》的 Obsidian 筆記中：\n> {raw_text}")
                logging.info(f"已將用戶心得評語寫入月度日誌最新條目: {title}")
                return
                
            reply_feishu_message(chat_id, "👋 您好！請發送 YouTube 或 Bilibili 影片連結給我，我將啟動智慧多模態流水線為您深度精讀！")
            return
            
        # 3. 檢測是否為未指定分 P 的 B 站多 P 系列課程
        if len(items) == 1:
            pl = check_bilibili_playlist(items[0][0])
            if pl:
                pending_playlist_sessions[chat_id] = pl
                menu = format_playlist_menu(pl)
                reply_feishu_message(chat_id, menu)
                logging.info(f"檢測到多 P 影片清單，已向會話 {chat_id} 發送選擇選單 (共 {pl['total']} 講)")
                return

        # 4. 常規單影片或自帶 ?p= 的影片直接投遞至後台線程池
        bot_executor.submit(process_video_items_worker, chat_id, items)
        logging.info(f"已將 {len(items)} 個影片任務分發至後台異步線程池處理")
            
    except Exception as e:
        logging.error(f"處理飛書訊息出錯: {e}")

def main():
    print("=" * 60)
    print("🚀 飛書 Obsidian 影片知識庫智慧流水線服務 (Smart Multimodal + Rule 5)")
    print(f"📁 倉庫路徑: {VAULT_ROOT}")
    print(f"🤖 飛書 App ID: {APP_ID}")
    print(f"🌐 代理狀態: {'已啟用 (' + PROXY_URL + ')' if PROXY_URL else '未啟用 (直連)'}")
    print("=" * 60)
    
    event_dispatcher = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(handle_incoming_message) \
        .build()
        
    cli = Client(
        app_id=APP_ID,
        app_secret=APP_SECRET,
        event_handler=event_dispatcher,
        log_level=lark.LogLevel.INFO
    )
    
    print("[*] 正在透過 WebSocket 長連接通道連線飛書伺服器...")
    print("[*] 連線成功後，請直接在飛書私聊發送影片連結！")
    cli.start()

if __name__ == "__main__":
    main()
