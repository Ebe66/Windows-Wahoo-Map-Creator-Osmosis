""" 
Pre generates bicycle/MTB route files and elevation files using Sonny's Lidar data.
This runs in a venv in wsl
Setting up a venv and installing a different python verion in WSL:
https://cloudbytes.dev/snippets/upgrade-python-to-latest-version-on-ubuntu-linux
To start venv:
source wmc/bin/activate 
Do the thing:
python wmc-e.py zuid-holland
To leave venv:
deactivate
"""
#!/usr/bin/python
# -*- coding:utf-8 -*-

# pylint: disable=C0103

import glob
import multiprocessing
import os
import os.path
import subprocess
import sys
import time
import shutil
import math
import geojson
import requests
import osmium

from shapely.geometry import Polygon, shape
# from osgeo import ogr, osr, gdal

# Configurable Parameters

# Maximum age of source maps and land shape files etc before they are redownloaded
Max_Days_Old = 28

# Force (re)processing of source maps and the land shape file
# If 0, use Max_Days_Old to check for expired maps
# If 1, force redownloading/processing of maps and landshape
Force_Processing = 0

# Number of threads to use in the mapwriter plug-in
threads = str(multiprocessing.cpu_count() - 1)
if int(threads) < 1:
    threads = '8'
# Or set it manually to:
# threads = '1'
# print(f'threads = {threads}/n')

# Number of workers for the Osmosis read binary fast function
workers = '4'

# Keep (1) or delete (0) the country/region map folders after compression
keep_folders = 1
generate_elevation = True
integrate_Wandrer = False
x_y_processing_mode = False
Wanted_X = 131
Wanted_Y = 84
Process_Routing = False
#REL_TYPES = ["historic", "mtb", "bicycle", "foot", "hiking"]
REL_TYPES = ["bicycle", "mtb"]

# End of Configurable Parameters

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
    def __init__(self, all_ways, way_writer):
        global id_counter
        osmium.SimpleHandler.__init__(self)
        self.all_ways = all_ways
        self.way_writer = way_writer
        self.way_id = id_counter
                       
    def way(self, w):
        global id_counter
        
        #print (f'{w}')
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

                    #print (f'{w.nodes}')
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
                id_counter = self.way_id
                
    #def node(self, n):
    #    #print (f'{n}')
    #    self.way_writer.add_node(n)

def deg2num(lat_deg, lon_deg, zoom=8):
    """ Convert lon./lat. to tile numbers """
    lat_rad = math.radians(lat_deg)
    n = 2.0 ** zoom
    xtile = int((lon_deg + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return (xtile, ytile)

def num2deg(xtile, ytile, zoom=8):
    """ Convert tile numbers to lon./lat. """
    n = 2.0 ** zoom
    lon_deg = xtile / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * ytile / n)))
    lat_deg = math.degrees(lat_rad)
    return (lat_deg, lon_deg)

def geom(wanted):
    """ Get the Geofabrik outline of the desired country/region from the Geofabrik json file and the download url of the map.
    input parameter is the name of the desired country/region as use by Geofabric in their json file. """
    with open('geofabrik.json', encoding='utf8') as f_gf:
        data = geojson.load(f_gf)
    f_gf.close()

    # loop through all entries in the json file to find the one we want
    for x in data.features:
        props = x.properties
        idv = props.get('id', '')
        if idv != wanted:
            continue
        # print (props.get('urls', ''))
        wurls = props.get('urls', '')
        return (x.geometry, wurls.get('pbf', ''))
    return None, None

def find_geofbrik_url(name, geofabrik_json):
    """ Get the map download url from a region with the already loaded json data """
    for x in geofabrik_json.features:
        props = x.properties
        idv = props.get('id', '')
        if idv != name:
            continue
        # print (props.get('urls', ''))
        wurls = props.get('urls', '')
        return wurls.get('pbf', '')
    return None

