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
STAGES = ['Spec', 'Spec review', 'Coding', 'Code review', 'Unlabelled']
ARCHETYPE_STAGES = {
    'spec-writer': 'Spec',
    'reviewer-spec': 'Spec review',
    'coder': 'Coding',
    'reviewer-code': 'Code review',
}
ROLE_STAGES = ARCHETYPE_STAGES
COORDINATION_ARCHETYPES = frozenset({'orchestrator', 'product-owner'})
COORDINATION_ROLES = COORDINATION_ARCHETYPES


def _role_name(value):
    return str(value or '').split(':', 1)[0].strip().lower()


def _stage_for_identity(value):
    name = _role_name(value)
    if name in ARCHETYPE_STAGES:
        return ARCHETYPE_STAGES[name]
    if name in COORDINATION_ARCHETYPES:
        return 'Coordination'
    return None


def _stage_for_role(value):
    name = _role_name(value)
    if name in ROLE_STAGES:
        return ROLE_STAGES[name]
    if name in COORDINATION_ROLES:
        return 'Coordination'
    return None


def classify_assignments(assignments):
    """Classify assignments from holder identity, then recorded role/link data."""
    by_id = {a['id']: a for a in assignments}
    memo = {}

    def classify(assignment, active=()):
        assignment_id = assignment['id']
        if assignment_id in memo:
            return memo[assignment_id]
        if assignment_id in active:
            return 'Unlabelled'
        stage = _stage_for_identity(assignment.get('holderArchetype'))
        if stage is None:
            stage = _stage_for_role(assignment.get('holderRole'))
        if stage is None and assignment.get('reviewsAssignmentId'):
            producer = by_id.get(assignment['reviewsAssignmentId'])
            producer_stage = classify(producer, active + (assignment_id,)) if producer else None
            if producer_stage == 'Spec':
                stage = 'Spec review'
            elif producer_stage == 'Coding':
                stage = 'Code review'
        stage = stage or 'Unlabelled'
        memo[assignment_id] = stage
        return stage

    return {assignment_id: classify(assignment) for assignment_id, assignment in by_id.items()}


def classify_turn(turn, assignment_stages):
    """Use the turn's session archetype before assignment fallbacks."""
    stage = _stage_for_identity(turn.get('sessionArchetype'))
    if stage is None:
        stage = _stage_for_role(turn.get('roleRef'))
    return stage or assignment_stages.get(turn.get('assignmentId'), 'Unlabelled')


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
    else:
        membership = {r['id']: r['group'] for r in groups['items']}
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
    assignment_stages = classify_assignments(assignments)
    by_item = defaultdict(list)
    successors = defaultdict(list)
    for a in assignments:
        a['stage'] = assignment_stages[a['id']]
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
        if not producer or producer['stage'] not in ('Spec', 'Coding'):
            continue
        counts[a['workItemId']][producer['stage']] += 1
        if any(r['openedAt'] > v['ts'] for r in successors[producer['id']]):
            counts[a['workItemId']][a['stage']] += 1
    item_turns = defaultdict(list)
    for t in turns:
        t['stage'] = classify_turn(t, assignment_stages)
        item_turns[by_id[t['assignmentId']]['workItemId']].append(t)
    for item in items:
        wi = item['id']
        item['group'] = membership[wi]
        item['stages'] = []
        its_turns = item_turns[wi]
        item['lastActivity'] = max((t['endedAt'] or t['startedAt'] or 0 for t in its_turns), default=0)
        item['runningTurns'] = sum(t['status'] == 'running' for t in its_turns)
        item['queuedTurns'] = sum(t['status'] == 'queued' for t in its_turns)
        item['openAssignments'] = sum(a['state'] == 'open' for a in by_item[wi])
        item['coordinationTurns'] = sum(t['stage'] == 'Coordination' for t in its_turns)
        for name in STAGES:
            rows = sorted((t for t in its_turns if t['stage'] == name and t['startedAt'] is not None), key=lambda t:(t['startedAt'],t['seq']))
            gaps, excluded = [], 0
            for prev, cur in zip(rows, rows[1:]):
                if prev['endedAt'] is None or prev['status'] not in ('delivered','failed') or cur['startedAt'] < prev['endedAt']:
                    excluded += 1
                else:
                    gaps.append((cur['startedAt'] - prev['endedAt']) / 1000)
            item['stages'].append({'name':name,'returns':None if name=='Unlabelled' else counts[wi][name],
                'gaps':gaps,'turns':len(rows),'excluded':excluded,
                'assignments':sum(a['stage']==name for a in by_item[wi])})
    return {'capturedAt':dt.datetime.now(dt.timezone.utc).isoformat(), 'readMs':round((time.monotonic()-started)*1000),
        'groupSource':groups['source'],'missingIds':sorted(set(ids)-{i['id'] for i in items}),
        'stages':STAGES,'items':items,'assignmentCount':len(assignments),
        'unlabelledAssignments':sum(a['stage']=='Unlabelled' for a in assignments)}


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
