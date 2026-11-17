from sqlalchemy import create_engine,inspect,text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .settings import DATABASE_URL
class Base(DeclarativeBase): pass
engine=create_engine(DATABASE_URL,pool_pre_ping=True)
Session=sessionmaker(bind=engine,expire_on_commit=False)
def db_session():
    with Session() as db: yield db

# create_all only adds missing tables/columns for new databases.  Existing demo
# volumes still need the purge bookkeeping columns and a nullable frozen
# snapshot so a cleared note no longer has to retain its old values.
_ADDED_COLUMNS={
    'density_runs':{'purged':'BOOLEAN DEFAULT FALSE NOT NULL','purge_id':'INTEGER'},
    'visit_plans':{'purged':'BOOLEAN DEFAULT FALSE NOT NULL','purge_id':'INTEGER'},
}
def ensure_schema():
    insp=inspect(engine)
    existing=set(insp.get_table_names())
    with engine.begin() as conn:
        for table,columns in _ADDED_COLUMNS.items():
            if table not in existing:continue
            present={c['name'] for c in insp.get_columns(table)}
            for name,ddl in columns.items():
                if name not in present:
                    conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))
        if engine.dialect.name=='postgresql' and 'visit_plans' in existing:
            conn.execute(text('ALTER TABLE visit_plans ALTER COLUMN frozen_result DROP NOT NULL'))
