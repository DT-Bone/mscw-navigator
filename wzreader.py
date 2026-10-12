"""Minimal read-only parser for MapleStory Classic World .wz files.

Reads the static game data files on disk (no process access). Supports the
split layout used by this client (e.g. Data/String/String_000.wz), PKG1 headers
with a version byte, and unkeyed (zero-IV) string encryption.

Usage:
    py -3.11 wzreader.py ls   <file.wz>                 # list directory tree
    py -3.11 wzreader.py dump <file.wz> <Image.img>     # dump one image as JSON
"""
import json
import mmap
import struct
import sys

CRYPTO_CONSTANT = 0x581C3F6D
M32 = 0xFFFFFFFF


def version_hash(v):
    h = 0
    for c in str(v):
        h = (32 * h + ord(c) + 1) & M32
    return h


def encrypted_version(h):
    return 0xFF ^ (h >> 24) & 0xFF ^ (h >> 16) & 0xFF ^ (h >> 8) & 0xFF ^ h & 0xFF


class Reader:
    def __init__(self, data):
        self.d = data
        self.p = 0

    def _unpack(self, fmt, n):
        v = struct.unpack_from(fmt, self.d, self.p)[0]
        self.p += n
        return v

    def u8(self): return self._unpack('<B', 1)
    def s8(self): return self._unpack('<b', 1)
    def i16(self): return self._unpack('<h', 2)
    def u16(self): return self._unpack('<H', 2)
    def i32(self): return self._unpack('<i', 4)
    def u32(self): return self._unpack('<I', 4)
    def i64(self): return self._unpack('<q', 8)
    def f32(self): return self._unpack('<f', 4)
    def f64(self): return self._unpack('<d', 8)

    def cint(self):
        v = self.s8()
        return self.i32() if v == -128 else v

    def clong(self):
        v = self.s8()
        return self.i64() if v == -128 else v

    def wzstr(self):
        n = self.s8()
        if n == 0:
            return ''
        if n > 0:
            if n == 127:
                n = self.i32()
            mask, out = 0xAAAA, []
            for _ in range(n):
                out.append(chr(self.u16() ^ mask))
                mask = (mask + 1) & 0xFFFF
            return ''.join(out)
        n = self.i32() if n == -128 else -n
        raw = bytearray(self.d[self.p:self.p + n])
        self.p += n
        mask = 0xAA
        for i in range(n):
            raw[i] ^= mask
            mask = (mask + 1) & 0xFF
        return raw.decode('latin-1')


