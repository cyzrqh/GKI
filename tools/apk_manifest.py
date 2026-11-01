"""Read the package and native versions from a compiled Android XML manifest."""
from __future__ import annotations

import struct

MAX_MANIFEST = 2 * 1024 * 1024


def manifest_identity(raw: bytes) -> dict:
    if not 8 <= len(raw) <= MAX_MANIFEST:
        raise ValueError('Android manifest has an invalid size')
    kind, header, size = struct.unpack_from('<HHI', raw)
    if kind != 3 or header != 8 or size != len(raw):
        raise ValueError('Expected a compiled Android XML manifest')
    strings = None
    result = None
    position = header
    while position < size:
        if position + 8 > size:
            raise ValueError('Truncated Android XML chunk')
        kind, head, length = struct.unpack_from('<HHI', raw, position)
        end = position + length
        if head < 8 or length < head or end > size:
            raise ValueError('Invalid Android XML chunk extent')
        chunk = raw[position:end]
        if kind == 1:
            if strings is not None or head < 28:
                raise ValueError('Invalid Android XML string pool')
            count, styles, flags, start, style_start = struct.unpack_from('<IIIII', chunk, 8)
            if count > 65536 or head + (count + styles) * 4 > start or start > length:
                raise ValueError('Invalid Android XML string offsets')
            limit = style_start or length
            if not start <= limit <= length:
                raise ValueError('Invalid Android XML string data extent')
            utf8 = bool(flags & 0x100)

            def string_length(at):
                width = 1 if utf8 else 2
                if at + width > limit:
                    raise ValueError('Truncated Android XML string length')
                number = chunk[at] if utf8 else struct.unpack_from('<H', chunk, at)[0]
                bit = 0x80 if utf8 else 0x8000
                at += width
                if number & bit:
                    if at + width > limit:
                        raise ValueError('Truncated Android XML long string length')
                    low = chunk[at] if utf8 else struct.unpack_from('<H', chunk, at)[0]
                    number = ((number & (bit - 1)) << (width * 8)) | low
                    at += width
                return number, at

            strings = []
            for index in range(count):
                offset = struct.unpack_from('<I', chunk, head + index * 4)[0]
                at = start + offset
                units, at = string_length(at)
                if utf8:
                    units, at = string_length(at)
                byte_length = units if utf8 else units * 2
                terminator = 1 if utf8 else 2
                if at + byte_length + terminator > limit or any(chunk[at + byte_length:at + byte_length + terminator]):
                    raise ValueError('Invalid Android XML string extent')
                strings.append(chunk[at:at + byte_length].decode('utf-8' if utf8 else 'utf-16le'))
        elif kind == 0x0102:
            if strings is None or head < 16 or head + 20 > length:
                raise ValueError('Invalid Android XML element')
            ns, name, first, width, count = struct.unpack_from('<IIHHH', chunk, head)
            if name >= len(strings) or width < 20 or first < 20 or head + first + count * width > length:
                raise ValueError('Invalid Android XML attributes')
            if strings[name] == 'manifest':
                if result is not None:
                    raise ValueError('Duplicate Android manifest element')
                result = {}
                for index in range(count):
                    at = head + first + index * width
                    namespace, key, value, value_size, reserved, datatype, typed = struct.unpack_from('<IIIHBBI', chunk, at)
                    if key >= len(strings) or value_size != 8 or reserved:
                        raise ValueError('Invalid Android manifest attribute')
                    key = strings[key]
                    if key not in ('package', 'versionCode', 'versionName'):
                        continue
                    if key in result:
                        raise ValueError('Duplicate Android manifest identity attribute')
                    if datatype == 3 and typed < len(strings):
                        result[key] = strings[typed]
                    elif datatype in (0x10, 0x11):
                        result[key] = typed
                    elif value < len(strings):
                        result[key] = strings[value]
                    else:
                        raise ValueError('Unsupported Android manifest identity value')
        position = end
    if result is None or set(result) != {'package', 'versionCode', 'versionName'}:
        raise ValueError('Android manifest lacks its package and native versions')
    if (not isinstance(result['package'], str) or not isinstance(result['versionName'], str)
            or type(result['versionCode']) is not int or not 1 <= result['versionCode'] <= 2100000000):
        raise ValueError('Android manifest identity types are invalid')
    return {'package': result['package'], 'version_code': result['versionCode'], 'version_name': result['versionName']}