def find_geofbrik_parent(name, geofabrik_json):
    """Find the parent country/region of a country/region example find europe for netherlands"""
    for x in geofabrik_json.features:
        props = x.properties
        idv = props.get('id', '')
        if idv != name:
            continue
        return (props.get('parent', ''), props.get('id', ''))
    return None, None

def find_needed_countries(bbox_tiles_l, wanted_map_l, xy_mode=False):
    """ # Find the maps to download from Geofabrik for a given range of tiles
    arguments are
    - list of tiles of the desired region bounding box
    - name of desired region as used in Geofabrik json file
    - are we processing a single tile? """
    output = []

    with open('geofabrik.json', encoding='utf8') as file_gf:
        geofabrik_json_data = geojson.load(file_gf)
    file_gf.close()

    # itterate through tiles and find Geofabrik regions that are in the tiles
    counter = 1
    for tile_l in bbox_tiles_l:
        # Do progress indicator every 50 tiles
        if counter % 50 == 0:
            print(
                f'Processing tile {counter} of {len(bbox_tiles_l)+1}', end='\r')
        counter += 1

        parent_added = 0
        force_added = 0

        # example contents of tile: {'index': 0, 'x': 130, 'y': 84, 'tile_left': 2.8125, 'tile_top': 52.48278022207821,
        # 'tile_right': 4.21875, 'tile_bottom': 51.6180165487737}
        # convert tile x/y to tile polygon lon/lat
        poly = Polygon([(tile_l["tile_left"], tile_l["tile_top"]), (tile_l["tile_right"], tile_l["tile_top"]), (tile_l["tile_right"],
                       tile_l["tile_bottom"]), (tile_l["tile_left"], tile_l["tile_bottom"]), (tile_l["tile_left"], tile_l["tile_top"])])

        # (re)initialize list of needed maps and their url's
        must_download_maps = []
        must_download_urls = []

        # itterate through countries/regions in the geofabrik json file
        for regions in geofabrik_json_data.features:
            props = regions.properties
            parent = props.get('parent', '')
            regionname = props.get('id', '')
            rurls = props.get('urls', '')
            rurl = rurls.get('pbf', '')
            rgeom = regions.geometry
            rshape = shape(rgeom)

            if not xy_mode:
                # print (f'Processing region: {regionname}')

                # check if the region we are processing is needed for the tile we are processing

                # If currently processing country/region IS the desired country/region
                if regionname == wanted_map_l:
                    # Check if it is part of the tile we are processing
                    if rshape.intersects(poly):  # if so
                        if regionname not in must_download_maps and regionname not in geofabrik_regions:
                            must_download_maps.append(regionname)
                            must_download_urls.append(rurl)
                        # if there is an intersect, force the tile to be put in the output
                        force_added = 1
                    else:  # currently processing tile does not contain, a part of, the desired region
                        continue

                # currently processing country/region is NOT the desired country/region but might be in the tile (neighbouring country)
                if regionname != wanted_map_l:
                    # check if we are processing a country or a sub-region. For countries only process other countries. also block special geofabrik sub regions
                    # processing a country and no special sub-region
                    if parent in geofabrik_regions and regionname not in block_download and regionname not in geofabrik_regions:
                        # Check if rshape is a part of the tile
                        if rshape.intersects(poly):
                            # print(f'\tintersecting tile: {regionname} tile={tile}')
                            if regionname not in must_download_maps:
                                must_download_maps.append(regionname)
                                must_download_urls.append(rurl)

            else:  # XY mode
                # processing a country and no special sub-region
                if parent in geofabrik_regions and regionname not in block_download and regionname not in geofabrik_regions:
                    # Check if rshape is a part of the tile
                    if rshape.intersects(poly):
                        # print(f'\tintersecting tile: {regionname} tile={tile}')
                        if regionname not in must_download_maps:
                            must_download_maps.append(regionname)
                            must_download_urls.append(rurl)

        # If this tile contains the desired region, add it to the output
        # print (f'map= {wanted_map_l}\tmust_download= {must_download_maps}\tparent_added= {parent_added}\tforce_added= {force_added}')
        if wanted_map_l in must_download_maps or parent_added == 1 or force_added == 1 or xy_mode is True:
            # first replace any forward slashes with underscores (us/texas to us_texas)
            must_download_maps = [sub.replace(
                '/', '_') for sub in must_download_maps]
            output.append({'x': tile_l['x'], 'y': tile_l['y'], 'left': tile_l['tile_left'], 'top': tile_l['tile_top'],
                          'right': tile_l['tile_right'], 'bottom': tile_l['tile_bottom'], 'countries': must_download_maps, 'urls': must_download_urls})
        # print (f'\nmust_download: {must_download_maps}')
        # print (f'must_download: {must_download_urls}')
    return output


