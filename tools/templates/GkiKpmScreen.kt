package __NAMESPACE__.ui.kpm

import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
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
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import __NAMESPACE__.ui.util.getRootShell

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
    var detail by remember { mutableStateOf<String?>(null) }

    suspend fun command(vararg args: String): String = withContext(Dispatchers.IO) {
        val shell = getRootShell()
        check(shell.isRoot) { "无法获得授权的 root shell" }
        val positional = setOf("load", "install", "unload", "uninstall", "control", "info")
        val literalArgs = if (args.firstOrNull() in positional) listOf(args.first(), "--") + args.drop(1) else args.toList()
        val result = shell.newJob().add((listOf(daemon, "kpm") + literalArgs).joinToString(" ") { quote(it) }).exec()
        check(result.isSuccess) { result.err.joinToString("\n").take(4096).ifBlank { "KPM 操作失败：${result.code}" } }
        result.out.joinToString("\n").also { check(it.toByteArray().size <= 16384) }
    }

    fun names(text: String): List<String> {
        val array = JSONArray(text)
        require(array.length() <= 128)
        return (0 until array.length()).map { moduleName(array.getString(it)) }.distinct()
    }

    suspend fun refresh() {
        val probe = JSONObject(command("supported", "--json"))
        require(probe.getInt("abi") == 2)
        supported = probe.getBoolean("supported")
        if (!supported) {
            loaded = emptyList()
            saved = emptyList()
            status = "当前内核没有可用的 KPM 载荷，请使用开启 KPM 的配套内核。"
            return
        }
        val version = JSONObject(command("version", "--json"))
        require(version.getInt("abi") == 2)
        loaded = names(command("list", "--json"))
        saved = names(command("installed", "--json"))
        status = "KPM ${version.getString("version")} · 已加载 ${loaded.size} · 已保存 ${saved.size}"
    }

    fun operation(block: suspend () -> Unit) {
        if (busy) return
        busy = true
        scope.launch {
            try {
                block()
            } catch (error: Exception) {
                detail = error.message?.take(4096) ?: "KPM 操作失败"
            } finally {
                busy = false
            }
        }
    }

    val picker = rememberLauncherForActivityResult(ActivityResultContracts.GetContent()) { uri: Uri? ->
        if (uri != null) operation {
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

    LaunchedEffect(Unit) { operation { refresh() } }
    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(start = 16.dp, end = 16.dp, top = 48.dp, bottom = 120.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item {
            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text("KPM 管理")
                Text(status)
                Button(enabled = !busy, onClick = { operation { refresh() } }) { Text("刷新") }
                OutlinedTextField(value = arguments, onValueChange = { arguments = it }, label = { Text("模块参数（可选）") }, modifier = Modifier.fillMaxWidth())
                Button(enabled = supported && !busy, onClick = { persistent = false; picker.launch("*/*") }) { Text("加载模块（仅本次启动）") }
                Button(enabled = supported && !busy, onClick = { persistent = true; picker.launch("*/*") }) { Text("安装模块（重启自动加载）") }
            }
        }
        items((loaded + saved).distinct(), key = { it }) { name ->
            Card(modifier = Modifier.fillMaxWidth()) {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(name)
                    Text(listOfNotNull(if (name in loaded) "已加载" else null, if (name in saved) "重启自动加载" else null).joinToString(" · "))
                    if (name in loaded) {
                        TextButton(enabled = !busy, onClick = { operation { detail = command("info", name) } }) { Text("模块信息") }
                        TextButton(enabled = !busy, onClick = { operation { detail = command("control", name, arguments) } }) { Text("发送控制参数") }
                        TextButton(enabled = !busy, onClick = { operation { command("unload", name); refresh() } }) { Text("卸载当前已加载模块") }
                    }
                    if (name in saved) {
                        TextButton(enabled = !busy, onClick = { operation { command("uninstall", name); refresh() } }) { Text("取消自动加载并删除已保存模块") }
                    }
                }
            }
        }
    }
    detail?.let { message ->
        AlertDialog(onDismissRequest = { detail = null }, title = { Text("KPM") }, text = { Text(message) }, confirmButton = { TextButton(onClick = { detail = null }) { Text("关闭") } })
    }
}
