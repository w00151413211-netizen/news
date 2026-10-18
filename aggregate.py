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

SOURCES = [
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
HOT_WORDS = [
    "crash", "accident", "grounded", "crash-land", "emergency",
    "first flight", "delivery", "order", "orders", "launch",
    "bankrupt", "merger", "acquisition", "record", "banned",
    "delay", "delays", "strike", "fleet", "widebody",
]

MAX_ITEMS      = 40        # 最终保留多少条
TRANSLATE_TO   = "zh-CN"   # 目标语言
FETCH_PER_SRC  = 12        # 每个源最多取多少条
REQUEST_DELAY  = 0.4       # 抓取间隔（秒），对服务器友好


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
    """把摘要里的 HTML 标签剥掉，只留纯文本。"""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def collect():
    """从所有源抓取文章，去重，算热度分。"""
    seen = set()
    items = []

    for name, url, weight in SOURCES:
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

            # ---- 热度分 ----
            # 来源权重（0-10 分）
            source_score = weight * 2
            # 新鲜度：24 小时内满分，随时间指数衰减，72 小时后趋近 0
            age_hours = max(0, (time.time() - pub) / 3600)
            freshness = 6 * math.exp(-age_hours / 30)
            # 标题信号词
            t_lower = title.lower()
            signal = sum(2 for w in HOT_WORDS if w in t_lower)

            score = source_score + freshness + signal

            items.append({
                "title_en": title,
                "title_zh": "",
                "summary":  summary,
                "summary_zh": "",
                "link":     link,
                "source":   name,
                "pub_ts":   pub,
                "score":    round(score, 2),
            })

        time.sleep(REQUEST_DELAY)

    items.sort(key=lambda x: x["score"], reverse=True)
    return items[:MAX_ITEMS]


# ============================================================
# 2. 翻译
# ============================================================

def _translate_google(text, target="zh-CN"):
    """免费 Google 翻译接口（无需密钥）。失败返回 None。"""
    if not text:
        return ""
    q = urllib.parse.quote(text)
    url = (f"https://translate.googleapis.com/translate_a/single"
           f"?client=gtx&sl=auto&tl={target}&dt=t&q={q}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return "".join(seg[0] for seg in data[0] if seg[0])
    except Exception:
        return None


def _translate_github_ai(texts, target="zh-CN"):
    """用 GitHub 自带的免费 AI 接口批量翻译（GitHub Actions 里无需任何密钥）。

    一次请求翻完一批标题，速度快、质量也比普通机器翻译好得多。
    免费账号有调用额度限制，超了会自动回退到英文。
    """
    token = os.environ.get("GITHUB_TOKEN")
    if not token or not texts:
        return None

    # 把待翻译内容编号后一起发过去，要求按同样顺序返回
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))
    payload = {
        "model": "openai/gpt-4o-mini",
        "messages": [
            {"role": "system", "content":
             "你是专业民航新闻翻译。把英文新闻标题/摘要翻译成简体中文，"
             "要求：准确、简洁、符合中文新闻表达习惯；"
             "保留航空公司名、机型（如 Boeing 737 MAX）、机场代码等专有名词原文；"
             "只输出翻译结果，每行一条，格式为「序号. 译文」，不要任何解释。"},
            {"role": "user", "content": numbered}
        ],
        "temperature": 0.3,
    }
    try:
        req = urllib.request.Request(
            "https://models.github.ai/inference/v1/chat/completions",
            method="POST",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json",
                     "User-Agent": "codebuddy"},
            data=json.dumps(payload).encode("utf-8"))
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"  (AI 翻译不可用: {e})")
        return None

    # 解析返回：按「序号. 译文」逐行提取
    result = []
    for line in content.splitlines():
        line = line.strip()
        m = re.match(r"^\d+[\.、]\s*(.+)$", line)
        if m:
            result.append(m.group(1).strip())
    # 数量对不上就认为这次翻译不可靠，交给调用方回退
    return result if len(result) == len(texts) else None


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
        # MyMemory 失败时有时会原样返回，做一下校验
        return out if out and out != text else None
    except Exception:
        return None


