# -*- coding: utf-8 -*-
"""
民航新闻聚合流水线
==================
每天自动执行：
  1. 从多家民航媒体 RSS 抓取最新文章
  2. 计算热度分（来源权重 + 新鲜度 + 标题信号词），排序
  3. 把英文标题翻译成中文
  4. 生成一个静态新闻网站（index.html）

只依赖 Python 标准库 + feedparser，GitHub Actions 免费运行。
"""

import base64
import json
import math
import os
import re
import time
import html
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

try:
    import feedparser
except ImportError:
    feedparser = None
    print("警告：feedparser 未安装，RSS 抓取将被跳过")


# ============================================================
# 配置区：想改来源、翻译目标、数量都在这里
# ============================================================

# ---- 板块一：民航（全球）----
SOURCES_AVIATION = [
    # (名称, RSS地址, 来源权重)
    # 权重反映权威性/影响力，直接参与热度计算
    ("Aviation Week",   "https://aviationweek.com/rss.xml",                5),
    ("FlightGlobal",    "https://www.flightglobal.com/rss",                5),
    ("Aviation24",      "https://www.aviation24.be/feed/",                 3),
    ("OMAAT",           "https://onemileatatime.com/feed/",                3),
    ("Simple Flying",   "https://simpleflying.com/feed/",                  3),
    ("AeroTime",        "https://www.aerotime.aero/feed",                  2),
]

# 这些词出现在标题里会提分（大新闻信号）
HOT_WORDS_AVIATION = [
    "crash", "accident", "grounded", "crash-land", "emergency",
    "first flight", "delivery", "order", "orders", "launch",
    "bankrupt", "merger", "acquisition", "record", "banned",
    "delay", "delays", "strike", "fleet", "widebody",
]

# ---- 板块二：美国国内（时政为主，科技互联网为辅）----
SOURCES_US = [
    # 时政（权重高，保证排前面）
    ("NPR Politics",  "https://feeds.npr.org/1014/rss.xml",                    5),
    ("Politico",      "https://rss.politico.com/politics-news.xml",            5),
    ("Guardian US",   "https://www.theguardian.com/us-news/rss",               5),
    ("NYT Politics",  "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml", 5),
    ("NBC News",      "https://feeds.nbcnews.com/nbcnews/public/news",         4),
    ("CBS News",      "https://www.cbsnews.com/latest/rss/main",               4),
    ("ABC Politics",  "https://abcnews.go.com/abcnews/politicsheadlines",      4),
    ("CNN Politics",  "https://rss.cnn.com/rss/cnn_allpolitics.rss",           4),
    ("NPR News",      "https://feeds.npr.org/1001/rss.xml",                    4),
    ("The Hill",      "https://thehill.com/rss/syndicator/19109-176787",       3),
    ("USA Today",     "https://rssfeeds.usatoday.com/usatoday-NewsTopStories", 3),
    ("PBS NewsHour",  "https://www.pbs.org/newshour/feeds/rss/headlines",      3),
    ("Axios",         "https://api.axios.com/feed/",                           3),
    # 科技互联网（权重稍低，作为辅助）
    ("The Verge",     "https://www.theverge.com/rss/index.xml",                3),
    ("TechCrunch",    "https://techcrunch.com/feed/",                          3),
    ("Ars Technica",  "https://arstechnica.com/feed/",                         3),
    ("Wired",         "https://www.wired.com/feed/rss",                        3),
]

HOT_WORDS_US = [
    "trump", "biden", "congress", "senate", "white house", "election",
    "supreme court", "shutdown", "impeach", "indict", "verdict",
    "governor", "campaign", "ballot", "lawsuit", "ruling", "subpoena",
    "deportation", "immigration", "tariff", "fbi", "pentagon",
    "state department", "openai", "semiconductor", "antitrust",
]

# 同一个板块内给某些来源留固定席位：
# 美国板块要保证「科技为辅」不会被一堆时政新闻挤掉
US_GROUPS = {"The Verge": "tech", "TechCrunch": "tech",
             "Ars Technica": "tech", "Wired": "tech"}

# 板块定义（顺序即界面顺序）
# quota: {分组: (最少条数, 最多条数)}
SECTIONS = [
    {"key": "aviation", "name": "民航资讯", "icon": "✈️",
     "sources": SOURCES_AVIATION, "hot": HOT_WORDS_AVIATION,
     "groups": None, "quota": None},
    {"key": "us",       "name": "美国要闻", "icon": "🇺🇸",
     "sources": SOURCES_US,       "hot": HOT_WORDS_US,
     "groups": US_GROUPS, "quota": {"tech": (5, 12)}},
]

MAX_ITEMS      = 40        # 最终保留多少条
TRANSLATE_TO   = "zh-CN"   # 目标语言
FETCH_PER_SRC  = 12        # 每个源最多取多少条
REQUEST_DELAY  = 0.4       # 抓取间隔（秒），对服务器友好

# ---- 正文（全文）相关 ----
BODY_LIMIT     = 7000      # 每篇英文正文最多保留多少字符（要够装下一篇完整报道）
BODY_TRANSLATE = 8000      # 正文最多翻译多少字符（跟上全文上限，中英段数才对得齐）
BODY_PARA_MAX  = 20        # 每篇最多保留多少段
BODY_MIN       = 900       # RSS 自带正文少于这个数，就去原网页抓全文
BODY_MIN_SEG   = 3         # 或者段数少于这个数，也去抓全文（很多源只给一句话摘要）
BODY_TIMEOUT   = 12        # 抓单个网页的超时（秒）
NOISE_WORDS    = ("cookie", "subscribe", "newsletter", "sign up", "sign-up",
                  "advertisement", "follow us", "read more", "copyright",
                  "all rights reserved", "photo:", "getty", "share this",
                  "click here", "terms of service", "privacy policy",
                  "add simple flying", "preferred source", "follow on google",
                  "add to home screen", "continue reading",
                  # 网站导航/菜单类的垃圾文本（抓全文时容易混进来）
                  "expand/collapse", "submenu", "skip to main content",
                  "skip to content", "toggle navigation", "main menu",
                  "open menu", "close menu", "search this site",
                  "sign in to", "back to top", "follow npr")


# ============================================================
# 1. 抓取
# ============================================================

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

def fetch_feed(url):
    """带超时和 UA 的 RSS 抓取，任何失败都返回空列表而不是崩溃。"""
    if not feedparser:
        return []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
        return feedparser.parse(data).entries[:FETCH_PER_SRC]
    except Exception as e:
        print(f"  抓取失败 [{url}]: {e}")
        return []


def parse_date(entry):
    """把 RSS 里五花八门的日期字段统一成时间戳，失败就给当前时间。"""
    for attr in ("published_parsed", "updated_parsed"):
        val = getattr(entry, attr, None) or entry.get(attr)
        if val:
            try:
                return time.mktime(val)
            except Exception:
                pass
    for attr in ("published", "updated", "dc_date"):
        val = entry.get(attr)
        if val:
            try:
                return parsedate_to_datetime(val).timestamp()
            except Exception:
                pass
    return time.time()


def clean_html(text):
    """把摘要里的 HTML 标签剥掉，只留纯文本。

    先反转义再剥标签、最后再反转义一次：
    有些源（如 Aviation Week）给的是被转义过的 HTML，
    只剥一次会把 <span> 变成明文标签留下来。
    """
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def html_to_paragraphs(raw, limit=BODY_LIMIT, min_len=45, max_paras=BODY_PARA_MAX):
    """把一段 HTML 变成干净的正文段落列表。

    会跳过脚本、广告、版权之类的噪声，只留像样的正文段落，
    并且总长度控制在 limit 以内，避免详情页塞进整本书。
    插图（img/figcaption）只会被跳过，不会打断后面的正文提取。
    """
    if not raw:
        return []
    # 去掉脚本、样式、表单等无关区块（figure 不整块删，里面的正文段落要留）
    raw = re.sub(r"(?is)<(script|style|noscript|svg|form|iframe|"
                 r"figcaption|nav|header|footer|aside)[^>]*>.*?</\1>", " ", raw)
    # 先把网页源码里的排版换行压成空格，免得一个自然段被切成好几片
    raw = re.sub(r"[ \t\r\f\v]*\n[ \t\r\f\v]*", " ", raw)
    # 段落/换行标记换成换行符，方便后面切段
    raw = re.sub(r"(?is)<(br|/p|/div|/h[1-6]|/li|p|div)[^>]*>", "\n", raw)
    raw = html.unescape(raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html.unescape(raw)

    # 图片标签本身清掉，避免 alt 文字混进正文
    raw = re.sub(r"(?is)<img[^>]*>", " ", raw)

    out, total = [], 0
    for piece in re.split(r"\n+", raw):
        p = re.sub(r"\s+", " ", piece).strip()
        if len(p) < min_len:
            continue
        low = p.lower()[:80]
        if any(w in low for w in NOISE_WORDS):
            continue
        if total + len(p) > limit and out:
            break
        if len(out) >= max_paras:
            break
        out.append(p)
        total += len(p)
    return out


def main_block(raw):
    """优先截取网页里的正文区域（article / main），避开导航菜单和页脚。

    找不到足够长的正文区块就原样返回整页。
    """
    for tag in ("article", "main"):
        m = re.search(r"(?is)<%s\b[^>]*>(.*?)</%s\s*>" % (tag, tag), raw)
        if m and len(m.group(1)) > 1500:
            return m.group(1)
    # 有些站点不用语义标签，退而求其次拿带 article 类名的容器
    m = re.search(r'(?is)<div[^>]+class="[^"]*\b(?:article|post|story)[^"]*"[^>]*>(.*?)</div>\s*</div>',
                  raw)
    if m and len(m.group(1)) > 1500:
        return m.group(1)
    return raw


def dedup_paras(paras):
    """去掉重复段落（RSS 摘要常常和正文第一段重复）。"""
    seen, out = set(), []
    for p in paras or []:
        key = re.sub(r"\W+", "", (p or "").lower())[:140]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def fetch_article_html(url):
    """下载新闻原网页的 HTML。失败返回空字符串。"""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": UA,
                          "Accept": "text/html,application/xhtml+xml",
                          "Accept-Language": "en-US,en;q=0.9"})
        with urllib.request.urlopen(req, timeout=BODY_TIMEOUT) as resp:
            raw = resp.read(600_000).decode("utf-8", "ignore")
        return raw
    except Exception:
        return ""


