#!/usr/bin/python
#-*- coding:utf-8 -*-

import getopt
import glob
import json
import multiprocessing
import os
import os.path
import requests
import subprocess
import sys
import time
import geojson
import math
import shutil

from shapely.geometry import Polygon, Point, MultiPolygon, shape
#from osgeo import ogr, osr, gdal

########### Configurable Parameters

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
threads = '1'
#print(f'threads = {threads}/n')

# Number of workers for the Osmosis read binary fast function
workers = '4'

# Keep (1) or delete (0) the country/region map folders after compression
#keep_folders = 0
keep_folders = 1
generate_elevation = 1
integrate_Wandrer = 1

########### End of Configurable Parameters

# Valhalla (routing) vars and functions
valhalla_tiles = [{'level': 2, 'size': 0.25}]
LEVEL_BITS = 3
TILE_INDEX_BITS = 22
ID_INDEX_BITS = 21
LEVEL_MASK = (2**LEVEL_BITS) - 1
TILE_INDEX_MASK = (2**TILE_INDEX_BITS) - 1
ID_INDEX_MASK = (2**ID_INDEX_BITS) - 1
INVALID_ID = (ID_INDEX_MASK << (TILE_INDEX_BITS + LEVEL_BITS)) | (TILE_INDEX_MASK << LEVEL_BITS) | LEVEL_MASK

def get_tile_level(id):
  return id & LEVEL_MASK

def get_tile_index(id):
  return (id >> LEVEL_BITS) & TILE_INDEX_MASK

def get_index(id):
  return (id >> (LEVEL_BITS + TILE_INDEX_BITS)) & ID_INDEX_MASK

def tiles_for_bounding_box(left, bottom, right, top):
  #if this is crossing the anti meridian split it up and combine
  if left > right:
    east = tiles_for_bounding_box(left, bottom, 180.0, top)
    west = tiles_for_bounding_box(-180.0, bottom, right, top)
    return east + west
  #move these so we can compute percentages
  left += 180
  right += 180
  bottom += 90
  top += 90
  tiles = []
  #for each size of tile
  for tile_set in valhalla_tiles:
    #for each column
    for x in range(int(left/tile_set['size']), int(right/tile_set['size']) + 1):
      #for each row
      for y in range(int(bottom/tile_set['size']), int(top/tile_set['size']) + 1):
        #give back the level and the tile index
        tiles.append((tile_set['level'], int(y * (360.0/tile_set['size']) + x)))
  return tiles
# End Valhalla

def get_tile_id(tile_level, lat, lon):
  level = list(filter(lambda x: x['level'] == tile_level, valhalla_tiles))[0]
  width = int(360 / level['size'])
  return int((lat + 90) / level['size']) * width + int((lon + 180 ) / level['size'])

def get_ll(id):
  tile_level = get_tile_level(id)
  tile_index = get_tile_index(id)
  level = list(filter(lambda x: x['level'] == tile_level, valhalla_tiles))[0]
  width = int(360 / level['size'])
  height = int(180 / level['size'])
  return int(tile_index / width) * level['size'] - 90, (tile_index % width) * level['size'] - 180

# Convert on./lat. to tile numbers
def deg2num(lat_deg, lon_deg, zoom=8):
  lat_rad = math.radians(lat_deg)
  n = 2.0 ** zoom
  xtile = int((lon_deg + 180.0) / 360.0 * n)
  ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
  return (xtile, ytile)

# Convert tile numbers to lon./lat.
def num2deg(xtile, ytile, zoom=8):
  n = 2.0 ** zoom
  lon_deg = xtile / n * 360.0 - 180.0
  lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * ytile / n)))
  lat_deg = math.degrees(lat_rad)
  return (lat_deg, lon_deg)

# Get the Geofabrik outline of the desired country/region from the Geofabrik json file and the download url of the map.
# input parameter is the name of the desired country/region as use by Geofabric in their json file.
def geom(wanted):
    with open('geofabrik.json', encoding='utf8') as f:
        data = geojson.load(f)
    f.close()

    # loop through all entries in the json file to find the one we want
    for x in data.features:
        props = x.properties
        id = props.get('id', '')
        if id != wanted:
            continue
        #print (props.get('urls', ''))
        wurls = props.get('urls', '')
        return (x.geometry, wurls.get('pbf', ''))
    return None, None

# Get the map download url from a region with the already loaded json data
def find_geofbrik_url (name, geofabrik_json):
    for x in geofabrik_json.features:
        props = x.properties
        id = props.get('id', '')
        if id != name:
            continue
        #print (props.get('urls', ''))
        wurls = props.get('urls', '')
        return (wurls.get('pbf', ''))
    return None
    
# Get the parent map/region of a region from the already loaded json data
def find_geofbrik_parent (name, geofabrik_json):
    for x in geofabrik_json.features:
        props = x.properties
        id = props.get('id', '')
        if id != name:
            continue
        return (props.get ('parent', ''), props.get ('id', ''))
    return None, None

