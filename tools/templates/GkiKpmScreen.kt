package __NAMESPACE__.ui.kpm

import __NAMESPACE__.ui.util.getRootShell
import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FloatingActionButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject

private fun quote(value: String): String {
    require('\u0000' !in value)
    return "'" + value.replace("'", "'\\''") + "'"
}

private fun moduleName(value: String): String {
    require(value.toByteArray().size in 1..31 && value != "." && value != "..")
    require(value.none { it.isISOControl() || it == '/' || it == '\\' })
    return value
}

/** All operations execute the paired daemon; unsupported kernels stay unsupported. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun GkiKpmScreen() {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val daemon = remember { File(context.applicationInfo.nativeLibraryDir, "libksud.so").absolutePath }
    var busy by remember { mutableStateOf(false) }
    var supported by remember { mutableStateOf(false) }
    var status by remember { mutableStateOf("正在检测 KPM…") }
    var loaded by remember { mutableStateOf(emptyList<String>()) }
    var saved by remember { mutableStateOf(emptyList<String>()) }
    var arguments by remember { mutableStateOf("") }
    var persistent by remember { mutableStateOf(false) }
    var showImport by remember { mutableStateOf(false) }
    var detail by remember { mutableStateOf<String?>(null) }

    suspend fun command(vararg args: String): String = withContext(Dispatchers.IO) {
        val shell = getRootShell()
        check(shell.isRoot) { "无法获得授权的 root shell" }
        val positional = setOf("load", "install", "unload", "uninstall", "control", "info")
        val literalArgs = if (args.firstOrNull() in positional) listOf(args.first(), "--") + args.drop(1) else args.toList()
        // libsu does not collect output unless explicit buffers are supplied.
        // Keep stderr separate: a diagnostic must never become JSON input.
        val result = shell.newJob()
            .add((listOf("/system/bin/toybox", "timeout", "20", daemon, "kpm") + literalArgs).joinToString(" ") { quote(it) })
            .to(ArrayList<String>(), ArrayList<String>()).exec()
        check(result.isSuccess) {
            result.err.joinToString("\n").take(4096).ifBlank {
                if (result.code == 124) "KPM 检测超时，请刷新重试" else "KPM 操作失败（退出码 ${result.code}）"
            }
        }
        result.out.joinToString("\n").also { check(it.toByteArray().size <= 16384) { "KPM 返回的数据过大" } }
    }

    suspend fun jsonCommand(vararg args: String): String {
        val text = command(*args).trim()
        check(text.isNotEmpty()) { "配套守护程序没有返回 KPM 数据，请确认安装的是同次构建的管理器与内核。" }
        return text
    }

    fun names(text: String): List<String> {
        val array = JSONArray(text)
        require(array.length() <= 128)
        return (0 until array.length()).map { moduleName(array.getString(it)) }.distinct()
    }

    suspend fun refresh() {
        supported = false
        loaded = emptyList()
        saved = emptyList()
        status = "正在检测 KPM…"
        val probe = JSONObject(jsonCommand("supported", "--json"))
        require(probe.getInt("abi") == 2) { "KPM 接口版本不匹配，请使用配套内核" }
        supported = probe.getBoolean("supported")
        if (!supported) {
            loaded = emptyList()
            saved = emptyList()
            status = "当前内核没有可用的 KPM 载荷，请使用开启 KPM 的配套内核。"
            return
        }
        val version = JSONObject(jsonCommand("version", "--json"))
        require(version.getInt("abi") == 2) { "KPM 接口版本不匹配，请使用配套内核" }
        loaded = names(jsonCommand("list", "--json"))
        saved = names(jsonCommand("installed", "--json"))
        status = "KPM ${version.getString("version")} · 已加载 ${loaded.size} · 已保存 ${saved.size}"
    }

    fun operation(block: suspend () -> Unit) {
        if (busy) return
        busy = true
        scope.launch {
            try {
                block()
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (error: Exception) {
                supported = false
                status = if (error is org.json.JSONException) "KPM 返回的数据格式无效，请确认管理器与内核配套。" else error.message?.take(4096) ?: "KPM 操作失败"
                detail = status
            } finally {
                busy = false
            }
        }
    }

    val picker = rememberLauncherForActivityResult(ActivityResultContracts.GetContent()) { uri: Uri? ->
        if (uri != null) {
            operation {
                require(supported && arguments.toByteArray().size <= 1023 && '\u0000' !in arguments)
                val temporary = withContext(Dispatchers.IO) {
                    val target = File.createTempFile("gki-kpm-", ".kpm", context.cacheDir)
                    try {
                        val input = context.contentResolver.openInputStream(uri) ?: error("无法读取模块")
                        input.use { stream ->
                            target.outputStream().use { output ->
                                val buffer = ByteArray(65536)
                                var size = 0
                                while (true) {
                                    val count = stream.read(buffer)
                                    if (count < 0) break
                                    size += count
                                    require(size <= 32 * 1024 * 1024) { "模块不能超过 32 MiB" }
                                    output.write(buffer, 0, count)
                                }
                            }
                        }
                        val header = ByteArray(64)
                        require(target.inputStream().use { it.read(header) } == 64)
                        require(header.copyOfRange(0, 7).contentEquals(byteArrayOf(127, 69, 76, 70, 2, 1, 1))) { "需要 ARM64 KPM ELF 模块" }
                        val elf = ByteBuffer.wrap(header).order(ByteOrder.LITTLE_ENDIAN)
                        require(elf.getShort(16).toInt() == 1 && elf.getShort(18).toInt() == 183) { "需要 ARM64 可重定位模块" }
                        target
                    } catch (error: Exception) {
                        target.delete()
                        throw error
                    }
                }
                try {
                    command(if (persistent) "install" else "load", temporary.absolutePath, arguments)
                    refresh()
                } finally {
                    withContext(NonCancellable + Dispatchers.IO) { temporary.delete() }
                }
            }
        }
    }

    LaunchedEffect(Unit) { operation { refresh() } }
    Scaffold(
        topBar = {
            TopAppBar(title = { Text("KPM 管理") }, actions = {
                IconButton(enabled = !busy, onClick = { operation { refresh() } }) {
                    Icon(Icons.Filled.Refresh, contentDescription = "刷新")
                }
            })
        },
        floatingActionButton = {
            FloatingActionButton(onClick = { if (supported && !busy) showImport = true }) {
                Icon(Icons.Filled.Add, contentDescription = "添加 KPM 模块")
            }
        },
    ) { padding ->
        LazyColumn(
            modifier = Modifier.fillMaxSize().padding(padding),
            contentPadding = PaddingValues(start = 16.dp, end = 16.dp, top = 12.dp, bottom = 160.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            item {
                Card(modifier = Modifier.fillMaxWidth()) {
                    Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        Text(status)
                        if (busy) LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
                        if (supported) Text("点击 + 添加模块；嵌入会保存模块与参数，并在重启后自动加载。")
                    }
                }
            }
            if (supported && loaded.isEmpty() && saved.isEmpty()) item { Text("尚未添加 KPM 模块") }
            items((loaded + saved).distinct(), key = { it }) { name ->
                Card(modifier = Modifier.fillMaxWidth()) {
                    Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        Text(name, style = MaterialTheme.typography.titleMedium)
                        Text(listOfNotNull(if (name in loaded) "已加载" else null, if (name in saved) "重启自动加载" else null).joinToString(" · "))
                        if (name in loaded) {
                            TextButton(enabled = !busy, onClick = { operation { detail = command("info", name) } }) { Text("模块信息") }
                            TextButton(enabled = !busy, onClick = { operation { detail = command("control", name, arguments) } }) { Text("发送控制参数") }
                            TextButton(enabled = !busy, onClick = {
                                operation {
                                    command("unload", name)
                                    refresh()
                                }
                            }) { Text("卸载当前已加载模块") }
                        }
                        if (name in saved) {
                            TextButton(enabled = !busy, onClick = {
                                operation {
                                    command("uninstall", name)
                                    refresh()
                                }
                            }) { Text("取消自动加载并删除已保存模块") }
                        }
                    }
                }
            }
        }
    }
    if (showImport) {
        AlertDialog(
            onDismissRequest = { showImport = false },
            title = { Text("添加 KPM 模块") },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                    Text("选择加载或嵌入后，打开 .kpm 文件。加载仅本次启动有效；嵌入会在重启后自动加载。")
                    OutlinedTextField(value = arguments, onValueChange = { arguments = it }, label = { Text("模块参数（可选）") }, modifier = Modifier.fillMaxWidth())
                }
            },
            confirmButton = {
                TextButton(onClick = {
                    showImport = false
                    persistent = true
                    picker.launch("*/*")
                }) { Text("嵌入") }
            },
            dismissButton = {
                TextButton(onClick = {
                    showImport = false
                    persistent = false
                    picker.launch("*/*")
                }) { Text("加载") }
                TextButton(onClick = { showImport = false }) { Text("取消") }
            },
        )
    }
    detail?.let { message ->
        AlertDialog(onDismissRequest = { detail = null }, title = { Text("KPM") }, text = { Text(message) }, confirmButton = { TextButton(onClick = { detail = null }) { Text("关闭") } })
    }
}