def enrich_bodies(items):
    """给正文不够长的文章补全文。

    有些源（Aviation24、Simple Flying）的 RSS 只给一句话摘要，
    这时直接去原网页把正文抓下来。抓不到就保持摘要，不影响其它流程。
    """
    need = []
    for it in items:
        paras = it.get("body_en") or []
        # 字符太少、或只有一两段（很多源只给一句话摘要），都去原网页抓全文
        if sum(len(p) for p in paras) < BODY_MIN or len(paras) < BODY_MIN_SEG:
            need.append(it)
    if not need:
        return
    print(f"  补充抓取正文 {len(need)} 篇 ...")

    def grab(it):
        raw = fetch_article_html(it["link"])
        if not raw:
            return 0
        # 先裁出正文区域，导航菜单就不会混进正文里
        paras = html_to_paragraphs(main_block(raw))
        if sum(len(p) for p in paras) > sum(len(p) for p in (it.get("body_en") or [])):
            it["body_en"] = paras
            return 1
        return 0

    # 并发抓，别让 20 多篇串行下载拖慢整个流程
    got = 0
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=6) as pool:
            for r in pool.map(grab, need):
                got += r
    except Exception as e:
        print(f"  (并发抓取失败，退回串行: {e})")
        got = sum(grab(it) for it in need)
    print(f"  成功补充 {got}/{len(need)} 篇")

    # 去重：抓回来的全文常常把摘要重复一遍
    for it in items:
        if it.get("body_en"):
            it["body_en"] = dedup_paras(it["body_en"])


def _img_ok(url):
    """过滤掉占位图、追踪像素之类的假图片。"""
    u = (url or "").strip()
    if not u.startswith("http"):
        return ""
    low = u.lower()
    if any(x in low for x in ("pixel", "spacer", "1x1", "tracking",
                              "beacon", ".svg", "gravatar")):
        return ""
    return u


def pick_image(entry):
    """从 RSS 条目里挑一张配图；没有就返回空字符串。

    依次尝试 media:content → media:thumbnail → enclosure → 正文里的第一张图。
    """
    # ① 媒体扩展字段（大多数主流媒体都带）
    for key in ("media_content", "media_thumbnail"):
        for m in (entry.get(key) or []):
            if not isinstance(m, dict):
                continue
            u = _img_ok(m.get("url") or "")
            if u:
                return u

    # ② 附件
    for en in (entry.get("enclosures") or []):
        if "image" in ((en.get("type") or "").lower()):
            u = _img_ok(en.get("href") or en.get("url") or "")
            if u:
                return u

    # ③ 正文或摘要里的第一张图
    raw = ""
    if entry.get("content"):
        raw = entry["content"][0].get("value", "") or ""
    raw += " " + (entry.get("summary") or "")
    m = re.search(r"(?is)<img[^>]+src=[\"']([^\"']+)[\"']", raw)
    if m:
        return _img_ok(m.group(1))
    return ""


def apply_quota(items, quota):
    """按分组控制入选条数，保证「为辅」的那类不会被挤光。

    quota: {分组: (最少条数, 最多条数)}。先给每组补足下限，
    剩下的位置再按热度分从高到低补，受每组上限约束。
    """
    if not quota:
        return items[:MAX_ITEMS]

    groups = {}
    for it in items:                      # items 已按分数降序
        groups.setdefault(it.get("grp") or "main", []).append(it)

    chosen, cid = [], set()
    for g, (mn, mx) in quota.items():
        for it in groups.get(g, [])[:mn]:
            chosen.append(it)
            cid.add(id(it))

    for it in items:
        if len(chosen) >= MAX_ITEMS:
            break
        if id(it) in cid:
            continue
        g = it.get("grp") or "main"
        mx = quota.get(g, (0, MAX_ITEMS))[1]
        already = sum(1 for x in chosen if (x.get("grp") or "main") == g)
        if already >= mx:
            continue
        chosen.append(it)
        cid.add(id(it))

    for it in items:                      # 名额没填满时不浪费
        if len(chosen) >= MAX_ITEMS:
            break
        if id(it) not in cid:
            chosen.append(it)
            cid.add(id(it))

    chosen.sort(key=lambda x: x["score"], reverse=True)
    return chosen


def collect(sources, hot_words, groups=None, quota=None):
    """从指定源抓取文章，去重，算热度分。"""
    seen = set()
    items = []

    for name, url, weight in sources:
        print(f"抓取 {name} ...")
        entries = fetch_feed(url)
        print(f"  得到 {len(entries)} 条")

        for e in entries:
            link = (e.get("link") or "").strip()
            title = clean_html(e.get("title") or "")
            if not link or not title:
                continue

            # 用链接前 80 个字符去重（去掉跟踪参数）
            key = re.sub(r"[?#].*$", "", link)[:80]
            if key in seen:
                continue
            seen.add(key)

            summary = clean_html(e.get("summary") or e.get("description") or "")[:260]
            pub = parse_date(e)

            # ---- 正文：RSS 里有全文的直接用，没有的后面再补抓 ----
            content_html = ""
            if e.get("content"):
                content_html = e["content"][0].get("value", "") or ""
            elif e.get("content_detail"):
                content_html = e["content_detail"].get("value", "") or ""
            body_en = html_to_paragraphs(content_html)
            if not body_en:
                # 有些源把全文直接塞在 description 里
                body_en = html_to_paragraphs(e.get("summary") or "")

            # ---- 热度分 ----
            # 来源权重（0-10 分）
            source_score = weight * 2
            # 新鲜度：24 小时内满分，随时间指数衰减，72 小时后趋近 0
            age_hours = max(0, (time.time() - pub) / 3600)
            freshness = 6 * math.exp(-age_hours / 30)
            # 标题信号词
            t_lower = title.lower()
            signal = sum(2 for w in hot_words if w in t_lower)

            score = source_score + freshness + signal

            items.append({
                "title_en": title,
                "title_zh": "",
                "summary":  summary,
                "summary_zh": "",
                "body_en":  body_en,     # 英文正文段落
                "body_zh":  [],          # 中文正文段落（翻译后填）
                "img":      pick_image(e),
                "link":     link,
                "source":   name,
                "pub_ts":   pub,
                "score":    round(score, 2),
                "grp":      (groups or {}).get(name, "main"),
            })

        time.sleep(REQUEST_DELAY)

    items.sort(key=lambda x: x["score"], reverse=True)
    return apply_quota(items, quota)


# ============================================================
# 2. 翻译
# ============================================================

CACHE_PATH = os.path.join("docs", "translate_cache.json")
CACHE_MAX  = 5000          # 缓存最多保留多少条新闻的译文
AI_MODELS  = ["openai/gpt-4o-mini", "openai/gpt-4.1-mini"]
AI_DISABLED = False      # AI 探测失败后置 True，本次直接全走 Google，省掉上百次无效请求


def probe_ai():
    """先用两条短句探路：AI 不通就把 AI 全部跳过（日志已证实接口在 Actions 里不可用）。"""
    global AI_DISABLED
    for model in AI_MODELS:
        got = _ai_batch(["Boeing 737", "Airbus A350"], model)
        if got and any(has_cn(x) for x in got):
            log(f"  AI 探测通过（{model}）")
            return
    log("  AI 探测失败，本次正文/标题全部走 Google 免费翻译")
    AI_DISABLED = True


AI_BATCH   = 10            # 每批翻译几条：太大易超时，太小请求多
AI_ENDPOINT = "https://models.github.ai/inference/v1/chat/completions"

SYS_PROMPT = ("你是专业民航新闻翻译。把英文新闻标题/摘要逐条翻译成简体中文。\n"
              "要求：准确、简洁，符合中文新闻表达习惯；"
              "航空公司名、机型（如 Boeing 737 MAX）、机场三字码等专有名词保留英文原文；"
              "不要添加任何解释、前言或后记。\n"
              "必须严格按输入的行数输出，每行格式为「序号| 译文」，序号与输入一致。")


def has_cn(text):
    """判断字符串里有没有中文，用来确认翻译是否真的成功。"""
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


