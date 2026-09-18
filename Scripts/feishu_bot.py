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
TRANSCRIPTS_DIR = VAULT_ROOT / "Transcripts"
INBOX_FILE = VAULT_ROOT / "Inbox_Pending.md"
ENV_FILE = VAULT_ROOT / ".env"
COOKIES_FILE = VAULT_ROOT / "cookies.txt"

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
    """提取文字中的影片項目，包含 (url, title_hint)，嚴防中文連寫"""
    items = []
    
    # 1. 匹配中文分享格式 【標題】 URL 或 【【標題】】 URL
    bili_matches = re.findall(r'【+([^】]+)】+\s*((?:https?://|www\.)[a-zA-Z0-9\.\-_/~\?#=%&:;+@!*]+)', text)
    for title, url in bili_matches:
        clean_url = resolve_short_url(url.strip().rstrip(').,;!?\'"'))
        clean_title = title.strip().strip('【】')
        items.append((clean_url, clean_title))

    # 2. 匹配 Markdown 連結 [標題](URL)
    md_matches = re.findall(r'\[(.*?)\]\(((?:https?://|www\.)[a-zA-Z0-9\.\-_/~\?#=%&:;+@!*]+)\)', text)
    for title, url in md_matches:
        clean_url = resolve_short_url(url.strip().rstrip(').,;!?\'"'))
        if not any(clean_url == it[0] for it in items):
            items.append((clean_url, title.strip()))
        
    # 3. 匹配常規 ASCII URL（嚴格排除中文字符，防止連寫時將中文判定為網址）
    raw_urls = re.findall(r'(?:https?://|www\.)[a-zA-Z0-9\.\-_/~\?#=%&:;+@!*]+', text)
    for u in raw_urls:
        clean_u = resolve_short_url(u.strip().rstrip(').,;!?\'"'))
        if not any(clean_u == it[0] for it in items):
            items.append((clean_u, None))
            
    return items

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

def append_to_vault(url: str, info: dict, ai_summary: str, mode_name: str, transcript_text: str = "") -> str:
    """原子寫入 Obsidian 月度日誌，並強制執行長視頻獨立逐字稿規範 (Rule 3)"""
    today = datetime.now()
    month_str = today.strftime("%Y-%m")
    today_str = today.strftime("%Y-%m-%d")
    
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    log_file = KNOWLEDGE_DIR / f"{month_str}_Knowledge_Log.md"
    
    if not log_file.exists():
        header = f"# {today.strftime('%Y年%m月')} 知識與時政影片閱讀長流\n\n---\n"
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
> - 📑 返回月度日誌精讀條目：[[Knowledge_Logs/{month_str}_Knowledge_Log#{today_str} {title}|{today.strftime('%Y年%m月')}知識日誌]]
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

    entry = f"""
## [{today_str}] {title}
- [ ] [platform:: {platform}] | [channel:: {channel}] | [status:: 待觀看] | [importance:: 3]
- [url:: {url}]
{original_url_line}- [tags:: #AI #科技 #知識]
- [精讀模式:: {mode_name}]
{transcript_backlink}
{ai_summary}

### 個人批判性評語與心得 (Obsidian 手動補充)
> [!quote] 筆記與心得記錄區
> （觀看後在此記錄個人心得、反思與批判性評語...）

---
"""
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(entry)
        
    return log_file.name

bot_executor = ThreadPoolExecutor(max_workers=3)
processed_msg_ids = set()
pending_playlist_sessions: dict[str, dict] = {}

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
        for idx, (url, title_hint) in enumerate(items, 1):
            progress_prefix = f"({idx}/{total_count}) " if total_count > 1 else ""
            display_title = title_hint if title_hint else url
            logging.info(f"開始啟動智慧流水線: {progress_prefix}{url} (提示標題: {title_hint})")
            reply_feishu_message(chat_id, f"🔍 正在為您啟動多模態流水線分析：\n{progress_prefix}{display_title}\n請稍候...")
            
            info, summary, mode_name, transcript_content = process_video_pipeline(url, title_hint=title_hint)
            
            logging.info(f"--> 寫入 Obsidian (模式: {mode_name})...")
            file_name = append_to_vault(url, info, summary, mode_name, transcript_content)
            
            orig_msg = f"\n🔗 原片溯源：{info['original_url']}" if info.get('original_url') else ""
            reply_msg = f"✅ {progress_prefix}影片《{info.get('title')}》精讀成功！\n🎯 精讀模式：【{mode_name}】{orig_msg}\n📂 已存入 Obsidian：`{file_name}`\nOneDrive 正在實時同步中。"
            reply_feishu_message(chat_id, reply_msg)
            logging.info(f"--> [完成] {progress_prefix}回傳飛書成功！")
    except Exception as e:
        logging.error(f"後台處理影片流水線出錯: {e}")

def handle_incoming_message(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    """處理飛書接收到的訊息（非阻塞快速 ACK + 多 P 互動選單）"""
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
                            f"{title} (P{p:02d} {part_title})"
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

        # 2. 正常提取網址
        items = extract_video_items(raw_text)
        if not items:
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