CurDir = os.getcwd()  # Get Current Directory

MAP_PATH = os.path.join(CurDir, 'Maps')
OUT_PATH = os.path.join(CurDir, 'Output')
land_polygons_file = os.path.join(
    CurDir, 'land-polygons-split-4326', 'land_polygons.shp')
geofabrik_json_file = os.path.join(CurDir, 'geofabrik.json')
geofabrik_regions = ['africa', 'asia', 'australia-oceania', 'baden-wuerttemberg', 
                     'bayern', 'brazil', 'california', 'canada', 'europe', 'france',
                     'germany', 'india', 'indonesia', 'italy', 'japan', 'netherlands', 
                     'nordrhein-westfalen', 'north-america', 'poland', 'russia', 'south-america', 'spain', 'united-kingdom' , 'us']

# List of regions to block. these regions are "collections" of other countries/regions/states
block_download = ['africa', 'alps', 'asia', 'australia-oceania', 'britain-and-ireland', 'canada', 'dach', 'europe', 'great-britain' , 'norcal' ,'north-america',
                  'russia','socal', 'south-africa-and-lesotho', 'south-america', 'us', 'us-midwest', 'us-northeast', 'us-pacific', 'us-south', 'us-west']

url = ''
Map_File_Deleted = 0

if len(sys.argv) != 2:
    print(f'Usage: {sys.argv[0]} Geofabrik Country or Region name.')
    sys.exit()

wanted_map = sys.argv[1]
# replace spaces in wanted_map with geofabrik minuses
wanted_map = wanted_map.replace(" ", "-")
wanted_map = wanted_map.replace("_", "/") 
wanted_map = wanted_map.lower()

earthexplorer_user = earthexplorer_password = None
if generate_elevation == 1:
    try:
        with open('account.json', encoding='utf8') as File_EE:
            accounts = geojson.load(File_EE)
        File_EE.close()
        # print (accounts)
        earthexplorer_user = accounts['earthexplorer-user']
        earthexplorer_password = accounts['earthexplorer-password']
    except:
        print("Could not read account.json file. Edit the account.json file.")
        accounts = {
            "earthexplorer-user": "Username",
            "earthexplorer-password": "Password"
        }
        File_EE_O = open('account.json', 'w', encoding='utf8')
        File_EE_O.write(geojson.dumps(accounts, indent=4))
        File_EE_O.close()
        sys.exit()

# is geofabrik json file present and not older then Max_Days_Old?
now = time.time()
To_Old = now - 60 * 60 * 24 * Max_Days_Old
try:
    FileCreation = os.path.getmtime(geofabrik_json_file)
    if FileCreation < To_Old:
        print('# Deleting old Geofabriks json file')
        os.remove(os.path.join(CurDir, 'geofabrik.json'))
        # Force_Processing = 1
except:
    # Force_Processing = 1
    pass


# if not present download Geofabriks json file
if not os.path.exists(geofabrik_json_file) or not os.path.isfile(geofabrik_json_file) or Force_Processing == 1:
    print('# Downloading Geofabrik json file')
    url = 'https://download.geofabrik.de/index-v1.json'
    r = requests.get(url, allow_redirects=True, stream=True, timeout=30)
    if r.status_code != 200:
        print('failed to find or download Geofabrik json file')
        sys.exit()
    Download = open(os.path.join(CurDir, 'geofabrik.json'), 'wb')
    for chunk in r.iter_content(chunk_size=10240):
        Download.write(chunk)
    Download.close()

