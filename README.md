# 民航资讯 · 每日自动聚合

自动从主流民航媒体抓取新闻，按热度排序，翻译成中文，每天定时更新。

## 本地运行

```bash
pip install feedparser
python3 aggregate.py
```

生成结果在 `docs/index.html`。

## GitHub Actions 自动部署

1. 把整个文件夹推到一个 GitHub 仓库
2. 仓库 Settings → Pages → Source 选 **GitHub Actions**
3. Actions 会每天定时跑，自动更新 `docs/index.html` 并发布

## 配置

编辑 `aggregate.py` 顶部的 `SOURCES` 区域即可增删新闻源。
