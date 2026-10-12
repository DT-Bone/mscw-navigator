"""Synchronous client for the persistent Windows OCR process (ocr.ps1 -Server)."""
import os
import pathlib
import subprocess
import tempfile

from PIL import Image

OCR_PS1 = pathlib.Path(__file__).with_name('ocr.ps1')


class SyncOcr:
    def __init__(self):
        self.proc = subprocess.Popen(
            ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(OCR_PS1), '-Server'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding='utf-8',
            creationflags=subprocess.CREATE_NO_WINDOW)
        self.path = os.path.join(tempfile.gettempdir(), f'mscw_ocr_{os.getpid()}_{id(self)}.png')

    def lines(self, image):
        return self._ask(image, '')

    def words(self, image):
        """[(text, x, y, w, h)] in the image's pixel coordinates."""
        out = []
        for line in self._ask(image, 'W:'):
            parts = line.split('\t')
            if len(parts) == 5 and not parts[0].startswith('!'):
                out.append((parts[0], *map(float, parts[1:])))
        return out

    def _ask(self, image, prefix):
        if image.shape[0] < 4 or image.shape[1] < 4:
            return []
        Image.fromarray(image).save(self.path)
        self.proc.stdin.write(prefix + self.path + '\n')
        self.proc.stdin.flush()
        out = []
        while (line := self.proc.stdout.readline()) and line.strip() != '<<END>>':
            out.append(line.rstrip('\n'))
        return out
