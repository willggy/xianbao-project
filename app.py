import os
import sqlite3
import threading
import time
import base64
import re
import json
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

# --- 安全配置 ---
# 必须修改此密钥，否则 Session 无法使用 (用于登录验证)
app.secret_key = os.environ.get('SECRET_KEY', 'local_dev_secret_key_x82ns@!09zx') 
# 后台管理密码
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '123')  

# --- 上传限制 ---
# 允许最大请求体 100MB (防止上传大图报错)
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

# --- 银行关键词 (硬编码，因涉及别名映射) ---
BANK_KEYWORDS = {
    "农行": ["农行", "农业银行", "农"],
    "工行": ["工行", "工商银行", "工"],
    "建行": ["建行", "建设银行", "建", "CCB"],
    "中行": ["中行", "中国银行", "中hang"]
}
ALL_BANK_VALS = [word for words in BANK_KEYWORDS.values() for word in words]

# --- 路径配置 (适配 Zeabur/Docker) ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "/app/data")
if not os.path.exists(DATA_DIR): 
    os.makedirs(DATA_DIR)
DB_PATH = os.path.join(DATA_DIR, "xianbao.db")

# --- 全局变量 ---
PER_PAGE = 30
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"}
last_scrape_time = 0
COOLDOWN_SECONDS = 30
scrape_lock = threading.Lock()

# 网络请求优化
session_req = requests.Session()
session_req.headers.update(HEADERS)
adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=2)
session_req.mount('http://', adapter)
session_req.mount('https://', adapter)

# ==========================================
# 2. 辅助工具 & 数据库
# ==========================================

# 登录验证装饰器
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('is_logged_in'):
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