# ---------- 运行诊断：把失败原因写进 docs/，方便上线后直接查看 ----------

RUN_LOG = []


def log(msg):
    """同时打印到控制台和日志（最后一起写进 docs/translate_log.txt）。"""
    text = str(msg)
    print(text, flush=True)
    RUN_LOG.append(text)


# ---------- 译文缓存：翻过的就不再重复调用接口 ----------

def load_cache():
    """优先读 docs/ 下的缓存；没有再读仓库根目录的（方便手动上传补充）。"""
    for path in (CACHE_PATH, "translate_cache.json"):
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data:
                return data
        except Exception:
            continue
    return {}


def save_cache(cache):
    try:
        if len(cache) > CACHE_MAX:
            keys = sorted(cache, key=lambda k: cache[k].get("ts", 0))
            for k in keys[:len(cache) - CACHE_MAX]:
                cache.pop(k, None)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception as e:
        print(f"  (缓存写入失败: {e})")


# ---------- 翻译接口 ----------

def _translate_google(text, target="zh-CN", sl="auto", timeout=8):
    """免费 Google 翻译接口（无需密钥）。失败返回 None。"""
    if not text:
        return ""
    q = urllib.parse.quote(text)
    url = (f"https://translate.googleapis.com/translate_a/single"
           f"?client=gtx&sl={sl}&tl={target}&dt=t&q={q}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return "".join(seg[0] for seg in data[0] if seg[0])
    except Exception:
        return None


def _ai_batch(texts, model):
    """调用 GitHub 免费 AI 接口翻译一批（约 10 条）。失败返回 None。"""
    token = os.environ.get("GITHUB_TOKEN")
    if not token or not texts:
        return None

    numbered = "\n".join(f"{i+1}| {t}" for i, t in enumerate(texts))
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYS_PROMPT},
            {"role": "user", "content": numbered},
        ],
        "temperature": 0.3,
    }
    try:
        req = urllib.request.Request(
            AI_ENDPOINT, method="POST",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json",
                     "User-Agent": "aviation-news-bot"},
            data=json.dumps(payload).encode("utf-8"))
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
    except Exception as e:
        log(f"  ({model} 调用失败: {e})")
        return None

    # 按「序号| 译文」解析，允许模型偶尔多吐几行废话
    got = {}
    for line in content.splitlines():
        line = line.strip()
        m = re.match(r"^(\d+)\s*[|｜.、:：]\s*(.+)$", line)
        if m:
            i = int(m.group(1)) - 1
            val = m.group(2).strip()
            if 0 <= i < len(texts) and val:
                got[i] = val

    if len(got) == len(texts):
        return [got[i] for i in range(len(texts))]
    # 拿到七成以上就算成功，缺的用原文补上，避免整批白翻
    if len(got) >= max(1, int(len(texts) * 0.7)):
        return [got.get(i, texts[i]) for i in range(len(texts))]
    log(f"  ({model} 返回条数不足: {len(got)}/{len(texts)})")
    return None


def ai_translate(texts):
    """分批 + 多模型 + 重试。返回与 texts 等长的列表，失败的位置是 None。"""
    if not texts or AI_DISABLED:
        return [None] * len(texts) if texts else []
    result = [None] * len(texts)

    for model in AI_MODELS:
        todo = [i for i, v in enumerate(result) if v is None]
        if not todo:
            break
        done_batches = 0
        for s in range(0, len(todo), AI_BATCH):
            idxs = todo[s:s + AI_BATCH]
            got = None
            for attempt in range(2):          # 每批最多试两次
                got = _ai_batch([texts[i] for i in idxs], model)
                if got:
                    break
                time.sleep(2 + attempt * 2)
            if got:
                for i, v in zip(idxs, got):
                    result[i] = v
                done_batches += 1
        if done_batches == 0:
            print(f"  AI 模型 {model} 本次完全不可用")
        if all(v is not None for v in result):
            break

    return result


BODY_SYS_PROMPT = ("你是专业民航新闻翻译。把英文新闻正文逐段翻译成简体中文。\n"
                   "如果输入里带有 [[段号]] 标记，请原样保留这些标记，一段对应一段翻译。\n"
                   "无论如何：段落数量必须与输入一致，一段对一段，不要合并、拆分或省略，"
                   "段落之间用一个空行分隔。\n"
                   "语言通顺自然，航空公司名、机型、机场三字码保留英文；不要加任何解释或标题。")


def _ai_batch_body(blocks, model):
    """翻译多篇正文，保持段落一一对应。

    blocks: [[段落1, 段落2, ...], ...]，每篇一个列表
    返回与 blocks 同结构的译文（失败为 None，缺失段落为空串）
    """
    token = os.environ.get("GITHUB_TOKEN")
    if not token or not blocks:
        return None

    numbered = []
    for i, paras in enumerate(blocks):
        inner = "\n".join(f"[[{j+1}]]{p}" for j, p in enumerate(paras))
        numbered.append(f"<<<{i+1}>>>\n{inner}")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": BODY_SYS_PROMPT},
            {"role": "user", "content": "\n".join(numbered)},
        ],
        "temperature": 0.3,
        "max_tokens": 4096,
    }
    def _post(body):
        req = urllib.request.Request(
            AI_ENDPOINT, method="POST",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json",
                     "User-Agent": "aviation-news-bot"},
            data=json.dumps(body).encode("utf-8"))
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        try:
            data = _post(payload)
        except Exception as e1:
            # 有些模型不接受 max_tokens，去掉再试一次
            payload.pop("max_tokens", None)
            try:
                data = _post(payload)
            except Exception as e2:
                detail = ""
                try:
                    detail = e2.read().decode("utf-8", "ignore")[:160]
                except Exception:
                    pass
                log(f"  ({model} 正文调用失败: {e2} {detail})")
                return None
        content = data["choices"][0]["message"]["content"]
    except Exception as e:
        log(f"  ({model} 正文解析失败: {e})")
        return None

    got = {}
    parts = re.split(r"<<<\s*(\d+)\s*>>>", content)
    for i in range(1, len(parts) - 1, 2):
        idx = int(parts[i]) - 1
        if not (0 <= idx < len(blocks)):
            continue
        segs = re.split(r"\[\[\s*(\d+)\s*\]\]", parts[i + 1])
        paras = {}
        for j in range(1, len(segs) - 1, 2):
            pidx = int(segs[j]) - 1
            val = segs[j + 1].strip()
            if 0 <= pidx < len(blocks[idx]) and val:
                paras[pidx] = val
        need = len(blocks[idx])
        if not paras:
            # 模型没按 [[段号]] 输出时，退一步按空行切块，按顺序对上段落
            chunks = [c.strip() for c in re.split(r"\n\s*\n", parts[i + 1]) if c.strip()]
            if chunks:
                paras = {k: (chunks[k] if k < len(chunks) else "")
                         for k in range(min(need, len(chunks)))}
        if len(paras) >= max(1, int(need * 0.5)):
            got[idx] = [paras.get(k, "") for k in range(need)]

    if len(got) == len(blocks):
        return [got[i] for i in range(len(blocks))]

    # 模型完全没用任何标记时（单篇最常见）：整篇按空行切块，按顺序对上段落
    if not got and len(blocks) == 1:
        need = len(blocks[0])
        chunks = [c.strip() for c in re.split(r"\n\s*\n", content) if c.strip()]
        if len(chunks) >= max(1, int(need * 0.5)):
            return [[chunks[k] if k < len(chunks) else "" for k in range(need)]]

    if len(got) >= max(1, int(len(blocks) * 0.5)):
        log(f"  ({model} 正文部分成功: {len(got)}/{len(blocks)})")
        return [got.get(i) for i in range(len(blocks))]
    log(f"  ({model} 正文返回不足: {len(got)}/{len(blocks)})，开头: " + repr(content[:120]))
    return None


def ai_translate_bodies(blocks, batch=1):
    """分批 + 多模型翻译正文。返回与 blocks 等长的列表，失败位置是 None。"""
    if not blocks or AI_DISABLED:
        return [None] * len(blocks) if blocks else []
    result = [None] * len(blocks)

    for model in AI_MODELS:
        todo = [i for i, v in enumerate(result) if v is None]
        if not todo:
            break
        done = 0
        for s in range(0, len(todo), batch):
            idxs = todo[s:s + batch]
            got = None
            for attempt in range(1):          # 只试一次，失败的交给逐段兜底，省时间
                got = _ai_batch_body([blocks[i] for i in idxs], model)
                if got:
                    break
                time.sleep(1)
            if got:
                for i, v in zip(idxs, got):
                    result[i] = v
                done += 1
            time.sleep(0.5)        # 放慢一点，别把免费额度打爆
        if done == 0:
            print(f"  AI 模型 {model} 正文不可用")
        if all(v is not None for v in result):
            break

    return result


PARA_BATCH = 6     # 逐段翻译每批几条：正文比标题长，批次要小一点

PARA_SYS_PROMPT = ("你是专业民航新闻翻译。把英文新闻正文逐段翻译成简体中文。\n"
                   "严格按输入的行数输出，每行格式为「序号| 译文」，序号从 1 开始，与输入一一对应。\n"
                   "每一段只输出一行，不要在译文中间换行；"
                   "航空公司名称、机型、机场三字码保留英文；不要加任何解释或标题。")


