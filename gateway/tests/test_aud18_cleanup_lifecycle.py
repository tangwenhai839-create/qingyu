import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import app.importers.cleanup as cleanup_module
import app.importers.processor as processor_module
import pytest
from app.db import engine
from app.importers.cleanup import cleanup_failed_staging
from app.importers.worker import ImportWorker
from app.importers.worker_runtime import WorkerRuntime
from app.models import (
    ArchiveObject,
    Attachment,
    Conversation,
    ImportJob,
    Message,
    MessageObservation,
    MessageRevision,
    SourceAccount,
)
from sqlalchemy import delete
from sqlalchemy.orm import Session


def _clear(db):
    for model in (Attachment,MessageObservation,MessageRevision,Message,Conversation,ImportJob,SourceAccount,ArchiveObject): db.execute(delete(model))
    db.commit()


def _job(db,status,lease_expires_at=None,retain_lease=False):
    account=SourceAccount(platform='cleanup',account_label=str(uuid.uuid4()),metadata_json={})
    archive=ArchiveObject(sha256=uuid.uuid4().hex*2,size_bytes=1,relative_path='unused',media_type='x',integrity_status='verified',metadata_json={})
    db.add_all([account,archive]); db.flush()
    token=uuid.uuid4()
    job=ImportJob(source_account_id=account.id,archive_object_id=archive.id,parser_name='fixture',parser_version='1',status=status,lease_token=token if status=='running' or retain_lease else None,lease_expires_at=lease_expires_at,attempt_count=1,report_json={})
    db.add(job); db.commit()
    return job, token


def _attempt(root,job_id,token,attempt=1,days_old=10):
    path=root/'staging'/str(job_id)/str(attempt)/str(token); path.mkdir(parents=True); (path/'diagnostic').write_text('x')
    stamp=time.time()-days_old*86400; os.utime(path,(stamp,stamp)); return path.resolve()


def _symlink_directory(link, target):
    try:
        os.symlink(target,link,target_is_directory=True)
    except OSError as exc:
        pytest.skip(f'directory symlink unavailable: {exc.__class__.__name__}')


def test_pending_running_and_expired_running_attempts_are_protected(tmp_path):
    with Session(engine,expire_on_commit=False) as db:
        _clear(db)
        pending,pending_token=_job(db,'pending')
        running,running_token=_job(db,'running',datetime.now(UTC)+timedelta(minutes=5))
        expired,expired_token=_job(db,'running',datetime.now(UTC)-timedelta(minutes=5))
        paths={
            _attempt(tmp_path,pending.id,pending_token),
            _attempt(tmp_path,running.id,running_token),
            _attempt(tmp_path,expired.id,expired_token),
        }
        result=cleanup_failed_staging(tmp_path,db,retention_days=7)
        assert set(result.skipped_active)==paths and not result.deleted
        assert all(path.exists() for path in paths)


def test_terminal_orphan_recent_and_unknown_paths_are_classified(tmp_path):
    with Session(engine,expire_on_commit=False) as db:
        _clear(db)
        completed,completed_token=_job(db,'completed')
        failed,failed_token=_job(db,'failed')
        completed_path=_attempt(tmp_path,completed.id,completed_token)
        failed_path=_attempt(tmp_path,failed.id,failed_token)
        orphan_path=_attempt(tmp_path,uuid.uuid4(),uuid.uuid4())
        recent_path=_attempt(tmp_path,uuid.uuid4(),uuid.uuid4(),days_old=1)
        unknown=tmp_path/'staging'/'not-a-job'; unknown.mkdir(parents=True)
        active_upload=tmp_path/'staging'/'import-upload-active'; active_upload.write_text('upload')
        result=cleanup_failed_staging(tmp_path,db,retention_days=7)
        assert set(result.deleted)=={completed_path,failed_path,orphan_path}
        assert result.skipped_recent==[recent_path]
        assert {unknown.resolve(),active_upload.resolve()} <= set(result.skipped_unknown)
        assert active_upload.exists()
        assert result.counts=={
            'deleted':3,
            'skipped_active':0,
            'skipped_retention':1,
            'skipped_recent':1,
            'skipped_unknown':2,
            'quarantined':0,
            'quarantine_recovered':0,
            'failed':0,
        }


