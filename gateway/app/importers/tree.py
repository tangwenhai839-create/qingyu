
class TreeValidator:
    def validate(self, nodes):
        seen=set(); anomalies=[]; mapping={n.node_id:n for n in nodes}
        for n in nodes:
            if n.node_id in seen: anomalies.append({"type":"duplicate_node","node_id":n.node_id})
            seen.add(n.node_id)
            if n.parent_node_id == n.node_id: anomalies.append({"type":"self_parent","node_id":n.node_id})
            if n.parent_node_id and n.parent_node_id not in mapping: anomalies.append({"type":"missing_parent","node_id":n.node_id,"parent":n.parent_node_id}); anomalies.append({"type":"orphan","node_id":n.node_id,"parent":n.parent_node_id})
        for n in nodes:
            cur=n.node_id; path=set()
            while cur and cur in mapping:
                if cur in path: anomalies.append({"type":"cycle","node_id":n.node_id}); break
                path.add(cur); cur=mapping[cur].parent_node_id
        return anomalies

class TreeQuery:
    def __init__(self, nodes): self.nodes={n.node_id:n for n in nodes}
    def get_children(self, node_id): return [n for n in self.nodes.values() if n.parent_node_id == node_id]
    def get_path_to_leaf(self, leaf_id):
        out=[]; cur=leaf_id; seen=set()
        while cur and cur in self.nodes and cur not in seen: seen.add(cur); out.append(self.nodes[cur]); cur=self.nodes[cur].parent_node_id
        return list(reversed(out))

