/**
 * Pages Functions：把 /skills/** 直接转 R2，其余走静态资源。
 *
 * 为什么要有这个（P0 第 4 步）
 * --------------------------
 * 静态部署 = 每次发布要把 4200 个文件重新传一遍（约 2.5 分钟），
 * 而且新供应商建档后要等下一次部署才看得见（2026-09-10 耐特斯 404 的真因）。
 * 接上 R2 之后：**R2 是真源**，写进去立刻可读，Pages 只负责路由。
 *
 * 路径分工
 * --------
 *   /skills/**  → R2 bucket（binding 名 CAPS）
 *   其他         → Pages 静态资源（env.ASSETS），也就是 index.html / 404.html
 *
 * ⚠ 两个必须知道的事
 * ------------------
 * 1. 有 _worker.js 之后它就接管**全部**请求，静态文件必须显式走 env.ASSETS.fetch()，
 *    否则首页也会 404。
 * 2. R2 binding **可能没绑上**。这时 env.CAPS 是 undefined，直接 .get() 会抛
 *    TypeError → 平台层 500（空 body，最难查）。所以下面显式判空并返回 503
 *    + 说明，让"没配 binding"和"文件不存在"这两种情况能被区分开。
 */

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname.startsWith("/skills/")) {
      return serveFromR2(request, env, url);
    }

    // 静态资源（index.html / 404.html / manifest.json / full/ slim/ 等）
    if (env.ASSETS) {
      return env.ASSETS.fetch(request);
    }
    return new Response("ASSETS binding 缺失", { status: 500 });
  },
};

async function serveFromR2(request, env, url) {
  if (!env.CAPS) {
    return new Response(
      "R2 binding 未配置（Pages 项目需要把 bucket 绑成 CAPS）",
      { status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" } }
    );
  }

  // R2 里 key 不带前导斜杠（上传时就是 skills/vendors/xxx/SKILL.md）
  const key = decodeURIComponent(url.pathname.slice(1));

  const obj = await env.CAPS.get(key);
  if (obj === null) {
    // 关键：不存在必须返回真 404。App 的 HEAD 探活靠它区分
    // 「已发布」和「还没进快照」—— 返回 200 会让探活永远成功。
    return new Response("Not found", {
      status: 404,
      headers: { "Content-Type": "text/plain; charset=utf-8" },
    });
  }

  const headers = new Headers();
  obj.writeHttpMetadata(headers);
  headers.set("etag", obj.httpEtag);
  // 扩展名兜底：R2 存的 contentType 有时是 application/octet-stream
  if (key.endsWith(".md")) {
    headers.set("Content-Type", "text/markdown; charset=utf-8");
  } else if (key.endsWith(".json")) {
    headers.set("Content-Type", "application/json; charset=utf-8");
  }
  headers.set("Access-Control-Allow-Origin", "*");
  // 能力卡会更新，但不用每次回源：5 分钟边缘缓存
  headers.set("Cache-Control", "public, max-age=300");

  // HEAD 请求（App 探活）不能带 body，否则部分客户端会 hang
  if (request.method === "HEAD") {
    return new Response(null, { status: 200, headers });
  }
  return new Response(obj.body, { status: 200, headers });
}
