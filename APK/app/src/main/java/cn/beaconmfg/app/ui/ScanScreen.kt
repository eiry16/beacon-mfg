package cn.beaconmfg.app.ui

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.util.Log
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.LocalLifecycleOwner
import cn.beaconmfg.app.data.Uscc
import cn.beaconmfg.app.i18n.Strings
import com.google.zxing.BarcodeFormat
import com.google.zxing.BinaryBitmap
import com.google.zxing.DecodeHintType
import com.google.zxing.MultiFormatReader
import com.google.zxing.PlanarYUVLuminanceSource
import com.google.zxing.common.HybridBinarizer
import java.util.concurrent.Executors

private const val TAG = "BeaconScan"

/**
 * 扫营业执照上的码，取出 18 位统一社会信用代码。
 *
 * ## 必须说清的一件事：这个界面**不等于**身份核验
 *
 * 2023 年后新版营业执照上的「企业码」，只有电子营业执照小程序、微信/支付宝，
 * 或已获市场监管部门接入资质的系统，才能解出照面信息（名称/法人/经营范围）。
 * 通用扫码器扫它，可能只得到一串内部 ID，甚至什么都得不到。
 *
 * 所以这里的设计是**尽力而为 + 诚实降级**：
 * - 扫出 18 位号码 → 校验位通过后直接用（这是最理想的情况）
 * - 扫出别的东西 → 原样显示给用户看，并说明「这是正常的，不是坏了」，引导手输
 * - 什么都没扫到 → 同上
 *
 * 绝不做的事：把「扫到了一串 ID」包装成「已核验该企业身份」。
 * 那是在拿主人的数据信用开玩笑——校验位通过都不代表企业真实存在，
 * 更别说一串谁也看不懂的 ID 了。
 */
@Composable
fun ScanScreen(
    s: Strings,
    onUscc: (String) -> Unit,
    onClose: () -> Unit,
) {
    val context = LocalContext.current
    var granted by remember {
        mutableStateOf(
            ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) ==
                PackageManager.PERMISSION_GRANTED
        )
    }
    // 用户彻底拒绝（勾了「不再询问」）后，再弹系统框也不会有反应，
    // 这时必须给一条能走下去的路（手输），而不是让界面卡在"请授权"。
    var permanentlyDenied by remember { mutableStateOf(false) }
    var raw by remember { mutableStateOf<String?>(null) }
    var manual by remember { mutableStateOf("") }
    var error by remember { mutableStateOf<String?>(null) }

    val launcher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { ok ->
        granted = ok
        if (!ok) permanentlyDenied = true
    }

    LaunchedEffect(Unit) {
        if (!granted) {
            runCatching { launcher.launch(Manifest.permission.CAMERA) }
                .onFailure { error = it.message }
        }
    }

    Surface(modifier = Modifier.fillMaxSize()) {
        Column(Modifier.fillMaxSize().imePadding()) {
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .weight(1f),
            ) {
                when {
                    !granted -> Column(
                        modifier = Modifier
                            .fillMaxSize()
                            .padding(20.dp),
                        verticalArrangement = Arrangement.Center,
                    ) {
                        Text(
                            if (permanentlyDenied) s.scanPermissionDenied else s.scanPermissionNeeded,
                            style = MaterialTheme.typography.bodyMedium,
                        )
                        if (!permanentlyDenied) {
                            Spacer(Modifier.height(12.dp))
                            Button(onClick = { launcher.launch(Manifest.permission.CAMERA) }) {
                                Text(s.scanPermissionNeeded)
                            }
                        }
                    }

                    error != null -> CenteredText(error!!)

                    else -> CameraPreview(
                        s = s,
                        onPayload = { text ->
                            val code = Uscc.extract(text)
                            if (code != null && Uscc.check(code).first) {
                                onUscc(code)
                            } else {
                                raw = text
                            }
                        },
                        onError = { error = it },
                    )
                }
            }

            // 下半区：状态 / 手输 / 关闭
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(16.dp),
            ) {
                if (raw != null) {
                    Text(
                        s.scanNoUscc,
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.error,
                    )
                    Spacer(Modifier.height(6.dp))
                    Text(
                        s.scanRawPrefix + raw!!.take(120),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        maxLines = 3,
                        overflow = TextOverflow.Ellipsis,
                    )
                    Spacer(Modifier.height(6.dp))
                    Text(
                        s.scanWhyNot,
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                } else if (granted && error == null) {
                    Text(
                        s.scanHint,
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }

                Spacer(Modifier.height(12.dp))

                OutlinedTextField(
                    value = manual,
                    onValueChange = { manual = it },
                    modifier = Modifier.fillMaxWidth(),
                    placeholder = { Text(s.scanManualHint) },
                    singleLine = true,
                )

                Spacer(Modifier.height(8.dp))

                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    // 手输也要走同一套校验：不校验就把 18 位乱码当凭证收下，
                    // 后面用它做主体锚点时会一路错下去。
                    Button(
                        onClick = {
                            val code = Uscc.extract(manual)
                            if (code != null && Uscc.check(code).first) {
                                onUscc(code)
                            } else {
                                val (_, why) = Uscc.check(code ?: manual)
                                error = why
                            }
                        },
                        enabled = manual.isNotBlank(),
                    ) { Text(s.send) }
                    TextButton(onClick = onClose) { Text(s.actionCancel) }
                }
            }
        }
    }
}

