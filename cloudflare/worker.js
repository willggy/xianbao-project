export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/api/save_list" && request.method === "POST") {
      const body = await request.json();
      await env.DB.prepare(
        `INSERT OR REPLACE INTO articles
         (title, url, match_keyword, updated_at)
         VALUES (?, ?, ?, CURRENT_TIMESTAMP)`
      ).bind(body.title, body.url, body.match).run();
      return Response.json({ ok: true });
    }

    if (url.pathname === "/api/save_content" && request.method === "POST") {
      const body = await request.json();
      await env.DB.prepare(
        `INSERT OR REPLACE INTO article_content
         (url, content, updated_at)
         VALUES (?, ?, CURRENT_TIMESTAMP)`
      ).bind(body.url, body.content).run();
      return Response.json({ ok: true });
    }

    if (url.pathname === "/api/list") {
      const page = Number(url.searchParams.get("page") || 1);
      const per = 20;
      const offset = (page - 1) * per;

      const data = await env.DB.prepare(
        `SELECT title, url, match_keyword
         FROM articles
         ORDER BY updated_at DESC
         LIMIT ? OFFSET ?`
      ).bind(per, offset).all();

      return Response.json(data.results);
    }

    if (url.pathname === "/api/content") {
      const target = url.searchParams.get("url");
      const row = await env.DB.prepare(
        "SELECT content FROM article_content WHERE url = ?"
      ).bind(target).first();

      return Response.json(row || {});
    }

    return new Response("Not Found", { status: 404 });
  }
}