# Find the maps to download from Geofabrik for a given range of tiles
# arguments are 
#   - list of tiles of the desired region bounding box
#   - name of desired region as used in Geofabrik json file
#   - polygon of desired region as present in the Geofabrik json file
def find_needed_countries(bbox_tiles, wanted_map, wanted_region_polygon):
    output = []
    
    with open('geofabrik.json', encoding='utf8') as f:
        geofabrik_json_data = geojson.load(f)
    f.close()

    # itterate through tiles and find Geofabrik regions that are in the tiles
    counter = 1
    for tile in bbox_tiles:
        # Do progress indicator every 50 tiles
        if counter % 50 == 0:
            print (f'Processing tile {counter} of {len(bbox_tiles)+1}',end='\r')
        counter += 1
        
        parent_added = 0
        force_added = 0
        
        # example contents of tile: {'index': 0, 'x': 130, 'y': 84, 'tile_left': 2.8125, 'tile_top': 52.48278022207821, 'tile_right': 4.21875, 'tile_bottom': 51.6180165487737}
        # convert tile x/y to tile polygon lon/lat
        poly = Polygon([(tile["tile_left"], tile["tile_top"]), (tile["tile_right"], tile["tile_top"]), (tile["tile_right"], tile["tile_bottom"]), (tile["tile_left"], tile["tile_bottom"]), (tile["tile_left"], tile["tile_top"])])

        # (re)initialize list of needed maps and their url's
        must_download_maps = []
        must_download_urls = []

        # itterate through countries/regions in the geofabrik json file
        for regions in geofabrik_json_data.features:
            props = regions.properties
            parent = props.get ('parent', '')
            regionname = props.get('id', '')
            rurls = props.get('urls', '')
            rurl = rurls.get('pbf', '')
            rgeom = regions.geometry
            rshape = shape(rgeom)
            
            #print (f'Processing region: {regionname}')
            
            # check if the region we are processing is needed for the tile we are processing
            
            # If currently processing country/region IS the desired country/region 
            if regionname == wanted_map:
                # Check if it is part of the tile we are processing
                if rshape.intersects(poly): # if so 
                    if regionname not in must_download_maps and regionname not in geofabrik_regions:
                        must_download_maps.append (regionname)
                        must_download_urls.append (rurl)
                    # if there is an intersect, force the tile to be put in the output        
                    force_added = 1
                else: # currently processing tile does not contain, a part of, the desired region
                    continue
            
            # currently processing country/region is NOT the desired country/region but might be in the tile (neighbouring country)
            if regionname != wanted_map:
                # check if we are processing a country or a sub-region. For countries only process other countries. also block special geofabrik sub regions
                if parent in geofabrik_regions and regionname not in block_download and regionname not in geofabrik_regions: # processing a country and no special sub-region
                    # Check if rshape is a part of the tile
                    if rshape.intersects(poly):
                        #print(f'\tintersecting tile: {regionname} tile={tile}')
                        if regionname not in must_download_maps:
                            must_download_maps.append (regionname)
                            must_download_urls.append (rurl)
        
        # If this tile contains the desired region, add it to the output 
        #print (f'map= {wanted_map}\tmust_download= {must_download_maps}\tparent_added= {parent_added}\tforce_added= {force_added}')
        if wanted_map in must_download_maps or parent_added == 1 or force_added == 1:
            # first replace any forward slashes with underscores (us/texas to us_texas)
            must_download_maps = [sub.replace('/', '_') for sub in must_download_maps]
            output.append ({'x':tile['x'], 'y':tile['y'], 'left':tile['tile_left'], 'top':tile['tile_top'], 'right':tile['tile_right'], 'bottom':tile['tile_bottom'], 'countries':must_download_maps, 'urls':must_download_urls})
        #print (f'\nmust_download: {must_download_maps}')    
        #print (f'must_download: {must_download_urls}')
    return output

CurDir = os.getcwd() # Get Current Directory

MAP_PATH = os.path.join (CurDir, 'Maps')
OUT_PATH = os.path.join (CurDir, 'Output')
land_polygons_file = os.path.join (CurDir, 'land-polygons-split-4326', 'land_polygons.shp')
geofabrik_json_file = os.path.join (CurDir, 'geofabrik.json')
geofabrik_regions = ['africa', 'antarctica', 'asia', 'australia-oceania', 'baden-wuerttemberg', 'bayern', 'brazil', 'california', 'canada', 'central-america', 'europe', 'france', 'germany', 'great-britain', 'india', 'indonesia', 'italy', 'japan', 'netherlands', 'nordrhein-westfalen', 'north-america', 'poland', 'russia', 'south-america', 'spain', 'us']

# List of regions to block. these regions are "collections" of other countries/regions/states 
block_download = ['dach', 'alps', 'britain-and-ireland', 'south-africa', 'us-midwest', 'us-northeast', 'us-pacific', 'us-south', 'us-west']

# Special_regions like (former) colonies where the map of the wanted region is not present in the map of the parent country.
# example Guadeloupe, it's Geofabrik parent country is France but Guadeloupe is not located within the region covered by the map of France.
special_regions = ['guadeloupe', 'guyane', 'martinique', 'mayotte', 'reunion']
url = ''
Map_File_Deleted = 0

# Tags to keep
# Too keep in mind!!! The merging step merges objects (node/way/relation) NOT tags of multiple identical objects
# So for a ferry route with OSM id=123 tag bicycle=yes, route=ferry and name=test Merging the split map files in the order of objects_to_keep_without_name and than 
# objects_to_keep_with_name the resulting merged file will for node 123 have just bicycle=yes
# Merging first objects_to_keep_with_name and than objects_to_keep_without_name results in a merged file with route=ferry and name=test in the merged output file
# 
# If somebody knows of a way to do a tag merge, so the resulting id 123 having all 3 tags, please, pretty please, let me know! 

