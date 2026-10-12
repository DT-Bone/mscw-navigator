"""Measure where the character really is on screen vs. where the navigator predicts.

    py -3.11 calibrate.py [character name] [seconds]

While the game is in front, about once a second: find the player's minimap dot, predict the
feet position on screen, then OCR the area around it for the character's name tag (which is
centred under the feet). Writes samples to calibration.jsonl and prints a summary.
"""
import difflib
import json
import pathlib
import sys
import time

import nav_live  # sets DPI awareness first
import minimap
import mss
import numpy as np

from maps import find as find_map, load_maps
from navgraph import load_graph
from ocr_client import SyncOcr

HERE = pathlib.Path(__file__).parent



def main():
    name = sys.argv[1] if len(sys.argv) > 1 else 'Bankruptcy'
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 120
    maps, graph = load_maps(), load_graph()
    ocr = SyncOcr()
    out = open(HERE / 'calibration.jsonl', 'a', encoding='utf-8')
    samples, map_id, last_header = [], None, None
    end = time.time() + seconds
    sct = getattr(mss, 'MSS', mss.mss)()
    print(f'Measuring for {seconds:.0f}s while the game is in front; looking for name tag "{name}".', flush=True)
    while time.time() < end:
        hwnd, rect = nav_live.game_client_rect()
        if not hwnd or nav_live.user32.GetForegroundWindow() != hwnd:
            time.sleep(0.3)
            continue
        gx, gy, gw, gh = rect
        # Two dot readings 0.15s apart give the movement direction.
        full0 = nav_live.grab(sct, gx, gy, gw, gh)
        time.sleep(0.15)
        full = nav_live.grab(sct, gx, gy, gw, gh)
        wells = [w for w in minimap.find_wells(full) if minimap.find_dot(full, w)]
        if not wells:
            continue
        well = max(wells, key=lambda w: w.w * w.h)
        dot0, dot = minimap.find_dot(full0, well), minimap.find_dot(full, well)
        hx0, hy0, hx1, hy1 = minimap.header_rect(well)
        header = np.ascontiguousarray(full[hy0:hy1, hx0:hx1])
        if last_header is None or header.shape != last_header.shape or np.abs(header.astype(int) - last_header).mean() > 4:
            text = ' '.join(l for l in ocr.lines(header) if len(l) >= 3 and 'mini' not in l.lower())
            score, m = find_map(text, maps, limit=1)[0]
            map_id = m['id'] if score >= 0.6 else None
            last_header = header.astype(int)
            print(f'map: "{text}" -> {map_id}', flush=True)
        geom = graph.get(map_id) if map_id else None
        if not geom or not geom.get('mm') or dot is None:
            continue
        pos = nav_live.map_position(full, well, dot, geom, gh, map_id)
        if pos is None:
            continue
        mx, my = pos
        s = gh / nav_live.VIEW_H / 2 ** geom['mm']['mag']
        vx = (dot[0] - dot0[0]) / s / 0.15 if dot0 else 0.0  # map units per second (approx. on scrolling minimaps)
        fx, fy, sw = nav_live.feet_on_screen(geom, mx, my, gw, gh)
        # Search a generous box around the prediction for the name tag.
        x0, x1 = int(max(0, fx - 160 * sw)), int(min(gw, fx + 160 * sw))
        y0, y1 = int(max(0, fy - 80 * sw)), int(min(gh, fy + 120 * sw))
        words = ocr.words(np.ascontiguousarray(full[y0:y1, x0:x1]))
        best = max(words, key=lambda w: difflib.SequenceMatcher(None, w[0].lower(), name.lower()).ratio(), default=None)
        if not best or difflib.SequenceMatcher(None, best[0].lower(), name.lower()).ratio() < 0.7:
            print(f'  ({mx:.0f},{my:.0f}) name tag not found nearby', flush=True)
            continue
        tag_cx = x0 + best[1] + best[3] / 2
        tag_top = y0 + best[2]
        sample = {'t': time.time(), 'map': map_id, 'mx': mx, 'my': my, 'vx': vx, 'sw': sw,
                  'pred_x': fx, 'pred_y': fy, 'tag_cx': tag_cx, 'tag_top': tag_top,
                  'dx_units': (tag_cx - fx) / sw, 'dy_units': (tag_top - fy) / sw}
        samples.append(sample)
        out.write(json.dumps(sample) + '\n')
        out.flush()
        move = 'right' if vx > 40 else 'left' if vx < -40 else 'still'
        print(f'  map {map_id} pos ({mx:.0f},{my:.0f}) {move:>5}: tag is {sample["dx_units"]:+.0f} units sideways, '
              f'tag top {sample["dy_units"]:+.0f} units below predicted feet', flush=True)
    if samples:
        for label, sel in (('still', lambda s: abs(s['vx']) <= 40), ('moving right', lambda s: s['vx'] > 40),
                           ('moving left', lambda s: s['vx'] < -40), ('all', lambda s: True)):
            group = [s for s in samples if sel(s)]
            if group:
                dx = np.median([s['dx_units'] for s in group])
                dy = np.median([s['dy_units'] for s in group])
                print(f'{label:>12}: n={len(group):3d}  median sideways {dx:+.1f} units, tag top {dy:+.1f} units below feet')
    else:
        print('no samples collected')


if __name__ == '__main__':
    main()
