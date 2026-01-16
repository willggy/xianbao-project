import os
import sqlite3
import threading
import time
import base64
import re
# 【修改1】引入 timezone 模块以支持新版时间标准
from datetime import datetime, timedelta, timezone
from functools import wraps, lru_cache
from urllib.parse import quote, unquote, urlparse
import requests
from requests.adapters import HTTPAdapter
from flask import Flask, flash, render_template, request, Response, redirect, session, url_for
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler
from waitress import serve

# ==========================================
# 1. 基础配置
# ==========================================
app = Flask(__name__)

# 密钥配置
SITE_TITLE = "古希腊掌管羊毛的神"
app.secret_key = os.environ.get('SECRET_KEY', 'xianbao_secret_key_888') 
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '123')  
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024 
CRON_SECRET = os.environ.get('CRON_SECRET', 'xianbao_secret_key_999')

# 站点配置
SITES_CONFIG = {
    "xianbao": { 
        "name": "线报库", 
        "domain": "https://new.xianbao.fun", 
        "list_url": "https://new.xianbao.fun/", 
        "list_selector": "#mainbox > div.listbox tr, #mainbox > div.listbox li", 
        "content_selector": "#mainbox article .article-content, #art-fujia, #mainbox > article > div.art-content > div.art-copyright.br > div:nth-child(1)"
    },
    "iehou": { 
        "name": "爱猴线报", 
        "domain": "https://iehou.com", 
        "list_url": "https://iehou.com/", 
        "list_selector": "#body ul li",
        "content_selector": ".thread-content"
    },
    "xianbao_icu": {
        "name": "鲸线报",  
        "domain": "https://xianbao.icu",
        "list_url": "https://xianbao.icu/xianbao",  
        "list_selector": "main div div div:nth-child(3) > div:nth-child(2) a, main a[href*='/xianbao/detail'], main a[href*='/detail'], ul li a[href*='/detail']",
        "content_selector": "main > div:nth-of-type(2) > div > div, .prose, .prose-max, .content, .entry-content, .post-body, .detail-body, .markdown, .article-detail, .text"
   }
}

# 银行关键词
BANK_KEYWORDS = {
    "农行": ["农行", "农业银行", "农", "nh"],
    "工行": ["工行", "工商银行", "工", "gh"],
    "建行": ["建行", "建设银行", "建", "CCB", "jh"],
    "中行": ["中行", "中国银行", "中hang"]
}
ALL_BANK_VALS = [word for words in BANK_KEYWORDS.values() for word in words]

# 数据库路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "xianbao.db")

PER_PAGE = 30
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": "https://www.google.com/"
}

# 网络请求 Session
session_req = requests.Session()
session_req.headers.update(HEADERS)
adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=1)
session_req.mount('http://', adapter)
session_req.mount('https://', adapter)

scrape_lock = threading.Lock()

# 【修改2】符合 Python 3.12+ 标准的北京时间获取函数
def get_beijing_now():
    # 1. 获取带时区信息的 UTC 时间 (datetime.now(timezone.utc))
    # 2. 转换为北京时区 (.astimezone(...))
    # 3. 移除时区信息 (.replace(tzinfo=None)) -> 变成“无时区”对象
    # 为什么要移除时区？因为你的数据库和后续的减法逻辑使用的是简单的数字计算，
    # 如果保留时区，Python 会报错 "can't subtract offset-naive and offset-aware datetimes"
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)

# 初始化活跃时间
LAST_ACTIVE_TIME = get_beijing_now()

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

def make_links_clickable(text):
    # 匹配 http/https URL，但排除已经在 href= 里的情况
    pattern = re.compile(r'(?<!href=")(https?://[^\s"<]+)', re.IGNORECASE)
    return pattern.sub(r'<a href="\1" target="_blank" rel="noopener noreferrer" class="content-link">\1</a>', text)

