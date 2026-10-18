"""Free Market prices and player-shop listings from MesoWatch (meso.watch), for the item info panel.

Used with MesoWatch's written permission (email, October 2026; Free Market prices and shop locations, for the
app as a whole). Their terms otherwise forbid automated access, so this keeps to a light footprint:
  - an item is looked up only when a player opens its info panel (nothing is fetched in the background);
  - results are cached for a few hours, and cached files are deleted after a day;
  - requests say who is asking (USER_AGENT);
  - if the site refuses (blocked, rate limited, a bot check), it's left alone for BACKOFF seconds; such
    measures are never worked around;
  - the panel credits MesoWatch and links to the item's page.

Data: data/list.json has every item's price summary (~160 KB compressed); data/markets/<market>/item/<id>.json
has one item's shop listings and recent sales. Items are keyed "basil:<8-digit item id>".
"""
import json
import pathlib
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SITE = 'https://meso.watch/'
USER_AGENT = 'MSCW-Navigator (+https://github.com/DT-Bone/mscw-navigator)'
LIST_TTL = 3 * 3600   # seconds a price summary is reused before it's fetched again
ITEM_TTL = 3600       # the same for one item's shop listings
KEEP = 24 * 3600      # cached files older than this are deleted
BACKOFF = 1800        # after a refusal, seconds before trying again
RETRY_AFTER = 120     # seconds before an item whose look-up failed is tried again
CACHE = pathlib.Path(__file__).with_name('mesowatch_cache')


def item_key(item_id):
    return f'basil:{item_id:08d}'


def item_link(item_id):
    """The item's page on meso.watch (opens with the item selected)."""
    return f'{SITE}?item={item_id:08d}'


def ago(iso, now=None):
    """'12 min ago' for an ISO time, or '' if it can't be read."""
    try:
        t = datetime.fromisoformat(str(iso).replace('Z', '+00:00'))
    except ValueError:
        return ''
    s = max(0, ((now or datetime.now(timezone.utc)) - t).total_seconds())
    if s < 3600:
        return f'{max(1, int(s // 60))} min ago'
    if s < 86400:
        return f'{int(s // 3600)} h ago'
    return f'{int(s // 86400)} d ago'


class MesoWatch:
    """Looks items up on demand (lookup), on its own thread; finished look-ups appear in `ready` (item ids),
    for the caller to pick up on its own thread."""

    def __init__(self):
        self.ready = queue.Queue()
        self.lock = threading.Lock()
        self.summaries, self.list_time, self.list_doc = {}, 0.0, None  # key -> list entry
        self.items = {}         # key -> (fetched at, item file)
        self.pending = set()    # item ids being looked up
        self.tried = {}         # item id -> when it was last looked up (a failed one isn't retried at once)
        self.refused_until = 0.0
        CACHE.mkdir(exist_ok=True)
        self._load_cache()

    # -- what the panel shows (no network) --------------------------------------------------------
    def cached(self, item_id):
        """(summary or None, item file or None, fresh) for an item, from memory only."""
        key = item_key(item_id)
        with self.lock:
            fetched, doc = self.items.get(key, (0.0, None))
            fresh = (time.time() - self.list_time < LIST_TTL and
                     (key not in self.summaries or time.time() - fetched < ITEM_TTL))
            return self.summaries.get(key), doc, fresh

    def state(self, item_id):
        """'loading' while it's being looked up; else 'ready' if a price list is loaded, 'offline' if not."""
        if item_id in self.pending:
            return 'loading'
        return 'ready' if self.list_time > 0 else 'offline'

    # -- fetching ---------------------------------------------------------------------------------
    def lookup(self, item_id):
        """Refresh what's stale for this item in the background; its id is put in `ready` when done."""
        now = time.time()
        if item_id in self.pending or now < self.refused_until or now - self.tried.get(item_id, 0) < RETRY_AFTER:
            return
        self.pending.add(item_id)
        self.tried[item_id] = now
        threading.Thread(target=self._lookup, args=(item_id,), daemon=True).start()

    def _lookup(self, item_id):
        try:
            if time.time() - self.list_time >= LIST_TTL:
                doc = self._get('data/list.json')
                if doc and isinstance(doc.get('items'), list):
                    with self.lock:
                        self.summaries = {it['id']: it for it in doc['items'] if isinstance(it, dict) and 'id' in it}
                        self.list_time, self.list_doc = time.time(), doc
                    self._save('list.json', doc)
            key = item_key(item_id)
            with self.lock:
                market = (self.list_doc or {}).get('market')
                stale = key in self.summaries and time.time() - self.items.get(key, (0.0, None))[0] >= ITEM_TTL
            if market and stale:
                name = ''.join(c if c.isalnum() else '_' for c in key) + '.json'
                doc = self._get(f'data/markets/{urllib.parse.quote(market)}/item/{name}')
                if doc is not None:
                    with self.lock:
                        self.items[key] = (time.time(), doc)
                    self._save(f'item_{name}', doc)
        except Exception as ex:  # a failed look-up just shows no market data
            print(f'[mesowatch] lookup failed: {ex!r}')
        finally:
            self.pending.discard(item_id)
            self.ready.put(item_id)

    def _get(self, path):
        """JSON from meso.watch, or None (also on refusal, which pauses look-ups for BACKOFF)."""
        request = urllib.request.Request(SITE + path, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=20) as r:
                return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as ex:
            if ex.code in (401, 403, 429, 503):  # blocked, rate limited or a bot check: leave the site alone
                self.refused_until = time.time() + BACKOFF
                print(f'[mesowatch] meso.watch refused ({ex.code}); not asking again for {BACKOFF // 60} min')
            return None
        except (OSError, ValueError) as ex:  # offline, timed out, or not JSON (e.g. a challenge page)
            print(f'[mesowatch] {path}: {ex!r}')
            return None

    # -- the cache on disk (so a restart doesn't fetch again) ----------------------------------------
    def _save(self, name, doc):
        try:
            (CACHE / name).write_text(json.dumps(doc), encoding='utf-8')
        except OSError:
            pass

    def _load_cache(self):
        now = time.time()
        for f in CACHE.glob('*.json'):
            try:
                age = now - f.stat().st_mtime
                if age > KEEP:
                    f.unlink()
                    continue
                doc = json.loads(f.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if f.name == 'list.json' and isinstance(doc.get('items'), list):
                self.summaries = {it['id']: it for it in doc['items'] if isinstance(it, dict) and 'id' in it}
                self.list_time, self.list_doc = now - age, doc
            elif f.name.startswith('item_') and doc.get('id'):
                self.items[doc['id']] = (now - age, doc)
