# -*- coding: utf-8 -*-
import os
import sys
import re
import sqlite3
import atexit
import requests
from datetime import datetime
from flask import Flask, render_template, request
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler
from waitress import serve  # 生产环境服务器

app = Flask(__name__)

# --- 1. Zeabur 专用配置 ---

# 挂载卷路径：Zeabur 推荐挂载在 /app/data
# 如果环境变量没设置，默认就用 /app/data
DATA_DIR = os.environ.get('DATA_DIR', '/app/data')

# 确保文件夹存在
if not os.path.exists(DATA_DIR):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception as e:
        print(f"Warning: Could not create {DATA_DIR}, falling back to current dir. Error: {e}")
        DATA_DIR = '.'

# 数据库文件路径
DB_PATH = os.path.join(DATA_DIR, 'xianbao.db')

# 基础配置
REQUEST_TIMEOUT = 15
PER_PAGE = 30
TARGET_DOMAIN = "https://new.xianbao.fun"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"
}

# --- 2. 关键词配置 ---
BANK_KEYWORDS = {
    "农行": "农", "工行": "工", "建行": "建", "中行": "中"
}

# 已移除 "券"
KEYWORDS = ["hang", "行", "立减金", "ljj", "水", "红包"] + list(BANK_KEYWORDS.values())
EXCLUSION_KEYWORDS = ["排行榜", "排 行 榜", "榜单", "置顶"]

