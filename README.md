# MSCW Navigator

An overlay for MapleStory Classic World. It shows the way to any map, NPC or monster, guides you through
quests, tracks EXP and mesos, and shows item info when you right-click an item.

It only reads what's on your screen and the game's own data files. It never touches the game's memory,
and it never presses keys or clicks for you.

## What it does

- **Search** (top of the game window) for a map, NPC, monster, quest or item. An arrow shows the way, portal by
  portal. Boats are included (Shanks off Maple Island, Pason and Pison for Florina Beach), and taxis (the Regular Cab,
  Phil in Lith Harbor) and return scrolls if you turn them on. A **Stop** button ends the navigation.
- **Quest helper.** It guides a quest step by step: who gives it, where to hunt or gather what it needs, and where
  to hand it in.
  - It follows your counts in the game's Quest Helper box. A **Guide** button sits next to each quest there.
  - Right-click a quest in your quest book to open its guide.
  - Quests that come from breaking things (Pio's crates, Riel's coconuts, Wing's herbs) mark every spawn on screen
    and on the minimap.
- **Leveling routes**, summarised from TokiLee's [Maple Classic Guide](https://mapleclassic.guide/leveling).
  - **Search → Leveling** starts one from any stage. It moves you from quest to quest and hunting spot to hunting
    spot, and remembers each character's place.
  - At level 10 it asks which job you want, takes you to the instructor (by Phil's taxi from Lith Harbor), then lets
    you pick Route A or B.
- **Item info.** Right-click an item to see:
  - where it drops, with the best spot to hunt it;
  - crafting recipes;
  - its vendor price, Free Market price and player shops (from [MesoWatch](https://meso.watch/), with their
    permission);
  - whether you can use it.
- **On the map:** hidden street entrances, teleporters and quest spawns are marked on screen and on the minimap.
- **Kerning PQ:** on the first stage, a panel lists Cloto's questions with their coupon counts, and highlights the
  one she asked you.
- **EXP / meso tracker** (optional): gains per hour.
- The panels fade while you move (optional), and the overlay can be shown or hidden on stream and recordings.

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

- **To quit:** click **Search** (top of the game window), then **Quit**.
- **To uninstall:** delete the desktop shortcut and the folder `%LOCALAPPDATA%\MSCW-Navigator`.

**Credits:**
- Free Market prices and the player shops selling an item come from [MesoWatch](https://meso.watch/), used with
  their permission. An item is looked up only when you open its info panel, and results are cached for a few hours.
- The leveling routes are summarised in our own words from TokiLee's
  [Maple Classic Guide](https://mapleclassic.guide/leveling). If it helps you, consider
  [supporting the guide](https://ko-fi.com/tokilee).

**Needs:** Windows 10 or 11 with English text recognition (it comes with English Windows), and MapleStory
Classic World installed. Using it is your call: it's a third-party program running next to the game.

## For the maintainer

- **Publishing changes.** Installed copies run whatever is on the `main` branch, from their next start.
  Commit and push when a change is ready.
- **Your own copy.** `Start Navigator.bat` runs your working folder, with your own Python and a console
  window.
- **What stays out of the repository** (see `.gitignore`): built game data, settings, logs, `debug/` captures, and
  the Kerning PQ question log. Each installed copy builds its own game data from its PC's game files. It rebuilds
  when the game is patched or the code that reads the data (`navgraph.py` and friends) changes.
- **Settings are per character.** `nav_prefs.json` keeps each character's leveling-route place under `characters`,
  keyed by the name on the status bar. Whether a quest is done is deliberately not tracked; the route's **Skip**
  button covers quests done outside it.
- **Kerning PQ answers.** Cloto's questions come from the server, not the game files. Every question the navigator
  reads goes into `kpq_questions.txt`, with a picture in `kpq_dialogs/`. Check new ones against `KPQ_QUESTIONS` in
  `nav_live.py`, and mark an answer confirmed once she has been seen asking it.
- **Hand-filled game facts** (the server decides these, so they aren't in the game files):
  - boats and Phil's taxi: `travel.py`;
  - which items come from crates, coconuts, herbs and flowers: `gathering.py`;
  - the job instructors: `routeguide.py`;
  - Cloto's questions: `nav_live.py`.
- **The bundled Python.** It's the `runtime-N` release's `runtime.zip`, built by `build_runtime.py`: the
  official embeddable Python 3.11.9, plus Tcl/Tk and the packages in `requirements.txt`.
  - It only needs rebuilding when `requirements.txt` changes.
  - When it does, raise the number in `runtime.txt`, run `py -3.11 build_runtime.py`, upload the zip with
    `gh release create runtime-N build\runtime.zip`, and push.
  - Installed copies download the new runtime once.
