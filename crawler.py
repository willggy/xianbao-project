import requests
from bs4 import BeautifulSoup
import urllib.parse
import time
import os

API = os.environ.get("API_WORKER_URL", "https://xianbao-api-worker.gaoguanyu777.workers.dev/api") 

TARGET_DOMAIN = "https://new.xianbao.fun"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": TARGET_DOMAIN
}

KEYWORDS = ["hang", "行", "立减金", "ljj", "水", "红包", "券"]
EXCLUDE = ["排行榜", "排 行 榜", "榜单"]

def save_list(title, url, keyword):
    requests.post(f"{API}/save_list", json={
        "title": title,
        "url": url,
        "match": keyword
    })

def save_content(url, html):
    requests.post(f"{API}/save_content", json={
        "url": url,
        "content": html
    })

def extract_content_html(target_url):
    resp = requests.get(target_url, headers=HEADERS, timeout=15)
    resp.encoding = "utf-8"

    soup = BeautifulSoup(resp.text, "html.parser")

    content = soup.find("td", class_="t_f") \
           or soup.find("div", class_="message") \
           or soup.find("div", class_="content")

    if not content:
        divs = soup.find_all("div")
        if divs:
            content = max(divs, key=lambda t: len(t.get_text()))

    if not content:
        return "<div>无法提取正文</div>"

    for tag in content(["script", "style", "iframe"]):
        tag.extract()

    for img in content.find_all("img"):
        src = img.get("src") or img.get("file")
        if not src:
            continue
        if not src.startswith("http"):
            if src.startswith("/"):
                src = TARGET_DOMAIN + src
            else:
                src = TARGET_DOMAIN + "/" + src
        img["src"] = src
        if "lazyloadthumb" in img.attrs:
            del img["lazyloadthumb"]

    for elem in content.find_all(True):
        txt = elem.get_text()
        if any(x in txt for x in [
            "本文关联的评论", "发表评论",
            "发布评论", "条评论", "评论列表"
        ]):
            parent = elem.find_parent(["div", "tr", "td", "table"])
            if parent and len(parent.get_text()) < 800:
                parent.extract()

    return str(content)

def crawl_once():
    print("开始爬取列表...")

    resp = requests.get(TARGET_DOMAIN + "/", headers=HEADERS, timeout=15)
    resp.encoding = "utf-8"

    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.find_all("tr") or soup.find_all("li")

    for row in rows:
        a = row.find("a")
        if not a:
            continue

        title = a.get_text().strip()
        title_lower = title.lower()

        if any(kw.lower() in title_lower for kw in EXCLUDE):
            continue

        url = a.get("href")
        if not url.startswith("http"):
            if url.startswith("/"):
                url = TARGET_DOMAIN + url
            else:
                url = TARGET_DOMAIN + "/" + url

        matched_kw = None
        for kw in KEYWORDS:
            if kw.lower() in title_lower:
                matched_kw = kw
                break

        if not matched_kw:
            continue

        print("保存列表：", title)
        save_list(title, url, matched_kw)

        print("抓取正文：", url)
        html = extract_content_html(url)
        save_content(url, html)

        time.sleep(1)

    print("任务完成！")

if __name__ == "__main__":
    crawl_once()
