from app.importers.revision import compute_revision_fingerprint


def base(**kw):
    x={'role':'user','content_text':'hello','content_json':{'a':1,'b':2},'model':'m','raw_timestamp':'1'}; x.update(kw); return compute_revision_fingerprint(**x)
def test_role_and_json_semantics_change_fingerprint():
    assert base() != base(role='assistant')
    assert base() != base(content_json={'content_type':'multimodal_text'})
def test_model_is_revision_semantics():
    assert base() != base(model='other')
def test_object_order_is_canonical_but_arrays_keep_order():
    assert base(content_json={'b':2,'a':1}) == base()
    assert base(content_json=['a','b']) != base(content_json=['b','a'])