def _ai_batch_para(texts, model):
    """逐段翻译一批正文段落，沿用标题那种「序号| 译文」格式（这条路径已验证可用）。

    返回与 texts 等长的译文列表；失败返回 None。
    """
    token = os.environ.get("GITHUB_TOKEN")
    if not token or not texts:
        return None

    numbered = "\n".join(f"{i+1}| {t}" for i, t in enumerate(texts))
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": PARA_SYS_PROMPT},
            {"role": "user", "content": numbered},
        ],
        "temperature": 0.3,
    }
    try:
        req = urllib.request.Request(
            AI_ENDPOINT, method="POST",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json",
                     "User-Agent": "aviation-news-bot"},
            data=json.dumps(payload).encode("utf-8"))
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
    except Exception as e:
        log(f"  ({model} 逐段调用失败: {e})")
        return None

    # 解析：序号开头算新的一段；没有序号的行视为上一段的续行
    got, last = {}, None
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\d+)\s*[|｜.、:：]\s*(.*)$", line)
        if m:
            i = int(m.group(1)) - 1
            if 0 <= i < len(texts):
                last = i
                got[i] = m.group(2).strip()
        elif last is not None:
            got[last] = (got.get(last, "") + " " + line).strip()

    got = {k: v for k, v in got.items() if v}
    if len(got) >= max(1, int(len(texts) * 0.7)):
        return [got.get(i, "") for i in range(len(texts))]
    log(f"  ({model} 逐段返回不足: {len(got)}/{len(texts)})")
    return None


def ai_translate_paragraphs(packs):
    """把多篇文章的段落拉平后逐段翻译，再按原结构拼回去。

    packs: [[段落, 段落, ...], ...]（一篇文章一个列表）
    返回同结构列表，整篇失败的位置是 None。
    """
    if AI_DISABLED:
        return [None] * len(packs) if packs else []
    flat, spans = [], []
    for paras in packs:
        spans.append((len(flat), len(flat) + len(paras)))
        flat.extend(paras)
    if not flat:
        return [None] * len(packs)

    result = [None] * len(flat)
    for model in AI_MODELS:
        todo = [i for i, v in enumerate(result) if v is None]
        if not todo:
            break
        done = 0
        for s in range(0, len(todo), PARA_BATCH):
            idxs = todo[s:s + PARA_BATCH]
            got = None
            for attempt in range(2):
                got = _ai_batch_para([flat[i] for i in idxs], model)
                if got:
                    break
                time.sleep(3 + attempt * 5)
            if got:
                for i, v in zip(idxs, got):
                    result[i] = v
                done += 1
            time.sleep(1)
        if done == 0:
            print(f"  AI 模型 {model} 逐段不可用")
        if all(v is not None for v in result):
            break

    out = []
    for a, b in spans:
        chunk = result[a:b]
        out.append(None if all(v is None for v in chunk)
                   else [(v or "") for v in chunk])
    return out


def _pack_spans(packs):
    """把多篇文章的段落拉平，同时记下每篇在拉平后列表里的起止位置。"""
    flat, spans = [], []
    for paras in packs:
        spans.append((len(flat), len(flat) + len(paras)))
        flat.extend(paras)
    return flat, spans


def _rebuild(result, spans, n_packs):
    """按原结构把译文拼回「一篇文章一个列表」。"""
    if not result:
        return [None] * n_packs
    out = []
    for a, b in spans:
        chunk = result[a:b]
        out.append(None if all(v is None for v in chunk)
                   else [(v or "") for v in chunk])
    return out


def google_translate_paragraphs(packs):
    """用免费 Google 接口逐段翻译正文（不占 GitHub AI 额度）。

    packs: [[段落, 段落...], ...]；返回同结构列表，整篇失败的位置是 None。
    """
    flat, spans = _pack_spans(packs)
    if not flat:
        return [None] * len(packs)

    result = [None] * len(flat)
    ok = 0
    miss = 0
    for i, t in enumerate(flat):
        out = _translate_google(t, sl="en", timeout=12)
        if out and has_cn(out):
            result[i] = out
            ok += 1
            miss = 0
        else:
            miss += 1
            if miss >= 4 and ok == 0:
                log(f"  Google 逐段翻译连续失败 {miss} 次，放弃")
                break
        time.sleep(0.2 + 0.2 * (i % 3))     # 稍微抖一抖，别打得太密
    log(f"  Google 逐段翻译 {ok}/{len(flat)} 段")
    return _rebuild(result, spans, len(packs))


def mymemory_translate_paragraphs(packs, budget=4000):
    """最后兜底：MyMemory 逐段翻。有每日字数限额，用完就停。"""
    flat, spans = _pack_spans(packs)
    if not flat:
        return [None] * len(packs)

    result = [None] * len(flat)
    used = 0
    ok = 0
    for i, t in enumerate(flat):
        if used >= budget:
            break
        out = _translate_mymemory(t)
        used += len(t or "")
        if out and has_cn(out):
            result[i] = out
            ok += 1
        time.sleep(0.4)
    log(f"  MyMemory 逐段翻译 {ok}/{len(flat)} 段（用掉 {used} 字额度）")
    return _rebuild(result, spans, len(packs))