# Objects (node/way/relation) to keep with just this tags, discard (almost) all other tags
objects_to_keep_without_name = 'access=private \
    admin_level=2 \
    aeroway=aerodrome =airport =gate =helipad \
    amenity=atm =bar =bench =bicycle_rental =biergarten =bus_station =cafe =drinking_water =fast_food =fuel =hospital =ice_cream =pharmacy =police =pub =restaurant =shelter =telephone =toilets \
    area=yes \
    bicycle= \
    bridge= \
    building=church =cathedral \
    emergency=phone \
    foot=ft_yes =foot_designated \
    highway=abandoned =bus_guideway =disused =bridleway =byway =construction =cycleway =footway =living_street =motorway =motorway_link =path =pedestrian =primary =primary_link =residential =road =secondary =secondary_link =service =steps =tertiary =tertiary_link =track =trunk =trunk_link =unclassified \
    historic=memorial =monument \
    landuse=forest =building =commercial =industrial =military =residential =reservoir =retail \
    leisure=picnic_table \
    natural=coastline =nosea =sea =beach =land =scrub =water =wetland =wood =spring \
    man_made=cutline =pier \
    place=isolated_dwelling =islet =square \
    railway=abandoned =bus_guideway =disused =funicular =halt =light_rail =miniature =monorail =narrow_gauge =platform =preserved =rail =station =stop =subway =tram \
    shop=bakery =bicycle =laundry =mall =supermarket \
    shelter_type=picnic_shelter \
    station=light_rail =subway =halt =stop\
    surface= \
    tourism=alpine_hut =attraction =hostel =hotel =information =viewpoint \
    tracktype= \
    tunnel= \
    wandrer= \
    waterway=drain =stream =riverbank \
    wood=deciduous'

# Objects (node/way/relation) to keep with just this tags AND the name and ele(vation) tag if present , discard (almost) all other tags
objects_to_keep_with_name = 'historic=ruins =castle \
    leisure=park =nature_reserve \
    mountain_pass= \
    natural=peak =volcano \
    place=city =hamlet =island =locality =suburb =town =village =country \
    route=ferry \
    tourism=museum =zoo \
    waterway=canal =river'

if len(sys.argv) != 2:
    print(f'Usage: {sys.argv[0]} Geofabrik Country or Region name.')
    sys.exit()

wanted_map = sys.argv[1]
# replace spaces in wanted_map with geofabrik minuses 
wanted_map = wanted_map.replace(" ","-")
wanted_map=wanted_map.lower()

if generate_elevation == 1:
    try:
        with open('account.json', encoding='utf8') as f:
            accounts = geojson.load(f)
        f.close()
        #print (accounts)
        earthexplorer_user = accounts['earthexplorer-user']
        earthexplorer_password = accounts['earthexplorer-password']
    except:
        print ("Could not read account.json file. Edit the account.json file.")
        accounts = {
            "earthexplorer-user": "Username",
            "earthexplorer-password": "Password"
            }
        f = open('account.json','w', encoding='utf8')
        f.write(geojson.dumps(accounts, indent=4))
        f.close()
        sys.exit()

# is geofabrik json file present and not older then Max_Days_Old?
now = time.time()
To_Old = now - 60 * 60 * 24 * Max_Days_Old
try:
    FileCreation = os.path.getmtime(geofabrik_json_file)
    if FileCreation < To_Old:
        print (f'# Deleting old Geofabriks json file')
        os.remove(os.path.join (CurDir, 'geofabrik.json'))
        #Force_Processing = 1
except:
    pass
    #Force_Processing = 1
    
# if not present download Geofabriks json file
if not os.path.exists(geofabrik_json_file) or not os.path.isfile(geofabrik_json_file) or Force_Processing == 1:
    print('# Downloading Geofabrik json file')
    url = 'https://download.geofabrik.de/index-v1.json'
    r = requests.get(url, allow_redirects=True, stream = True)
    if r.status_code != 200:
        print(f'failed to find or download Geofabrik json file')
        sys.exit()
    Download=open(os.path.join (CurDir, 'geofabrik.json'), 'wb')
    for chunk in r.iter_content(chunk_size=10240):
        Download.write(chunk)
    Download.close()

# Check if wanted_map is in the json file and if so get the polygon (shape)
wanted_map_geom, wanted_url = geom(wanted_map)
if not wanted_map_geom:
    # try to prepend us\ to the wanted_map
    wanted_map_geom, wanted_url = geom('us/'+wanted_map)
    if wanted_map_geom:
        wanted_map='us/'+wanted_map
    else:
        print(f'failed to find country or region {wanted_map} in Geofabrik json file')
        sys.exit()
#print(f'geom={wanted_map_geom}, url={wanted_url}')
#sys.exit()

# convert to shape (multipolygon) of the desired area
wanted_region = shape(wanted_map_geom)
#print (f'shape = {wanted_region}')

# get bounding box of the disired area
(bbox_left, bbox_bottom, bbox_right, bbox_top) = wanted_region.bounds
#print(f'bbox={bbox_left},{bbox_top},{bbox_right},{bbox_bottom}')
#print(f'bbox=min_x:{bbox_left}, max_y{bbox_top}, max_x{bbox_right}, min_y{bbox_bottom}')
#sys.exit()

# convert bounding box from coordinates to slippy tiles
(top_x,top_y)=deg2num(bbox_top,bbox_left)
(bot_x,bot_y)=deg2num(bbox_bottom,bbox_right)
#print (f'voor tx {top_x}, ty {top_y} - bx {bot_x}, by {bot_y}')

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
#print (f'na tx {top_x}, ty {top_y} - bx {bot_x}, by {bot_y}')
#sys.exit()

