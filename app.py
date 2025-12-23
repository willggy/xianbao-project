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
# 建议在 Zeabur 挂载硬盘到 /data 目录
DATA_DIR = "/app/data"
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "xianbao.db")

PER_PAGE = 30
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"}

# 全局状态变量
last_scrape_time = 0
COOLDOWN_SECONDS = 30
scrape_lock = threading.Lock()

session = requests.Session()
session.headers.update(HEADERS)
adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=2)
session.mount('http://', adapter)
session.mount('https://', adapter)

# ================== 2. 数据库核心逻辑 ==================

def get_db_connection():
    # check_same_thread=False 允许在多线程环境下使用同一个连接（由 Flask 处理）
    conn = sqlite3.connect(DB_PATH, timeout=20, check_same_thread=False)
    conn.row_factory = sqlite3.Row 
    return conn

def init_db():
    """强力初始化：即使数据库已存在也会检查并补齐缺失的表"""
    conn = get_db_connection()
    try:
        conn.execute('PRAGMA journal_mode=WAL;') # 开启 WAL 模式提高并发性能
        
        # 补齐所有可能缺失的表
        sql_statements = [
            '''CREATE TABLE IF NOT EXISTS articles(
                id INTEGER PRIMARY KEY AUTOINCREMENT, 
                title TEXT, url TEXT UNIQUE, site_source TEXT,
                match_keyword TEXT, original_time TEXT, 
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
            
            '''CREATE TABLE IF NOT EXISTS visit_stats(
                ip TEXT PRIMARY KEY, 
                visit_count INTEGER DEFAULT 1, 
                last_visit TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
            
            '''CREATE TABLE IF NOT EXISTS article_content(
                url TEXT PRIMARY KEY, content TEXT, 
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
            
            '''CREATE TABLE IF NOT EXISTS scrape_log(
                id INTEGER PRIMARY KEY, last_scrape TIMESTAMP)'''
        ]
        
        for sql in sql_statements:
            conn.execute(sql)
            
        conn.commit()
        print(f"[{datetime.now()}] 数据库表结构校验/补全成功。")
    except Exception as e:
        print(f"数据库初始化失败: {e}")
    finally:
        conn.close()

def record_visit():
    """访客统计逻辑"""
    # 获取真实 IP (处理代理)
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if not ip: return
    
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
    except Exception as e:
        print(f"访客记录失败: {e}")
    finally:
        conn.close()

# ================== 3. 抓取引擎 ==================

def scrape_all_sites(force=False):
    if scrape_lock.locked() and not force: return
    with scrape_lock:
        start_time = time.time()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{now_str}] >>> 启动抓取任务...")
        
        conn = get_db_connection()
        total_new = 0
        
        for site_key, config in SITES_CONFIG.items():
            try:
                resp = session.get(config['list_url'], timeout=12)
                resp.encoding = 'utf-8'
                soup = BeautifulSoup(resp.text, "html.parser")
                
                site_entries = []
                for item in soup.select(config['list_selector']):
                    a = item.select_one("a[href*='view'], a[href*='thread'], a[href*='post'], a[href*='.htm']") or item.find("a")
                    if not a: continue
                    
                    href = a.get("href")
                    full_url = href if href.startswith("http") else (config['domain'] + (href if href.startswith("/") else "/" + href))
                    
                    # 路径排除逻辑
                    if "new.xianbao.fun/haodan/" in full_url: continue

                    title = a.get_text(strip=True)
                    match_kw = next((kw for kw in KEYWORDS if kw.lower() in title.lower()), None)
                    if not match_kw: continue
                    
                    site_entries.append((title, full_url, site_key, match_kw, datetime.now().strftime("%H:%M")))

                if site_entries:
                    cursor = conn.cursor()
                    # 批量插入新数据，忽略重复 URL
                    cursor.executemany('INSERT OR IGNORE INTO articles (title, url, site_source, match_keyword, original_time) VALUES(?,?,?,?,?)', site_entries)
                    site_new = cursor.rowcount
                    total_new += site_new
                    print(f"  [-] {config['name']}: 本次新增 {site_new} 条")
                    
            except Exception as e:
                print(f"  [!] {site_key} 抓取异常: {e}")

        # 记录最后抓取成功时间
        conn.execute('INSERT OR REPLACE INTO scrape_log(id, last_scrape) VALUES(1, ?)', (now_str,))
        conn.commit()
        conn.close()
        
        duration = time.time() - start_time
        print(f"[{now_str}] <<< 抓取结束。总计新增: {total_new} 条 | 耗时: {duration:.2f}s\n")

# ================== 4. 路由处理 ==================

@app.route('/')
def index():
    record_visit()
    tag = request.args.get('tag')
    page = request.args.get('page', 1, type=int)
    
    # 30秒冷却逻辑：防止频繁刷新导致封IP
    global last_scrape_time
    if page == 1 and not tag:
        current_ts = time.time()
        if current_ts - last_scrape_time > COOLDOWN_SECONDS:
            last_scrape_time = current_ts
            threading.Thread(target=scrape_all_sites).start()
        else:
            print(f"  [Info] 冷却中（剩余 {int(COOLDOWN_SECONDS - (current_ts - last_scrape_time))}s），跳过重复抓取。")
    
    conn = get_db_connection()
    where, params = ("", []) if not tag else ("WHERE match_keyword = ?", [tag.strip()])
    total = conn.execute(f'SELECT COUNT(*) FROM articles {where}', params).fetchone()[0]
    db_data = conn.execute(f'SELECT id, title, original_time FROM articles {where} ORDER BY id DESC LIMIT ? OFFSET ?', params + [PER_PAGE, (page-1)*PER_PAGE]).fetchall()
    conn.close()
    
    articles = [{"title": r['title'], "view_link": f"/view?id={r['id']}", "time": r['original_time']} for r in db_data]
    tags = [{"name": "全部", "tag": None}] + [{"name": k, "tag": v} for k, v in BANK_KEYWORDS.items()] + [{"name": "红包", "tag": "红包"}]
    return render_template('index.html', articles=articles, current_page=page, total_pages=(total+PER_PAGE-1)//PER_PAGE, current_tag=tag, bank_tag_list=tags)

@app.route('/logs')
def show_logs():
    """网页版实时监控中心"""
    conn = get_db_connection()
    try:
        # 获取最新的抓取记录和访客信息
        articles = conn.execute('SELECT title, match_keyword, updated_at FROM articles ORDER BY id DESC LIMIT 50').fetchall()
        visitors = conn.execute('SELECT ip, visit_count, last_visit FROM visit_stats ORDER BY last_visit DESC LIMIT 30').fetchall()
        
        # 统计汇总数据
        total_art = conn.execute('SELECT COUNT(*) FROM articles').fetchone()[0]
        total_vis = conn.execute('SELECT COUNT(*) FROM visit_stats').fetchone()[0]
        last_s = conn.execute('SELECT last_scrape FROM scrape_log WHERE id=1').fetchone()
        
        conn.close()
        return render_template('logs.html', 
                               articles=articles, 
                               visitors=visitors, 
                               total_articles=total_art, 
                               total_visitors=total_vis, 
                               last_scrape=last_s['last_scrape'] if last_s else "暂无记录")
    except Exception as e:
        conn.close()
        return f"<h3>日志加载失败</h3><p>可能是由于数据库表正在初始化，请刷新重试。<br>错误详情: {e}</p>"

@app.route('/view')
def view():
    article_id = request.args.get('id', type=int)
    conn = get_db_connection()
    row = conn.execute('SELECT url, title, site_source FROM articles WHERE id=?', (article_id,)).fetchone()
    if not row: 
        conn.close()
        return "文章不存在"
    
    url, title, site_key = row['url'], row['title'], row['site_source']
    # 尝试读取缓存的正文
    cached = conn.execute('SELECT content FROM article_content WHERE url=?', (url,)).fetchone()
    conn.close()
    
    if cached and len(cached['content']) > 50:
        content = cached['content'] # 此处建议配合 clean_html 函数使用
    else:
        # 此处省略具体的 BeautifulSoup 内容提取逻辑，同之前版本
        content = f"正文正在抓取中或提取失败，请访问原文：<a href='{url}'>{url}</a>"
    
    return render_template('detail.html', content=content, title=title, original_url=url)

@app.route('/img_proxy')
def img_proxy():
    url = request.args.get('url')
    if not url: return Response(status=400)
    try:
        r = requests.get(url, headers=HEADERS, timeout=8)
        return Response(r.content, content_type=r.headers.get('Content-Type'))
    except: return Response(status=404)

# ================== 5. 服务启动 ==================

if __name__ == '__main__':
    # 1. 启动时立即执行一次数据库补表检查
    init_db()
    
    # 2. 启动定时任务 (每 10 分钟自动运行)
    scheduler = BackgroundScheduler()
    scheduler.add_job(scrape_all_sites, 'interval', minutes=10, kwargs={'force': True})
    scheduler.start()
    
    # 3. 使用 Waitress 生产级服务器运行
    print(">>> 监控助手已就绪，正在监听端口 8080...")
    serve(app, host='0.0.0.0', port=8080, threads=8)

