import os
import sqlite3
import threading
import time
import base64
import re
import gc  # 用于手动回收内存
import ipaddress
import socket
from datetime import datetime, timedelta, timezone
from functools import wraps, lru_cache
from urllib.parse import quote, unquote, urlparse
import requests
from requests.adapters import HTTPAdapter
from flask import Flask, flash, render_template, request, Response, redirect, session, url_for
from bs4 import BeautifulSoup
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
# ALLOW_INSECURE_DEFAULTS=1--本地开发环境放行
ALLOW_INSECURE_DEFAULTS = os.environ.get('ALLOW_INSECURE_DEFAULTS', '').strip() == '1'

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


def _warn(msg: str):
    print(f"[SECURITY WARNING] {msg}")


def ensure_secure_config_or_exit():
    """
    上线安全保护：如果仍在使用默认密钥/默认密码，则拒绝启动。
    本地开发可设置 ALLOW_INSECURE_DEFAULTS=1 放行（会打印强警告）。
    """
    problems = []

    if not os.environ.get('SECRET_KEY') or app.secret_key == 'xianbao_secret_key_888':
        problems.append("SECRET_KEY 未设置或仍为默认值")

    if not os.environ.get('ADMIN_PASSWORD') or ADMIN_PASSWORD == '123':
        problems.append("ADMIN_PASSWORD 未设置或仍为默认值")

    if not os.environ.get('CRON_SECRET') or CRON_SECRET == 'xianbao_secret_key_999':
        problems.append("CRON_SECRET 未设置或仍为默认值")

    if not problems:
        return

    msg = (
        "检测到不安全的默认配置，将拒绝启动。\n"
        + "\n".join([f"- {p}" for p in problems])
        + "\n\n请在环境变量中设置：SECRET_KEY、ADMIN_PASSWORD、CRON_SECRET。\n"
        "如仅本机临时调试，可设置 ALLOW_INSECURE_DEFAULTS=1 跳过（不建议对公网）。"
    )

    if ALLOW_INSECURE_DEFAULTS:
        _warn(msg)
        return

    raise RuntimeError(msg)

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
    
    # 【新增索引】加速 30 分钟去重和 API 检查
    conn.execute('CREATE INDEX IF NOT EXISTS idx_articles_title_time ON articles(title, updated_at)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_articles_id_desc ON articles(id DESC)')
    
    conn.commit()
    return conn

def make_links_clickable(text):
    # 匹配 http/https URL，但排除已经在 href= 里的情况
    pattern = re.compile(r'(?<!href=")(https?://[^\s"<]+)', re.IGNORECASE)
    return pattern.sub(r'<a href="\1" target="_blank" rel="noopener noreferrer" class="content-link">\1</a>', text)