# Build list of tiles to process from the bounding box
bbox_tiles=[]
for x in range(top_x, bot_x + 1):
    for y in range(top_y, bot_y + 1):
        (tile_top,tile_left)=num2deg(x, y)
        (tile_bottom,tile_right)=num2deg(x+1, y+1)
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
        bbox_tiles.append ({'x':x, 'y':y, 'tile_left':tile_left, 'tile_top':tile_top, 'tile_right':tile_right, 'tile_bottom':tile_bottom})

# Get a list of the Geofabrik maps needed to process these tiles
print (f'\nSearching for needed maps, this can take a while.\n')
country = find_needed_countries (bbox_tiles, wanted_map, wanted_region)
#print (f'Country= {country}')
#sys.exit()

# Check for expired land polygons file and delete it
print('\n\n# check land_polygons.shp file')
now = time.time()
To_Old = now - 60 * 60 * 24 * Max_Days_Old
try:
    FileCreation = os.path.getmtime(land_polygons_file)
    if FileCreation < To_Old:
        print (f'# Deleting old land polygons file')
        os.remove(os.path.join (CurDir, 'land-polygons-split-4326', 'land_polygons.shp'))
        #Force_Processing = 1
except:
    pass
    #Force_Processing = 1

# If land polygons file does not exist or or Force_Processing active, (re)download it
if not os.path.exists(land_polygons_file) or not os.path.isfile(land_polygons_file) or Force_Processing == 1:
    print('# Downloading land polygons file')
    url = 'https://osmdata.openstreetmap.de/download/land-polygons-split-4326.zip'
    r = requests.get(url, allow_redirects=True, stream = True)
    if r.status_code != 200:
        print(f'failed to find or download land polygons file')
        sys.exit()
    Download=open(os.path.join (CurDir, 'land-polygons-split-4326.zip'), 'wb')
    for chunk in r.iter_content(chunk_size=10240):
        Download.write(chunk)
    Download.close()
    # unpack it
    cmd = ['7za', 'x', '-y', os.path.join (CurDir, 'land-polygons-split-4326.zip')]
    #print(cmd)
    result = subprocess.run(cmd)
    os.remove(os.path.join (CurDir, 'land-polygons-split-4326.zip'))
    if result.returncode != 0:
        print(f'Error unpacking land polygons file')
        sys.exit()

print('\n\n# check countries .osm.pbf files')
# Build list of countries needed
border_countries = {}
for tile in country:
    for c in tile['countries']:
        if c not in border_countries:
            border_countries[c] = {'map_file':c}

print (f'{border_countries}')
#sys.exit()
#time.sleep(60)

# Check for expired maps and delete them
print(f'# Checking for old maps and remove them')
now = time.time()
To_Old = now - 60 * 60 * 24 * Max_Days_Old
for c in border_countries:
#    map_files = glob.glob(f'{MAP_PATH}/{c}*.osm.pbf')
    # Prevent matching to multiple maps like australia and australia-oceania
    map_files = glob.glob(f'{MAP_PATH}/{c}-latest.osm.pbf')
    if len(map_files) != 1:
        #map_files = glob.glob(f'{MAP_PATH}/**/{c}*.osm.pbf')
        # Prevent matching to multiple maps like australia and australia-oceania
        map_files = glob.glob(f'{MAP_PATH}/**/{c}-latest.osm.pbf') 
    if len(map_files) == 1 and os.path.isfile(map_files[0]):
        FileCreation = os.path.getmtime(map_files[0])
        if FileCreation < To_Old or Force_Processing == 1:
            print (f'# Deleting old map of {c} {map_files},{FileCreation},{To_Old},{Force_Processing}')
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

    print (f'Tile = {tile}')
    # search for and download missing map files from Geofabrik 
    i = 0
    for c in tile['countries']: # we want this osm.pbf
        if c not in border_countries: # and it is not yet in our list of files
            print(f'# Checking mapfile for {c}')
            #map_files = glob.glob(f'{MAP_PATH}/{c}*.osm.pbf') # Search for it in the map folder
            # Prevent matching to multiple maps like australia and australia-oceania
            map_files = glob.glob(f'{MAP_PATH}/{c}-latest.osm.pbf') # Search for it in the map folder
            if len(map_files) != 1: # not found in the map folder
                #map_files = glob.glob(f'{MAP_PATH}/**/{c}*.osm.pbf') # and the sub-folders of the map folder
                # Prevent matching to multiple maps like australia and australia-oceania
                map_files = glob.glob(f'{MAP_PATH}/**/{c}-latest.osm.pbf') # and the sub-folders of the map folder
            if len(map_files) != 1 or not os.path.isfile(map_files[0]): # Also not found in a sub folder so download it
                print(f'# Trying to download missing map of {c}.')
                url = tile['urls'][i]
                r = requests.get(url, allow_redirects=True, stream = True)
                if r.status_code != 200:
                    print(f'failed to find or download country: {c}')
                    sys.exit()
                Download=open(os.path.join (MAP_PATH, f'{c}' + '-latest.osm.pbf'), 'wb')
                for chunk in r.iter_content(chunk_size=10240):
                    Download.write(chunk)
                Download.close()
                map_files = [os.path.join (MAP_PATH, f'{c}' + '-latest.osm.pbf')]
                print(f'# Map of {c} downloaded.')
            border_countries[c] = {'map_file':map_files[0]}
        i += 1

# Filter out any keys/values we are not going to use. this prevents out-of-memory errors when creating the map files and speeds up the splitting out of tiles
# Smaller files spilt faster!!
print('\n\n# filter tags from country osm.pbf files')
for key, val  in border_countries.items():
    # print(key, val)
