import os, sys, json, tempfile, shutil, time
from pathlib import Path
from fastapi.testclient import TestClient

root = Path(tempfile.mkdtemp(prefix='purge-test-'))
db = root / 'test.db'
art = root / 'artifacts'
art.mkdir()
os.environ['DATABASE_URL'] = 'sqlite:///' + str(db)
os.environ['ARTIFACT_DIR'] = str(art)
os.environ['SEED_DEMO'] = 'false'
os.environ['REDIS_URL'] = 'redis://localhost:6379/0'
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))

import src.services.runs as runs_mod
import src.worker as worker_mod

class FakeQueue:
    def __init__(self): self.items=[]
    def rpush(self, key, value): self.items.append(int(value))
    def blpop(self, key, timeout=0): return None
    def ping(self): return True
fake = FakeQueue()
runs_mod.queue = fake
worker_mod.queue = fake

from src.main import app
from src.config.database import Session, Base, engine
import src.models.purge

Base.metadata.create_all(engine)
from src.models.user import User
from src.utils.security import hash_password
with Session() as db:
    for role in ('admin','analyst','viewer'):
        if not db.query(User).filter_by(email=role+'@station-density.demo').first():
            db.add(User(email=role+'@station-density.demo',password_hash=hash_password('Research2026!'),role=role))
    db.commit()

from fastapi.testclient import TestClient as _TC
c = _TC(app)
def wait_purge(pid,headers=None):
    import time
    for _ in range(100):
        r=c.get(f'/purge-requests/{pid}',headers=headers or viewer).json()
        if r['state'] in ('completed','failed'):return r
        time.sleep(0.05)
    raise AssertionError('purge did not settle')
def login(email):
    r=c.post('/auth/login',json={'email':email,'password':'Research2026!'})
    assert r.status_code==200, r.text
    return {'Authorization':'Bearer '+r.json()['token']}
analyst=login('analyst@station-density.demo')
admin=login('admin@station-density.demo')
viewer=login('viewer@station-density.demo')

# make a source with 6 records: A1..A6, plus another source B1..B3
s=c.post('/sources',headers=analyst,json={'name':'Test A','filename':'a.csv','description':'x'}).json()
aid=s['id']
rows=[{'reference':f'A{i}','values':{'temperature':20+i,'humidity':50+i,'particles':10+i}} for i in range(1,7)]
r=c.post(f'/sources/{aid}/records',headers=analyst,json={'records':rows}); assert r.status_code==200,r.text
s2=c.post('/sources',headers=analyst,json={'name':'Test B','filename':'b.csv','description':'x'}).json()
bid=s2['id']
rows2=[{'reference':f'B{i}','values':{'temperature':21+i,'humidity':51+i,'particles':11+i}} for i in range(1,5)]
assert c.post(f'/sources/{bid}/records',headers=analyst,json={'records':rows2}).status_code==200

# run on source A, include A2; and a run on B
run=c.post('/runs',headers=analyst,json={'source_id':aid,'name':'Run A','parameters':{'eps':0.55,'min_samples':2}}).json()
runb=c.post('/runs',headers=analyst,json={'source_id':bid,'name':'Run B','parameters':{'eps':0.55,'min_samples':2}}).json()
worker_mod.execute(run['id']); worker_mod.execute(runb['id'])
run=c.get(f"/runs/{run['id']}",headers=viewer).json()
runb=c.get(f"/runs/{runb['id']}",headers=viewer).json()
assert run['state']=='completed', run
assert runb['state']=='completed', runb
assert (art/f"run-{run['id']}.csv").exists()
assert (art/f"run-{runb['id']}.csv").exists()

note=c.post('/notes',headers=analyst,json={'run_id':run['id'],'title':'Note A','body':'body text'}).json()
note=c.post(f"/notes/{note['id']}/approve",headers=admin,json={}).json()
assert note['state']=='approved'
# approved export works pre-purge
assert c.get(f"/notes/{note['id']}/export",headers=viewer).status_code==200