def clean_html(html_content, site_key):
    if not html_content:
        return ""

    soup = BeautifulSoup(html_content, "html.parser")

    for tag in soup.find_all(True):

        # ============================
        # 1) 图片处理逻辑
        # ============================
        if tag.name == 'img':
            src = tag.get('src', '').strip()
            if not src:
                continue

            # ---- 避免重复包装 /img_proxy ----
            if src.startswith("/img_proxy"):
                continue

            # ---- 补全各种相对路径 ----
            if src.startswith('//'):  # //img.xx.com/xx.jpg
                src = 'https:' + src

            elif src.startswith('/'):  # /upload/xxx.jpg
                src = SITES_CONFIG[site_key]['domain'] + src

            elif src.startswith('./'):  # ./images/xxx.jpg
                src = SITES_CONFIG[site_key]['domain'] + src[1:]

            elif src.startswith('../'):  # ../xx/xx.jpg
                src = SITES_CONFIG[site_key]['domain'] + src.replace('../', '', 1)

            # ---- 这里不做更多处理，否则容易误判 HTML 图片 ----

            # ---- URL 转义 + 走 img_proxy ----
            proxy_url = "/img_proxy?url=" + quote(src, safe='/:?=&')

            tag.attrs = {
                'src': proxy_url,
                'loading': 'lazy',
                'style': 'max-width:100%; height:auto; border-radius:8px; margin:10px 0;'
            }

        # ============================
        # 2) 链接处理逻辑
        # ============================
        elif tag.name == 'a':
            href = tag.get('href', '').strip()
            if not href:
                continue

            # ---- 避免自引用 /img_proxy ----
            if href.startswith('/img_proxy'):
                continue

            # ---- 补全相对路径 ----
            if href.startswith('//'):
                href = 'https:' + href
            elif href.startswith('/'):
                href = SITES_CONFIG[site_key]['domain'] + href

            # ---- 保留为正常蓝色链接 ----
            tag.attrs = {
                'href': href,
                'target': '_blank',
                'rel': 'noopener noreferrer',
                'style': 'color:#007aff; text-decoration:underline; word-break:break-all;'
            }

    return str(soup)



def record_visit():
    ua = request.headers.get('User-Agent', '')
    if 'HealthCheck' in ua or 'Zeabur' in ua: return
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    
    global LAST_ACTIVE_TIME
    LAST_ACTIVE_TIME = get_beijing_now()
    
    try:
        conn = get_db_connection()
        conn.execute('''INSERT INTO visit_stats (ip, visit_count, last_visit) VALUES (?, 1, CURRENT_TIMESTAMP)
                     ON CONFLICT(ip) DO UPDATE SET visit_count = visit_count + 1, last_visit = CURRENT_TIMESTAMP''', (ip,))
        conn.commit(); conn.close()
    except: pass

def upload_to_img_cdn(img_data):
    return f"data:image/png;base64,{base64.b64encode(img_data).decode()}"

# ==========================================
# 3. 核心路由
# ==========================================