#    outFile = os.path.join(OUT_PATH, f'filtered-{key}.osm.pbf')
#    outFileNames = os.path.join(OUT_PATH, f'filtered-{key}Names.osm.pbf')
    outFileo5m = os.path.join(OUT_PATH, f'outFile-{key}.o5m')
    outFileo5mFiltered = os.path.join(OUT_PATH, f'outFileFiltered-{key}.o5m')
    outFileo5mFilteredNames = os.path.join(OUT_PATH, f'outFileFiltered-{key}Names.o5m')
    
    # Convert osm.pbf file to o5m for processing with osmfilter
    # print(outFile)
    if not os.path.isfile(outFileo5mFiltered) or Force_Processing == 1:
        #print('! create filtered country file(s)')
        print(f'\n\n# Converting map of {key} to o5m format')
        cmd = ['osmconvert']
        cmd.extend(['-v', '--hash-memory=2500', '--complete-ways', '--complete-multipolygons', '--complete-boundaries', '--drop-author', '--drop-version'])
        cmd.append(val['map_file'])
        cmd.append('-o='+outFileo5m)
        # print(cmd)
        result = subprocess.run(cmd)
        if result.returncode != 0:
            print(f'Error in OSMConvert with country: {c}')
            sys.exit()
		
		# Keep keys/values we want to have without keeping the name key (saving space in the map file)
        print(f'\n\n# Filtering unwanted map objects out of map of {key}')
        cmd = ['osmfilter']
        cmd.append(outFileo5m)
        cmd.append('--verbose')
        cmd.append('--keep='+objects_to_keep_without_name)
        cmd.append('--keep-tags=all type= layer= '+objects_to_keep_without_name)
        #cmd.append('--drop-relations')
        cmd.append('-o='+outFileo5mFiltered)
        # print(cmd)
        result = subprocess.run(cmd)
        if result.returncode != 0:
            print(f'Error in OSMFilter with country: {c}')
            sys.exit()
        
        # Keep keys/values we want with the name key (cities etc)
        cmd = ['osmfilter']
        cmd.append(outFileo5m)
        cmd.append('--verbose')
        cmd.append('--keep='+objects_to_keep_with_name)
        #cmd.append('--keep-tags=all name= type= '+objects_to_keep_without_name)
        cmd.append('--keep-tags=all type= name= layer= ele= '+objects_to_keep_with_name)
        cmd.append('-o='+outFileo5mFilteredNames)
        # print(cmd)
        result = subprocess.run(cmd)
        if result.returncode != 0:
            print(f'Error in OSMFilter with country: {c}')
            sys.exit()

        os.remove(outFileo5m)
#        os.remove(outFileo5mFiltered)
#        os.remove(outFileo5mFilteredNames)
								
    border_countries[key]['filtered_file'] = outFileo5mFiltered
    border_countries[key]['filtered_fileNames'] = outFileo5mFilteredNames

print('\n\n# Generate land')
TileCount = 1
for tile in country:
    landFile = os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'land.shp')
    outFile = os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'land')

    if not os.path.isfile(landFile) or Force_Processing == 1:
        print(f'\n\n# Generate land {TileCount} of {len(country)} for Coordinates: {tile["x"]} {tile["y"]}')
        cmd = ['ogr2ogr', '-overwrite', '-skipfailures']
        # Try to prevent getting outside of the +/-180 and +/- 90 degrees borders. Normally the +/- 0.1 are there to prevent white lines at border borders. 
        if tile["x"] == 255 or tile["y"] == 255 or tile["x"] == 0 or tile["y"] == 0:
            cmd.extend(['-spat', f'{tile["left"]:.6f}',
                        f'{tile["bottom"]:.6f}',
                        f'{tile["right"]:.6f}',
                        f'{tile["top"]:.6f}'])
        else:
            cmd.extend(['-spat', f'{tile["left"]-0.1:.6f}',
                        f'{tile["bottom"]-0.1:.6f}',
                        f'{tile["right"]+0.1:.6f}',
                        f'{tile["top"]+0.1:.6f}'])           
        cmd.append(landFile)
        cmd.append(land_polygons_file)
        # print(cmd)
        subprocess.run(cmd)

    if not os.path.isfile(outFile+'1.osm') or Force_Processing == 1:
        cmd = ['python', 'shape2osm.py', '-l', outFile, landFile]
        # print(cmd)
        subprocess.run(cmd)
    TileCount += 1

print('\n\n# Generate sea')
TileCount = 1
for tile in country:
    outFile = os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'sea.osm')
    if not os.path.isfile(outFile) or Force_Processing == 1:
        print(f'# Generate sea {TileCount} of {len(country)} for Coordinates: {tile["x"]} {tile["y"]}')
        with open('sea.osm') as f:
            sea_data = f.read()
            f.close()

            # Try to prevent getting outside of the +/-180 and +/- 90 degrees borders. Normally the +/- 0.1 are there to prevent white lines at tile borders
            if tile["x"] == 255 or tile["y"] == 255 or tile["x"] == 0 or tile["y"] == 0:
                sea_data = sea_data.replace('$LEFT', f'{tile["left"]:.6f}')
                sea_data = sea_data.replace('$BOTTOM',f'{tile["bottom"]:.6f}')
                sea_data = sea_data.replace('$RIGHT',f'{tile["right"]:.6f}')
                sea_data = sea_data.replace('$TOP',f'{tile["top"]:.6f}')
            else:
                sea_data = sea_data.replace('$LEFT', f'{tile["left"]-0.1:.6f}')
                sea_data = sea_data.replace('$BOTTOM',f'{tile["bottom"]-0.1:.6f}')
                sea_data = sea_data.replace('$RIGHT',f'{tile["right"]+0.1:.6f}')
                sea_data = sea_data.replace('$TOP',f'{tile["top"]+0.1:.6f}')

            with open(outFile, 'w') as of:
                of.write(sea_data)
                of.close()
    TileCount += 1
    
