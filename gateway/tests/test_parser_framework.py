from app.importers.generic import GenericTreeParser


def test_generic_fixture_parser_preserves_tree_and_unknown_content() -> None:
    payload = {"schema_marker":"qingyu-fixture-v1", "conversations": [{"id": "c1", "title": "Fixture", "current_node": "n2", "mapping": {
        "n1": {"message": {"author": {"role": "user"}, "content": {"parts": ["你好"]}}, "parent": None},
        "n2": {"message": {"author": {"role": "assistant"}, "content": {"parts": ["你好！"]}}, "parent": "n1"},
        "n3": {"message": {"author": {"role": "tool"}, "content": {"weird": True}}, "parent": "n1"},
    }}]}
    result = next(GenericTreeParser().parse(payload))
    assert result.external_id == "c1"
    assert [(m.node_id, m.parent_node_id, m.role) for m in result.messages] == [("n1", None, "user"), ("n2", "n1", "assistant"), ("n3", "n1", "tool")]
    assert result.messages[2].content_text is not None
