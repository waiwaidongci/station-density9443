import csv
import io
import json
import logging
import re
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import delete, func, select, update

from ..config.database import Session
from ..config.settings import ARTIFACT_DIR
from ..models.note import Note
from ..models.purge import PurgeRequest, PurgedRecord
from ..models.record import Record
from ..models.run import Run
from ..models.source import Source
from ..utils.serialization import now, row
from .audit import log

# Lifecycle: accepted -> processing -> completed, or failed (retryable).
#
# The accepting transaction commits tombstones and marks every affected run /
# note before a single value is removed, so reads and exports are fenced the
# moment the request is accepted.  Clearing is idempotent: retries re-run the
# same database, file and verification steps without repeating an irreversible
# action, and a receipt only moves from processing to completed once every
# copy (source rows, frozen inputs, results, note snapshots, shared files) has
# been verified absent.  Interrupting between the database commit and file
# removal is safe because unfinished receipts are resumed at startup.

RESUMABLE_STATES = ('accepted', 'processing')


def normalize_references(values):
    if values is None:
        return []
    if isinstance(values, str):
        values = re.split(r'[\s,，;；]+', values)
    if not isinstance(values, (list, tuple, set)):
        values = [values]
    out = []
    for value in values:
        if isinstance(value, dict):
            value = value.get('reference') or value.get('id')
        if value is None:
            continue
        text = str(value).strip()
        if text and text not in out:
            out.append(text)
    return out


def collect_references(data):
    raw = None
    for field in ('references', 'record_references', 'reference'):
        raw = getattr(data, field, None)
        if raw is not None:
            break
    return normalize_references(raw)


def resolve_source(db, source_id=None, source=None):
    found = None
    if source_id is not None:
        try:
            found = db.get(Source, int(source_id))
        except (TypeError, ValueError):
            found = None
    if found is None and source is not None:
        text = str(source).strip()
        found = db.scalar(select(Source).where(Source.key == text))
        if found is None and text.isdigit():
            found = db.get(Source, int(text))
    if found is None:
        raise HTTPException(404, '未找到来源批次')
    return found


def lock_source(db, source_id):
    source = db.scalar(select(Source).where(Source.id == source_id).with_for_update())
    if source is None:
        raise HTTPException(404, '未找到来源批次')
    return source


def purged_reference_set(db, source_id):
    return set(db.scalars(select(PurgedRecord.reference).where(PurgedRecord.source_id == source_id)))


def _references_of(container, key='rows'):
    if not isinstance(container, dict):
        return set()
    rows = container.get(key)
    if not isinstance(rows, list):
        return set()
    return {item.get('reference') for item in rows if isinstance(item, dict)}


def _snapshot_references(run):
    snapshot = run.snapshot or {}
    records = snapshot.get('records') if isinstance(snapshot, dict) else None
    if not isinstance(records, list):
        return set()
    return {item.get('reference') for item in records if isinstance(item, dict)}


def _result_references(run):
    return _references_of(run.result, 'rows')


def _frozen_references(note):
    return _references_of(note.frozen_result, 'rows')


def _affected_runs(db, source_id, references):
    wanted = set(references)
    affected = []
    for run in db.scalars(
        select(Run).where(Run.source_id == source_id).with_for_update()
    ).unique():
        if _snapshot_references(run) & wanted:
            affected.append(run)
    return affected


def _affected_notes(db, run_ids, references, source_run_ids):
    wanted = set(references)
    in_scope = set(run_ids) | set(source_run_ids)
    notes = []
    for note in db.scalars(select(Note).with_for_update()).unique():
        if note.run_id not in in_scope:
            continue
        # Notes on a cleared run are fenced even if their snapshot only shows
        # aggregate values; notes from other sources are never touched.
        if note.run_id in run_ids or (_frozen_references(note) & wanted):
            notes.append(note)
    return notes


def _safe_artifact_name(name):
    text = str(name).strip()
    base = Path(text).name
    if not base or base in ('.', '..') or base != text:
        return None
    return base


def _file_contains_reference(path, wanted):
    """Check the reference column only, so a short id that happens to appear
    as a substring of another value does not cause an unrelated file to be
    treated as a copy."""
    try:
        text = path.read_text('utf-8-sig', errors='ignore')
    except OSError:
        return True
    try:
        reader = csv.reader(io.StringIO(text))
        for index, line in enumerate(reader):
            if index == 0 and line and line[0] == 'reference':
                continue
            if line and line[0] in wanted:
                return True
    except csv.Error:
        return False
    return False


