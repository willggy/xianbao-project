import os
import sqlite3
import threading
import time
from datetime import datetime
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from flask import Flask, render_template, request, Response
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler
from waitress import serve

# ================== 1. 配置中心 ==================
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
        "content_selector": ".thread-content.message, .thread-content, .message.break-all, .message"
    }
}

# 标签筛选配置
BANK_KEYWORDS = {"农行": "农", "工行": "工", "建行": "建", "中行": "中"}
KEYWORDS = list(BANK_KEYWORDS.values()) + [
    "立减金", "红包", "话费", "水", "毛", "招", "hang", "信",
    "移动", "联通", "京东", "支付宝", "微信", "流量", "充值", "zfb"
]

DB_PATH = "/app/data/xianbao.db"
PER_PAGE = 30
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122"}

COOLDOWN_SECONDS = 30
last_scrape_time = 0
scrape_lock = threading.Lock()

session = requests.Session()
session.headers.update(HEADERS)
adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=2)
session.mount("http://", adapter)
session.mount("https://", adapter)

# ================== 2. Flask 初始化 ==================
app = Flask(__name__)

# ================== 3. 数据库 ==================
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=20, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db_connection()
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS visit_stats (
            ip TEXT PRIMARY KEY,
            visit_count INTEGER DEFAULT 1,
            last_visit TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            url TEXT UNIQUE,
            site_source TEXT,
            match_keyword TEXT,
            original_time TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS article_content (
            url TEXT PRIMARY KEY,
            content TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS scrape_log (
            id INTEGER PRIMARY KEY,
            last_scrape TIMESTAMP
        );
        """)
        conn.commit()
        print("[DB] 初始化完成")
    finally:
        conn.close()

init_db()

def record_visit():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    if not ip: return
    conn = get_db_connection()
    try:
        conn.execute("""
            INSERT INTO visit_stats (ip, visit_count, last_visit)
            VALUES (?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(ip) DO UPDATE SET
                visit_count = visit_count + 1,
                last_visit = CURRENT_TIMESTAMP
        """, (ip,))
        conn.commit()
    except: pass
    finally: conn.close()

# ================== 4. 抓取逻辑 ==================
def scrape_all_sites(force=False):
    global last_scrape_time
    if scrape_lock.locked(): return
    
    now = time.time()
    if not force and now - last_scrape_time < COOLDOWN_SECONDS:
        return

    last_scrape_time = now
    with scrape_lock:
        print("[Scrape] 开始抓取...")
        conn = get_db_connection()
        total_new = 0
        for site_key, config in SITES_CONFIG.items():
            try:
                r = session.get(config["list_url"], timeout=10)
                soup = BeautifulSoup(r.text, "html.parser")
                entries = []
                for item in soup.select(config["list_selector"]):
                    a = item.find("a")
                    if not a: continue
                    href = a.get("href", "")
                    url = href if href.startswith("http") else config["domain"] + href
                    title = a.get_text(strip=True)
                    
                    # 标签匹配逻辑
                    kw = next((k for k in KEYWORDS if k.lower() in title.lower()), None)
                    if not kw: continue

                    entries.append((title, url, site_key, kw, datetime.now().strftime("%H:%M")))

                if entries:
                    cur = conn.cursor()
                    cur.executemany("INSERT OR IGNORE INTO articles (title, url, site_source, match_keyword, original_time) VALUES (?,?,?,?,?)", entries)
                    total_new += cur.rowcount
            except Exception as e:
                print(f"[Scrape] {site_key} 失败: {e}")

        conn.execute("INSERT OR REPLACE INTO scrape_log(id,last_scrape) VALUES(1,?)", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))
        conn.commit()
        conn.close()
        print(f"[Scrape] 完成，新增 {total_new} 条")

# ================== 5. 工具 ==================
def clean_html(html, site_key):
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup.find_all(True):
        if tag.name == "img":
            src = tag.get("src", "")
            if src.startswith("/"): src = SITES_CONFIG[site_key]["domain"] + src
            tag.attrs = {"src": f"/img_proxy?url={src}", "style": "max-width:100%;border-radius:8px;"}
        else:
            tag.attrs = {}
    return str(soup)

# ================== 6. 路由 ==================
@app.route("/")
def index():
    record_visit()
    tag = request.args.get("tag") # 获取筛选标签
    page = request.args.get("page", 1, type=int)

    conn = get_db_connection()
    # 动态 SQL 筛选
    query = "SELECT * FROM articles"
    params = []
    if tag:
        query += " WHERE match_keyword = ?"
        params.append(tag)
    
    total = conn.execute(f"SELECT COUNT(*) FROM ({query})", params).fetchone()[0]
    rows = conn.execute(f"{query} ORDER BY id DESC LIMIT ? OFFSET ?", params + [PER_PAGE, (page-1)*PER_PAGE]).fetchall()
    conn.close()

    articles = [{"title": r["title"], "view_link": f"/view?id={r['id']}", "time": r["original_time"]} for r in rows]
    
    # 构造标签栏数据
    tag_list = [{"name": "全部", "value": None}] + [{"name": k, "value": v} for k, v in BANK_KEYWORDS.items()] + [{"name": "红包", "value": "红包"}]
    
    if page == 1 and not tag:
        threading.Thread(target=scrape_all_sites).start()

    return render_template("index.html", articles=articles, current_page=page, total_pages=(total+PER_PAGE-1)//PER_PAGE, tags=tag_list, current_tag=tag)

@app.route("/view")
def view():
    article_id = request.args.get("id", type=int)
    conn = get_db_connection()
    row = conn.execute("SELECT url,title,site_source FROM articles WHERE id=?", (article_id,)).fetchone()
    if not row: return "文章不存在"

    url, title, site_key = row["url"], row["title"], row["site_source"]
    cached = conn.execute("SELECT content FROM article_content WHERE url=?", (url,)).fetchone()

    if cached and len(cached['content']) > 50:
        content = clean_html(cached["content"], site_key)
    else:
        try:
            r = session.get(url, timeout=10)
            soup = BeautifulSoup(r.text, "html.parser")
            # 强化正文提取
            node = None
            for selector in SITES_CONFIG[site_key]["content_selector"].split(','):
                node = soup.select_one(selector.strip())
                if node: break
            
            raw = str(node) if node else "无法提取正文，请查看原文"
            conn.execute("INSERT OR REPLACE INTO article_content(url,content) VALUES(?,?)", (url, raw))
            conn.commit()
            content = clean_html(raw, site_key)
        except Exception as e:
            content = f"抓取失败: {e}"

    conn.close()
    return render_template("detail.html", title=title, content=content, original_url=url)

@app.route("/logs")
def logs():
    """新增统计与日志页面"""
    conn = get_db_connection()
    visitors = conn.execute("SELECT * FROM visit_stats ORDER BY last_visit DESC LIMIT 50").fetchall()
    last_s = conn.execute("SELECT last_scrape FROM scrape_log WHERE id=1").fetchone()
    total_art = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    conn.close()
    return render_template("logs.html", visitors=visitors, last_scrape=last_s[0] if last_s else "从未抓取", total_articles=total_art)

@app.route("/img_proxy")
def img_proxy():
    url = request.args.get("url")
    if not url: return Response(status=400)
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        return Response(r.content, content_type=r.headers.get("Content-Type"))
    except: return Response(status=404)

if __name__ == '__main__':
    serve(app, host='0.0.0.0', port=8080, threads=10)
