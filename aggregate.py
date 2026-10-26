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
# quota: {分组: (最少条数, 最多条数)}，_max_per_source 限制同一媒体的席位数
SECTIONS = [
    {"key": "aviation", "name": "民航资讯", "icon": "✈️",
     "sources": SOURCES_AVIATION, "hot": HOT_WORDS_AVIATION,
     "groups": None, "quota": None},
    {"key": "us",       "name": "美国要闻", "icon": "🇺🇸",
     "sources": SOURCES_US,       "hot": HOT_WORDS_US,
     "groups": US_GROUPS, "quota": {"tech": (5, 12), "_max_per_source": 3}},
]

MAX_ITEMS      = 40        # 最终保留多少条
TRANSLATE_TO   = "zh-CN"   # 目标语言
FETCH_PER_SRC  = 12        # 每个源最多取多少条
REQUEST_DELAY  = 0.4       # 抓取间隔（秒），对服务器友好

# ---- 同一事件归并 ----
EVENT_GROUP       = True   # 打开后，同事件的几家报道会折叠成一张卡
EVENT_BODY_OVERLAP = 0.14  # 正文前几段实词重合度达到这个值，就认定是同一事件
                           # 实测：真同事件 0.14~0.59，不同事件（同州不同事）≤0.07

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
                  "skip to content", "skip to navigation", "toggle navigation",
                  "main menu", "open menu", "close menu", "search this site",
                  "sign in to", "back to top", "follow npr",
                  # 现场实测抓出来的残留（NPR 播放器、Guardian live blog 等）
                  "embed iframe", "this live blog is now closed",
                  "read our full report", "more on this story",
                  "related coverage", "transcripts may be",
                  "npr transcripts", "was this helpful",
                  "republished with permission", "originally published",
                  "share this page", "most viewed", "latest stories",
                  "home airlines",             # Aviation24 的面包屑导航
                  "ie 11", "optimal experience",  # NBC 等站的浏览器兼容提示
                  "another browser", "update your browser")

# 老译文里可能还挂着这些当年的站点标识/导航词，属于「陈旧译文」，必须重翻不能复用
STALE_CN = ("|卫报", "｜卫报", "|the guardian", "跳到主要内容", "跳到导航",
            "播客和节目", "展开/折叠", "嵌入iframe", "嵌入 iframe",
            "首页 航空", "跳过导航", "返回顶部")

# 「竖线 + 站点名」才算站点标识。注意不能只看竖线——
# 科技文章正文里经常有命令行、表格，竖线本身不是问题。
STALE_SITE_RE = re.compile(
    r"[|｜]\s*(卫报|guardian|npr|abc news|cbs news|nbc news|cnn|pbs|"
    r"politico|axios|nytimes|纽约时报|福克斯|天空新闻)")

# 社交分享条里常见的平台名。注意：单独出现不算噪声（正文里也会提 Facebook），
# 要一段里同时冒出好几个才判为分享条。
SOCIAL_WORDS = ("facebook", "instagram", "linkedin", "twitter", "x.com",
                "whatsapp", "telegram", "pinterest", "reddit", "vkontakte",
                "youtube", "tiktok", "rss", "newsletter", "podcast")

# 段落里挂在开头或结尾的站点标识/跳转链接，直接裁掉而不是丢掉整段
SITE_CHROME = ("skip to main content", "skip to content", "skip to navigation",
               "jump to content", "|卫报", "| the guardian", "| guardian",
               "| abc news", "| pbs", "| npr", "| cnn", "| nytimes",
               "| axios", "| politico", "| cbs news", "| nbc news",
               "跳到主要内容", "跳到导航", "分享此页", "查看更多")


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


def clean_chrome(p):
    """裁掉段落开头/结尾挂着的站点标识和跳转链接文字。

    比如卫报的《标题 | The Guardian 跳到主要内容 跳到导航》——整段是有用的标题+导语，
    因为尾巴上沾了这些词就被丢掉太可惜，所以只裁掉尾巴。
    """
    changed = True
    while changed:
        changed = False
        for w in SITE_CHROME:
            ww = re.escape(w)
            # 尾巴上的：允许前面挂分隔符（Case 1: "...coaster |卫报"）
            new = re.sub(r"[\s|·—–,，:：-]*%s[\s|·—–,，:：-]*$" % ww, "", p, flags=re.I)
            if new == p:               # 开头的：「Skip to main content …」
                new = re.sub(r"^[\s|·—–,，:：-]*%s[\s|·—–,，:：-]*" % ww, "", p, flags=re.I)
            # 别为了擦掉几个字把整段弄没（后面还有 min_len 兜底，这里放宽些）
            if new != p and len(new.strip()) >= 12:
                p, changed = new, True
                break
    return p.strip(" |·-—–,，:：")


def _looks_embedded(p):
    """判断这一段其实是没清干净的播放器/嵌入代码残留。"""
    low = p.lower()
    return ('src="http' in low or 'href="http' in low
            or low.count('="') >= 3
            or ('width=' in low and 'height=' in low))


def _looks_socialbar(p):
    """判断这一段是文章末尾/侧栏的社交分享条。

    正文里提到一两个平台很正常（科技新闻天天讲 Facebook），
    但一段短文字里同时冒出三个以上平台名，基本就是分享按钮了。
    """
    low = (p or "").lower()
    if len(low) > 200:
        return False
    hit = sum(1 for w in SOCIAL_WORDS if w in low)
    return hit >= 3


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
        # 播放器 / 嵌入代码没清干净的残留，直接丢
        if _looks_embedded(p):
            continue
        # 社交分享条（Facebook Instagram LinkedIn RSS X ...）
        if _looks_socialbar(p):
            continue
        p = clean_chrome(p)
        if len(p) < min_len:
            continue
        low = p.lower()[:120]
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


def best_paras(raw):
    """从网页 HTML 里挑出正文最多的一段提取结果。

    main_block 能把导航菜单挡在外面，但不是所有站点都把正文放在 <article> 里——
    NPR 就是典型：<article> 里只有标题和播放器，真正的正文在外面。
    所以这里把「只取正文区块」和「整页」两种切法都算一遍，取正文更多的那一种，
    保证加了 main_block 之后抓取效果只会变好、不会变差。
    """
    block = main_block(raw)
    cands = [raw] if block is raw else [block, raw]
    best = []
    for c in cands:
        ps = html_to_paragraphs(c)
        if sum(len(x) for x in ps) > sum(len(x) for x in best):
            best = ps
    return best


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


# ============================================================
# 1.5 同一事件归并
# ============================================================
# 同一条新闻常有五六家媒体分头报道（阿拉斯加参议员选举诉讼一次来了 5 条）。
# 全部删掉会丢信息，全部留下又刷屏，所以聚成「事件簇」：
# 列表里只占一张卡，卡上标「5 家报道」，详情页里能切换着看。

# 地名/国家/州：事件的「坐标」
EVENT_PLACES = set("""alaska california kansas arkansas georgia florida ohio michigan
texas arizona nevada pennsylvania wisconsin virginia carolina milwaukee doral york jersey
washington chicago boston houston atlanta phoenix denver seattle miami detroit nashville
iran israel china ukraine taiwan japan korea russia india brazil mexico canada france
germany britain gulf venezuela milan europe africa asia""".split())

# 事件核心词：真正描述「发生了什么」的词
EVENT_CORE = set("""election vote voter ballot ballots midterm primary poll polls campaign
race seat candidate democrat democrats gop republican republicans justice doj court judge
lawsuit sue sues sued ruled ruling certify certification challenge dismiss sequester
delay results recount immigration border deport deportation detention tariff trade
shooting shooter killed injured dies death hospital police suspect protest protests
march strike missile military war senate congress impeachment budget shutdown""".split())

# 词形归并：sue / sues / lawsuit 说的是同一件事，判重时必须当成同一个词
EVENT_LEMMA = {
    "sue": "sue", "sues": "sue", "sued": "sue", "suing": "sue",
    "lawsuit": "sue", "lawsuits": "sue", "litigation": "sue",
    "challenge": "sue", "challenges": "sue", "challenged": "sue",
    "judge": "court", "judges": "court", "court": "court", "courts": "court",
    "ruling": "court", "ruled": "court",
    "certify": "certify", "certification": "certify", "certified": "certify",
    "sequester": "sequester", "sequestered": "sequester",
    "vote": "vote", "votes": "vote", "voter": "vote", "voters": "vote",
    "voting": "vote", "ballot": "vote", "ballots": "vote",
    "race": "race", "races": "race", "seat": "race", "seats": "race",
    "democrat": "dem", "democrats": "dem", "democratic": "dem",
    "republican": "gop", "republicans": "gop",
    "result": "result", "results": "result",
    "delay": "delay", "delays": "delay", "delayed": "delay",
    "dismiss": "dismiss", "dismissed": "dismiss", "dismissal": "dismiss",
    "protest": "protest", "protests": "protest", "protester": "protest",
    "protesters": "protest",
    "shoot": "shoot", "shooting": "shoot", "shooter": "shoot",
    "shootings": "shoot",
    "kill": "kill", "killed": "kill", "killing": "kill", "dead": "kill",
    "death": "kill", "dies": "kill", "died": "kill",
    "injure": "injure", "injured": "injure", "injuries": "injure",
    "deport": "deport", "deported": "deport", "deportation": "deport",
    "detention": "detention", "detained": "detention", "detain": "detention",
    "tariff": "tariff", "tariffs": "tariff",
    "poll": "poll", "polls": "poll", "polling": "poll",
}

# 停用词：不算信息量
EVENT_STOP = set("""the a an and or but of to in on for at by from as is are was were be
been being it its this that these those will would could should can may might must have
has had do does did with about after before over under into out up down off than then so
if not no more most other some such only own same too very get got make made take took
go goes going say says said see seen know known come comes what who whom which when where
why how his her their our your my him them us they we you i he she want wants doing am
also one two three first last new now way like just even still back again around because
while report reports here what's there's they're we're don't didn't isn't wasn't""".split())


def _ev_tokens(text):
    return re.findall(r"[A-Za-z][A-Za-z'\-]{2,}", text or "")


def _ev_lemma(w):
    w = (w or "").lower().strip(".")
    return EVENT_LEMMA.get(w, w)


def _ev_places(title):
    return {w.lower().strip(".") for w in _ev_tokens(title)} & EVENT_PLACES


def _ev_core(title):
    """标题里的事件核心词，先做词形归并（sues / lawsuit 都算 sue）。"""
    return {_ev_lemma(w) for w in _ev_tokens(title)
            if w.lower().strip(".") in EVENT_CORE}


def _ev_words(it):
    """正文前几段 + 摘要的实词集合，用来判断是不是同一件事。"""
    parts = (it.get("body_en") or [])[:3]
    txt = " ".join(parts)
    if len(txt) < 220:                       # 正文太短（NYT 常常只给一段）就用摘要补
        txt += " " + (it.get("summary") or "")
    return {w.lower() for w in _ev_tokens(txt)
            if len(w) > 3 and w.lower() not in EVENT_STOP}


def _ev_overlap(wa, wb):
    """两篇正文实词的重合度；样本太小就返回 None（不下结论）。"""
    if min(len(wa), len(wb)) < 8:
        return None
    return len(wa & wb) / min(len(wa), len(wb))