if (generate_elevation == 1):    
    print('\n\n# Generate elevation data PBF')
    TileCount = 1
    for tile in country:
        outFile = os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'elevation')
        elevation_files = glob.glob(os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'elevation*.pbf'))
        if not elevation_files or Force_Processing == 1:
            print(f'# Generate elevation {TileCount} of {len(country)} for Coordinates: {tile["x"]} {tile["y"]}')
            cmd = ['phyghtmap']
            cmd.append('-a '+f'{tile["left"]}' + ':' + f'{tile["bottom"]}' + ':' + f'{tile["right"]}' + ':' + f'{tile["top"]}')
            cmd.extend(['-o', f'{outFile}','-s 10', '-c 100,50','--source=view1,view3,srtm3', '--pbf', '--jobs=15', '--viewfinder-mask=1', '--start-node-id=20000000000','--max-nodes-per-tile=0','--start-way-id=2000000000','--write-timestamp', '--no-zero-contour', '--earthexplorer-user='+f'{earthexplorer_user}','--earthexplorer-password='+f'{earthexplorer_password}'])
            #print(cmd)
            #sys.exit()
            result = subprocess.run(cmd)
            if result.returncode != 0:
                print(f'Error in phyghtmap with country: {c}')
                sys.exit()      
        TileCount += 1    
    #sys.exit()
    #print('\n\n# Generate elevation data OSM')
    #TileCount = 1
    #for tile in country:
    #    outFile = os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'elevation')
    #    elevation_files = glob.glob(os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'elevation*.osm'))
    #    if not elevation_files or Force_Processing == 1:
    #        print(f'# Generate elevation {TileCount} of {len(country)} for Coordinates: {tile["x"]} {tile["y"]}')
    #        cmd = ['phyghtmap']
    #        cmd.append('-a '+f'{tile["left"]}' + ':' + f'{tile["bottom"]}' + ':' + f'{tile["right"]}' + ':' + f'{tile["top"]}')
    #        cmd.extend(['-o', f'{outFile}','-s 10', '-c 100,50','--source=view1,view3,srtm3', '--jobs=15', '--viewfinder-mask=1', '--start-node-id=20000000000','--max-nodes-per-tile=0','--start-way-id=2000000000','--write-timestamp', '--no-zero-contour', '--earthexplorer-user='+f'{earthexplorer_user}','--earthexplorer-password='+f'{earthexplorer_password}'])
    #        #print(cmd)
    #        #sys.exit()
    #        result = subprocess.run(cmd)
    #        if result.returncode != 0:
    #            print(f'Error in phyghtmap with country: {c}')
    #            sys.exit()      
    #    TileCount += 1    
    ##sys.exit()
    
print('\n\n# Split filtered country files to tiles')

# Check if there is a wandrer map
if integrate_Wandrer:
    inWandrer_files = glob.glob(os.path.join(MAP_PATH, f'wandrer*.osm.pbf'))
    if inWandrer_files and integrate_Wandrer:
        doWandrer=True
    else:
        doWandrer=False
TileCount = 1
for tile in country:
    for c in tile['countries']:
        print(f'\n\n# Splitting tile {TileCount} of {len(country)} for Coordinates: {tile["x"]},{tile["y"]} from map of {c}')
        outFile = os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'split-{c}.osm.pbf')
        outMerged = os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'merged.osm.pbf')
        if not os.path.isfile(outMerged) or Force_Processing == 1:
            #cmd = ['.\\osmosis\\bin\\osmosis.bat', '--rbf',border_countries[c]['filtered_file'],'workers='+workers, '--buffer', 'bufferCapacity=12000', '--bounding-box', 'completeWays=yes', 'completeRelations=yes']
            #cmd.extend(['left='+f'{tile["left"]}', 'bottom='+f'{tile["bottom"]}', 'right='+f'{tile["right"]}', 'top='+f'{tile["top"]}', '--buffer', 'bufferCapacity=12000', '--wb'])
            #cmd.append('file='+outFile)
            #cmd.append('omitmetadata=true')
            cmd = ['osmconvert', '-v', '--hash-memory=2500']
            cmd.append('-b='+f'{tile["left"]}' + ',' + f'{tile["bottom"]}' + ',' + f'{tile["right"]}' + ',' + f'{tile["top"]}')
            cmd.extend(['--complete-ways', '--complete-multipolygons', '--complete-boundaries'])
            cmd.append(border_countries[c]['filtered_file'])
            cmd.append('-o='+outFile)
            # print(cmd)
            result = subprocess.run(cmd)
            if result.returncode != 0:
                print(f'Error in Osmconvert with country: {c}')
                sys.exit()            
            # print(border_countries[c]['filtered_file'])
            
        outFile = os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'split-{c}Names.osm.pbf')