# Check if wanted_map is in the json file and if so get the polygon (shape)
wanted_map_geom, wanted_url = geom(wanted_map)
if not wanted_map_geom:
    # try to prepend us/ to the wanted_map
    wanted_map_geom, wanted_url = geom('us/'+wanted_map)
    if wanted_map_geom:
        wanted_map = 'us/'+wanted_map
    else:
        print(
            f'failed to find country or region {wanted_map} in Geofabrik json file')
        sys.exit()
# print(f'geom={wanted_map_geom}, url={wanted_url}')
# sys.exit()

# convert to shape (multipolygon) of the desired area
wanted_region = shape(wanted_map_geom)
# print (f'shape = {wanted_region}')

# get bounding box of the disired area
(bbox_left, bbox_bottom, bbox_right, bbox_top) = wanted_region.bounds
# print(f'bbox={bbox_left},{bbox_top},{bbox_right},{bbox_bottom}')
# print(f'bbox=min_x:{bbox_left}, max_y{bbox_top}, max_x{bbox_right}, min_y{bbox_bottom}')
# sys.exit()

# convert bounding box from coordinates to slippy tiles
(top_x, top_y) = deg2num(bbox_top, bbox_left)
(bot_x, bot_y) = deg2num(bbox_bottom, bbox_right)
# print (f'voor tx {top_x}, ty {top_y} - bx {bot_x}, by {bot_y}')

# and stay within the allowed tilenumber range!
if top_x < 0:
    top_x = 0
if top_x > 255:
    top_x = 255
if top_y < 0:
    top_y = 0
if top_y > 255:
    top_y = 255
if bot_x < 0:
    bot_x = 0
if bot_x > 255:
    bot_x = 255
if bot_y < 0:
    bot_y = 0
if bot_y > 255:
    bot_y = 255
# print (f'na tx {top_x}, ty {top_y} - bx {bot_x}, by {bot_y}')
# sys.exit()

# Build list of tiles to process from the bounding box
bbox_tiles = []
for x_coord in range(top_x, bot_x + 1):
    for y_coord in range(top_y, bot_y + 1):
        (tile_top, tile_left) = num2deg(x_coord, y_coord)
        (tile_bottom, tile_right) = num2deg(x_coord+1, y_coord+1)
        if tile_left < -180:
            tile_left = -180
        if tile_left > 180:
            tile_left = 180
        if tile_right < -180:
            tile_right = -180
        if tile_right > 180:
            tile_right = 180
        if tile_top < -90:
            tile_top = -90
        if tile_top > 90:
            tile_top = 90
        if tile_bottom < -90:
            tile_bottom = -90
        if tile_bottom > 90:
            tile_bottom = 90
        bbox_tiles.append({'x': x_coord, 'y': y_coord, 'tile_left': tile_left, 'tile_top': tile_top,
                          'tile_right': tile_right, 'tile_bottom': tile_bottom})

# Get a list of the Geofabrik maps needed to process these tiles
print('\nSearching for needed maps, this can take a while.\n')
country = find_needed_countries(bbox_tiles, wanted_map)
# print (f'Country= {country}')
# sys.exit()


# Check for expired land polygons file and delete it
print('\n\n# check land_polygons.shp file')
now = time.time()
To_Old = now - 60 * 60 * 24 * Max_Days_Old
try:
    FileCreation = os.path.getmtime(land_polygons_file)
    if FileCreation < To_Old:
        print('# Deleting old land polygons file')
        # Keep pregenerated files to reduce processing time
        # os.remove(os.path.join (CurDir, 'land-polygons-split-4326', 'land_polygons.shp'))
        # Force_Processing = 1
except:
    pass
    # Force_Processing = 1

# If land polygons file does not exist or Force_Processing active, (re)download it
if not os.path.exists(land_polygons_file) or not os.path.isfile(land_polygons_file) or Force_Processing == 1:
    print('# Downloading land polygons file')
