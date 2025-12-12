# -*- coding: utf-8 -*-
from flask import Flask, render_template, request
import requests
from bs4 import BeautifulSoup
import sqlite3, os, sys, time, re, atexit
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from waitress import serve

app = Flask(__name__)

# --- 1. 环境与持久化配置 (适配 Zeabur) ---
ENV = os.environ.get('ENV', 'local') 

# Zeabur 挂载卷配置：
# 如果设置了环境变量 DATA_DIR (在 Zeabur 设置里)，就用它；否则默认用本地 'data' 目录
DATA_DIR = os.environ.get('DATA_DIR', 'data') 
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)

# 数据库文件存放在挂载目录中，防止重启丢失
DB_PATH = os.path.join(DATA_DIR, 'xianbao.db')

MAX_RECORDS = 300 
PER_PAGE = 30     
REQUEST_TIMEOUT = 15 

# --- 2. 爬虫关键词配置 ---
TARGET_DOMAIN = "https://new.xianbao.fun"

BANK_KEYWORDS = {
    "农行": "农", "工行": "工", "建行": "建", "中行": "中"
}

# 关键字列表：已移除 "券"
KEYWORDS = ["hang", "行", "立减金", "ljj", "水", "红包"] + list(BANK_KEYWORDS.values())

# 排除列表
EXCLUSION_KEYWORDS = ["排行榜", "排 行 榜", "榜单", "置顶"] 

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"
}

