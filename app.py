import os
import sqlite3
import threading
import time
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter
from flask import Flask, render_template, request, Response
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler
from waitress import serve

# ================== 1. 基础配置 ==================
app = Flask(__name__)

DB_PATH = "/app/data/xianbao.db"
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

PER_PAGE = 30
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"
}

# 冷却控制
COOLDOWN_SECONDS = 30
last_scrape_time = 0
scrape_lock = threading.Lock()

# ================== 2. 站点与关键词 ==================
SITES_CONFIG = {
    "xianbao": {
        "name": "线报库",
        "domain": "https://new.xianbao.fun",
        "list_url": "https://new.xianbao.fun/",
        "list_selector": "tr, li",
        "content_selector": "#mainbox article .article-content, #art-fujia"
    },
    "iehou": {
        "name": "爱猴线报",
        "domain": "https://iehou.com",
        "list_url": "https://iehou.com/",
        "list_selector": "#body ul li",
        "content_selector": ".thread-content.message, .thread-content, .message"
    }
}

BANK_KEYWORDS = {"农行": "农", "工行": "工", "建行": "建", "中行": "中"}
KEYWORDS = list(BANK_KEYWORDS.values()) + [
    "立减金", "红包", "话费", "京东", "支付宝", "微信", "流量", "充值", "zfb"
]

# ================== 3. Session ==================
session = requests.Session()
session.headers.update(HEADERS)
adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=2)
session.mount("http://", adapter)
session.mount("https://", adapter)

# ================== 4. 数据库 ==================
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("PRAGMA journal_mode=WAL;")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS articles(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        url TEXT UNIQUE,
        site_source TEXT,
        match_keyword TEXT,
        original_time TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS article_content(
        url TEXT PRIMARY KEY,
        content TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS scrape_log(
        id INTEGER PRIMARY KEY,
        last_scrape TEXT
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS visit_stats(
        ip TEXT PRIMARY KEY,
        visit_count INTEGER DEFAULT 1,
        last_visit TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    conn.commit()
    conn.close()

# ================== 5. 抓取逻辑 ==================
def scrape_all_sites(force=False):
    global last_scrape_time

    if scrape_lock.locked():
        return

    with scrape_lock:
        now = time.time()
        if not force and now - last_scrape_time < COOLDOWN_SECONDS:
            print("⏳ 冷却中，跳过抓取")
            return

        last_scrape_time = now
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{now_str}] 开始抓取")

        conn = get_db()
        total_new = 0

        for site_key, cfg in SITES_CONFIG.items():
            try:
                session.headers.update({"Referer": cfg["domain"]})
                r = session.get(cfg["list_url"], timeout=10)
                r.encoding = "utf-8"
                soup = BeautifulSoup(r.text, "html.parser")

                rows = []
                for item in soup.select(cfg["list_selector"]):
                    a = item.find("a")
                    if not a:
                        continue

                    href = a.get("href")
                    if not href:
                        continue

                    url = href if href.startswith("http") else cfg["domain"] + href
                    if "haodan" in url:
                        continue

                    title = a.get_text(strip=True)
                    kw = next((k for k in KEYWORDS if k.lower() in title.lower()), None)
                    if not kw:
                        continue

                    rows.append((
                        title, url, site_key, kw,
                        datetime.now().strftime("%H:%M")
                    ))

                if rows:
                    cur = conn.cursor()
                    cur.executemany("""
                        INSERT OR IGNORE INTO articles
                        (title, url, site_source, match_keyword, original_time)
                        VALUES (?, ?, ?, ?, ?)
                    """, rows)
                    total_new += cur.rowcount

            except Exception as e:
                print(f"❌ {site_key} 抓取失败:", e)

        conn.execute(
            "INSERT OR REPLACE INTO scrape_log(id,last_scrape) VALUES(1,?)",
            (now_str,)
        )
        conn.commit()
        conn.close()

        print(f"[{now_str}] 抓取完成，新数据 {total_new}")

# ================== 6. HTML 清洗 ==================
def clean_html(html, site_key):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(True):
        if tag.name == "img":
            src = tag.get("src", "")
            if src.startswith("/"):
                src = SITES_CONFIG[site_key]["domain"] + src
            tag.attrs = {
                "src": f"/img_proxy?url={src}",
                "loading": "lazy",
                "style": "max-width:100%;border-radius:8px"
            }
        else:
            tag.attrs = {}
    return str(soup)

# ================== 7. 路由 ==================
@app.route("/")
def index():
    page = request.args.get("page", 1, type=int)
    tag = request.args.get("tag")

    threading.Thread(target=scrape_all_sites).start()

    conn = get_db()
    where = ""
    params = []

    if tag:
        where = "WHERE match_keyword=?"
        params.append(tag)

    total = conn.execute(f"SELECT COUNT(*) FROM articles {where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT id,title,original_time FROM articles {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [PER_PAGE, (page - 1) * PER_PAGE]
    ).fetchall()

    conn.close()

    articles = [
        {"title": r["title"], "time": r["original_time"], "view": f"/view?id={r['id']}"}
        for r in rows
    ]

    return render_template(
        "index.html",
        articles=articles,
        current_page=page,
        total_pages=(total + PER_PAGE - 1) // PER_PAGE,
        current_tag=tag,
        bank_tag_list=BANK_KEYWORDS
    )

@app.route("/view")
def view():
    aid = request.args.get("id", type=int)
    conn = get_db()

    row = conn.execute(
        "SELECT url,title,site_source FROM articles WHERE id=?",
        (aid,)
    ).fetchone()

    if not row:
        return "文章不存在"

    cached = conn.execute(
        "SELECT content FROM article_content WHERE url=?",
        (row["url"],)
    ).fetchone()

    if cached:
        content = clean_html(cached["content"], row["site_source"])
    else:
        r = session.get(row["url"], timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")

        raw = ""
        for sel in SITES_CONFIG[row["site_source"]]["content_selector"].split(","):
            node = soup.select_one(sel.strip())
            if node:
                raw = str(node)
                break

        if raw:
            conn.execute(
                "INSERT OR REPLACE INTO article_content(url,content) VALUES(?,?)",
                (row["url"], raw)
            )
            conn.commit()
            content = clean_html(raw, row["site_source"])
        else:
            content = "内容获取失败"

    conn.close()
    return render_template("detail.html", title=row["title"], content=content)

@app.route("/img_proxy")
def img_proxy():
    url = request.args.get("url")
    r = requests.get(url, headers=HEADERS, stream=True, timeout=10)
    return Response(r.content, content_type=r.headers.get("Content-Type"))

# ================== 8. 启动 ==================
if __name__ == "__main__":
    init_db()

    scheduler = BackgroundScheduler()
    scheduler.add_job(scrape_all_sites, "interval", minutes=10)
    scheduler.start()

    print("🚀 Zeabur 启动成功")
    serve(app, host="0.0.0.0", port=8080, threads=8)