# 数据库连接与初始化
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL;')
    
    # 文章表
    conn.execute('''CREATE TABLE IF NOT EXISTS articles(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT, url TEXT UNIQUE, site_source TEXT,
        match_keyword TEXT, original_time TEXT, is_top INTEGER DEFAULT 0,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # 内容缓存表
    conn.execute('CREATE TABLE IF NOT EXISTS article_content(url TEXT PRIMARY KEY, content TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
    
    # 日志表
    conn.execute('CREATE TABLE IF NOT EXISTS scrape_log(id INTEGER PRIMARY KEY AUTOINCREMENT, last_scrape TEXT)')
    conn.execute('CREATE TABLE IF NOT EXISTS visit_stats(ip TEXT PRIMARY KEY, visit_count INTEGER DEFAULT 1, last_visit TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')

    # 规则表 (支持 match_scope 区分标题和网址)
    conn.execute('''CREATE TABLE IF NOT EXISTS config_rules(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_type TEXT,  -- 'white' or 'black'
        keyword TEXT,
        match_scope TEXT DEFAULT 'title', -- 'title' or 'url'
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(keyword, match_scope)
    )''')
    
    # --- 数据库迁移检查 (兼容旧版本) ---
    try:
        conn.execute("ALTER TABLE config_rules ADD COLUMN match_scope TEXT DEFAULT 'title'")
    except sqlite3.OperationalError:
        pass # 列已存在，忽略

    # --- 初始化默认规则 ---
    cursor = conn.cursor()
    if cursor.execute("SELECT COUNT(*) FROM config_rules").fetchone()[0] == 0:
        defaults = ["立减金", "红包", "话费", "大水", "小水", "有水", "毛", "招", "hang", "信", "移动", "联通",  "支付宝", "微信", "流量", "话费券", "充值", "zfb"]
        # 默认只添加标题白名单
        cursor.executemany("INSERT OR IGNORE INTO config_rules (rule_type, keyword, match_scope) VALUES (?, ?, ?)", 
                           [('white', w, 'title') for w in defaults])
        # 默认添加几个网址黑名单
        cursor.executemany("INSERT OR IGNORE INTO config_rules (rule_type, keyword, match_scope) VALUES (?, ?, ?)", 
                           [('black', 'loans', 'url'), ('black', 'google_ads', 'url')])
        conn.commit()

    conn.commit()
    return conn

# 记录访问 IP
def record_visit():
    try:
        ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        conn = get_db_connection()
        conn.execute('''INSERT INTO visit_stats (ip, visit_count, last_visit) VALUES (?, 1, CURRENT_TIMESTAMP)
                     ON CONFLICT(ip) DO UPDATE SET visit_count = visit_count + 1, last_visit = CURRENT_TIMESTAMP''', (ip,))
        conn.commit()
        conn.close()
    except: pass

# HTML 清洗 (处理图片防盗链)
def clean_html(html_content, site_key):
    if not html_content: return ""
    soup = BeautifulSoup(html_content, "html.parser")
    for tag in soup.find_all(True):
        if tag.name == 'img':
            src = tag.get('src', '')
            if src.startswith('/'): src = SITES_CONFIG[site_key]['domain'] + src
            # 改为代理地址
            tag.attrs = {'src': f"/img_proxy?url={src}", 'loading': 'lazy', 'style': 'max-width:100%; border-radius:8px; display:block; margin:10px 0;'}
        elif tag.name == 'a':
            tag.attrs = {'href': tag.get('href'), 'target': '_blank', 'style': 'color: #007aff; text-decoration: none;'}
    return str(soup)

# 上传图片到 img.scdn.io
def upload_to_img_cdn(file_binary):
    try:
        url = 'https://img.scdn.io/api/v1.php'
        files = {'image': ('upload.jpg', file_binary)}
        data = {'cdn_domain': 'img.scdn.io'}
        res = requests.post(url, files=files, data=data, timeout=30)
        
        if res.status_code == 200:
            js = res.json()
            # 兼容多种返回格式
            if 'url' in js: return js['url']
            if 'data' in js and isinstance(js['data'], dict) and 'url' in js['data']: return js['data']['url']
            if 'data' in js and isinstance(js['data'], str) and js['data'].startswith('http'): return js['data']
        print(f"图床上传失败: {res.text}")
    except Exception as e: 
        print(f"图床异常: {e}")
    return None

# ==========================================
# 3. 核心采集逻辑
# ==========================================
def scrape_all_sites():
    if scrape_lock.locked(): return
    with scrape_lock:
        start_time = time.time()
        conn = get_db_connection()
        
        # 1. 加载规则
        rules = conn.execute("SELECT * FROM config_rules").fetchall()
        title_white = [r['keyword'] for r in rules if r['rule_type']=='white' and r['match_scope']=='title']
        title_black = [r['keyword'] for r in rules if r['rule_type']=='black' and r['match_scope']=='title']
        url_white   = [r['keyword'] for r in rules if r['rule_type']=='white' and r['match_scope']=='url']
        url_black   = [r['keyword'] for r in rules if r['rule_type']=='black' and r['match_scope']=='url']
        
        base_title_keywords = ALL_BANK_VALS + title_white
        
        # === ✅ 新增：本次批次去重集合 ===
        # 这个集合只在本次函数运行期间有效，下次运行又会清空
        # 用于防止：线报库刚发了一条，爱猴也发了一条一样的，本次只收录一条
        current_batch_titles = set()
        
        site_stats = {}
        now_beijing = datetime.utcnow() + timedelta(hours=8)
        
        for site_key, config in SITES_CONFIG.items():
            try:
                session_req.headers.update({"Referer": config['domain']})
                resp = session_req.get(config['list_url'], timeout=15)
                resp.encoding = 'utf-8'
                soup = BeautifulSoup(resp.text, "html.parser")
                
                entries = []
                for item in soup.select(config['list_selector']):
                    a = item.select_one("a[href*='view'], a[href*='thread'], a[href*='post'], a[href*='.htm']") or item.find("a")
                    if not a: continue
                    
                    href = a.get("href", "")
                    full_url = href if href.startswith("http") else (config['domain'] + (href if href.startswith("/") else "/" + href))
                    title = a.get_text(strip=True)
                    
                    # === ✅ 核心逻辑：批次内去重 ===
                    # 1. 如果这个标题在"本次"抓取中已经出现过，跳过
                    if title in current_batch_titles:
                        continue
                        
                    # 2. 如果标题在"当前站点"的列表里重复了(防止置顶帖和普通贴重复)，跳过
                    if any(e[0] == title for e in entries):
                        continue
                    # ==============================
                    
                    # --- 下面是常规的黑白名单筛选 ---
                    
                    # URL 黑名单
                    if any(bad in full_url for bad in url_black): continue
                    
                    # 标题 黑名单
                    if any(bad in title for bad in title_black): continue
                    
                    final_tag = None
                    
                    # URL 白名单
                    if any(good in full_url for good in url_white):
                        final_tag = "特别关注"
                    
                    # 标题 白名单
                    if not final_tag:
                        matched_kw = next((kw for kw in base_title_keywords if kw.lower() in title.lower()), None)
                        if matched_kw:
                            final_tag = matched_kw
                            for tag_name, val_list in BANK_KEYWORDS.items():
                                if matched_kw in val_list:
                                    final_tag = tag_name
                                    break
                    
                    if not final_tag: continue
                    
                    # 通过所有检查，加入待插入列表
                    entries.append((title, full_url, site_key, final_tag, now_beijing.strftime("%H:%M")))
                    
                    # ✅ 将标题加入"已存在"集合，后续如果其他站点也有这个标题，就会被上面拦截
                    current_batch_titles.add(title)
                
                if entries:
                    conn.executemany('INSERT OR IGNORE INTO articles (title, url, site_source, match_keyword, original_time) VALUES(?,?,?,?,?)', entries)
                    site_stats[config['name']] = len(entries)
            except Exception as e:
                print(f"站点 {site_key} 抓取错误: {e}")
        
        # 日志记录
        stats_str = ", ".join([f"{k}+{v}" for k,v in site_stats.items()]) if site_stats else "无新内容"
        log_msg = f"[{now_beijing.strftime('%Y-%m-%d %H:%M:%S')}] 任务完成: {stats_str}"
        print(log_msg)
        
        conn.execute('INSERT INTO scrape_log(last_scrape) VALUES(?)', (log_msg,))
        conn.execute('DELETE FROM scrape_log WHERE id NOT IN (SELECT id FROM scrape_log ORDER BY id DESC LIMIT 50)')
        conn.commit()
        conn.close()

# ==========================================
# 4. Web 路由
# ==========================================
# ================== 1. 新增：手动抓取接口 ==================
@app.route('/api/scrape_now')
@login_required
def api_scrape_now():
    # 异步启动抓取，不阻塞页面
    threading.Thread(target=scrape_all_sites).start()
    # 稍微延迟一下，让用户感觉“已启动”
    time.sleep(1) 
    return redirect('/admin')
# --- 登录相关 ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['is_logged_in'] = True
            return redirect(request.args.get('next') or '/admin')
        else:
            return render_template('login.html', error="密码错误")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

# --- 首页 ---
@app.route('/')
def index():
    record_visit()
    tag, q, page = request.args.get('tag'), request.args.get('q'), request.args.get('page', 1, type=int)
    
    # 首页首次加载触发一次采集
    global last_scrape_time
    if page == 1 and not tag and not q:
        if time.time() - last_scrape_time > COOLDOWN_SECONDS:
            last_scrape_time = time.time()
            threading.Thread(target=scrape_all_sites).start()

    conn = get_db_connection()
    where = "WHERE 1=1"
    params = []
    if tag: 
        where += " AND match_keyword = ?"
        params.append(tag)
    if q: 
        where += " AND title LIKE ?"
        params.append(f"%{q}%")
    
    sql = f'SELECT * FROM articles {where} ORDER BY is_top DESC, id DESC LIMIT ? OFFSET ?'
    articles = conn.execute(sql, params + [PER_PAGE, (page-1)*PER_PAGE]).fetchall()
    total = conn.execute(f'SELECT COUNT(*) FROM articles {where}', params).fetchone()[0]
    conn.close()

    bank_list = list(BANK_KEYWORDS.keys())
    return render_template('index.html', articles=articles, current_page=page, total_pages=(total+PER_PAGE-1)//PER_PAGE, current_tag=tag, q=q, bank_list=bank_list)

# --- 文章详情 ---
@app.route("/view")
def view():
    article_id = request.args.get("id", type=int)
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
    
    if not row: 
        conn.close()
        return "内容不存在", 404
    
    url, site_key, title = row["url"], row["site_source"], row["title"]
    
    # 尝试读缓存
    cached = conn.execute("SELECT content FROM article_content WHERE url=?", (url,)).fetchone()
    content = ""
    
    if cached and cached['content']:
        # 用户发布的直接显示，采集的经过清洗
        content = cached["content"] if site_key == "user" else clean_html(cached["content"], site_key)
    elif site_key in SITES_CONFIG:
        # 缓存无数据，实时抓取
        try:
            r = session_req.get(url, timeout=10)
            r.encoding = 'utf-8'
            soup = BeautifulSoup(r.text, "html.parser")
            selectors = SITES_CONFIG[site_key]["content_selector"].split(',')
            node = None
            for sel in selectors:
                node = soup.select_one(sel.strip())
                if node: break
            
            if node:
                content_raw = str(node)
                conn.execute("INSERT OR REPLACE INTO article_content(url, content) VALUES(?,?)", (url, content_raw))
                conn.commit()
                content = clean_html(content_raw, site_key)
            else:
                content = f"<div class='alert alert-warning'>正文提取失败，<a href='{url}' target='_blank'>点击访问原文</a></div>"
        except Exception as e:
            content = f"加载失败: {e}"
    else:
        content = f"无法加载内容，<a href='{url}' target='_blank'>点击访问原文</a>"
        
    conn.close()
    return render_template("detail.html", title=title, content=content, original_url=url, time=row['original_time'])

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

# --- 🔒 后台管理面板 ---
@app.route('/admin')
@login_required
def admin_panel():
    conn = get_db_connection()
    
    # 1. 获取规则
    whitelist = conn.execute("SELECT * FROM config_rules WHERE rule_type='white' ORDER BY id DESC").fetchall()
    blacklist = conn.execute("SELECT * FROM config_rules WHERE rule_type='black' ORDER BY id DESC").fetchall()
    
    # 2. 获取文章列表
    my_articles = conn.execute("SELECT * FROM articles WHERE site_source='user' ORDER BY id DESC").fetchall()
    
    # 3. 获取统计数据 (新增)
    total_articles = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    total_visits = conn.execute("SELECT SUM(visit_count) FROM visit_stats").fetchone()[0] or 0
    
    # 获取最后抓取日志
    last_log = conn.execute("SELECT last_scrape FROM scrape_log ORDER BY id DESC LIMIT 1").fetchone()
    last_scrape_time = last_log[0].split(']')[0].replace('[', '') if last_log else "暂无记录"

    conn.close()
    
    return render_template('admin.html', 
                           whitelist=whitelist, 
                           blacklist=blacklist, 
                           my_articles=my_articles,
                           stats={
                               'total_articles': total_articles,
                               'total_visits': total_visits,
                               'last_update': last_scrape_time
                           })

# --- 🔒 规则管理 API ---
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
        try: 
            conn.execute("INSERT INTO config_rules (rule_type, keyword, match_scope) VALUES (?, ?, ?)", (rtype, kw, scope))
        except: pass
    elif action == 'delete' and rid:
        conn.execute("DELETE FROM config_rules WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    return redirect('/admin')

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

# --- 🔒 删除文章 ---
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

# --- 🔒 系统日志 ---
@app.route('/logs')
@login_required
def show_logs():
    conn = get_db_connection()
    logs = conn.execute('SELECT last_scrape FROM scrape_log ORDER BY id DESC LIMIT 50').fetchall()
    visitors = conn.execute('SELECT * FROM visit_stats ORDER BY last_visit DESC LIMIT 30').fetchall()
    conn.close()
    return render_template('logs.html', logs=logs, visitors=visitors)

# --- 图片代理 (防盗链) ---
@app.route('/img_proxy')
def img_proxy():
    url = request.args.get('url')
    if not url: return "", 404
    try:
        r = requests.get(url, headers=HEADERS, stream=True, timeout=10)
        return Response(r.content, content_type=r.headers.get('Content-Type'))
    except: return Response(status=404)

# ==========================================
# 5. 启动入口
# ==========================================
if __name__ == '__main__':
    # 初始化 DB
    get_db_connection().close()
    
    # 启动定时任务
    scheduler = BackgroundScheduler()
    scheduler.add_job(scrape_all_sites, 'interval', minutes=10)
    scheduler.start()
    
    # 启动时立即抓一次
    threading.Thread(target=scrape_all_sites).start()
    
    print("Waitress 服务器启动中: http://0.0.0.0:8080")
    # max_request_body_size 设置为 100MB，解决 Request Entity Too Large

    serve(app, host='0.0.0.0', port=8080, threads=10, max_request_body_size=104857600)
