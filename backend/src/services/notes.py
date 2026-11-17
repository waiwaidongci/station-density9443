from fastapi import HTTPException
from sqlalchemy import select
from ..models.note import Note
from ..utils.serialization import row
from .runs import get_run_for_update
from .audit import log
def list_notes(db):
    rows=[]
    for n in db.scalars(select(Note).order_by(Note.id.desc())):
        d=row(n)
        if n.purged:
            d['frozen_result']=None;d['title']='已清除的业务说明';d['body']=''
        rows.append(d)
    return rows
def create_note(db,user,data):
    r=get_run_for_update(db,data.run_id)
    if r.state!='completed':raise HTTPException(409,'只可为已完成结果提交说明')
    n=Note(run_id=r.id,title=data.title,body=data.body,state='draft',frozen_result=r.result,author=user.email,purged=False)
    db.add(n);db.flush();log(db,user,'note.created',n.id,n.title);db.commit();return row(n)
def approve_note(db,user,id):
    n=db.scalar(select(Note).where(Note.id==id).with_for_update())
    if not n:raise HTTPException(404,'未找到说明')
    if n.purged:raise HTTPException(423,'该说明已被彻底清除，不能批准或导出')
    if n.state!='draft':raise HTTPException(409,'该说明已经批准')
    n.state='approved';n.approved_by=user.email;log(db,user,'note.approved',n.id,n.title);db.commit();return row(n)