def test_terminal_jobs_with_valid_ownership_are_protected_before_status(tmp_path):
    with Session(engine,expire_on_commit=False) as db:
        _clear(db)
        future=datetime.now(UTC)+timedelta(minutes=5)
        failed,failed_token=_job(db,'failed',future,retain_lease=True)
        completed,completed_token=_job(db,'completed',future,retain_lease=True)
        failed_path=_attempt(tmp_path,failed.id,failed_token)
        completed_path=_attempt(tmp_path,completed.id,completed_token)
        result=cleanup_failed_staging(tmp_path,db,7)
        assert set(result.skipped_active)=={failed_path,completed_path}
        assert not result.deleted
        assert failed_path.exists() and completed_path.exists()


def test_terminal_jobs_with_expired_or_absent_ownership_are_deleted(tmp_path):
    with Session(engine,expire_on_commit=False) as db:
        _clear(db)
        expired,expired_token=_job(db,'failed',datetime.now(UTC)-timedelta(minutes=5),retain_lease=True)
        no_lease,no_lease_token=_job(db,'completed')
        paths={_attempt(tmp_path,expired.id,expired_token),_attempt(tmp_path,no_lease.id,no_lease_token)}
        result=cleanup_failed_staging(tmp_path,db,7)
        assert set(result.deleted)==paths
        assert all(not path.exists() for path in paths)


def test_terminal_job_with_live_but_mismatched_ownership_fails_closed(tmp_path):
    with Session(engine,expire_on_commit=False) as db:
        _clear(db)
        job,token_a=_job(db,'failed',datetime.now(UTC)+timedelta(minutes=5),retain_lease=True)
        token_b=uuid.uuid4(); mismatched=_attempt(tmp_path,job.id,token_b)
        wrong_attempt=_attempt(tmp_path,job.id,token_a,attempt=2)
        result=cleanup_failed_staging(tmp_path,db,7)
        assert set(result.skipped_unknown)=={mismatched,wrong_attempt}
        assert not result.deleted
        assert mismatched.exists() and wrong_attempt.exists()


def test_cleanup_delete_failure_is_reported_without_stopping_other_candidates(tmp_path,monkeypatch):
    with Session(engine) as db:
        _clear(db); path=_attempt(tmp_path,uuid.uuid4(),uuid.uuid4()); safe=_attempt(tmp_path,uuid.uuid4(),uuid.uuid4())
        original_move=cleanup_module._move_to_quarantine; original_remove=cleanup_module._remove_tree_without_links
        quarantines={}
        def observed_move(candidate,base):
            quarantine=original_move(candidate,base); quarantines[candidate]=quarantine; return quarantine
        def fail_selected(candidate):
            if candidate==quarantines.get(path): raise PermissionError('busy')
            return original_remove(candidate)
        monkeypatch.setattr(cleanup_module,'_move_to_quarantine',observed_move)
        monkeypatch.setattr(cleanup_module,'_remove_tree_without_links',fail_selected)
        result=cleanup_failed_staging(tmp_path,db,7)
        assert result.quarantined==[quarantines[path]] and result.deleted==[safe]
        assert quarantines[path].exists() and not path.exists() and not safe.exists()


def test_directory_symlink_cannot_alias_and_delete_active_attempt(tmp_path):
    with Session(engine,expire_on_commit=False) as db:
        _clear(db)
        completed,completed_token=_job(db,'completed')
        running,running_token=_job(db,'running',datetime.now(UTC)+timedelta(minutes=5))
        unsafe=_attempt(tmp_path,completed.id,completed_token)
        target=_attempt(tmp_path,running.id,running_token)
        link=unsafe/'active-alias'; _symlink_directory(link,target)
        result=cleanup_failed_staging(tmp_path,db,7)
        assert unsafe in result.skipped_unknown
        assert target in result.skipped_active
        assert target.exists() and (target/'diagnostic').exists()
        assert set(result.deleted).isdisjoint(result.skipped_active)


