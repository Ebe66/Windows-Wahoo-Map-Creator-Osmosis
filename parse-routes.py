import sys
import os
import osmium

#REL_TYPES = ["historic", "mtb", "bicycle", "foot", "hiking"]
REL_TYPES = ["bicycle", "mtb"]

all_ways = {}
id_counter = 22000000000

class RelationsHandler(osmium.SimpleHandler):
    def __init__(self, all_ways):
        osmium.SimpleHandler.__init__(self)
        self.all_ways = all_ways

    def relation(self, r):
        if r.tags.get("type") == "route" and r.tags.get("route") in REL_TYPES:
            relation = {
                "route": "",
                "type": "",
            }

            relation["route"] = r.tags.get("route")
            relation["type"] = r.tags.get("type")

            for m in r.members:
                if m.type == "w":
                    if m.ref not in self.all_ways:
                        self.all_ways[m.ref] = []
                    self.all_ways[m.ref].append(relation)
        
class WayHandler(osmium.SimpleHandler):
    def __init__(self, all_ways):
        osmium.SimpleHandler.__init__(self)
        global id_counter
        self.all_ways = all_ways
        self.way_id = id_counter

    def way(self, w):
        global id_counter
        if w.id in self.all_ways:
            rel_count = len(self.all_ways[w.id])

            for i in range(0, rel_count):
                relation = self.all_ways[w.id][i]

                if (
                    (rel_count == 2 and i == 1)
                    or
                    (rel_count > 2 and (relation["route"] == "bicycle" or relation["route"] == "mtb"))
                    ):
                    way_nodes = []

                    for node in w.nodes:
                        way_nodes.append(node)
                    
                    new_nodes = []

                    for i in range(len(way_nodes)-1, -1, -1):
                        new_nodes.append(way_nodes[i])
                    way = w.replace(id=self.way_id, nodes=new_nodes, tags=relation)
                else:
                    way = w.replace(id=self.way_id, tags=relation)

class NodeHandler(osmium.SimpleHandler):
    def __init__(self, all_ways, way_writer):
        osmium.SimpleHandler.__init__(self)
        self.all_ways = all_ways
        self.way_writer = way_writer
                       
    def way(self, w):
        #print (f'{w}')
        if w.id in self.all_ways:
            rel_count = len(self.all_ways[w.id])

            for i in range(0, rel_count):
                relation = self.all_ways[w.id][i]

                self.way_writer.add_way(w)
                
    def node(self, n):
        #print (f'{n}')
        # Loop through all_ways and see if a way contains the node
        
        if n.id in self.all_ways:
            self.way_writer.add_node(n)

file_name = sys.argv[1]
output_tmp = sys.argv[1] + ".tmp.xml"
output_name = sys.argv[2]

#print("Parsing relations...")

rel_parser = RelationsHandler(all_ways)
rel_parser.apply_file(file_name)

#print("...done, found ways: %d" % len(all_ways))

#print("Extracting relation ways...")

if os.path.exists(output_tmp):
    os.remove(output_tmp)

if os.path.exists(output_name):
    os.remove(output_name)

way_parser = WayHandler(all_ways)
way_parser.apply_file(file_name)

way_writer = osmium.SimpleWriter(output_tmp)
node_parser = NodeHandler(all_ways, way_writer)
node_parser.apply_file(file_name)
way_writer.close()

with open(output_name, "w") as output:
    with open(output_tmp, "r") as tmp:
        i = 0
        for l in tmp:
            output.write(l)
            i = i + 1

os.remove(output_tmp)
#os.remove(bbox_name)

print("...done")