# --- 3. 数据库初始化 ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # 核心表：增加了 original_time 用于存储显示的时间
    c.execute('''CREATE TABLE IF NOT EXISTS articles(
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        title TEXT NOT NULL, 
        url TEXT UNIQUE NOT NULL, 
        match_keyword TEXT, 
        original_time TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    # 内容缓存表
    c.execute("CREATE TABLE IF NOT EXISTS article_content(url TEXT PRIMARY KEY, content TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    conn.commit()
    conn.close()

# --- 4. 核心爬虫逻辑 ---
def scrape_list():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 开始执行抓取任务...")
    url = TARGET_DOMAIN + "/"
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    try:
        # 可选：清理旧缓存以保持数据最新（视需求而定，这里保留最新抓取逻辑）
        c.execute('DELETE FROM article_content')
        c.execute('DELETE FROM articles')
        conn.commit()
        
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT) 
        resp.encoding = 'utf-8'
        soup = BeautifulSoup(resp.text, "html.parser")
        
        # 兼容多种列表结构的行选择器
        rows = soup.select("tr, li")
        
        count = 0
        for row in rows:
            # 查找链接
            a_tag = row.select_one("a[href*='view']") or row.select_one("a[href*='thread']") or row.select_one("a")
            if not a_tag: continue
            
            title = a_tag.get_text(strip=True)
            href = a_tag.get("href")
            
            # 基础过滤
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
            
            # 补全链接
            if href.startswith("/"): href = TARGET_DOMAIN + href
            elif not href.startswith("http"): href = TARGET_DOMAIN + "/" + href

            # 提取时间 (从行文本中正则匹配 HH:MM 或 MM-DD)
            row_text = row.get_text(" ", strip=True) 
            text_without_title = row_text.replace(title, "")
            time_match = re.search(r'(\d{2}-\d{2}|\d{2}:\d{2}|\d{4}-\d{2}-\d{2})', text_without_title)
            
            original_time = time_match.group(1) if time_match else datetime.now().strftime("%H:%M")

            # 入库
            c.execute('''
                INSERT OR IGNORE INTO articles
                (title, url, match_keyword, original_time, updated_at) 
                VALUES(?,?,?,?,CURRENT_TIMESTAMP)
            ''', (title, href, match_kw.strip(), original_time))
            count += 1
        
        conn.commit()
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 抓取完成，更新了 {count} 条数据。")
        return True
    except Exception as e:
        sys.stderr.write(f"scrape_list failed: {e}\n")
        return False
    finally:
        conn.close()

# --- 5. 列表读取逻辑 ---
def get_list_data(page=1, per_page=PER_PAGE, tag_keyword=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    where_clause = ""
    params = []
    
    if tag_keyword:
        clean_tag = tag_keyword.strip() 
        if clean_tag in BANK_KEYWORDS.values():
            # 银行简称匹配，防止匹配到单字“行”
            where_clause = "WHERE match_keyword = ? AND match_keyword != ?"
            params.append(clean_tag)
            params.append('行') 
        else:
            where_clause = "WHERE match_keyword = ?"
            params.append(clean_tag)

    c.execute(f'SELECT COUNT(*) FROM articles {where_clause}', params)
    total_count = c.fetchone()[0]
    
    # 如果数据库空了，紧急触发一次抓取
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
        article_id, title, url, time_str = row
        data.append({
            'title': title,
            'view_link': f"/view?id={article_id}",
            'time': time_str
        })
    return data, total_count, total_pages

# --- 6. 详情页内容抓取与清洗 ---
def get_article_content(article_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT url, title FROM articles WHERE id = ?', (article_id,))
    article_info = c.fetchone()
    if not article_info:
        conn.close()
        return '<div class="alert alert-danger">文章不存在或已被清理</div>', "Error", None
    target_url, title = article_info
    c.execute('SELECT content FROM article_content WHERE url = ?', (target_url,))
    result = c.fetchone()
    conn.close()
    
    # 清洗闭包函数
    def clean_and_format_content(html_content):
        if not html_content: return ""
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 移除垃圾干扰元素
        garbage_classes = ['head-info', 'mochu_us_shoucang', 'mochu-us-coll', 'xg1', 'y', 'top_author_desc']
        for cls in garbage_classes:
            for tag in soup.find_all(class_=cls): tag.decompose()

        # 图片优化：移除写死的宽高，添加lazy load
        for img in soup.find_all('img'):
            img['loading'] = 'lazy'
            if 'width' in img.attrs: del img.attrs['width']
            if 'height' in img.attrs: del img.attrs['height']

        # 移除超链接标签，只保留文本
        for a_tag in soup.find_all('a'): a_tag.replace_with(a_tag.get_text())
        
        # 移除特定互动文本
        for tag in soup.find_all(string=re.compile(r"线报酷内部交流互动版块.*")):
            if tag.parent and tag.parent.name == 'p': tag.parent.decompose(); break
        for h in soup.find_all(['h1','h2','h3','h4','h5','h6']): h.decompose()
        
        # 正则深度清理
        text = str(soup)
        text = re.sub(r"微博线报.*?文章正文", "", text, flags=re.IGNORECASE)
        text = re.sub(r"首页赚客吧文章正文", "", text, flags=re.IGNORECASE)
        text = re.sub(r"[\u4e00-\u9fa5a-zA-Z0-9]{2,20}20\d{2}年\d{1,2}月\d{1,2}日.*?(举报)?", "", text)
        text = re.sub(r"欢迎您发表评论：\s*发布评论", "", text, flags=re.DOTALL) 
        text = re.sub(r"复制文案|点击复制|一键复制|复制链接", "", text, flags=re.IGNORECASE)
        
        # 修复链接：将纯文本链接转换为可点击链接，支持斜杠
        link_pattern = re.compile(r'(?<!["\'/=])(\bhttps?://[^\s<>"\'\u4e00-\u9fa5]+)')
        text = link_pattern.sub(lambda m: f'<a href="{m.group(1).rstrip(".,;:")}" target="_blank">{m.group(1).rstrip(".,;:")}</a>', text)
        
        # 去除多余空行
        return re.sub(r'(\s*\n\s*){2,}', '\n\n', text).strip()

    # 命中缓存
    if result and result[0]: return clean_and_format_content(result[0]), title, target_url
    
    # 未命中，实时抓取
    try:
        resp = requests.get(target_url, headers=HEADERS, timeout=REQUEST_TIMEOUT) 
        resp.encoding = 'utf-8'
        soup = BeautifulSoup(resp.text, 'html.parser')
        # 尝试定位正文区域
        content_node = soup.find('td', class_='t_f') or soup.find('div', class_='message') or soup.select_one('div[class*="content"]')
        if content_node:
            content_html = str(content_node)
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            try:
                c.execute('INSERT OR REPLACE INTO article_content (url, content, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)', (target_url, content_html))
                conn.commit()
            except: pass
            finally: conn.close()
            return clean_and_format_content(content_html), title, target_url
        else: return '<div class="alert alert-warning">无法自动提取正文，请点击右上角原网页查看。</div>', title, target_url
    except Exception as e: return f'内容获取错误: {e}', title, target_url

# --- 7. 定时任务配置 (APScheduler) ---
def start_scheduler():
    # 使用 BackgroundScheduler 在后台运行
    scheduler = BackgroundScheduler()
    # 每 10 分钟抓取一次
    scheduler.add_job(func=scrape_list, trigger="interval", minutes=10)
    scheduler.start()
    # 注册退出时的关闭事件
    atexit.register(lambda: scheduler.shutdown())

# --- 8. Web 路由 ---
@app.route('/')
def index():
    current_tag = request.args.get('tag')
    page = request.args.get('page', 1, type=int)
    articles, total_count, total_pages = get_list_data(page=page, per_page=PER_PAGE, tag_keyword=current_tag)
    
    bank_tag_list = [
        {"name": "全部", "tag": None},
        {"name": "农行", "tag": BANK_KEYWORDS["农行"]},
        {"name": "工行", "tag": BANK_KEYWORDS["工行"]},
        {"name": "建行", "tag": BANK_KEYWORDS["建行"]},
        {"name": "中行", "tag": BANK_KEYWORDS["中行"]},
        {"name": "立减金", "tag": "立减金"},
        {"name": "红包", "tag": "红包"}
    ]
    return render_template('index.html', articles=articles, current_page=page, total_pages=total_pages, current_tag=current_tag, bank_tag_list=bank_tag_list) 

@app.route('/view')
def view_article():
    content, title, _ = get_article_content(request.args.get('id', type=int))
    return render_template('detail.html', content=content, title=title) 

# --- 9. 程序入口 ---
if __name__ == '__main__':
    # 初始化数据库
    init_db()
    
    # 首次启动若无数据，执行抓取
    if not os.path.exists(DB_PATH) or os.path.getsize(DB_PATH) < 100:
        print("初始化：数据库为空，执行首次抓取...")
        scrape_list()

    # 启动定时任务 (仅在主进程中启动，防止 debug reload 时重复)
    # 环境变量 WERKZEUG_RUN_MAIN 是 Flask debug 模式的标记
    if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        start_scheduler()

    if ENV != 'local':
        # 生产环境 (Zeabur) 使用 waitress
        port = int(os.environ.get('PORT', 8080))
        print(f"Production Server starting on port {port}...")
        serve(app, host='0.0.0.0', port=port)
    else:
        # 本地开发环境
        app.run(host='0.0.0.0', port=5000, debug=True)