def _translate_mymemory(text, target="zh-CN"):
    """备用翻译：MyMemory（免费，无需密钥，每天限额约 5000 字）。失败返回 None。"""
    if not text:
        return ""
    url = ("https://api.mymemory.translated.net/get?q="
           + urllib.parse.quote(text) + f"&langpair=en|{target}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        out = data.get("responseData", {}).get("translatedText", "")
        return out if out and out != text else None
    except Exception:
        return None


def _fallback_translate(items):
    """AI 不通时的兜底：Google → MyMemory，只对还缺中文的条目逐条翻。"""
    use_google = _translate_google("test") is not None
    log("  兜底翻译：" + ("Google" if use_google else "MyMemory"))

    def tr(text):
        if not text:
            return None
        out = _translate_google(text) if use_google else None
        if out and has_cn(out):
            return out
        for _ in range(2):
            out = _translate_mymemory(text)
            if out and has_cn(out):
                return out
            time.sleep(1.2)
        return None

    n = 0
    for it in items:
        if not has_cn(it.get("title_zh")):
            t = tr(it["title_en"])
            if t:
                it["title_zh"] = t
                n += 1
        if (it.get("summary") or "").strip() and not has_cn(it.get("summary_zh")):
            s = tr(it["summary"][:150])
            if s:
                it["summary_zh"] = s
        time.sleep(0.2)
    print(f"  兜底翻译完成 {n} 条标题")


def translate_items(items):
    """翻译标题和摘要。

    策略（从稳到快）：
      1. 先查历史译文缓存，命中的直接用 —— 不消耗任何接口额度
      2. 新条目用 GitHub 免费 AI 分批翻译，每批成功立刻入缓存
      3. 仍失败的走 Google / MyMemory 兜底
      4. 最后无论如何都填成英文，界面上绝不会出现空白标题
    """
    cache = load_cache()
    if cache:
        print(f"  译文缓存 {len(cache)} 条，可复用")

    title_todo, sum_todo, body_todo = [], [], []
    for it in items:
        c = cache.get(it["link"]) or {}
        it["title_zh"] = c.get("t") if has_cn(c.get("t")) else ""
        it["summary_zh"] = c.get("s") if has_cn(c.get("s")) else ""
        cache_body = c.get("b") or ""
        # 注意：不能过滤空段，否则上次没翻出来的段会让后面所有段落错位
        cached = cache_body.split("\n") if has_cn(cache_body) else []
        cached_ok = sum(1 for p in cached if p.strip())
        # 缓存段落明显比现在的正文少，说明上次没翻全，这次重翻
        if cached and cached_ok >= max(1, int(len(it.get("body_en") or []) * 0.6)):
            it["body_zh"] = cached
        else:
            it["body_zh"] = []
        if not it["title_zh"]:
            title_todo.append(it)
        if (it.get("summary") or "").strip() and not it["summary_zh"]:
            sum_todo.append(it)
        if it.get("body_en") and not it["body_zh"]:
            body_todo.append(it)

    hit = len(items) - len(title_todo)
    print(f"  缓存命中 {hit} 条，需新翻译 {len(title_todo)} 条")

    # 开工前先探一次 AI 的路：不通就全程走 Google，别白耗几十次请求
    if title_todo or sum_todo or body_todo:
        probe_ai()

    # ① AI 翻译标题
    if title_todo:
        zhs = ai_translate([t["title_en"] for t in title_todo])
        ok = 0
        for it, zh in zip(title_todo, zhs):
            if zh and has_cn(zh):
                it["title_zh"] = zh
                rec = cache.setdefault(it["link"], {})
                rec["t"] = zh
                rec["ts"] = time.time()
                ok += 1
        print(f"  AI 翻译标题 {ok}/{len(title_todo)} 条")
        save_cache(cache)

    # ② AI 翻译摘要（长度截断，控制额度）
    if sum_todo:
        zhs = ai_translate([(t["summary"] or "")[:150] for t in sum_todo])
        ok = 0
        for it, zh in zip(sum_todo, zhs):
            if zh and has_cn(zh):
                it["summary_zh"] = zh
                rec = cache.setdefault(it["link"], {})
                rec["s"] = zh
                rec["ts"] = time.time()
                ok += 1
        print(f"  AI 翻译摘要 {ok}/{len(sum_todo)} 条")
        save_cache(cache)

    # ②⑤ AI 翻译正文：逐段翻译，让中文段落和英文段落一一对上
    if body_todo:
        packs = []
        for it in body_todo:
            paras, total = [], 0
            for p in it["body_en"]:
                if total and total + len(p) > BODY_TRANSLATE:
                    break
                paras.append(p)
                total += len(p)
            packs.append(paras)

        def absorb(todo_list, packs_list, zhs):
            """把译文写回条目并更新缓存，返回 (成功篇数, 仍缺的条目, 仍缺的段落包)。"""
            ok, rest_it, rest_pk = 0, [], []
            for it, pack, zh in zip(todo_list, packs_list, zhs):
                body = []
                if zh:
                    body = [(x or "").replace("\n", " ").strip() for x in zh]
                    body = (body + [""] * len(pack))[:len(pack)]  # 长度对齐，缺的留空
                if any(has_cn(x) for x in body):
                    it["body_zh"] = body
                    rec = cache.setdefault(it["link"], {})
                    rec["b"] = "\n".join(body)
                    rec["ts"] = time.time()
                    ok += 1
                else:
                    rest_it.append(it)
                    rest_pk.append(pack)
            return ok, rest_it, rest_pk

        total_paras = sum(len(p) for p in packs)
        log(f"  待翻正文 {len(packs)} 篇 / {total_paras} 段")

        # ① AI 整篇翻（一次调用翻完一篇，译文质量最好）
        zhs = ai_translate_bodies(packs)
        ok, todo_it, todo_pk = absorb(body_todo, packs, zhs)
        log(f"  AI 整篇翻译正文 {ok}/{len(body_todo)} 篇")
        save_cache(cache)

        # ② AI 逐段翻（和标题翻译同一套机制）
        if todo_it:
            zhs2 = ai_translate_paragraphs(todo_pk)
            ok2, todo_it, todo_pk = absorb(todo_it, todo_pk, zhs2)
            log(f"  AI 逐段翻译正文 {ok2} 篇")
            save_cache(cache)

        # ③ Google 免费接口逐段翻（不占 GitHub AI 额度，额度不够时的主力）
        if todo_it:
            zhs3 = google_translate_paragraphs(todo_pk)
            ok3, todo_it, todo_pk = absorb(todo_it, todo_pk, zhs3)
            log(f"  Google 逐段翻译正文 {ok3} 篇")
            save_cache(cache)

        # ④ MyMemory 最后兜底（有每日字数限额）
        if todo_it:
            zhs4 = mymemory_translate_paragraphs(todo_pk)
            ok4, todo_it, todo_pk = absorb(todo_it, todo_pk, zhs4)
            log(f"  MyMemory 逐段翻译正文 {ok4} 篇")
            save_cache(cache)

        if todo_it:
            log(f"  仍有 {len(todo_it)} 篇正文没翻出来")

    # ③ 兜底：还缺中文的再走免费接口
    missing = [it for it in items
               if not has_cn(it.get("title_zh"))
               or ((it.get("summary") or "").strip() and not has_cn(it.get("summary_zh")))]
    if missing:
        print(f"  {len(missing)} 条未翻译成功，尝试兜底接口")
        _fallback_translate(missing)
        for it in missing:
            if has_cn(it.get("title_zh")):
                rec = cache.setdefault(it["link"], {})
                rec["t"] = it["title_zh"]
                rec["ts"] = time.time()
            if has_cn(it.get("summary_zh")):
                rec = cache.setdefault(it["link"], {})
                rec["s"] = it["summary_zh"]
                rec["ts"] = time.time()
        save_cache(cache)

    # ④ 最终保底：任何情况下都不留空白
    for it in items:
        if not it.get("title_zh"):
            it["title_zh"] = it["title_en"]
        if not it.get("summary_zh"):
            it["summary_zh"] = it["summary"]

    zh_n = sum(1 for it in items if has_cn(it["title_zh"]))
    print(f"  最终中文标题 {zh_n}/{len(items)} 条")
    return items


# ============================================================
# 3. 生成网页
# ============================================================

def fmt_time(ts):
    dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=8)))
    now = datetime.now(tz=timezone(timedelta(hours=8)))
    diff = now - dt
    if diff.days == 0:
        h = diff.seconds // 3600
        return f"{h} 小时前" if h else "刚刚"
    if diff.days == 1:
        return "昨天"
    return dt.strftime("%m月%d日")


def hot_label(score):
    """根据热度分给一个中文标签和等级，界面用。"""
    if score >= 14: return "爆", 3
    if score >= 10: return "热", 2
    if score >= 6:  return "荐", 1
    return "", 0


DETAIL_CSS = """
/* ========== 二级页面（详情） ========== */
.detail{
  position:fixed;inset:0;z-index:40;background:var(--bg);
  display:flex;flex-direction:column;
  transform:translateX(100%);visibility:hidden;
  transition:transform .3s cubic-bezier(.32,.72,0,1), visibility .3s;
  box-shadow:-1px 0 12px rgba(0,0,0,.06);
}
.detail.open{transform:none;visibility:visible}

/* 详情顶栏 */
.dbar{
  flex:none;display:flex;align-items:center;gap:10px;
  padding:calc(var(--safe-t) + 10px) 14px 10px;
  background:color-mix(in srgb, var(--bg) 90%, transparent);
  backdrop-filter:saturate(180%) blur(20px);-webkit-backdrop-filter:saturate(180%) blur(20px);
  border-bottom:.5px solid var(--sep);
}
.dback{
  flex:none;display:inline-flex;align-items:center;gap:2px;
  background:none;border:0;padding:4px 2px;margin:0;
  color:var(--accent);font-size:16px;font-family:inherit;font-weight:500;cursor:pointer;
}
.dback .chev{font-size:22px;line-height:1;margin-top:-2px}
.dback:active{opacity:.5}
.dbar-info{flex:1;min-width:0;text-align:right;font-size:12px;color:var(--sub);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dsrc{float:left;color:var(--accent);font-weight:600;max-width:60%;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}

/* 详情内容 */
.dscroll{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;overscroll-behavior:contain}
.dwrap{padding:18px 18px calc(var(--safe-b) + 20px);max-width:680px;margin:0 auto}
.dmeta{display:flex;align-items:center;gap:7px;margin-bottom:9px;font-size:11.5px;color:var(--sub)}
.dtitle{margin:0 0 16px;font-size:23px;line-height:1.35;font-weight:800;letter-spacing:.2px}

.dsec{margin-bottom:18px}
.dsec-h{
  display:flex;align-items:center;gap:6px;
  font-size:11.5px;font-weight:700;color:var(--sub);letter-spacing:.6px;
  margin:0 0 8px;
}
.dsec-h::before{content:"";width:3px;height:11px;border-radius:2px;background:var(--accent)}
.en-tag{font-size:9.5px;font-weight:700;letter-spacing:.5px;color:var(--sub);
  border:.5px solid var(--sep);border-radius:4px;padding:1px 4px}
/* 正文：中英逐段对照 */
.orgtitle{font-size:12.5px;color:var(--sub);line-height:1.55;margin:0 0 15px;
  padding-left:9px;border-left:2px solid var(--sep)}
.segbar{display:flex;background:var(--sep);border-radius:9px;padding:2px;margin-bottom:18px}
.seg{flex:1;border:0;background:none;padding:7px 0;border-radius:7px;font-size:13.5px;
  font-family:inherit;color:var(--text);cursor:pointer;transition:all .15s}
.seg.on{background:var(--card);font-weight:600;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.para{margin-bottom:20px}
.p-en{font-size:16.5px;line-height:1.8;color:var(--sub);margin-bottom:9px}
.p-zh{font-size:16.5px;line-height:1.8;color:var(--text);padding-left:12px;
  border-left:3px solid var(--accent)}
.p-zh.missing{font-size:12px;color:var(--sub);opacity:.65;border-left-color:var(--sep)}
.p-zh-only{font-size:16.5px;line-height:1.8;margin:0 0 17px}
.p-en-only{font-size:16.5px;line-height:1.8;color:var(--sub);margin:0 0 15px}
.p-none{text-align:center;padding:30px 12px;color:var(--sub);font-size:14px}

.dorig{background:var(--card);border:.5px solid var(--sep);border-radius:12px;padding:14px}
.dentitle{margin:0 0 8px;font-size:16px;line-height:1.45;font-weight:600}
.densum{margin:0;font-size:14px;line-height:1.7;color:var(--sub)}
.densum p{margin:0 0 11px}
.densum p:last-child{margin-bottom:0}
.more{
  display:block;width:100%;margin-top:10px;padding:9px 0;
  background:none;border:.5px solid var(--sep);border-radius:9px;
  color:var(--accent);font-size:13.5px;font-weight:600;font-family:inherit;cursor:pointer;
}
.more:active{opacity:.6}
.dlink{display:block;margin-top:10px;font-size:11.5px;color:var(--sub);
  word-break:break-all;text-decoration:none;opacity:.85}

/* 上一条 / 下一条 */
.dnav{display:flex;gap:10px;margin-top:6px}
.dnav button{
  flex:1;padding:11px 0;border-radius:10px;border:.5px solid var(--sep);
  background:var(--card);color:var(--accent);font-size:14px;font-weight:600;
  font-family:inherit;cursor:pointer;
}
.dnav button:disabled{color:var(--sub);opacity:.45}
.dnav button:active:not(:disabled){opacity:.55}

/* 底部操作栏 */
.dbottom{
  flex:none;display:flex;align-items:center;gap:8px;
  padding:10px 14px calc(var(--safe-b) + 10px);
  background:color-mix(in srgb, var(--bg) 92%, transparent);
  backdrop-filter:saturate(180%) blur(20px);-webkit-backdrop-filter:saturate(180%) blur(20px);
  border-top:.5px solid var(--sep);
}
.dbtn{
  display:inline-flex;align-items:center;justify-content:center;
  height:46px;padding:0 14px;border-radius:12px;font-size:15px;font-weight:600;
  font-family:inherit;text-decoration:none;cursor:pointer;border:0;
}
.dbtn.sub{flex:none;background:var(--card);color:var(--text);border:.5px solid var(--sep);font-size:13.5px}
.dbtn.primary{flex:1;background:var(--accent);color:#fff;font-size:16px}
.dbtn.primary span{font-size:15px;margin-left:2px}
.dbtn:active{opacity:.72}

/* 卡片可点击 */
.card{cursor:pointer;transition:background .12s, transform .12s}
.card:active{background:color-mix(in srgb, var(--card) 92%, #000);transform:scale(.994)}
.card .title{pointer-events:none}
.badge-zh{display:none}
"""

