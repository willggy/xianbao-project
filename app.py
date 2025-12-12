# -*- coding: utf-8 -*-
import os
import re
import sqlite3
import atexit
import threading
from datetime import datetime, timedelta
import requests
from flask import Flask, render_template, request
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler
from waitress import serve

app = Flask(__name__)

# ---------------- Zeabur 配置 ----------------
DATA_DIR = os.environ.get("DATA_DIR", "./data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "xianbao.db")
print("Server starting...", "DATA_DIR=" + DATA_DIR, "DB_PATH=" + DB_PATH)

REQUEST_TIMEOUT = 15
PER_PAGE = 30
TARGET_DOMAIN = "https://new.xianbao.fun"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"
}

BANK_KEYWORDS = {"农行": "农", "工行": "工", "建行": "建", "中行": "中"}
KEYWORDS = ["hang", "行", "立减金", "ljj", "水", "红包"] + list(BANK_KEYWORDS.values())
EXCLUSION_KEYWORDS = ["排行榜", "排 行 榜", "榜单", "置顶"]

# ---------------- 数据库初始化 ----------------
def init_db_if_needed():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS articles(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        url TEXT UNIQUE NOT NULL,
        match_keyword TEXT,
        original_time TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS article_content(
        url TEXT PRIMARY KEY,
        content TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS scrape_log(
        id INTEGER PRIMARY KEY,
        last_scrape TIMESTAMP
    )''')
    conn.commit()
    conn.close()

init_db_if_needed()

# ---------------- 抓取列表逻辑 ----------------
def scrape_list(force=False):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # 检查时间间隔（30秒内不重复抓取）
    if not force:
        c.execute('SELECT last_scrape FROM scrape_log WHERE id=1')
        row = c.fetchone()
        if row and row[0]:
            try:
                last_time = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
                if datetime.now() - last_time < timedelta(seconds=30):
                    conn.close()
                    return False
            except ValueError:
                pass

    # print(f"[{datetime.now().strftime('%H:%M:%S')}] 开始检查更新...") # 可选：如觉得太吵可注释
    url = TARGET_DOMAIN + "/"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.encoding = 'utf-8'
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("tr, li")
        
        insert_list = []
        for row in rows:
            a_tag = row.select_one("a[href*='view']") or row.select_one("a[href*='thread']") or row.select_one("a")
            if not a_tag: continue
            
            title = a_tag.get_text(strip=True)
            href = a_tag.get("href")
            
            if not title or not href: continue
            if any(e in title for e in EXCLUSION_KEYWORDS): continue
            
            match_kw = None
            for kw in KEYWORDS:
                if kw.lower() in title.lower():
                    match_kw = kw
                    break
            if not match_kw: continue
            
            if href.startswith("/"): href = TARGET_DOMAIN + href
            elif not href.startswith("http"): href = TARGET_DOMAIN + "/" + href
            
            row_text = row.get_text(" ", strip=True)
            text_without_title = row_text.replace(title, "")
            time_match = re.search(r'(\d{2}-\d{2}|\d{2}:\d{2}|\d{4}-\d{2}-\d{2})', text_without_title)
            original_time = time_match.group(1) if time_match else datetime.now().strftime("%H:%M")
            
            insert_list.append((title, href, match_kw.strip(), original_time))

        # 记录抓取前的总数，用于计算新增（近似值，因为IGNORE会忽略重复）
        # 这里为了准确显示新增条数，我们可以用 rowcount，但在IGNORE下 rowcount 行为取决于驱动
        # 简单处理：只要列表不为空就尝试插入
        
        c.executemany('''INSERT OR IGNORE INTO articles
            (title, url, match_keyword, original_time, updated_at)
            VALUES(?,?,?,?,CURRENT_TIMESTAMP)
        ''', insert_list)
        
        added_count = c.rowcount # SQLite中 IGNORE 时重复的行 rowcount 为 0
        
        c.execute('INSERT OR REPLACE INTO scrape_log(id,last_scrape) VALUES(1,?)',
                  (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))
        conn.commit()
        
        # --- 日志修改：只写更新了几条 ---
        if added_count > 0:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 抓取完成，新增 {added_count} 条")
        
        return True
    except Exception as e:
        print(f"Scrape error: {e}")
        return False
    finally:
        conn.close()

# ---------------- 获取列表数据 ----------------
def get_list_data(page=1, per_page=PER_PAGE, tag_keyword=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    where_clause = ""
    params = []
    
    if tag_keyword:
        clean_tag = tag_keyword.strip()
        if clean_tag in BANK_KEYWORDS.values():
            where_clause = "WHERE match_keyword = ? AND match_keyword != ?"
            params.extend([clean_tag, "行"])
        else:
            where_clause = "WHERE match_keyword = ?"
            params.append(clean_tag)
            
    c.execute(f'SELECT COUNT(*) FROM articles {where_clause}', params)
    total_count = c.fetchone()[0]
    
    if total_count == 0 and not tag_keyword:
        conn.close()
        if scrape_list(force=True):
            return get_list_data(page, per_page, tag_keyword)
        else:
            return [], 0, 1
            
    total_pages = (total_count + per_page - 1) // per_page if total_count else 1
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
            "title": row[1],
            "view_link": f"/view?id={row[0]}",
            "time": row[3]
        })
    return data, total_count, total_pages

# ---------------- 获取文章内容 ----------------
def get_article_content(article_id):
    if not article_id:
        return "文章不存在", "Error", None
        
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT url, title FROM articles WHERE id=?', (article_id,))
    res = c.fetchone()
    if not res:
        conn.close()
        return "文章不存在", "Error", None
    target_url, title = res
    
    c.execute('SELECT content FROM article_content WHERE url=?', (target_url,))
    cached = c.fetchone()
    conn.close()

    def clean(html_content):
        if not html_content: return ""
        soup = BeautifulSoup(html_content, "html.parser")
        
        # 移除常规干扰
        for cls in ['head-info', 'mochu_us_shoucang', 'mochu-us-coll', 'xg1', 'y', 'top_author_desc', 'rate', 'modact']:
            for tag in soup.find_all(class_=cls): tag.decompose()
        
        # 移除标题防止重复
        for h1 in soup.find_all('h1'): h1.decompose()
        for subject in soup.select('#thread_subject, .ts'): subject.decompose()

        # 图片处理
        for img in soup.find_all('img'):
            img['loading'] = 'lazy'
            if 'width' in img.attrs: del img.attrs['width']
            if 'height' in img.attrs: del img.attrs['height']
            if img.get('src', '').startswith('/'):
                img['src'] = TARGET_DOMAIN + img['src']

        text = str(soup)
        link_ptn = re.compile(r'(?<!["\'/=])(\bhttps?://[^\s<>"\'\u4e00-\u9fa5]+)')
        text = link_ptn.sub(lambda m: f'<a href="{m.group(1)}" target="_blank">{m.group(1)}</a>', text)
        return text

    # --- 命中缓存 ---
    if cached and cached[0]:
        # 日志已移除：不用写点哪个文章了
        return clean(cached[0]), title, target_url

    # --- 无缓存，抓取详情 ---
    try:
        r = requests.get(target_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.encoding = 'utf-8'
        full_soup = BeautifulSoup(r.text, 'html.parser')

        # === 针对性移除指定ID元素 ===
        # 1. 移除评论框: //*[@id="commentbox"]
        for cb in full_soup.select('#commentbox'):
            cb.decompose()

        # 2. 移除主盒子下第一个div: //*[@id="mainbox"]/div[1]
        mainbox = full_soup.select_one('#mainbox')
        if mainbox:
            # 获取 direct children divs
            direct_divs = mainbox.find_all('div', recursive=False)
            if direct_divs:
                direct_divs[0].decompose()
            
            # 3. 移除文章下第一个div: //*[@id="mainbox"]/article/div[1]
            article = mainbox.find('article', recursive=False)
            if article:
                art_divs = article.find_all('div', recursive=False)
                if art_divs:
                    art_divs[0].decompose()
        
        # 尝试精简提取
        node = full_soup.find('td', class_='t_f') or \
               full_soup.find('div', class_='message') or \
               full_soup.select_one('div[class*="content"]')
               
        if node:
            content = str(node)
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            try:
                c.execute('INSERT OR REPLACE INTO article_content(url, content, updated_at) VALUES(?,?,CURRENT_TIMESTAMP)',
                          (target_url, content))
                conn.commit()
            finally:
                conn.close()
            # 日志已移除
            return clean(content), title, target_url
            
        return "无法提取正文", title, target_url
    except Exception as e:
        return f"Error: {e}", title, target_url

# ---------------- 路由 ----------------
@app.route('/')
def index():
    tag = request.args.get('tag')
    page = request.args.get('page', 1, type=int)
    
    # 异步触发抓取，不阻塞页面加载
    if page == 1 and not tag:
        threading.Thread(target=scrape_list, args=(False,)).start()
    
    articles, total, pages = get_list_data(page, PER_PAGE, tag)
    tags = [{"name": "全部", "tag": None}] + \
           [{"name": k, "tag": v} for k, v in BANK_KEYWORDS.items()] + \
           [{"name": "立减金", "tag": "立减金"}, {"name": "红包", "tag": "红包"}]
           
    return render_template('index.html', articles=articles, current_page=page, total_pages=pages,
                           current_tag=tag, bank_tag_list=tags)

@app.route('/view')
def view():
    # 点文章不触发 scrape_list，只获取详情
    article_id = request.args.get('id', type=int)
    content, title, original_url = get_article_content(article_id)
    return render_template('detail.html', content=content, title=title, original_url=original_url)

@app.route('/health')
def health():
    return "ok"

if __name__ == '__main__':
    if not os.path.exists(DB_PATH) or os.path.getsize(DB_PATH) < 1000:
        scrape_list(force=True)
        
    scheduler = BackgroundScheduler()
    scheduler.add_job(scrape_list, 'interval', minutes=10, kwargs={'force': True})
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown())

    port = int(os.environ.get('PORT', 8080))
    serve(app, host='0.0.0.0', port=port)