# Reject analyst? purge is writer allowed. viewer cannot.
assert c.post(f'/sources/{aid}/purge',headers=viewer,json={'references':['A2']}).status_code==403

# Purge A2
r=c.post(f'/sources/{aid}/purge',headers=analyst,json={'references':['A2']})
assert r.status_code==200, r.text
p=wait_purge(r.json()['id']); assert p['state']=='completed', p
assert p['checks']['files_absent'] is True, p['checks']

# source A remaining records exclude A2; others intact
recs=c.get(f'/sources/{aid}/records',headers=viewer).json()
refs=[x['reference'] for x in recs]
assert 'A2' not in refs and 'A1' in refs and 'A6' in refs, refs
# record_count
srcs={x['id']:x for x in c.get('/sources',headers=viewer).json()}
assert srcs[aid]['record_count']==5, srcs[aid]
assert srcs[bid]['record_count']==4

# Affected run and note blocked
assert c.get(f"/runs/{run['id']}",headers=viewer).status_code==423
assert c.get(f"/runs/{run['id']}/export",headers=viewer).status_code==423
assert c.get(f"/notes/{note['id']}/export",headers=viewer).status_code==423
assert c.post(f"/notes/{note['id']}/approve",headers=admin).status_code==423
notes=c.get('/notes',headers=viewer).json()
n=[x for x in notes if x['id']==note['id']][0]
assert n['frozen_result'] is None and n['state']=='purged', n
# unaffected run B still works
assert c.get(f"/runs/{runb['id']}",headers=viewer).status_code==200
assert c.get(f"/runs/{runb['id']}/export",headers=viewer).status_code==200
assert (art/f"run-{runb['id']}.csv").exists()

# artifact for A gone; no reference A2 in any artifact
assert not (art/f"run-{run['id']}.csv").exists()
for f in art.glob('run-*.csv'):
    assert 'A2' not in f.read_text('utf-8-sig')

# re-adding purged ref rejected; creating a run cannot resurrect it
r=c.post(f'/sources/{aid}/records',headers=analyst,json={'records':[{'reference':'A2','values':{'temperature':1,'humidity':1,'particles':1}}]})
assert r.status_code==409, (r.status_code,r.text)
r=c.post('/runs',headers=analyst,json={'source_id':aid,'name':'Run A2','parameters':{'eps':0.55,'min_samples':2}})
assert r.status_code==200, r.text
newrun=r.json()
snap=c.get(f"/runs/{newrun['id']}",headers=viewer).json()
assert all(x['reference']!='A2' for x in snap['snapshot']['records'])
worker_mod.execute(newrun['id'])
newrun=c.get(f"/runs/{newrun['id']}",headers=viewer).json()
assert newrun['state']=='completed'
assert all(x['reference']!='A2' for x in newrun['result']['rows'])

# idempotent retry: same request does not create a second purge
before=len(c.get('/purge-requests',headers=viewer).json())
r=c.post(f'/sources/{aid}/purge',headers=analyst,json={'references':['A2']})
assert r.json()['created'] is False
assert len(c.get('/purge-requests',headers=viewer).json())==before
assert c.post(f"/purge-requests/{p['id']}/retry",headers=analyst).status_code==200

# old approved notes obey purge: create+approve a second note on run B then purge B3
note2=c.post('/notes',headers=analyst,json={'run_id':runb['id'],'title':'Note B','body':'body b'}).json()
note2=c.post(f"/notes/{note2['id']}/approve",headers=admin,json={}).json()
r=c.post(f'/sources/{bid}/purge',headers=admin,json={'references':'B3'})
assert r.status_code==200, r.text
assert wait_purge(r.json()['id']).state if False else wait_purge(r.json()['id'])['state']=='completed'
assert c.get(f"/runs/{runb['id']}",headers=viewer).status_code==423
assert c.get(f"/notes/{note2['id']}/export",headers=viewer).status_code==423

# progress listing includes completed receipt with no raw values
ps=c.get('/purge-requests',headers=viewer).json()
assert all('checks' in x for x in ps)
assert len(ps)>=2
print('ALL OK', root)

