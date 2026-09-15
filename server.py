"""Read-only work circle view. No writes or runtime calls to Tightbeam."""
import argparse
from collections import defaultdict
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
UNKNOWN_ARCHETYPE = 'Unknown'
COORDINATION_ARCHETYPES = frozenset({'orchestrator', 'product-owner'})
RELEASE_STATUSES = ('Remaining', 'Landed', 'Standing ownership', 'Scope unresolved', 'Excluded')


def _archetype_name(value):
    value = str(value or '').strip()
    return value or UNKNOWN_ARCHETYPE


def archetype_sort_key(name):
    return (name == UNKNOWN_ARCHETYPE, name.casefold(), name)


def classify_assignments(assignments):
    """Use the holder session archetype, or an explicit Unknown marker."""
    return {a['id']: _archetype_name(a.get('holderArchetype')) for a in assignments}


def classify_turn(turn, _assignment_archetypes):
    """Use the turn's session archetype, or an explicit Unknown marker."""
    return _archetype_name(turn.get('sessionArchetype'))


def _description_record(record, source):
    """Return a safe, display-only description and its non-sensitive source."""
    value = record.get('summary')
    if value is None or not str(value).strip():
        value = record.get('description')
    if value is None or not str(value).strip():
        return 'No description recorded', 'No description recorded'
    return str(value), source


def _release_record(record):
    status = str(record.get('releaseStatus') or 'Scope unresolved').strip()
    if status not in RELEASE_STATUSES:
        status = 'Scope unresolved'
    checked_at = record.get('checkedAt')
    return status, None if checked_at is None else str(checked_at)


def read_snapshot(db_path, groups_path):
    groups_path = Path(groups_path).expanduser().resolve()
    db_path = Path(db_path).expanduser().resolve()
    groups = json.loads(groups_path.read_text())
    if groups.get('inventoryPath'):
        inventory_path = Path(groups['inventoryPath']).expanduser()
        if not inventory_path.is_absolute():
            inventory_path = groups_path.parent / inventory_path
        inventory = json.loads(inventory_path.resolve().read_text())
        membership = {r['id']: r.get('category', 'Ungrouped') for r in inventory}
        descriptions = {r['id']: _description_record(r, 'Inventory summary') for r in inventory}
        release_records = {r['id']: _release_record(r) for r in inventory}
    else:
        membership = {r['id']: r['group'] for r in groups['items']}
        descriptions = {r['id']: _description_record(r, 'Groups configuration') for r in groups['items']}
        release_records = {r['id']: _release_record(r) for r in groups['items']}
    ids = list(membership)
    if not ids:
        raise ValueError('No item IDs in the inventory')
    c = sqlite3.connect(Path(db_path).as_uri() + '?mode=ro', uri=True, timeout=1)
    c.row_factory = sqlite3.Row
    deadline = time.monotonic() + 4
    c.set_progress_handler(lambda: int(time.monotonic() > deadline), 5000)
    c.execute('PRAGMA query_only=ON')
    started = time.monotonic()
    try:
        c.execute('BEGIN')
        q = ','.join('?' for _ in ids)
        items = [dict(r) for r in c.execute(f'SELECT id,title,state,createdAt FROM work_items WHERE id IN ({q})', ids)]
        assignments = [dict(r) for r in c.execute(f'''SELECT a.id,a.workItemId,a.holderKey,a.holderRole,a.reviewsAssignmentId,a.openedAt,a.state,
                s.archetype AS holderArchetype
            FROM assignments a LEFT JOIN sessions s ON s.sessionKey=a.holderKey
            WHERE a.workItemId IN ({q})''', ids)]
        turns = [dict(r) for r in c.execute(f'''SELECT t.seq,t.assignmentId,t.sessionKey,t.roleRef,t.status,t.startedAt,t.endedAt,
                s.archetype AS sessionArchetype
            FROM assignments a JOIN turns t ON t.assignmentId=a.id
            LEFT JOIN sessions s ON s.sessionKey=t.sessionKey
            WHERE a.workItemId IN ({q})''', ids)]
        # One bounded scan: attests has no assignment index on the installed DB.
        verdicts = [dict(r) for r in c.execute(f'''SELECT v.id,v.assignmentId,v.ts FROM attests v JOIN assignments a ON a.id=v.assignmentId
            WHERE v.kind='verdict' AND v.verdictKind='changes-requested' AND a.workItemId IN ({q})''', ids)]
    finally:
        c.rollback()
        c.close()
    by_id = {a['id']: a for a in assignments}
    assignment_archetypes = classify_assignments(assignments)
    by_item = defaultdict(list)
    successors = defaultdict(list)
    for a in assignments:
        a['archetype'] = assignment_archetypes[a['id']]
        by_item[a['workItemId']].append(a)
        if a['reviewsAssignmentId']:
            successors[a['reviewsAssignmentId']].append(a)
    adverse = {}
    for v in verdicts:
        if v['assignmentId'] not in adverse or v['ts'] < adverse[v['assignmentId']]['ts']:
            adverse[v['assignmentId']] = v
    counts = defaultdict(lambda: defaultdict(int))
    for aid, v in adverse.items():
        a = by_id[aid]
        producer = by_id.get(a['reviewsAssignmentId'])
        if not producer:
            continue
        counts[a['workItemId']][producer['archetype']] += 1
        later_reviews = sorted(
            (r for r in successors[producer['id']] if r['openedAt'] > v['ts']),
            key=lambda r: (r['openedAt'], r['id']),
        )
        if later_reviews:
            counts[a['workItemId']][later_reviews[0]['archetype']] += 1
    item_turns = defaultdict(list)
    for t in turns:
        t['archetype'] = classify_turn(t, assignment_archetypes)
        item_turns[by_id[t['assignmentId']]['workItemId']].append(t)
    archetypes = sorted(
        {a['archetype'] for a in assignments}
        | {t['archetype'] for t in turns},
        key=archetype_sort_key,
    )
    for item in items:
        wi = item['id']
        item['group'] = membership[wi]
        item['description'], item['descriptionSource'] = descriptions.get(
            wi, ('No description recorded', 'No description recorded'))
        item['releaseStatus'], item['checkedAt'] = release_records.get(
            wi, ('Scope unresolved', None))
        item['stages'] = []
        its_turns = item_turns[wi]
        item['lastActivity'] = max((t['endedAt'] or t['startedAt'] or 0 for t in its_turns), default=0)
        item['runningTurns'] = sum(t['status'] == 'running' for t in its_turns)
        item['queuedTurns'] = sum(t['status'] == 'queued' for t in its_turns)
        item['openAssignments'] = sum(a['state'] == 'open' for a in by_item[wi])
        item['coordinationTurns'] = sum(t['archetype'] in COORDINATION_ARCHETYPES for t in its_turns)
        item_archetypes = sorted(
            {a['archetype'] for a in by_item[wi]}
            | {t['archetype'] for t in its_turns},
            key=archetype_sort_key,
        )
        for name in item_archetypes:
            rows = sorted((t for t in its_turns if t['archetype'] == name and t['startedAt'] is not None), key=lambda t:(t['startedAt'],t['seq']))
            gaps, excluded = [], 0
            for prev, cur in zip(rows, rows[1:]):
                if prev['endedAt'] is None or prev['status'] not in ('delivered','failed') or cur['startedAt'] < prev['endedAt']:
                    excluded += 1
                else:
                    gaps.append((cur['startedAt'] - prev['endedAt']) / 1000)
            item['stages'].append({'name':name,'archetype':name,'returns':counts[wi][name],
                'gaps':gaps,'turns':len(rows),'excluded':excluded,
                'assignments':sum(a['archetype']==name for a in by_item[wi])})
    return {'capturedAt':dt.datetime.now(dt.timezone.utc).isoformat(), 'readMs':round((time.monotonic()-started)*1000),
        'groupSource':groups['source'],'missingIds':sorted(set(ids)-{i['id'] for i in items}),
        'archetypes':archetypes,'stages':archetypes,'items':items,'assignmentCount':len(assignments),
        'unlabelledAssignments':sum(a['archetype']==UNKNOWN_ARCHETYPE for a in assignments)}


