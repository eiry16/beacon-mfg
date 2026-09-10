package cn.beaconmfg.app.ui

import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.platform.UriHandler
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import cn.beaconmfg.app.MainViewModel
import cn.beaconmfg.app.data.CapabilityCard
import cn.beaconmfg.app.data.CertTier
import cn.beaconmfg.app.data.Evidence
import cn.beaconmfg.app.data.Hit
import cn.beaconmfg.app.data.SupplierDetail
import cn.beaconmfg.app.i18n.Lang
import cn.beaconmfg.app.i18n.Strings

@Composable
fun ChatScreen(vm: MainViewModel, modifier: Modifier = Modifier) {
    val messages by vm.messages.collectAsState()
    val busy by vm.busy.collectAsState()
    val status by vm.status.collectAsState()
    val settings by vm.settings.collectAsState()
    val s = remember(settings.lang) { Strings(Lang.of(settings.lang)) }
    var input by remember { mutableStateOf("") }

    // 能力卡展开状态按供应商 ID 存，不放在卡片内部——
    // 卡片滑出列表再滑回来时 remember 会重置，用户刚点开的又缩回去了。
    val capOpen = remember { mutableStateMapOf<String, Boolean>() }

    val samples = s.samples()

    // imePadding：edge-to-edge 下系统不再替我们把内容顶上去，
    // 软键盘弹出时必须自己吃掉 IME 高度，否则输入框整条被键盘盖住。
    Column(
        modifier
            .fillMaxSize()
            .padding(horizontal = 12.dp)
            .imePadding()
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(vertical = 6.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            if (busy) CircularProgressIndicator(modifier = Modifier.padding(end = 8.dp))
            Text(
                status,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 2,
            )
        }
        if (busy) LinearProgressIndicator(modifier = Modifier.fillMaxWidth())

        LazyColumn(
            modifier = Modifier
                .weight(1f)
                .fillMaxWidth(),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            items(messages, key = { it.id }) { m ->
                MessageRow(
                    m = m,
                    s = s,
                    skillBase = settings.capabilityBase,
                    isOpen = { id -> capOpen[id] ?: m.autoOpenCap },
                    onToggle = { id -> capOpen[id] = !(capOpen[id] ?: m.autoOpenCap) },
                )
            }
        }

        // 快捷问题：手机上打字成本高，给几个真实场景的起手式。
        // 只在首页且未输入时显示；一旦开始搜索或已有消息流，立即隐藏，
        // 避免盖住搜索结果卡片的工艺位/ID。
        if (messages.isEmpty() && input.isBlank()) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                samples.take(2).forEach { q ->
                    Button(
                        onClick = { input = ""; vm.send(q) },
                        modifier = Modifier.weight(1f),
                    ) { Text(q, maxLines = 1, fontSize = 11.sp) }
                }
            }
        }

        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            OutlinedTextField(
                value = input,
                onValueChange = { input = it },
                modifier = Modifier.weight(1f),
                placeholder = { Text(s.inputHint) },
                maxLines = 3,
            )
            Button(
                onClick = { vm.send(input); input = "" },
                modifier = Modifier.padding(start = 8.dp),
                enabled = !busy,
            ) { Text(s.send) }
        }
    }
}

