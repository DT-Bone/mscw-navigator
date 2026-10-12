"""Open every .wz under the game's Data folder and parse every image, reporting failures."""
import collections
import pathlib
import sys
import time

from wzreader import WzFile
import gamepath

DATA = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else gamepath.DATA

t0 = time.time()
stats = collections.Counter()
errors = []
for path in sorted(DATA.rglob('*.wz')):
    try:
        wz = WzFile(str(path))
    except Exception as ex:
        errors.append((str(path), '<file>', repr(ex)))
        stats['files_failed'] += 1
        continue
    stats['files_ok'] += 1
    stats[f'version_{wz.version}'] += 1
    for name, entry in wz.images():
        try:
            wz.read_image(entry)
            stats['images_ok'] += 1
        except Exception as ex:
            stats['images_failed'] += 1
            errors.append((str(path), name, repr(ex)))
    wz.close()

print(dict(stats))
print(f'{time.time() - t0:.1f}s')
for e in errors[:40]:
    print(*e, sep=' | ')