def clean_html(html_content, site_key):
    if not html_content:
        return ""

    # 【优化】使用 lxml 解析器
    soup = BeautifulSoup(html_content, "lxml")

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

    # 【优化】先保存结果再销毁解析树
    result = str(soup)
    soup.decompose()
    return result



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

    # --- 修改后的 3 分钟刷新逻辑 ---
    # 计算相对于当前小时，下一个 3 分钟的整点
    # 例如：13:01 -> 13:03, 13:05 -> 13:06
    next_interval = ((now.minute // 3) + 1) * 3
    
    if next_interval >= 60:
        next_refresh_obj = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    else:
        next_refresh_obj = now.replace(minute=next_interval, second=0, microsecond=0)

    next_refresh_time = next_refresh_obj.strftime("%H:%M")
    # ----------------------------

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
                           total_pages=(total+PER_PAGE-1)//PER_PAGE,
                           latest_id=articles[0]['id'] if articles else 0)

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
                
                # 第一个容器：核心正文（保留完整 HTML）
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
                    
                    # 步骤1：清理常见干扰（全角冒号、空格、实体）
                    full_raw_content = full_raw_content.replace('：', ':').replace('&nbsp;', ' ').replace('\xa0', ' ')
                    
                    # 步骤2：来源网址变超链接（更宽松匹配）
                    full_raw_content = re.sub(
                        r'(来源网址|原文链接|原文地址|来源地址)[:：]?\s*(https?://[^\s<"]+)',
                        r'<br><br>\1: <a href="\2" target="_blank" rel="noopener noreferrer" style="color:#0066cc; text-decoration:underline;">\2</a><br>',
                        full_raw_content,
                        flags=re.IGNORECASE | re.MULTILINE
                    )
                    
                    conn.execute("INSERT OR REPLACE INTO article_content(url, content) VALUES(?,?)", (url, full_raw_content))
                    conn.commit()
                    content = clean_html(full_raw_content, site_key)
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


@app.route('/api/check_update')
def check_update():
    """【新增】轻量级检查接口，极度节省流量"""
    conn = get_db_connection()
    row = conn.execute("SELECT id FROM articles ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return {"last_id": row['id'] if row else 0}


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

    def _is_ip_private_or_disallowed(ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return True
        return any([
            addr.is_private,
            addr.is_loopback,
            addr.is_link_local,
            addr.is_reserved,
            addr.is_multicast,
            getattr(addr, "is_unspecified", False),
        ])

    def _host_resolves_to_disallowed_ip(host: str) -> bool:
        host = (host or "").strip().lower()
        if not host:
            return True
        if host in {"localhost"}:
            return True

        # IP 字面量
        try:
            ipaddress.ip_address(host)
            return _is_ip_private_or_disallowed(host)
        except ValueError:
            pass

        # 解析域名 A/AAAA，任一命中内网/保留即拒绝
        try:
            infos = socket.getaddrinfo(host, None)
        except Exception:
            return True

        resolved = set()
        for info in infos:
            sockaddr = info[4]
            if isinstance(sockaddr, tuple) and sockaddr:
                resolved.add(sockaddr[0])
        if not resolved:
            return True

        return any(_is_ip_private_or_disallowed(ip) for ip in resolved)

    host = parsed.hostname or ""
    if _host_resolves_to_disallowed_ip(host):
        print("[WARN] Blocked SSRF host:", host, "url:", url)
        return "", 404

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Referer": "https://new.xianbao.fun/",  # 关键：用源站域名
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
            "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="122", "Google Chrome";v="122"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "image",
            "sec-fetch-mode": "no-cors",
            "sec-fetch-site": "cross-site"
        }

        # 【优化】使用 stream=True 进行流式传输，显著降低 RAM 占用
        r = session_req.get(url, headers=headers, timeout=15, stream=True, allow_redirects=True)

        # SSRF 防护：如果发生跳转，二次校验最终落点（防止跳到内网）
        final_url = getattr(r, "url", "") or url
        final_parsed = urlparse(final_url)
        final_host = final_parsed.hostname or ""
        if final_parsed.scheme not in ("http", "https") or _host_resolves_to_disallowed_ip(final_host):
            print("[WARN] Blocked SSRF redirect:", final_url)
            try:
                r.close()
            except Exception:
                pass
            return "", 404
        
        if r.status_code != 200:
            print(f"[IMG_PROXY] {url} 返回 {r.status_code}")
            return "", r.status_code

        content_type = r.headers.get("Content-Type", "image/jpeg")
        
        # 【优化】验证 Content-Type 是否为图片类型
        if not content_type or not any(img_type in content_type.lower() for img_type in ['image/', 'application/octet-stream']):
            print(f"[WARN] Content-Type 不是图片类型: {content_type}")
            return "", 404

        # 【优化】使用生成器流式传输数据，不再将整个图片存入内存
        def generate():
            for chunk in r.iter_content(chunk_size=4096):
                yield chunk
        
        return Response(generate(), content_type=content_type, status=200)

    except Exception as e:
        print(f"[IMG_PROXY ERROR] {url}: {e}")
        transparent_png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y1GNnUAAAAASUVORK5CYII=")
        return Response(transparent_png, content_type="image/png")


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

def normalize_title(title_text):
    """标题标准化函数：去除空格和标点符号"""
    if not title_text:
        return ""
    # 去除所有空格、换行符、制表符
    t = re.sub(r'\s+', '', title_text)
    # 去除中英文常见标点符号
    punctuation = r"""！？｡＂＃＄％＆＇（）＊＋，－／：；＜＝＞＠［＼］＾＿｀｛｜｝～｟｠｢｣､、〃》「」『』【】〔〕〖〗〘〙〚〛〜〝〞〟〰〾〿–—''‛""„‟…‧·.!,;:?"'()[]{}<>/-_=+"""
    t = re.sub(f"[{re.escape(punctuation)}]", "", t)
    return t.lower()

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
            
            # 用于本次抓取去重的集合（标题标准化后）
            seen_titles_this_run = set()
            
            # 【新增】获取最近30分钟的标题用于跨任务去重
            recent_articles = conn.execute(
                "SELECT title FROM articles WHERE updated_at > datetime('now', '-30 minutes')"
            ).fetchall()
            recent_norm_titles = {normalize_title(row['title']) for row in recent_articles}

            for skey, cfg in SITES_CONFIG.items():
                try:
                    print(f"\n=== 开始抓取 {cfg['name']} ({skey}) ===")
                    r = session_req.get(cfg['list_url'], timeout=15)
                    print(f"  状态码: {r.status_code}")
                    
                    # 【优化】改用 lxml 解析器，内存占用更低
                    soup = BeautifulSoup(r.text, "lxml")
                    items = soup.select(cfg['list_selector'])
                    print(f"  找到 {len(items)} 个匹配项")
                    
                    count = 0
                    for idx, item in enumerate(items, 1):
                        # --- 修改后的 a 标签提取逻辑 ---
                        if item.name == 'a':
                            a = item
                        else:
                            a = item.select_one("a[href*='view'], a[href*='thread'], a[href*='post'], a[href*='/detail'], a[href*='/xianbao/detail']") or item.find("a")
                        
                        if not a:
                            continue
                        
                        # 标题和 URL 必须从 a 取
                        t = a.get_text(strip=True).strip()
                        if not t or len(t) < 5:
                            continue
                        
                        h = a.get("href", "")
                        url = h if h.startswith("http") else (cfg['domain'] + (h if h.startswith("/") else "/" + h))
                        lower_t = t.lower()
                        lower_url = url.lower()
                        
                        # --- 标题规范化 + 本次运行/近30分钟去重（去符号/去空白/统一大小写） ---
                        norm_title = normalize_title(t)
                        if not norm_title:
                            continue
                        if norm_title in seen_titles_this_run:
                            continue
                        if norm_title in recent_norm_titles:
                            continue
                        
                        
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
                            # 只对“会被写入数据库”的候选做本次运行去重，避免误伤（如先遇到黑名单 URL）
                            seen_titles_this_run.add(norm_title)
                            tag = kw
                            for b_name, b_v in BANK_KEYWORDS.items():
                                if kw in b_v:
                                    tag = b_name
                                    break
                            
                            before_changes = conn.total_changes
                            conn.execute('INSERT OR IGNORE INTO articles (title, url, site_source, match_keyword, original_time) VALUES(?,?,?,?,?)',
                                        (t, url, skey, tag, now_beijing.strftime("%H:%M")))
                            inserted = (conn.total_changes - before_changes) > 0
                            
                            if inserted:
                                count += 1
                    
                    # 【核心优化】显式销毁解析树，手动触发垃圾回收
                    soup.decompose()
                    gc.collect()
                    
                    stats[cfg['name']] = count
                
                except Exception as e:
                    print(f"抓取 {skey} 失败: {e}")
                    stats[cfg['name']] = "Error"
                
                print(f"  {cfg['name']} 本次新增: {count} 条\n")
            
            # --- 清理旧数据 ---
            conn.execute("DELETE FROM articles WHERE site_source != 'user' AND updated_at < datetime('now', '-4 days')")
            
            # --- 记录日志 ---
            conn.execute('INSERT INTO scrape_log(last_scrape) VALUES(?)', 
                         (f"[{now_beijing.strftime('%m-%d %H:%M')}] {stats}",))
            
            conn.commit()
            conn.close()
            
        except Exception as e:
            print(f"Scrape Loop Error: {e}")

if __name__ == '__main__':
    get_db_connection().close()
    ensure_secure_config_or_exit()
    print("Serving on port 8080...")
    serve(app, host='0.0.0.0', port=8080, threads=80)