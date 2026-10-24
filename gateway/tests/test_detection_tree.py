from app.importers.generic import ParsedMessage, ParserRegistry
from app.importers.tree import TreeQuery, TreeValidator


def test_filename_does_not_select_parser():
    assert ParserRegistry().detect({"conversations": []})[0] == "unsupported_real_chatgpt_export"
    assert ParserRegistry().detect({"schema_marker":"wrong","conversations":[]})[0] == "unsupported_real_chatgpt_export"

def test_tree_anomalies_and_leaf_path():
    nodes=(ParsedMessage("a",None,"user","a",{},None,None,None),ParsedMessage("b","a","assistant","b",{},None,None,None),ParsedMessage("c","missing","user","c",{},None,None,None))
    assert any(x["type"]=="missing_parent" for x in TreeValidator().validate(nodes))
    assert [x.node_id for x in TreeQuery(nodes).get_path_to_leaf("b")] == ["a","b"]

def test_same_content_different_nodes_are_not_duplicates():
    nodes=(ParsedMessage("a",None,"user","same",{},None,None,None),ParsedMessage("b",None,"user","same",{},None,None,None))
    assert not any(x["type"]=="duplicate_node" for x in TreeValidator().validate(nodes))