def _same_event(a, b, wa, wb):
    """两篇是不是同一事件。先框住「同一个地方/国家」，再看是否同一件事。

    - 标题核心词撞上两个（阿拉斯加 + 选举 / 司法部 + 诉讼）：直接算同事件
    - 否则看正文前几段实词重合度，够了也算（同一事件各家措辞不同，标题往往对不上）。
      但这条要额外要求「两篇标题都至少带一个事件核心词」——否则像
      「Audi 在乔治亚建厂」和「Rick Jackson 在乔治亚竞选」这种同州不同事会被误并。
    """
    if not (_ev_places(a["title_en"]) & _ev_places(b["title_en"])):
        return False
    ca, cb = _ev_core(a["title_en"]), _ev_core(b["title_en"])
    if len(ca & cb) >= 2:
        return True
    if not (ca and cb):                      # 有一边标题完全看不出事件类型，先不合并
        return False
    ov = _ev_overlap(wa, wb)
    return ov is not None and ov >= EVENT_BODY_OVERLAP


def group_events(items):
    """把同一事件的新闻聚成簇，返回 [[主条, 同事件的其他条...], ...]。

    簇内按分数降序，第一条是主条（列表里显示的那条）；簇之间也按主条分数降序。
    只有 1 条的簇就是普通新闻。
    """
    if not EVENT_GROUP:
        return [[it] for it in items]

    words = [_ev_words(it) for it in items]
    n = len(items)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for i in range(n):
        for j in range(i + 1, n):
            if _same_event(items[i], items[j], words[i], words[j]):
                union(i, j)

    buckets = {}
    for i in range(n):
        buckets.setdefault(find(i), []).append(items[i])

    clusters = []
    for group in buckets.values():
        group.sort(key=lambda x: x["score"], reverse=True)
        clusters.append(group)
    clusters.sort(key=lambda g: g[0]["score"], reverse=True)
    return clusters


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
        # 两种切法择优：既能避开菜单，又不会像 NPR 那样把正文切没
        paras = best_paras(raw)
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

    # 同一家媒体最多占几席，免得科技的 5 个名额全被 TechCrunch 包了
    cap_src = quota.get("_max_per_source") or 0

    def pick(list_obj, want, ban=None):
        """从 list_obj 里挑 want 条出来（避开已选和超配额的来源）。"""
        got = []
        for it in list_obj:
            if len(got) >= want:
                break
            if id(it) in cid:
                continue
            if cap_src:
                already = sum(1 for x in chosen + got if x.get("source") == it.get("source"))
                if already >= cap_src:
                    continue
            got.append(it)
        return got

    chosen, cid = [], set()
    for g, val in quota.items():
        if not isinstance(g, str) or g.startswith("_"):   # _ 开头的是全局参数，不是分组
            continue
        mn, mx = val
        for it in pick(groups.get(g, []), mn):
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
        if cap_src:
            already_src = sum(1 for x in chosen if x.get("source") == it.get("source"))
            if already_src >= cap_src:
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


def _pack_for(it, idxs):
    """取出条目里指定段号的英文，打包成待翻列表（受单篇字数上限约束）。"""
    paras, keep, total = [], [], 0
    for i in idxs:
        if i >= len(it["body_en"]):
            continue
        p = it["body_en"][i]
        if total and total + len(p) > BODY_TRANSLATE:
            break
        paras.append(p)
        keep.append(i)
        total += len(p)
    return paras, keep


def _fp(p):
    """段落指纹：去掉标点空格后取前 80 个字符。

    用来判断「这一段英文」和「缓存里存的英文」是不是同一段话——
    只要正文有增删或顺序变动，指纹就对不上，从而避免复用错位了的旧译文。
    """
    return re.sub(r"\W+", "", (p or "").lower())[:80]


def _numset(s):
    return set(re.findall(r"\d[\d,\.]*", s or ""))


def _is_stale(zh):
    """判断一段中文是不是「清洗之前留下的老译文」。

    特征是尾巴上挂着站点标识（|卫报）或导航词（跳到主要内容）——
    现在的英文侧早就把这些剪掉了，中文里还留着就说明是旧货。
    """
    zl = (zh or "").lower()
    return any(w in zl for w in STALE_CN) or bool(STALE_SITE_RE.search(zl))


def _cached_body_aligned(en, czhs):
    """旧缓存没有指纹时，按位置复用之前先做体检。

    两把尺子：
      ① 数字——英文段落里的数字（日期、金额、百分比）翻译后一定留在对应中文里；
      ② 长度——中文字符数通常落在英文字符数的 0.18~2 倍之间，差太远就是张冠李戴。
    任一把尺子量出大面积不符，就判定「两次抓到的正文顺序不一样」，整篇重翻。
    """
    if len(czhs) != len(en):
        return False
    n_checked = n_bad = 0
    l_checked = l_bad = 0
    for i, p in enumerate(en):
        en_n = _numset(p)
        if len(en_n) >= 2:                       # 数字太少不作数
            n_checked += 1
            if not (en_n & _numset(czhs[i])):
                n_bad += 1
        if len(p) >= 60 and czhs[i]:
            l_checked += 1
            r = len(czhs[i]) / len(p)
            if r < 0.18 or r > 2.0:
                l_bad += 1
    # 有数字样本就以数字为准，最可靠
    if n_checked >= 2:
        return n_bad <= n_checked * 0.3
    # 数字太少就退而看长度比例
    if l_checked >= 3:
        return l_bad <= l_checked * 0.35
    # 实在没什么可比的，就信它（总比每次都重翻强）
    return True


def match_cached_body(en, c):
    """把缓存里的译文按「段落指纹」对回当前英文，返回 (中文列表, 待翻段号)。

    这是逐段对照不错位的关键：绝不按位置硬套，而是一段一段认人。
    认不出来的段留空，交给翻译流程补上。
    """
    zh = [""] * len(en)
    if not en:
        return zh, []
    cfps = (c.get("f") or "").split("\n")
    czhs = (c.get("b") or "").split("\n")

    # 陈旧译文（还挂着站点标识/导航词）一律整篇重翻，两条匹配路径都不例外
    if czhs and any(_is_stale(z) for z in czhs):
        return zh, list(range(len(en)))

    if cfps and len(cfps) == len(czhs):
        # ① 新缓存带指纹：逐段认人，最可靠
        idx = {}
        for j, f in enumerate(cfps):
            if f and f not in idx:
                idx.setdefault(f, j)
        miss = []
        for i, p in enumerate(en):
            j = idx.get(_fp(p))
            if j is not None and has_cn(czhs[j]):
                zh[i] = czhs[j]
            else:
                miss.append(i)
        return zh, miss

    if not czhs or not any(has_cn(x) for x in czhs):
        return zh, list(range(len(en)))

    # ② 老缓存没指纹：段落数一致且数字对得上才敢按位置复用
    if _cached_body_aligned(en, czhs):
        for i in range(len(en)):
            if i < len(czhs) and has_cn(czhs[i]):
                zh[i] = czhs[i]
        return zh, [i for i in range(len(en)) if not has_cn(zh[i])]

    # ③ 对不上就整篇重翻，宁可多花额度也不能让译文张冠李戴
    return zh, list(range(len(en)))


def align_bodies(items):
    """写进网页前最后兜底：中文段落数必须等于英文段落数。

    少的位置补空串（详情页会显示英文），多的直接截掉——
    这样「逐段对照」模式永远不会多挂一串、也不会前后错开。
    """
    for it in items:
        en = it.get("body_en") or []
        zh = list(it.get("body_zh") or [])
        it["body_zh"] = (zh + [""] * len(en))[:len(en)]


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
        it["body_zh"] = []
        if not it["title_zh"]:
            title_todo.append(it)
        if (it.get("summary") or "").strip() and not it["summary_zh"]:
            sum_todo.append(it)
        if it.get("body_en"):
            zh, miss = match_cached_body(it["body_en"], c)
            it["body_zh"] = zh
            if miss:
                it["_miss"] = miss
                body_todo.append(it)
            elif any(has_cn(v) for v in zh) and not c.get("f"):
                # 老缓存没指纹、但这次校验通过了：顺手把指纹补上。
                # 从下一轮起这篇就是精确匹配，再也不用靠猜。
                rec = cache.setdefault(it["link"], {})
                rec["f"] = "\n".join(_fp(p) for p in it["body_en"])
                rec["ts"] = time.time()

    hit = len(items) - len(title_todo)
    print(f"  缓存命中 {hit} 条，需新翻译 {len(title_todo)} 条")
    # 段落级命中情况写进日志，方便事后核对有没有大面积错位
    en_seg = sum(len(it.get("body_en") or []) for it in items)
    zh_seg = sum(1 for it in items for v in (it.get("body_zh") or []) if has_cn(v))
    if en_seg:
        print(f"  正文段落复用 {zh_seg}/{en_seg} 段" +
              (f"，待翻 {en_seg - zh_seg} 段" if zh_seg < en_seg else ""))

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
            paras, keep = _pack_for(it, it.get("_miss") or [])
            packs.append(paras)
            it["_keep"] = keep

        def absorb(todo_list, packs_list, zhs):
            """把译文按段号填回条目并更新缓存，返回 (成功篇数, 仍缺的条目, 仍缺的段落包)。

            注意：译文是「哪一段翻的」就写回「哪一段」，绝不按顺序硬塞——
            这就是逐段对照不会错位的最后一道保险。
            """
            ok, rest_it, rest_pk = 0, [], []
            for it, pack, zh in zip(todo_list, packs_list, zhs):
                en = it["body_en"]
                body = list(it.get("body_zh") or [""] * len(en))
                if zh:
                    keep = it.get("_keep") or []
                    clean = [(x or "").replace("\n", " ").strip() for x in zh]
                    for k, i in enumerate(keep):
                        if k < len(clean) and i < len(body) and has_cn(clean[k]):
                            body[i] = clean[k]
                it["body_zh"] = body
                if any(has_cn(x) for x in body):
                    rec = cache.setdefault(it["link"], {})
                    rec["b"] = "\n".join(body)
                    rec["f"] = "\n".join(_fp(p) for p in en)
                    rec["ts"] = time.time()
                left = [i for i in range(len(en)) if not has_cn(body[i])]
                if not left:
                    ok += 1
                    continue
                more, keep = _pack_for(it, left)
                if not more:            # 剩下的太长，这一轮先不翻了，别空转
                    continue
                rest_it.append(it)
                rest_pk.append(more)
                it["_keep"] = keep
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
.orgtitle{font-size:12.5px;color:var(--text);line-height:1.55;margin:0 0 15px;
  padding-left:9px;border-left:2px solid var(--sep)}
.segbar{display:flex;background:var(--sep);border-radius:9px;padding:2px;margin-bottom:18px}
.seg{flex:1;border:0;background:none;padding:7px 0;border-radius:7px;font-size:13.5px;
  font-family:inherit;color:var(--text);cursor:pointer;transition:all .15s}
