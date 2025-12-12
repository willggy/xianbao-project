# 引入定时任务库
from apscheduler.schedulers.background import BackgroundScheduler
import atexit

# ... (其他 import 保持不变)

app = Flask(__name__)

# --- 核心配置修改 ---
# Zeabur 会把代码放在 /app，我们把数据库存在 /app/data 这个挂载目录里
DATA_DIR = os.environ.get('DATA_DIR', 'data') # 默认为本地 data 目录
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, 'xianbao.db')

# ... (其他配置保持不变)

# --- 数据库初始化 (保持不变) ---
def init_db():
    # ... (代码不变)

# --- 定时任务函数 ---
def scheduled_task():
    print(f"[{datetime.now()}] 开始定时抓取任务...")
    scrape_list()
    print(f"[{datetime.now()}] 定时抓取完成。")

# --- 启动定时器 ---
# 只有在非调试模式下启动，防止 reload 时重复启动
if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
    scheduler = BackgroundScheduler()
    # 每隔 10 分钟抓取一次
    scheduler.add_job(func=scheduled_task, trigger="interval", minutes=10)
    scheduler.start()
    # 退出时关闭
    atexit.register(lambda: scheduler.shutdown())

# ... (scrape_list, get_list_data 等函数保持不变)
# ... (路由保持不变)

if __name__ == '__main__':
    # ... (初始化逻辑)
    init_db()
    
    # 首次启动如果没数据，立即抓一次
    if not os.path.exists(DB_PATH) or os.path.getsize(DB_PATH) < 100:
        print("初始化：数据库为空，执行首次抓取...")
        scrape_list()

    # Zeabur 生产环境启动方式
    if ENV != 'local':
        from waitress import serve
        # 监听所有 IP，端口由 Zeabur 环境变量 PORT 决定
        port = int(os.environ.get('PORT', 8080))
        print(f"Server starting on port {port}...")
        serve(app, host='0.0.0.0', port=port)
    else:
        app.run(host='0.0.0.0', port=5000, debug=True)