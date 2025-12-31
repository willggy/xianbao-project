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
    "农行": ["农行", "农业银行", "农","nh"],
    "工行": ["工行", "工商银行", "工","gh"],
    "建行": ["建行", "建设银行", "建", "CCB","jh"],
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
# 2. 核心辅助工具 (修复 NameError)
# ==========================================

# 登录验证装饰器
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('is_logged_in'):
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

# 数据库连接与并发优化
def get_db_connection():
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

# 记录访问 (过滤 Zeabur 监控)
def record_visit():
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

# HTML 清洗
def clean_html(html_content, site_key):
    if not html_content: return ""
    soup = BeautifulSoup(html_content, "html.parser")
    for tag in soup.find_all(True):
        if tag.name == 'img':
            src = tag.get('src', '')
            if src.startswith('/'): src = SITES_CONFIG[site_key]['domain'] + src
            tag.attrs = {'src': f"/img_proxy?url={src}", 'loading': 'lazy', 'style': 'max-width:100%; border-radius:8px;'}
        elif tag.name == 'a':
            tag.attrs = {'href': tag.get('href'), 'target': '_blank'}
    return str(soup)

# 上传图片到图床
def upload_to_img_cdn(file_binary):
    try:
        url = 'https://img.scdn.io/api/v1.php'
        files = {'image': ('upload.jpg', file_binary)}
        res = requests.post(url, files=files, data={'cdn_domain': 'img.scdn.io'}, timeout=30)
        if res.status_code == 200:
            js = res.json()
            if 'url' in js: return js['url']
            if 'data' in js and isinstance(js['data'], dict): return js['data'].get('url')
    except: pass
    return None

# ==========================================
# 3. 核心抓取与 4天自动清理
# ==========================================
def scrape_all_sites():
    if scrape_lock.locked(): return
    with scrape_lock:
        start_time = time.time()
        now_beijing = datetime.utcnow() + timedelta(hours=8)
        conn = get_db_connection()
        site_stats = {}

        for site_key, config in SITES_CONFIG.items():
            try:
                resp = session_http.get(config['list_url'], timeout=15)
                resp.encoding = 'utf-8'
                soup = BeautifulSoup(resp.text, "html.parser")
                
                new_count = 0
                for item in soup.select(config['list_selector']):
                    a = item.select_one("a[href*='view'], a[href*='thread'], a[href*='post']") or item.find("a")
                    if not a: continue
                    
                    href = a.get("href", "")
                    full_url = href if href.startswith("http") else (config['domain'] + (href if href.startswith("/") else "/" + href))
                    title = a.get_text(strip=True)
                    
                    matched_kw = next((kw for kw in KEYWORDS if kw.lower() in title.lower()), None)
                    if not matched_kw: continue
                    
                    final_tag = matched_kw
                    for tag_name, val_list in BANK_KEYWORDS.items():
                        if matched_kw in val_list: final_tag = tag_name; break
                    
                    try:
                        conn.execute('INSERT OR IGNORE INTO articles (title, url, site_source, match_keyword, original_time) VALUES(?,?,?,?,?)',
                                     (title, full_url, site_key, final_tag, now_beijing.strftime("%H:%M")))
                        if conn.total_changes > 0: new_count += 1
                    except: pass
                site_stats[config['name']] = new_count
            except: pass

        # 4天自动清理 (保护 user)
        conn.execute("DELETE FROM articles WHERE site_source != 'user' AND updated_at < datetime('now', '-4 days')")
        conn.execute("DELETE FROM article_content WHERE url NOT IN (SELECT url FROM articles)")
        conn.execute("DELETE FROM scrape_log WHERE id NOT IN (SELECT id FROM scrape_log ORDER BY id DESC LIMIT 50)")
        
        log_msg = f"[{now_beijing.strftime('%Y-%m-%d %H:%M:%S')}] 任务完成: {site_stats}"
        conn.execute('INSERT INTO scrape_log(last_scrape) VALUES(?)', (log_msg,))
        conn.commit()
        conn.close()

# ==========================================
# 4. 路由逻辑
# ==========================================

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['is_logged_in'] = True
            return redirect(url_for('admin_panel'))
    return render_template('login.html')