class Cache:
    def __init__(self, db, groups):
        self.db = Path(db).expanduser().resolve()
        self.groups = Path(groups).expanduser().resolve()
        self.lock = threading.Lock()
        self.body, self.at = None, 0
    def get(self):
        with self.lock:
            if self.body is None or time.monotonic()-self.at >= 10:
                snapshot = read_snapshot(self.db, self.groups)
                self.body = json.dumps(snapshot, separators=(',',':')).encode()
                self.at = time.monotonic()
            return self.body


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default=str(Path.home()/'.tightbeam'/'state.db'))
    parser.add_argument('--groups', default=str(ROOT/'groups.json'))
    parser.add_argument('--port', type=int, default=4391)
    args = parser.parse_args()
    cache = Cache(Path(args.db).expanduser().resolve(), Path(args.groups).expanduser().resolve())
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path=urlparse(self.path).path.removeprefix('/work-circles')
            try:
                if path == '/api/items':
                    body, mime = cache.get(), 'application/json'
                elif path in ('','/','/index.html'):
                    body, mime = (ROOT/'index.html').read_bytes(), 'text/html; charset=utf-8'
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type',mime)
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                body=json.dumps({'error':'Database read unavailable','detail':type(e).__name__}).encode()
                self.send_response(503)
                self.send_header('Content-Type','application/json')
                self.send_header('Cache-Control','no-store')
                self.end_headers()
                self.wfile.write(body)
                print(f'Read failed: {type(e).__name__}',flush=True)
        def log_message(self,*args):
            pass
    ThreadingHTTPServer(('127.0.0.1',args.port),Handler).serve_forever()


if __name__ == '__main__':
    main()