def translate_items(items):
    """翻译标题和摘要。Google 为主，MyMemory 兜底。

    先探测一次主接口是否可用：不可用就换备用源，
    两个都不通就直接跳过整个翻译环节（保留英文），
    避免几十条新闻每条都等超时。任何单条失败也不影响其它条目。
    """
    # ① 优先用 GitHub 自带 AI：一次把全部标题翻完，质量最好
    titles = [it["title_en"] for it in items]
    ai_titles = _translate_github_ai(titles)
    if ai_titles:
        print("  AI 翻译标题完成")
        for it, zh in zip(items, ai_titles):
            it["title_zh"] = zh

        # 摘要也翻一批（截断长度，控制额度）
        summaries = [(it["summary"] or "")[:150] for it in items]
        ai_sums = _translate_github_ai(summaries)
        if ai_sums:
            print("  AI 翻译摘要完成")
            for it, zh in zip(items, ai_sums):
                it["summary_zh"] = zh
        else:
            for it in items:
                it["summary_zh"] = it["summary"]
        return items

    # ② AI 不可用，退回传统的免费翻译接口
    print("  AI 翻译不可用，改用免费翻译接口")
    use_google = _translate_google("test") is not None
    if not use_google:
        if _translate_mymemory("test") is None:
            print("  翻译接口均不可用，跳过翻译（保留英文原文）")
            # 关键：跳过时也要把中文字段填成英文，否则界面上标题会是空白
            for it in items:
                it["title_zh"] = it["title_en"]
                it["summary_zh"] = it["summary"]
            return items
        print("  Google 不可用，改用 MyMemory 翻译")

    def tr(text):
        """翻译一段文字，失败时重试，再失败返回 None（由调用方回退到英文）。"""
        out = _translate_google(text) if use_google else None
        if out:
            return out
        # MyMemory 免费额度有限，偶尔会 429 限流，重试两次
        for _ in range(2):
            out = _translate_mymemory(text)
            if out:
                return out
            time.sleep(1.5)
        return None

    for i, it in enumerate(items):
        # 标题
        t = tr(it["title_en"])
        it["title_zh"] = t if t else it["title_en"]
        # 摘要
        s = tr(it["summary"])
        it["summary_zh"] = s if s else it["summary"]
        time.sleep(0.25)   # 控制频率，防止被封
        if (i + 1) % 10 == 0:
            print(f"  已翻译 {i+1}/{len(items)}")
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
.dzh{margin:0;font-size:16.5px;line-height:1.78;color:var(--text)}

.dorig{background:var(--card);border:.5px solid var(--sep);border-radius:12px;padding:14px}
.dentitle{margin:0 0 8px;font-size:16px;line-height:1.45;font-weight:600}
.densum{margin:0;font-size:14px;line-height:1.65;color:var(--sub)}
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
var DATA = __DATA__;
var detail  = document.getElementById('detail');
var dscroll = document.getElementById('dscroll');
var list    = document.getElementById('scroll');
var listPos = 0, cur = -1;

function $(id){ return document.getElementById(id); }
function put(id, v){ $(id).textContent = (v == null ? '' : v); }

function render(i){
  var d = DATA[i];
  cur = i;
  put('d-title', d.zh || d.en);

  var hb = $('d-hot');
  if (d.hot) { hb.textContent = d.hot; hb.className = 'hot hot' + d.lv; hb.style.display = ''; }
  else { hb.style.display = 'none'; }

  put('d-src',  d.src);
  put('d-time', d.t);
  put('d-rank', (i + 1) + ' / ' + DATA.length);

  var zhSec = $('d-zh-sec');
  if (d.sum) { put('d-zh-body', d.sum); zhSec.style.display = ''; }
  else { zhSec.style.display = 'none'; }

  put('d-en-title', d.en);
  var enSum = $('d-en-sum');
  if (d.ensum) { put('d-en-sum', d.ensum); enSum.style.display = ''; }
  else { enSum.style.display = 'none'; }

  $('d-open').href = d.link;
  put('d-link', d.link);

  $('d-prev').disabled = (i <= 0);
  $('d-next').disabled = (i >= DATA.length - 1);
}