@app.route('/')
def index():
    record_visit()
    tag, q, page = request.args.get('tag'), request.args.get('q'), request.args.get('page', 1, type=int)
    conn = get_db_connection()
    where = "WHERE 1=1"
    params = []
    if tag: where += " AND match_keyword = ?"; params.append(tag)
    if q: where += " AND title LIKE ?"; params.append(f"%{q}%")
    
    articles = conn.execute(f'SELECT * FROM articles {where} ORDER BY is_top DESC, id DESC LIMIT ? OFFSET ?', 
                            params + [PER_PAGE, (page-1)*PER_PAGE]).fetchall()
    total = conn.execute(f'SELECT COUNT(*) FROM articles {where}', params).fetchone()[0]
    conn.close()
    return render_template('index.html', articles=articles, site_title=SITE_TITLE, bank_list=list(BANK_KEYWORDS.keys()), 
                           current_tag=tag, q=q, current_page=page, total_pages=(total+PER_PAGE-1)//PER_PAGE)

@app.route("/view")
def view():
    article_id = request.args.get("id", type=int)
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
    if not row: return "内容不存在", 404
    
    url, site_key, title = row["url"], row["site_source"], row["title"]
    cached = conn.execute("SELECT content FROM article_content WHERE url=?", (url,)).fetchone()
    content = ""

    if cached and cached['content']:
        content = cached["content"] if site_key == "user" else clean_html(cached["content"], site_key)
    elif site_key in SITES_CONFIG:
        try:
            r = session_http.get(url, timeout=10)
            soup = BeautifulSoup(r.text, "html.parser")
            node = soup.select_one(SITES_CONFIG[site_key]["content_selector"].split(',')[0])
            if node:
                conn.execute("INSERT OR REPLACE INTO article_content(url, content) VALUES(?,?)", (url, str(node)))
                conn.commit()
                content = clean_html(str(node), site_key)
        except: content = "加载原文失败"
    
    conn.close()
    return render_template("detail.html", title=title, content=content, original_url=url, time=row['original_time'])

@app.route('/publish', methods=['GET', 'POST'])
@login_required
def publish():
    if request.method == 'POST':
        title = request.form.get('title')
        raw_content = request.form.get('content')
        is_top = 1 if request.form.get('publish_mode') == 'top' else 0
        def img_replacer(match):
            cdn = upload_to_img_cdn(base64.b64decode(match.group(2)))
            return f'src="{cdn}"' if cdn else match.group(0)
        processed = re.sub(r'src="data:image\/(.*?);base64,(.*?)"', img_replacer, raw_content)
        fake_url = f"user://{int(time.time())}"
        conn = get_db_connection()
        conn.execute("INSERT INTO articles (title, url, site_source, match_keyword, original_time, is_top) VALUES (?,?,?,?,?,?)",
                     (title, fake_url, "user", "羊毛精选", "刚刚", is_top))
        conn.execute("INSERT INTO article_content (url, content) VALUES (?,?)", (fake_url, processed))
        conn.commit(); conn.close()
        return redirect('/')
    return render_template('publish.html')

@app.route('/admin')
@login_required
def admin_panel():
    conn = get_db_connection()
    my_articles = conn.execute("SELECT * FROM articles WHERE site_source='user' ORDER BY id DESC").fetchall()
    stats = {
        'total': conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0],
        'visits': conn.execute("SELECT SUM(visit_count) FROM visit_stats").fetchone()[0] or 0
    }
    conn.close()
    return render_template('admin.html', my_articles=my_articles, stats=stats)

@app.route('/article/delete/<int:aid>')
@login_required
def delete_article(aid):
    conn = get_db_connection()
    row = conn.execute("SELECT url FROM articles WHERE id=? AND site_source='user'", (aid,)).fetchone()
    if row:
        conn.execute("DELETE FROM articles WHERE id=?", (aid,))
        conn.execute("DELETE FROM article_content WHERE url=?", (row['url'],))
        conn.commit()
    conn.close()
    return redirect('/admin')

@app.route('/logs')
@login_required
def show_logs():
    conn = get_db_connection()
    logs = conn.execute('SELECT last_scrape FROM scrape_log ORDER BY id DESC LIMIT 50').fetchall()
    visitors = conn.execute('SELECT * FROM visit_stats ORDER BY last_visit DESC LIMIT 30').fetchall()
    conn.close()
    return render_template('logs.html', logs=logs, visitors=visitors)

@app.route('/img_proxy')
def img_proxy():
    url = request.args.get('url')
    try:
        r = requests.get(url, headers=HEADERS, stream=True, timeout=10)
        return Response(r.content, content_type=r.headers.get('Content-Type'))
    except: return Response(status=404)

@app.route('/logout')
def logout():
    session.pop('is_logged_in', None)
    return redirect(url_for('index'))

# ==========================================
# 5. 启动入口
# ==========================================
if __name__ == '__main__':
    get_db_connection().close()
    scheduler = BackgroundScheduler()
    scheduler.add_job(scrape_all_sites, 'interval', minutes=5)
    scheduler.start()
    threading.Thread(target=scrape_all_sites).start()
    serve(app, host='0.0.0.0', port=8080, threads=10, max_request_body_size=104857600)
