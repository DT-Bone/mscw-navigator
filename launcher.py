"""Starts an installed copy of MSCW Navigator: brings it up to date, keeps its game data current, starts it.

"MSCW Navigator.bat" runs this with the copy's own Python (runtime\\pythonw.exe, so no console window);
a small window shows what it's doing:
  1. if the navigator is already running, say so and stop;
  2. download the latest code from GitHub if it changed, and a new runtime if runtime.txt asks for one
     (the .bat swaps that in and starts this again, since the current one is in use);
  3. find the game (game_path.txt, the usual places, or ask for its folder);
  4. rebuild the game data if the game or the code that reads it changed;
  5. put a shortcut on the desktop (first run), then start the navigator as administrator: MapleStory
     runs as administrator, and Windows hides mouse clicks from programs that don't.

    pythonw launcher.py --test     no update check, no shortcut, doesn't start the navigator
"""
import ctypes
import ctypes.wintypes as wt
import hashlib
import io
import json
import pathlib
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.filedialog
import tkinter.messagebox
import traceback
import urllib.request
import zipfile

REPO, BRANCH = 'DT-Bone/mscw-navigator', 'main'  # where updates come from
APP = pathlib.Path(__file__).resolve().parent
PYTHON = pathlib.Path(sys.executable).with_name('python.exe')  # this copy's own Python (runtime\)
INSTANCE_MUTEX = 'Local\\MSCW-Navigator'  # held by a running navigator (nav_live.py)
# The code that reads the game's files: when it changes, the game data is rebuilt.
READERS = ('navgraph.py', 'maps.py', 'mobs.py', 'npcs.py', 'quests.py', 'items.py', 'crafting.py', 'travel.py',
           'wzreader.py', 'canvas.py', 'gamepath.py')
BUILT = ('portals.json', 'mobs.json', 'npcs.json', 'quests.json', 'items.json', 'item_icons.npz', 'crafting.json',
         'travel.json')
GAME_PLACES = ('Nexon\\Library', 'Program Files\\Nexon\\Library', 'Program Files (x86)\\Nexon\\Library',
               'Games\\Nexon\\Library')
BG, FG, DIM, ACCENT = '#1d2433', '#ffffff', '#8b95a8', '#f2b134'  # the overlay's colours
NO_WINDOW = subprocess.CREATE_NO_WINDOW


def read(path):
    try:
        return path.read_text(encoding='utf-8-sig').strip()
    except OSError:
        return ''


def fetch(url, progress=None):
    """The bytes at url. progress(fraction) is called while a big download comes in."""
    request = urllib.request.Request(url, headers={'User-Agent': 'mscw-navigator'})
    with urllib.request.urlopen(request, timeout=30) as r:
        size, chunks, got = int(r.headers.get('Content-Length') or 0), [], 0
        while chunk := r.read(1 << 18):
            chunks.append(chunk)
            got += len(chunk)
            if progress and size:
                progress(got / size)
    return b''.join(chunks)


def navigator_running():
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenMutexW.restype = wt.HANDLE
    kernel32.OpenMutexW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
    handle = kernel32.OpenMutexW(0x00100000, False, INSTANCE_MUTEX)  # SYNCHRONIZE
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return ctypes.get_last_error() == 5  # access denied: it's there, held by the navigator running as admin


def game_data_dir(folder):
    """The Data folder of a MapleStory Classic World install, given its folder (maplestorycw), its appdata
    folder or the Data folder itself; None if it isn't one."""
    if not folder:
        return None
    folder = pathlib.Path(folder)
    for d in (folder, folder / 'Data', folder / 'appdata' / 'Data'):
        if (d / 'String' / 'String_000.wz').exists():
            return d
    return None


def fixed_drives():
    kernel32 = ctypes.windll.kernel32
    mask = kernel32.GetLogicalDrives()
    roots = [f'{chr(65 + i)}:\\' for i in range(26) if mask >> i & 1]
    return [r for r in roots if kernel32.GetDriveTypeW(r) == 3]  # DRIVE_FIXED


