# D:\py_file\Cloudflare\functions\[[path]].py

# 从 app.py 中导入您的 Flask 实例
from app import app as flask_app 

# Cloudflare Pages Functions 的标准入口
async def onRequest(context):
    """
    处理传入的请求，并将其传递给 Flask 应用。
    Pages 运行时环境会自动处理 Request/Response 的兼容性。
    """

    # 核心逻辑：直接返回 Flask 应用的响应
    # context.request 是 Pages 提供的标准 Request 对象
    # 运行时环境会自动将 Pages Request 适配给 Flask 
    return flask_app(context)