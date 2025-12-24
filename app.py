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
KEYWORDS = list(BANK_KEYWORDS.values()) + [
    "立减金", "红包", "话费", "水", "毛", "招", "hang", "信", "移动",
    "联通", "京东", "支付宝", "微信", "流量", "话费券", "充值",
    "话费充值", "zfb"
]

app = Flask(__name__)
DATA_DIR = "/app/data"
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "xianbao.db")

PER_PAGE = 30
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"}

# 冷却
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
    return conn

def init_db():
    conn = get_db_connection()
    try:
        conn.execute('PRAGMA journal_mode=WAL;')
        conn.execute('''CREATE TABLE IF NOT EXISTS articles(
            id INTEGER PRIMARY KEY AUTOINCREMENT, 
            title TEXT, url TEXT UNIQUE, site_source TEXT,
            match_keyword TEXT, original_time TEXT, 
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        conn.execute('CREATE TABLE IF NOT EXISTS article_content(url TEXT PRIMARY KEY, content TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
        conn.execute('CREATE TABLE IF NOT EXISTS scrape_log(id INTEGER PRIMARY KEY, last_scrape TIMESTAMP)')
        conn.execute('''CREATE TABLE IF NOT EXISTS visit_stats(
            ip TEXT PRIMARY KEY, 
            visit_count INTEGER DEFAULT 1, 
            last_visit TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        conn.commit()
        print("数据库初始化完成")
    finally:
        conn.close()

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

# ================== 3. 抓取逻辑 ==================
def scrape_all_sites(force=False):
    if scrape_lock.locked(): return
    with scrape_lock:
        start_time = time.time()
        now_str = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{now_str}] >>> 正在执行抓取任务...")

        conn = get_db_connection()
        total_new = 0

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
                    href = a.get("href")
                    full_url = href if href.startswith("http") else (config['domain'] + (href if href.startswith("/") else "/" + href))
                    if "new.xianbao.fun/haodan/" in full_url: continue

                    title = a.get_text(strip=True)
                    match_kw = next((kw for kw in KEYWORDS if kw.lower() in title.lower()), None)
                    if not match_kw: continue

                    # 使用北京时间
                    time_val = (datetime.utcnow() + timedelta(hours=8)).strftime("%H:%M")
                    site_entries.append((title, full_url, site_key, match_kw, time_val))

                if site_entries:
                    cursor = conn.cursor()
                    cursor.executemany('INSERT OR IGNORE INTO articles (title, url, site_source, match_keyword, original_time) VALUES(?,?,?,?,?)', site_entries)
                    site_new = cursor.rowcount
                    total_new += site_new
                    print(f"  [-] {config['name']}: 发现 {len(site_entries)} 条，新增 {site_new} 条")
            except Exception as e:
                print(f"  [!] {site_key} 抓取失败: {e}")

        conn.execute('INSERT OR REPLACE INTO scrape_log(id,last_scrape) VALUES(1,?)', (now_str,))
        conn.commit()
        user_count = conn.execute('SELECT COUNT(*) FROM visit_stats').fetchone()[0]
        conn.close()
        duration = time.time() - start_time
        print(f"[{now_str}] <<< 任务完成。新增: {total_new} 条 | 耗时: {duration:.2f}s | 独立访客: {user_count}\n")

def clean_html(html_content, site_key):
    if not html_content: 
        return ""
        
    soup = BeautifulSoup(html_content, "html.parser")
    
    # 遍历所有标签进行清洗
    for tag in soup.find_all(True):
        # --- 处理图片 ---
        if tag.name == 'img':
            src = tag.get('src', '')
            # 处理相对路径
            if src.startswith('/'): 
                src = SITES_CONFIG[site_key]['domain'] + src
            tag.attrs = {
                'src': f"/img_proxy?url={src}", 
                'loading': 'lazy', 
                'style': 'max-width:100%; height:auto; border-radius:8px;'
            }
            
        # --- 处理链接 (修复核心问题) ---
        elif tag.name == 'a':
            real_href = tag.get('href', '')
            # 补全相对路径
            if real_href.startswith('/'):
                real_href = SITES_CONFIG[site_key]['domain'] + real_href
            
            # 如果显示文字被截断（包含...），则强制显示完整 URL，防止误导
            display_text = tag.get_text(strip=True)
            if "..." in display_text and real_href.startswith('http'):
                tag.string = real_href 
            
            # 重新构建属性，必须保留 href
            tag.attrs = {
                'href': real_href,
                'target': '_blank',
                'rel': 'noopener noreferrer',
                'style': 'color: #007aff; text-decoration: underline; word-break: break-all;'
            }
            
        # --- 处理换行和段落 (保持结构) ---
        elif tag.name in ['br', 'p', 'div']:
            tag.attrs = {} # 清理 class, id 等冗余属性
            
        # --- 其他标签 ---
        else:
            tag.attrs = {} # 清理掉 style, class 等，只留纯标签
            
    return str(soup)

# ================== 4. 路由 ==================
@app.route('/')
def index():
    record_visit()
    tag = request.args.get('tag')
    page = request.args.get('page', 1, type=int)

    global last_scrape_time
    current_time = time.time()
    if page == 1 and not tag:
        if current_time - last_scrape_time > COOLDOWN_SECONDS:
            last_scrape_time = current_time
            threading.Thread(target=scrape_all_sites).start()
        else:
            print(f"  [Info] 冷却中，跳过抓取。")

    conn = get_db_connection()
    where, params = ("", []) if not tag else ("WHERE match_keyword = ?", [tag.strip()])
    total = conn.execute(f'SELECT COUNT(*) FROM articles {where}', params).fetchone()[0]
    db_data = conn.execute(f'SELECT id, title, original_time FROM articles {where} ORDER BY id DESC LIMIT ? OFFSET ?', params + [PER_PAGE, (page-1)*PER_PAGE]).fetchall()
    conn.close()

    articles = [{"title": r['title'], "view_link": f"/view?id={r['id']}", "time": r['original_time']} for r in db_data]
    tags = [{"name": "全部", "tag": None}] + [{"name": k, "tag": v} for k, v in BANK_KEYWORDS.items()] + [{"name": "红包", "tag": "红包"}]

    return render_template(
        'index.html',
        articles=articles,
        current_page=page,
        total_pages=(total+PER_PAGE-1)//PER_PAGE,
        current_tag=tag,
        bank_tag_list=tags
    )

@app.route("/view")
def view():
    article_id = request.args.get("id", type=int)
    conn = get_db_connection()
    row = conn.execute("SELECT url, title, site_source FROM articles WHERE id=?", (article_id,)).fetchone()
    
    if not row:
        conn.close()
        return "文章不存在"

    url, title, site_key = row["url"], row["title"], row["site_source"]
    
    # 尝试从数据库获取缓存
    cached = conn.execute("SELECT content FROM article_content WHERE url=?", (url,)).fetchone()
    
    if cached and len(cached['content']) > 100:
        content = clean_html(cached["content"], site_key)
    else:
        try:
            r = session.get(url, timeout=10)
            r.encoding = 'utf-8'
            soup = BeautifulSoup(r.text, "html.parser")
            
            # 采用测试脚本中的多选择器匹配逻辑
            selector_list = SITES_CONFIG[site_key]["content_selector"].split(',')
            node = None
            for sel in selector_list:
                temp_node = soup.select_one(sel.strip())
                if temp_node and len(temp_node.get_text(strip=True)) > 10:
                    node = temp_node
                    break
            
            raw_content = str(node) if node else "无法自动提取正文，请点击原文查看。"
            
            # 存入缓存
            conn.execute("INSERT OR REPLACE INTO article_content(url, content) VALUES(?,?)", (url, raw_content))
            conn.commit()
            content = clean_html(raw_content, site_key)
        except Exception as e:
            content = f"抓取详情页失败: {e}"

    conn.close()
    return render_template("detail.html", title=title, content=content, original_url=url)

@app.route('/logs')
def show_logs():
    conn = get_db_connection()
    articles = conn.execute('SELECT title, match_keyword, updated_at FROM articles ORDER BY id DESC LIMIT 50').fetchall()
    visitors = conn.execute('SELECT ip, visit_count, last_visit FROM visit_stats ORDER BY last_visit DESC LIMIT 30').fetchall()
    total_art = conn.execute('SELECT COUNT(*) FROM articles').fetchone()[0]
    total_vis = conn.execute('SELECT COUNT(*) FROM visit_stats').fetchone()[0]
    last_s = conn.execute('SELECT last_scrape FROM scrape_log WHERE id=1').fetchone()
    conn.close()
    return render_template('logs.html', articles=articles, visitors=visitors, total_articles=total_art, total_visitors=total_vis, last_scrape=last_s['last_scrape'] if last_s else "暂无记录")

@app.route('/img_proxy')
def img_proxy():
    url = request.args.get('url')
    if not url: return Response(status=400)
    try:
        r = requests.get(url, headers=HEADERS, stream=True, timeout=10)
        return Response(r.content, content_type=r.headers.get('Content-Type'))
    except:
        return Response(status=404)

# ================== 启动 ==================
if __name__ == '__main__':
    init_db()
    scheduler = BackgroundScheduler()
    scheduler.add_job(scrape_all_sites, 'interval', minutes=10, kwargs={'force': True})
    scheduler.start()
    print(">>> 监控助手已启动，端口: 8080")
    serve(app, host='0.0.0.0', port=8080, threads=10)

