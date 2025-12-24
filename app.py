<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>系统控制台</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { background: #1e1e1e; color: #d4d4d4; font-family: 'Consolas', monospace; padding: 20px; }
        .console-card { background: #252526; border-radius: 8px; border: 1px solid #333; padding: 15px; margin-bottom: 20px; }
        .log-line { border-bottom: 1px solid #2d2d2d; padding: 4px 0; font-size: 13px; }
        .log-time { color: #569cd6; margin-right: 10px; }
        .visitor-table { color: #ccc; font-size: 12px; }
        h6 { color: #ce9178; border-left: 4px solid #ce9178; padding-left: 10px; margin-bottom: 15px; }
        .btn-back { color: #9cdcfe; text-decoration: none; font-size: 14px; }
    </style>
</head>
<body>
    <div class="container-fluid">
        <a href="/" class="btn-back"> < 返回首页</a>
        <h4 class="my-4">System Console</h4>

        <div class="console-card">
            <h6>抓取任务流水 (最近50条)</h6>
            <div id="log-container">
                {% for log in logs %}
                <div class="log-line">
                    <span class="text-success">></span> {{ log.last_scrape }}
                </div>
                {% endfor %}
            </div>
        </div>

        <div class="console-card">
            <h6>访客统计</h6>
            <table class="table table-dark table-hover visitor-table">
                <thead>
                    <tr><th>IP</th><th>次数</th><th>最后活动</th></tr>
                </thead>
                <tbody>
                    {% for v in visitors %}
                    <tr>
                        <td>{{ v.ip }}</td>
                        <td>{{ v.visit_count }}</td>
                        <td>{{ v.last_visit }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
</body>
</html>
