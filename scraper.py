# scraper.py
import traceback
from app import scrape_all_sites, get_db_connection

if __name__ == "__main__":
    print("Scraper started...")
    try:
        # 初始化数据库
        get_db_connection().close()
        
        # 强制抓取（忽略无人访问休眠）
        scrape_all_sites()
        print("Scraper finished successfully!")
    except Exception as e:
        print("Scraper error:")
        traceback.print_exc()
