from fastapi import APIRouter,BackgroundTasks,Depends
from sqlalchemy import select
from ..config.database import db_session
from ..models.purge import PurgeRequest
from ..schemas.purge import PurgeInput
from ..services.auth import current_user,writer
from ..services.purge import collect_references,resolve_source,start_purge,retry_purge,list_purges,get_purge,process_purge
from ..utils.serialization import row
router=APIRouter()

def _respond(background,purge,created=False):
    # Only requests that still need clearing are queued; completed or
    # processing ones are simply reported, so retries never redo the work.
    if purge.state in ('accepted','processing','failed'):
        background.add_task(process_purge,purge.id)
    body=row(purge)
    body['created']=created
    return body

@router.post('/sources/{id}/purge')
def purge_source(id:int,data:PurgeInput,background:BackgroundTasks,db=Depends(db_session),u=Depends(writer)):
    source=resolve_source(db,source_id=id)
    purge,created=start_purge(db,u,source,collect_references(data))
    return _respond(background,purge,created)

@router.post('/sources/{id}/records/purge')
def purge_source_records(id:int,data:PurgeInput,background:BackgroundTasks,db=Depends(db_session),u=Depends(writer)):
    return purge_source(id,data,background,db,u)

@router.post('/purge-requests')
def create_purge(data:PurgeInput,background:BackgroundTasks,db=Depends(db_session),u=Depends(writer)):
    source=resolve_source(db,data.source_id,data.source)
    purge,created=start_purge(db,u,source,collect_references(data))
    return _respond(background,purge,created)

@router.get('/purge-requests')
def purge_requests(db=Depends(db_session),u=Depends(current_user)):
    return list_purges(db)

@router.get('/purge-requests/{id}')
def purge_request(id:int,db=Depends(db_session),u=Depends(current_user)):
    return get_purge(db,id)

@router.post('/purge-requests/{id}/retry')
def purge_retry(id:int,background:BackgroundTasks,db=Depends(db_session),u=Depends(writer)):
    return _respond(background,retry_purge(db,id))

@router.get('/sources/{id}/purges')
def source_purges(id:int,db=Depends(db_session),u=Depends(current_user)):
    return [row(p) for p in db.scalars(select(PurgeRequest).where(PurgeRequest.source_id==id).order_by(PurgeRequest.id.desc()))]
