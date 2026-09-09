package cn.beaconmfg.app.ui

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import cn.beaconmfg.app.MainViewModel
import cn.beaconmfg.app.llm.Preset

@Composable
fun SettingsScreen(vm: MainViewModel, modifier: Modifier = Modifier) {
    val s by vm.settings.collectAsState()
    val dataInfo by vm.dataInfo.collectAsState()
    val status by vm.status.collectAsState()

    var baseUrl by remember(s.baseUrl) { mutableStateOf(s.baseUrl) }
    var model by remember(s.model) { mutableStateOf(s.model) }
    var apiKey by remember(s.apiKey) { mutableStateOf(s.apiKey) }
    var dataBase by remember(s.dataBase) { mutableStateOf(s.dataBase) }
    var testResult by remember { mutableStateOf("") }
    var probe by remember { mutableStateOf("") }
    var probeOut by remember { mutableStateOf("") }

    Column(
        modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(12.dp)
    ) {
        Text("模型服务商（BYOK）", style = MaterialTheme.typography.titleMedium)
        Text(
            "key 只保存在本机 Keystore 加密区，请求直连你选的服务商，不经过本项目任何服务器。",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        if (!vm.secureStorageAvailable) {
            Text(
                "⚠ 本机 Keystore 不可用，key 将明文存储。建议换一台设备再填。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.error,
            )
        }
        Preset.values().filter { it != Preset.CUSTOM }.forEach { p ->
            Row(Modifier.fillMaxWidth().padding(vertical = 2.dp)) {
                Button(
                    onClick = {
                        vm.applyPreset(p)
                        baseUrl = p.endpoint
                        model = p.models.firstOrNull().orEmpty()
                    },
                    modifier = Modifier.weight(1f),
                ) { Text(p.label) }
                Text(
                    p.models.joinToString(" / "),
                    style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier
                        .weight(1f)
                        .padding(start = 8.dp),
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }

        OutlinedTextField(
            value = baseUrl,
            onValueChange = { baseUrl = it },
            label = { Text("API 端点（OpenAI 兼容）") },
            modifier = Modifier.fillMaxWidth().padding(top = 6.dp),
            singleLine = true,
        )
        OutlinedTextField(
            value = model,
            onValueChange = { model = it },
            label = { Text("模型名") },
            modifier = Modifier.fillMaxWidth().padding(top = 6.dp),
            singleLine = true,
        )
        OutlinedTextField(
            value = apiKey,
            onValueChange = { apiKey = it },
            label = { Text("API Key") },
            modifier = Modifier.fillMaxWidth().padding(top = 6.dp),
            singleLine = true,
            visualTransformation = PasswordVisualTransformation(),
        )
        Row(Modifier.padding(top = 8.dp)) {
            Button(
                onClick = {
                    vm.updateSettings(
                        s.copy(
                            baseUrl = baseUrl.trim(),
                            model = model.trim(),
                            apiKey = apiKey.trim(),
                            dataBase = dataBase.trim(),
                        )
                    )
                    testResult = "已保存"
                },
                modifier = Modifier.padding(end = 8.dp),
            ) { Text("保存") }
            Button(onClick = {
                testResult = "测试中…"
                vm.testLlm { testResult = it }
            }) { Text("测试连接") }
        }
        if (testResult.isNotEmpty()) {
            Text(testResult, style = MaterialTheme.typography.bodySmall)
        }

        Text(
            "数据源与更新",
            style = MaterialTheme.typography.titleMedium,
            modifier = Modifier.padding(top = 16.dp),
        )
        OutlinedTextField(
            value = dataBase,
            onValueChange = { dataBase = it },
            label = { Text("数据源根地址") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true,
        )
        Row(Modifier.padding(top = 8.dp)) {
            Button(onClick = { vm.refreshData() }, modifier = Modifier.padding(end = 8.dp)) {
                Text("立即更新")
            }
            Button(onClick = { vm.pingData { testResult = it } }) { Text("检测连通性") }
        }
        Row(Modifier.padding(vertical = 6.dp)) {
            Text("联网时自动更新指纹", style = MaterialTheme.typography.bodyMedium)
            Switch(
                checked = s.autoUpdate,
                onCheckedChange = { vm.updateSettings(s.copy(autoUpdate = it)) },
            )
        }
        if (status.isNotEmpty()) Text(status, style = MaterialTheme.typography.bodySmall)

        Card(
            modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant),
        ) {
            Column(Modifier.padding(10.dp)) {
                Text(dataInfo, style = MaterialTheme.typography.bodySmall)
            }
        }

        Text(
            "本地自检（不经过 LLM）",
            style = MaterialTheme.typography.titleMedium,
            modifier = Modifier.padding(top = 16.dp),
        )
        OutlinedTextField(
            value = probe,
            onValueChange = { probe = it },
            label = { Text("输入采购词，如 齿轮 / 输送线") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true,
        )
        Row(Modifier.padding(top = 8.dp)) {
            Button(onClick = {
                probeOut = vm.aliasExplain(probe) + "\n" + vm.localSearch(probe)
            }) { Text("本地检索") }
        }
        if (probeOut.isNotEmpty()) {
            Text(
                probeOut,
                style = MaterialTheme.typography.bodySmall,
                modifier = Modifier.padding(top = 6.dp),
            )
        }
        Text(
            "别名表 ${vm.aliasSize()} 条",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}
