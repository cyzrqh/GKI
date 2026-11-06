package __NAMESPACE__.ui.kpm

import __NAMESPACE__.ui.util.getRootShell
import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Extension
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExtendedFloatingActionButton
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
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import java.io.File
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

private data class SelectedKpmModule(val file: File, val name: String)

private class KpmRootUnavailable : Exception()

/** Limit quiet failures to authorization/root acquisition, never daemon work. */
private inline fun <T> kpmRootOrUnavailable(block: () -> T): T = try {
    block()
} catch (cancelled: CancellationException) {
    throw cancelled
} catch (_: Exception) {
    throw KpmRootUnavailable()
}

private inline fun kpmUiAuthorized(block: () -> Boolean): Boolean = try {
    kpmRootOrUnavailable(block)
} catch (_: KpmRootUnavailable) {
    false
}

/** All operations execute the paired daemon; unsupported kernels stay unsupported. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun GkiKpmScreen(bottomPadding: Dp = 104.dp) {
    val authorized = __KPM_AUTHORIZATION__
    if (!authorized) return
    GkiKpmTheme { GkiKpmScreenContent(bottomPadding) }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun GkiKpmScreenContent(bottomPadding: Dp) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val daemon = remember { File(context.applicationInfo.nativeLibraryDir, "libksud.so").absolutePath }
    var busy by remember { mutableStateOf(false) }
    var supported by remember { mutableStateOf(false) }
    var status by remember { mutableStateOf("正在检测 KPM…") }
    var loaded by remember { mutableStateOf(emptyList<String>()) }
    var saved by remember { mutableStateOf(emptyList<String>()) }
    var arguments by remember { mutableStateOf("") }
    var selected by remember { mutableStateOf<SelectedKpmModule?>(null) }
    var pickingFile by remember { mutableStateOf(false) }
    var search by remember { mutableStateOf("") }
    var controlTarget by remember { mutableStateOf<String?>(null) }
    var detail by remember { mutableStateOf<String?>(null) }

    suspend fun command(vararg args: String): String = withContext(Dispatchers.IO) {
        if (!kpmRootOrUnavailable { __KPM_COMMAND_AUTHORIZATION__ }) throw KpmRootUnavailable()
        val shell = kpmRootOrUnavailable { getRootShell() }
        if (!shell.isRoot) throw KpmRootUnavailable()
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
        return (0 until array.length()).map { checkedKpmModuleName(array.getString(it)) }.distinct()
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
            } catch (_: KpmRootUnavailable) {
                supported = false
                loaded = emptyList()
                saved = emptyList()
                status = ""
                detail = null
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
        pickingFile = false
        if (uri != null) {
            operation {
                require(supported)
                var staged: File? = null
                try {
                    val module = withContext(Dispatchers.IO) {
                        val target = File.createTempFile("gki-kpm-", ".kpm", context.cacheDir)
                        staged = target
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
                            SelectedKpmModule(target, selectedKpmModuleName(target.readBytes()))
                        } catch (error: Exception) {
                            target.delete()
                            throw error
                        }
                    }
                    selected?.file?.delete()
                    selected = module
                    staged = null
                } finally {
                    withContext(NonCancellable + Dispatchers.IO) { staged?.delete() }
                }
            }
        }
    }

    fun installSelected(embed: Boolean) {
        val module = selected ?: return
        operation {
            selected = null
            try {
                command(if (embed) "install" else "load", module.file.absolutePath, "")
                refresh()
            } finally {
                withContext(NonCancellable + Dispatchers.IO) { module.file.delete() }
            }
        }
    }

    DisposableEffect(Unit) { onDispose { selected?.file?.delete() } }

    LaunchedEffect(Unit) { operation { refresh() } }
    Scaffold(
        containerColor = MaterialTheme.colorScheme.surface,
        topBar = {
            TopAppBar(title = { Text("KPM") }, actions = {
                IconButton(enabled = !busy, onClick = { operation { refresh() } }) {
                    Icon(Icons.Filled.Refresh, contentDescription = "刷新")
                }
            })
        },
        floatingActionButton = {
            ExtendedFloatingActionButton(
                modifier = Modifier.padding(bottom = bottomPadding),
                onClick = {
                    if (supported && !busy && !pickingFile) {
                        pickingFile = true
                        try {
                            picker.launch("*/*")
                        } catch (error: Exception) {
                            pickingFile = false
                            detail = error.message ?: "无法打开文件选择器"
                        }
                    }
                },
                icon = { Icon(Icons.Filled.Add, contentDescription = null) },
                text = { Text("KPM 安装") },
            )
        },
    ) { padding ->
        LazyColumn(
            modifier = Modifier.fillMaxSize().padding(padding),
            contentPadding = PaddingValues(start = 16.dp, end = 16.dp, top = 12.dp, bottom = 160.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            item {
                Card(
                    modifier = Modifier.fillMaxWidth(),
                    shape = RoundedCornerShape(32.dp),
                    colors = CardDefaults.cardColors(
                        containerColor = MaterialTheme.colorScheme.surfaceContainer,
                        contentColor = MaterialTheme.colorScheme.onSurface,
                    ),
                ) {
                    Row(modifier = Modifier.padding(24.dp), horizontalArrangement = Arrangement.spacedBy(20.dp), verticalAlignment = Alignment.CenterVertically) {
                        Icon(Icons.Filled.Extension, contentDescription = null, modifier = Modifier.size(48.dp))
                        Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                            Text("内核模块", style = MaterialTheme.typography.titleLarge)
                            Text(if (supported) "已加载 ${loaded.size} 个模块" else status)
                        }
                    }
                }
                if (busy) LinearProgressIndicator(modifier = Modifier.fillMaxWidth().padding(top = 8.dp))
            }
            item {
                OutlinedTextField(
                    value = search,
                    onValueChange = { search = it },
                    singleLine = true,
                    placeholder = { Text("搜索内核模块") },
                    leadingIcon = { Icon(Icons.Filled.Search, contentDescription = null) },
                    shape = RoundedCornerShape(32.dp),
                    modifier = Modifier.fillMaxWidth(),
                )
            }
            val modules = (loaded + saved).distinct().filter { it.contains(search, ignoreCase = true) }
            if (supported && modules.isEmpty()) {
                item {
                    Column(
                        modifier = Modifier.fillMaxWidth().padding(top = 56.dp),
                        horizontalAlignment = Alignment.CenterHorizontally,
                        verticalArrangement = Arrangement.spacedBy(20.dp),
                    ) {
                        Icon(Icons.Filled.Extension, contentDescription = null, tint = MaterialTheme.colorScheme.primary, modifier = Modifier.size(80.dp))
                        Text(if (search.isBlank()) "当前没有安装内核模块" else "没有匹配的内核模块")
                    }
                }
            }
            items(modules, key = { it }) { name ->
                Card(
                    modifier = Modifier.fillMaxWidth(),
                    colors = CardDefaults.cardColors(
                        containerColor = MaterialTheme.colorScheme.surfaceContainer,
                        contentColor = MaterialTheme.colorScheme.onSurface,
                    ),
                ) {
                    Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        Text(name, style = MaterialTheme.typography.titleMedium)
                        Text(listOfNotNull(if (name in loaded) "已加载" else null, if (name in saved) "重启自动加载" else null).joinToString(" · "))
                        if (name in loaded) {
                            TextButton(enabled = !busy, onClick = { operation { detail = command("info", name) } }) { Text("模块信息") }
                            TextButton(enabled = !busy, onClick = {
                                arguments = ""
                                controlTarget = name
                            }) { Text("发送控制参数") }
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
    selected?.let { module ->
        AlertDialog(
            onDismissRequest = {
                if (!busy) {
                    selected = null
                    module.file.delete()
                }
            },
            icon = { Icon(Icons.Filled.Extension, contentDescription = null) },
            title = { Text("KPM 安装") },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(20.dp)) {
                    Text(module.name, style = MaterialTheme.typography.titleMedium)
                    Text("仅在本次开机期间加载，重启后失效。")
                    TextButton(enabled = !busy && supported, onClick = { installSelected(false) }, modifier = Modifier.fillMaxWidth()) { Text("加载") }
                    Text("立即加载并保存，开机时自动加载。请仅安装可信模块。")
                    Button(enabled = !busy && supported, onClick = { installSelected(true) }, modifier = Modifier.fillMaxWidth()) { Text("嵌入") }
                }
            },
            confirmButton = {
                TextButton(enabled = !busy, onClick = {
                    selected = null
                    module.file.delete()
                }) { Text("取消") }
            },
        )
    }
    controlTarget?.let { name ->
        AlertDialog(
            onDismissRequest = { controlTarget = null },
            title = { Text(name) },
            text = { OutlinedTextField(value = arguments, onValueChange = { arguments = it }, label = { Text("模块参数") }) },
            confirmButton = {
                TextButton(enabled = !busy, onClick = {
                    operation {
                        require(arguments.toByteArray().size <= 1023 && '\u0000' !in arguments)
                        detail = command("control", name, arguments)
                        controlTarget = null
                    }
                }) { Text("发送") }
            },
            dismissButton = { TextButton(onClick = { controlTarget = null }) { Text("取消") } },
        )
    }
    detail?.let { message ->
        AlertDialog(onDismissRequest = { detail = null }, title = { Text("KPM") }, text = { Text(message) }, confirmButton = { TextButton(onClick = { detail = null }) { Text("关闭") } })
    }
}
