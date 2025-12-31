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

# 采集源配置
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

# 银行自动分类逻辑 (保留别名映射)
BANK_KEYWORDS = {
    "农行": ["农行", "农业银行", "农", "nh"],
    "工行": ["工行", "工商银行", "工", "gh"],
    "建行": ["建行", "建设银行", "建", "CCB", "jh"],
    "中行": ["中行", "中国银行", "中hang"]
}
ALL_BANK_VALS = [word for words in BANK_KEYWORDS.values() for word in words]

# 数据库路径 (适配 Zeabur)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "xianbao.db")

PER_PAGE = 30
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"}

session_http = requests.Session()
session_http.headers.update(HEADERS)
adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=2)
session_http.mount('http://', adapter)
session_http.mount('https://', adapter)

scrape_lock = threading.Lock()

# ==========================================
# 2. 数据库与权限
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
    
    # 文章表
    conn.execute('''CREATE TABLE IF NOT EXISTS articles(
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        title TEXT, url TEXT UNIQUE, site_source TEXT,
        match_keyword TEXT, original_time TEXT, is_top INTEGER DEFAULT 0,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # 动态规则表
    conn.execute('''CREATE TABLE IF NOT EXISTS config_rules(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_type TEXT,  -- 'white' or 'black'
        keyword TEXT,
        match_scope TEXT DEFAULT 'title', -- 'title' or 'url'
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(keyword, match_scope))''')
    
    conn.execute('CREATE TABLE IF NOT EXISTS article_content(url TEXT PRIMARY KEY, content TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
    conn.execute('CREATE TABLE IF NOT EXISTS scrape_log(id INTEGER PRIMARY KEY AUTOINCREMENT, last_scrape TEXT)')
    conn.execute('CREATE TABLE IF NOT EXISTS visit_stats(ip TEXT PRIMARY KEY, visit_count INTEGER DEFAULT 1, last_visit TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
    
    # 初始化规则
    cursor = conn.cursor()
    if cursor.execute("SELECT COUNT(*) FROM config_rules").fetchone()[0] == 0:
        defaults = ["立减金", "红包", "话费", "水", "毛", "招", "信", "移动", "联通", "京东", "支付宝", "微信", "流量", "充值", "zfb"]
        cursor.executemany("INSERT OR IGNORE INTO config_rules (rule_type, keyword, match_scope) VALUES (?, ?, ?)", 
                           [('white', w, 'title') for w in defaults])
        conn.commit()

    conn.commit()
    return conn

def record_visit():
    ua = request.headers.get('User-Agent', '')
    if 'HealthCheck' in ua or 'Zeabur' in ua: return
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    try:
        conn = get_db_connection()
        conn.execute('''INSERT INTO visit_stats (ip, visit_count, last_visit) VALUES (?, 1, CURRENT_TIMESTAMP)
                     ON CONFLICT(ip) DO UPDATE SET visit_count = visit_count + 1, last_visit = CURRENT_TIMESTAMP''', (ip,))
        conn.commit()
        conn.close()
    except: pass

# ==========================================
# 3. 核心抓取逻辑 (规则匹配)
# ==========================================
def scrape_all_sites():
    if scrape_lock.locked(): return
    with scrape_lock:
        now_beijing = datetime.utcnow() + timedelta(hours=8)
        conn = get_db_connection()
        
        # 加载动态规则
        rules = conn.execute("SELECT * FROM config_rules").fetchall()
        title_white = [r['keyword'] for r in rules if r['rule_type']=='white' and r['match_scope']=='title']
        title_black = [r['keyword'] for r in rules if r['rule_type']=='black' and r['match_scope']=='title']
        url_white   = [r['keyword'] for r in rules if r['rule_type']=='white' and r['match_scope']=='url']
        url_black   = [r['keyword'] for r in rules if r['rule_type']=='black' and r['match_scope']=='url']
        
        base_keywords = ALL_BANK_VALS + title_white
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
                    
                    # 动态黑名单过滤
                    if any(bad in full_url for bad in url_black): continue
                    if any(bad in title for bad in title_black): continue
                    
                    final_tag = None
                    # 网址白名单匹配
                    if any(good in full_url for good in url_white):
                        final_tag = "特别关注"
                    
                    # 标题关键词匹配
                    if not final_tag:
                        matched_kw = next((kw for kw in base_keywords if kw.lower() in title.lower()), None)
                        if matched_kw:
                            final_tag = matched_kw
                            for tag_name, val_list in BANK_KEYWORDS.items():
                                if matched_kw in val_list: final_tag = tag_name; break
                    
                    if not final_tag: continue
                    
                    try:
                        conn.execute('INSERT OR IGNORE INTO articles (title, url, site_source, match_keyword, original_time) VALUES(?,?,?,?,?)',
                                     (title, full_url, site_key, final_tag, now_beijing.strftime("%H:%M")))
                        if conn.total_changes > 0: new_count += 1
                    except: pass
                site_stats[config['name']] = new_count
            except: pass

        # 清理 4 天前旧数据
        conn.execute("DELETE FROM articles WHERE site_source != 'user' AND updated_at < datetime('now', '-4 days')")
        conn.execute("DELETE FROM article_content WHERE url NOT IN (SELECT url FROM articles)")
        conn.commit()
        
        log_msg = f"[{now_beijing.strftime('%Y-%m-%d %H:%M:%S')}] 任务完成: {site_stats}"
        conn.execute('INSERT INTO scrape_log(last_scrape) VALUES(?)', (log_msg,))
        conn.commit(); conn.close()

# ==========================================
# 4. 路由与 API
# ==========================================
@app.route('/')
def index():
    record_visit()
    
    # --- 🕒 新增：计算下次刷新时间逻辑 ---
    # 获取当前北京时间
    now = datetime.utcnow() + timedelta(hours=8)
    # 计算距离下一个 5 分钟整点还有几分钟
    remain_mins = 5 - (now.minute % 5)
    next_refresh_dt = now + timedelta(minutes=remain_mins)
    # 格式化为 20:05 这种形式
    next_refresh_time = next_refresh_dt.strftime("%H:%M")
    # -------------------------------

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

    return render_template('index.html', 
                           articles=articles, 
                           site_title=SITE_TITLE, 
                           bank_list=list(BANK_KEYWORDS.keys()), 
                           current_tag=tag, 
                           q=q, 
                           current_page=page, 
                           total_pages=(total+PER_PAGE-1)//PER_PAGE,
                           next_refresh_time=next_refresh_time) # 关键：传给前端

@app.route('/admin')
@login_required
def admin_panel():
    conn = get_db_connection()
    whitelist = conn.execute("SELECT * FROM config_rules WHERE rule_type='white' ORDER BY match_scope DESC").fetchall()
    blacklist = conn.execute("SELECT * FROM config_rules WHERE rule_type='black' ORDER BY match_scope DESC").fetchall()
    my_articles = conn.execute("SELECT * FROM articles WHERE site_source='user' ORDER BY id DESC").fetchall()
    
    # 统计数据
    total_articles = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    total_visits = conn.execute("SELECT SUM(visit_count) FROM visit_stats").fetchone()[0] or 0
    last_log = conn.execute("SELECT last_scrape FROM scrape_log ORDER BY id DESC LIMIT 1").fetchone()
    last_update = last_log[0] if last_log else "暂无记录"

    conn.close()
    return render_template('admin.html', whitelist=whitelist, blacklist=blacklist, my_articles=my_articles, 
                           stats={'total_articles': total_articles, 'total_visits': total_visits, 'last_update': last_update})

@app.route('/api/rule', methods=['POST'])
@login_required
def api_rule():
    action = request.form.get('action')
    rtype = request.form.get('type')  # white/black
    scope = request.form.get('scope') # title/url
    kw = request.form.get('keyword', '').strip()
    rid = request.form.get('id')
    
    conn = get_db_connection()
    if action == 'add' and kw:
        try: conn.execute("INSERT INTO config_rules (rule_type, keyword, match_scope) VALUES (?, ?, ?)", (rtype, kw, scope))
        except: pass
    elif action == 'delete' and rid:
        conn.execute("DELETE FROM config_rules WHERE id=?", (rid,))
    conn.commit(); conn.close()
    return redirect('/admin')

@app.route('/api/scrape_now')
@login_required
def api_scrape_now():
    threading.Thread(target=scrape_all_sites).start()
    return redirect('/admin')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['is_logged_in'] = True
            return redirect('/admin')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear(); return redirect('/')

# (此处建议保留 app (1).py 或 app (2).py 中的 /view, /publish, /img_proxy 等功能路由代码)

# ==========================================
# 5. 启动
# ==========================================
if __name__ == '__main__':
    get_db_connection().close()
    scheduler = BackgroundScheduler()
    scheduler.add_job(scrape_all_sites, 'interval', minutes=5)
    scheduler.start()
    threading.Thread(target=scrape_all_sites).start()
    serve(app, host='0.0.0.0', port=8080, threads=10, max_request_body_size=104857600)
