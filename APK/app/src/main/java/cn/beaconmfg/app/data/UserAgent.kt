package cn.beaconmfg.app.data

import okhttp3.Interceptor
import okhttp3.Response

/**
 * 所有出站 HTTP 请求都要带的身份头。
 *
 * **为什么必须有**：
 * OkHttp **默认不发送 `User-Agent` 头**（这点跟 HttpURLConnection 不一样，后者会带 `Dalvik/...`）。
 * 而 Cloudflare 的浏览器签名校验（Bot Fight Mode / Browser Integrity Check）会把「无 UA」的
 * 请求直接拒掉，返回 403 + `error code: 1010`
 * （原文：The owner of this website has banned your access based on your browser's signature）。
 *
 * 2026-09-15 实测（beacon-mfg.pages.dev，各 3 次取一致结果）：
 *   - 不带 UA             → 403 `error code: 1010`（恒现）
 *   - 带任意 UA（含 Dalvik / BeaconMFG/1.0 / Mozilla）→ 200
 *
 * **这个坑为什么难查**：现象是「主源不通」，但浏览器访问一切正常，
 * `requests`/curl 也正常（它们都自带 UA）——只有 OkHttp 被拦，
 * 所以极易被误判成 CDN 抽风、地域屏蔽或数据本身有问题。
 *
 * 这里刻意**不伪装**成 Mozilla/Chrome：用一个可识别的应用 UA，
 * 既不触发 WAF 的「伪装浏览器」策略，也方便在 Cloudflare 侧做白名单/限流。
 */
const val BEACON_USER_AGENT = "BeaconMFG-Android/1.0 (+https://beacon-mfg.pages.dev/)"

/** 给客户端挂上统一身份头。所有访问 Cloudflare 前置资源的 OkHttpClient 都应该加它。 */
fun beaconIdentityInterceptor(): Interceptor = Interceptor { chain ->
    val req = chain.request().newBuilder()
        .header("User-Agent", BEACON_USER_AGENT)
        // 部分 WAF 也会看 Accept，补上更像正常客户端，且不影响响应解析
        .header("Accept", "*/*")
        .build()
    chain.proceed(req)
}

/** 诊断用：确认某个响应是不是被 Cloudflare 以「浏览器签名」为由拒掉的。 */
fun Response.isCfBrowserSignatureBlocked(): Boolean =
    code == 403 && runCatching {
        peekBody(64).string().contains("1010")
    }.getOrDefault(false)