DETAIL_JS = """
var DATASETS = __DATA__;
var SECTIONS = __SECTIONS__;
var curSec = SECTIONS[0].key;
var detail  = document.getElementById('detail');
var dscroll = document.getElementById('dscroll');
var list    = document.getElementById('scroll');
var listPos = 0, cur = -1;

/* 当前板块的数据（切换板块时自动跟着变） */
function D(){ return DATASETS[curSec] || []; }

function $(id){ return document.getElementById(id); }
function put(id, v){ $(id).textContent = (v == null ? '' : v); }
function esc(s){
  return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}
function paras(list){
  return (list || []).map(function(p){ return '<p>' + esc(p) + '</p>'; }).join('');
}
var mode = 'both';

function syncSeg(){
  var bs = document.querySelectorAll('.seg');
  for (var i = 0; i < bs.length; i++) {
    bs[i].classList.toggle('on', bs[i].getAttribute('data-m') === mode);
  }
}

function renderBody(d){
  var box = $('d-body');
  var en = d.enbody || [];
  var zh = d.body || [];
  var html = '';

  if (mode === 'zh') {
    var zs = zh.filter(function(x){ return x && x.trim(); });
    if (zs.length) {
      html = zs.map(function(t){ return '<p class="p-zh-only">' + esc(t) + '</p>'; }).join('');
    } else if (d.sum) {
      html = '<p class="p-zh-only">' + esc(d.sum) + '</p>';
    } else {
      html = '<div class="p-none">这篇还没有中文译文</div>';
    }
  } else if (mode === 'en') {
    if (en.length) {
      html = en.map(function(t){ return '<p class="p-en-only">' + esc(t) + '</p>'; }).join('');
    } else if (d.ensum) {
      html = '<p class="p-en-only">' + esc(d.ensum) + '</p>';
    } else {
      html = '<div class="p-none">暂无正文</div>';
    }
  } else {
    if (en.length) {
      for (var i = 0; i < en.length; i++) {
        var z = (i < zh.length && zh[i]) ? zh[i] : '';
        html += '<div class="para">'
             +  '<div class="p-en">' + esc(en[i]) + '</div>'
             +  (z ? '<div class="p-zh">' + esc(z) + '</div>'
                   : '<div class="p-zh missing">（本段未翻译）</div>')
             +  '</div>';
      }
    } else if (d.ensum) {
      html = '<div class="para"><div class="p-en">' + esc(d.ensum) + '</div>'
           + '<div class="p-zh">' + esc(d.sum || '') + '</div></div>';
    } else {
      html = '<div class="p-none">暂无正文，点下方「阅读原文」看完整报道</div>';
    }
  }
  box.innerHTML = html;
}

function render(i){
  var d = D()[i];
  if (!d) return;
  cur = i;
  put('d-title', d.zh || d.en);

  var hb = $('d-hot');
  if (d.hot) { hb.textContent = d.hot; hb.className = 'hot hot' + d.lv; hb.style.display = ''; }
  else { hb.style.display = 'none'; }

  put('d-src',  d.src);
  put('d-time', d.t);
  put('d-rank', (i + 1) + ' / ' + D().length);

  put('d-en-title', d.en);

  /* 顶部大图：有就显示，没有就整块收起来 */
  var box = $('d-imgbox'), im = $('d-img');
  if (d.img) { im.src = d.img; box.style.display = ''; }
  else { im.src = ''; box.style.display = 'none'; }

  mode = 'both';
  syncSeg();
  renderBody(d);

  $('d-open').href = d.link;
  put('d-link', d.link);

  $('d-prev').disabled = (i <= 0);
  $('d-next').disabled = (i >= D().length - 1);
}

function route(){
  var m = /^#\\/n\\/([a-z0-9]+)\\/(\\d+)$/.exec(location.hash || '');
  if (m) {
    var sec = m[1];
    if (DATASETS[sec]) {
      if (sec !== curSec) switchSec(sec, true);
      var i = parseInt(m[2], 10);
      if (i >= 0 && i < D().length) {
        if (!detail.classList.contains('open')) listPos = list.scrollTop;
        render(i);
        detail.classList.add('open');
        detail.setAttribute('aria-hidden', 'false');
        dscroll.scrollTop = 0;
        document.title = (D()[i].zh || D()[i].en).slice(0, 30);
        return;
      }
    }
  }
  detail.classList.remove('open');
  detail.setAttribute('aria-hidden', 'true');
  list.scrollTop = listPos;
  document.title = '每日资讯 · 民航 & 美国要闻';
}

function go(i){
  if (i < 0 || i >= D().length) return;
  history.replaceState(null, '', '#/n/' + curSec + '/' + i);
  render(i);
  dscroll.scrollTop = 0;
}

/* ---- 板块切换 ---- */
function buildFilters(){
  var bar = $('filters');
  var pane = document.getElementById('list-' + curSec);
  var cards = pane ? pane.querySelectorAll('.card') : [];
  var srcs = [];
  for (var i = 0; i < cards.length; i++) {
    if (srcs.indexOf(cards[i].dataset.source) < 0) srcs.push(cards[i].dataset.source);
  }
  bar.innerHTML = '<button class="f on" data-s="">全部</button>';
  srcs.forEach(function(s){
    var b = document.createElement('button');
    b.className = 'f'; b.textContent = s; b.dataset.s = s;
    b.onclick = function(){
      var all = bar.querySelectorAll('.f');
      for (var k = 0; k < all.length; k++) all[k].classList.toggle('on', all[k].dataset.s === s);
      for (var j = 0; j < cards.length; j++) {
        cards[j].style.display = (cards[j].dataset.source === s) ? 'flex' : 'none';
      }
    };
    bar.appendChild(b);
  });
  bar.querySelector('[data-s=""]').onclick = function(){
    var all = bar.querySelectorAll('.f');
    for (var k = 0; k < all.length; k++) all[k].classList.toggle('on', !all[k].dataset.s);
    for (var j = 0; j < cards.length; j++) cards[j].style.display = 'flex';
  };
}

function switchSec(key, keepHash){
  if (!DATASETS[key]) return;
  curSec = key;
  var tabs = document.querySelectorAll('.sectab');
  for (var i = 0; i < tabs.length; i++) {
    tabs[i].classList.toggle('on', tabs[i].getAttribute('data-sec') === key);
  }
  var panes = document.querySelectorAll('.listpane');
  for (var j = 0; j < panes.length; j++) {
    panes[j].classList.toggle('on', panes[j].id === 'list-' + key);
  }
  buildFilters();
  if (!keepHash) {
    detail.classList.remove('open');
    detail.setAttribute('aria-hidden', 'true');
    if (location.hash) history.replaceState(null, '', location.pathname);
    list.scrollTop = 0;
  }
}

var secbar = $('secbar');
if (secbar) {
  secbar.addEventListener('click', function(e){
    var b = e.target.closest ? e.target.closest('.sectab') : null;
    if (b) switchSec(b.getAttribute('data-sec'), false);
  });
}

/* 点卡片进入详情（每个板块各自监听） */
var panesAll = document.querySelectorAll('.listpane');
for (var pi = 0; pi < panesAll.length; pi++) {
  panesAll[pi].addEventListener('click', function(e){
    var c = e.target.closest ? e.target.closest('.card') : null;
    if (c) location.hash = '#/n/' + curSec + '/' + c.getAttribute('data-i');
  });
}

$('d-back').onclick = function(){
  if (history.length > 1) history.back();
  else { location.hash = ''; route(); }
};
$('d-prev').onclick = function(){ go(cur - 1); };
$('d-next').onclick = function(){ go(cur + 1); };

$('d-copy').onclick = function(){
  var link = D()[cur].link;
  var btn = $('d-copy'), old = btn.textContent;
  function done(){ btn.textContent = '已复制'; setTimeout(function(){ btn.textContent = old; }, 1400); }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(link).then(done, function(){
      window.prompt('复制下面的链接：', link); btn.textContent = old;
    });
  } else { window.prompt('复制下面的链接：', link); }
};

var shareBtn = $('d-share');
if (navigator.share) {
  shareBtn.onclick = function(){
    navigator.share({ title: D()[cur].zh || D()[cur].en, url: D()[cur].link }).catch(function(){});
  };
} else { shareBtn.style.display = 'none'; }

document.addEventListener('keydown', function(e){
  if (e.key === 'Escape' && detail.classList.contains('open')) $('d-back').click();
});

var segbar = $('d-segbar');
if (segbar) {
  segbar.addEventListener('click', function(e){
    var b = e.target.closest ? e.target.closest('.seg') : null;
    if (b && cur >= 0) {
      mode = b.getAttribute('data-m');
      syncSeg();
      renderBody(D()[cur]);
    }
  });
}

window.addEventListener('hashchange', route);
route();
"""


