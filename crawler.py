# crawler.py - 适用于 Zeabur MySQL 直写
import requests
from bs4 import BeautifulSoup
import os
import mysql.connector
from datetime import datetime
import time

# --- 数据库配置区 ---
# 爬虫作为独立服务运行，直接使用 Zeabur 注入的 MySQL 环境变量
MYSQL_CONFIG = {
    'host': os.environ.get('DB_HOST', 'localhost'),
    'user': os.environ.get('DB_USER', 'root'),
    'password': os.environ.get('DB_PASSWORD', 'your_password'),
    'database': os.environ.get('DB_DATABASE', 'xianbao_db'),
    'port': os.environ.get('DB_PORT', 3306),
}

# 爬虫和清理配置
MAX_RECORDS = 200 # 最多保存200条数据

# --- 爬虫配置 ---
TARGET_DOMAIN = "https://new.xianbao.fun"
KEYWORDS = ["hang", "行", "立减金", "ljj", "水", "红包", "券"] 
EXCLUSION_KEYWORDS = ["排行榜", "排 行 榜", "榜单"] 
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': TARGET_DOMAIN
}

# --- 数据库操作函数 ---

def get_mysql_conn():
    """连接到 Zeabur 提供的 MySQL 数据库"""
    try:
        conn = mysql.connector.connect(**MYSQL_CONFIG)
        return conn
    except mysql.connector.Error as err:
        print(f"致命错误: MySQL连接失败: {err}")
        raise

def init_db(conn):
    """确保表结构存在 (与 app.py 中保持一致)"""
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id INT AUTO_INCREMENT PRIMARY KEY,
            title VARCHAR(512) NOT NULL,
            url VARCHAR(2048) UNIQUE NOT NULL,
            match_keyword VARCHAR(100),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS article_content (
            id INT AUTO_INCREMENT PRIMARY KEY,
            url VARCHAR(2048) UNIQUE NOT NULL,
            content MEDIUMTEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            FOREIGN KEY (url) REFERENCES articles(url)
        )
    """)
    conn.commit()
    c.close()

def save_article(conn, title, url, match_kw):
    """保存或更新文章数据 (使用 ON DUPLICATE KEY UPDATE)"""
    c = conn.cursor()
    # MySQL 语法：INSERT ... ON DUPLICATE KEY UPDATE
    try:
        sql = '''
            INSERT INTO articles (title, url, match_keyword)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE 
                title = VALUES(title), 
                match_keyword = VALUES(match_keyword),
                updated_at = CURRENT_TIMESTAMP()
        '''
        c.execute(sql, (title, url, match_kw))
        conn.commit()
        return c.rowcount > 0 # 返回是否成功插入或更新
    except Exception as e:
        print(f"数据库写入失败: {e}")
        return False
    finally:
        c.close()

def cleanup_old_records(conn):
    """清理旧记录，保持最多 MAX_RECORDS 条"""
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM articles')
    count = c.fetchone()[0]
    
    if count > MAX_RECORDS:
        delete_count = count - MAX_RECORDS
        # MySQL 逻辑：先找出最早的记录 ID，然后删除
        c.execute(f'''
            DELETE FROM articles 
            ORDER BY created_at ASC 
            LIMIT {delete_count}
        ''')
        
        # 简单清理 content 表中没有对应 articles 的记录 (需要外键支持)
        # 也可以手动清理，此处为了简化，仅清理 articles
        
        conn.commit()
        print(f"MySQL已清理 {delete_count} 条旧记录")
    
    c.close()

# --- 爬虫核心逻辑 ---

def run_crawler():
    """运行爬虫主函数"""
    print(f"[{datetime.now()}] 🚀 爬虫启动，目标: {TARGET_DOMAIN}")
    
    conn = None
    try:
        conn = get_mysql_conn()
        init_db(conn) # 确保表存在
        
        resp = requests.get(TARGET_DOMAIN + "/", headers=HEADERS, timeout=30)
        resp.encoding = 'utf-8'
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        rows = soup.find_all('tr') 
        if not rows: 
            rows = soup.find_all('li')
        
        total_saved = 0
        
        for row in rows:
            link = row.find('a')
            if not link: continue
            
            title = link.get_text().strip()
            title_lower = title.lower()
            
            # 排除关键词过滤
            if any(ex_kw.lower() in title_lower for ex_kw in EXCLUSION_KEYWORDS):
                continue
            
            href = link.get('href')
            if href and not href.startswith('http'):
                href = TARGET_DOMAIN + (href if href.startswith('/') else '/' + href)
            
            # 关键词匹配和保存
            for kw in KEYWORDS:
                if kw.lower() in title_lower:
                    if save_article(conn, title, href, kw):
                        total_saved += 1
                    break
        
        print(f"[{datetime.now()}] ✅ 爬虫完成。总共处理了 {len(rows)} 条数据，保存/更新了 {total_saved} 条记录。")
        cleanup_old_records(conn) # 清理旧记录
        
    except requests.exceptions.RequestException as e:
        print(f"网络请求失败: {e}")
    except mysql.connector.Error as e:
        print(f"数据库操作失败: {e}")
    except Exception as e:
        print(f"发生未知错误: {e}")
    finally:
        if conn:
            conn.close()

if __name__ == '__main__':
    run_crawler()