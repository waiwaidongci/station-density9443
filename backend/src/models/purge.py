from sqlalchemy import Column,Integer,String,Text,JSON,ForeignKey,UniqueConstraint
from ..config.database import Base
from ..config.settings import CONFIG
class PurgeRequest(Base):
    __tablename__='purge_requests'
    id=Column(Integer,primary_key=True)
    source_id=Column(Integer,ForeignKey(CONFIG['source_table']+'.id'),nullable=False)
    source_key=Column(String(120),nullable=False)
    references=Column(JSON,nullable=False)
    state=Column(String(24),default='accepted',nullable=False)
    requested_by=Column(String(120),nullable=False)
    created_at=Column(String(40),nullable=False)
    updated_at=Column(String(40),default='')
    completed_at=Column(String(40),default='')
    affected_run_ids=Column(JSON,default=list)
    affected_note_ids=Column(JSON,default=list)
    files=Column(JSON,default=list)
    counts=Column(JSON,default=dict)
    checks=Column(JSON,default=dict)
    last_error=Column(Text,default='')
    attempts=Column(Integer,default=0)
class PurgedRecord(Base):
    __tablename__='purged_records'
    __table_args__=(UniqueConstraint('source_id','reference',name='uq_purged_record_reference'),)
    id=Column(Integer,primary_key=True)
    source_id=Column(Integer,ForeignKey(CONFIG['source_table']+'.id'),nullable=False)
    reference=Column(String(120),nullable=False)
    purge_id=Column(Integer,ForeignKey('purge_requests.id'),nullable=False)
    purged_at=Column(String(40),nullable=False)
