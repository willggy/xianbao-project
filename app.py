import requests
import urllib.parse
from flask import Flask, render_template, request

API = "https://xianbao-api-worker.gaoguanyu777.workers.dev/api"
app = Flask(__name__)
PER_PAGE = 20

@app.route("/")
def index():
    page = int(request.args.get("page", 1))
    res = requests.get(f"{API}/list?page={page}")
    data = res.json()

    articles = []
    for row in data:
        encoded_url = urllib.parse.quote(row["url"])
        articles.append({
            "title": row["title"],
            "view_link": f"/view?url={encoded_url}",
            "match": row.get("match_keyword", "")
        })

    return render_template("index.html",
                           articles=articles,
                           current_page=page,
                           total_pages=999,
                           total_count=999,
                           per_page=PER_PAGE)

@app.route("/view")
def view_article():
    url = request.args.get("url")
    res = requests.get(f"{API}/content?url={url}")
    data = res.json()
    content = data.get("content", "")
    return render_template("detail.html", content=content)

if __name__ == "__main__":
    app.run(debug=True)
