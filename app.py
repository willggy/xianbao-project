import os
import sys
import threading
import time
import base64
import re
import gc  # 用于手动回收内存
import ipaddress
import socket
from datetime import datetime, timedelta, timezone
from functools import wraps, lru_cache
from urllib.parse import quote, unquote, urlparse, urljoin
import psycopg
from psycopg.rows import dict_row
import requests
from requests.adapters import HTTPAdapter
from flask import Flask, flash, render_template, request, Response, redirect, session, url_for
from bs4 import BeautifulSoup
from waitress import serve

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    # Keep startup working even if python-dotenv is not installed yet.
    pass

try:
    # Improve Windows console readability for Chinese logs.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ==========================================
# 1. 基础配置
# ==========================================
app = Flask(__name__)

# 密钥配置
SITE_TITLE = "古希腊掌管羊毛的神"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
app.secret_key = os.environ.get('SECRET_KEY', 'xianbao_secret_key_888') 
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '123')  
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024 
CRON_SECRET = os.environ.get('CRON_SECRET', 'xianbao_secret_key_999')
# 本地参数
ALLOW_INSECURE_DEFAULTS = os.environ.get('ALLOW_INSECURE_DEFAULTS', '1').strip() == '1'

# 站点配置
SITES_CONFIG = {
    "xianbao": { 
        "name": "线报库", 
        "domain": "https://new.xianbao.fun", 
        "list_url": "https://new.xianbao.fun/", 
        "list_selector": "#mainbox > div.listbox tr, #mainbox > div.listbox li", 
        "content_selector": "#mainbox article .article-content, #art-fujia, #mainbox > article > div.art-content > div.art-copyright.br > div:nth-child(1)",
        "original_url_selectors": [
            "a[href*='source']",
            "a[href*='from']",
            "a[href*='origin']",
            "a[href*='jump']"
        ],
        "original_url_regexes": [
            r'(https?://[^\s<"\']+)'
        ]
    },
    "iehou": { 
        "name": "爱猴线报", 
        "domain": "https://iehou.com", 
        "list_url": "https://iehou.com/", 
        "list_selector": "#body ul li",
        "content_selector": ".thread-content",
        "original_url_selectors": [
            ".thread-content a[href]",
            "a[href*='url=']",
            "a[href*='target=']"
        ],
        "original_url_regexes": [
            r'(https?://[^\s<"\']+)'
        ]
    },
    "xianbao_icu": {
        "name": "鲸线报",  
        "domain": "https://xianbao.icu",
        "list_url": "https://xianbao.icu/xianbao",  
        "list_selector": "main div div div:nth-child(3) > div:nth-child(2) a, main a[href*='/xianbao/detail'], main a[href*='/detail'], ul li a[href*='/detail']",
        "content_selector": "main > div:nth-of-type(2) > div > div, .prose, .prose-max, .content, .entry-content, .post-body, .detail-body, .markdown, .article-detail, .text",
        "original_url_selectors": [
            "a[href*='source']",
            "a[href*='origin']",
            ".article-content a[href]"
        ],
        "original_url_regexes": [
            r'(https?://[^\s<"\']+)'
        ]
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

    if not os.environ.get('DATABASE_URL'):
        problems.append("DATABASE_URL 未设置（Supabase Postgres 连接串）")

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

# def get_db_connection():
#     """
#     获取 Supabase Postgres 连接（依赖环境变量 DATABASE_URL）。
#     使用 dict_row 以便 row['field'] 写法保持不变。
#     """
#     dsn = os.environ.get("DATABASE_URL")
#     if not dsn:
#         raise RuntimeError("DATABASE_URL 未设置")
#     return psycopg.connect(dsn, row_factory=dict_row)
# 本地
def get_db_connection():
    dsn = DATABASE_URL
    if not dsn:
        raise RuntimeError("DATABASE_URL not set. Configure env var DATABASE_URL before start.")
    return psycopg.connect(dsn, row_factory=dict_row)
def make_links_clickable(text):
    # 匹配 http/https URL，但排除已经在 href= 里的情况
    pattern = re.compile(r'(?<!href=")(https?://[^\s"<]+)', re.IGNORECASE)
    return pattern.sub(r'<a href="\1" target="_blank" rel="noopener noreferrer" class="content-link">\1</a>', text)

def extract_original_url(html_content, fallback_url="", site_key=""):
    """Extract source/original URL from article HTML using per-site config first."""
    if not html_content:
        return fallback_url

    try:
        soup = BeautifulSoup(html_content, "lxml")
    except Exception:
        soup = BeautifulSoup(html_content, "html.parser")

    cfg = SITES_CONFIG.get(site_key, {}) if site_key else {}
    selectors = cfg.get("original_url_selectors", []) or []
    regexes = cfg.get("original_url_regexes", []) or []

    def _normalize_href(href: str) -> str:
        href = (href or "").strip()
        if not href:
            return ""
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = urljoin(fallback_url, href) if fallback_url else href
        return href if href.startswith(("http://", "https://")) else ""

    # 1) Site-level selectors (highest priority)
    for sel in selectors:
        try:
            for a in soup.select(sel):
                href = _normalize_href(a.get("href", ""))
                if href:
                    return href
        except Exception:
            continue

    # 2) Keyword anchor text
    keywords = ("source", "origin", "original", "from", "link", "jump")
    for a in soup.select("a[href]"):
        href = _normalize_href(a.get("href", ""))
        if not href:
            continue
        t = a.get_text(" ", strip=True).lower()
        if any(k.lower() in t for k in keywords):
            return href

    # 3) Site-level regex + defaults
    text_blob = soup.get_text(" ", strip=True)
    default_patterns = [
        r"(?:source|origin|original|from|link)\s*[:：]?\s*(https?://[^\s<>\"']+)",
        r"(https?://[^\s<>\"']+)",
    ]
    for pat in list(regexes) + default_patterns:
        try:
            m = re.search(pat, text_blob, flags=re.IGNORECASE)
            if not m:
                continue
            if m.lastindex:
                for i in range(1, m.lastindex + 1):
                    cand = (m.group(i) or "").strip()
                    if cand.startswith(("http://", "https://")):
                        return cand
            cand = (m.group(0) or "").strip()
            if cand.startswith(("http://", "https://")):
                return cand
        except Exception:
            continue

    # 4) Fallback first http(s) link
    for a in soup.select("a[href]"):
        href = _normalize_href(a.get("href", ""))
        if href:
            return href

    return fallback_url


def safe_extract_original_url(html_content, fallback_url="", site_key=""):
    try:
        return extract_original_url(html_content, fallback_url=fallback_url, site_key=site_key)
    except Exception as e:
        print(f"[WARN] extract_original_url failed: {e}")
        return fallback_url

def clean_html(html_content, site_key):
    if not html_content:
        return ""

    # 【优化】使用 lxml 解析器
    # Convert plain URL after a colon (e.g. "????: https://...") into links.
    html_content = re.sub(
        r'([:?]\s*)(https?://[^\s<"]+)',
        r'\1<a href="\2" target="_blank" rel="noopener noreferrer" style="color:#007aff; text-decoration:underline; word-break:break-all;">\2</a>',
        html_content,
        flags=re.IGNORECASE,
    )

    soup = BeautifulSoup(html_content, "lxml")

    site_cfg = SITES_CONFIG.get(site_key, {})
    site_domain = site_cfg.get("domain", "")

    for tag in soup.find_all(True):

        # ============================
        # 1) 图片处理逻辑
        # ============================
        if tag.name == 'img':
            src = (
                tag.get('src', '').strip()
                or tag.get('data-src', '').strip()
                or tag.get('data-original', '').strip()
                or tag.get('data-lazy-src', '').strip()
            )

            # srcset often looks like: "url1 1x, url2 2x"
            if not src:
                srcset = tag.get('srcset', '').strip() or tag.get('data-srcset', '').strip()
                if srcset:
                    first = srcset.split(',')[0].strip()
                    src = first.split(' ')[0].strip()

            if not src:
                continue

            # ---- 避免重复包装 /img_proxy ----
            if src.startswith("/img_proxy"):
                continue

            # ---- 补全各种相对路径 ----
            if src.startswith('//'):  # //img.xx.com/xx.jpg
                src = 'https:' + src

            elif src.startswith('/'):  # /upload/xxx.jpg
                if site_domain:
                    src = urljoin(site_domain, src)
                else:
                    continue

            elif src.startswith('./'):  # ./images/xxx.jpg
                if site_domain:
                    src = urljoin(site_domain + '/', src)
                else:
                    continue

            elif src.startswith('../'):  # ../xx/xx.jpg
                if site_domain:
                    src = urljoin(site_domain + '/', src)
                else:
                    continue

            # ---- 这里不做更多处理，否则容易误判 HTML 图片 ----

            # ---- URL 转义 + 走 img_proxy ----
            # Keep existing percent-encoding, but encode query separators (&, =)
            # inside nested URLs so outer /img_proxy query string will not truncate.
            proxy_url = "/img_proxy?url=" + quote(src, safe=':/?%')

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
                if site_domain:
                    href = urljoin(site_domain, href)

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
        conn.execute(
            '''INSERT INTO visit_stats (ip, visit_count, last_visit)
               VALUES (%s, 1, CURRENT_TIMESTAMP)
               ON CONFLICT (ip) DO UPDATE
               SET visit_count = visit_stats.visit_count + 1,
                   last_visit = CURRENT_TIMESTAMP''',
            (ip,),
        )
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
        where += " AND match_keyword = %s"
        params.append(tag)
    if q:
        where += " AND title LIKE %s"
        params.append(f"%{q}%")
    
    articles = conn.execute(
        f'SELECT * FROM articles {where} ORDER BY is_top DESC, id DESC LIMIT %s OFFSET %s',
        params + [PER_PAGE, (page-1)*PER_PAGE],
    ).fetchall()
    
    total = conn.execute(f'SELECT COUNT(*) FROM articles {where}', params).fetchone()["count"]
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
    row = conn.execute("SELECT * FROM articles WHERE id=%s", (article_id,)).fetchone()
    if not row: return "内容不存在", 404
    
    url, site_key, title = row["url"], row["site_source"], row["title"]
    original_url = url
    cached = conn.execute("SELECT content FROM article_content WHERE url=%s", (url,)).fetchone()
    content = ""

    if cached and cached['content']:
        original_url = safe_extract_original_url(cached["content"], fallback_url=url, site_key=site_key)
        if site_key == "user" or site_key not in SITES_CONFIG:
            content = cached["content"]
        else:
            content = clean_html(cached["content"], site_key)
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
                    
                    conn.execute(
                        "INSERT INTO article_content(url, content) VALUES(%s, %s) "
                        "ON CONFLICT (url) DO UPDATE SET content = EXCLUDED.content, updated_at = CURRENT_TIMESTAMP",
                        (url, full_raw_content),
                    )
                    conn.commit()
                    original_url = safe_extract_original_url(full_raw_content, fallback_url=url, site_key=site_key)
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
                    conn.execute(
                        "INSERT INTO article_content(url, content) VALUES(%s, %s) "
                        "ON CONFLICT (url) DO UPDATE SET content = EXCLUDED.content, updated_at = CURRENT_TIMESTAMP",
                        (url, full_raw_content),
                    )
                    conn.commit()
                    original_url = safe_extract_original_url(full_raw_content, fallback_url=url, site_key=site_key)
                    content = clean_html(full_raw_content, site_key)
                else:
                    content = "暂无内容"
                    
        except Exception as e:
            print(f"Error fetching content: {e}")
            content = "加载原文失败，请尝试点击右上角原文链接。"
    conn.close()
    return render_template("detail.html", title=title, content=content, original_url=original_url, time=row['original_time'])

@app.route('/admin')
@login_required
def admin_panel():
    conn = get_db_connection()
    # 1. 先初始化所有变量，防止 UnboundLocalError
    whitelist, blacklist, my_articles = [], [], []
    total_arts, total_visits = 0, 0
    last_update = "尚未开始抓取"
    
    try:
        # 2. 执行数据库查询
        whitelist = conn.execute("SELECT * FROM config_rules WHERE rule_type='white'").fetchall()
        blacklist = conn.execute("SELECT * FROM config_rules WHERE rule_type='black'").fetchall()
        my_articles = conn.execute("SELECT id, title, is_top, updated_at FROM articles WHERE site_source='user' ORDER BY is_top DESC, id DESC").fetchall()
        
        last_log = conn.execute('SELECT last_scrape FROM scrape_log ORDER BY id DESC LIMIT 1').fetchone()
        if last_log:
            last_update = last_log["last_scrape"]
            
        # 注意：PostgreSQL 的 count 返回的是 dict，键名通常是 'count'
        res_count = conn.execute("SELECT COUNT(*) as cnt FROM articles").fetchone()
        total_arts = res_count["cnt"] if res_count else 0
        
        res_visits = conn.execute("SELECT SUM(visit_count) as s FROM visit_stats").fetchone()
        total_visits = res_visits["s"] if res_visits and res_visits["s"] else 0

    except Exception as e:
        print(f"后台数据加载失败: {e}") # 打印错误方便调试
    finally:
        conn.close()

    # 3. 此时变量一定存在，不会报错
    stats = {
        'total_articles': total_arts, 
        'total_visits': total_visits, 
        'last_update': last_update
    }
    return render_template('admin.html', whitelist=whitelist, blacklist=blacklist, my_articles=my_articles, stats=stats)
@app.route('/admin/refresh', methods=['GET', 'POST'])
@login_required  # manual refresh requires login
def admin_refresh():
    now = get_beijing_now()
    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] admin manual refresh triggered")

    summary = scrape_all_sites()
    status = (summary or {}).get("status", "error")

    if status == "success":
        site_stats = (summary or {}).get("site_stats", {})
        site_desc = ", ".join(
            [f"{k} +{int(v.get('new', 0))}" for k, v in site_stats.items()]
        ) or "No site stats"
        flash(
            f"Scrape success: +{int(summary.get('total_new', 0))} new, {summary.get('duration_sec', 0):.1f}s. {site_desc}",
            "success",
        )
    elif status == "skipped":
        reason = summary.get("reason", "skipped")
        flash(f"Scrape skipped: {reason}, {summary.get('duration_sec', 0):.1f}s", "warning")
    else:
        err = summary.get("error", "unknown error")
        flash(f"Scrape failed: {err}", "danger")

    return redirect(url_for('admin_panel'))  # back to admin panel

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
        conn.execute(
            "INSERT INTO articles (title, url, site_source, match_keyword, original_time, is_top) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (title, fake_url, "user", "羊毛精选", "刚刚", is_top),
        )
        conn.execute(
            "INSERT INTO article_content (url, content) VALUES (%s, %s) "
            "ON CONFLICT (url) DO UPDATE SET content = EXCLUDED.content, updated_at = CURRENT_TIMESTAMP",
            (fake_url, processed),
        )
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
        row = conn.execute("SELECT url FROM articles WHERE id=%s", (aid,)).fetchone()
        if row:
            conn.execute("UPDATE articles SET title=%s, is_top=%s WHERE id=%s", (title, is_top, aid))
            conn.execute("UPDATE article_content SET content=%s WHERE url=%s", (processed, row['url']))
            conn.commit()
        conn.close()
        return redirect('/admin')

    article = conn.execute("SELECT * FROM articles WHERE id=%s AND site_source='user'", (aid,)).fetchone()
    if not article: return "未找到文章", 404
    content = conn.execute("SELECT content FROM article_content WHERE url=%s", (article['url'],)).fetchone()['content']
    conn.close()
    return render_template('edit.html', article=article, content=content)

@app.route('/article/top/<int:aid>')
@login_required
def toggle_top(aid):
    conn = get_db_connection()
    conn.execute("UPDATE articles SET is_top = 1 - is_top WHERE id=%s", (aid,))
    conn.commit(); conn.close()
    return redirect('/admin')

@app.route('/article/delete/<int:aid>')
@login_required
def delete_article(aid):
    conn = get_db_connection()
    row = conn.execute("SELECT url FROM articles WHERE id=%s", (aid,)).fetchone()
    if row:
        conn.execute("DELETE FROM articles WHERE id=%s", (aid,))
        conn.execute("DELETE FROM article_content WHERE url=%s", (row['url'],))
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
            conn.execute(
                "INSERT INTO config_rules (rule_type, keyword, match_scope) VALUES (%s, %s, %s) "
                "ON CONFLICT (keyword, match_scope) DO NOTHING",
                (rtype, kw, scope),
            )
        elif action == 'delete' and rid:
            conn.execute("DELETE FROM config_rules WHERE id=%s", (rid,))
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
        dynamic_referer = f"{parsed.scheme}://{host}/" if host else ""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            # Use target site as Referer to reduce anti-hotlink false negatives.
            "Referer": dynamic_referer,
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
        # Some image hosts reject unknown Referer values; retry once without Referer.
        r = session_req.get(url, headers=headers, timeout=15, stream=True, allow_redirects=True)
        if r.status_code in (401, 403, 404):
            try:
                r.close()
            except Exception:
                pass
            headers_no_referer = dict(headers)
            headers_no_referer.pop("Referer", None)
            r = session_req.get(url, headers=headers_no_referer, timeout=15, stream=True, allow_redirects=True)

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
        summary = scrape_all_sites()
        summary["executed_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        return summary, 200
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
    started_at = time.time()

    if scrape_lock.locked():
        print("scrape lock busy, skip this run")
        return {
            "status": "skipped",
            "reason": "lock_busy",
            "site_stats": {},
            "total_new": 0,
            "duration_sec": round(time.time() - started_at, 2),
        }

    with scrape_lock:
        conn = None
        site_stats = {}
        try:
            now_beijing = get_beijing_now()

            # Sleep mode: skip when no user activity for a long time.
            if (now_beijing - LAST_ACTIVE_TIME).total_seconds() > 3600:
                if now_beijing.minute % 60 == 0:
                    print(f"[{now_beijing.strftime('%H:%M')}] inactive >1h, skip scrape")
                return {
                    "status": "skipped",
                    "reason": "inactive_sleep",
                    "site_stats": {},
                    "total_new": 0,
                    "duration_sec": round(time.time() - started_at, 2),
                }

            # Night low-frequency mode.
            if 1 <= now_beijing.hour <= 5 and now_beijing.minute % 30 != 0:
                return {
                    "status": "skipped",
                    "reason": "night_low_frequency_window",
                    "site_stats": {},
                    "total_new": 0,
                    "duration_sec": round(time.time() - started_at, 2),
                }

            conn = get_db_connection()
            rules = conn.execute("SELECT * FROM config_rules").fetchall()
            title_white = [r['keyword'] for r in rules if r['rule_type']=='white' and r['match_scope']=='title']
            title_black = [r['keyword'] for r in rules if r['rule_type']=='black' and r['match_scope']=='title']
            url_black   = [r['keyword'] for r in rules if r['rule_type']=='black' and r['match_scope']=='url']

            base_keywords = ALL_BANK_VALS + title_white
            log_stats = {}

            # ?????????????????????????????
            seen_titles_this_run = set()

            # ?????????????0??????????????????
            recent_articles = conn.execute(
                "SELECT title FROM articles WHERE updated_at > (now() - interval '30 minutes')"
            ).fetchall()
            recent_norm_titles = {normalize_title(row['title']) for row in recent_articles}

            for skey, cfg in SITES_CONFIG.items():
                count = 0
                try:
                    print(f"\n=== start scrape: {skey} ===")
                    r = session_req.get(cfg['list_url'], timeout=15)
                    print(f"  status: {r.status_code}")

                    # ??????????lxml ???????????????
                    soup = BeautifulSoup(r.text, "lxml")
                    items = soup.select(cfg['list_selector'])
                    print(f"  matched items: {len(items)}")

                    for idx, item in enumerate(items, 1):
                        if item.name == 'a':
                            a = item
                        else:
                            a = item.select_one("a[href*='view'], a[href*='thread'], a[href*='post'], a[href*='/detail'], a[href*='/xianbao/detail']") or item.find("a")

                        if not a:
                            continue

                        t = a.get_text(strip=True).strip()
                        if not t or len(t) < 5:
                            continue

                        h = a.get("href", "")
                        url = h if h.startswith("http") else (cfg['domain'] + (h if h.startswith("/") else "/" + h))
                        lower_t = t.lower()
                        lower_url = url.lower()

                        norm_title = normalize_title(t)
                        if not norm_title:
                            continue
                        if norm_title in seen_titles_this_run:
                            continue
                        if norm_title in recent_norm_titles:
                            continue

                        if 'jd.com' in lower_url or 'tb.cn' in lower_url or 'jd.com' in lower_t or 'tb.cn' in lower_t:
                            continue

                        black_hit = any(b in url for b in url_black) or any(b in t for b in title_black)
                        if black_hit:
                            continue

                        kw = next((k for k in base_keywords if k.lower() in lower_t), None)
                        if kw:
                            seen_titles_this_run.add(norm_title)
                            tag = kw
                            for b_name, b_v in BANK_KEYWORDS.items():
                                if kw in b_v:
                                    tag = b_name
                                    break

                            with conn.cursor() as cur:
                                cur.execute(
                                    'INSERT INTO articles (title, url, site_source, match_keyword, original_time) '
                                    'VALUES (%s, %s, %s, %s, %s) '
                                    'ON CONFLICT (url) DO NOTHING',
                                    (t, url, skey, tag, now_beijing.strftime("%H:%M")),
                                )
                                if cur.rowcount > 0:
                                    count += 1

                    soup.decompose()
                    gc.collect()

                    site_stats[skey] = {"name": cfg['name'], "new": count, "status": "ok"}
                    log_stats[cfg['name']] = count

                except Exception as e:
                    print(f"scrape error on {skey}: {e}")
                    site_stats[skey] = {"name": cfg.get('name', skey), "new": 0, "status": "error", "error": str(e)}
                    log_stats[cfg.get('name', skey)] = "Error"

                print(f"  {skey} new items: {count}")

            conn.execute("DELETE FROM articles WHERE site_source != 'user' AND updated_at < (now() - interval '4 days')")

            conn.execute(
                'INSERT INTO scrape_log(last_scrape) VALUES(%s)',
                (f"[{now_beijing.strftime('%m-%d %H:%M')}] {log_stats}",),
            )

            conn.commit()

            total_new = sum(int(v.get("new", 0)) for v in site_stats.values())
            return {
                "status": "success",
                "site_stats": site_stats,
                "total_new": total_new,
                "duration_sec": round(time.time() - started_at, 2),
            }

        except Exception as e:
            print(f"Scrape Loop Error: {e}")
            return {
                "status": "error",
                "error": str(e),
                "site_stats": site_stats,
                "total_new": sum(int(v.get("new", 0)) for v in site_stats.values()) if site_stats else 0,
                "duration_sec": round(time.time() - started_at, 2),
            }
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

if __name__ == '__main__':
    get_db_connection().close()
    ensure_secure_config_or_exit()
    print("Serving on port 8080...")
    serve(app, host='0.0.0.0', port=8080, threads=80)