@Composable
private fun MessageRow(
    m: MainViewModel.UiMessage,
    s: Strings,
    skillBase: String,
    isOpen: (String) -> Boolean,
    onToggle: (String) -> Unit,
) {
    val isUser = m.role == MainViewModel.Role.USER
    Box(
        modifier = Modifier.fillMaxWidth(),
        contentAlignment = if (isUser) Alignment.CenterEnd else Alignment.CenterStart,
    ) {
        when {
            // 工具回显：**只显示一行状态**。
            // 卡片曾经在这里和最终回答各渲染一遍 → 同一次搜索出现两轮一模一样的灯牌。
            m.isTool -> Surface(
                color = MaterialTheme.colorScheme.surfaceVariant,
                shape = RoundedCornerShape(8.dp),
                modifier = Modifier.fillMaxWidth(0.95f),
            ) {
                Text(m.text, style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(8.dp))
            }

            else -> Surface(
                color = when (m.role) {
                    MainViewModel.Role.USER -> MaterialTheme.colorScheme.primaryContainer
                    MainViewModel.Role.SYSTEM -> MaterialTheme.colorScheme.surfaceVariant
                    else -> MaterialTheme.colorScheme.secondaryContainer
                },
                shape = RoundedCornerShape(12.dp),
                modifier = Modifier.widthIn(max = 340.dp),
            ) {
                Column(Modifier.padding(10.dp)) {
                    Text(m.text, style = MaterialTheme.typography.bodyMedium)
                    if (m.streaming) {
                        Text(
                            "▍",
                            color = MaterialTheme.colorScheme.primary,
                            style = MaterialTheme.typography.bodyMedium,
                        )
                    }
                    if (m.hits.isNotEmpty()) Column(Modifier.padding(top = 6.dp)) {
                        if (m.fallback) {
                            Text(
                                s.localFallback,
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                                modifier = Modifier.padding(bottom = 2.dp),
                            )
                        }
                        m.hits.forEach { h ->
                            SupplierCard(
                                h = h,
                                cap = m.caps[h.fp.id],
                                s = s,
                                skillBase = skillBase,
                                open = isOpen(h.fp.id),
                                onToggle = { onToggle(h.fp.id) },
                            )
                        }
                    }
                    m.detail?.let { d ->
                        DetailCard(d, s, skillBase, m.caps[d.id] ?: d.cap)
                    }
                }
            }
        }
    }
}

/**
 * 拨号意图。**用 ACTION_DIAL 不是 ACTION_CALL**——
 * 只把号码预填进拨号盘，由用户自己按拨出键。
 * 直接呼出既不合规（会静默产生通话），也需要 CALL_PHONE 权限；DIAL 两者都不需要。
 */
private fun dialIntent(raw: String): Intent? {
    // 源数据里一条记录可能带多个号（; ， 、 / 分隔），取第一个可用的
    val first = raw.split(';', '；', '，', ',', '、', '/', ' ')
        .firstOrNull { it.isNotBlank() }
        ?.trim()
        .orEmpty()
    val digits = first.filter { it.isDigit() || it == '+' }
    // 少于 7 位不可能是有效电话——不跳拨号盘，免得点一下什么也没发生
    if (digits.length < 7) return null
    return Intent(Intent.ACTION_DIAL, Uri.parse("tel:$digits"))
        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
}

/** 可点击的电话行：点一下带着号码进拨号盘。号码缺失/待核实时不可点，也不假装有号。 */
@Composable
private fun PhoneLine(text: String, rawNumber: String, s: Strings) {
    val ctx = LocalContext.current
    Text(
        buildAnnotatedString {
            withStyle(
                SpanStyle(
                    color = MaterialTheme.colorScheme.primary,
                    fontWeight = FontWeight.SemiBold,
                    textDecoration = TextDecoration.Underline,
                )
            ) { append(text) }
        },
        style = MaterialTheme.typography.bodySmall,
        modifier = Modifier
            .fillMaxWidth()
            .clickable {
                dialIntent(rawNumber)?.let { runCatching { ctx.startActivity(it) } }
            }
            .padding(vertical = 2.dp),
    )
}