@app.route('/')
def index():
    record_visit()
    now = get_beijing_now()

    next_min = ((now.minute // 10) + 1) * 10
    if next_min >= 60:
        next_refresh_obj = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    else:
        next_refresh_obj = now.replace(minute=next_min, second=0, microsecond=0)

    next_refresh_time = next_refresh_obj.strftime("%H:%M")

    tag = request.args.get('tag')
    q = request.args.get('q')
    page = request.args.get('page', 1, type=int)
    
    conn = get_db_connection()
    where = "WHERE 1=1"
    params = []
    if tag:
        where += " AND match_keyword = ?"
        params.append(tag)
    if q:
        where += " AND title LIKE ?"
        params.append(f"%{q}%")
    
    articles = conn.execute(f'SELECT * FROM articles {where} ORDER BY is_top DESC, id DESC LIMIT ? OFFSET ?', 
                            params + [PER_PAGE, (page-1)*PER_PAGE]).fetchall()
    
    total = conn.execute(f'SELECT COUNT(*) FROM articles {where}', params).fetchone()[0]
    conn.close()

    return render_template('index.html', 
                           articles=articles, 
                           next_refresh_time=next_refresh_time,
                           bank_list=list(BANK_KEYWORDS.keys()), 
                           current_tag=tag, 
                           q=q, 
                           current_page=page, 
                           total_pages=(total+PER_PAGE-1)//PER_PAGE)

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
            
            # 只针对鲸线报使用两个精确容器
            if site_key == "xianbao_icu":
                content_parts = []
                
                # 第一个容器：核心正文 .article-content（保留完整 HTML）
                node1 = soup.select_one('#__nuxt > div > section > main > div:nth-child(2) > div.el-col.el-col-24.el-col-xs-24.el-col-lg-16.is-guttered > div > div > div.article-content')
                if node1:
                    content_parts.append(str(node1))
                
                # 第二个容器：来源 / 其他补充（保留完整 HTML）
                node2 = soup.select_one('#__nuxt > div > section > main > div:nth-child(2) > div.el-col.el-col-24.el-col-xs-24.el-col-lg-16.is-guttered > div > div > div:nth-child(6) > div > div > div:nth-child(1)')
                if node2:
                    content_parts.append(str(node2))
                
                if content_parts:
                    # 合并完整 HTML（两个容器之间加 <br><br> 分隔）
                    full_raw_content = "<br><br>".join(content_parts)
                    
                    # 来源网址变超链接（宽松匹配）
                    full_raw_content = re.sub(
                        r'(来源网址|原文链接|原文地址)[：:]?\s*(https?://[^\s<"]+)',
                        r'<br><br>\1：<a href="\2" target="_blank" rel="noopener noreferrer" style="color:#0066cc; text-decoration:underline;">\2</a><br>',
                        full_raw_content,
                        flags=re.IGNORECASE | re.MULTILINE
                    )
                    
                    # 额外处理 HTML 实体和空格
                    full_raw_content = full_raw_content.replace('&nbsp;', ' ').replace('\xa0', ' ')
                    
                    conn.execute("INSERT OR REPLACE INTO article_content(url, content) VALUES(?,?)", (url, full_raw_content))
                    conn.commit()
                    content = clean_html(full_raw_content, site_key)
                    if site_key == "xianbao" and content:
                        
                        content = re.sub(
                            r'原文链接：(https?://[^\s<]+)',
                            r'<br><br>原文链接：<a href="\1" target="_blank" rel="noopener noreferrer" style="color:#0066cc; text-decoration:underline;">\1</a><br>',
                            content,
                            flags=re.IGNORECASE
                        )
                else:
                    content = "暂无核心内容"
            else:
                # 其他站点保持原逻辑（不变）
                selectors = SITES_CONFIG[site_key]["content_selector"].split(',')
                content_nodes = []
                for sel in selectors:
                    node = soup.select_one(sel.strip())
                    if node: content_nodes.append(str(node))
                
                if content_nodes:
                    full_raw_content = "".join(content_nodes)
                    conn.execute("INSERT OR REPLACE INTO article_content(url, content) VALUES(?,?)", (url, full_raw_content))
                    conn.commit()
                    content = clean_html(full_raw_content, site_key)
                else:
                    content = "暂无内容"
                    
        except Exception as e:
            print(f"Error fetching content: {e}")
            content = "加载原文失败，请尝试点击右上角原文链接。"
    conn.close()
    return render_template("detail.html", title=title, content=content, original_url=url, time=row['original_time'])

@app.route('/admin')
@login_required
def admin_panel():
    conn = get_db_connection()
    try:
        whitelist = conn.execute("SELECT * FROM config_rules WHERE rule_type='white'").fetchall()
        blacklist = conn.execute("SELECT * FROM config_rules WHERE rule_type='black'").fetchall()
        my_articles = conn.execute("SELECT id, title, is_top, updated_at FROM articles WHERE site_source='user' ORDER BY is_top DESC, id DESC").fetchall()
        
        last_log = conn.execute('SELECT last_scrape FROM scrape_log ORDER BY id DESC LIMIT 1').fetchone()
        last_update = last_log[0] if last_log else "尚未开始抓取"
        
        total_arts = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        total_visits_row = conn.execute("SELECT SUM(visit_count) FROM visit_stats").fetchone()
        total_visits = total_visits_row[0] if total_visits_row and total_visits_row[0] else 0
        stats = {'total_articles': total_arts, 'total_visits': total_visits, 'last_update': last_update}
    finally:
        conn.close()
    return render_template('admin.html', whitelist=whitelist, blacklist=blacklist, my_articles=my_articles, stats=stats)

@app.route('/admin/refresh', methods=['GET', 'POST'])
@login_required  # 必须登录才能手动刷新（最安全）
def admin_refresh():
    now = get_beijing_now()
    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 手动刷新触发 by admin")
    
    try:
        scrape_all_sites()  # 直接调用抓取函数
        flash("手动刷新成功！已抓取最新内容", "success")  # Flask flash 提示
    except Exception as e:
        print(f"手动刷新失败: {e}")
        flash(f"刷新失败：{str(e)}", "error")
    
    return redirect(url_for('admin_panel'))  # 刷新后跳回 admin 面板

@app.route('/publish', methods=['GET', 'POST'])
@login_required
def publish():
    if request.method == 'POST':
        title = request.form.get('title')
        raw_content = request.form.get('content')
        is_top = 1 if request.form.get('publish_mode') == 'top' else 0
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

@app.route('/article/edit/<int:aid>', methods=['GET', 'POST'])
@login_required
def edit_article(aid):
    conn = get_db_connection()
    if request.method == 'POST':
        title = request.form.get('title')
        raw_content = request.form.get('content')
        is_top = 1 if request.form.get('publish_mode') == 'top' else 0
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

@app.route('/article/top/<int:aid>')
@login_required
def toggle_top(aid):
    conn = get_db_connection()
    conn.execute("UPDATE articles SET is_top = 1 - is_top WHERE id=?", (aid,))
    conn.commit(); conn.close()
    return redirect('/admin')

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

@app.route('/api/rule', methods=['POST'])
@login_required
def api_rule():
    action = request.form.get('action')
    rtype = request.form.get('type')
    scope = request.form.get('scope', 'title')
    kw = request.form.get('keyword', '').strip()
    rid = request.form.get('id')
    conn = get_db_connection()
    try:
        if action == 'add' and kw:
            conn.execute("INSERT OR IGNORE INTO config_rules (rule_type, keyword, match_scope) VALUES (?, ?, ?)", (rtype, kw, scope))
        elif action == 'delete' and rid:
            conn.execute("DELETE FROM config_rules WHERE id=?", (rid,))
        conn.commit()
    except Exception as e:
        print(f"规则操作失败: {e}")
    finally:
        conn.close()
    return redirect(url_for('admin_panel'))

@app.route('/logs')
@login_required
def show_logs():
    conn = get_db_connection()
    logs = conn.execute('SELECT last_scrape FROM scrape_log ORDER BY id DESC LIMIT 50').fetchall()
    visitors = conn.execute('SELECT * FROM visit_stats ORDER BY last_visit DESC LIMIT 30').fetchall()
    conn.close()
    return render_template('logs.html', logs=logs, visitors=visitors)

@lru_cache(maxsize=200)
def fetch_image_cached(url):
    """
    从远程源下载图片并缓存，避免重复下载。
    返回 (bytes, content-type)
    """
    r = session_req.get(url, headers={"User-Agent": HEADERS["User-Agent"], "Referer": ""}, timeout=15)
    return r.content, r.headers.get("Content-Type", "image/jpeg")


@app.route('/img_proxy')
def img_proxy():
    raw = request.args.get('url', '').strip()
    if not raw:
        return "", 404

    url = unquote(raw)

    if url.startswith("/img_proxy"):
        print("[WARN] Blocked nested img_proxy:", url)
        return "", 404

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        print("[WARN] Blocked invalid scheme:", url)
        return "", 404

    try:
        img_bytes, ctype = fetch_image_cached(url)
        return Response(img_bytes, content_type=ctype)

    except Exception as e:
        print("[IMG_PROXY ERROR]", e)
        
        error_html = f'''
        <div style="text-align:center; padding:20px; color:#666; font-family: system-ui, sans-serif;">
            <p>代理加载失败（源站拒绝连接）</p>
            <p><a href="{url}" target="_blank" style="color:#007aff; text-decoration:underline;">点击查看原图</a></p>
            <small>（可能需科学上网或稍后重试）</small>
        </div>
        '''
        return Response(error_html, content_type='text/html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST' and request.form.get('password') == ADMIN_PASSWORD:
        session['is_logged_in'] = True
        return redirect('/admin')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear(); return redirect('/')

@app.route('/cron/scrape', methods=['GET', 'POST'])
def cron_scrape():
    # 支持 header 或 query 参数验证
    provided_secret = (
        request.headers.get('Authorization') or
        request.args.get('secret') or
        request.form.get('secret')
    )
    
    if provided_secret != CRON_SECRET:
        return {"error": "Unauthorized"}, 401
    
    now = get_beijing_now()
    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] Cron triggered by: {request.headers.get('User-Agent', 'Unknown')}")
    # 可选：最近5分钟有人访问过就跳过，避免和高峰冲突
    # if (now - LAST_ACTIVE_TIME).total_seconds() < 300:
    #     print(f"[{now}] Skip cron: recent activity detected")
    #     return {"status": "skipped", "reason": "recent activity"}, 200
    
    try:
        scrape_all_sites()
        return {
            "status": "success",
            "executed_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "message": "抓取完成"
        }, 200
    except Exception as e:
        print(f"Cron error: {e}")
        return {"status": "error", "message": str(e)}, 500

# ==========================================
# 4. 抓取与启动
# ==========================================

def scrape_all_sites():
    global LAST_ACTIVE_TIME
    if scrape_lock.locked():
        print("抓取锁被占用，跳过本次执行")
        return
    
    with scrape_lock:
        try:
            now_beijing = get_beijing_now()
            
            # 无人访问休眠逻辑
            if (now_beijing - LAST_ACTIVE_TIME).total_seconds() > 3600:
                if now_beijing.minute % 60 == 0:
                    print(f"[{now_beijing.strftime('%H:%M')}] 系统处于无人访问休眠状态...")
                return

            # 夜间低频模式
            if 1 <= now_beijing.hour <= 5:
                if now_beijing.minute % 30 != 0:
                    return

            conn = get_db_connection()
            rules = conn.execute("SELECT * FROM config_rules").fetchall()
            title_white = [r['keyword'] for r in rules if r['rule_type']=='white' and r['match_scope']=='title']
            title_black = [r['keyword'] for r in rules if r['rule_type']=='black' and r['match_scope']=='title']
            url_black   = [r['keyword'] for r in rules if r['rule_type']=='black' and r['match_scope']=='url']
            
            base_keywords = ALL_BANK_VALS + title_white
            stats = {}

            for skey, cfg in SITES_CONFIG.items():
                try:
                    print(f"\n=== 开始抓取 {cfg['name']} ({skey}) ===")
                    r = session_req.get(cfg['list_url'], timeout=15)
                    print(f"  状态码: {r.status_code}")
                    
                    soup = BeautifulSoup(r.text, "html.parser")
                    items = soup.select(cfg['list_selector'])
                    print(f"  找到 {len(items)} 个匹配项")
                    
                    count = 0
                    for idx, item in enumerate(soup.select(cfg['list_selector']), 1):
                        # --- 修改后的 a 标签提取逻辑 ---
                        if item.name == 'a':
                            # 如果 item 本身就是 <a> 标签（常见于鲸线报等站点），直接使用它
                            # print(f"  [{skey} {idx:02d}] Item 本身就是 <a> 标签，使用它。")
                            a = item
                        else:
                            # 否则，在 item 内部查找合适的 <a>（兼容其他站点）
                            # print(f"  [{skey} {idx:02d}] Item 不是 <a>，在内部查找 <a>。")
                            a = item.select_one("a[href*='view'], a[href*='thread'], a[href*='post'], a[href*='/detail'], a[href*='/xianbao/detail']") or item.find("a")
                        
                        if not a:
                            # print(f"  [{skey} {idx:02d}] ERROR: 未能找到 <a> 标签，跳过")
                            continue
                        
                        # 标题和 URL 必须从 a 取
                        t = a.get_text(strip=True).strip()
                        if not t or len(t) < 5:
                            # print(f"  [{skey} {idx:02d}] 标题太短或为空，跳过")
                            continue
                        
                        h = a.get("href", "")
                        url = h if h.startswith("http") else (cfg['domain'] + (h if h.startswith("/") else "/" + h))
                        lower_t = t.lower()
                        lower_url = url.lower()
                        
                        # jd/tb 过滤
                        if 'jd.com' in lower_url or 'tb.cn' in lower_url or 'jd.com' in lower_t or 'tb.cn' in lower_t:
                            # print(f"  [{skey} {idx:02d}] jd/tb 过滤跳过")
                            continue
                        
                        # 黑名单过滤
                        black_hit = any(b in url for b in url_black) or any(b in t for b in title_black)
                        if black_hit:
                            # print(f"  [{skey} {idx:02d}] 被黑名单过滤跳过")
                            continue
                        
                        # 关键词匹配
                        kw = next((k for k in base_keywords if k.lower() in lower_t), None)
                        if kw:
                            # print(f"  [{skey} {idx:02d}] 匹配关键词: {kw}")
                            tag = kw
                            for b_name, b_v in BANK_KEYWORDS.items():
                                if kw in b_v:
                                    tag = b_name
                                    break
                            
                            # print(f"      → 尝试插入 (tag={tag}) URL: {url}")
                            conn.execute('INSERT OR IGNORE INTO articles (title, url, site_source, match_keyword, original_time) VALUES(?,?,?,?,?)',
                                        (t, url, skey, tag, now_beijing.strftime("%H:%M")))
                            changes = conn.total_changes
                            # print(f"      → 插入结果 changes={changes}")
                            
                            if changes > 0:
                                count += 1
                                # print("      → 成功插入！count +1")
                            # else:
                            #     print("      → 未插入（可能是重复URL）")
                        # else:
                        #     print(f"  [{skey} {idx:02d}] 无关键词匹配，跳过")
                        
                        # print("─" * 80)  # 分隔线，便于阅读
                    stats[cfg['name']] = count
                
                
                except Exception as e:
                    print(f"抓取 {skey} 失败: {e}")
                    stats[cfg['name']] = "Error"
                print(f"  {cfg['name']} 本次新增: {count} 条\n")
            conn.execute("DELETE FROM articles WHERE site_source != 'user' AND updated_at < datetime('now', '-4 days')")
            conn.execute('INSERT INTO scrape_log(last_scrape) VALUES(?)', 
                         (f"[{now_beijing.strftime('%m-%d %H:%M')}] {stats}",))
            conn.commit()
            conn.close()
            
        except Exception as e:
            print(f"Scrape Loop Error: {e}")

if __name__ == '__main__':
    get_db_connection().close()
    print("Serving on port 8080...")
    serve(app, host='0.0.0.0', port=8080, threads=80)

