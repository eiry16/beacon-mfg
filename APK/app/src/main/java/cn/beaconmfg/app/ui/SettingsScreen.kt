package cn.beaconmfg.app.ui

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
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
import cn.beaconmfg.app.i18n.Lang
import cn.beaconmfg.app.i18n.Strings
import cn.beaconmfg.app.llm.Preset

@Composable
fun SettingsScreen(vm: MainViewModel, modifier: Modifier = Modifier) {
    val s by vm.settings.collectAsState()
    val dataInfo by vm.dataInfo.collectAsState()
    val status by vm.status.collectAsState()
    val st = remember(s.lang) { Strings(Lang.of(s.lang)) }

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
            .imePadding()
            .verticalScroll(rememberScrollState())
            .padding(12.dp)
    ) {
        // ── 语言 ────────────────────────────────────────────────────────────
        // 放最上面：主人看得见才切得动，埋在底部等于没有。
        Text(st.secLanguage, style = MaterialTheme.typography.titleMedium)
        Row(Modifier.fillMaxWidth().padding(top = 6.dp)) {
            Lang.entries.forEach { l ->
                val selected = Lang.of(s.lang) == l
                Button(
                    onClick = { vm.updateSettings(s.copy(lang = l.code)) },
                    modifier = Modifier
                        .weight(1f)
                        .padding(end = if (l == Lang.entries.first()) 6.dp else 0.dp),
                    colors = if (selected) {
                        ButtonDefaults.buttonColors()
                    } else {
                        ButtonDefaults.outlinedButtonColors()
                    },
                ) { Text(l.label) }
            }
        }
        Text(
            st.langNote,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        Text(
            st.secProvider,
            style = MaterialTheme.typography.titleMedium,
            modifier = Modifier.padding(top = 16.dp),
        )
        Text(
            st.keyNote,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        if (!vm.secureStorageAvailable) {
            Text(
                st.keystoreWarn,
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
                ) { Text(p.label(Lang.of(s.lang))) }
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
            label = { Text(st.labelEndpoint) },
            modifier = Modifier.fillMaxWidth().padding(top = 6.dp),
            singleLine = true,
        )
        OutlinedTextField(
            value = model,
            onValueChange = { model = it },
            label = { Text(st.labelModel) },
            modifier = Modifier.fillMaxWidth().padding(top = 6.dp),
            singleLine = true,
        )
        OutlinedTextField(
            value = apiKey,
            onValueChange = { apiKey = it },
            label = { Text(st.labelApiKey) },
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
                    testResult = st.saved
                },
                modifier = Modifier.padding(end = 8.dp),
            ) { Text(st.save) }
            Button(onClick = {
                testResult = st.testing
                vm.testLlm { testResult = it }
            }) { Text(st.testConn) }
        }
        if (testResult.isNotEmpty()) {
            Text(testResult, style = MaterialTheme.typography.bodySmall)
        }

        Text(
            st.secData,
            style = MaterialTheme.typography.titleMedium,
            modifier = Modifier.padding(top = 16.dp),
        )
        OutlinedTextField(
            value = dataBase,
            onValueChange = { dataBase = it },
            label = { Text(st.labelDataBase) },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true,
        )
        Row(Modifier.padding(top = 8.dp)) {
            Button(onClick = { vm.refreshData() }, modifier = Modifier.padding(end = 8.dp)) {
                Text(st.updateNow)
            }
            Button(onClick = { vm.pingData { testResult = it } }) { Text(st.ping) }
        }
        Row(Modifier.padding(vertical = 6.dp)) {
            Text(st.autoUpdate, style = MaterialTheme.typography.bodyMedium)
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
            st.secProbe,
            style = MaterialTheme.typography.titleMedium,
            modifier = Modifier.padding(top = 16.dp),
        )
        OutlinedTextField(
            value = probe,
            onValueChange = { probe = it },
            label = { Text(st.probeHint) },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true,
        )
        Row(Modifier.padding(top = 8.dp)) {
            Button(onClick = {
                probeOut = vm.aliasExplain(probe) + "\n" + vm.localSearch(probe)
            }) { Text(st.probeRun) }
        }
        if (probeOut.isNotEmpty()) {
            Text(
                probeOut,
                style = MaterialTheme.typography.bodySmall,
                modifier = Modifier.padding(top = 6.dp),
            )
        }
        Text(
            st.aliasCount(vm.aliasSize()),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}