# --- 3. 数据库初始化 ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # 核心表
    c.execute('''CREATE TABLE IF NOT EXISTS articles(
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        title TEXT NOT NULL, 
        url TEXT UNIQUE NOT NULL, 
        match_keyword TEXT, 
        original_time TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    # 内容表
    c.execute("CREATE TABLE IF NOT EXISTS article_content(url TEXT PRIMARY KEY, content TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    conn.commit()
    conn.close()

# --- 4. 爬虫逻辑 ---
def scrape_list():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 开始抓取...")
    url = TARGET_DOMAIN + "/"
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT) 
        resp.encoding = 'utf-8'
        soup = BeautifulSoup(resp.text, "html.parser")
        
        rows = soup.select("tr, li")
        count = 0
        
        for row in rows:
            a_tag = row.select_one("a[href*='view']") or row.select_one("a[href*='thread']") or row.select_one("a")
            if not a_tag: continue
            
            title = a_tag.get_text(strip=True)
            href = a_tag.get("href")
            
            if not title or not href: continue
            if any(e in title for e in EXCLUSION_KEYWORDS): continue
            
            # 关键词匹配
            match_kw = None
            title_lower = title.lower()
            for kw in KEYWORDS:
                if kw.lower() in title_lower:
                    match_kw = kw
                    break
            if not match_kw: continue
            
            # 链接补全
            if href.startswith("/"): href = TARGET_DOMAIN + href
            elif not href.startswith("http"): href = TARGET_DOMAIN + "/" + href

            # 时间提取
            row_text = row.get_text(" ", strip=True) 
            text_without_title = row_text.replace(title, "")
            time_match = re.search(r'(\d{2}-\d{2}|\d{2}:\d{2}|\d{4}-\d{2}-\d{2})', text_without_title)
            original_time = time_match.group(1) if time_match else datetime.now().strftime("%H:%M")

            c.execute('''
                INSERT OR IGNORE INTO articles
                (title, url, match_keyword, original_time, updated_at) 
                VALUES(?,?,?,?,CURRENT_TIMESTAMP)
            ''', (title, href, match_kw.strip(), original_time))
            count += 1
        
        conn.commit()
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 抓取结束，新增处理 {count} 条。")
        return True
    except Exception as e:
        sys.stderr.write(f"Scrape error: {e}\n")
        return False
    finally:
        conn.close()

# --- 5. 获取列表数据 ---
def get_list_data(page=1, per_page=PER_PAGE, tag_keyword=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    where_clause = ""
    params = []
    
    if tag_keyword:
        clean_tag = tag_keyword.strip() 
        if clean_tag in BANK_KEYWORDS.values():
            where_clause = "WHERE match_keyword = ? AND match_keyword != ?"
            params.append(clean_tag)
            params.append('行') 
        else:
            where_clause = "WHERE match_keyword = ?"
            params.append(clean_tag)

    c.execute(f'SELECT COUNT(*) FROM articles {where_clause}', params)
    total_count = c.fetchone()[0]
    
    # 数据库为空时紧急抓取
    if total_count == 0 and not tag_keyword:
        conn.close()
        if scrape_list(): return get_list_data(page, per_page, tag_keyword) 
        else: return [], 0, 1 
            
    total_pages = (total_count + per_page - 1) // per_page if total_count > 0 else 1
    offset = (page - 1) * per_page
    
    c.execute(f'''
        SELECT id, title, url, original_time 
        FROM articles 
        {where_clause}
        ORDER BY id DESC 
        LIMIT ? OFFSET ?
    ''', params + [per_page, offset])
    
    db_data = c.fetchall()
    conn.close()

    data = []
    for row in db_data:
        data.append({
            'title': row[1],
            'view_link': f"/view?id={row[0]}",
            'time': row[3]
        })
    return data, total_count, total_pages

# --- 6. 获取详情与清洗 ---
def get_article_content(article_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT url, title FROM articles WHERE id = ?', (article_id,))
    res = c.fetchone()
    if not res:
        conn.close()
        return '文章不存在', 'Error', None
    target_url, title = res
    c.execute('SELECT content FROM article_content WHERE url = ?', (target_url,))
    cached = c.fetchone()
    conn.close()
    
    def clean(html):
        if not html: return ""
        soup = BeautifulSoup(html, 'html.parser')
        
        # 移除垃圾标签
        for cls in ['head-info', 'mochu_us_shoucang', 'mochu-us-coll', 'xg1', 'y', 'top_author_desc']:
            for tag in soup.find_all(class_=cls): tag.decompose()

        # 图片处理
        for img in soup.find_all('img'):
            img['loading'] = 'lazy'
            if 'width' in img.attrs: del img.attrs['width']
            if 'height' in img.attrs: del img.attrs['height']

        # 文本处理
        for a in soup.find_all('a'): a.replace_with(a.get_text())
        for h in soup.find_all(re.compile('^h[1-6]$')): h.decompose()
        for t in soup.find_all(string=re.compile(r"线报酷内部")): 
            if t.parent.name == 'p': t.parent.decompose()
            
        text = str(soup)
        # 正则移除残留文本
        text = re.sub(r"微博线报.*?文章正文", "", text, flags=re.IGNORECASE)
        text = re.sub(r"首页赚客吧文章正文", "", text, flags=re.IGNORECASE)
        text = re.sub(r"[\u4e00-\u9fa5a-zA-Z0-9]{2,20}20\d{2}年\d{1,2}月\d{1,2}日.*?(举报)?", "", text)
        text = re.sub(r"欢迎您发表评论.*", "", text, flags=re.DOTALL)
        text = re.sub(r"复制(文案|链接)", "", text)
        
        # 链接复原
        link_ptn = re.compile(r'(?<!["\'/=])(\bhttps?://[^\s<>"\'\u4e00-\u9fa5]+)')
        text = link_ptn.sub(lambda m: f'<a href="{m.group(1).rstrip(".,;:")}" target="_blank">{m.group(1).rstrip(".,;:")}</a>', text)
        
        return re.sub(r'(\s*\n\s*){2,}', '\n\n', text).strip()

    if cached and cached[0]: return clean(cached[0]), title, target_url
    
    try:
        r = requests.get(target_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.encoding = 'utf-8'
        soup = BeautifulSoup(r.text, 'html.parser')
        node = soup.find('td', class_='t_f') or soup.find('div', class_='message') or soup.select_one('div[class*="content"]')
        
        if node:
            content = str(node)
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            try:
                c.execute('INSERT OR REPLACE INTO article_content (url, content, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)', (target_url, content))
                conn.commit()
            except: pass
            finally: conn.close()
            return clean(content), title, target_url
        return '无法提取正文', title, target_url
    except Exception as e: return f'Error: {e}', title, target_url

# --- 7. 路由 ---
@app.route('/')
def index():
    tag = request.args.get('tag')
    page = request.args.get('page', 1, type=int)
    articles, total, pages = get_list_data(page, PER_PAGE, tag)
    
    tags = [{"name": "全部", "tag": None}] + \
           [{"name": k, "tag": v} for k, v in BANK_KEYWORDS.items()] + \
           [{"name": "立减金", "tag": "立减金"}, {"name": "红包", "tag": "红包"}]
           
    return render_template('index.html', articles=articles, current_page=page, total_pages=pages, current_tag=tag, bank_tag_list=tags)

@app.route('/view')
def view():
    c, t, _ = get_article_content(request.args.get('id', type=int))
    return render_template('detail.html', content=c, title=t)

# --- 8. 启动入口 (生产环境) ---
if __name__ == '__main__':
    print(f"Server starting...")
    print(f"Data directory: {DATA_DIR}")
    
    # 1. 初始化数据库
    init_db()
    
    # 2. 检查数据，如果为空则立即抓取
    if not os.path.exists(DB_PATH) or os.path.getsize(DB_PATH) < 1000:
        print("Database empty, performing initial scrape...")
        scrape_list()
        
    # 3. 启动后台定时任务 (每10分钟)
    scheduler = BackgroundScheduler()
    scheduler.add_job(func=scrape_list, trigger="interval", minutes=10)
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown())
    
    # 4. 启动 Waitress 服务器 (Zeabur 生产环境专用)
    # Zeabur 会通过环境变量 PORT 告诉我们用哪个端口
    port = int(os.environ.get('PORT', 8080))
    print(f"Running waitress server on port {port}")
    serve(app, host='0.0.0.0', port=port)