#        if not os.path.isfile(outFile) or Force_Processing == 1:
        if not os.path.isfile(outMerged) or Force_Processing == 1:
            #cmd = ['.\\osmosis\\bin\\osmosis.bat', '--rbf',border_countries[c]['filtered_file'],'workers='+workers, '--buffer', 'bufferCapacity=12000', '--bounding-box', 'completeWays=yes', 'completeRelations=yes']
            #cmd.extend(['left='+f'{tile["left"]}', 'bottom='+f'{tile["bottom"]}', 'right='+f'{tile["right"]}', 'top='+f'{tile["top"]}', '--buffer', 'bufferCapacity=12000', '--wb'])
            #cmd.append('file='+outFile)
            #cmd.append('omitmetadata=true')
            cmd = ['osmconvert', '-v', '--hash-memory=2500']
            cmd.append('-b='+f'{tile["left"]}' + ',' + f'{tile["bottom"]}' + ',' + f'{tile["right"]}' + ',' + f'{tile["top"]}')
            cmd.extend(['--complete-ways', '--complete-multipolygons', '--complete-boundaries'])
            cmd.append(border_countries[c]['filtered_fileNames'])
            cmd.append('-o='+outFile)
            # print(cmd)
            result = subprocess.run(cmd)
            if result.returncode != 0:
                print(f'Error in Osmconvert with country: {c}')
                sys.exit()            
            # print(border_countries[c]['filtered_file'])

        if doWandrer:
            for wandrer_map in inWandrer_files:
                outWandrer = os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'split-{os.path.basename(wandrer_map)}')
                if not os.path.isfile(outWandrer) or Force_Processing == 1:
                    cmd = ['osmconvert', '-v', '--hash-memory=2500']
                    cmd.append('-b='+f'{tile["left"]}' + ',' + f'{tile["bottom"]}' + ',' + f'{tile["right"]}' + ',' + f'{tile["top"]}')
                    cmd.extend(['--complete-ways', '--complete-multipolygons', '--complete-boundaries'])
                    cmd.append(wandrer_map)
                    cmd.append('-o='+outWandrer)
                    # print(cmd)
                    result = subprocess.run(cmd)
                    if result.returncode != 0:
                        print(f'Error in Osmconvert while processing Wandrer file')
                        sys.exit()            
    TileCount += 1

print('\n\n# Merge splitted tiles with land, sea and elevation')
TileCount = 1
for tile in country:
    print(f'\n\n# Merging tiles for tile {TileCount} of {len(country)} for Coordinates: {tile["x"]},{tile["y"]}')
    outFile = os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'merged.osm.pbf')
    if not os.path.isfile(outFile) or Force_Processing == 1:
        cmd = [os.path.join (CurDir, 'Osmosis', 'bin', 'osmosis.bat')]
        loop=0
        for c in tile['countries']:
            cmd.append('--rbf')
            cmd.append(os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'split-{c}Names.osm.pbf'))
            cmd.append('workers='+workers)
            if loop > 0:
                cmd.append('--merge')
            cmd.append('--rbf')
            cmd.append(os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'split-{c}.osm.pbf'))
            cmd.append('workers='+workers)
            cmd.append('--merge')
            loop+=1
        if (generate_elevation == 1):
            elevation_files = glob.glob(os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'elevation*.pbf'))
            for elevation in elevation_files:
                cmd.append('--rbf')
                cmd.append(os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'{elevation}'))
                cmd.append('workers='+workers)
                cmd.append('--merge')
        if (doWandrer):
            wandrer_files = glob.glob(os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'split-wandrer*.osm.pbf'))
            for wandrer in wandrer_files:
                cmd.append('--rbf')
                cmd.append(os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'{wandrer}'))
                cmd.append('workers='+workers)
                cmd.append('--merge')
        land_files = glob.glob(os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'land*.osm'))
        for land in land_files:
            cmd.extend(['--rx', 'file='+os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'{land}'), '--s', '--m'])
        cmd.extend(['--rx', 'file='+os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'sea.osm'), '--s', '--m'])
        cmd.extend(['--tag-transform', 'file=' + os.path.join (CurDir, 'tunnel-transform.xml'), '--buffer', '--wb', outFile, 'omitmetadata=true'])
        #print(cmd)
        result = subprocess.run(cmd)
        if result.returncode != 0:
            print(f'Error in Osmosis with country: {c}')
            sys.exit()   
    TileCount += 1

print('\n\n# Creating .map files')
TileCount = 1
for tile in country:
    print(f'\n\nCreating map file for tile {TileCount} of {len(country)} for Coordinates: {tile["x"]}, {tile["y"]}')
    outFile = os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}.map')
    if not os.path.isfile(outFile+'.lzma') or Force_Processing == 1:
        mergedFile = os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', 'merged.osm.pbf')
        cmd = [os.path.join (CurDir, 'Osmosis', 'bin', 'osmosis.bat'), '--rbf', mergedFile, 'workers='+workers, '--buffer', '--mw', 'file='+outFile]
        cmd.append(f'bbox={tile["bottom"]:.6f},{tile["left"]:.6f},{tile["top"]:.6f},{tile["right"]:.6f}')
        cmd.append('zoom-interval-conf=10,0,17')
        # cmd.append('way-clipping=false')
        cmd.append('threads='+threads)
        cmd.append('tag-conf-file=' + os.path.join (CurDir, 'tag-wahoo.xml'))
        # print(cmd)
        result = subprocess.run(cmd)
        if result.returncode != 0:
            print(f'Error in Osmosis with country: {c}')
            sys.exit()        

        print('\n# compress .map file')
        cmd = ['lzma', 'e', outFile, outFile+'.lzma', f'-mt{threads}', '-d27', '-fb273', '-eos']
        # print(cmd)
        subprocess.run(cmd)
        
    # Create "tile present" file
    f = open(outFile + '.lzma.17', 'wb')
    f.close()
        
    TileCount += 1

print('\n# zip .map.lzma files')

try:
    res = wanted_map.index('/')
    wanted_map = wanted_map[res+1:]