@Composable
fun SupplierCard(
    h: Hit,
    cap: CapabilityCard?,
    s: Strings,
    skillBase: String,
    open: Boolean,
    onToggle: () -> Unit,
) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 3.dp),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surface
        ),
    ) {
        Column(Modifier.padding(10.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    h.fp.name,
                    fontWeight = FontWeight.SemiBold,
                    style = MaterialTheme.typography.bodyMedium,
                    modifier = Modifier.weight(1f),
                )
                // 认证徽章放在公司名后面：采购扫一眼就知道这份信息核验到什么程度。
                // 等级来自数据；缺失或不在 L0–L3 里的值一律按「未核验」显示。
                CertBadge(h.fp.cl, s)
                EvidenceTag(h.evidence, s)
            }
            Text(
                listOf(
                    h.fp.city.ifEmpty { s.cityUnknown },
                    if (h.fp.gb.isNotEmpty()) "${h.fp.gb} ${h.fp.gbName}" else "",
                ).filter { it.isNotEmpty() }.joinToString(s.dotSep),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            // 电话单独一行：有号码就直给（采购最关心的就是这个），
            // 取不到就如实说「待核实」，绝不拿占位值或猜的号充数。
            // 有号就整行可点：点进去是预填好的拨号盘，**不自动呼出**。
            if (h.fp.phone.isNotEmpty()) {
                PhoneLine(s.phone(h.fp.phone), h.fp.phone, s)
            } else {
                Text(
                    if (h.fp.tel) s.phonePending else s.phoneNone,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            if (h.fp.cert.isNotEmpty()) {
                Text(
                    s.certLabel + h.fp.cert.joinToString(s.sep),
                    style = MaterialTheme.typography.bodySmall,
                )
            }
            // 折叠态给一行工艺摘要，采购扫一眼就知道这家能做什么，
            // 不用等模型再调一次 get_supplier_detail——「减少使用负荷」的硬要求。
            // 展开后由 CapabilityBlock 给完整 chips，这里就不再重复。
            if (cap != null && !open && cap.processes.isNotEmpty()) {
                val names = cap.processes
                    .sortedBy { if (it.level == "primary") 0 else 1 }
                    .map { it.name }
                Text(
                    s.procLabel + names.take(4).joinToString(s.sep) +
                        (if (names.size > 4) s.procMore(names.size) else ""),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }

            // 能力卡入口：以前只有模型主动调 get_supplier_detail 才看得到，
            // 触发条件太苛刻。这里每家都给「打开」按钮，一点即展。
            if (cap != null) {
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    modifier = Modifier.padding(top = 2.dp),
                ) {
                    Text(
                        if (cap.processes.isEmpty()) s.capEmpty else s.capCount(cap.processes.size),
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.weight(1f),
                    )
                    TextButton(onClick = onToggle) {
                        Text(if (open) s.capHide else s.capOpen)
                    }
                }
                if (open) {
                    val uriHandler = LocalUriHandler.current
                    CapabilityBlock(cap, s, skillBase, uriHandler)
                }
            } else {
                Text(
                    s.capNoCard,
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(top = 2.dp),
                )
            }

            Text(
                h.fp.id,
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

@Composable
private fun EvidenceTag(e: Evidence, s: Strings) {
    val (bg, fg) = when (e) {
        Evidence.LITERAL -> Color(0xFF1B5E20) to Color.White
        Evidence.ALIAS_PRIMARY -> Color(0xFF0D47A1) to Color.White
        Evidence.ALIAS_SECONDARY -> Color(0xFF4E342E) to Color(0xFFFFCC80)
    }
    Surface(color = bg, shape = RoundedCornerShape(6.dp)) {
        Text(
            e.label(s),
            color = fg,
            fontSize = 10.sp,
            modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp),
        )
    }
}

/**
 * 认证徽章（灯牌）。
 *
 * 配色沿用 docs/CERTIFICATION_V1.md §1：未核验灰 / 已认领蓝 / 已认证金 / 已验厂深金。
 * 这里**不做任何升级判断**：传进来什么等级就画什么等级，
 * 绝大多数企业是 L0（自动收录、未核验），如实画灰色即可 —— 藏起来才是对采购的误导。
 */
@Composable
private fun CertBadge(cl: String, s: Strings) {
    val tier = CertTier.of(cl)
    val (bg, fg) = when (tier) {
        CertTier.L1 -> Color(0xFF1565C0) to Color.White
        CertTier.L2 -> Color(0xFFF57F17) to Color.White
        CertTier.L3 -> Color(0xFF7A4F01) to Color.White
        CertTier.L0 -> Color(0xFF3A3A3A) to Color(0xFFBDBDBD)
    }
    Surface(
        color = bg,
        shape = RoundedCornerShape(6.dp),
        modifier = Modifier.padding(end = 4.dp),
    ) {
        Text(
            "${tier.code} ${tier.label(s)}",
            color = fg,
            fontSize = 10.sp,
            modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp),
        )
    }
}

@Composable
private fun DetailCard(
    d: SupplierDetail,
    s: Strings,
    skillBase: String,
    cap: CapabilityCard?,
) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 3.dp),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.tertiaryContainer
        ),
    ) {
    val todayIso = remember {
        java.text.SimpleDateFormat("yyyy-MM-dd", java.util.Locale.CHINA).format(java.util.Date())
    }
    Column(Modifier.padding(10.dp)) {
        Text(d.company, fontWeight = FontWeight.Bold)
        // 认证：等级 + 含义 + 存证日期。能不能接自动询价也是从这里判的（L2 起）。
        Row(verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier.padding(top = 2.dp)) {
            Text(s.detailBeacon, style = MaterialTheme.typography.bodySmall)
            CertBadge(d.beacon, s)
        }
        Text(CertTier.of(d.beacon).hint(s),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        d.certification?.let { cert ->
            val bits = ArrayList<String>()
            if (cert.issuedAt.isNotBlank()) bits.add(s.beaconIssued(cert.issuedAt))
            if (cert.expiresAt.isNotBlank()) bits.add(s.beaconValidUntil(cert.expiresAt))
            cert.completeness?.let { bits.add(s.beaconCompleteness("%.1f".format(it))) }
            if (bits.isNotEmpty()) {
                Text(bits.joinToString(s.dotSep), style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            if (cert.expired(todayIso) || d.certExpired) {
                Text(s.beaconExpired, style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error)
            }
        }
        if (CertTier.of(d.beacon).acceptsAutoRfq) {
            Text(s.beaconAutoRfq, style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.primary)
        }
        // 灯牌不是评级，这句限定必须在详情页出现一次，
        // 否则采购看到金色徽章会理解成「这家厂质量好」。
        Text(s.beaconNote, style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Text(
                locationLine(d.province, d.city, d.address, s),
                style = MaterialTheme.typography.bodySmall,
            )
            if (d.phone.isNotEmpty()) {
                PhoneLine(s.detailPhone + d.phone, d.phone, s)
            }
            if (d.website.isNotEmpty()) {
                Text(s.detailSite + d.website, style = MaterialTheme.typography.bodySmall)
            }
            if (d.certs.isNotEmpty()) {
                Text(s.detailCerts + d.certs.joinToString(s.sep), style = MaterialTheme.typography.bodySmall)
            }
            if (cap != null) {
                CapabilityBlock(cap, s, skillBase, LocalUriHandler.current)
            } else {
                Text(
                    s.capNoneInDetail,
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

/**
 * 详情页那行「省 · 市  地址」。
 *
 * 两处去重，都是为了不把同一件事说三遍（真机实测：上海的企业会显示成
 * 「上海 · 上海  上海市奉贤区宏杨路158号」）：
 *  1. 直辖市 `province == city`（上海/北京/天津/重庆），只留一个；
 *  2. 地址常常已经以城市名开头（「上海市奉贤区…」），此时前缀整个省掉。
 * 拿不准就只显示地址——少说一句，也比重复强。
 */
private fun locationLine(province: String, city: String, address: String, s: Strings): String {
    val parts = listOf(province, city).filter { it.isNotBlank() }.distinct()
    val prefix = parts.joinToString(s.dotSep)
    if (prefix.isBlank()) return address
    if (address.isBlank()) return prefix
    if (parts.any { address.startsWith(it) }) return address
    return "$prefix  $address"
}

/**
 * 工艺位（L1 能力卡）。
 *
 * 两条红线在这里体现：
 *  1. **硬指标没填就写「未填报」**，不显示 0。4136 张卡里只有 6 张有实质硬指标，
 *     显示 0 会让客户以为这家厂公差能做到 0。
 *  2. **自动整理的卡必须标注来源**。工艺是平台从企业名称推断的，企业没确认过，
 *     不标注等于把推断当承诺。
 */
@Composable
private fun CapabilityBlock(
    cap: CapabilityCard,
    s: Strings,
    skillBase: String,
    uriHandler: UriHandler,
) {
    Column(Modifier.padding(top = 6.dp)) {
        Text(
            if (cap.isSelfReported) s.capSelf else s.capAuto,
            style = MaterialTheme.typography.labelSmall,
            color = if (cap.isSelfReported) MaterialTheme.colorScheme.primary
            else MaterialTheme.colorScheme.onSurfaceVariant,
        )

        if (cap.processes.isNotEmpty()) {
            FlowRow(
                horizontalArrangement = Arrangement.spacedBy(4.dp),
                modifier = Modifier.padding(top = 4.dp),
            ) {
                cap.processes.forEach { ProcChip(it, s) }
            }
        }

        if (cap.materials.isNotEmpty()) {
            Text(
                s.matLabel + cap.materials.joinToString(s.sep),
                style = MaterialTheme.typography.bodySmall,
                modifier = Modifier.padding(top = 2.dp),
            )
        }

        val lims = cap.limitLines(s)
        if (lims.isNotEmpty()) {
            Text(
                s.limLabel + lims.joinToString(s.dotSep),
                style = MaterialTheme.typography.bodySmall,
                modifier = Modifier.padding(top = 2.dp),
            )
        } else {
            Text(
                s.limLabel + s.limEmpty,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 2.dp),
            )
        }

        // 厂商 skill（L2 自述层）。CDN 部署前这个 URL 不可达（L2 不进 Git），
        // 按钮仍然给出路径——采购可以照着路径去仓库找，比藏起来强。
        if (cap.skillPath.isNotEmpty()) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier.padding(top = 4.dp),
            ) {
                Text(
                    s.skillLabel + cap.skillPath.substringAfterLast('/').removeSuffix(".md") +
                        s.dotSep + (if (cap.skillVerified) s.verified else s.unverified),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.weight(1f),
                )
                if (skillBase.isNotBlank()) {
                    Button(
                        onClick = {
                            uriHandler.openUri(skillBase.trimEnd('/') + "/" + cap.skillPath)
                        }
                    ) { Text(s.openSkill) }
                }
            }
        }
    }
}

@Composable
private fun ProcChip(p: cn.beaconmfg.app.data.ProcItem, s: Strings) {
    val primary = p.level == "primary"
    val label = p.levelLabel(s)
    Surface(
        color = if (primary) MaterialTheme.colorScheme.primaryContainer
        else MaterialTheme.colorScheme.surfaceVariant,
        shape = RoundedCornerShape(6.dp),
    ) {
        Text(
            p.name.ifEmpty { p.code } + (label.takeIf { it.isNotEmpty() }?.let { s.dotSep + it } ?: ""),
            fontSize = 11.sp,
            fontWeight = if (primary) FontWeight.SemiBold else FontWeight.Normal,
            modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp),
        )
    }
}

@Composable
fun StatusText(text: String) {
    Text(
        text,
        style = MaterialTheme.typography.bodySmall,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier
            .padding(8.dp)
            .background(MaterialTheme.colorScheme.surfaceVariant, RoundedCornerShape(6.dp))
            .padding(8.dp),
    )
}

@Composable
fun Spacer4() = Spacer(Modifier.padding(2.dp))