.seg.on{background:var(--card);font-weight:600;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.para{margin-bottom:20px}
/* 以英文为主阅读语言：英文用主色，中文译文退为次要色 */
.p-en{font-size:16.5px;line-height:1.8;color:var(--text);margin-bottom:9px}
.p-zh{font-size:16.5px;line-height:1.8;color:var(--sub);padding-left:12px;
  border-left:3px solid var(--accent)}
.p-zh.missing{font-size:12px;color:var(--sub);opacity:.65;border-left-color:var(--sep)}
.p-zh-only{font-size:16.5px;line-height:1.8;color:var(--sub);margin:0 0 17px}
.p-en-only{font-size:16.5px;line-height:1.8;color:var(--text);margin:0 0 15px}
.p-none{text-align:center;padding:30px 12px;color:var(--sub);font-size:14px}

/* 可点单词：平时看不出来，点下去才高亮，不打扰阅读 */
.w{border-radius:3px;transition:background .1s}
.w:active{background:color-mix(in srgb, var(--accent) 22%, transparent)}
.w.on{background:color-mix(in srgb, var(--accent) 22%, transparent)}

/* 查词浮窗 */
.dictpop{
  position:fixed;z-index:99;max-width:min(300px, calc(100vw - 24px));
  background:var(--card);border:.5px solid var(--sep);border-radius:13px;
  padding:11px 13px;box-shadow:0 8px 28px rgba(0,0,0,.18);
  opacity:0;visibility:hidden;transform:translateY(4px);
  transition:opacity .13s ease, transform .13s ease;
  pointer-events:none;
}
.dictpop.on{opacity:1;visibility:visible;transform:none;pointer-events:auto}
.dp-w{display:flex;align-items:baseline;gap:7px;margin-bottom:6px}
.dp-w b{font-size:16.5px;color:var(--text);font-weight:700}
.dp-ph{font-size:12.5px;color:var(--sub)}
.dp-play{margin-left:auto;font-size:15px;cursor:pointer;opacity:.75;flex:none}
.dp-play:active{opacity:1}
.dp-pos{display:inline-block;font-size:10.5px;color:var(--sub);font-style:italic;
  margin-bottom:3px;opacity:.85}
.dp-zh{font-size:14.5px;line-height:1.5;color:var(--text);font-weight:600}
.dp-en{font-size:12.5px;line-height:1.5;color:var(--sub);margin-top:4px}
.dp-load,.dp-none{font-size:13px;color:var(--sub);opacity:.75}

.dorig{background:var(--card);border:.5px solid var(--sep);border-radius:12px;padding:14px}
.dentitle{margin:0 0 8px;font-size:16px;line-height:1.45;font-weight:600}
.densum{margin:0;font-size:14px;line-height:1.7;color:var(--text)}
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

/* 同一事件的其他家报道 */
.dalso{margin-top:18px;padding-top:14px;border-top:.5px solid var(--sep)}
.dalso-h{font-size:12.5px;font-weight:600;color:var(--sub);margin-bottom:8px}
.dalso-i{display:flex;align-items:baseline;gap:8px;padding:9px 11px;margin-bottom:6px;
  border-radius:10px;background:var(--card);border:.5px solid var(--sep);
  text-decoration:none;color:var(--text)}
.dalso-i:active{opacity:.7}
.dalso-s{flex:none;font-size:11px;font-weight:700;color:var(--accent)}
.dalso-t{flex:1;min-width:0;font-size:13.5px;line-height:1.45}
.dalso-g{flex:none;font-size:12px;color:var(--sub);opacity:.6}

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

DICT_WORDS = {}          # 构建时生成，供页面点词查义

DICT_JS = r"""
/* ================= 点词查义 ================= */
/* 设计要点：
   1) 详情页英文段落按词包成 <span class="w">，点一下就能查；
   2) 释义先查页面内置词表（构建时预生成，零延迟、断网可用），
      查不到才现场联网，结果存 localStorage，下次秒出；
   3) 发音优先用 dictionaryapi.dev 的真人 MP3，拿不到就用手机自带 TTS；
   4) 浮窗贴在点击位置旁边，上方放不下自动翻到下方，点空白处关掉。 */

var DICT = window.__DICT__ || {};          // 构建时预生成的词表
var DK = 'newsdict.v2';
var dmem = null;

function dstore(){
  if (dmem) return dmem;
  try { dmem = JSON.parse(localStorage.getItem(DK) || '{}'); }
  catch (e) { dmem = {}; }
  return dmem;
}
function dsaveDesc(){
  try { localStorage.setItem(DK, JSON.stringify(dmem || {})); } catch (e) {}
}

/* 词形还原：running / studies / voters / delayed 之类还原成基础形 */
function lemma(w){
  var x = (w || '').toLowerCase();
  if (x.length <= 3) return x;
  var rules = [
    [/ies$/, 'y'], [/ves$/, 'f'],
    [/([^aeiou])es$/, '$1'], [/([^aeiou])s$/, '$1'],
    /* 先试去掉 e（voter→vote、maker→make），再去掉整个 er，避免 voter→vot */
    [/([^aeiou])er$/, '$1e'], [/([^aeiou])er$/, '$1'],
    [/([^aeiou])est$/, '$1e'], [/([^aeiou])est$/, '$1'],
    [/ing$/, ''], [/ing$/, 'e'],
    [/ed$/, ''], [/ed$/, 'e'],
    [/ly$/, '']
  ];
  for (var i = 0; i < rules.length; i++){
    if (rules[i][0].test(x)) {
      var y = x.replace(rules[i][0], rules[i][1]);
      if (y.length >= 3 && y !== x) return y;
    }
  }
  return x;
}

/* 候选词：原词 → 还原形 → 去复数，按顺序试 */
function candidates(w){
  var x = (w || '').toLowerCase().replace(/^[^a-z]+|[^a-z]+$/g, '');
  if (!x) return [];
  var out = [x];
  /* 反复还原两轮：voters → voter → vote */
  var cur = x;
  for (var k = 0; k < 2; k++){
    var nxt = lemma(cur);
    if (nxt === cur) break;
    out.push(nxt);
    cur = nxt;
  }
  if (/[^s]s$/.test(x)) out.push(x.slice(0, -1));
  return out.filter(function(v, i, a){ return v && v.length >= 3 && a.indexOf(v) === i; });
}

var POP = document.createElement('div');
POP.className = 'dictpop';
POP.setAttribute('aria-hidden', 'true');
document.body.appendChild(POP);
var popOpen = false;
var AUD = document.createElement('audio');
AUD.preload = 'none';

function popHide(){
  popOpen = false;
  POP.classList.remove('on');
  POP.setAttribute('aria-hidden', 'true');
  var on = document.querySelectorAll('.w.on');
  for (var i = 0; i < on.length; i++) on[i].classList.remove('on');
}

function place(el){
  var r = el.getBoundingClientRect();
  var vw = window.innerWidth, vh = window.innerHeight;
  POP.style.visibility = 'hidden';
  POP.classList.add('on');
  var pw = POP.offsetWidth, ph = POP.offsetHeight;
  var top = r.top - ph - 9;
  if (top < 8) top = r.bottom + 9;                 // 上面放不下就翻到下面
  var left = r.left + r.width / 2 - pw / 2;
  left = Math.max(10, Math.min(left, vw - pw - 10));
  top = Math.max(8, Math.min(top, vh - ph - 10));
  POP.style.top = top + 'px';
  POP.style.left = left + 'px';
  POP.style.visibility = '';
}

function tts(text){
  if (!('speechSynthesis' in window)) return;
  try {
    speechSynthesis.cancel();
    var u = new SpeechSynthesisUtterance(text);
    u.lang = 'en-US';
    u.rate = 0.9;
    speechSynthesis.speak(u);
  } catch (e) {}
}

function speak(text, audioUrl){
  if (audioUrl) {
    try {
      AUD.src = audioUrl;
      var p = AUD.play();
      if (p && p.catch) { p.catch(function(){ tts(text); }); }
      return;
    } catch (e) {}
  }
  tts(text);
}

/* data: {zh, en, ph, pos, au}；loading 为真时先占位 */
function popShow(el, word, data, loading){
  var h = '<div class="dp-w"><b>' + esc(word) + '</b>';
  if (data && data.ph) h += '<span class="dp-ph">' + esc(data.ph) + '</span>';
  h += '<span class="dp-play">🔊</span></div>';

  if (loading) {
    h += '<div class="dp-load">查询中…</div>';
  } else if (data && (data.zh || data.en)) {
    if (data.pos) h += '<div class="dp-pos">' + esc(data.pos) + '</div>';
    if (data.zh) h += '<div class="dp-zh">' + esc(data.zh) + '</div>';
    if (data.en) h += '<div class="dp-en">' + esc(data.en) + '</div>';
  } else {
    h += '<div class="dp-none">没查到释义，点 🔊 仍可朗读</div>';
  }
  POP.innerHTML = h;
  popOpen = true;
  el.classList.add('on');
  place(el);
}

/* 一次查词的完整流程 */
function lookup(word, el){
  var cands = candidates(word), i, key;

  /* ① 内置词表（最快，断网也有） */
  for (i = 0; i < cands.length; i++){
    key = cands[i];
    if (DICT[key]) {
      var z = DICT[key];
      var d0 = { zh: typeof z === 'string' ? z : z[0], ph: '', en: '', au: '', pos: '' };
      popShow(el, word, d0, false);
      speak(key, '');
      return;
    }
  }

  /* ② 之前查过，本地有记录 */
  var mem = dstore();
  for (i = 0; i < cands.length; i++){
    key = cands[i];
    if (mem[key]) {
      popShow(el, word, mem[key], false);
      speak(key, mem[key].au || '');
      return;
    }
  }

  /* ③ 现场联网：先拿音标/英文释义/真人发音 */
  popShow(el, word, null, true);
  key = cands[0];
  var url = 'https://api.dictionaryapi.dev/api/v2/entries/en/' + encodeURIComponent(key);
  fetch(url).then(function(r){ return r.ok ? r.json() : null; }).then(function(j){
    var rec = { zh: '', en: '', ph: '', au: '', pos: '' };
    if (j && j[0]) {
      var e = j[0];
      rec.ph = e.phonetic || '';
      var phs = e.phonetics || [];
      for (var t = 0; t < phs.length; t++){
        if (!rec.ph && phs[t].text) rec.ph = phs[t].text;
        if (!rec.au && phs[t].audio) rec.au = phs[t].audio;
      }
      var m0 = e.meanings && e.meanings[0];
      if (m0) {
        rec.pos = m0.partOfSpeech || '';
        if (m0.definitions && m0.definitions[0]) {
          rec.en = m0.definitions[0].definition || '';
        }
      }
    }
    return rec;
  }).catch(function(){
    return { zh: '', en: '', ph: '', au: '', pos: '' };
  }).then(function(rec){
    if (rec.zh || rec.en) { mem[key] = rec; dsaveDesc(); }
    if (popOpen) popShow(el, word, rec, false);
    speak(key, rec.au || '');
  });
}

/* 把英文段落切成可点的词（保留原有标点和空格） */
function wrapWords(text){
  return String(text == null ? '' : text).replace(
    /([A-Za-z][A-Za-z'\-]*)/g,
    function(m){ return '<span class="w">' + esc(m) + '</span>'; }
  );
}

/* 点词 */
document.addEventListener('click', function(e){
  var t = e.target;
  var w = (t && t.classList && t.classList.contains('w')) ? t : null;
  if (w) {
    e.preventDefault();
    e.stopPropagation();
    if (popOpen && w.classList.contains('on')) { popHide(); return; }
    var on = document.querySelectorAll('.w.on');
    for (var i = 0; i < on.length; i++) on[i].classList.remove('on');
    lookup(w.textContent, w);
    return;
  }
  /* 点浮窗上的小喇叭重听 */
  if (popOpen && POP.contains(t)) {
    if (t.classList && t.classList.contains('dp-play')) {
      var cur = POP.querySelector('.dp-w b');
      if (cur) speak(cur.textContent, AUD.src || '');
      e.stopPropagation();
    }
    return;
  }
  if (popOpen) popHide();
}, true);

/* 滑动 / 转屏时收起，免得挡住正文 */
if (dscroll) dscroll.addEventListener('scroll', function(){ if (popOpen) popHide(); }, {passive:true});
window.addEventListener('resize', function(){ if (popOpen) popHide(); });
window.addEventListener('orientationchange', function(){ if (popOpen) popHide(); });
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
      html = en.map(function(t){ return '<p class="p-en-only">' + wrapWords(t) + '</p>'; }).join('');
    } else if (d.ensum) {
      html = '<p class="p-en-only">' + wrapWords(d.ensum) + '</p>';
    } else {
      html = '<div class="p-none">暂无正文</div>';
    }
  } else {
    if (en.length) {
      for (var i = 0; i < en.length; i++) {
        var z = (i < zh.length && zh[i]) ? zh[i] : '';
        html += '<div class="para">'
             +  '<div class="p-en">' + wrapWords(en[i]) + '</div>'
             +  (z ? '<div class="p-zh">' + esc(z) + '</div>'
                   : '<div class="p-zh missing">（本段未翻译）</div>')
             +  '</div>';
      }
    } else if (d.ensum) {
      html = '<div class="para"><div class="p-en">' + wrapWords(d.ensum) + '</div>'
           + '<div class="p-zh">' + esc(d.sum || '') + '</div></div>';
    } else {
      html = '<div class="p-none">暂无正文，点下方「阅读原文」看完整报道</div>';
    }
  }
  box.innerHTML = html;
}

/* 同一事件的其他家报道：列出来，可点开原文对照 */
function renderAlso(d){
  var box = $('d-also');
  var also = d.also || [];
  if (!also.length) { box.innerHTML = ''; box.style.display = 'none'; return; }
  box.style.display = '';
  var html = '<div class="dalso-h">这一事件还有 ' + also.length + ' 家在报道</div>';
  also.forEach(function(a){
    html += '<a class="dalso-i" href="' + esc(a.link) + '" target="_blank" rel="noopener">'
         +  '<span class="dalso-s">' + esc(a.src) + '</span>'
         +  '<span class="dalso-t">' + esc(a.en) + '</span>'
         +  '<span class="dalso-g">↗</span>'
         +  '</a>';
  });
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
  renderAlso(d);

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
            also    = it.get("also_list") or []
            # 「N 家报道」标记：点进去能切换着看，避免同一件事铺满整个列表
            also_html = (f'<span class="also">+{len(also)} 家报道</span>'
                         if also else '')

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
                # 同一事件还有哪些家在报：[[来源, 标题, 链接], ...]
                "also":   it.get("also_list") or [],
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
      {also_html}
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

    # 点词查义：预置词表 + 查词逻辑一起塞进页面
    dict_payload = json.dumps(DICT_WORDS, ensure_ascii=False, separators=(",", ":"))
    dict_payload = dict_payload.replace("</", "<\\/")
    js += ("\nwindow.__DICT__ = " + dict_payload + ";\n" + DICT_JS)

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
<link rel="apple-touch-startup-image" media="(device-width: 430px) and (device-height: 932px) and (-webkit-device-pixel-ratio: 3)" href="splash-1290x2796.png">
<link rel="apple-touch-startup-image" media="(device-width: 393px) and (device-height: 852px) and (-webkit-device-pixel-ratio: 3)" href="splash-1179x2556.png">
<link rel="apple-touch-startup-image" media="(device-width: 390px) and (device-height: 844px) and (-webkit-device-pixel-ratio: 3)" href="splash-1170x2532.png">
<link rel="apple-touch-startup-image" media="(device-width: 414px) and (device-height: 896px) and (-webkit-device-pixel-ratio: 2)" href="splash-828x1792.png">
<link rel="apple-touch-startup-image" media="(device-width: 375px) and (device-height: 667px) and (-webkit-device-pixel-ratio: 2)" href="splash-750x1334.png">
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
/* 「+N 家报道」：同一事件被多家媒体同时报道 */
.also{{font-size:10.5px;font-weight:600;color:var(--sub);background:var(--bg);
  border:.5px solid var(--sep);border-radius:5px;padding:1px 5px;flex:none;
  font-variant-numeric:tabular-nums}}
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

      <div class="dalso" id="d-also"></div>

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
<script>
if ('serviceWorker' in navigator) {{
  window.addEventListener('load', function() {{
    navigator.serviceWorker.register('./sw.js').catch(function() {{}});
  }});
}}
</script>
</body>
</html>"""


# ---- PWA 素材（PNG 的 base64，运行时直接解码写盘，无需 Pillow）----
ICON_1024 = "iVBORw0KGgoAAAANSUhEUgAABAAAAAQACAIAAADwf7zUAAAaD0lEQVR4nO3dTW4kyXmA4WgrMAOIO/sMvpikG0ha9VZcWUewL2brCLZ3vRhAQGoxAAENetjsZFUGo97nuUAHmlWZ31tf/Xz67V+OAQAANMzjEAAAAFDxL6sPAAAAXEcAAABAiAAAAIAQAQAAACECAAAAQgQAAACECAAAAAgRAAAAECIAAAAgRAAAAECIAAAAgBABAAAAIQIAAABCBAAAAIQIAAAACBEAAAAQIgAAACBkjnGsPgMAAHARGwAAAAiZFgAAANBhAwAAACECAAAAQgQAAACECAAAAAgRAAAAECIAAAAgRAAAAECIAAAAgBABAAAAIQIAAABCBAAAAIQIAAAACBEAAAAQIgAAACBEAAAAQIgAAACAkDnGsfoMAADARWwAAAAgRAAAAECIAAAAgJDpIwAAANBhAwAAACECAAAAQgQAAACECAAAAAgRAAAAECIAAAAgRAAAAECIAAAAgBABAAAAIQIAAABCBAAAAIQIAAAACBEAAAAQIgAAACBkjnGsPgMAAHARGwAAAAgRAAAAECIAAAAgRAAAAECIAAAAgJDpS4AAAKDDBgAAAEIEAAAAhAgAAAAIEQAAABAiAAAAIEQAAABAiAAAAIAQAQAAACECAAAAQgQAAACECAAAAAgRAAAAEDLHOFafAQAAuIgNAAAAhAgAAAAIEQAAABAiAAAAIEQAAABAiAAAAIAQAQAAACHTzwAAAECHDQAAAIQIAAAACBEAAAAQIgAAACBEAAAAQIgAAACAEAEAAAAhAgAAAEIEAAAAhAgAAAAImWMcq88AAABcxAYAAABCBAAAAIQIAAAACBEAAAAQIgAAACBEAAAAQIgAAACAkOlXAAAAoMMGAAAAQqYfAgYAgA4bAAAACBEAAAAQIgAAACBEAAAAQIgAAACAEAEAAAAhAgAAAEIEAAAAhMzhl8AAACDDBgAAAEIEAAAAhAgAAAAIEQAAABAiAAAAIEQAAABAiAAAAIAQAQAAACECAAAAQgQAAACECAAAAAiZ41h9BAAA4Co2AAAAECIAAAAgRAAAAECIAAAAgBABAAAAIQIAAABC5vA9oAAAkGEDAAAAIQIAAABCBAAAAIQIAAAACBEAAAAQIgAAACBEAAAAQIgAAACAEAEAAAAhAgAAAEIEAAAAhAgAAAAIEQAAABAyx7H6CAAAwFVsAAAAIEQAAABAiAAAAIAQAQAAACFz+BQwAABk2AAAAECIAAAAgBABAAAAIQIAAABCBAAAAIQIAAAACBEAAAAQIgAAACBEAAAAQIgAAACAEAEAAAAhAgAAAEIEAAAAhAgAAAAIEQAAABAyx7H6CAAAwFVsAAAAIEQAAABAyBzeAwQAABk2AAAAECIAAAAgRAAAAECIAAAAgBABAAAAIQIAAABCBAAAAIQIAAAACBEAAAAQIgAAACBEAAAAQIgAAACAEAEAAAAhAgAAAEIEAAAAhAgAAAAImeNYfQQAAOAqNgAAABAyhxUAAABk2AAAAECIAAAAgBABAAAAIQIAAABCBAAAAIQIAAAACBEAAAAQIgAAACBEAAAAQIgAAACAEAEAAAAhAgAAAEIEAAAAhAgAAAAImcfqEwAAAJexAQAAgBABAAAAIQIAAABC5jh8CgAAACpsAAAAIEQAAABAiAAAAIAQAQAAACECAAAAQgQAAACECAAAAAgRAAAAECIAAAAgRAAAAECIAAAAgBABAAAAIQIAAABCBAAAAIQIAAAACBEAAAAQIgAAACBEAAAAQMgc41h9BgAA4CI2AAAAEDItAAAAoMMGAAAAQgQAAACECAAAAAgRAAAAECIAAAAgRAAAAECIAAAAgBABAAAAIQIAAABCBAAAAIQIAAAACBEAAAAQIgAAACBEAAAAQIgAAACAEAEAAAAhc4xj9RkAAICL2AAAAECIAAAAgBABAAAAIdNHAAAAoMMGAAAAQgQAAACECAAAAAgRAAAAECIAAAAgRAAAAECIAAAAgBABAAAAIQIAAABCBAAAAIQIAAAACBEAAAAQIgAAACBEAAAAQMgc41h9BgAA4CI2AAAAECIAAAAgRAAAAECIAAAAgBABAAAAIdOXAAEAQIcNAAAAhAgAAAAIEQAAABAiAAAAIEQAAABAiAAAAIAQAQAAACECAAAAQgQAAACECAAAAAgRAAAAECIAAAAgZI5xrD4DAABwERsAAAAIEQAAABAiAAAAIEQAAABAiAAAAIAQAQAAACECAAAAQqafAQAAgA4bAAAACBEAAAAQIgAAACBEAAAAQIgAAACAEAEAAAAhAgAAAEIEAAAAhAgAAAAIEQAAABAyxzhWnwEAALiIDQAAAIQIAAAACJneAAQAAB02AAAAECIAAAAgRAAAAECIAAAAgBABAAAAIQIAAABCph8CBgCADhsAAAAIEQAAABAiAAAAIEQAAABAiAAAAIAQAQAAACECAAAAQgQAAACEzOGXwAAAIMMGAAAAQgQAAACECAAAAAgRAAAAECIAAAAgZK4+ANT99B//uvoIAFf78c//v/oI0PXphz/93+ozQI6hH+CFGICLCQC4lNEf4KtkAFxGAMBFjP4A3yQD4AICAO7O6A/wXWQA3JVvAYL7Mv0DfC9XTrirTz/80QYA7uWnv7qHAZz045/sAeAuBADchdEf4CZkANyctwDB7Zn+AW7FFRVuTgDAjblXAdyW6yrclgAAAIAQAQC35GUqgHtwdYUbEgBwM+5PAPfjGgu38umHP/7v6jPAI/jpr/+2+ggAj+9HP2AK72YDAAAAIQIAbsDL/wDXcL2F9xMAAAAQIgDgvbwcBXAlV114JwEAAAAhAgDexQtRANdz7YX3EAAAABAiAAAAIEQAAABAiACA87wJFWAVV2A4TQAAAECIAAAAgJA5jtVHAAA4wQwDp9gAAABAiAAAAIAQAQAAACECAAAAQqZP0AAAezLDwBk2AAAAECIAAAAgRAAAAECIAAAAgBABAAAAIQIAAABC5uoDAO/19Lz6BPCdvnxe9k97vtzEwr8g8H4CADZmlGFTPz90Lx4iPV9uaMlfELgVAQBbMsrwAC4bIj1f7kQGwKZ8BgD2Y5rhkdz78ez5cm/+h2E7AgAAAEIEAGzGi208nvs9qj1fruH/GfYiAAAAIEQAwE68zMajusdj2/PlSv63YSMCAAAAQuY4Vh8BAOAEMwycYgMAAAAhAgAAAEKm/RkAsCczDJxhAwAAACECAAAAQgQAAACECAAAAAgRAAAAECIAAAAgRAAAAECIAAAAgBABAAAAIQIAAABCBAAAAIQIAAAACBEAAAAQIgAAACBEAAAAQIgAAACAEAEAAAAhcxyrjwAAcIIZBk6Znj0AwJ7MMHCGtwABAECIAAAAgBABAAAAIQIAAABCBAAAAIQIAAAACBEAAAAQIgAAACBEAAAAQIgAAACAEAEAAAAh81h9AgCAE8wwcI4NAAAAhAgAAAAIEQAAABAiAAAAIEQAAABAiAAAAICQ6Uu0AIA9mWHgjOm5AwBsyQwDp3gLEAAAhAgAAAAIEQAAABAiAAAAIEQAAABAiAAAAIAQAQAAACECAHby5fPqE8B9eGwDXEYAAABAiACAzXihlMfjUQ1wJQEAwEqmf4CLCQDYj4GJh+HBDHA9AQBb+vLZ5MT2PIYBlpirDwCc9/P89PS8+hzwnYz+AAsJANieWQoAeDtvAQIAgBABAAAAIXOMY/UZAABOMMPAGTYAAAAQIgAAACBk2p4BAFsyw8ApNgAAABAiAAAAIEQAAABAiAAAAIAQAQAAACECAAAAQgQAAACECAAAAAgRAAAAECIAAAAgRAAAAECIAAAAgBABAAAAIQIAAABCBAAAAITMMY7VZwAAOMEMA2fYAAAAQIgAAACAEAEAAAAhAgAAAEKmz88AAFsyw8ApNgAAABAiAAAAIEQAAABAiAAA4DF9+bz6BCX+t2EjAgAAAEIEAAAPy8vS1/D/DHsRAADAeaZ/2I4AAOCRGU/vyn8v7EgAAPDgvnw2p96F/1XY1Fx9AAC4ws/T6tPz6nM8BKM/bE0AABBicgXwFiAAAAgRAAAAEDLHOFafAQDgBDMMnGEDAAAAIQIAAABCBAAAAIQIAAAACBEAAAAQIgAAACBk+gYtAGBLZhg4xQYAAABCBAAAAIQIAAAACBEAAAAQIgAAACBEAAAAQIgAAACAkLn6AMBtPD2vPgGwwpfPq08A7EYAwN7M/RD3chFQAsAbeQsQbMz0D7xwQQDeSADArtzsgV9wWQDeYo5xrD4D8N3c5oGvenpOvRfIDANn2ADAfkz/wCtcIoDXCQDYjFs78E0uFMArBAAAAIQIAAAACBEAsBNrfeCNXC6AXyMAAAAgRAAAAECIAAAAgJDpNzQAgC2ZYeAUGwAAAAgRALCTL59XnwDYhMsF8GsEAAAAhAgAAAAIEQCwGWt94JtcKIBXCADYj1s78AqXCOB1AgC25AYPfJWLA/BNAgB25TYP/ILLAvAWAgA25mYPvHBBAN5oHn5GD3b2cst/el56DmCR8txvhoFz5uoDALdRHgIAgLfzFiAAAAgRAAAAECIAAAAgRAAAAECIAAAAgBABAAAAIQIAAABCBAAAAIRMP6IHAGzJDAOn2AAAAECIAAAAgBABAAAAIQIAAABCBAAAAIQIAAAACBEAAAAQIgAAACBk+hUNAGBPZhg4wwYAAABCBAAAAIQIAAAACBEAAAAQIgAAACBEAAAAQIgAAACAEAEAAAAhAgAAAEIEAAAAhAgAAAAImeNYfQQAgBPMMHCKDQAAAIQIAAAACBEAAAAQIgAAACBEAAAAQIgAAACAkOk7tACAPZlh4AwbAAAACBEAAAAQIgAAACBEAAAAQMhcfQDgNp6eV58AWOHL59UnAHYjAGBv5n6Ie7kIKAHgjbwFCDZm+gdeuCAAbyQAYFdu9sAvuCwAbyEAYEtu88BXuTgA3yQAYD9u8MArXCKA1wkA2IxbO/BNLhTAKwQAAACECAAAAAiZ41h9BODNrPWBN3p6DvwygBkGTrEBAACAEAEAAAAhAgAAAEIEAAAAhEyfoAEA9mSGgTNsAGAnj/+dHsCNuFwAv0YAAABAiAAAAIAQAQCbsdYHvsmFAniFAID9uLUDr3CJAF4nAGBLbvDAV7k4AN8kAGBXbvPAL7gsAG8hAGBjbvbACxcE4I3m6gMA7/Jyy396XnoOYBFzP/C9BAA8CEMAAPAW3gIEAAAhAgAAAEIEAAAAhAgAAAAIEQAAABAyx7H6CAAAJ5hh4BQbAAAACBEAAAAQMu3PAIA9mWHgDBsAAAAIEQAAABAiAAAAIEQAAABAiAAAAIAQAQAAACECAAAAQgQAAACECAAAAAgRAAAAECIAAAAgRAAAAECIAAAAgBABAAAAIQIAAABCBAAAAIQIAAAACJnjWH0EAIATzDBwyvTsAQD2ZIaBM7wFCAAAQgQAAACECAAAAAgRAAAAECIAAAAgRAAAAEDI9AVaAMCOzDBwjg0AAACECAAAAAgRAAAAECIAAAAgRAAAAEDIXH0A4DaenlefgEf35fPqEwBwCwIAtmf05xo/P9JkAMDuBABszOjP9WQAwO58BgB2ZfpnIQ8/gH0JANiS8YvlPAgBNiUAYD8GLz4ID0WAHc0xjtVnAAA4wQwDZ0zPHdiL11z5UJ6efSCYdcwwcIq3AAEAQIgAgJ14+Z8PyMMSYC8CAAAAQgQAAACECAAAAAgRAAAAECIAAAAgRAAAAECIAICd+MUlPiAPS4C9CAAAAAgRALAZr7byoXhAAmxHAAAAQIgAgP14zZUPwkMRYEcCALZk8GI5D0KATQkAAL6b6R9gX3P1AYAznp5Xn4Aqoz/A7gQA7Mf0zxJGf4DHMMc4Vp8B+A6m/62ZoeGmzDBwhs8AAABAiAAAAICQaXsGG/H+n909PXsXENyOGQZOsQEAAIAQAQAAACECAAAAQgQAAACECAAAAAgRALATXyCzO39BAJYTAAAAECIAAAAgRADAZryHZF/+dgB8BAIA9mOO3JG/GgAfhACALZkm9+LvBcDHIQBgV2bKXfhLAfChCADYmMny4/M3AuCjmasPALzLy3z59Lz0HPwzcz8AH5YAgAdh4gQA3mKOcaw+AwDACWYYOMNnAAAAIEQAAABAiAAAAIAQAQAAACHT52cAgC2ZYeAUGwAAAAgRAAAAECIAAAAgRADAefMPf1t9BIAoV2A4TQAAAECIAAAAgBABAAAAIQIA3sWbUAGu59oL7yEAAAAgRADAe3khCuBKrrrwTgIAAABCBADcgJejAK7hegvvJwAAACDk029+/9+rzwAP4u//+e+rjwDwyOYf/mf1EeAR2ADAzbgzAdyPayzcigCAW3J/ArgHV1e4IQEAAAAhAgBuzMtUALflugq3JQDg9tyrAG7FFRVu7tNvfudbgOBe/v5fvhcI4KT5e6M/3IUNANyRuxfAOa6fcD8CAO7LPQzge7lywl15CxBcxNuBAL7J6A8XEABwKRkA8FVGf7iMAIAFZADAC6M/XEwAwGJiAAgy9MNCAgAAAEJ8CxAAAIQIAAAACBEAAAAQIgAAACBkjnGsPgMAAHARGwAAAAgRAAAAECIAAAAgRAAAAECIAAAAgBABAAAAIQIAAABCBAAAAIRMvwMGAAAdNgAAABAiAAAAIEQAAABAiAAAAIAQAQAAACECAAAAQqZvAQUAgA4bAAAACBEAAAAQMoefAgYAgAwbAAAACBEAAAAQIgAAACBEAAAAQIgAAACAEAEAAAAhAgAAAEIEAAAAhAgAAAAIEQAAABAyx7H6CAAAwFVsAAAAIEQAAABAiAAAAIAQAQAAACECAAAAQgQAAACECAAAAAiZww8BAABAhg0AAACECAAAAAgRAAAAECIAAAAgRAAAAECIAAAAgBABAAAAIQIAAABCBAAAAIQIAAAACBEAAAAQIgAAACBkjmP1EQAAgKvYAAAAQIgAAACAEAEAAAAhAgAAAEIEAAAAhMzha4AAACDDBgAAAEIEAAAAhAgAAAAIEQAAABAiAAAAIEQAAABAiAAAAIAQAQAAACECAAAAQgQAAACECAAAAAgRAAAAECIAAAAgRAAAAEDIHMfqIwAAAFexAQAAgBABAAAAIQIAAABC5vAhAAAAyLABAACAEAEAAAAhAgAAAEIEAAAAhAgAAAAIEQAAABAiAAAAIEQAAABAiAAAAIAQAQAAACECAAAAQgQAAACECAAAAAgRAAAAECIAAAAgRAAAAEDIHMfqIwAAAFexAQAAgJA5rAAAACDDBgAAAEIEAAAAhAgAAAAIEQAAABAiAAAAIEQAAABAiAAAAIAQAQAAACECAAAAQgQAAACECAAAAAgRAAAAECIAAAAgRAAAAECIAAAAgBABAAAAIQIAAABCBAAAAITMcRyrzwAAAFzEBgAAAEIEAAAAhAgAAAAImT4BAAAAHTYAAAAQIgAAACBEAAAAQIgAAACAEAEAAAAhAgAAAEIEAAAAhAgAAAAIEQAAABAiAAAAIEQAAABAiAAAAIAQAQAAACECAAAAQuYYx+ozAAAAF7EBAACAkGkBAAAAHTYAAAAQIgAAACBEAAAAQIgAAACAEAEAAAAhAgAAAEIEAAAAhAgAAAAIEQAAABAiAAAAIEQAAABAiAAAAIAQAQAAACECAAAAQgQAAACECAAAAAiZYxyrzwAAAFzEBgAAAEIEAAAAhEzvAAIAgA4bAAAACBEAAAAQIgAAACBEAAAAQIgAAACAEAEAAAAhAgAAAEIEAAAAhAgAAAAIEQAAABAiAAAAIEQAAABAiAAAAIAQAQAAACECAAAAQuYYx+ozAAAAF7EBAACAEAEAAAAhAgAAAEIEAAAAhEyfAQYAgA4bAAAACBEAAAAQIgAAACBEAAAAQIgAAACAEAEAAAAhAgAAAEIEAAAAhAgAAAAIEQAAABAiAAAAIEQAAABAiAAAAICQOcax+gwAAMBFbAAAACBEAAAAQIgAAACAEAEAAAAhAgAAAEIEAAAAhEzfAgoAAB02AAAAECIAAAAgRAAAAECIAAAAgBABAAAAIQIAAABCBAAAAIQIAAAACBEAAAAQIgAAACBEAAAAQMgc41h9BgAA4CI2AAAAECIAAAAgRAAAAECIAAAAgBABAAAAIQIAAABCBAAAAIQIAAAACJl+BwwAADpsAAAAIGRaAAAAQIcNAAAAhAgAAAAIEQAAABAiAAAAIEQAAABAiAAAAIAQAQAAACFz+ClgAADIsAEAAIAQAQAAACECAAAAQgQAAACECAAAAAgRAAAAECIAAAAgRAAAAECIAAAAgBABAAAAIXMcq48AAABcxQYAAABCBAAAAIQIAAAACBEAAAAQIgAAACBEAAAAQIgAAACAkDn8EAAAAGTYAAAAQIgAAACAEAEAAAAhAgAAAEIEAAAAhAgAAAAIEQAAABAiAAAAIEQAAABAiAAAAIAQAQAAACECAAAAQuY4Vh8BAAC4ig0AAACECAAAAAgRAAAAECIAAAAgRAAAAEDIHL4GCAAAMmwAAAAgRAAAAECIAAAAgBABAAAAIQIAAABCBAAAAIQIAAAACBEAAAAQIgAAACBEAAAAQIgAAACAEAEAAAAhAgAAAEIEAAAAhMxxrD4CAABwFRsAAAAIEQAAABAiAAAAIGQOHwIAAIAMGwAAAAgRAAAAECIAAAAgRAAAAECIAAAAgBABAAAAIQIAAABCBAAAAIQIAAAACBEAAAAQIgAAACBEAAAAQIgAAACAEAEAAAAhAgAAAEIEAAAAhMxxrD4CAABwFRsAAAAImcMKAAAAMmwAAAAgRAAAAECIAAAAgBABAAAAIQIAAABCBAAAAIQIAAAACBEAAAAQIgAAACBEAAAAQIgAAACAEAEAAAAhAgAAAEIEAAAAhAgAAAAIEQAAABAiAAAAIEQAAABAyD8AgH7IdIUsB0UAAAAASUVORK5CYII="
ICON_180 = "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"
SPLASH_1290 = "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"
SPLASH_1179 = "iVBORw0KGgoAAAANSUhEUgAABJsAAAn8CAIAAADKs3doAABF/UlEQVR4nOzdXayl+13Q8d+z9po557S0pwVpaC3lxWIVgVAKYjGEF6NUUiAaQ4LBYEJFoxIbveDCgMF46YVcKaiJQgxeKDGagDEklhcR0JgCiYAgUFpqxULpy3mbmb0eL+a0lNM53XvW7H3W8x0+n5xMzpm389trJrPXd37/53mWF/2DdQAAAAjar6uiAwAASNqdegAAAACOpOgAAACqFB0AAECVogMAAKhSdAAAAFWKDgAAoErRAQAAVCk6AACAKkUHAABQpegAAACqFB0AAECVogMAAKhSdAAAAFWKDgAAoErRAQAAVCk6AACAKkUHAABQpegAAACqFB0AAECVogMAAKhSdAAAAFWKDgAAoErRAQAAVCk6AACAKkUHAABQpegAAACqFB0AAECVogMAAKhSdAAAAFWKDgAAoErRAQAAVCk6AACAKkUHAABQpegAAACqFB0AAECVogMAAKhSdAAAAFWKDgAAoErRAQAAVCk6AACAKkUHAABQpegAAACqFB0AAECVogMAAKhSdAAAAFWKDgAAoGo/s556BgAAAI5hRwcAAFCl6AAAAKoUHQAAQJWiAwAAqNq7MQoAAECUHR0AAECVogMAAKhSdAAAAFWKDgAAoErRAQAAVCk6AACAKkUHAABQpegAAACqFB0AAECVogMAAKhSdAAAAFWKDgAAoErRAQAAVCk6AACAKkUHAABQpegAAACqFB0AAECVogMAAKhSdAAAAFWKDgAAoErRAQAAVCk6AACAKkUHAABQpegAAACqFB0AAECVogMAAKhSdAAAAFWKDgAAoErRAQAAVCk6AACAKkUHAABQpegAAACqFB0AAECVogMAAKhSdAAAAFWKDgAAoErRAQAAVCk6AACAKkUHAABQpegAAACqFB0AAEDVfmY99QwAAAAcw44OAACgStEBAABUKToAAIAqRQcAAFCl6AAAAKoUHQAAQJWiAwAAqFJ0AAAAVYoOAACgaj/rqUcAAADgKHZ0AAAAVYoOAACgStEBAABUKToAAIAqRQcAAFCl6AAAAKoUHQAAQJWiAwAAqFJ0AAAAVYoOAACgStEBAABUKToAAIAqRQcAAFCl6AAAAKoUHQAAQJWiAwAAqFJ0AAAAVYoOAACgStEBAABUKToAAIAqRQcAAFCl6AAAAKoUHQAAQJWiAwAAqFJ0AAAAVYoOAACgStEBAABUKToAAIAqRQcAAFCl6AAAAKoUHQAAQJWiAwAAqFJ0AAAAVYoOAACgStEBAABUKToAAICq/cx66hkAAAA4hh0dAABAlaIDAACoUnQAAABVig4AAKBK0QEAAFQpOgAAgCpFBwAAUKXoAAAAqhQdAABAlaIDAACoUnQAAABVig4AAKBK0QEAAFQpOgAAgKr9rKceAQAAgKPY0QEAAFQpOgAAgCpFBwAAUKXoAAAAqhQdAABAlaIDAACoUnQAAABVig4AAKBK0QEAAFQpOgAAgCpFBwAAUKXoAAAAqhQdAABAlaIDAACoUnQAAABVig4AAKBK0QEAAFQpOgAAgCpFBwAAUKXoAAAAqhQdAABAlaIDAACoUnQAAABVig4AAKBK0QEAAFQpOgAAgCpFBwAAUKXoAAAAqhQdAABAlaIDAACoUnQAAABVig4AAKBqP7OeegYAAACOYUcHAABQpegAAACqFB0AAECVogMAAKhSdAAAAFWKDgAAoErRAQAAVCk6AACAKkUHAABQpegAAACqFB0AAECVogMAAKhSdAAAAFWKDgAAoErRAQAAVCk6AACAKkUHAABQpegAAACq9rOeegQAAACOYkcHAABQpegAAACqFB0AAECVogMAAKhSdAAAAFWKDgAAoErRAQAAVCk6AACAKkUHAABQpegAAACqFB0AAECVogMAAKhSdAAAAFWKDgAAoErRAQAAVCk6AACAKkUHAABQpegAAACqFB0AAECVogMAAKhSdAAAAFWKDgAAoErRAQAAVCk6AACAKkUHAABQpegAAACqFB0AAEDVfmY99QwAAAAcw44OAACgStEBAABUKToAAIAqRQcAAFCl6AAAAKoUHQAAQJWiAwAAqFJ0AAAAVYoOAACgStEBAABUKToAAIAqRQcAAFCl6AAAAKoUHQAAQJWiAwAAqFJ0AAAAVYoOAACgStEBAABU7ddTTwAAAMBx7OgAAACqFB0AAECVogMAAKhSdAAAAFX7cWsUAACAJjs6AACAKkUHAABQpegAAACqFB0AAECVogMAAKhSdAAAAFWKDgAAoErRAQAAVCk6AACAKkUHAABQpegAAACqFB0AAECVogMAAKhSdAAAAFWKDgAAoErRAQAAVCk6AACAKkUHAABQpegAAACqFB0A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AECVogMAAKhSdAAAAFWKDgAAoErRAQAAVCk6AACAKkUHAABQpegAAACqFB0AAECVogMAAKhSdAAAAFWKDgAAoErRAQAAVCk6AACAKkUHAABQpegAAACqFB0AAECVogMAAKhSdAAAAFWKDgAAoErRAQAAVCk6AACAKkUHAABQpegAAACqFB0AAECVogMAAKhSdAAAAFWKDgAAoErRAQAAVCk6AACAKkUHAABQpegAAACq9jPrqWcAAADgGHZ0AAAAVYoOAACgStEBAABUKToAAIAqRQcAAFCl6AAAAKoUHQAAQNXe4+gAAACi7OgAAACqFB0AAECVogMAAKhSdAAAAFWKDgAAoErRAQAAVCk6AACAKkUHAABQpegAAACqFB0AAECVogMAAKhSdAAAAFWKDgAAoErRAQAAVCk6AACAKkUHAABQpegAAACqFB0AAECVogMAAKhSdAAAAFWKDgAAoErRAQAAVCk6AACAKkUHAABQpegAAACqFB0AAECVogMAAKhSdAAAAFWKDgAAoErRAQAAVCk6AACAKkUHAABQpegAAACqFB0AAECVogMAAKhSdAAAAFWKDgAAoErRwf9v545NAIBhIIi58P4rJzukCQfSBG6PBwMAQNXOnN83AAAA8MJGBwAAUKXoAAAAqhQdAABAlaIDAACoUnQAAABVig4AAKBK0QEAAFQpOgAAgCpFBwAAUKXoAAAAqhQdAABAlaIDAACoUnQAAABVO+f3CQAAADyx0QEAAFQpOgAAgCpFBwAAUKXoAAAAqhQdAABAlaIDAACoUnQAAABVig4AAKBK0QEAAFQpOgAAgCpFBwAAUKXoAAAAqhQdAABAlaIDAACoUnQAAABVig4AAKBK0QEAAFQpOgAAgCpFBwAAUKXoAAAAqhQdAABAlaIDAACoUnQAAABVig4AAKBK0QEAAFQpOgAAgCpFBwAAUKXoAAAAqhQdAABAlaIDAACoUnQAAABVig4AAKBqZ87vGwAAAHhhowMAAKhSdAAAAFWKDgAAoErRAQAAVCk6AACAKkUHAABQpegAAACqFB0AAECVogMAAKhSdAAAAFWKDgAAoErRAQAAVCk6AACAKkUHAABQpegAAACqFB0AAECVogMAAKhSdAAAAFU75/cJAAAAPLHRAQAAVCk6AACAKkUHAABQpegAAACqFB0AAECVogMAAKhSdAAA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"
SPLASH_1170 = "iVBORw0KGgoAAAANSUhEUgAABJIAAAnkCAIAAADZI9xUAABFNElEQVR4nOzdbcyl+V3Q8d917mtmd1vabalsaC3lwWIVgVAKYjGEB6NUUiAaQ4LBYEJFoxIbfcELAwbjS1/IKwU1UYjBF0qMJmAMieVBBDSmQCIgCJSWWrFQ+rBPM3OfyxezLaU9Z3Zn9p493zP9fLLZ7M7szP7uM3fmPt/5/a/rWl70D7YBAACgat022QYAANC1O/UAAAAA3IlsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkrTPbqWcAAADgKNs2AACANNkGAACQJtsAAADSZBsAAEDa6o4kAAAAZbZtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAaevMduoZAAAAOMq2DQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIG2d7dQjAAAAcJxtGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgbZ3ZTj0DAAAAR9m2AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAEDaOtupRwAAAOA42zYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkrTPbqWcAAADgKNs2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQts526hEAAAA4zrYNAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIC0dWY79QwAAAAcZdsGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADS1u3UEwAAAHAHtm0AAABpsg0AACBNtgEAAKTJNgAAgLR13JMEAAAgzLYNAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQNo6s516BgAAAI6ybQMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIG2d7dQjAAAAcJxtGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASFtntlPPAAAAwFG2bQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEhbZzv1CAAAABxn2wYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASFtntlPPAAAAwFG2bQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADS1tlOPQIAAADH2bYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAaet4cBsAAECYbRsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEhbZzv1CAAAABxn2wYAAJAm2wAAANJkGwAAQNo6Lm4DAAAIs20DAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgLR1O/UEAAAA3IFtGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANLWGQ/cBgAA6FpVGwAAQJlDkgAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQts5sp54BAACAo2zbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBtdSNJAACAMts2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApK0z26lnAAAA4CjbNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgLR1tlOPAAAAwHG2bQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACAtHVmO/UMAAAAHGXbBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBtne3UIwAAAHCcbRsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQts5sp54BAACAo2zbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIW2c79QgAAAAcZ9sGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQts5sp54BAACAo2zbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0tbt1BMAAABwB7ZtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANLW2U49AgAAAMfZtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABA2jqznXoGAAAAjrJtAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIC0dbZTjwAAAMBxtm0AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIW2e2U88AAADAUbZtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgbZ3t1CMAAABwnG0bAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABp68x26hkAAAA4yrYNAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQNo626lHAAAA4DjbNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBtHQ9uAwAACLNtAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgbZ3t1CMAAABwnG0bAABAmmwDAABIW8cpSQAAgDDbNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0tbt1BMAAABwB7ZtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgbZ3ZTj0DAAAAR9m2AQAApK2WbQAAAGW2bQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACAtHVmO/UMAAAAHGXbBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpqxtJAgAAlNm2AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGnrzHbqGQAAADjKtg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgLR1tlOPAAAAwHG2bQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIG099QAAPGN97FNPPQL8Prd+6z2nHgGAGds2AACAONkGAACQJtsAAADSXNsG0LGdegAAoEi2AWSoNgDgEIckAQAA0mQbAABAmkOSAB1OSQIAB8g2gAzVBgAc4pAkAABAmmwDAABIc0gSoMMpSQDgANs2AACANNs2gAzLNgDgENs2AACAtNWf7gJk+A2ZGp+TAAm2bQAAAGmubQPIsNgAAA6xbQMAAEizbQPosG4DAA6wbQMAAEizbQOosGsDAA6SbQAZug0AOES2AXToNgDgANe2AQAApMk2AACANIckATKckQQADrFtAwAASLNtA+iwbgMADrBtAwAASJNtAAAAaQ5JAmQ4IwkAHLJ6lwCQ4XdkYnxKAjQ4JAkAAJAm2wAAANJc2waQ4UAaAHCIbAPo0G0AwAEOSQIAAKTJNgAAgDSHJAEynJEEAA6RbQAdug0AOMAhSQAA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k20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBtne3UIwAAAHCcbRsAAEDaOtZtAAAAYbZtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgbd1OPQEAAAB3YNsGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAEDaOrOdegYAAACOsm0DAABIk20AAABpqzOSAAAAZbZtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkrTPbqWcAAADgKNs2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIG312DYAAIAy2zYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAaevMduoZAAAAOMq2DQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACAtHW2U48AAADAcbZtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgbZ3ZTj0DAAAAR9m2AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIG2d7dQjAAAAcJxtGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkrTPbqWcAAADgKNs2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEhbZzv1CAAAABxn2wYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgbd1OPQEAAAB3YNsGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQts5sp54BAACAo2zbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0tbZTj0CAAAAx9m2AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQts5sp54BAACAo2zbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgLR1tlOPAAAAwHG2bQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADS1pnt1DMAAABwlG0bAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBtne3UIwAAAHCcbRsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAEDaOrOdegYAAACOsm0DAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABA2jrbqUcAAADgONs2AACANNkGAACQJtsAAADSZBsAAECabAMAAEhbx60kAQAAwmzbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBtne3UIwAAAHDcOroNAAAgzCFJAACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApK0etg0AAFBm2wYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAaevMduoZAAAAOMq2DQAAIE22AQAApMk2AACAtNWlbQAAAGW2bQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIG2d2U49AwAAAEfZtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpq8e2AQAAlNm2AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEhbZ7ZTzwAAAMBRtm0AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgLR1tlOPAAAAwHG2bQAAAGmyDQAAIE22AQAApMk2AACAtP/fzh3aAADDQBALyP4rtxMU94A9Qejppcg2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAEDazpzfNwAAAPBkbQMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGk75/cJAAAAvFnbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABI25nz+wYAAACerG0AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0nbO7xMAAAB4s7YBAACkyTYAAIA02QYAAJAm2wAAANLWRxIAAIAyaxsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEjbmfP7BgAAAJ6sbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApO2c3ycAAADwZm0DAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpO3N+3wAAAMCTtQ0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpO+f3CQAAALxZ2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgbWfO7xsAAAB4srYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJkGwAAQJpsAwAASJNtAAAAabINAAAgTbYBAACkyTYAAIA02QYAAJAm2wAAANJ2zu8TAAAAeLO2AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApO3M+X0DAAAAT9Y2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACANNkGAACQJtsAAADSZBsAAECabAMAAEiTbQAAAGmyDQAAIE22AQAApMk2AACAtJ3z+wQAAADerG0AAABpsg0AACBNtgEAAKTJNgAAgLQdP0kAAADCrG0AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0mQbAABAmmwDAABIk20AAABpsg0AACBNtgEAAKTJNgAAgDTZBgAAkCbbAAAA0i4u+VdJLr0mlQAAAABJRU5ErkJggg=="
SPLASH_828 = "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AAgQbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAwLodewUAAFzJpA0AIEC0AQAEiDYAgADRBgAQINoAAALWGfePAgDs3arZAAD2z/YoAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAALWme3YawAA4AombQAAAaINACBAtAEABKyOtAEA7J9JGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBgndmOvQYAAK5g0gYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgYPXEDwCA/TNpAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIWGe2Y68BAIArmLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgIDVW6wAAPbPpA0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAtaZ7dhrAADgCiZtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAELDOduwlAABwFZM2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQsM5sx14DAABXMGkDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAELB64AcAwP6ZtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAgHW8xwoAYPdM2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABKwz27HXAADAFUzaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAetsx14CAABXMWkDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASs443xAAC7Z9IGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASssx17CQAAXMWkDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAAB63i6LgDA7pm0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgIB1tmMvAQCAq5i0AQAEiDYAgADRBgAQINoAAAJEGwBAwDpuHwUA2D2TNgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAAB62zHXgIAAFcxaQMACFjHqA0AYPdM2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIWLdjrwAAgCuZtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAtYZr4wHANg7kzYAgIDVoA0AYP9M2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAErDPbsdcAAMAVTNoAAAJEGwBAgGgDAAgQbQAAAav7EAAA9s+kDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBgPfYCgN+3vuyTjr0E+ANu//ZvHnsJwB0mbQAAAaINACBAtAEABDjTBruyHXsBAOyUaIM90WwAXML2KABAgGgDAAiwPQq7Yn8UgIuJNtgTzQbAJWyPAgAErH5rD8DlXCNgL2yPwq64QAJwMdujAAABJm2wJwZtAFzCpA0AIMCkDXbFqA2Ai5m0AQAEmLTBnhi0AXCJ1UUCgEu5RsBumLTBrrhCAnAxZ9oAAAJM2mBHzNkAuIxogz1RbQBcQrTBrqg2AC7mTBsAQIBoAwAIsD0Ke2J3FIBLmLQBAASYtMGuGLUBcDGTNgCAANEGABBgexT2xO4oAJcQbbArqg2Ai9keBQAIEG0AAAG2R2FP7I4CcAnRBrui2gC4mO1RAIAA0QYAEGB7FPbE7igAlxBtsCuqDYCL2R4FAAgQbQAAAavtGNgPPxvZH/9Vwl440wZ7srlAAnAx26MAAAGiDQAgQLQBAAQ40wZ74kwbAJcwaQMACBBtAAABog0AIMCZNtgVZ9oAuJhogz3RbABcYnWRAOBSrhGwG860AQAE2B6FXTHWAOBiog32RLMBcAnbowAAAaINACDA9ijsiv1RAC5m0gYAEGDSBnti0AbAJUzaAAACTNpgV4zaALiYaIMdkWwAXEa0wZ6oNgAuIdqA67HMnCwzy7HXsTOHbTYtDlwH0Qa7Ur28n54sT59tZ0/NHKpfwn2xzHJjuXljORyUG3CvRBtwT5aZZZkPPn548QtOXvfpp3/o+e5J/32PP7297ddvv/N3DuvNZT0RtMA9EW3A3TvfC33yqe0tf/axt3zJzU/5RMX2kX73ie27/8dT3/oDTzxxax5ZdRtw95Ybf+c3jr0G4I7l0ZvHXsJzs8zcur197ze84Gtf98g4v/VRlmVOlpmZ//XrZ2/8Z+97zxPbWuu27aknj70E4A6TNtiV0vX89GT54OOHb33T8772dY88fTbriRsRLrBtc+swn/eq03/5dc//mu96/+m6tH6Ugf2wlwHcjWWZp25tn/zy02/58sfOtrlxcmekxEdYlnnkdG4f5qs/65E//5k3nnrycOrXXeCu+MUDuBunyxye3v7ca288/9Flm1kU21W2bf7S5zwyZ2aRwF2yPQp7Eto322YO82mfeLKd/++LSmSfh7cumwhu2/V8/MtywYdxfo/tq19yMstsW+oHGtiN1S8esCepn4/bdvtw6dzosO10w/TChZ1/43Wt95KInbM7hduSWzA8tEzagLv3MSrnZJlf+t158vacLLu47C8zh21urvOaT/zIv7TNnCzze0/OO947pyd3udrlmVb7zJdd+rHsMmKBDNEGXKfzWdI2803/ab77bXffQPfDMnM4zDe8br7zTTPPnMM72+ZkmR/8xXnz98/vPDnLPSTmMnN2mC969fz7r52XPm9mc9QPuE5uRACu0+EwyzI/+vb5Vz81N9Y5Xfb1x3o6/+In58d+bZZlzg6znf8iuM23//C8873zyDon9/APP1nmsRvzwz8//+Zts9zZDAW4NiZtsCehR9Nu50f3L17wu5+Y5XTWZW7v6QvaZtbTWU7nd574/W9cltm2+cDTsz5yPR//yY159xOX/LXtmfsd9vSxABUmbcBduzQ91pNruxnzep135vpRv/KdLtd2r+vhon8+wL3zSwtw/XaYax/uo5d3vQve+ZcPRIk2AIAAZ9pgT0IjGgez7s7mowPukmiDXWldzFur3QO9Btw926MAAAEmbbAjoSHM9mF/5jkxbQPujkkbcP12/iKAnS8P4EImbbAnD8vDdW8dZlnmZJnDA17VVc7fCn/riMvycF3gbok24Dqdv23zk18422GeuDWny45CdFnm6adnm3nVi+78X4CQ1W/4gGt0ssy2zRe+ar7ty+ef/89jr+YP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tAEABIg2AIAA0QYAELCOOxEAAHbPpA0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAwDrbsZcAAMBVTNoAAALWMWoDANg9kzYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQMDqdfEAAPtn0gYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAwDqzHXsNAABcwaQNACBgNWgDANg/kzYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAevMduw1AABwBZM2AIAA0QYAECDaAAACRBsAQIBoAwAIWN08CgCwfyZtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIGCd2Y69BgAArmDSBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AICA1bN1AQD2z6QNACBAtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAevMduw1AABwBZM2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAlavHgUA2D+TNgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQMA6sx17DQAAXMGkDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAwDrbsZcAAMBVTNoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQMDqLVYAAPtn0gYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgYJ3x0A8AgL0zaQMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAHrbMdeAgAAVzFpAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgIB1Zjv2GgAAuIJJGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAErLMdewkAAFzFpA0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABKzjPVYAALtn0gYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAELDOduwlAABwFZM2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAHreOYHAMDumbQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAtbZjr0EAACuYtIGABAg2gAAAkQbAECAaAMACFjHnQgAALtn0gYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASssx17CQAAXGUd1QYAsHu2RwEAAkQbAECAaAMACBBtAAABog0AIGB17ygAwP6ZtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AICAdWY79hoAALiCSRsAQIBoAwAIWO2OAgDsn0kbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAErDPbsdcAAMAVTNoAAAJEGwBAgGgDAAgQbQAAAaINACBgdfMoAMD+mbQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAgHVmO/YaAAC4gkkbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAwOqFCAAA+2fSBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIGCd2Y69BgAArmDSBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIWL0vHgBg/0zaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAevMduw1AABwBZM2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIGCd7dhLAADgKiZtAAABog0AIGC1OwoAsH8mbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABCwzmzHXgMAAFcwaQMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAHrbMdeAgAAVzFpAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgIB1Zjv2GgAAuIJJGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgIB1tmMvAQCAq5i0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABKzjlQgAALtn0gYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAELDOduwlAABwFZM2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAHreOYHAMDumbQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAkQbAEDAOtuxlwAAwFVM2gAAAkQbAEDAOvZHAQB2z6QNACBAtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAgQLQBAASINgCAANEGABAg2gAAAkQbAECAaAMACBBtAAABog0AIEC0AQAEiDYAgADRBgAQINoAAAJEGwBAgGgDAAgQbQAAAaINACBAtAEABIg2AIAA0QYAECDaAAACRBsAQIBoAwAIEG0AAAGiDQAg4P8D2+NVfQQlGpQAAAAASUVORK5CYII="
SPLASH_750 = "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"

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
        {"src": "icon-1024.png", "sizes": "1024x1024", "type": "image/png", "purpose": "any maskable"},
    ],
}


