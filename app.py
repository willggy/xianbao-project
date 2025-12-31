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

SITE_TITLE = "古希腊掌管羊毛的神"
app.secret_key = os.environ.get('SECRET_KEY', 'xianbao_secret_key_888') 
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '123')  
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024 

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
    "农行": ["农行", "农业银行", "农", "nh"],
    "工行": ["工行", "工商银行", "工", "gh"],
    "建行": ["建行", "建设银行", "建", "CCB", "jh"],
    "中行": ["中行", "中国银行", "中hang"]
}
ALL_BANK_VALS = [word for words in BANK_KEYWORDS.values() for word in words]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "xianbao.db")

PER_PAGE = 30
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"}

session_req = requests.Session()
session_req.headers.update(HEADERS)
adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=2)
session_req.mount('http://', adapter)
session_req.mount('https://', adapter)

scrape_lock = threading.Lock()

# ==========================================
# 2. 数据库与工具函数
# ==========================================

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('is_logged_in'):
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL;')
    
    conn.execute('''CREATE TABLE IF NOT EXISTS articles(
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        title TEXT, url TEXT UNIQUE, site_source TEXT,
        match_keyword TEXT, original_time TEXT, is_top INTEGER DEFAULT 0,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    conn.execute('''CREATE TABLE IF NOT EXISTS config_rules(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_type TEXT, keyword TEXT, match_scope TEXT DEFAULT 'title',
        UNIQUE(keyword, match_scope))''')
    
    conn.execute('CREATE TABLE IF NOT EXISTS article_content(url TEXT PRIMARY KEY, content TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
    conn.execute('CREATE TABLE IF NOT EXISTS scrape_log(id INTEGER PRIMARY KEY AUTOINCREMENT, last_scrape TEXT)')
    conn.execute('CREATE TABLE IF NOT EXISTS visit_stats(ip TEXT PRIMARY KEY, visit_count INTEGER DEFAULT 1, last_visit TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
    
    conn.commit()
    return conn

def clean_html(html_content, site_key):
    if not html_content: return ""
    soup = BeautifulSoup(html_content, "html.parser")
    for tag in soup.find_all(True):
        if tag.name == 'img':
            src = tag.get('src', '')
            if src.startswith('/'): src = SITES_CONFIG.get(site_key, {}).get('domain', '') + src
            tag.attrs = {'src': f"/img_proxy?url={src}", 'style': 'max-width:100%; border-radius:12px; height:auto;'}
        elif tag.name == 'a':
            tag.attrs = {'href': tag.get('href'), 'target': '_blank'}
    return str(soup)

def record_visit():
    ua = request.headers.get('User-Agent', '')
    if 'HealthCheck' in ua or 'Zeabur' in ua: return
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    try:
        conn = get_db_connection()
        conn.execute('''INSERT INTO visit_stats (ip, visit_count, last_visit) VALUES (?, 1, CURRENT_TIMESTAMP)
                     ON CONFLICT(ip) DO UPDATE SET visit_count = visit_count + 1, last_visit = CURRENT_TIMESTAMP''', (ip,))
        conn.commit(); conn.close()
    except: pass

# ==========================================
# 3. 核心路由
# ==========================================

@app.route('/')
def index():
    record_visit()
    now = datetime.utcnow() + timedelta(hours=8)
    next_refresh_time = (now + timedelta(minutes=5 - (now.minute % 5))).strftime("%H:%M")

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
                           current_tag=tag, q=q, current_page=page, total_pages=(total+PER_PAGE-1)//PER_PAGE,
                           next_refresh_time=next_refresh_time)

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
            r = session_req.get(url, timeout=10)
            r.encoding = 'utf-8'
            soup = BeautifulSoup(r.text, "html.parser")
            node = soup.select_one(SITES_CONFIG[site_key]["content_selector"].split(',')[0])
            if node:
                conn.execute("INSERT OR REPLACE INTO article_content(url, content) VALUES(?,?)", (url, str(node)))
                conn.commit()
                content = clean_html(str(node), site_key)
        except: content = "加载原文失败"
    conn.close()
    return render_template("detail.html", title=title, content=content, original_url=url, time=row['original_time'])

@app.route('/admin')
@login_required
def admin_panel():
    conn = get_db_connection()
    whitelist = conn.execute("SELECT * FROM config_rules WHERE rule_type='white'").fetchall()
    blacklist = conn.execute("SELECT * FROM config_rules WHERE rule_type='black'").fetchall()
    my_articles = conn.execute("SELECT * FROM articles WHERE site_source='user' ORDER BY id DESC").fetchall()
    
    last_log = conn.execute('SELECT last_scrape FROM scrape_log ORDER BY id DESC LIMIT 1').fetchone()
    last_update = last_log[0] if last_log else "暂无记录"
    
    stats = {'total_articles': conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0],
             'total_visits': conn.execute("SELECT SUM(visit_count) FROM visit_stats").fetchone()[0] or 0,
             'last_update': last_update}
    conn.close()
    return render_template('admin.html', whitelist=whitelist, blacklist=blacklist, my_articles=my_articles, stats=stats)

# --- 🔒 发布新文章 ---
@app.route('/publish', methods=['GET', 'POST'])
@login_required
def publish():
    if request.method == 'POST':
        title = request.form.get('title')
        raw_content = request.form.get('content')
        is_top = 1 if request.form.get('publish_mode') == 'top' else 0
        
        # 只处理 Base64 图片上传
        def img_replacer(match):
            try:
                cdn = upload_to_img_cdn(base64.b64decode(match.group(2)))
                return f'src="{cdn}"' if cdn else match.group(0)
            except: return match.group(0)
        
        processed = re.sub(r'src="data:image\/(.*?);base64,(.*?)"', img_replacer, raw_content)
        fake_url = f"user://{int(time.time())}"
        
        conn = get_db_connection()
        conn.execute("INSERT INTO articles (title, url, site_source, match_keyword, original_time, is_top) VALUES (?,?,?,?,?,?)",
                     (title, fake_url, "user", "羊毛精选", "刚刚", is_top))
        conn.execute("INSERT INTO article_content (url, content) VALUES (?,?)", (fake_url, processed))
        conn.commit()
        conn.close()
        return redirect('/')
    return render_template('publish.html')
# --- 🔒 编辑文章 ---
@app.route('/article/edit/<int:aid>', methods=['GET', 'POST'])
@login_required
def edit_article(aid):
    conn = get_db_connection()
    
    if request.method == 'POST':
        title = request.form.get('title')
        raw_content = request.form.get('content')
        is_top = 1 if request.form.get('publish_mode') == 'top' else 0
        
        # 只上传新粘贴的 Base64 图片
        def img_replacer(match):
            try:
                cdn = upload_to_img_cdn(base64.b64decode(match.group(2)))
                return f'src="{cdn}"' if cdn else match.group(0)
            except: return match.group(0)
            
        processed = re.sub(r'src="data:image\/(.*?);base64,(.*?)"', img_replacer, raw_content)
        
        row = conn.execute("SELECT url FROM articles WHERE id=?", (aid,)).fetchone()
        if row:
            conn.execute("UPDATE articles SET title=?, is_top=? WHERE id=?", (title, is_top, aid))
            conn.execute("UPDATE article_content SET content=? WHERE url=?", (processed, row['url']))
            conn.commit()
        
        conn.close()
        return redirect('/admin')

    article = conn.execute("SELECT * FROM articles WHERE id=? AND site_source='user'", (aid,)).fetchone()
    if not article: return "未找到文章", 404
    
    content = conn.execute("SELECT content FROM article_content WHERE url=?", (article['url'],)).fetchone()['content']
    conn.close()
    return render_template('edit.html', article=article, content=content)


# --- 置顶切换 ---
@app.route('/article/top/<int:aid>')
@login_required
def toggle_top(aid):
    conn = get_db_connection()
    conn.execute("UPDATE articles SET is_top = 1 - is_top WHERE id=?", (aid,))
    conn.commit(); conn.close()
    return redirect('/admin')

# --- 删除文章 ---
@app.route('/article/delete/<int:aid>')
@login_required
def delete_article(aid):
    conn = get_db_connection()
    row = conn.execute("SELECT url FROM articles WHERE id=?", (aid,)).fetchone()
    if row:
        conn.execute("DELETE FROM articles WHERE id=?", (aid,))
        conn.execute("DELETE FROM article_content WHERE url=?", (row['url'],))
        conn.commit()
    conn.close()
    return redirect('/admin')

# --- 系统日志与访客 ---
@app.route('/logs')
@login_required
def show_logs():
    conn = get_db_connection()
    logs = conn.execute('SELECT last_scrape FROM scrape_log ORDER BY id DESC LIMIT 50').fetchall()
    visitors = conn.execute('SELECT * FROM visit_stats ORDER BY last_visit DESC LIMIT 30').fetchall()
    conn.close()
    return render_template('logs.html', logs=logs, visitors=visitors)

# --- 规则 API (添加/删除筛选词) ---
@app.route('/api/rule', methods=['POST'])
@login_required
def api_rule():
    action, rtype, scope, kw, rid = request.form.get('action'), request.form.get('type'), request.form.get('scope'), request.form.get('keyword', '').strip(), request.form.get('id')
    conn = get_db_connection()
    if action == 'add' and kw:
        try: conn.execute("INSERT INTO config_rules (rule_type, keyword, match_scope) VALUES (?, ?, ?)", (rtype, kw, scope))
        except: pass
    elif action == 'delete' and rid:
        conn.execute("DELETE FROM config_rules WHERE id=?", (rid,))
    conn.commit(); conn.close()
    return redirect('/admin')

@app.route('/img_proxy')
def img_proxy():
    url = request.args.get('url')
    try:
        r = session_req.get(url, headers=HEADERS, stream=True, timeout=10)
        return Response(r.content, content_type=r.headers.get('Content-Type'))
    except: return Response(status=404)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST' and request.form.get('password') == ADMIN_PASSWORD:
        session['is_logged_in'] = True
        return redirect('/admin')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear(); return redirect('/')

# ==========================================
# 4. 抓取与启动
# ==========================================

def scrape_all_sites():
    if scrape_lock.locked(): return
    with scrape_lock:
        now_beijing = (datetime.utcnow() + timedelta(hours=8))
        conn = get_db_connection()
        rules = conn.execute("SELECT * FROM config_rules").fetchall()
        title_white = [r['keyword'] for r in rules if r['rule_type']=='white' and r['match_scope']=='title']
        title_black = [r['keyword'] for r in rules if r['rule_type']=='black' and r['match_scope']=='title']
        url_black   = [r['keyword'] for r in rules if r['rule_type']=='black' and r['match_scope']=='url']
        
        base_keywords = ALL_BANK_VALS + title_white
        stats = {}

        for skey, cfg in SITES_CONFIG.items():
            try:
                r = session_req.get(cfg['list_url'], timeout=15)
                soup = BeautifulSoup(r.text, "html.parser")
                count = 0
                for item in soup.select(cfg['list_selector']):
                    a = item.select_one("a[href*='view'], a[href*='thread'], a[href*='post']") or item.find("a")
                    if not a: continue
                    t, h = a.get_text(strip=True), a.get("href", "")
                    url = h if h.startswith("http") else (cfg['domain'] + (h if h.startswith("/") else "/" + h))
                    
                    if any(b in url for b in url_black) or any(b in t for b in title_black): continue
                    
                    kw = next((k for k in base_keywords if k.lower() in t.lower()), None)
                    if kw:
                        tag = kw
                        for b_name, b_v in BANK_KEYWORDS.items():
                            if kw in b_v: tag = b_name; break
                        conn.execute('INSERT OR IGNORE INTO articles (title, url, site_source, match_keyword, original_time) VALUES(?,?,?,?,?)',
                                     (t, url, skey, tag, now_beijing.strftime("%H:%M")))
                        if conn.total_changes > 0: count += 1
                stats[cfg['name']] = count
            except: pass
        
        conn.execute("DELETE FROM articles WHERE site_source != 'user' AND updated_at < datetime('now', '-4 days')")
        conn.execute('INSERT INTO scrape_log(last_scrape) VALUES(?)', (f"[{now_beijing.strftime('%m-%d %H:%M')}] {stats}",))
        conn.commit(); conn.close()

if __name__ == '__main__':
    get_db_connection().close()
    scheduler = BackgroundScheduler()
    scheduler.add_job(scrape_all_sites, 'interval', minutes=5)
    scheduler.start()
    threading.Thread(target=scrape_all_sites).start()
    serve(app, host='0.0.0.0', port=8080, threads=10)