function route(){
  var m = /^#\\/n\\/(\\d+)$/.exec(location.hash || '');
  if (m) {
    var i = parseInt(m[1], 10);
    if (i >= 0 && i < DATA.length) {
      if (!detail.classList.contains('open')) listPos = list.scrollTop;
      render(i);
      detail.classList.add('open');
      detail.setAttribute('aria-hidden', 'false');
      dscroll.scrollTop = 0;
      if (document.title.indexOf('·') > 0) document.title = (DATA[i].zh || DATA[i].en).slice(0, 30);
      return;
    }
  }
  detail.classList.remove('open');
  detail.setAttribute('aria-hidden', 'true');
  list.scrollTop = listPos;
  document.title = '民航资讯 · 每日热点';
}

function go(i){
  if (i < 0 || i >= DATA.length) return;
  history.replaceState(null, '', '#/n/' + i);
  render(i);
  dscroll.scrollTop = 0;
}

/* 点卡片进入详情 */
document.getElementById('list').addEventListener('click', function(e){
  var c = e.target.closest ? e.target.closest('.card') : null;
  if (c) location.hash = '#/n/' + c.getAttribute('data-i');
});

$('d-back').onclick = function(){
  if (history.length > 1) history.back();
  else { location.hash = ''; route(); }
};
$('d-prev').onclick = function(){ go(cur - 1); };
$('d-next').onclick = function(){ go(cur + 1); };

$('d-copy').onclick = function(){
  var link = DATA[cur].link;
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
    navigator.share({ title: DATA[cur].zh || DATA[cur].en, url: DATA[cur].link }).catch(function(){});
  };
} else { shareBtn.style.display = 'none'; }

document.addEventListener('keydown', function(e){
  if (e.key === 'Escape' && detail.classList.contains('open')) $('d-back').click();
});