def build_html(sections_out):
    """sections_out: [(板块配置, 该板块的新闻列表), ...]"""
    now = datetime.now(tz=timezone(timedelta(hours=8)))
    updated = now.strftime("%Y年%m月%d日 %H:%M")

    datasets = {}
    panes = []
    sec_meta = []

    for pos, (sec, items) in enumerate(sections_out):
        key = sec["key"]
        data_js = []
        cards = []
        for idx, it in enumerate(items):
            label, level = hot_label(it["score"])
            label_html = (f'<span class="hot hot{level}">{label}</span>'
                          if label else '<span class="hot hot0"></span>')
            # 双保险：中文字段为空时一律回退到英文原文，界面上绝不会出现空白标题
            title   = it["title_zh"] or it["title_en"]
            summary = it["summary_zh"] or it["summary"]
            img     = it.get("img") or ""

            data_js.append({
                "zh":     it["title_zh"] or it["title_en"],
                "en":     it["title_en"],
                "sum":    it["summary_zh"] or it["summary"],
                "ensum":  it["summary"],
                "body":   it.get("body_zh") or [],
                # 英文段落跟中文段落取同样长度，对照时才不会末尾挂一串「未翻译」
                "enbody": (it.get("body_en") or [])[:len(it.get("body_zh") or []) or 12],
                "src":    it["source"],
                "t":      fmt_time(it["pub_ts"]),
                "link":   it["link"],
                "img":    img,
                "hot":    label,
                "lv":     level,
            })

            # 图片加载失败就整块隐藏，不留空白框
            thumb = (f'<div class="thumb"><img src="{html.escape(img)}" alt="" '
                     f'loading="lazy" referrerpolicy="no-referrer" '
                     f'onerror="this.parentNode.style.display=\'none\'"></div>'
                     if img else '')

            cards.append(f"""
<article class="card" data-i="{idx}" data-source="{html.escape(it['source'])}" role="button" tabindex="0">
  <div class="rank">{idx + 1}</div>
  <div class="body">
    <div class="meta">
      {label_html}
      <span class="src">{html.escape(it['source'])}</span>
      <span class="time">{fmt_time(it['pub_ts'])}</span>
    </div>
    <h2 class="title">{html.escape(title)}</h2>
    {f'<p class="sum">{html.escape(summary)}</p>' if summary else ''}
  </div>
  {thumb}
  <div class="go">›</div>
</article>""")

        cards_html = "\n".join(cards) if cards else \
            '<div class="empty">这个板块今天没抓到新闻，明天再来。</div>'
        panes.append(
            f'<div class="listpane{" on" if pos == 0 else ""}" id="list-{key}">\n'
            f'{cards_html}\n</div>')
        datasets[key] = data_js
        sec_meta.append({"key": key, "name": sec["name"], "icon": sec["icon"]})

    tabs = "\n".join(
        f'    <button class="sectab{" on" if i == 0 else ""}" data-sec="{s["key"]}">'
        f'{s["icon"]} {s["name"]}</button>'
        for i, s in enumerate(sec_meta))

    payload = json.dumps(datasets, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("</", "<\\/").replace("<!--", "<\\!--")
    sec_payload = json.dumps(sec_meta, ensure_ascii=False, separators=(",", ":"))
    js = (DETAIL_JS.replace("__DATA__", payload)
                   .replace("__SECTIONS__", sec_payload))

    panes_html = "\n".join(panes)
    footers = " ｜ ".join(
        f'{sec["name"]}：{" / ".join(n for n, _, _ in sec["sources"][:6])}'
        for sec, _ in sections_out)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#F6F7F9">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="每日资讯">
<title>每日资讯 · 民航 & 美国要闻</title>
<link rel="manifest" href="manifest.json">
<link rel="apple-touch-icon" href="icon-180.png">
<style>
:root{{
  --bg:#F6F7F9; --card:#FFF; --text:#111; --sub:#8A8F99; --sep:#EBECF0;
  --accent:#0064E0; --hot3:#FF3B30; --hot2:#FF9500; --hot1:#34A853;
  --safe-t:env(safe-area-inset-top,0px); --safe-b:env(safe-area-inset-bottom,0px);
}}
@media (prefers-color-scheme: dark){{
  :root{{--bg:#000;--card:#1C1C1E;--text:#F2F2F7;--sub:#98989D;--sep:#2C2C2E;--accent:#0A84FF;}}
}}
*{{box-sizing:border-box;-webkit-tap-highlight-color:transparent}}
html,body{{margin:0;height:100%;overflow:hidden}}
body{{background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC","Helvetica Neue",sans-serif;
  overscroll-behavior:none;}}

/* 顶栏 */
.topbar{{
  position:fixed;top:0;left:0;right:0;z-index:20;
  padding:calc(var(--safe-t) + 12px) 16px 12px;
  background:color-mix(in srgb, var(--bg) 90%, transparent);
  backdrop-filter:saturate(180%) blur(20px);-webkit-backdrop-filter:saturate(180%) blur(20px);
  border-bottom:.5px solid var(--sep);
}}
.topbar h1{{margin:0;font-size:26px;font-weight:800;letter-spacing:.3px}}
.topbar .sub{{font-size:12px;color:var(--sub);margin-top:3px}}

/* 板块切换（顶部，像新闻 App 那样） */
.segtop{{display:flex;gap:4px;background:var(--sep);border-radius:11px;padding:3px}}
.sectab{{
  flex:1;border:0;background:transparent;padding:9px 4px;border-radius:9px;
  font-family:inherit;font-size:14.5px;font-weight:600;color:var(--sub);
  transition:background .18s,color .18s;
}}
.sectab.on{{background:var(--card);color:var(--text);box-shadow:0 1px 4px rgba(0,0,0,.10)}}
.listpane{{display:none}}
.listpane.on{{display:block}}

/* 列表配图 / 详情大图 */
.thumb{{flex:none;width:76px;height:76px;margin-left:9px;border-radius:9px;overflow:hidden;background:var(--sep)}}
.thumb img{{width:100%;height:100%;object-fit:cover;display:block}}
.dimg{{margin:0 0 14px;border-radius:12px;overflow:hidden;background:var(--sep)}}
.dimg img{{width:100%;display:block;max-height:230px;object-fit:cover}}

/* 内容 */
.scroll{{position:fixed;top:calc(var(--safe-t) + 64px);bottom:0;left:0;right:0;
  overflow-y:auto;-webkit-overflow-scrolling:touch;
  padding:0 12px calc(var(--safe-b) + 24px);}}

/* 来源筛选 */
.filters{{display:flex;gap:7px;overflow-x:auto;padding:12px 4px;scrollbar-width:none}}
.filters::-webkit-scrollbar{{display:none}}
.f{{flex:none;padding:6px 13px;border-radius:999px;font-size:13px;background:var(--card);
  border:.5px solid var(--sep);color:var(--text);cursor:pointer;font-family:inherit;transition:all .15s}}
.f.on{{background:var(--accent);color:#fff;border-color:transparent;font-weight:600}}

/* 新闻卡片 */
.card{{
  display:flex;gap:10px;align-items:center;background:var(--card);border-radius:14px;padding:14px;
  margin-bottom:10px;border:.5px solid var(--sep);
  box-shadow:0 1px 3px rgba(0,0,0,.04);
}}
.rank{{
  flex:none;width:26px;font-size:20px;font-weight:800;text-align:center;
  color:var(--sub);font-variant-numeric:tabular-nums;
}}
.card:nth-child(-n+3) .rank{{color:var(--hot3)}}
.body{{flex:1;min-width:0}}
.meta{{display:flex;align-items:center;gap:7px;margin-bottom:5px;font-size:11.5px;color:var(--sub)}}
.hot{{font-size:10px;font-weight:700;padding:2px 5px;border-radius:4px;color:#fff;flex:none}}
.hot3{{background:var(--hot3)}} .hot2{{background:var(--hot2)}} .hot1{{background:var(--hot1)}}
.hot0{{display:none}}
.src{{font-weight:600;color:var(--accent)}}
.title{{margin:0;font-size:16.5px;font-weight:700;line-height:1.38;color:var(--text)}}
.sum{{font-size:14px;color:var(--sub);line-height:1.55;margin:5px 0 0;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
.go{{flex:none;color:var(--sub);font-size:20px;opacity:.45;margin-right:-2px}}
.empty{{text-align:center;padding:60px 20px;color:var(--sub);font-size:15px}}

/* 底部信息 */
.footer{{text-align:center;padding:8px 16px 4px;font-size:11px;color:var(--sub);opacity:.7}}
{DETAIL_CSS}
</style>
</head>
<body>

<div class="topbar">
  <div class="segtop" id="secbar">
{tabs}
  </div>
  <div class="sub">每日自动更新 · 更新于 {updated}</div>
</div>

<div class="scroll" id="scroll">
  <div class="filters" id="filters">
    <button class="f on" data-s="">全部</button>
  </div>
{panes_html}
  <div class="footer">{footers}</div>
</div>

<!-- 二级页面：新闻详情 -->
<div class="detail" id="detail" aria-hidden="true">
  <div class="dbar">
    <button class="dback" id="d-back"><span class="chev">‹</span> 返回</button>
    <div class="dbar-info">
      <span class="dsrc" id="d-src"></span>
      <span id="d-rank"></span>
    </div>
  </div>

  <div class="dscroll" id="dscroll">
    <div class="dwrap">
      <div class="dmeta">
        <span class="hot" id="d-hot"></span>
        <span id="d-time"></span>
      </div>
      <h1 class="dtitle" id="d-title"></h1>

      <div class="dimg" id="d-imgbox" style="display:none">
        <img id="d-img" alt="" referrerpolicy="no-referrer"
             onerror="this.parentNode.style.display='none'">
      </div>

      <div class="orgtitle" id="d-en-title"></div>

      <div class="segbar" id="d-segbar">
        <button class="seg on" data-m="both">中英对照</button>
        <button class="seg" data-m="zh">只看中文</button>
        <button class="seg" data-m="en">只看英文</button>
      </div>

      <div class="dbody" id="d-body"></div>

      <div class="dlink" id="d-link"></div>

      <div class="dnav">
        <button id="d-prev">‹ 上一条</button>
        <button id="d-next">下一条 ›</button>
      </div>
    </div>
  </div>

  <div class="dbottom">
    <button class="dbtn sub" id="d-copy">复制链接</button>
    <button class="dbtn sub" id="d-share">分享</button>
    <a class="dbtn primary" id="d-open" href="#" target="_blank" rel="noopener">阅读原文 <span>↗</span></a>
  </div>
</div>

<script>
{js}
buildFilters();
</script>
</body>
</html>"""


ICON_B64 = "iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAYAAAA9zQYyAAADtElEQVR42u3dQXIaSRRF0cqM2gcsTl4VLE5aCR44HPZASAioIv/Lc4fdEW21OHr86rDptozS2/tlUd3OxzbCl9EAVhLwBrGScDeIlYS7gawk2A1kJcHuMOulPdlMA1lJa91hVtJad5iVhLqBrKQTpMOspLXuMCsJdYdZSai775aS6s/+CZFeudIdZiWh7jArCbUbWpPc0NZZBVe6w6wk1E4OhZ8c1lmFV9pCK3ihrbOKr7SFVvgNLUWAdm4o4Oyw0HJySGODdm4o5Oyw0Dt1OR18E5wcEtDDrrOV3gO0+1lBd7SF3vl2ttJODgnoEdfZSgMtAT3yOltpoCWgR15nAQ2+gIYUaPkBAFpwAi0/CEBDKaAFtPZeZwsPtAT0yLezlQZaAnrkRbXSQEtAj7ykVhpoCWgBLQEtAS0BLaAloCWgJaAloAW0BLQEtAS0BLSAloCWgJaAloAW0BLQEtAS0BLQAloCOr89Pnvucjr4jLs7asvb+8W3YRzIV1+oXx9eAKDrQwYb6EjIYAMdCRlsD4XRD2IeHC10LAprPTno1HWbGfaUoGd5m54RdofZM4GFBtliAw0y2ECDDPbcN7Tf1DP3D33MQkNsrSNAgwx2BGiQwY4ADTLYEaBBBvuWOsy69TWo8DoMvdAgW+yIhfbfkr1rRiw0xNY6AjTIYEeABhnsKW5o6d5W34L71mfrd5f/V847GdBRtyHcQMc8tX/3tQEOdCnA1hvoaMDWG+hoxIBPAtqnCM15nqwAw50EfIVYScBXgJV0nqwAK2m9V4iVtN4rwEpa7w6zkl5Tv31UUQEtoCWgJaAloAW0BLQEtG7LH6cCOg4b1EBvDmxvZFADHQfLp7ECHYkJaqDjAFlroCPRQA10HBRrDXQkDqiBjgNhrf8U8//6HgnyZ38M6XI6XP3re/z6s+TTR3dCdO3v+/RQoMsgfuSfBzfQJRF/9+uADXTUTWq1f/pQuCzLrA+GXwEZ/cGq8te+Wedjs9BFIVhuJ0fsmsH9rw6zf6c80Odj82al6vfz9AstJ4dUBLSzQ8XPDQstJ4dUB7SzQ4XPDQutCU4OK62i62yhNclDoZVWwXX+eqGhVjHMTg5NcnJYaRVc59sWGmoVwXz7yQG1CmB2Q2uyG9pKq9A6/3yhodbAmO87OaDWoJiX5e/nctzbxB/0qLEgP+eh0FprIMyPg4ZaA2F+/ORwgmgQyM9baGutgcxsB9Ba6wXjt/2igq0d38X3PRHghnjjXnfzwg1xFGjAAd6g36rs1XqMajeMAAAAAElFTkSuQmCC"

MANIFEST = {
    "name": "每日资讯 · 民航 & 美国要闻",
    "short_name": "每日资讯",
    "start_url": "./index.html",
    "scope": "./",
    "display": "standalone",
    "orientation": "portrait",
    "background_color": "#F6F7F9",
    "theme_color": "#0064E0",
    "lang": "zh-CN",
    "icons": [
        {"src": "icon-180.png", "sizes": "180x180", "type": "image/png", "purpose": "any"},
    ],
}


def write_assets():
    """图标和 PWA 配置内嵌在脚本里，无需额外文件。"""
    with open(os.path.join("docs", "icon-180.png"), "wb") as f:
        f.write(base64.b64decode(ICON_B64))
    with open(os.path.join("docs", "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(MANIFEST, f, ensure_ascii=False, indent=2)


def prev_has_chinese():
    """上一版网页是不是中文的？用来决定这次能不能覆盖它。"""
    path = os.path.join("docs", "data.json")
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return any(has_cn(i.get("title_zh", "")) for i in data)
    except Exception:
        pass
    return False


def main():
    os.makedirs("docs", exist_ok=True)

    log("GITHUB_TOKEN: " + ("已配置" if os.environ.get("GITHUB_TOKEN") else "缺失"))
    probe = _translate_google("Boeing 737", sl="en", timeout=10)
    log("Google 通道探测: " + (repr(probe) if probe else "不可用"))

    sections_out = []
    zh_total = 0
    for sec in SECTIONS:
        print(f"\n===== 板块：{sec['name']} =====")
        items = collect(sec["sources"], sec["hot"],
                        groups=sec.get("groups"), quota=sec.get("quota"))
        print(f"共采集 {len(items)} 条")
        if items:
            enrich_bodies(items)        # 摘要不够长的，去原网页补全文
            log("翻译中 ...")
            translate_items(items)
        zh_total += sum(1 for it in items if has_cn(it.get("title_zh", "")))
        sections_out.append((sec, items))

    # 防退化：这次一条都没翻成功、而上一版是中文时，
    # 保留上一版网页不动，绝不把网站打回纯英文
    total_items = sum(len(items) for _, items in sections_out)
    if total_items and zh_total == 0 and prev_has_chinese():
        print("！本次翻译全部失败，保留上一版中文网页，不覆盖")
    else:
        out = build_html(sections_out)
        with open("docs/index.html", "w", encoding="utf-8") as f:
            f.write(out)
        # 分板块保存原始数据（data.json 保持民航，向后兼容）
        for sec, items in sections_out:
            name = "data.json" if sec["key"] == "aviation" else f"{sec['key']}.json"
            with open(os.path.join("docs", name), "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False, indent=2)
        print(f"已生成 docs/index.html（{len(out)} 字符）")

    # 统计一下这轮的翻译成果，一起写进日志
    for sec, items in sections_out:
        en_p = sum(len(it.get("body_en") or []) for it in items)
        zh_p = sum(len([x for x in (it.get("body_zh") or []) if x.strip()]) for it in items)
        zh_t = sum(1 for it in items if has_cn(it.get("title_zh", "")))
        img_n = sum(1 for it in items if it.get("img"))
        log(f"结果[{sec['name']}]：{len(items)} 条，中文标题 {zh_t}，"
            f"英文段落 {en_p}，中文段落 {zh_p}，配图 {img_n}")

    write_assets()

    # 把诊断日志存到 docs/，这样上线后能直接打开查看失败原因
    try:
        with open(os.path.join("docs", "translate_log.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(RUN_LOG[-500:]))
    except Exception as e:
        print(f"(日志写入失败: {e})")


if __name__ == "__main__":
    main()
