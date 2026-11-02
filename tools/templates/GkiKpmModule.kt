package __NAMESPACE__.ui.kpm

import java.nio.ByteBuffer
import java.nio.ByteOrder

internal fun checkedKpmModuleName(value: String): String {
    require(value.toByteArray().size in 1..31 && value != "." && value != "..") { "无效的 KPM 模块名称" }
    require(value.none { it.isISOControl() || it == '/' || it == '\\' }) { "无效的 KPM 模块名称" }
    return value
}

/** Read .kpm.info with the same ELF/name bounds as the native installer. */
internal fun selectedKpmModuleName(bytes: ByteArray): String {
    require(bytes.size in 64..32 * 1024 * 1024) { "无效的 KPM 文件大小" }
    require(bytes.copyOfRange(0, 7).contentEquals(byteArrayOf(127, 69, 76, 70, 2, 1, 1))) { "需要 ARM64 KPM ELF 模块" }
    val elf = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN)
    fun u16(offset: Int) = elf.getShort(offset).toInt() and 0xffff
    require(u16(16) == 1 && u16(18) == 183) { "需要 ARM64 可重定位模块" }
    fun region(offset: Long, size: Long): ByteArray {
        require(offset >= 0 && size >= 0 && offset <= bytes.size && size <= bytes.size - offset) { "KPM 文件区段越界" }
        return bytes.copyOfRange(offset.toInt(), (offset + size).toInt())
    }
    val table = elf.getLong(40)
    val count = u16(60)
    val stringsIndex = u16(62)
    require(u16(58) == 64 && count in 1..4096 && stringsIndex < count) { "无效的 KPM 区段表" }
    region(table, count * 64L)
    val stringsHeader = (table + stringsIndex * 64L).toInt()
    val strings = region(elf.getLong(stringsHeader + 24), elf.getLong(stringsHeader + 32))
    var name: String? = null
    for (index in 0 until count) {
        val header = (table + index * 64L).toInt()
        val offset = elf.getInt(header).toLong() and 0xffffffffL
        require(offset < strings.size) { "无效的 KPM 区段名称" }
        val tail = strings.copyOfRange(offset.toInt(), minOf(offset + 1024, strings.size.toLong()).toInt())
        val end = tail.indexOf(0)
        require(end >= 0) { "无效的 KPM 区段名称" }
        if (!tail.copyOfRange(0, end).contentEquals(".kpm.info".toByteArray())) continue
        val infoSize = elf.getLong(header + 32)
        require(infoSize in 1..65536) { "KPM 模块信息过大" }
        val info = region(elf.getLong(header + 24), infoSize)
        require(info.lastOrNull() == 0.toByte()) { "无效的 KPM 模块信息" }
        var start = 0
        for (position in info.indices) {
            if (info[position] != 0.toByte()) continue
            val field = info.copyOfRange(start, position)
            if (field.size >= 5 && field.copyOfRange(0, 5).contentEquals("name=".toByteArray())) {
                require(name == null) { "KPM 模块名称重复" }
                name = checkedKpmModuleName(field.decodeToString(5, field.size, throwOnInvalidSequence = true))
            }
            start = position + 1
        }
    }
    return requireNotNull(name) { "KPM 模块缺少名称" }
}
