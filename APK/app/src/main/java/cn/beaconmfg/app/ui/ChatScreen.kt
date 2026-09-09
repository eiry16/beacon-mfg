package cn.beaconmfg.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
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
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import cn.beaconmfg.app.MainViewModel
import cn.beaconmfg.app.data.Evidence
import cn.beaconmfg.app.data.Hit
import cn.beaconmfg.app.data.SupplierDetail

@Composable
fun ChatScreen(vm: MainViewModel, modifier: Modifier = Modifier) {
    val messages by vm.messages.collectAsState()
    val busy by vm.busy.collectAsState()
    val status by vm.status.collectAsState()
    var input by remember { mutableStateOf("") }

    val samples = listOf(
        "上海有没有做输送线的厂",
        "东莞做齿轮的小厂，要有电话",
        "找宁波的压铸厂",
        "304 不锈钢钣金加工，深圳",
    )

    Column(modifier.fillMaxSize().padding(horizontal = 12.dp)) {
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
            items(messages, key = { it.id }) { m -> MessageRow(m) }
        }

        // 快捷问题：手机上打字成本高，给几个真实场景的起手式
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            samples.take(2).forEach { q ->
                Button(
                    onClick = { input = q; vm.send(q) },
                    modifier = Modifier.weight(1f),
                ) { Text(q, maxLines = 1, fontSize = 11.sp) }
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
                placeholder = { Text("想找什么供应商？") },
                maxLines = 3,
            )
            Button(
                onClick = { vm.send(input); input = "" },
                modifier = Modifier.padding(start = 8.dp),
                enabled = !busy,
            ) { Text("发送") }
        }
    }
}

@Composable
private fun MessageRow(m: MainViewModel.UiMessage) {
    val isUser = m.role == MainViewModel.Role.USER
    Box(
        modifier = Modifier.fillMaxWidth(),
        contentAlignment = if (isUser) Alignment.CenterEnd else Alignment.CenterStart,
    ) {
        when {
            m.role == MainViewModel.Role.SYSTEM && m.text.startsWith("调用 ") -> {
                // 工具调用回显：折叠成一行，点开看完整返回没必要，UI 只给结论
                Surface(
                    color = MaterialTheme.colorScheme.surfaceVariant,
                    shape = RoundedCornerShape(8.dp),
                    modifier = Modifier.fillMaxWidth(0.95f),
                ) {
                    Column(Modifier.padding(8.dp)) {
                        Text(m.text, style = MaterialTheme.typography.bodySmall)
                        if (m.hits.isNotEmpty()) Column {
                            m.hits.forEach { SupplierCard(it) }
                        }
                        m.detail?.let { DetailCard(it) }
                    }
                }
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
                        m.hits.forEach { SupplierCard(it) }
                    }
                    m.detail?.let { DetailCard(it) }
                }
            }
        }
    }
}

@Composable
fun SupplierCard(h: Hit) {
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
                EvidenceTag(h.evidence)
            }
            Text(
                listOf(
                    h.fp.city.ifEmpty { "城市未知" },
                    if (h.fp.gb.isNotEmpty()) "${h.fp.gb} ${h.fp.gbName}" else "",
                    if (h.fp.tel) "有电话" else "无电话",
                    "灯牌 ${h.fp.cl}",
                ).filter { it.isNotEmpty() }.joinToString(" · "),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            if (h.fp.cert.isNotEmpty()) {
                Text(
                    "认证：" + h.fp.cert.joinToString("、"),
                    style = MaterialTheme.typography.bodySmall,
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
private fun EvidenceTag(e: Evidence) {
    val (bg, fg) = when (e) {
        Evidence.LITERAL -> Color(0xFF1B5E20) to Color.White
        Evidence.ALIAS_PRIMARY -> Color(0xFF0D47A1) to Color.White
        Evidence.ALIAS_SECONDARY -> Color(0xFF4E342E) to Color(0xFFFFCC80)
    }
    Surface(color = bg, shape = RoundedCornerShape(6.dp)) {
        Text(
            e.label,
            color = fg,
            fontSize = 10.sp,
            modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp),
        )
    }
}

@Composable
private fun DetailCard(d: cn.beaconmfg.app.data.SupplierDetail) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 3.dp),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.tertiaryContainer
        ),
    ) {
        Column(Modifier.padding(10.dp)) {
            Text(d.company, fontWeight = FontWeight.Bold)
            Text(
                "${d.province}·${d.city}  ${d.address}",
                style = MaterialTheme.typography.bodySmall,
            )
            if (d.phone.isNotEmpty()) Text("电话：${d.phone}", style = MaterialTheme.typography.bodySmall)
            if (d.website.isNotEmpty()) Text("官网：${d.website}", style = MaterialTheme.typography.bodySmall)
            if (d.certs.isNotEmpty()) {
                Text("认证：" + d.certs.joinToString("、"), style = MaterialTheme.typography.bodySmall)
            }
        }
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