def _artifact_paths(db, purge):
    """Every artifact that may still hold a purged value.

    Candidate names come from the receipt, the affected run ids and every run
    of the same source (a worker can write late).  A content scan then keeps
    only files of this source that really carry a purged reference, so
    unrelated research — even if it reuses a reference name in another source
    — is left untouched.
    """
    names = set()
    for item in purge.files or []:
        safe = _safe_artifact_name(item)
        if safe:
            names.add(safe)
    source_runs = _source_run_ids(db, purge.source_id)
    affected = set(purge.affected_run_ids or [])
    names.update(f'run-{run_id}.csv' for run_id in source_runs | affected)
    names.update(f'run-{run_id}.csv.tmp' for run_id in source_runs | affected)
    found = set()
    for name in names:
        for path in ARTIFACT_DIR.glob(name):
            if path.is_file():
                found.add(path)
    wanted = set(purge.references or [])
    if wanted and ARTIFACT_DIR.exists():
        for path in ARTIFACT_DIR.iterdir():
            if not path.is_file() or path.name == 'heartbeat':
                continue
            match = re.fullmatch(r'run-(\d+)\.csv(\.tmp)?', path.name)
            if not match:
                continue
            run_id = int(match.group(1))
            if run_id not in source_runs and run_id not in affected:
                continue
            if _file_contains_reference(path, wanted):
                found.add(path)
    return sorted(found)


def _record_artifact_names(purge, paths):
    names = {path.name for path in paths}
    files = set(purge.files or []) | names
    purge.files = sorted(
        name for name in (_safe_artifact_name(item) for item in files) if name
    )
    run_ids = set(purge.affected_run_ids or [])
    for name in names:
        match = re.fullmatch(r'run-(\d+)\.csv.*', name)
        if match:
            run_ids.add(int(match.group(1)))
    purge.affected_run_ids = sorted(run_ids)


def _source_run_ids(db, source_id):
    return set(db.scalars(select(Run.id).where(Run.source_id == source_id)))


def _clear_database(db, purge):
    references = set(purge.references or [])
    source_id = purge.source_id
    record_count = db.scalar(
        select(func.count()).select_from(Record).where(
            Record.source_id == source_id, Record.reference.in_(references)
        )
    ) or 0
    db.execute(
        delete(Record).where(
            Record.source_id == source_id, Record.reference.in_(references)
        )
    )

    run_ids = set(purge.affected_run_ids or [])
    note_ids = set(purge.affected_note_ids or [])
    snapshot_entries = 0
    results_cleared = 0
    for run in db.scalars(
        select(Run).where(Run.source_id == source_id).with_for_update()
    ).unique():
        snapshot = run.snapshot or {}
        records = snapshot.get('records') if isinstance(snapshot, dict) else None
        if isinstance(records, list):
            kept = [
                item for item in records
                if not (isinstance(item, dict) and item.get('reference') in references)
            ]
            if len(kept) != len(records):
                snapshot_entries += len(records) - len(kept)
                run.snapshot = {**snapshot, 'records': kept}
                run_ids.add(run.id)
        if run.id in run_ids or (_result_references(run) & references):
            run_ids.add(run.id)
            run.purged = True
            if run.purge_id is None:
                run.purge_id = purge.id
            if run.result is not None:
                run.result = None
                results_cleared += 1

    # Notes are cleared when they belong to a cleared run of this source, or
    # when their own frozen snapshot still carries a purged reference.
    source_runs = _source_run_ids(db, source_id) | run_ids
    for note in db.scalars(select(Note).with_for_update()).unique():
        if note.run_id not in source_runs:
            continue
        if note.run_id in run_ids or (_frozen_references(note) & references):
            note_ids.add(note.id)
            note.purged = True
            note.state = 'purged'
            note.frozen_result = None
            note.body = ''
            if note.purge_id is None:
                note.purge_id = purge.id

    purge.affected_run_ids = sorted(run_ids)
    purge.affected_note_ids = sorted(note_ids)
    previous = dict(purge.counts or {})
    purge.counts = {
        'references': len(references),
        'records': max(int(previous.get('records', 0)), int(record_count)),
        'runs': len(run_ids),
        'notes': len(note_ids),
        'snapshot_entries': max(int(previous.get('snapshot_entries', 0)), snapshot_entries),
        'results': max(int(previous.get('results', 0)), results_cleared),
        'files': len(purge.files or []),
    }


def _clear_files(db, purge):
    paths = _artifact_paths(db, purge)
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logging.exception('could not remove purge artifact %s', path)
    _record_artifact_names(purge, paths)
    db.flush()


