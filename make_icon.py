"""Draws the navigator's icon (a compass needle on a dark disc) for the desktop shortcut.

    python make_icon.py app.ico
"""
import sys

from PIL import Image, ImageDraw

BG, ACCENT, LIGHT = '#1d2433', '#f2b134', '#e8ecf4'  # the overlay's colours
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def draw(size=256):
    im = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    m, c, w = size * 0.04, size / 2, size * 0.14
    d.ellipse([m, m, size - m, size - m], fill=BG, outline=ACCENT, width=int(size * 0.06))
    d.polygon([(c, size * 0.15), (c + w, c), (c - w, c)], fill=ACCENT)  # the needle's north half
    d.polygon([(c, size * 0.85), (c + w, c), (c - w, c)], fill=LIGHT)
    return im


def save(path):
    draw().save(path, sizes=SIZES)


if __name__ == '__main__':
    save(sys.argv[1])