class Launcher:
    def __init__(self, test):
        self.test = test
        self.messages, self.answers = queue.Queue(), queue.Queue()
        root = self.root = tk.Tk()
        root.title('MSCW Navigator')
        root.configure(bg=BG)
        root.resizable(False, False)
        if (APP / 'app.ico').exists():
            root.iconbitmap(str(APP / 'app.ico'))
        tk.Label(root, text='MSCW Navigator', font=('Segoe UI', 13, 'bold'), bg=BG, fg=ACCENT).pack(padx=28, pady=(18, 6))
        self.status = tk.Label(root, text='Starting...', font=('Segoe UI', 10), bg=BG, fg=FG, width=50, height=3,
                               wraplength=440, justify='center')
        self.status.pack(padx=28)
        self.detail = tk.Label(root, text='', font=('Segoe UI', 9), bg=BG, fg=DIM, width=60, wraplength=440)
        self.detail.pack(padx=28, pady=(0, 16))
        root.update_idletasks()
        w, h = root.winfo_reqwidth(), root.winfo_reqheight()
        root.geometry(f'+{(root.winfo_screenwidth() - w) // 2}+{(root.winfo_screenheight() - h) // 3}')
        threading.Thread(target=self._work, daemon=True).start()
        root.after(50, self._poll)
        root.mainloop()

    # -- talking to the window (the work runs on another thread) -------------------------------------
    def say(self, text, detail=''):
        self.messages.put(('say', text, detail))

    def _finish(self, kind, text=''):
        self.messages.put((kind, text, ''))

    def _ask_folder(self, title):
        self.messages.put(('folder', title, ''))
        return self.answers.get()

    def _poll(self):
        try:
            while True:
                kind, text, detail = self.messages.get_nowait()
                if kind == 'say':
                    self.status.config(text=text)
                    self.detail.config(text=detail)
                elif kind == 'folder':
                    self.answers.put(tkinter.filedialog.askdirectory(parent=self.root, title=text, mustexist=True))
                elif kind == 'error':
                    tkinter.messagebox.showerror('MSCW Navigator', text, parent=self.root)
                    self.root.destroy()
                    return
                elif kind == 'info':
                    tkinter.messagebox.showinfo('MSCW Navigator', text, parent=self.root)
                    self.root.destroy()
                    return
                elif kind == 'done':
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    # -- the steps -------------------------------------------------------------------------------------
    def _work(self):
        try:
            if navigator_running():
                return self._finish('info', 'MSCW Navigator is already running.\n\n'
                                            'Its Search button is at the top of the game window.')
            if not self.test and self._update():
                return self._restart()  # a new runtime was downloaded
            data = self._game_folder()
            if data is None:
                return self._finish('error', "MapleStory Classic World's game files weren't found, so the "
                                             'navigator has nothing to read its maps and quests from.')
            self._game_data(data)
            if self.test:
                return self._finish('done')
            self._shortcut()
            self.say('Starting...', 'Windows asks for permission: MapleStory runs as administrator, '
                                    'so the navigator has to as well.')
            if not self._start_navigator():
                return self._finish('error', "Windows didn't give permission, so the navigator wasn't started.\n\n"
                                             'Double-click MSCW Navigator to try again.')
            self._finish('done')
        except Exception as ex:
            with open(APP / 'launcher_log.txt', 'a', encoding='utf-8') as log:
                log.write(f'--- {time.strftime("%Y-%m-%d %H:%M:%S")}\n{traceback.format_exc()}\n')
            self._finish('error', f'Something went wrong: {ex}\n\nThe details are in {APP / "launcher_log.txt"}.')

    def _update(self):
        """Download the latest code if it changed. True if a new runtime was downloaded too."""
        self.say('Checking for updates...')
        try:
            sha = json.loads(fetch(f'https://api.github.com/repos/{REPO}/commits/{BRANCH}'))['sha']
        except (OSError, ValueError, KeyError):
            self.say("Couldn't check for updates.", 'Starting the version you have.')
            return False
        if sha != read(APP / 'version.txt'):
            self.say('Updating...')
            files = zipfile.ZipFile(io.BytesIO(fetch(f'https://codeload.github.com/{REPO}/zip/{sha}')))
            for info in files.infolist():
                rel = info.filename.split('/', 1)[1] if '/' in info.filename else ''  # GitHub's top folder
                if not rel or info.is_dir() or '..' in rel.split('/'):
                    continue
                target, content = APP / rel, files.read(info)
                if not target.exists() or target.read_bytes() != content:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
            (APP / 'version.txt').write_text(sha, encoding='utf-8')
        wanted = read(APP / 'runtime.txt')
        if not wanted or wanted == read(PYTHON.parent / 'runtime.txt'):
            return False
        self.say('Downloading an update to its Python...')
        data = fetch(f'https://github.com/{REPO}/releases/download/{wanted}/runtime.zip',
                     lambda f: self.say('Downloading an update to its Python...', f'{f:.0%}'))
        new = APP / 'runtime.new'
        shutil.rmtree(new, ignore_errors=True)
        zipfile.ZipFile(io.BytesIO(data)).extractall(new)
        return True

    def _restart(self):
        """The .bat swaps runtime.new in (once this Python has exited) and starts the launcher again."""
        self.say('Restarting...')
        subprocess.Popen(['cmd', '/c', str(APP / 'MSCW Navigator.bat')], cwd=APP, creationflags=NO_WINDOW)
        self._finish('done')

    def _game_folder(self):
        saved = APP / 'game_path.txt'
        places = [read(saved), 'C:\\Nexon\\Library\\maplestorycw'] + \
                 [f'{drive}{sub}\\maplestorycw' for drive in fixed_drives() for sub in GAME_PLACES]
        data = next((d for d in map(game_data_dir, places) if d), None)
        if data is None:
            self.say("MapleStory Classic World isn't in the usual place.",
                     'Pick its folder (called maplestorycw) in the window that opens.')
            data = game_data_dir(self._ask_folder('Select the MapleStory Classic World folder (maplestorycw)'))
        if data is not None:
            saved.write_text(str(data), encoding='utf-8')
        return data

    def _game_data(self, data):
        """Rebuild the navigator's map/monster/NPC/quest/item data from the game's files, if the game or the
        code that reads it changed since the last build."""
        stamp = hashlib.sha256()
        for name in READERS:
            stamp.update((APP / name).read_bytes())
        wz = [p.stat() for p in pathlib.Path(data).rglob('*.wz')]
        stamp.update(f'{data}|{len(wz)}|{sum(s.st_size for s in wz)}|{max((s.st_mtime_ns for s in wz), default=0)}'.encode())
        stamp = stamp.hexdigest()
        if all((APP / name).exists() for name in BUILT) and read(APP / 'data.stamp') == stamp:
            return
        heading = 'Reading the maps, quests and items from your game files.\nThis takes about a minute.'
        self.say(heading)
        proc = subprocess.Popen([str(PYTHON), '-u', 'navgraph.py', 'build'], cwd=APP, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                                creationflags=NO_WINDOW)
        lines = []
        for line in proc.stdout:
            lines.append(line.rstrip())
            self.say(heading, line.strip()[:90])
        if proc.wait() != 0:
            (APP / 'build_log.txt').write_text('\n'.join(lines), encoding='utf-8')
            raise RuntimeError('reading the game data failed:\n' + '\n'.join(lines[-4:]))
        (APP / 'data.stamp').write_text(stamp, encoding='utf-8')

    def _shortcut(self):
        """A desktop shortcut, made once (deleting it is respected)."""
        made = APP / 'shortcut.txt'
        if made.exists():
            return
        icon = APP / 'app.ico'
        if not icon.exists():
            import make_icon
            make_icon.save(icon)
        q = lambda p: "'" + str(p).replace("'", "''") + "'"
        script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut("
                  "[IO.Path]::Combine([Environment]::GetFolderPath('Desktop'), 'MSCW Navigator.lnk')); "
                  f"$s.TargetPath = {q(APP / 'MSCW Navigator.bat')}; $s.WorkingDirectory = {q(APP)}; "
                  f"$s.IconLocation = {q(icon)}; $s.WindowStyle = 7; $s.Description = 'MSCW Navigator'; $s.Save()")
        subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script],
                       creationflags=NO_WINDOW, check=True, timeout=60)
        made.write_text('The desktop shortcut was made. Delete this file to get it made again.\n', encoding='utf-8')

    def _start_navigator(self):
        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        shell32.ShellExecuteW.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_int]
        result = shell32.ShellExecuteW(None, 'runas', str(PYTHON.with_name('pythonw.exe')),
                                       f'"{APP / "nav_live.py"}"', str(APP), 1)
        return (result or 0) > 32  # 5 = the permission prompt was declined


if __name__ == '__main__':
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # sharp text on scaled screens
    except (AttributeError, OSError):
        pass
    Launcher('--test' in sys.argv)
