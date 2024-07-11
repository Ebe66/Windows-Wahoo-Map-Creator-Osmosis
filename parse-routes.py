import sys
import os
import osmium

#REL_TYPES = ["historic", "mtb", "bicycle", "foot", "hiking"]
REL_TYPES = ["bicycle", "mtb"]

all_ways = {}

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
    def __init__(self, all_ways, way_writer):
        osmium.SimpleHandler.__init__(self)
        self.all_ways = all_ways
        self.way_writer = way_writer
        self.way_id = -800000000000

    def way(self, w):
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

                self.way_writer.add_way(way)
                self.way_id = self.way_id + 1
                

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

way_writer = osmium.SimpleWriter(output_tmp)
way_parser = WayHandler(all_ways, way_writer)
way_parser.apply_file(file_name)
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