except:
    pass

# copy the needed tiles to the country folder
print (f'Copying Wahoo and map tiles to output folders')
for tile in country:
    src = os.path.join(f'{OUT_PATH}', f'{tile["x"]}', f'{tile["y"]}.map.lzma')
    dst = os.path.join(f'{OUT_PATH}', f'{wanted_map}', f'{tile["x"]}', f'{tile["y"]}.map.lzma')
    outdir = os.path.join(f'{OUT_PATH}', f'{wanted_map}', f'{tile["x"]}')
    if not os.path.isdir(outdir):
        os.makedirs(outdir)
    try:
        shutil.copy2(src, dst)
    except:
        print (f'Error copying tiles of country {wanted_map}')
        sys.exit()
        
    src = os.path.join(f'{OUT_PATH}', f'{tile["x"]}', f'{tile["y"]}.map.lzma.17')
    dst = os.path.join(f'{OUT_PATH}', f'{wanted_map}', f'{tile["x"]}', f'{tile["y"]}.map.lzma.17')
    outdir = os.path.join(f'{OUT_PATH}', f'{wanted_map}', f'{tile["x"]}')
    if not os.path.isdir(outdir):
        os.makedirs(outdir)
    try:
        shutil.copy2(src, dst)
    except:
        print (f'Error copying precense files of country {wanted_map}')
        sys.exit()
        
    src = os.path.join(f'{OUT_PATH}', f'{tile["x"]}', f'{tile["y"]}.map')
    dst = os.path.join(f'{OUT_PATH}', f'{wanted_map}-maps', f'{tile["x"]}', f'{tile["y"]}.map')
    outdir = os.path.join(f'{OUT_PATH}', f'{wanted_map}-maps', f'{tile["x"]}')
    if not os.path.isdir(outdir):
        os.makedirs(outdir)
    try:
        shutil.copy2(src, dst)
    except:
        print (f'Error copying map tiles of country {wanted_map}')
        sys.exit()

# Process routing tiles if present
IN_R_PATH = os.path.join(CurDir, f'valhalla_tiles', f'2', f'000')
rtile = None
if os.path.isdir(IN_R_PATH):
    # Calculate which routing tiles are needed
    routing_tiles = tiles_for_bounding_box(bbox_left,bbox_bottom,bbox_right,bbox_top)
    #print (f'Routing Tiles = {routing_tiles}')
    
    for rtile in routing_tiles:
        #print (f'rtile = {rtile[1]}')
        print('\n# compress routing tile file')
        inRFile = os.path.join(IN_R_PATH, f'{str(rtile[1])[0:3]}', f'{str(rtile[1])[3:6]}.gph')
        print(f'inRFile = {inRFile}')
        outRFile = os.path.join(OUT_PATH, f'{wanted_map}-routing', f'routing', f'2', f'000', f'{str(rtile[1])[0:3]}', f'{str(rtile[1])[3:6]}.gph')
        print(f'outRFile = {outRFile}')
        
        outdir = os.path.join(f'{OUT_PATH}', f'{wanted_map}-routing', f'routing', f'2', f'000', f'{str(rtile[1])[0:3]}')
        if not os.path.isdir(outdir):
            os.makedirs(outdir)
            #print(f'outdir = {outdir}')
        
        if os.path.isfile(inRFile):
            try:
                shutil.copy2(inRFile, outRFile)
            except:
                print (f'Error copying routing tile of country {wanted_map}')
                sys.exit()
        
            cmd = ['lzma', 'e', outRFile, outRFile+'.lzma', f'-mt{threads}', '-d27', '-fb273', '-eos']
            #print(cmd)
            subprocess.run(cmd)
        
            # Create "tile present" file
            f = open(outRFile + '.lzma.17', 'wb')
            f.close()
            
            # Remove copied routing tile
            try:
                os.remove (outRFile)
            except OSError as e:
                print(f'Error, could not delete routing tile ' + f'{outRFile}')
    

cmd = ['7za', 'a', '-tzip', wanted_map, os.path.join(f'{OUT_PATH}', f'{wanted_map}', f'*')]
subprocess.run(cmd, check=True, cwd=OUT_PATH)

cmd = ['7za', 'a', '-tzip', wanted_map + '-maps.zip', os.path.join(f'{OUT_PATH}', f'{wanted_map}-maps', f'*')]
subprocess.run(cmd, check=True, cwd=OUT_PATH)

# Compress routing tiles
if rtile:
    cmd = ['7za', 'a', '-tzip', wanted_map+f'-routing' , os.path.join(f'{OUT_PATH}', f'{wanted_map}-routing', f'*')]
    subprocess.run(cmd, check=True, cwd=OUT_PATH)

# if desired, delete the Wahoo and map folders after compression
if keep_folders == 0:
    try:
        shutil.rmtree(os.path.join(f'{OUT_PATH}', f'{wanted_map}'))
    except OSError as e:
        print(f'Error, could not delete folder ' + os.path.join(f'{OUT_PATH}', f'{wanted_map}'))
    try:
        shutil.rmtree(os.path.join(f'{OUT_PATH}', f'{wanted_map}-maps'))
    except OSError as e:
        print(f'Error, could not delete folder ' + os.path.join(f'{OUT_PATH}', f'{wanted_map}-maps'))
    if rtile:
        try:
            shutil.rmtree(os.path.join(f'{OUT_PATH}', f'{wanted_map}-routing'))
        except OSError as e:
            print(f'Error, could not delete folder ' + os.path.join(f'{OUT_PATH}', f'{wanted_map}-routing'))