def _verify(db, purge):
    references = set(purge.references or [])
    source_id = purge.source_id
    checks = {}
    remaining = db.scalar(
        select(func.count()).select_from(Record).where(
            Record.source_id == source_id, Record.reference.in_(references)
        )
    ) or 0
    checks['source_samples_absent'] = remaining == 0
    checks['remaining_source_samples'] = int(remaining)
    snapshot_leaks = 0
    result_leaks = 0
    for run in db.scalars(select(Run).where(Run.source_id == source_id)):
        if _snapshot_references(run) & references:
            snapshot_leaks += 1
        if _result_references(run) & references:
            result_leaks += 1
    checks['frozen_inputs_absent'] = snapshot_leaks == 0
    checks['frozen_input_leaks'] = snapshot_leaks
    checks['results_absent'] = result_leaks == 0
    checks['result_leaks'] = result_leaks
    note_leaks = 0
    source_runs = _source_run_ids(db, source_id)
    for note in db.scalars(select(Note)):
        if note.run_id in source_runs and (_frozen_references(note) & references):
            note_leaks += 1
    checks['notes_absent'] = note_leaks == 0
    checks['note_leaks'] = note_leaks
    paths = _artifact_paths(db, purge)
    checks['files_absent'] = not paths
    checks['files_remaining'] = [path.name for path in paths]
    return checks


def _database_clear(checks):
    return (
        checks.get('source_samples_absent') is True
        and checks.get('frozen_inputs_absent') is True
        and checks.get('results_absent') is True
        and checks.get('notes_absent') is True
    )


def _all_clear(checks):
    return _database_clear(checks) and checks.get('files_absent') is True


def _fail(db, purge, message, checks=None):
    """Move processing -> failed, but never overwrite a concurrent completion."""
    values = {'state': 'failed', 'last_error': message, 'updated_at': now()}
    if checks is not None:
        values['checks'] = checks
    db.execute(
        update(PurgeRequest)
        .where(PurgeRequest.id == purge.id, PurgeRequest.state == 'processing')
        .values(**values)
    )
    db.commit()
    db.expire_all()
    return db.get(PurgeRequest, purge.id)


def _claim(db, purge_id):
    """Atomically move an accepted/failed request into processing.  Concurrent
    accept, retry and resume calls race here, so only the winner clears;
    everyone else observes the receipt instead of repeating the work."""
    result = db.execute(
        update(PurgeRequest)
        .where(PurgeRequest.id == purge_id, PurgeRequest.state.in_(('accepted', 'failed')))
        .values(
            state='processing',
            attempts=func.coalesce(PurgeRequest.attempts, 0) + 1,
            updated_at=now(),
            last_error='',
        )
    )
    db.commit()
    return result.rowcount == 1


def _finalize(db, purge):
    """Only flip processing -> completed; never re-complete or overwrite."""
    result = db.execute(
        update(PurgeRequest)
        .where(PurgeRequest.id == purge.id, PurgeRequest.state == 'processing')
        .values(
            state='completed',
            checks=purge.checks,
            completed_at=now(),
            updated_at=now(),
        )
    )
    db.commit()
    db.expire_all()
    return result.rowcount == 1


def process_purge(purge_id):
    """Resume/retry a purge.  Safe to call repeatedly: already cleared copies
    are re-checked, and verification alone decides completion."""
    with Session() as db:
        if not _claim(db, purge_id):
            return db.get(PurgeRequest, purge_id)
        purge = db.get(PurgeRequest, purge_id)
        try:
            db.refresh(purge)
            # Hold the source row while clearing so a concurrent run creation
            # cannot freeze samples between fencing and deletion.
            lock_source(db, purge.source_id)
            _clear_database(db, purge)
            db.commit()
            checks = _verify(db, purge)
            if not _database_clear(checks):
                return _fail(db, purge, '数据库副本未核验消失，可重试清除', checks)
            _clear_files(db, purge)
            db.commit()
            # The failed-path and completion-path must see the widest set of
            # files/runs recorded by any concurrent retry, so refresh first.
            purge = db.get(PurgeRequest, purge.id)
            db.refresh(purge)
            checks = _verify(db, purge)
            purge.checks = checks
            if not _all_clear(checks):
                return _fail(db, purge, '文件清除后仍有副本残留，可重试清除', checks)
            if not _finalize(db, purge):
                return db.get(PurgeRequest, purge_id)
            log(db, 'system', 'purge.completed', purge.id, json.dumps({
                'source': purge.source_key,
                'references': len(purge.references or []),
                'runs': len(purge.affected_run_ids or []),
                'notes': len(purge.affected_note_ids or []),
                'files': len(purge.files or []),
            }, ensure_ascii=False))
            db.commit()
            return db.get(PurgeRequest, purge_id)
        except Exception as exc:
            db.rollback()
            purge = db.get(PurgeRequest, purge_id)
            if purge is not None and purge.state != 'completed':
                purge.state = 'failed'
                purge.last_error = str(exc)[:2000]
                purge.updated_at = now()
                db.commit()
            logging.exception('purge %s failed', purge_id)
            return purge