# ---- Service Worker：缓存核心资源，离线也能看、二次打开秒开 ----
SW_JS = """var CACHE = 'news-v3';   /* 改版后升一位，老用户下次打开就能拿到新版 */
var CORE = ['./', './index.html', './manifest.json', './icon-180.png',
            './data.json', './us.json'];

self.addEventListener('install', function(e) {
  e.waitUntil(caches.open(CACHE).then(function(c) {
    // 逐个缓存，某个资源暂时取不到也不让整个安装失败
    return Promise.all(CORE.map(function(u) {
      return c.add(u).catch(function() {});
    }));
  }).then(function() { return self.skipWaiting(); }));
});

self.addEventListener('activate', function(e) {
  e.waitUntil(caches.keys().then(function(keys) {
    return Promise.all(keys.filter(function(k) { return k !== CACHE; })
      .map(function(k) { return caches.delete(k); }));
  }).then(function() { return self.clients.claim(); }));
});

// 缓存优先 + 后台更新：离线能看到上次的内容，有网时悄悄换新
self.addEventListener('fetch', function(e) {
  var req = e.request;
  if (req.method !== 'GET') return;
  var url = new URL(req.url);
  if (url.origin !== location.origin) return;   // 外域图片直接放行
  e.respondWith(
    caches.match(req).then(function(hit) {
      var net = fetch(req).then(function(resp) {
        if (resp && resp.status === 200) {
          var copy = resp.clone();
          caches.open(CACHE).then(function(c) { c.put(req, copy); });
        }
        return resp;
      }).catch(function() { return hit; });
      return hit || net;
    })
  );
});"""