@Composable
private fun CenteredText(text: String) {
    Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Text(text, style = MaterialTheme.typography.bodyMedium)
    }
}

/**
 * CameraX 预览 + zxing 解码。
 *
 * 解析到第一个有效载荷就回调一次，之后不再回调（用 `consumed` 闸住）——
 * 相机每秒 30 帧，不闸住的话 onPayload 会被刷爆。
 */
@Composable
private fun CameraPreview(
    s: Strings,
    onPayload: (String) -> Unit,
    onError: (String) -> Unit,
) {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    val executor = remember { Executors.newSingleThreadExecutor() }

    // 只开需要的格式：营业执照上是 QR，另加两种常见一维条码做兜底。
    // 格式列表越短，解码越快——每帧都要试一遍，这是实打实的开销。
    val reader = remember {
        MultiFormatReader().apply {
            setHints(
                mapOf<DecodeHintType, Any>(
                    DecodeHintType.POSSIBLE_FORMATS to listOf(
                        BarcodeFormat.QR_CODE,
                        BarcodeFormat.CODE_128,
                        BarcodeFormat.CODE_39,
                    )
                )
            )
        }
    }

    // 闸：第一帧命中后立刻置 true。放在 Compose 状态里也行，但这是个纯回调侧的开关，
    // 用普通数组更直接，也避免重组带来的歧义。
    val consumed = remember { booleanArrayOf(false) }

    DisposableEffect(Unit) {
        onDispose { executor.shutdown() }
    }

    // 只保留最新一帧：营业执照是静态的，堆着处理旧帧没有意义，
    // 而且 STRATEGY_KEEP_ONLY_LATEST 能避免低端机上分析器积压。
    val analyzer = remember {
        ImageAnalysis.Analyzer { proxy -> analyze(proxy, reader, consumed, onPayload) }
    }

    AndroidView(
        modifier = Modifier.fillMaxSize(),
        factory = { ctx ->
            PreviewView(ctx).also { view ->
                val future = ProcessCameraProvider.getInstance(ctx)
                future.addListener({
                    val provider = runCatching { future.get() }.getOrNull()
                    if (provider == null) {
                        onError(ctx.cameraErrorMessage(s))
                        return@addListener
                    }
                    val preview = Preview.Builder().build().also {
                        it.surfaceProvider = view.surfaceProvider
                    }
                    val analysis = ImageAnalysis.Builder()
                        .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                        .build()
                        .also { it.setAnalyzer(executor, analyzer) }
                    runCatching {
                        provider.unbindAll()
                        provider.bindToLifecycle(
                            lifecycleOwner,
                            CameraSelector.DEFAULT_BACK_CAMERA,
                            preview,
                            analysis,
                        )
                    }.onFailure { onError(it.message ?: ctx.cameraErrorMessage(s)) }
                }, ContextCompat.getMainExecutor(ctx))
            }
        },
    )
}

private fun Context.cameraErrorMessage(s: Strings): String =
    if (packageManager.hasSystemFeature(PackageManager.FEATURE_CAMERA_ANY)) s.cameraFailed
    else s.scanNoCamera

/**
 * 从相机的一帧里解码。
 *
 * ## 为什么只取 Y 平面
 *
 * YUV_420_888 的 Y 通道本身就是灰度图，而 zxing 的解码只需要亮度信息。
 * 所以直接拿 Y 平面喂 `PlanarYUVLuminanceSource`，**不需要任何 RGB 转换**——
 * 那一步是纯浪费，还会拖慢每帧的处理速度。
 *
 * ## rowStride 不等于 width
 *
 * 相机给的 Y 平面每行末尾可能有对齐填充，`rowStride >= width`。
 * 用 width 当行宽会把图像拧斜，解码必然失败。所以行宽用 rowStride，
 * 再用 crop 参数把右侧填充裁掉。
 *
 * ## 旋转怎么办
 *
 * 这里**不做手动旋转**，依赖 zxing 的定位图案检测（QR 四角有 finder pattern，
 * 90/180/270 都能认出来）。如果实测斜着/倒着扫不出来，再补旋转逻辑。
 */
private fun analyze(
    proxy: ImageProxy,
    reader: MultiFormatReader,
    consumed: BooleanArray,
    onPayload: (String) -> Unit,
) {
    if (consumed[0]) {
        proxy.close()
        return
    }
    try {
        val plane = proxy.planes[0]
        val buffer = plane.buffer
        val data = ByteArray(buffer.remaining())
        buffer.get(data)

        val rowStride = plane.rowStride
        val height = proxy.height
        val source = PlanarYUVLuminanceSource(
            data,
            rowStride,
            height,
            0,
            0,
            minOf(proxy.width, rowStride),
            height,
            false,
        )
        val text = runCatching { reader.decodeWithState(BinaryBitmap(HybridBinarizer(source))) }
            .getOrNull()
            ?.text
        if (!text.isNullOrBlank() && !consumed[0]) {
            consumed[0] = true
            onPayload(text)
        }
    } catch (t: Throwable) {
        // 单帧解码失败是常态（模糊、角度不对、没有码），不值得刷日志
        Log.d(TAG, "frame skipped: ${t.message}")
    } finally {
        // reset 必须调：MultiFormatReader 是有状态的，不重置会让下一帧继续用上一次的中间状态
        runCatching { reader.reset() }
        // 无论成败都必须 close，否则 CameraX 的帧缓冲会被卡死，预览直接冻住
        proxy.close()
    }
}
