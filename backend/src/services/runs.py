from sqlalchemy import select
from fastapi import HTTPException
from redis import Redis
from ..config.settings import REDIS_URL
from ..models.run import Run
from ..utils.serialization import row,now
from .sources import source_or_404,list_records,lock_source
from .domain_validation import validate_parameters
from .audit import log
from .purge import purged_reference_set
queue=Redis.from_url(REDIS_URL)
def get_run(db,id):
    r=db.get(Run,id)
    if not r:raise HTTPException(404,'未找到分析任务')
    if r.purged:raise HTTPException(423,'该研究已被彻底清除，输入与结果不可读取或导出')
    return r
def get_run_for_update(db,id):
    r=db.scalar(select(Run).where(Run.id==id).with_for_update())
    if not r:raise HTTPException(404,'未找到分析任务')
    if r.purged:raise HTTPException(423,'该研究已被彻底清除，输入与结果不可读取或导出')
    return r
def create_run(db,user,data):
    s=source_or_404(db,data.source_id)
    lock_source(db,s.id)
    purged=purged_reference_set(db,s.id)
    records=[r for r in list_records(db,s.id) if r['reference'] not in purged]
    if len(records)<3:raise HTTPException(422,'至少录入3条样本后才能执行')
    try: params=validate_parameters(data.parameters)
    except ValueError as e:raise HTTPException(422,str(e))
    r=Run(source_id=s.id,name=data.name,parameters=params,snapshot={'source':row(s),'records':records},created_at=now(),state='queued',purged=False)
    db.add(r);db.flush();log(db,user,'run.queued',r.id,data.name);db.commit()
    try:queue.rpush('analysis:queue',r.id)
    except Exception:
        r.state='failed';r.error='队列暂不可用，请重新创建任务';db.commit();raise HTTPException(503,r.error)
    return row(r)
def list_runs(db):
    return [{k:v for k,v in row(r).items() if k not in ('snapshot','result')} for r in db.scalars(select(Run).where(Run.purged==False).order_by(Run.id.desc()))]