def write_assets():
    """PWA 素材全部内嵌在脚本里，运行时解码写盘，无需额外文件。"""
    assets = {
        "icon-180.png": ICON_180,
        "icon-1024.png": ICON_1024,
        "splash-1290x2796.png": SPLASH_1290,
        "splash-1179x2556.png": SPLASH_1179,
        "splash-1170x2532.png": SPLASH_1170,
        "splash-828x1792.png": SPLASH_828,
        "splash-750x1334.png": SPLASH_750,
    }
    for name, b64str in assets.items():
        with open(os.path.join("docs", name), "wb") as f:
            f.write(base64.b64decode(b64str))
    with open(os.path.join("docs", "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(MANIFEST, f, ensure_ascii=False, indent=2)
    with open(os.path.join("docs", "sw.js"), "w", encoding="utf-8") as f:
        f.write(SW_JS)


# ============================================================
# 点词查义用的词表
# ============================================================
# 手机端点击英文单词要立刻出中文释义，但：
#   - 词典接口（dictionaryapi.dev）经常限流，且没有中文；
#   - 手机端现查现译要等一两秒，还依赖网络。
# 所以每天构建时，把当天正文里的高频词批量翻好，直接内嵌进网页。
# 这样点词是「零延迟」的，断网也能用；只有生僻词才现场联网兜底。

DICT_PATH  = os.path.join("docs", "dict_cache.json")
DICT_MAX   = 2600          # 词表最多带多少个词（控制网页体积）
DICT_MIN_N = 2             # 正文里至少出现几次才收进词表（太生僻的交给现场查）
DICT_STOP = set("""the a an and or but of to in on for at by from as is are was were be
been being it its this that these those will would could should can may might must have
has had do does did with about after before over under into out up down off than then so
if not no nor more most other some such only own same too very get got make made take
took go goes going say says said see seen know known come comes what who whom which when
where why how his her their our your my him them us they we you i he she one two three
first last new now way like just even still back again around because while also am are
was've they're we're don't didn't isn't wasn't there their theirs they'd they'll
mr mrs ms dr st no vs per via amid yet both each every either neither another much many
few own several such being doing having""".split())

_dict_run_cache = {}      # 本次运行内的临时缓存，避免同一个词翻两次


def _dict_words(items, max_n=DICT_MAX, min_n=DICT_MIN_N):
    """从所有新闻正文里挑出值得做词表的词，按出现次数降序。"""
    from collections import Counter
    cnt = Counter()
    for it in items:
        for p in (it.get("body_en") or []):
            for w in re.findall(r"[A-Za-z][A-Za-z'\-]+", p or ""):
                w = w.lower().strip("-'")
                if len(w) < 3 or len(w) > 18 or w in DICT_STOP:
                    continue
                if not re.match(r"^[a-z][a-z'\-]*$", w):
                    continue
                cnt[w] += 1
    return [w for w, n in cnt.most_common() if n >= min_n][:max_n]


def _dict_ask(words):
    """批量问中文释义。一次请求塞多个词（用换行分隔），省调用次数。"""
    if not words:
        return {}
    out = {}
    CHUNK = 40
    for i in range(0, len(words), CHUNK):
        part = words[i:i + CHUNK]
        joined = "\n".join(part)
        zh = _translate_google(joined, sl="en", timeout=15)
        if not zh:
            continue
        lines = [x.strip() for x in zh.split("\n") if x.strip()]
        # 行数对得上才敢按行对应，否则这一批作废（宁可没释义，也不要给错释义）
        if len(lines) != len(part):
            if len(part) == 1:
                out[part[0]] = lines[0]
            continue
        for w, line in zip(part, lines):
            line = re.sub(r"^[0-9]+[.、) ]*", "", line).strip()
            if line and has_cn(line):
                out[w] = line[:60]
    return out


def build_dict_cache(items):
    """生成/更新词表缓存。旧词保留，新词补上，最多 DICT_MAX 个。"""
    cache = {}
    if os.path.exists(DICT_PATH):
        try:
            with open(DICT_PATH, encoding="utf-8") as f:
                cache = json.load(f) or {}
        except Exception:
            cache = {}

    want = _dict_words(items)
    need = [w for w in want if w not in cache and w not in _dict_run_cache]
    if need:
        log(f"  生成点词词表：待查 {len(need)} 个新词")
        got = _dict_ask(need)
        for w, zh in got.items():
            cache[w] = zh
            _dict_run_cache[w] = zh
        log(f"  词表新增 {len(got)}/{len(need)}")

    # 只保留还有用的词，别让缓存无限长
    keep = [w for w in want if w in cache]
    cache = {w: cache[w] for w in keep}

    try:
        os.makedirs("docs", exist_ok=True)
        with open(DICT_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception as e:
        log(f"  词表写入失败: {e}")
    return cache


def prev_has_chinese():
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
            clusters = group_events(items)   # 同一事件的多家报道聚成一簇
            kept = []
            for c in clusters:
                head = c[0]
                # 同事件的其他报道：只带轻量信息，够在详情页里列出并跳转
                head["also_list"] = [{"src": x["source"], "en": x["title_en"],
                                      "link": x["link"], "t": fmt_time(x["pub_ts"])}
                                     for x in c[1:]]
                kept.append(head)
            n_dup = sum(len(c) - 1 for c in clusters)
            if n_dup:
                print(f"归并同事件 {n_dup} 条，"
                      f"其中 {sum(1 for c in clusters if len(c) > 1)} 组有多家报道")
            items = kept
            log("翻译中 ...")
            translate_items(items)
            align_bodies(items)         # 中文段落数不许超过英文，保证逐段对照不错位
        zh_total += sum(1 for it in items if has_cn(it.get("title_zh", "")))
        sections_out.append((sec, items))

    # 防退化：这次一条都没翻成功、而上一版是中文时，
    # 保留上一版网页不动，绝不把网站打回纯英文
    total_items = sum(len(items) for _, items in sections_out)
    if total_items and zh_total == 0 and prev_has_chinese():
        print("！本次翻译全部失败，保留上一版中文网页，不覆盖")
    else:
        # 点词查义用的词表：把当天正文的高频词提前翻好，点一下就出结果
        global DICT_WORDS
        all_items = [it for _, items in sections_out for it in items]
        try:
            DICT_WORDS = build_dict_cache(all_items)
            log(f"点词词表：{len(DICT_WORDS)} 个词")
        except Exception as e:
            print(f"(词表生成失败，页面仍可现场联网查词: {e})")
            DICT_WORDS = {}
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
