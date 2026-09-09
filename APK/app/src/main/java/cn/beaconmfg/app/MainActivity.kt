package cn.beaconmfg.app

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import cn.beaconmfg.app.ui.ChatScreen
import cn.beaconmfg.app.ui.SettingsScreen

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            MaterialTheme(colorScheme = darkColorScheme()) {
                BeaconApp()
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun BeaconApp(vm: MainViewModel = viewModel()) {
    var tab by remember { mutableIntStateOf(0) }

    Scaffold(
        topBar = {
            TopAppBar(
                // 主标题 + 小字副标题走同一行：用 AnnotatedString 保证基线对齐，
                // 比塞两个 Text 进 Row 干净（Row 会让小字垂直居中，看着是歪的）
                title = {
                    Text(
                        buildAnnotatedString {
                            append("炫招灯塔")
                            withStyle(
                                SpanStyle(
                                    fontSize = 11.sp,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                            ) { append("——让AI世界看见") }
                        },
                        maxLines = 1,
                    )
                },
                actions = {
                    // 清空只在对话页出现——设置页没有可清的内容
                    if (tab == 0) {
                        TextButton(onClick = { vm.clearChat() }) { Text("清空") }
                    }
                    // 设置入口从底部 TabRow 挪到这里：底部导航条会吃掉手势区的点击
                    TextButton(onClick = { tab = if (tab == 0) 1 else 0 }) {
                        Text(if (tab == 0) "设置" else "对话")
                    }
                },
            )
        },
        // 刻意不放 bottomBar：手势导航条（home 栏）会覆盖底部可点击区域。
        // 底部内边距由 Scaffold 的 contentWindowInsets（systemBars）自动处理。
    ) { padding ->
        val m = Modifier.padding(padding)
        when (tab) {
            0 -> ChatScreen(vm, m)
            else -> SettingsScreen(vm, m)
        }
    }
}
