#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日早报数据抓取脚本
----------------------------------------
由 GitHub Actions 定时触发，在服务器端（而非用户浏览器）完成所有抓取，
避免了浏览器直接访问外站导致的跨域(CORS)限制。
抓取结果写入 docs/data.json，前端页面 docs/index.html 只负责读取展示。

数据源说明（务必知悉，方便以后维护）：
  - BBC / CNN：使用官方公开 RSS，最稳定，自带摘要文本，明确限定为英文。
  - Reuters（路透社）：没有官方公开 RSS，改用必应新闻搜索的 RSS 输出，
    并强制指定英文界面/地区参数，避免混入其他语言的结果。
  - 新华社 / 人民日报：同样没有稳定官方RSS，优先用必应新闻搜索，
    如果必应没抓到内容（比如被临时限流），自动改用谷歌新闻搜索兜底，
    两边都强制指定中文简体。
  - 微博热搜：使用微博前端页面自带的 ajax 接口（非官方开放API，
    可能随时变化）。如果该接口抓取失败，会自动尝试退回到
    s.weibo.com/top/summary 页面解析。

任何一个来源抓取失败都不会被"悄悄吞掉"：只要最终一条新闻都没抓到，
就会往 data.json 的 "errors" 字段里写一条具体的失败原因，
方便下次直接照着报错去改，而不是对着"抓取失败"四个字瞎猜。
"""

import json
import re
import html
from datetime import datetime, timezone, timedelta

import requests
import feedparser
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

HEADERS_ZH = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
HEADERS_EN = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
TIMEOUT = 15
BEIJING_TZ = timezone(timedelta(hours=8))

# 各板块展示的条数，按需求调整：国内焦点每个来源5条(共10条)，
# 国际视野三家媒体总共5条，微博热搜5条
DOMESTIC_LIMIT_PER_SOURCE = 5
WEIBO_LIMIT = 5


def clean_text(raw, limit=140):
    """去掉HTML标签、多余空白，截断成摘要长度"""
    if not raw:
        return ""
    text = BeautifulSoup(html.unescape(raw), "html.parser").get_text(separator=" ")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def fetch_rss(url, headers, limit):
    """通用RSS抓取。抓到0条也视为失败，抛异常交给上层记录，方便定位问题"""
    resp = requests.get(url, headers=headers, timeout=TIMEOUT)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    items = []
    for entry in feed.entries[:limit]:
        title = clean_text(entry.get("title", ""), limit=80)
        summary = clean_text(entry.get("summary", entry.get("description", "")), limit=140)
        link = entry.get("link", "")
        if not title or not link:
            continue
        items.append({"title": title, "summary": summary, "link": link})
    if not items:
        raise ValueError(f"HTTP {resp.status_code}，但解析出0条内容（可能页面结构变化或被限流）")
    return items


def fetch_bing_news(query, limit, lang="zh"):
    """必应新闻搜索RSS。lang='zh' 强制中文结果，lang='en' 强制英文结果"""
    if lang == "zh":
        url = f"https://www.bing.com/news/search?q={requests.utils.quote(query)}&format=RSS&cc=CN&setlang=zh-Hans"
        headers = HEADERS_ZH
    else:
        url = f"https://www.bing.com/news/search?q={requests.utils.quote(query)}&format=RSS&cc=US&setlang=en-US"
        headers = HEADERS_EN
    return fetch_rss(url, headers, limit)


def fetch_google_news(query, limit, lang="zh"):
    """谷歌新闻搜索RSS，作为必应失败时的第二重兜底"""
    if lang == "zh":
        url = (f"https://news.google.com/rss/search?q={requests.utils.quote(query)}"
               "&hl=zh-CN&gl=CN&ceid=CN:zh-Hans")
        headers = HEADERS_ZH
    else:
        url = (f"https://news.google.com/rss/search?q={requests.utils.quote(query)}"
               "&hl=en-US&gl=US&ceid=US:en")
        headers = HEADERS_EN
    return fetch_rss(url, headers, limit)


def fetch_with_fallback(query, limit, lang):
    """先试必应，失败再试谷歌新闻，两个都失败才真正报错"""
    errors = []
    try:
        return fetch_bing_news(query, limit, lang), None
    except Exception as exc:  # noqa: BLE001
        errors.append(f"必应搜索失败({exc})")
    try:
        return fetch_google_news(query, limit, lang), None
    except Exception as exc:  # noqa: BLE001
        errors.append(f"谷歌新闻搜索也失败({exc})")
    return [], "；".join(errors)


def fetch_weibo_hot():
    """微博热搜榜。优先走ajax接口，失败则退回到网页表格解析"""
    try:
        url = "https://weibo.com/ajax/side/hotSearch"
        headers = dict(HEADERS_ZH)
        headers["Referer"] = "https://weibo.com/"
        resp = requests.get(url, headers=headers, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        raw_list = data.get("data", {}).get("realtime", [])
        items = []
        for i, entry in enumerate(raw_list[:WEIBO_LIMIT], start=1):
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
        resp = requests.get("https://s.weibo.com/top/summary", headers=HEADERS_ZH, timeout=TIMEOUT)
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
            if rank >= WEIBO_LIMIT:
                break
        return items


def main():
    errors = []
    sections = []

    # ---------------- 国内焦点：新华社 / 人民日报（各5条，共10条）----------------
    domestic_sources = []
    for query, name in [
        ("site:news.cn", "新华社"),
        ("site:people.com.cn", "人民日报"),
    ]:
        items, err = fetch_with_fallback(query, DOMESTIC_LIMIT_PER_SOURCE, lang="zh")
        if err:
            errors.append(f"{name}: {err}")
        for it in items:
            it["source"] = name
        domestic_sources.append({"source_name": name, "items": items})
    sections.append({"id": "domestic", "name": "国内焦点", "sources": domestic_sources})

    # ---------------- 国际视野：BBC / CNN / Reuters（合计约5条）----------------
    international_sources = []
    intl_jobs = [
        ("rss", "http://feeds.bbci.co.uk/news/world/rss.xml", "BBC", 2),
        ("rss", "http://rss.cnn.com/rss/cnn_topstories.rss", "CNN", 2),
        ("bing_en", "site:reuters.com", "路透社 Reuters", 1),
    ]
    for kind, target, name, limit in intl_jobs:
        try:
            if kind == "bing_en":
                items, err = fetch_with_fallback(target, limit, lang="en")
                if err:
                    errors.append(f"{name}: {err}")
            else:
                items = fetch_rss(target, HEADERS_EN, limit)
        except Exception as exc:  # noqa: BLE001
            items = []
            errors.append(f"{name}: {exc}")
        for it in items:
            it["source"] = name
        international_sources.append({"source_name": name, "items": items})
    sections.append({"id": "international", "name": "国际视野", "sources": international_sources})

    # ---------------- 微博热搜（5条）----------------
    try:
        weibo_items = fetch_weibo_hot()
    except Exception as exc:  # noqa: BLE001
        weibo_items = []
        errors.append(f"微博热搜: {exc}")
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
