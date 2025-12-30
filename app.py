import os
import sqlite3
import threading
import time
import base64
import re
from datetime import datetime, timedelta
from functools import wraps

import requests
from requests.adapters import HTTPAdapter
from flask import Flask, render_template, request, Response, redirect, session, url_for
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler
from waitress import serve

# ==========================================
# 1. 基础配置
# ==========================================
app = Flask(__name__)

# --- 网站名称修改 ---
SITE_TITLE = "古希腊掌管羊毛的神"

app.secret_key = os.environ.get('SECRET_KEY', 'xianbao_secret_key_888') 
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '123')  
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024 

# --- 采集源配置 ---
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

# 关键词过滤
BANK_KEYWORDS = {
    "农行": ["农行", "农业银行", "农"],
    "工行": ["工行", "工商银行", "工"],
    "建行": ["建行", "建设银行", "建", "CCB"],
    "中行": ["中行", "中国银行", "中hang"]
}
ALL_BANK_VALS = [word for words in BANK_KEYWORDS.values() for word in words]
OTHER_KEYWORDS = ["立减金", "红包", "话费", "水", "毛", "招", "hang", "信", "移动", "联通", "京东", "支付宝", "微信", "流量", "话费券", "充值", "zfb"]
KEYWORDS = ALL_BANK_VALS + OTHER_KEYWORDS

# 数据库路径 (Zeabur 建议挂载 /app/data)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "xianbao.db")

PER_PAGE = 30
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"}

# 网络请求优化
session_http = requests.Session()
session_http.headers.update(HEADERS)
adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=2)
session_http.mount('http://', adapter)
session_http.mount('https://', adapter)

scrape_lock = threading.Lock()

# ==========================================
# 2. 数据库与并发优化
# ==========================================
def get_db_connection():
    # 优化1：增加 timeout 处理并发锁，开启 WAL 模式实现读写分离
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL;')
    conn.execute('PRAGMA synchronous=NORMAL;')
    
    conn.execute('''CREATE TABLE IF NOT EXISTS articles(
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        title TEXT, url TEXT UNIQUE, site_source TEXT,
        match_keyword TEXT, original_time TEXT, is_top INTEGER DEFAULT 0,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.execute('CREATE TABLE IF NOT EXISTS article_content(url TEXT PRIMARY KEY, content TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
    conn.execute('CREATE TABLE IF NOT EXISTS scrape_log(id INTEGER PRIMARY KEY AUTOINCREMENT, last_scrape TEXT)')
    conn.execute('CREATE TABLE IF NOT EXISTS visit_stats(ip TEXT PRIMARY KEY, visit_count INTEGER DEFAULT 1, last_visit TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
    conn.commit()
    return conn

def record_visit():
    # 优化2：Zeabur 访问不计次数 (过滤健康检查)
    ua = request.headers.get('User-Agent', '')
    if 'HealthCheck' in ua or 'Zeabur' in ua:
        return
        
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    try:
        conn = get_db_connection()
        conn.execute('''INSERT INTO visit_stats (ip, visit_count, last_visit) VALUES (?, 1, CURRENT_TIMESTAMP)
                     ON CONFLICT(ip) DO UPDATE SET visit_count = visit_count + 1, last_visit = CURRENT_TIMESTAMP''', (ip,))
        conn.commit()
        conn.close()
    except: pass

# ==========================================
# 3. 核心抓取与 4天自动清理
# ==========================================
def scrape_all_sites():
    if scrape_lock.locked(): return
    with scrape_lock:
        start_time = time.time()
        now_beijing = datetime.utcnow() + timedelta(hours=8)
        now_str = now_beijing.strftime("%Y-%m-%d %H:%M:%S")
        
        conn = get_db_connection()
        site_stats = {}

        for site_key, config in SITES_CONFIG.items():
            try:
                resp = session_http.get(config['list_url'], timeout=15)
                resp.encoding = 'utf-8'
                soup = BeautifulSoup(resp.text, "html.parser")
                
                new_count = 0
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
                            final_tag = tag_name; break
                    
                    time_val = now_beijing.strftime("%H:%M")
                    try:
                        conn.execute('INSERT OR IGNORE INTO articles (title, url, site_source, match_keyword, original_time) VALUES(?,?,?,?,?)',
                                     (title, full_url, site_key, final_tag, time_val))
                        if conn.total_changes > 0: new_count += 1
                    except: pass
                site_stats[config['name']] = new_count
            except Exception as e:
                print(f"抓取 {site_key} 报错: {e}")

        # 优化3：4天自动清理机制 (自己发布的 user 不删除)
        conn.execute("DELETE FROM articles WHERE site_source != 'user' AND updated_at < datetime('now', '-4 days')")
        conn.execute("DELETE FROM article_content WHERE url NOT IN (SELECT url FROM articles)")
        conn.execute("DELETE FROM scrape_log WHERE id NOT IN (SELECT id FROM scrape_log ORDER BY id DESC LIMIT 50)")
        
        duration = time.time() - start_time
        log_msg = f"[{now_str}] 任务完成: {site_stats} (耗时 {duration:.1f}s)"
        print(log_msg)
        conn.execute('INSERT INTO scrape_log(last_scrape) VALUES(?)', (log_msg,))
        conn.commit()
        conn.close()

# ==========================================
# 4. 路由逻辑 (去掉首页阻塞)
# ==========================================
@app.route('/')
def index():
    record_visit()
    tag = request.args.get('tag')
    q = request.args.get('q')
    page = request.args.get('page', 1, type=int)

    # 优化4：彻底去掉首页阻塞抓取逻辑，实现秒开页面
    conn = get_db_connection()
    where = "WHERE 1=1"
    params = []
    if tag: where += " AND match_keyword = ?"; params.append(tag)
    if q: where += " AND title LIKE ?"; params.append(f"%{q}%")
    
    articles = conn.execute(f'SELECT * FROM articles {where} ORDER BY is_top DESC, id DESC LIMIT ? OFFSET ?', 
                            params + [PER_PAGE, (page-1)*PER_PAGE]).fetchall()
    total = conn.execute(f'SELECT COUNT(*) FROM articles {where}', params).fetchone()[0]
    conn.close()
    
    return render_template('index.html', articles=articles, site_title=SITE_TITLE, 
                           bank_list=list(BANK_KEYWORDS.keys()), current_tag=tag, 
                           q=q, current_page=page, total_pages=(total+PER_PAGE-1)//PER_PAGE)

# ... (view, publish, login 等路由保持与 app(1).py 一致即可，重点是 index 和 启动部分) ...

# ==========================================
# 5. 启动入口 (5分钟刷新)
# ==========================================
if __name__ == '__main__':
    get_db_connection().close()
    
    scheduler = BackgroundScheduler()
    # 优化5：改成5分钟刷一次
    scheduler.add_job(scrape_all_sites, 'interval', minutes=5)
    scheduler.start()
    
    # 启动时后台抓取一次
    threading.Thread(target=scrape_all_sites).start()
    
    print(f"{SITE_TITLE} 启动成功，端口: 8080")
    serve(app, host='0.0.0.0', port=8080, threads=10, max_request_body_size=104857600)
