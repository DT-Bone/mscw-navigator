"""Where MapleStory Classic World keeps its game data: the Data folder of .wz files.

The Nexon launcher's default location is used unless game_path.txt (next to this file) names another
one. The launcher (launcher.ps1) writes that file when the game is installed somewhere else.
"""
import pathlib

DEFAULT = pathlib.Path(r'C:\Nexon\Library\maplestorycw\appdata\Data')
CONFIG = pathlib.Path(__file__).with_name('game_path.txt')


def _data_dir():
    if CONFIG.exists():
        custom = pathlib.Path(CONFIG.read_text(encoding='utf-8-sig').strip())
        if (custom / 'String' / 'String_000.wz').exists():
            return custom
    return DEFAULT


DATA = _data_dir()