@pytest.mark.skipif(sys.platform!='win32',reason='Windows junction race integration')
def test_candidate_replaced_by_junction_at_quarantine_boundary_is_not_followed(tmp_path,monkeypatch):
    target=tmp_path/'race-target'; target.mkdir(); sentinel=target/'sentinel'; sentinel.write_text('preserve')
    root=tmp_path/'root'; candidate=_attempt(root,uuid.uuid4(),uuid.uuid4()); backup=tmp_path/'candidate-backup'
    original_replace=cleanup_module.os.replace; injected=False
    def replace_with_race(source,destination):
        nonlocal injected
        if not injected and Path(source)==candidate:
            injected=True
            original_replace(source,backup)
            created=subprocess.run(['cmd','/c','mklink','/J',str(candidate),str(target)],capture_output=True,text=True,check=False)
            if created.returncode: pytest.skip('Windows junction creation unavailable')
        return original_replace(source,destination)
    monkeypatch.setattr(cleanup_module.os,'replace',replace_with_race)
    try:
        with Session(engine) as db:
            result=cleanup_failed_staging(root,db,7)
        assert injected
        assert candidate in result.skipped_unknown and not result.deleted
        assert sentinel.read_text()=='preserve'
    finally:
        monkeypatch.undo()
        if candidate.exists(): os.rmdir(candidate)
        if backup.exists(): cleanup_module._remove_tree_without_links(backup)


@pytest.mark.skipif(sys.platform!='win32',reason='Windows directory handle integration')
def test_directory_identity_guard_blocks_replacement_during_traversal(tmp_path,monkeypatch):
    candidate=tmp_path/'guarded'; candidate.mkdir(); (candidate/'local').write_text('local')
    outside=tmp_path/'outside'; outside.mkdir(); sentinel=outside/'sentinel'; sentinel.write_text('preserve')
    backup=tmp_path/'guarded-backup'; original_scandir=cleanup_module.os.scandir
    attempted=False; blocked=False
    def racing_scandir(path):
        nonlocal attempted,blocked
        if not attempted and Path(path)==candidate:
            attempted=True
            try:
                os.replace(candidate,backup)
            except PermissionError:
                blocked=True
        return original_scandir(path)
    monkeypatch.setattr(cleanup_module.os,'scandir',racing_scandir)
    cleanup_module._remove_tree_without_links(candidate)
    assert attempted and blocked
    assert sentinel.read_text()=='preserve'
    assert not candidate.exists() and not backup.exists()


@pytest.mark.parametrize('target_name',['outside-sentinel','archive-objects','manifests'])
def test_symlink_targets_outside_staging_are_never_followed(tmp_path,target_name):
    root=tmp_path/'root'
    if target_name=='archive-objects': target=root/'archive'/'objects'
    elif target_name=='manifests': target=root/'manifests'
    else: target=tmp_path/'outside-sentinel'
    target.mkdir(parents=True); sentinel=target/'sentinel'; sentinel.write_text('preserve')
    with Session(engine,expire_on_commit=False) as db:
        _clear(db); completed,token=_job(db,'completed'); unsafe=_attempt(root,completed.id,token)
        nested=unsafe/'a'/'b'; nested.mkdir(parents=True); _symlink_directory(nested/'redirect',target)
        old=time.time()-10*86400; os.utime(unsafe,(old,old))
        result=cleanup_failed_staging(root,db,7)
        assert unsafe in result.skipped_unknown
        assert not result.deleted
        assert sentinel.read_text()=='preserve'


@pytest.mark.skipif(sys.platform!='win32',reason='Windows junction integration')
def test_windows_junction_is_detected_without_following(tmp_path):
    target=tmp_path/'junction-target'; target.mkdir(); sentinel=target/'sentinel'; sentinel.write_text('preserve')
    root=tmp_path/'root'
    with Session(engine,expire_on_commit=False) as db:
        _clear(db); completed,token=_job(db,'completed'); unsafe=_attempt(root,completed.id,token)
        junction=unsafe/'junction'
        created=subprocess.run(['cmd','/c','mklink','/J',str(junction),str(target)],capture_output=True,text=True,check=False)
        if created.returncode:
            pytest.skip('Windows junction creation unavailable')
        try:
            result=cleanup_failed_staging(root,db,7)
            assert unsafe in result.skipped_unknown and not result.deleted
            assert sentinel.read_text()=='preserve'
        finally:
            if junction.exists(): os.rmdir(junction)