window.addEventListener('hashchange', route);
route();
"""


def build_html(items):
    now = datetime.now(tz=timezone(timedelta(hours=8)))
    updated = now.strftime("%Y年%m月%d日 %H:%M")

    data_js = []
    cards = []
    for idx, it in enumerate(items):
        label, level = hot_label(it["score"])
        label_html = (f'<span class="hot hot{level}">{label}</span>'
                      if label else '<span class="hot hot0"></span>')
        # 双保险：中文字段为空时一律回退到英文原文，界面上绝不会出现空白标题
        title   = it["title_zh"] or it["title_en"]
        summary = it["summary_zh"] or it["summary"]

        data_js.append({
            "zh":    it["title_zh"] or it["title_en"],
            "en":    it["title_en"],
            "sum":   it["summary_zh"] or it["summary"],
            "ensum": it["summary"],
            "src":   it["source"],
            "t":     fmt_time(it["pub_ts"]),
            "link":  it["link"],
            "hot":   label,
            "lv":    level,
        })

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
  <div class="go">›</div>
</article>""")

    cards_html = "\n".join(cards) if cards else \
        '<div class="empty">今天暂时没有抓取到新闻，明天再来。</div>'

    payload = json.dumps(data_js, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("</", "<\\/").replace("<!--", "<\\!--")
    js = DETAIL_JS.replace("__DATA__", payload)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#F6F7F9">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="民航资讯">
<title>民航资讯 · 每日热点</title>
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
  <h1>民航资讯</h1>
  <div class="sub">每日自动更新 · 更新于 {updated}</div>
</div>

<div class="scroll" id="scroll">
  <div class="filters" id="filters">
    <button class="f on" data-s="">全部</button>
  </div>
  <div id="list">
{cards_html}
  </div>
  <div class="footer">来源：{" / ".join(n for n,_,_ in SOURCES)}</div>
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

      <div class="dsec" id="d-zh-sec">
        <div class="dsec-h">中文翻译</div>
        <p class="dzh" id="d-zh-body"></p>
      </div>

      <div class="dsec">
        <div class="dsec-h">原文 <span class="en-tag">ENGLISH</span></div>
        <div class="dorig">
          <h2 class="dentitle" id="d-en-title"></h2>
          <p class="densum" id="d-en-sum"></p>
          <div class="dlink" id="d-link"></div>
        </div>
      </div>

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
// 来源筛选
(function(){{
  const srcs = [...new Set([...document.querySelectorAll('.card')].map(c => c.dataset.source))];
  const bar = document.getElementById('filters');
  srcs.forEach(s => {{
    const b = document.createElement('button');
    b.className = 'f'; b.textContent = s; b.dataset.s = s;
    b.onclick = () => {{
      document.querySelectorAll('.f').forEach(x => x.classList.toggle('on', x.dataset.s === s));
      document.querySelectorAll('.card').forEach(c => {{
        c.style.display = (c.dataset.source === s) ? 'flex' : 'none';
      }});
    }};
    bar.appendChild(b);
  }});
  bar.querySelector('[data-s=""]').onclick = () => {{
    document.querySelectorAll('.f').forEach(x => x.classList.toggle('on', !x.dataset.s));
    document.querySelectorAll('.card').forEach(c => c.style.display = 'flex');
  }};
}})();
</script>
<script>
{js}
</script>
</body>
</html>"""


ICON_B64 = "iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAYAAAA9zQYyAAADtElEQVR42u3dQXIaSRRF0cqM2gcsTl4VLE5aCR44HPZASAioIv/Lc4fdEW21OHr86rDptozS2/tlUd3OxzbCl9EAVhLwBrGScDeIlYS7gawk2A1kJcHuMOulPdlMA1lJa91hVtJad5iVhLqBrKQTpMOspLXuMCsJdYdZSai775aS6s/+CZFeudIdZiWh7jArCbUbWpPc0NZZBVe6w6wk1E4OhZ8c1lmFV9pCK3ihrbOKr7SFVvgNLUWAdm4o4Oyw0HJySGODdm4o5Oyw0Dt1OR18E5wcEtDDrrOV3gO0+1lBd7SF3vl2ttJODgnoEdfZSgMtAT3yOltpoCWgR15nAQ2+gIYUaPkBAFpwAi0/CEBDKaAFtPZeZwsPtAT0yLezlQZaAnrkRbXSQEtAj7ykVhpoCWgBLQEtAS0BLaAloCWgJaAloAW0BLQEtAS0BLSAloCWgJaAloAW0BLQEtAS0BLQAloCOr89Pnvucjr4jLs7asvb+8W3YRzIV1+oXx9eAKDrQwYb6EjIYAMdCRlsD4XRD2IeHC10LAprPTno1HWbGfaUoGd5m54RdofZM4GFBtliAw0y2ECDDPbcN7Tf1DP3D33MQkNsrSNAgwx2BGiQwY4ADTLYEaBBBvuWOsy69TWo8DoMvdAgW+yIhfbfkr1rRiw0xNY6AjTIYEeABhnsKW5o6d5W34L71mfrd5f/V847GdBRtyHcQMc8tX/3tQEOdCnA1hvoaMDWG+hoxIBPAtqnCM15nqwAw50EfIVYScBXgJV0nqwAK2m9V4iVtN4rwEpa7w6zkl5Tv31UUQEtoCWgJaAloAW0BLQEtG7LH6cCOg4b1EBvDmxvZFADHQfLp7ECHYkJaqDjAFlroCPRQA10HBRrDXQkDqiBjgNhrf8U8//6HgnyZ38M6XI6XP3re/z6s+TTR3dCdO3v+/RQoMsgfuSfBzfQJRF/9+uADXTUTWq1f/pQuCzLrA+GXwEZ/cGq8te+Wedjs9BFIVhuJ0fsmsH9rw6zf6c80Odj82al6vfz9AstJ4dUBLSzQ8XPDQstJ4dUB7SzQ4XPDQutCU4OK62i62yhNclDoZVWwXX+eqGhVjHMTg5NcnJYaRVc59sWGmoVwXz7yQG1CmB2Q2uyG9pKq9A6/3yhodbAmO87OaDWoJiX5e/nctzbxB/0qLEgP+eh0FprIMyPg4ZaA2F+/ORwgmgQyM9baGutgcxsB9Ba6wXjt/2igq0d38X3PRHghnjjXnfzwg1xFGjAAd6g36rs1XqMajeMAAAAAElFTkSuQmCC"

MANIFEST = {
    "name": "民航资讯 · 每日热点",
    "short_name": "民航资讯",
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


def main():
    os.makedirs("docs", exist_ok=True)

    items = collect()
    print(f"\n共采集 {len(items)} 条")

    if items:
        print("翻译中 ...")
        translate_items(items)

    out = build_html(items)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(out)

    write_assets()

    # 保存原始数据，方便以后做 API 或历史记录
    with open("docs/data.json", "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

    print(f"已生成 docs/index.html（{len(out)} 字符）")


if __name__ == "__main__":
    main()