# --- interruption/resume: simulate a purge stuck in processing after DB clear
from src.models.purge import PurgeRequest
from src.services import purge as purge_service
from src.config.database import Session as S2
src3=c.post('/sources',headers=analyst,json={'name':'Test C','filename':'c.csv','description':'x'}).json(); cid=src3['id']
assert c.post(f'/sources/{cid}/records',headers=analyst,json={'records':[{'reference':f'C{i}','values':{'temperature':20+i,'humidity':50+i,'particles':10+i}} for i in range(1,6)]}).status_code==200
run3=c.post('/runs',headers=analyst,json={'source_id':cid,'name':'Run C','parameters':{'eps':0.55,'min_samples':2}}).json()
worker_mod.execute(run3['id'])
with S2() as d:
    p=PurgeRequest(source_id=cid,source_key=src3['key'],references=['C3'],state='processing',requested_by='analyst@station-density.demo',created_at='x',updated_at='x',affected_run_ids=[run3['id']],affected_note_ids=[],files=[f"run-{run3['id']}.csv"],counts={},checks={},attempts=0)
    d.add(p);d.commit();pid=p.id
# Simulate crash after DB cleared, before files: manually clear DB only
from src.services.purge import _clear_database
with S2() as d:
    req=d.get(PurgeRequest,pid);_clear_database(d,req);d.commit()
assert (art/f"run-{run3['id']}.csv").exists()  # file still there -> interruption point
purge_service.resume_pending()
with S2() as d:
    req=d.get(PurgeRequest,pid);assert req.state=='completed',req.state
assert not (art/f"run-{run3['id']}.csv").exists()
# resume again does nothing (no duplicate)
purge_service.resume_pending()
with S2() as d:
    req=d.get(PurgeRequest,pid);assert req.state=='completed' and req.attempts==1,(req.state,req.attempts)
print('RESUME OK')

# --- late worker completion cannot reintroduce purged data
src4=c.post('/sources',headers=analyst,json={'name':'Test D','filename':'d.csv','description':'x'}).json(); did=src4['id']
assert c.post(f'/sources/{did}/records',headers=analyst,json={'records':[{'reference':f'D{i}','values':{'temperature':20+i,'humidity':50+i,'particles':10+i}} for i in range(1,7)]}).status_code==200
run4=c.post('/runs',headers=analyst,json={'source_id':did,'name':'Run D','parameters':{'eps':0.55,'min_samples':2}}).json()
# purge D2 while run is still queued
r=c.post(f'/sources/{did}/purge',headers=analyst,json={'references':['D2']})
assert r.status_code==200, r.text
assert wait_purge(r.json()['id'])['state']=='completed'
# late worker picks up the run
worker_mod.execute(run4['id'])
with S2() as d:
    rr=d.get(__import__('src.models.run',fromlist=['Run']).Run,run4['id'])
    assert rr.state=='failed' and rr.result is None and not (art/f"run-{run4['id']}.csv").exists(),(rr.state,rr.result)
# run not listed; D2 not in any result
assert all(x['id']!=run4['id'] for x in c.get('/runs',headers=viewer).json())
print('LATE WORKER OK')

# concurrent-ish: create run after purge repeatedly never sees D2; create/approve note blocked
for _ in range(3):
    rn=c.post('/runs',headers=analyst,json={'source_id':did,'name':'Again','parameters':{'eps':0.55,'min_samples':2}}).json()
    worker_mod.execute(rn['id'])
    got=c.get(f"/runs/{rn['id']}",headers=viewer).json()
    assert all(x['reference']!='D2' for x in got['snapshot']['records'])
    assert all(x['reference']!='D2' for x in got['result']['rows'])
print('NO RESURRECTION OK')

