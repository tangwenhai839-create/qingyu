import stat
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from app.archive.service import ArchiveService
from app.db import engine
from app.importers.zip_staging import UnsafeZipError, safe_extract
from app.models import (
    ArchiveObject,
    ArchiveObservation,
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
    for model in (Attachment,MessageObservation,MessageRevision,Message,Conversation,ArchiveObservation,ImportJob,SourceAccount,ArchiveObject): db.execute(delete(model))
    db.commit()


@pytest.mark.parametrize('name',["../x","/x","C:/x","\\\\server\\x","file.txt:hidden","CON.txt","aux","trail. ","..\\x"])
def test_windows_unsafe_members_rejected_before_write(tmp_path,name):
    archive=tmp_path/'bad.zip'
    with zipfile.ZipFile(archive,'w') as output: output.writestr(name,b'x')
    destination=tmp_path/'out'
    with pytest.raises(UnsafeZipError): safe_extract(archive,destination)
    assert not destination.exists() or not list(destination.rglob('*'))


def test_case_collision_duplicate_and_symlink_rejected(tmp_path):
    collision=tmp_path/'collision.zip'
    with zipfile.ZipFile(collision,'w') as output: output.writestr('Folder/File.txt',b'a'); output.writestr('folder/file.TXT',b'b')
    with pytest.raises(UnsafeZipError): safe_extract(collision,tmp_path/'out1')
    link=tmp_path/'link.zip'; info=zipfile.ZipInfo('link'); info.create_system=3; info.external_attr=(stat.S_IFLNK|0o777)<<16
    with zipfile.ZipFile(link,'w') as output: output.writestr(info,'target')
    with pytest.raises(UnsafeZipError): safe_extract(link,tmp_path/'out2')


def test_concurrent_archive_publication_keeps_one_object_and_two_observations(tmp_path):
    root=tmp_path/'data'; source=tmp_path/'source.bin'; source.write_bytes(b'concurrent immutable bytes')
    with Session(engine) as db:
        _clear(db)
    def archive(label):
        with Session(engine) as db: return ArchiveService(root,db).archive_file(source,f'{label}.bin','test','application/octet-stream')
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(archive,['a','b']))
    assert results[0].object_id==results[1].object_id
    with Session(engine) as db: assert db.query(ArchiveObject).count()==1; assert db.query(ArchiveObservation).count()==2; record=db.query(ArchiveObject).one(); assert record.media_type=='application/octet-stream'
    assert len(list((root/'archive'/'objects').rglob(results[0].sha256)))==1


def test_orphan_file_and_missing_manifest_are_recovered(tmp_path):
    root=tmp_path/'data'; source=tmp_path/'source.bin'; source.write_bytes(b'recoverable')
    with Session(engine) as db:
        _clear(db)
        with pytest.raises(RuntimeError): ArchiveService(root,db,lambda event: (_ for _ in ()).throw(RuntimeError('crash')) if event=='file_published' else None).archive_file(source)
        db.rollback()
    assert len(list((root/'archive'/'objects').rglob('*'))) >= 1
    with Session(engine) as db: recovered=ArchiveService(root,db).archive_file(source); assert recovered.duplicate and recovered.manifest_path.exists()
