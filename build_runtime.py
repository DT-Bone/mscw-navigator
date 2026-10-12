"""Builds runtime.zip: the private Python that installed copies of the navigator run on.

    py -3.11 build_runtime.py        -> build\\runtime.zip

It's python.org's embeddable Python 3.11.9 (the official, signed python.exe), plus tkinter from
python.org's matching Tcl/Tk package (the embeddable one leaves it out) and the packages in
requirements.txt. Installed copies unpack it into their runtime\\ folder.

Rebuild it only when requirements.txt changes. Then raise the number in runtime.txt, and upload the zip
as a GitHub release named after it:
    gh release create runtime-2 build\\runtime.zip --title "Python runtime 2" --notes "Packages for MSCW Navigator"
Installed copies notice the new runtime.txt and download it once.
"""
import pathlib
import shutil
import subprocess
import sys
import urllib.request
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
BUILD = HERE / 'build'
EMBED_URL = 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip'
TCLTK_URL = 'https://www.python.org/ftp/python/3.11.9/amd64/tcltk.msi'
# Where Python looks for modules: its standard library, tkinter (Lib), the packages, and the folder above
# (the navigator's code; an embeddable Python doesn't add the script's own folder by itself).
PTH = 'python311.zip\n.\nLib\nLib\\site-packages\n..\n'


def download(url):
    path = BUILD / url.rsplit('/', 1)[1]
    if not path.exists():
        print('downloading', url)
        urllib.request.urlretrieve(url, path)
    return path


def main():
    out = BUILD / 'runtime'
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    zipfile.ZipFile(download(EMBED_URL)).extractall(out)

    # unpack the Tcl/Tk installer package (an "administrative install" only extracts it; no admin needed)
    base = BUILD / 'tcltk'
    shutil.rmtree(base, ignore_errors=True)
    subprocess.run(['msiexec', '/a', str(download(TCLTK_URL)), '/qn', f'TARGETDIR={base}'], check=True)
    for name in ('_tkinter.pyd', 'tcl86t.dll', 'tk86t.dll'):
        shutil.copy2(base / 'DLLs' / name, out / name)
    shutil.copytree(base / 'Lib' / 'tkinter', out / 'Lib' / 'tkinter', ignore=shutil.ignore_patterns('test', '__pycache__'))
    # Tcl/Tk's own script libraries, found by _tkinter at <prefix>\tcl\tcl8.6
    for lib in ('tcl8.6', 'tk8.6'):
        shutil.copytree(base / 'tcl' / lib, out / 'tcl' / lib, ignore=shutil.ignore_patterns('demos', 'images'))

    subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-compile',
                    '--target', str(out / 'Lib' / 'site-packages'), '--only-binary=:all:', '--platform', 'win_amd64',
                    '--python-version', '3.11', '--implementation', 'cp', '-r', str(HERE / 'requirements.txt')],
                   check=True)
    (out / 'python311._pth').write_text(PTH, encoding='ascii')
    tag = (HERE / 'runtime.txt').read_text(encoding='utf-8-sig').strip()
    (out / 'runtime.txt').write_text(tag + '\n', encoding='utf-8')

    target = BUILD / 'runtime.zip'
    target.unlink(missing_ok=True)
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(out.rglob('*')):
            if f.is_file():
                z.write(f, f.relative_to(out))
    print(f'{target} ({target.stat().st_size / 1e6:.1f} MB), {tag}')


if __name__ == '__main__':
    main()
