import os
import time
import uuid
from pathlib import Path

from app.db import engine
from app.importers.cleanup import cleanup_failed_staging
from app.importers.generic import ParsedMessage
from app.importers.tree import TreeQuery
from sqlalchemy.orm import Session


def test_cleanup_old_attempt(tmp_path: Path):
    p=tmp_path/'staging'/str(uuid.uuid4())/'1'/str(uuid.uuid4()); p.mkdir(parents=True); (p/'x').write_text('x'); old=time.time()-9*86400; os.utime(p,(old,old))
    with Session(engine) as db: assert cleanup_failed_staging(tmp_path,db,7).counts['deleted']==1
    assert not p.exists()

def test_tree_query_path():
    a=ParsedMessage('a',None,'user','a',{},None,None,None); b=ParsedMessage('b','a','assistant','b',{},None,None,None); assert [x.node_id for x in TreeQuery((a,b)).get_path_to_leaf('b')]==['a','b']
