"""Decode WZ canvas (image) data into an RGBA numpy array.

Canvas pixels are zlib-compressed, optionally split into length-prefixed blocks ("listWz"; this
client uses no key, so the blocks are just joined). Formats: 1 = BGRA4444, 2 = BGRA8888,
513 = RGB565, 517 = RGB565 in 16x16 blocks.
"""
import struct
import zlib

import numpy as np

ZLIB_HEADERS = (b'\x78\x9c', b'\x78\xda', b'\x78\x01', b'\x78\x5e')


def decode(wz, data):
    """data: the '_data' list wzreader stores for a canvas -> (h, w, 4) uint8 RGBA, or None."""
    offset, size, w, h, fmt, scale = data
    raw = bytes(wz.data[offset:offset + size])
    if raw[:2] not in ZLIB_HEADERS:
        joined, p = bytearray(), 0
        while p + 4 <= len(raw):
            n = struct.unpack_from('<i', raw, p)[0]
            joined += raw[p + 4:p + 4 + n]
            p += 4 + n
        raw = bytes(joined)
    try:
        px = zlib.decompressobj().decompress(raw)  # streams may lack a valid end checksum
    except zlib.error:
        return None
    w, h = w >> scale, h >> scale  # newer clients may store a downscaled image
    a = np.frombuffer(px, np.uint8)
    if fmt == 1:  # BGRA4444
        a = a[:w * h * 2].reshape(h, w, 2)
        lo, hi = a[..., 0], a[..., 1]
        return np.stack([(hi & 15) * 17, (lo >> 4) * 17, (lo & 15) * 17, (hi >> 4) * 17], axis=2).astype(np.uint8)
    if fmt == 2:  # BGRA8888
        a = a[:w * h * 4].reshape(h, w, 4)
        return a[..., [2, 1, 0, 3]].copy()
    if fmt in (513, 517):  # RGB565 (517: one value per 16x16 block)
        bw, bh = (w, h) if fmt == 513 else ((w + 15) // 16, (h + 15) // 16)
        v = a[:bw * bh * 2].view('<u2').reshape(bh, bw).astype(np.uint32)
        rgb = np.stack([(v >> 11) * 255 // 31, ((v >> 5) & 63) * 255 // 63, (v & 31) * 255 // 31,
                        np.full_like(v, 255)], axis=2).astype(np.uint8)
        if fmt == 517:
            rgb = rgb.repeat(16, axis=0).repeat(16, axis=1)[:h, :w]
        return rgb
    return None
