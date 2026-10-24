from app.db import engine
from app.importers.cleanup import cleanup_failed_staging
from app.importers.generic import ParserRegistry
from app.importers.report import empty_report
from sqlalchemy.orm import Session


def test_cleanup_dry_run_and_protected_attempt(tmp_path):
    import uuid
    old = tmp_path/'staging'/str(uuid.uuid4())/'1'/str(uuid.uuid4()); old.mkdir(parents=True); old.touch()
    import os
    import time
    old_time = time.time() - 10*86400; os.utime(old, (old_time, old_time))
    with Session(engine) as db:
        assert cleanup_failed_staging(tmp_path, db, retention_days=7, dry_run=True, protected_paths=[old]).skipped_active == [old.resolve()]
        assert old.exists()
        assert cleanup_failed_staging(tmp_path, db, retention_days=7, dry_run=True).deleted == [old.resolve()]
        assert cleanup_failed_staging(tmp_path, db, retention_days=7).counts['deleted'] == 1
    assert not old.exists()

def test_unsupported_real_export_is_explicit():
    code, parser = ParserRegistry().detect({'conversations': []})
    assert code == 'unsupported_real_chatgpt_export' and parser is None

def test_report_has_detailed_categories():
    report = empty_report('fixture', '0.1', 'matched')
    assert set(report['attachments']) == {'available','missing','referenced_only','failed'}
    assert set(report['conversations']) == {'new','existing','changed','skipped','failed'}