class WzFile:
    def __init__(self, path):
        self.path = path
        self._f = open(path, 'rb')
        self.data = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
        r = Reader(self.data)
        if self.data[:4] != b'PKG1':
            raise ValueError(f'{path}: not a PKG1 wz file')
        r.p = 4
        self.size = r.i64()
        self.fstart = r.u32()
        r.p = self.fstart
        self.enc_version = r.u16()
        self.root_pos = r.p
        self.version, self.hash, self.root = self._detect_version()

    def close(self):
        self.data.close()
        self._f.close()

    # --- directory ---------------------------------------------------------
    def _offset(self, r, h):
        o = ((r.p - self.fstart) ^ M32) & M32
        o = (o * h) & M32
        o = (o - CRYPTO_CONSTANT) & M32
        s = o & 0x1F
        o = ((o << s) | (o >> (32 - s))) & M32 if s else o
        o ^= r.u32()
        return (o + self.fstart * 2) & M32

    def _parse_dir(self, pos, h, depth=0):
        r = Reader(self.data)
        r.p = pos
        entries = []
        for _ in range(r.cint()):
            t = r.u8()
            if t == 1:
                r.i32(); r.i16(); self._offset(r, h)
                continue
            if t == 2:
                soff = r.i32()
                save = r.p
                r.p = self.fstart + soff
                t = r.u8()
                name = r.wzstr()
                r.p = save
            elif t in (3, 4):
                name = r.wzstr()
            else:
                raise ValueError(f'bad dir entry type {t}')
            size = r.cint()
            r.cint()  # checksum
            off = self._offset(r, h)
            entries.append({'type': 'dir' if t == 3 else 'img', 'name': name,
                            'size': size, 'offset': off})
        for e in entries:
            if e['type'] == 'dir':
                if depth > 32 or not (0 < e['offset'] < len(self.data)):
                    raise ValueError('bad subdir offset')
                e['children'] = self._parse_dir(e['offset'], h, depth + 1)
        return entries

    def _valid(self, entries):
        for e in entries:
            if not (self.fstart < e['offset'] < len(self.data)):
                return False
            if e['type'] == 'img' and e['size'] > 0 and self.data[e['offset']] not in (0x73, 0x01):
                return False
            if e['type'] == 'dir' and not self._valid(e['children']):
                return False
        return True

    def _detect_version(self):
        for v in range(0, 1000):
            h = version_hash(v)
            if encrypted_version(h) != self.enc_version:
                continue
            try:
                root = self._parse_dir(self.root_pos, h)
            except (ValueError, struct.error, IndexError):
                continue
            if self._valid(root):
                return v, h, root
        raise ValueError(f'{self.path}: could not detect wz version')

    def images(self, entries=None, prefix=''):
        for e in self.root if entries is None else entries:
            path = f'{prefix}{e["name"]}'
            if e['type'] == 'img':
                yield path, e
            else:
                yield from self.images(e['children'], path + '/')

    # --- image property tree ---------------------------------------------
    def read_image(self, entry):
        r = Reader(self.data)
        base = r.p = entry['offset']
        tag = r.u8()
        if tag != 0x73:
            return {'_unsupported_image_tag': tag}
        r.wzstr()  # "Property"
        r.u16()
        return self._prop_list(r, base)

    def _str_block(self, r, base):
        b = r.u8()
        if b in (0x00, 0x73):
            return r.wzstr()
        if b in (0x01, 0x1B):
            rel = r.i32()
            save = r.p
            r.p = base + rel
            s = r.wzstr()
            r.p = save
            return s
        raise ValueError(f'bad string block tag {b:#x} at {r.p - 1:#x}')

    def _prop_list(self, r, base):
        out = {}
        for _ in range(r.cint()):
            name = self._str_block(r, base)
            t = r.u8()
            if t == 0:
                v = None
            elif t in (2, 0x0B):
                v = r.i16()
            elif t in (3, 0x13):
                v = r.cint()
            elif t == 0x14:
                v = r.clong()
            elif t == 4:
                v = r.f32() if r.u8() == 0x80 else 0.0
            elif t == 5:
                v = r.f64()
            elif t == 8:
                v = self._str_block(r, base)
            elif t == 9:
                end = r.u32() + r.p
                v = self._extended(r, base)
                r.p = end
            else:
                raise ValueError(f'unknown property type {t:#x} for {name!r}')
            out[name] = v
        return out

    def _extended(self, r, base):
        kind = self._str_block(r, base)
        if kind == 'Property':
            r.u16()
            return self._prop_list(r, base)
        if kind == 'Canvas':
            r.u8()
            children = {}
            if r.u8() == 1:
                r.u16()
                children = self._prop_list(r, base)
            w, h = r.cint(), r.cint()
            fmt, scale = r.cint(), r.u8()
            r.p += 4
            size = r.i32() - 1
            r.p += 1
            # where the (compressed) pixels are, for canvas.decode: offset, size, w, h, format, scale
            return {'_canvas': f'{w}x{h}', '_data': [r.p, size, w, h, fmt, scale], **children}
        if kind == 'Shape2D#Vector2D':
            return {'x': r.cint(), 'y': r.cint()}
        if kind == 'UOL':
            r.u8()
            return {'_uol': self._str_block(r, base)}
        if kind == 'Sound_DX8':
            return '<sound>'
        return f'<{kind}>'


def _print_tree(wz):
    for path, e in wz.images():
        print(f'{path}  ({e["size"]} bytes)')


if __name__ == '__main__':
    cmd, path = sys.argv[1], sys.argv[2]
    wz = WzFile(path)
    # Any version whose hash decodes the offsets works; this is not the game patch number.
    print(f'# {path}: offset key from version {wz.version}', file=sys.stderr)
    if cmd == 'ls':
        _print_tree(wz)
    elif cmd == 'dump':
        entry = dict(wz.images())[sys.argv[3]]
        json.dump(wz.read_image(entry), sys.stdout, indent=1, ensure_ascii=False)
        print()
