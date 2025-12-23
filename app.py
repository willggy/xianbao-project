import os
import sqlite3
import threading
import time
from datetime import datetime

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

BANK_KEYWORDS = {"农行": "农", "工行": "工", "建行": "建", "中行": "中"}
KEYWORDS = list(BANK_KEYWORDS.values()) + [
    "立减金", "红包", "话费", "水", "毛", "招", "hang", "信", "移动", 
    "联通", "京东", "支付宝", "微信", "流量", "话费券", "充值", "zfb"
]

app = Flask(__name__)
# 确保在 Zeabur 挂载硬盘时路径正确，建议设置为 /data/xianbao.db
DATA_DIR = "./data"
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "xianbao.db")

PER_PAGE = 30
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"}

# 全局变量
last_scrape_time = 0
COOLDOWN_SECONDS = 30
scrape_lock = threading.Lock()

session = requests.Session()
session.headers.update(HEADERS)
adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=2)
session.mount('http://', adapter)
session.mount('https://', adapter)

# ================== 2. 数据库与统计逻辑 ==================
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row 
    return conn

def init_db():
    """初始化数据库：采用 IF NOT EXISTS 确保旧库也能自动升级补表"""
    conn = get_db_connection()
    try:
        conn.execute('PRAGMA journal_mode=WAL;')
        # 1. 文章基础表
        conn.execute('''CREATE TABLE IF NOT EXISTS articles(
            id INTEGER PRIMARY KEY AUTOINCREMENT, 
            title TEXT, url TEXT UNIQUE, site_source TEXT,
            match_keyword TEXT, original_time TEXT, 
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        # 2. 访客统计表 (关键：解决 OperationalError: no such table 报错)
        conn.execute('''CREATE TABLE IF NOT EXISTS visit_stats(
            ip TEXT PRIMARY KEY, 
            visit_count INTEGER DEFAULT 1, 
            last_visit TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        # 3. 内容缓存表
        conn.execute('CREATE TABLE IF NOT EXISTS article_content(url TEXT PRIMARY KEY, content TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
        # 4. 抓取日志表
        conn.execute('CREATE TABLE IF NOT EXISTS scrape_log(id INTEGER PRIMARY KEY, last_scrape TIMESTAMP)')
        conn.commit()
        print(f"[{datetime.now()}] 数据库初始化/检查成功。")
    except Exception as e:
        print(f"数据库初始化失败: {e}")
    finally:
        conn.close()

def record_visit():
    """记录访问者IP，通过 ON CONFLICT 实现不同人的统计"""
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    conn = get_db_connection()
    try:
        conn.execute('''
            INSERT INTO visit_stats (ip, visit_count, last_visit) 
            VALUES (?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(ip) DO UPDATE SET 
                visit_count = visit_count + 1,
                last_visit = CURRENT_TIMESTAMP
        ''', (ip,))
        conn.commit()
    except: pass
    finally: conn.close()

# ================== 3. 抓取与清洗逻辑 ==================
def scrape_all_sites(force=False):
    if scrape_lock.locked(): return
    with scrape_lock:
        start_time = time.time()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{now_str}] >>> 开始抓取...")
        
        conn = get_db_connection()
        total_new = 0
        
        for site_key, config in SITES_CONFIG.items():
            try:
                resp = session.get(config['list_url'], timeout=10)
                resp.encoding = 'utf-8'
                soup = BeautifulSoup(resp.text, "html.parser")
                
                site_entries = []
                for item in soup.select(config['list_selector']):
                    a = item.select_one("a[href*='view'], a[href*='thread'], a[href*='post'], a[href*='.htm']") or item.find("a")
                    if not a: continue
                    
                    href = a.get("href")
                    full_url = href if href.startswith("http") else (config['domain'] + (href if href.startswith("/") else "/" + href))
                    
                    # 过滤逻辑
                    if "new.xianbao.fun/haodan/" in full_url: continue
                    title = a.get_text(strip=True)
                    match_kw = next((kw for kw in KEYWORDS if kw.lower() in title.lower()), None)
                    if not match_kw: continue
                    
                    site_entries.append((title, full_url, site_key, match_kw, datetime.now().strftime("%H:%M")))

                if site_entries:
                    cursor = conn.cursor()
                    cursor.executemany('INSERT OR IGNORE INTO articles (title, url, site_source, match_keyword, original_time) VALUES(?,?,?,?,?)', site_entries)
                    site_new = cursor.rowcount
                    total_new += site_new
                    print(f"  [-] {config['name']}: 扫描到 {len(site_entries)} 条，新增 {site_new} 条")
            except Exception as e:
                print(f"  [!] {site_key} 抓取异常: {e}")

        conn.execute('INSERT OR REPLACE INTO scrape_log(id,last_scrape) VALUES(1,?)', (now_str,))
        conn.commit()
        conn.close()
        
        duration = time.time() - start_time
        print(f"[{now_str}] <<< 结束。新增: {total_new} 条 | 耗时: {duration:.2f}s\n")

def clean_html(html_content, site_key):
    if not html_content: return ""
    soup = BeautifulSoup(html_content, "html.parser")
    for tag in soup.find_all(True):
        if tag.name != 'img': 
            tag.attrs = {}
        else:
            src = tag.get('src', '')
            if src.startswith('/'): src = SITES_CONFIG[site_key]['domain'] + src
            tag.attrs = {'src': f"/img_proxy?url={src}", 'style': 'max-width:100%; height:auto; border-radius:8px;'}
    return str(soup)

# ================== 4. 路由逻辑 ==================
@app.route('/')
def index():
    record_visit()
    tag = request.args.get('tag')
    page = request.args.get('page', 1, type=int)
    
    # 30秒刷新冷却逻辑
    global last_scrape_time
    if page == 1 and not tag:
        if time.time() - last_scrape_time > COOLDOWN_SECONDS:
            last_scrape_time = time.time()
            threading.Thread(target=scrape_all_sites).start()
        else:
            print("  [Info] 30秒内已抓取过，跳过当前刷新触发。")
    
    conn = get_db_connection()
    where, params = ("", []) if not tag else ("WHERE match_keyword = ?", [tag.strip()])
    total = conn.execute(f'SELECT COUNT(*) FROM articles {where}', params).fetchone()[0]
    db_data = conn.execute(f'SELECT id, title, original_time FROM articles {where} ORDER BY id DESC LIMIT ? OFFSET ?', params + [PER_PAGE, (page-1)*PER_PAGE]).fetchall()
    conn.close()
    
    articles = [{"title": r['title'], "view_link": f"/view?id={r['id']}", "time": r['original_time']} for r in db_data]
    tags = [{"name": "全部", "tag": None}] + [{"name": k, "tag": v} for k, v in BANK_KEYWORDS.items()] + [{"name": "红包", "tag": "红包"}]
    return render_template('index.html', articles=articles, current_page=page, total_pages=(total+PER_PAGE-1)//PER_PAGE, current_tag=tag, bank_tag_list=tags)

@app.route('/view')
def view():
    article_id = request.args.get('id', type=int)
    conn = get_db_connection()
    row = conn.execute('SELECT url, title, site_source FROM articles WHERE id=?', (article_id,)).fetchone()
    if not row: return "文章不存在"
    
    url, title, site_key = row['url'], row['title'], row['site_source']
    cached = conn.execute('SELECT content FROM article_content WHERE url=?', (url,)).fetchone()
    
    if cached and len(cached['content']) > 50:
        content = clean_html(cached['content'], site_key)
    else:
        try:
            r = session.get(url, timeout=10)
            r.encoding = 'utf-8'
            soup = BeautifulSoup(r.text, 'html.parser')
            raw_content = ""
            config = SITES_CONFIG.get(site_key)
            for sel in [s.strip() for s in config['content_selector'].split(',')]:
                node = soup.select_one(sel)
                if node and len(node.get_text(strip=True)) > 10:
                    raw_content = str(node)
                    break
            
            if raw_content:
                conn.execute('INSERT OR REPLACE INTO article_content(url, content) VALUES(?,?)', (url, raw_content))
                conn.commit()
                content = clean_html(raw_content, site_key)
            else:
                content = "抓取正文失败，请点击下方查看原文。"
        except Exception as e:
            content = f"加载异常: {e}"
    conn.close()
    return render_template('detail.html', content=content, title=title, original_url=url)

@app.route('/logs')
def show_logs():
    """查看抓取日志和访客统计"""
    conn = get_db_connection()
    try:
        articles = conn.execute('SELECT title, match_keyword, updated_at FROM articles ORDER BY id DESC LIMIT 50').fetchall()
        visitors = conn.execute('SELECT ip, visit_count, last_visit FROM visit_stats ORDER BY last_visit DESC LIMIT 30').fetchall()
        total_art = conn.execute('SELECT COUNT(*) FROM articles').fetchone()[0]
        total_vis = conn.execute('SELECT COUNT(*) FROM visit_stats').fetchone()[0]
        last_s = conn.execute('SELECT last_scrape FROM scrape_log WHERE id=1').fetchone()
        return render_template('logs.html', articles=articles, visitors=visitors, total_articles=total_art, total_visitors=total_vis, last_scrape=last_s['last_scrape'] if last_s else "暂无记录")
    except Exception as e:
        return f"日志页面加载失败，可能是数据库表尚未创建完毕，请刷新首页后再试。错误: {e}"
    finally:
        conn.close()

@app.route('/img_proxy')
def img_proxy():
    url = request.args.get('url')
    if not url: return Response(status=400)
    try:
        r = requests.get(url, headers=HEADERS, stream=True, timeout=10)
        return Response(r.content, content_type=r.headers.get('Content-Type'))
    except: return Response(status=404)

if __name__ == '__main__':
    init_db()
    scheduler = BackgroundScheduler()
    scheduler.add_job(scrape_all_sites, 'interval', minutes=10, kwargs={'force': True})
    scheduler.start()
    serve(app, host='0.0.0.0', port=8080, threads=10)