def _existing(db, source_id, references):
    wanted = set(references)
    for purge in db.scalars(
        select(PurgeRequest).where(PurgeRequest.source_id == source_id).order_by(PurgeRequest.id.desc())
    ):
        if wanted <= set(purge.references or []):
            return purge
    return None


def start_purge(db, user, source, references):
    refs = normalize_references(references)
    if not refs:
        raise HTTPException(422, '至少指定一个需要清除的样本编号')
    if len(refs) > 1000:
        raise HTTPException(422, '单次最多清除1000个样本编号')
    source = lock_source(db, source.id)
    existing = _existing(db, source.id, refs)
    if existing is not None:
        db.commit()
        db.expire_all()
        return db.get(PurgeRequest, existing.id), False

    purge = PurgeRequest(
        source_id=source.id,
        source_key=source.key,
        references=refs,
        state='accepted',
        requested_by=user.email,
        created_at=now(),
        updated_at=now(),
        affected_run_ids=[],
        affected_note_ids=[],
        files=[],
        counts={},
        checks={},
        attempts=0,
    )
    db.add(purge)
    db.flush()

    known = purged_reference_set(db, source.id)
    for reference in refs:
        if reference not in known:
            db.add(PurgedRecord(
                source_id=source.id,
                reference=reference,
                purge_id=purge.id,
                purged_at=now(),
            ))

    # Fence first, clear later.  The commit below makes the purge visible to
    # every read path before any value is removed.
    runs = _affected_runs(db, source.id, refs)
    run_ids = sorted({run.id for run in runs})
    for run in runs:
        run.purged = True
        if run.purge_id is None:
            run.purge_id = purge.id
    notes = _affected_notes(db, set(run_ids), refs, _source_run_ids(db, source.id))
    note_ids = sorted({note.id for note in notes})
    for note in notes:
        note.purged = True
        note.state = 'purged'
        if note.purge_id is None:
            note.purge_id = purge.id

    record_count = db.scalar(
        select(func.count()).select_from(Record).where(
            Record.source_id == source.id, Record.reference.in_(refs)
        )
    ) or 0
    purge.affected_run_ids = run_ids
    purge.affected_note_ids = note_ids
    purge.files = [f'run-{run_id}.csv' for run_id in run_ids]
    purge.counts = {
        'references': len(refs),
        'records': int(record_count),
        'runs': len(run_ids),
        'notes': len(note_ids),
        'files': len(purge.files),
    }
    purge.checks = {}
    db.flush()
    log(db, user, 'purge.accepted', purge.id, json.dumps({
        'source': source.key,
        'references': len(refs),
        'records': int(record_count),
        'runs': len(run_ids),
        'notes': len(note_ids),
    }, ensure_ascii=False))
    db.commit()

    # The request is now durable and fencing is visible to every reader.  The
    # actual clearing runs after the response so progress can be observed; if
    # the process dies here, startup resume finishes it.
    db.expire_all()
    return db.get(PurgeRequest, purge.id), True


def retry_purge(db, purge_id):
    purge = db.get(PurgeRequest, purge_id)
    if purge is None:
        raise HTTPException(404, '未找到清除凭据')
    db.commit()
    db.expire_all()
    return db.get(PurgeRequest, purge_id)


def list_purges(db):
    return [row(purge) for purge in db.scalars(select(PurgeRequest).order_by(PurgeRequest.id.desc()))]


def get_purge(db, purge_id):
    purge = db.get(PurgeRequest, purge_id)
    if purge is None:
        raise HTTPException(404, '未找到清除凭据')
    return row(purge)


def resume_pending():
    """Called on backend start: finish purges interrupted between blocking,
    database clearing and file removal.  Safe to repeat; only receipts left in
    accepted/processing are re-queued and re-verification decides completion."""
    try:
        with Session() as db:
            ids = list(db.scalars(
                select(PurgeRequest.id).where(PurgeRequest.state.in_(RESUMABLE_STATES))
            ))
            # At startup no request can be genuinely in flight, so an
            # interrupted request is returned to accepted for the claim step.
            db.execute(
                update(PurgeRequest)
                .where(PurgeRequest.state == 'processing')
                .values(state='accepted')
            )
            db.commit()
        for purge_id in ids:
            try:
                process_purge(purge_id)
            except Exception:
                logging.exception('resume purge %s failed', purge_id)
    except Exception:
        logging.exception('could not resume pending purges')