@pytest.mark.skipif(sys.platform!='win32',reason='Windows file sharing integration')
def test_real_windows_file_lock_failure_does_not_stop_cleanup(tmp_path):
    import ctypes
    from ctypes import wintypes
    with Session(engine) as db:
        _clear(db); locked=_attempt(tmp_path,uuid.uuid4(),uuid.uuid4()); safe=_attempt(tmp_path,uuid.uuid4(),uuid.uuid4())
        locked_file=locked/'diagnostic'
        create_file=ctypes.windll.kernel32.CreateFileW
        create_file.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        create_file.restype=wintypes.HANDLE
        handle=create_file(str(locked_file),0x80000000,0,None,3,0,None)
        assert handle not in (0,wintypes.HANDLE(-1).value)
        try:
            result=cleanup_failed_staging(tmp_path,db,7)
            # Windows may reject the atomic directory rename while a child was
            # opened without FILE_SHARE_DELETE. This is an observable rename
            # failure, so the source remains intact for the next pass.
            assert result.failed==[locked] and safe in result.deleted
            assert locked.exists() and not safe.exists()
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
        recovered=cleanup_failed_staging(tmp_path,db,7)
        assert locked in recovered.deleted
        assert not locked.exists()


def test_expired_attempt_survives_cleanup_recovery_and_retry_claim(tmp_path):
    with Session(engine,expire_on_commit=False) as db:
        _clear(db); job,token_a=_job(db,'running',datetime.now(UTC)-timedelta(minutes=1)); old_path=_attempt(tmp_path,job.id,token_a)
        assert cleanup_failed_staging(tmp_path,db,7).skipped_active==[old_path]
        assert ImportWorker(db).recover_stale()==1
        claim=ImportWorker(db).claim_next(60); assert claim.job_id==job.id and claim.lease_token!=token_a
        new_path=Path(ImportWorker(db).attempt_staging_path(tmp_path,claim)); new_path.mkdir(parents=True)
        result=cleanup_failed_staging(tmp_path,db,7)
        assert result.skipped_active==[new_path.resolve()]
        assert result.skipped_unknown==[old_path]
        job=db.get(ImportJob,job.id); job.status='completed'; job.lease_token=None; job.lease_expires_at=None; db.commit()
        result=cleanup_failed_staging(tmp_path,db,7)
        assert result.deleted==[old_path]
        assert result.skipped_recent==[new_path.resolve()]


def test_cleanup_cannot_delete_attempt_while_processor_is_copying(tmp_path,monkeypatch):
    root=tmp_path/'concurrent'; payload={'schema_marker':'qingyu-fixture-v1','conversations':[{'id':'cleanup-concurrent','mapping':{}}]}; source=tmp_path/'input.zip'
    with zipfile.ZipFile(source,'w') as archive: archive.writestr('fixture.json',json.dumps(payload))
    content=source.read_bytes(); digest=hashlib.sha256(content).hexdigest(); target=root/'archive'/'objects'/digest[:2]/digest; target.parent.mkdir(parents=True); target.write_bytes(content)
    with Session(engine) as db:
        _clear(db); account=SourceAccount(platform='cleanup-runtime',account_label=str(uuid.uuid4()),metadata_json={}); archive=ArchiveObject(sha256=digest,size_bytes=len(content),relative_path=target.relative_to(root).as_posix(),media_type='application/zip',integrity_status='verified',metadata_json={}); db.add_all([account,archive]); db.flush(); job=ImportJob(source_account_id=account.id,archive_object_id=archive.id,parser_name='fixture',parser_version='1',status='pending',report_json={}); db.add(job); db.commit(); job_id=job.id
    entered=threading.Event(); release=threading.Event(); original=processor_module.copy_and_verify_archive
    def paused_copy(*args):
        result=original(*args); entered.set(); assert release.wait(2); return result
    monkeypatch.setattr(processor_module,'copy_and_verify_archive',paused_copy)
    runtime=WorkerRuntime(root,poll_interval=.01,heartbeat_interval=.02,lease_duration=5); thread=threading.Thread(target=runtime.run_once); thread.start(); assert entered.wait(2)
    attempt=next((root/'staging'/'jobs'/str(job_id)).glob('*/*')).resolve(); old=time.time()-10*86400; os.utime(attempt,(old,old))
    with Session(engine) as cleanup_db:
        result=cleanup_failed_staging(root,cleanup_db,7); assert result.skipped_active==[attempt] and attempt.exists()
    release.set(); thread.join(timeout=3); assert not thread.is_alive()
    with Session(engine) as db: assert db.get(ImportJob,job_id).status=='completed'
