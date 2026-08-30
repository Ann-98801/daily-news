#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日早报数据抓取脚本
----------------------------------------
由 GitHub Actions 定时触发，在服务器端（而非用户浏览器）完成所有抓取，
避免了浏览器直接访问外站导致的跨域(CORS)限制。
抓取结果写入 docs/data.json，前端页面 docs/index.html 只负责读取展示。

数据源说明（务必知悉，方便以后维护）：
  - BBC / CNN：使用官方公开 RSS，最稳定，自带摘要文本。
  - Reuters（路透社）/ 新华社 / 人民日报：这几家没有稳定的官方公开 RSS，
    这里改用必应新闻搜索的 RSS 输出（site:域名 语法），是公开可访问的
    搜索结果 RSS，比直接爬网页结构稳定，但仍可能因必应调整而失效，
    出现问题时优先检查这里。
  - 微博热搜：使用微博前端页面自带的 ajax 接口（非官方开放API，
    可能随时变化）。如果该接口抓取失败，会自动尝试退回到
    s.weibo.com/top/summary 页面解析。

任何一个来源抓取失败都不会导致整体脚本崩溃：失败会被记录进
data.json 的 "errors" 字段，前端会提示"该来源暂时抓取失败"。
"""

import json
import re
import sys
import time
import html
from datetime import datetime, timezone, timedelta

import requests
import feedparser
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
TIMEOUT = 15
MAX_ITEMS_PER_SOURCE = 8
BEIJING_TZ = timezone(timedelta(hours=8))


def clean_text(raw, limit=140):
    """去掉HTML标签、多余空白，截断成摘要长度"""
    if not raw:
        return ""
    text = BeautifulSoup(html.unescape(raw), "html.parser").get_text(separator=" ")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def fetch_rss(url, source_name, limit=MAX_ITEMS_PER_SOURCE):
    """通用RSS抓取，返回 items 列表；失败抛异常交给上层记录"""
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    items = []
    for entry in feed.entries[:limit]:
        title = clean_text(entry.get("title", ""), limit=80)
        summary = clean_text(entry.get("summary", entry.get("description", "")), limit=140)
        link = entry.get("link", "")
        if not title or not link:
            continue
        items.append({"title": title, "summary": summary, "link": link, "source": source_name})
    return items


def fetch_bing_news(query, source_name, limit=MAX_ITEMS_PER_SOURCE, market="zh-CN"):
    """用必应新闻搜索的RSS输出，作为没有官方RSS的媒体的替代方案"""
    url = (
        "https://www.bing.com/news/search"
        f"?q={requests.utils.quote(query)}&format=RSS&setmkt={market}"
    )
    return fetch_rss(url, source_name, limit=limit)


def fetch_weibo_hot():
    """微博热搜榜。优先走ajax接口，失败则退回到网页表格解析"""
    try:
        url = "https://weibo.com/ajax/side/hotSearch"
        headers = dict(HEADERS)
        headers["Referer"] = "https://weibo.com/"
        resp = requests.get(url, headers=headers, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        raw_list = data.get("data", {}).get("realtime", [])
        items = []
        for i, entry in enumerate(raw_list[:20], start=1):
            word = entry.get("word") or entry.get("word_scheme", "")
            word = word.replace("#", "").strip()
            if not word:
                continue
            hot_value = entry.get("raw_hot") or entry.get("num") or ""
            items.append({
                "rank": i,
                "title": word,
                "hot_value": str(hot_value),
                "link": f"https://s.weibo.com/weibo?q={requests.utils.quote('#' + word + '#')}",
            })
        if items:
            return items
        raise ValueError("ajax接口返回为空，转入网页兜底")
    except Exception:
        # 兜底方案：解析热搜网页表格
        resp = requests.get("https://s.weibo.com/top/summary", headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("table tr")
        items = []
        rank = 0
        for row in rows:
            link_tag = row.select_one("td.td-02 a")
            if not link_tag:
                continue
            rank += 1
            title = link_tag.get_text(strip=True)
            href = link_tag.get("href", "")
            if href.startswith("/"):
                href = "https://s.weibo.com" + href
            hot_tag = row.select_one("td.td-02 span")
            hot_value = hot_tag.get_text(strip=True) if hot_tag else ""
            items.append({"rank": rank, "title": title, "hot_value": hot_value, "link": href})
            if rank >= 20:
                break
        return items


def build_source_block(fetch_fn, *args, **kwargs):
    """统一包装：抓取成功返回 (items, None)，失败返回 ([], 错误信息)"""
    try:
        return fetch_fn(*args, **kwargs), None
    except Exception as exc:  # noqa: BLE001 - 任何异常都要吞掉并记录，不能让脚本中断
        return [], str(exc)


def main():
    errors = []
    sections = []

    # ---------------- 国内焦点：新华社 / 人民日报 ----------------
    domestic_sources = []
    for query, name in [
        ("site:news.cn OR site:xinhuanet.com", "新华社"),
        ("site:people.com.cn", "人民日报"),
    ]:
        items, err = build_source_block(fetch_bing_news, query, name)
        if err:
            errors.append(f"{name}: {err}")
        domestic_sources.append({"source_name": name, "items": items})
    sections.append({"id": "domestic", "name": "国内焦点", "sources": domestic_sources})

    # ---------------- 国际视野：BBC / CNN / Reuters ----------------
    international_sources = []
    intl_jobs = [
        (fetch_rss, ("http://feeds.bbci.co.uk/news/world/rss.xml", "BBC"), {}),
        (fetch_rss, ("http://rss.cnn.com/rss/cnn_topstories.rss", "CNN"), {}),
        (fetch_bing_news, ("site:reuters.com", "路透社 Reuters"), {"market": "en-US"}),
    ]
    for fn, args, kwargs in intl_jobs:
        items, err = build_source_block(fn, *args, **kwargs)
        name = args[1]
        if err:
            errors.append(f"{name}: {err}")
        international_sources.append({"source_name": name, "items": items})
    sections.append({"id": "international", "name": "国际视野", "sources": international_sources})

    # ---------------- 微博热搜 ----------------
    weibo_items, weibo_err = build_source_block(fetch_weibo_hot)
    if weibo_err:
        errors.append(f"微博热搜: {weibo_err}")
    sections.append({"id": "weibo", "name": "微博热搜", "items": weibo_items})

    now_bj = datetime.now(BEIJING_TZ)
    output = {
        "updated_at": now_bj.strftime("%Y-%m-%d %H:%M:%S (北京时间)"),
        "sections": sections,
        "errors": errors,
    }

    with open("docs/data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"完成，共 {len(sections)} 个板块；错误 {len(errors)} 条：{errors}")


if __name__ == "__main__":
    main()