#    url = 'https://osmdata.openstreetmap.de/download/land-polygons-split-4326.zip'
    url = 'https://osmdata.openstreetmap.de/download/land-polygons-split-4326.zip'
    r = requests.get(url, allow_redirects=True, stream=True, timeout=30)
    if r.status_code != 200:
        print('failed to find or download land polygons file')
        sys.exit()
    Download = open(os.path.join(CurDir, 'land-polygons-split-4326.zip'), 'wb')
    for chunk in r.iter_content(chunk_size=10240):
        Download.write(chunk)
    Download.close()
    # unpack it
    cmd = ['7za', 'x', '-y',
           os.path.join(CurDir, 'land-polygons-split-4326.zip')]
    # print(cmd)
    result = subprocess.run(cmd, check=True)
    os.remove(os.path.join(CurDir, 'land-polygons-split-4326.zip'))
    if result.returncode != 0:
        print('Error unpacking land polygons file')
        sys.exit()

print('\n\n# check countries .osm.pbf files')
# Build list of countries needed
border_countries = {}
for tile in country:
    for c in tile['countries']:
        if c not in border_countries:
            border_countries[c] = {'map_file': c}

# print (f'{border_countries}')
# sys.exit()
# time.sleep(60)

# Check for expired maps and delete them
print('# Checking for old maps and remove them')
now = time.time()
To_Old = now - 60 * 60 * 24 * Max_Days_Old
for c in border_countries:
    # Prevent matching to multiple maps like australia and australia-oceania
    map_files = glob.glob(f'{MAP_PATH}/{c}-latest.osm.pbf')
    if len(map_files) != 1:
        # Prevent matching to multiple maps like australia and australia-oceania
        map_files = glob.glob(f'{MAP_PATH}/**/{c}-latest.osm.pbf')
    if len(map_files) == 1 and os.path.isfile(map_files[0]):
        FileCreation = os.path.getmtime(map_files[0])
        if FileCreation < To_Old or Force_Processing == 1:
            print(
                f'# Deleting old map of {c} {map_files},{FileCreation},{To_Old},{Force_Processing}')
            os.remove(map_files[0])
            Map_File_Deleted = 1

if Map_File_Deleted == 1:
    Force_Processing = 1

# Init vars
border_countries = {}
border_countries_urls = {}

# Check/create tile folder structure
for tile in country:
    outdir = os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}')
    if not os.path.isdir(outdir):
        os.makedirs(outdir)

    # print(f'Tile = {tile}')
    # search for and download missing map files from Geofabrik
    i = 0
    for c in tile['countries']:  # we want this osm.pbf
        if c not in border_countries:  # and it is not yet in our list of files
            print(f'# Checking mapfile for {c}')
            # Prevent matching to multiple maps like australia and australia-oceania
            # Search for it in the map folder
            map_files = glob.glob(f'{MAP_PATH}/{c}-latest.osm.pbf')
            if len(map_files) != 1:  # not found in the map folder
                # Prevent matching to multiple maps like australia and australia-oceania
                # and the sub-folders of the map folder
                map_files = glob.glob(f'{MAP_PATH}/**/{c}-latest.osm.pbf')
            # Also not found in a sub folder so download it
            if len(map_files) != 1 or not os.path.isfile(map_files[0]):
                print(f'# Trying to download missing map of {c}.')
                url = tile['urls'][i]
                r = requests.get(url, allow_redirects=True, stream=True, timeout=30)
                if r.status_code != 200:
                    print(f'failed to find or download country: {c}')
                    sys.exit()
                Download = open(os.path.join(
                    MAP_PATH, f'{c}' + '-latest.osm.pbf'), 'wb')
                for chunk in r.iter_content(chunk_size=10240):
                    Download.write(chunk)
                Download.close()
                map_files = [os.path.join(
                    MAP_PATH, f'{c}' + '-latest.osm.pbf')]
                print(f'# Map of {c} downloaded.')
            border_countries[c] = {'map_file': map_files[0]}
        i += 1
        

