# MSCW Navigator

An overlay for MapleStory Classic World. It shows the way to any map, NPC or monster, guides you through
quests, tracks EXP and mesos, and shows item info when you right-click an item.

It only reads what's on your screen and the game's own data files. It never touches the game's memory,
and it never presses keys or clicks for you.

## Install

1. Get **MSCW Navigator.bat**, either from whoever shared the navigator with you or from this repository
   (right-click [this link](../../raw/main/MSCW%20Navigator.bat) → *Save link as…*). Then double-click it.
   - Windows may warn you about a downloaded file: choose **Run** (or **More info → Run anyway**).
2. The first run downloads the navigator with its own copy of Python (about 35 MB). Nothing is installed
   on Windows. It then reads the maps, quests and items from your game files, which takes about a minute.
3. Windows asks for permission to start it. MapleStory runs as administrator, so the navigator has to as
   well, or it can't see your right-clicks.

You'll then have an **MSCW Navigator** shortcut on your desktop. Start it from there. It checks for updates
every time it starts.

- **To quit:** click **Navigate** (top of the game window), then **Quit**.
- **To uninstall:** delete the desktop shortcut and the folder `%LOCALAPPDATA%\MSCW-Navigator`.

**Needs:** Windows 10 or 11 with English text recognition (it comes with English Windows), and MapleStory
Classic World installed. Using it is your call: it's a third-party program running next to the game.

## For the maintainer

- **Publishing changes.** Installed copies run whatever is on the `main` branch, from their next start.
  Commit and push when a change is ready.
- **Your own copy.** `Start Navigator.bat` runs your working folder, with your own Python and a console
  window.
- **What stays out of the repository** (see `.gitignore`): built game data, settings, logs and `debug/`
  captures. Each installed copy builds its own game data from its PC's game files. It rebuilds when the
  game is patched or the code that reads the data (`navgraph.py` and friends) changes.
- **The bundled Python.** It's the `runtime-N` release's `runtime.zip`, built by `build_runtime.py`: the
  official embeddable Python 3.11.9, plus Tcl/Tk and the packages in `requirements.txt`.
  - It only needs rebuilding when `requirements.txt` changes.
  - When it does, raise the number in `runtime.txt`, run `py -3.11 build_runtime.py`, upload the zip with
    `gh release create runtime-N build\runtime.zip`, and push.
  - Installed copies download the new runtime once.
