import json,time,logging,os
from redis import Redis
from sqlalchemy import select
from .config.database import Session
from .config.settings import REDIS_URL,ARTIFACT_DIR
from .models.run import Run
from .models import source,record,user,note,audit,purge
from .services.algorithm import calculate
from .services.audit import log
from .services.export import csv_response
queue=Redis.from_url(REDIS_URL)
ARTIFACT_DIR.mkdir(parents=True,exist_ok=True)
def artifact_for(id):return ARTIFACT_DIR/f'run-{id}.csv'
def remove_artifact(id):
    try:artifact_for(id).unlink(missing_ok=True)
    except OSError:logging.exception('could not remove artifact for run %s',id)
def execute(id):
    with Session() as db:
        r=db.get(Run,id)
        if not r or r.state!='queued':return
        if r.purged:
            r.state='failed';r.error='该研究的输入已被彻底清除，任务已终止';db.commit()
            log(db,'worker','run.purged',id,r.error);db.commit();remove_artifact(id);return
        r.state='running';db.commit()
        snapshot=r.snapshot;parameters=r.parameters
    try:
        result=calculate(snapshot['records'],parameters)
        json.dumps(result,allow_nan=False)
    except Exception as e:
        logging.exception('analysis failed')
        with Session() as db:
            r=db.scalar(select(Run).where(Run.id==id).with_for_update())
            if r is None:return
            if r.purged:remove_artifact(id);return
            if r.state!='running':return
            r.state='failed';r.error=str(e)[:2000]
            log(db,'worker','run.failed',r.id,r.error);db.commit()
        return
    with Session() as db:
        r=db.scalar(select(Run).where(Run.id==id).with_for_update())
        if r is None:remove_artifact(id);return
        if r.purged:
            remove_artifact(id)
            if r.result is not None or r.state in ('queued','running'):
                r.result=None
                if r.state!='failed':
                    r.state='failed';r.error='该研究的输入已被彻底清除，任务已终止'
                    log(db,'worker','run.purged',r.id,r.error)
            db.commit();return
        if r.state!='running':
            db.commit();return
        response=csv_response(result,'run.csv')
        tmp=ARTIFACT_DIR/f'run-{r.id}.csv.tmp'
        try:
            tmp.write_bytes(response.body)
            os.replace(tmp,ARTIFACT_DIR/f'run-{r.id}.csv')
        finally:
            try:tmp.unlink(missing_ok=True)
            except OSError:pass
        r.result=result;r.state='completed'
        log(db,'worker','run.completed',r.id)
        db.commit()
def main():
    while True:
        (ARTIFACT_DIR/'heartbeat').write_text(str(time.time()))
        value=queue.blpop('analysis:queue',timeout=2)
        if value:execute(int(value[1]))
if __name__=='__main__':main()