# Extract bike routes from source maps
print('\n\n# Extracting bike routes from country osm.pbf files')
for key, val in border_countries.items():
    #print(key, val)
    all_ways = {}

    outFileo5m = os.path.join(OUT_PATH, f'routes-{key}.osm.pbf')

    if not os.path.isfile(outFileo5m) or Force_Processing == 1:
        print(f'\n\n# Extracting routes from map of {key}')

        print("Parsing relations...")
        rel_parser = RelationsHandler(all_ways)
        rel_parser.apply_file(val['map_file'], locations=True)
        #rel_parser.apply_file(os.path.join(OUT_PATH, f'outFileFiltered-{key}.o5m'), locations=True)
        print("...done, found ways: %d" % len(all_ways))
        print("Extracting relation ways...")

        way_writer = osmium.SimpleWriter(outFileo5m)
        way_parser = WayHandler(all_ways, way_writer)
        way_parser.apply_file(val['map_file'], locations=True)
        #way_parser.apply_file(os.path.join(OUT_PATH, f'outFileFiltered-{key}.o5m'), locations=True)
        way_writer.close()
        
if generate_elevation == 1:
    print('\n\n# Generate elevation data PBF')
    TileCount = 1
    for tile in country:
        outFile = os.path.join(
            OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', 'elevation')
        elevation_files = glob.glob(os.path.join(
            OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', 'elevation*.pbf'))
        # print (f'\nElevation_files = {elevation_files}')
        if not elevation_files or Force_Processing == 1:
            print(
                f'# Generate elevation {TileCount} of {len(country)} for Coordinates: {tile["x"]} {tile["y"]}')
            cmd = ['pyhgtmap']
            cmd.append('-a '+f'{tile["left"]}' + ':' + f'{tile["bottom"]}' +
                       ':' + f'{tile["right"]}' + ':' + f'{tile["top"]}')
            # Old initial version
            cmd.extend(['-o', f'{outFile}', '-s 10', '-c 100,50', '--source=sonn1,view1', '--simplifyContoursEpsilon=0.00001', '--pbf', '--jobs=15',
						'--start-node-id=20000000000','--max-nodes-per-tile=0',
						'--max-nodes-per-way=200', '--start-way-id=21000000000', '--write-timestamp', '--no-zero-contour'])
			#cmd.extend(['-o', f'{outFile}', '-s 10', '-c 100,50', '--source=view1', '--simplifyContoursEpsilon=0.00001', '--pbf', '--jobs=15',
			#			'--start-node-id=20000000000','--max-nodes-per-tile=0', '--srtm 1',
			#			'--max-nodes-per-way=200', '--start-way-id=21000000000', '--write-timestamp', '--no-zero-contour'])
            #cmd.extend(['-o', f'{outFile}', '-s 10', '-c 100,50', '--source=srtm1,view1,view3,srtm3', '--pbf', '--jobs=15', '--viewfinder-mask=1', '--start-node-id=20000000000', '--max-nodes-per-tile=0',
            #           '--start-way-id=21000000000', '--write-timestamp', '--no-zero-contour', '--earthexplorer-user='+f'{earthexplorer_user}', '--earthexplorer-password='+f'{earthexplorer_password}'])
            #cmd.extend(['-o', f'{outFile}', '-s 10', '-c 100,50', '--source=view1,srtm1,view3,srtm3', '--pbf', '--jobs=15', '--viewfinder-mask=1', '--start-node-id=20000000000', '--max-nodes-per-tile=0',
            #           '--start-way-id=21000000000', '--write-timestamp', '--no-zero-contour', '--earthexplorer-user='+f'{earthexplorer_user}', '--earthexplorer-password='+f'{earthexplorer_password}'])
            # print(cmd)
            # sys.exit()
            result = subprocess.run(cmd, check=True)
            if result.returncode != 0:
                print(
                    f'Error in phyghtmap with tile: {tile["x"]}, {tile["y"]}')
                sys.exit()
        TileCount += 1
    sys.exit()