# --- fence-before-clear: while a purge is only accepted (not yet cleared),
# reads/writes of affected material must already be blocked
from src.services import purge as ps
from src.models.source import Source as Src
srcE=c.post('/sources',headers=analyst,json={'name':'Test E','filename':'e.csv','description':'x'}).json(); eid=srcE['id']
assert c.post(f'/sources/{eid}/records',headers=analyst,json={'records':[{'reference':f'E{i}','values':{'temperature':20+i,'humidity':50+i,'particles':10+i}} for i in range(1,6)]}).status_code==200
runE=c.post('/runs',headers=analyst,json={'source_id':eid,'name':'Run E','parameters':{'eps':0.55,'min_samples':2}}).json()
worker_mod.execute(runE['id'])
noteE=c.post('/notes',headers=analyst,json={'run_id':runE['id'],'title':'Note E','body':'body e'}).json()
with S2() as d:
    src=d.get(Src,eid)
    pe,created=ps.start_purge(d,type('U',(),{'email':'analyst@x'})(),src,['E4'])
    peid=pe.id
    # do NOT process yet: simulate accepted state
    st=pe.state
assert st=='accepted',st
# immediately blocked
assert c.get(f'/runs/{runE["id"]}',headers=viewer).status_code==423
assert c.get(f'/runs/{runE["id"]}/export',headers=viewer).status_code==423
assert c.post(f'/notes/{noteE["id"]}/approve',headers=admin).status_code==423
# new run freezes without E4 even though row still exists
rn=c.post('/runs',headers=analyst,json={'source_id':eid,'name':'E after fence','parameters':{'eps':0.55,'min_samples':2}}).json()
got=c.get(f'/runs/{rn["id"]}',headers=viewer).json()
assert all(x['reference']!='E4' for x in got['snapshot']['records']),got['snapshot']['records']
# source listing hides E4 already
assert all(r['reference']!='E4' for r in c.get(f'/sources/{eid}/records',headers=viewer).json())
# now clear
ps.process_purge(peid)
with S2() as d:
    assert d.get(PurgeRequest,peid).state=='completed'
print('FENCE BEFORE CLEAR OK')

# --- same reference name in an unrelated source is untouched by a purge
srcF=c.post('/sources',headers=analyst,json={'name':'Test F','filename':'f.csv','description':'x'}).json(); fid=srcF['id']
srcG=c.post('/sources',headers=analyst,json={'name':'Test G','filename':'g.csv','description':'x'}).json(); gid=srcG['id']
assert c.post(f'/sources/{fid}/records',headers=analyst,json={'records':[{'reference':'DUP','values':{'temperature':20,'humidity':50,'particles':10}},{'reference':'F2','values':{'temperature':25,'humidity':55,'particles':15}},{'reference':'F3','values':{'temperature':30,'humidity':60,'particles':20}}]}).status_code==200
assert c.post(f'/sources/{gid}/records',headers=analyst,json={'records':[{'reference':'DUP','values':{'temperature':21,'humidity':51,'particles':11}},{'reference':'G2','values':{'temperature':26,'humidity':56,'particles':16}},{'reference':'G3','values':{'temperature':31,'humidity':61,'particles':21}}]}).status_code==200
runF=c.post('/runs',headers=analyst,json={'source_id':fid,'name':'Run F','parameters':{'eps':0.55,'min_samples':2}}).json()
runG=c.post('/runs',headers=analyst,json={'source_id':gid,'name':'Run G','parameters':{'eps':0.55,'min_samples':2}}).json()
worker_mod.execute(runF['id']); worker_mod.execute(runG['id'])
rp=c.post(f'/sources/{fid}/purge',headers=analyst,json={'references':['DUP']})
assert wait_purge(rp.json()['id'])['state']=='completed'
assert c.get(f"/runs/{runF['id']}",headers=viewer).status_code==423
assert c.get(f"/runs/{runG['id']}",headers=viewer).status_code==200
assert (art/f"run-{runG['id']}.csv").exists()
assert 'DUP' in [r['reference'] for r in c.get(f'/sources/{gid}/records',headers=viewer).json()]
# cannot re-add DUP to F, but may add to G's other refs
assert c.post(f'/sources/{fid}/records',headers=analyst,json={'records':[{'reference':'DUP','values':{'temperature':1,'humidity':1,'particles':1}}]}).status_code==409
print('SOURCE SCOPING OK')
