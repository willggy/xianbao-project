import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta

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

BANK_KEYWORDS = {
    "农行": ["农行", "农业银行", "农"],
    "工行": ["工行", "工商银行", "工"],
    "建行": ["建行", "建设银行", "建", "CCB"],
    "中行": ["中行", "中国银行", "中hang"]
}

ALL_BANK_VALS = [word for words in BANK_KEYWORDS.values() for word in words]
OTHER_KEYWORDS = [
    "立减金", "红包", "话费", "水", "毛", "招", "hang", "信", "移动",
    "联通", "京东", "支付宝", "微信", "流量", "话费券", "充值",
    "话费充值", "zfb"
]
KEYWORDS = ALL_BANK_VALS + OTHER_KEYWORDS

app = Flask(__name__)
DATA_DIR = "/app/data"
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "xianbao.db")

PER_PAGE = 30
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"}

last_scrape_time = 0
COOLDOWN_SECONDS = 30

session = requests.Session()
session.headers.update(HEADERS)
adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=2)
session.mount('http://', adapter)
session.mount('https://', adapter)
scrape_lock = threading.Lock()

# ================== 2. 数据库 ==================
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=20, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # 自动确保表存在
    conn.execute('PRAGMA journal_mode=WAL;')
    conn.execute('''CREATE TABLE IF NOT EXISTS articles(
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        title TEXT, url TEXT UNIQUE, site_source TEXT,
        match_keyword TEXT, original_time TEXT, 
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.execute('CREATE TABLE IF NOT EXISTS article_content(url TEXT PRIMARY KEY, content TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
    conn.execute('CREATE TABLE IF NOT EXISTS scrape_log(id INTEGER PRIMARY KEY AUTOINCREMENT, last_scrape TEXT)')
    conn.execute('''CREATE TABLE IF NOT EXISTS visit_stats(
        ip TEXT PRIMARY KEY, 
        visit_count INTEGER DEFAULT 1, 
        last_visit TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    return conn

def init_db():
    # 只是确保数据库文件存在和表创建
    get_db_connection().close()

def record_visit():
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    conn = get_db_connection()
    conn.execute('''
        INSERT INTO visit_stats (ip, visit_count, last_visit) 
        VALUES (?, 1, CURRENT_TIMESTAMP)
        ON CONFLICT(ip) DO UPDATE SET 
            visit_count = visit_count + 1,
            last_visit = CURRENT_TIMESTAMP
    ''', (ip,))
    conn.commit()
    conn.close()

# ================== 3. 核心逻辑 ==================
def scrape_all_sites(force=False):
    if scrape_lock.locked(): return
    with scrape_lock:
        start_time = time.time()
        now_beijing = datetime.utcnow() + timedelta(hours=8)
        now_str = now_beijing.strftime("%Y-%m-%d %H:%M:%S")

        conn = get_db_connection()
        site_stats = {}

        for site_key, config in SITES_CONFIG.items():
            try:
                session.headers.update({"Referer": config['domain']})
                resp = session.get(config['list_url'], timeout=10)
                resp.encoding = 'utf-8'
                soup = BeautifulSoup(resp.text, "html.parser")

                site_entries = []
                for item in soup.select(config['list_selector']):
                    a = item.select_one("a[href*='view'], a[href*='thread'], a[href*='post'], a[href*='.htm']") or item.find("a")
                    if not a: continue

                    href = a.get("href", "")
                    full_url = href if href.startswith("http") else (config['domain'] + (href if href.startswith("/") else "/" + href))

                    title = a.get_text(strip=True)
                    matched_kw = next((kw for kw in KEYWORDS if kw.lower() in title.lower()), None)
                    if not matched_kw: continue

                    final_tag = matched_kw
                    for tag_name, val_list in BANK_KEYWORDS.items():
                        if matched_kw in val_list:
                            final_tag = tag_name
                            break

                    time_val = now_beijing.strftime("%H:%M")
                    site_entries.append((title, full_url, site_key, final_tag, time_val))

                if site_entries:
                    cursor = conn.cursor()
                    cursor.executemany('INSERT OR IGNORE INTO articles (title, url, site_source, match_keyword, original_time) VALUES(?,?,?,?,?)', site_entries)
                    site_stats[config['name']] = cursor.rowcount
            except Exception as e:
                print(f"抓取失败 {site_key}: {e}")

        duration = time.time() - start_time
        stats_str = ", ".join([f"{name}+{count}" for name, count in site_stats.items()]) if site_stats else "无新数据"
        log_msg = f"[{now_str}] 任务完成: {stats_str} (耗时 {duration:.2f}s)"

        conn.execute('INSERT INTO scrape_log(last_scrape) VALUES(?)', (log_msg,))
        conn.execute('DELETE FROM scrape_log WHERE id NOT IN (SELECT id FROM scrape_log ORDER BY id DESC LIMIT 50)')
        conn.commit()
        conn.close()

def clean_html(html_content, site_key):
    if not html_content: return ""
    soup = BeautifulSoup(html_content, "html.parser")
    for tag in soup.find_all(True):
        if tag.name == 'img':
            src = tag.get('src', '')
            if src.startswith('/'): src = SITES_CONFIG[site_key]['domain'] + src
            tag.attrs = {'src': f"/img_proxy?url={src}", 'loading': 'lazy', 'style': 'max-width:100%; height:auto; border-radius:8px;'}
        elif tag.name == 'a':
            real_href = tag.get('href', '')
            if real_href.startswith('/'): real_href = SITES_CONFIG[site_key]['domain'] + real_href
            display_text = tag.get_text(strip=True)
            if "..." in display_text and real_href.startswith('http'):
                tag.string = real_href 
            tag.attrs = {'href': real_href, 'target': '_blank', 'rel': 'noopener', 'style': 'color: #007aff; text-decoration: underline; word-break: break-all;'}
        elif tag.name in ['br', 'p', 'div']:
            tag.attrs = {}
        else:
            tag.attrs = {}
    return str(soup)

# ================== 4. 路由 ==================
@app.route('/')
def index():
    record_visit()
    tag = request.args.get('tag')
    page = request.args.get('page', 1, type=int)

    global last_scrape_time
    current_time = time.time()
    conn = get_db_connection()

    if page == 1 and not tag:
        if current_time - last_scrape_time > COOLDOWN_SECONDS:
            last_scrape_time = current_time
            threading.Thread(target=scrape_all_sites).start()
        else:
            now_s = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute('INSERT INTO scrape_log(last_scrape) VALUES(?)', (f"[{now_s}] 触发刷新：处于冷却中，跳过抓取",))
            conn.commit()

    where, params = ("", []) if not tag else ("WHERE match_keyword = ?", [tag.strip()])
    db_data = conn.execute(f'SELECT id, title, original_time FROM articles {where} ORDER BY id DESC LIMIT ? OFFSET ?', params + [PER_PAGE, (page-1)*PER_PAGE]).fetchall()
    total = conn.execute(f'SELECT COUNT(*) FROM articles {where}', params).fetchone()[0]
    conn.close()

    articles = [{"title": r['title'], "view_link": f"/view?id={r['id']}", "time": r['original_time']} for r in db_data]

    tags = [{"name": "全部", "tag": None}]
    for k in BANK_KEYWORDS.keys():
        tags.append({"name": k, "tag": k})
    for extra in ["红包", "话费", "京东", "支付宝"]:
        tags.append({"name": extra, "tag": extra})

    return render_template('index.html', articles=articles, current_page=page, total_pages=(total+PER_PAGE-1)//PER_PAGE, current_tag=tag, bank_tag_list=tags)

@app.route("/view")
def view():
    article_id = request.args.get("id", type=int)
    conn = get_db_connection()
    row = conn.execute("SELECT url, title, site_source FROM articles WHERE id=?", (article_id,)).fetchone()
    if not row:
        conn.close()
        return "文章不存在"

    url, title, site_key = row["url"], row["title"], row["site_source"]
    cached = conn.execute("SELECT content FROM article_content WHERE url=?", (url,)).fetchone()

    if cached and len(cached['content']) > 100:
        content = clean_html(cached["content"], site_key)
    else:
        try:
            r = session.get(url, timeout=10)
            r.encoding = 'utf-8'
            soup = BeautifulSoup(r.text, "html.parser")
            selector_list = SITES_CONFIG[site_key]["content_selector"].split(',')
            node = None
            for sel in selector_list:
                temp_node = soup.select_one(sel.strip())
                if temp_node and len(temp_node.get_text(strip=True)) > 5:
                    node = temp_node
                    break
            raw_content = str(node) if node else "内容提取失败，请查看原文。"
            conn.execute("INSERT OR REPLACE INTO article_content(url, content) VALUES(?,?)", (url, raw_content))
            conn.commit()
            content = clean_html(raw_content, site_key)
        except Exception as e:
            content = f"详情页加载失败: {e}"
    conn.close()
    return render_template("detail.html", title=title, content=content, original_url=url)

@app.route('/logs')
def show_logs():
    conn = get_db_connection()
    logs = conn.execute('SELECT last_scrape FROM scrape_log ORDER BY id DESC LIMIT 50').fetchall()
    visitors = conn.execute('SELECT ip, visit_count, last_visit FROM visit_stats ORDER BY last_visit DESC LIMIT 30').fetchall()
    conn.close()
    return render_template('logs.html', logs=logs, visitors=visitors)

@app.route('/img_proxy')
def img_proxy():
    url = request.args.get('url')
    if not url: return Response(status=400)
    try:
        r = requests.get(url, headers=HEADERS, stream=True, timeout=10)
        return Response(r.content, content_type=r.headers.get('Content-Type'))
    except:
        return Response(status=404)

# ================== 5. 启动 ==================
if __name__ == '__main__':
    init_db()  # ⚠️ 确保数据库和表存在
    # 启动前做一次抓取，避免 scheduler 首次触发报错
    threading.Thread(target=scrape_all_sites, args=(True,)).start()

    scheduler = BackgroundScheduler()
    scheduler.add_job(scrape_all_sites, 'interval', minutes=10)
    scheduler.start()

    serve(app, host='0.0.0.0', port=8080, threads=10)
