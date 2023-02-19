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
    threads = '1'
# Or set it manually to:
threads = '1'
#print(f'threads = {threads}/n')

# Number of workers for the Osmosis read binary fast function
workers = '1'

# Keep (1) or delete (0) the country/region map folders after compression
#keep_folders = 0
keep_folders = 1

########### End of Configurable Parameters

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

# Get the Geofabrik outline of the desired country/region from the Geofabrik json fiule and the download url of the map.
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
                    # catch special_regions like (former) colonies where the map of the region is not fysically in the map of the parent country.
                    # example Guadeloupe, it's parent country is France but Guadeloupe is not located within the region covered by the map of France
                    if wanted_map not in special_regions:
                        # If we are proseccing a sub-region add the parent of this sub-region to the must download list.
                        # This to prevent downloading several small regions AND it's containing region
                        if parent not in geofabrik_regions and regionname not in geofabrik_regions: # we are processing a sub-regiongo find the parent region
                            x=0
                            while parent not in geofabrik_regions: # handle sub-sub-regions like unterfranken->bayern->germany
                                parent,child = find_geofbrik_parent (parent, geofabrik_json_data)
                                if parent in geofabrik_regions:
                                    parent = child
                                    break
                                if x > 10: # prevent endless loop
                                    print (f'Can not find parent map of region: {regionname}')
                                    sys.exit()
                                x += 1
                            if parent not in must_download_maps:
                                must_download_maps.append (parent)
                                must_download_urls.append (find_geofbrik_url(parent, geofabrik_json_data))
                                #parent_added = 1
                        else:
                            if regionname not in must_download_maps:
                                must_download_maps.append (regionname)
                                must_download_urls.append (rurl)
                    else: 
                        # wanted_map is a special region like Guadeloupe, France
                        if regionname not in must_download_maps:
                            must_download_maps.append (regionname)
                            must_download_urls.append (rurl)
                    # if there is an intersect, force the tile to be put in the output        
                    force_added = 1
                else: # currently processing tile does not contain, a part of, the desired region
                    continue
            
            # currently processing country/region is NOT the desired country/region but might be in the tile (neighbouring country)
            if regionname != wanted_map:
                # check if we are processing a country or a sub-region. For countries only process other countries. also block special geofabrik sub regions
                if parent in geofabrik_regions and regionname not in block_download: # processing a country and no special sub-region
                    # check if rshape is subset of desired region. If so discard it
                    if wanted_region_polygon.contains(rshape):
                        #print (f'\t{regionname} is a subset of {wanted_map}, discard it')
                        continue
                    # check if rshape is a superset of desired region. if so discard it
                    if rshape.contains(wanted_region_polygon):
                        #print (f'\t{regionname} is a superset of {wanted_map}, discard it')
                        #if regionname not in must_download_maps:
                        #    must_download_maps.append (regionname)
                        #    must_download_urls.append (rurl)
                        #    parent_added = 1
                        continue
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
geofabrik_regions = ['africa', 'antarctica', 'asia', 'australia-oceania', 'central-america', 'europe', 'north-america', 'south-america', 'russia']

# List of regions to block. these regions are "collections" of other countries/regions/states 
block_download = ['dach', 'alps', 'britain-and-ireland', 'south-africa-and-lesotho', 'us-midwest', 'us-northeast', 'us-pacific', 'us-south', 'us-west', 'kaliningrad']

# Special_regions like (former) colonies where the map of the wanted region is not present in the map of the parent country.
# example Guadeloupe, it's Geofabrik parent country is France but Guadeloupe is not located within the region covered by the map of France.
special_regions = ['guadeloupe', 'guyane', 'martinique', 'mayotte', 'reunion']
url = ''
Map_File_Deleted = 0

# Tags to keep

## my version based on wahoo app render theme
#filtered_tags = 'access= \
#    bicycle= \
#    bridge= \
#    foot=ft_yes =foot_designated \
#    highway=abandoned =bus_guideway =disused =mini_roundabout =turning_circle =bridleway =byway =construction =cycleway =footway =living_street =motorway =motorway_link =path =pedestrian =primary =primary_link =raceway =residential =road =secondary =secondary_link =service =services =steps =tertiary =tertiary_link =track =trunk =trunk_link =unclassified \
#    natural=coastline =nosea =sea =beach =land =scrub =water =wetland =wood \
#    leisure=park =nature_reserve \
#    railway=abandoned =bus_guideway =disused =funicular =light_rail =miniature =narrow_gauge =platform =preserved =rail =subway =tram =halt =station \
#    surface= \
#    tf:difficulty= \
#    tracktype= \
#    tourism=attraction =camp_site =chalet =information =museum =picnic_site =viewpoint \
#    tunnel= \
#    waterway=canal =dam =drain =river =riverbank =stream \
#    wood=deciduous'
#    
#filtered_tags_with_name = 'admin_level=2 \
#    area= \
#    mountain_pass= \
#    natural= \
#    tourism= \
#    place=city =hamlet =island =isolated_dwelling =islet =locality =suburb =town =village =neighbourhood =neighborhood =country'

# my version based on wahoo app render theme
filtered_tags = 'access= \
    area=yes \
    bicycle= \
    bridge= \
    foot=ft_yes =foot_designated \
    highway=abandoned =bus_guideway =disused =bridleway =byway =construction =cycleway =footway =living_street =motorway =motorway_link =path =pedestrian =primary =primary_link =residential =road =secondary =secondary_link =service =steps =tertiary =tertiary_link =track =trunk =trunk_link =unclassified \
    natural=coastline =nosea =sea =beach =land =scrub =water =wetland =wood \
    leisure=park =nature_reserve \
    railway=abandoned =bus_guideway =disused =funicular =light_rail =miniature =narrow_gauge =preserved =rail =subway =tram \
    surface= \
    tracktype= \
    tunnel= \
    waterway=canal =drain =river =riverbank \
    wood=deciduous'
    
filtered_tags_with_name = 'admin_level=2 \
    area=yes \
    mountain_pass= \
    natural= \
    place=city =hamlet =island =isolated_dwelling =islet =locality =suburb =town =village =country'

if len(sys.argv) != 2:
    print(f'Usage: {sys.argv[0]} Geofabrik Country or Region name.')
    sys.exit()

wanted_map = sys.argv[1]
# replace spaces in wanted_map with geofabrik minuses 
wanted_map = wanted_map.replace(" ","-")
wanted_map=wanted_map.lower()

# is geofabrik json file present and not older then Max_Days_Old?
now = time.time()
To_Old = now - 60 * 60 * 24 * Max_Days_Old
try:
    FileCreation = os.path.getmtime(geofabrik_json_file)
    if FileCreation < To_Old:
        print (f'# Deleting old Geofabriks json file')
        os.remove(os.path.join (CurDir, 'geofabrik.json'))
        Force_Processing = 1
except:
    Force_Processing = 1
    
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

# convert to shape (multipolygon)
wanted_region = shape(wanted_map_geom)
#print (f'shape = {wanted_region}')

# get bounding box
(bbox_left, bbox_bottom, bbox_right, bbox_top) = wanted_region.bounds
#print(f'bbox={bbox_left},{bbox_top},{bbox_right},{bbox_bottom}')
#sys.exit()

# convert bounding box to list of tiles at zoom level 8
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

# Build list of tiles from the bounding box
bbox_tiles=[]
for x in range(top_x, bot_x + 1):
    for y in range(top_y, bot_y + 1):
        (tile_top,tile_left)=num2deg(x, y)
        (tile_bottom,tile_right)=num2deg(x+1, y+1)
        bbox_tiles.append ({'x':x, 'y':y, 'tile_left':tile_left, 'tile_top':tile_top, 'tile_right':tile_right, 'tile_bottom':tile_bottom})

print (f'\nSearching for needed maps, this can take a while.\n')
country = find_needed_countries (bbox_tiles, wanted_map, wanted_region)
#print (f'Country= {country}')

print('\n\n# check land_polygons.shp file')
# Check for expired land polygons file and delete it
now = time.time()
To_Old = now - 60 * 60 * 24 * Max_Days_Old
try:
    FileCreation = os.path.getmtime(land_polygons_file)
    if FileCreation < To_Old:
        print (f'# Deleting old land polygons file')
        os.remove(os.path.join (CurDir, 'land-polygons-split-4326', 'land_polygons.shp'))
        Force_Processing = 1
except:
    Force_Processing = 1

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

#print (f'{border_countries}')
#sys.exit()
#time.sleep(60)

# Check for expired maps and delete them
print(f'# Checking for old maps and remove them')
now = time.time()
To_Old = now - 60 * 60 * 24 * Max_Days_Old
for c in border_countries:
    map_files = glob.glob(f'{MAP_PATH}/{c}*.osm.pbf')
    if len(map_files) != 1:
        map_files = glob.glob(f'{MAP_PATH}/**/{c}*.osm.pbf')
    if len(map_files) == 1 and os.path.isfile(map_files[0]):
        FileCreation = os.path.getmtime(map_files[0])
        if FileCreation < To_Old or Force_Processing == 1:
            print (f'# Deleting old map of {c} {map_files},{FileCreation},{To_Old},{Force_Processing}')
            os.remove(map_files[0])
            Map_File_Deleted = 1
            
if Map_File_Deleted == 1:
    Force_Processing = 1

border_countries = {}
border_countries_urls = {}
for tile in country:
    outdir = os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}')
    if not os.path.isdir(outdir):
        os.makedirs(outdir)

# search for and download missing map files from Geofabrik 
    i = 0
    for c in tile['countries']:
        if c not in border_countries:
            print(f'# Checking mapfile for {c}')
            map_files = glob.glob(f'{MAP_PATH}/{c}*.osm.pbf')
            if len(map_files) != 1:
                map_files = glob.glob(f'{MAP_PATH}/**/{c}*.osm.pbf')
            if len(map_files) != 1 or not os.path.isfile(map_files[0]):
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

print('\n\n# filter tags from country osm.pbf files')
for key, val  in border_countries.items():
    # print(key, val)
    outFile = os.path.join(OUT_PATH, f'filtered-{key}.osm.pbf')
    outFileNames = os.path.join(OUT_PATH, f'filtered-{key}Names.osm.pbf')
    outFileo5m = os.path.join(OUT_PATH, f'outFile-{key}.o5m')
    outFileo5mFiltered = os.path.join(OUT_PATH, f'outFileFiltered-{key}.o5m')
    outFileo5mFilteredNames = os.path.join(OUT_PATH, f'outFileFiltered-{key}Names.o5m')
    
    # print(outFile)
    if not os.path.isfile(outFile) or Force_Processing == 1:
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
				
        print(f'\n\n# Filtering unwanted map objects out of map of {key}')
        cmd = ['osmfilter']
        cmd.append(outFileo5m)
        cmd.append('--verbose')
        cmd.append('--keep='+filtered_tags)
        #cmd.append('--keep-tags=all name= type= '+filtered_tags)
        cmd.append('--keep-tags=all type= layer= '+filtered_tags)
        #cmd.append('--drop-relations')
        cmd.append('-o='+outFileo5mFiltered)
        # print(cmd)
        result = subprocess.run(cmd)
        if result.returncode != 0:
            print(f'Error in OSMFilter with country: {c}')
            sys.exit()
        
        cmd = ['osmfilter']
        cmd.append(outFileo5m)
        cmd.append('--verbose')
        cmd.append('--keep='+filtered_tags_with_name)
        #cmd.append('--keep-tags=all name= type= '+filtered_tags)
        cmd.append('--keep-tags=all type= name= layer= '+filtered_tags_with_name)
        cmd.append('-o='+outFileo5mFilteredNames)
        # print(cmd)
        result = subprocess.run(cmd)
        if result.returncode != 0:
            print(f'Error in OSMFilter with country: {c}')
            sys.exit()
								
        print(f'\n\n# Converting map of {key} back to osm.pbf format')
        cmd = ['osmconvert', '-v', '--hash-memory=2500', outFileo5mFiltered]
        cmd.append('-o='+outFile)
        # print(cmd)
        result = subprocess.run(cmd)
        if result.returncode != 0:
            print(f'Error in OSMConvert with country: {c}')
            sys.exit()      
            
        cmd = ['osmconvert', '-v', '--hash-memory=2500', outFileo5mFilteredNames]
        cmd.append('-o='+outFileNames)
        # print(cmd)
        result = subprocess.run(cmd)
        if result.returncode != 0:
            print(f'Error in OSMConvert with country: {c}')
            sys.exit() 

        os.remove(outFileo5m)
        os.remove(outFileo5mFiltered)
        os.remove(outFileo5mFilteredNames)
								
    border_countries[key]['filtered_file'] = outFile
    border_countries[key]['filtered_fileNames'] = outFileNames

print('\n\n# Generate land')
TileCount = 1
for tile in country:
    landFile = os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'land.shp')
    outFile = os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'land')

    if not os.path.isfile(landFile) or Force_Processing == 1:
        print(f'\n\n# Generate land {TileCount} of {len(country)} for Coordinates: {tile["x"]} {tile["y"]}')
        cmd = ['ogr2ogr', '-overwrite', '-skipfailures']
        cmd.extend(['-spat', f'{tile["left"]-0.1:.6f}',
                    f'{tile["bottom"]-0.1:.6f}',
                    f'{tile["right"]+0.1:.6f}',
                    f'{tile["top"]+0.1:.6f}'])
        cmd.append(landFile)
        cmd.append(land_polygons_file)
        #print(cmd)
        subprocess.run(cmd)

    if not os.path.isfile(outFile+'1.osm') or Force_Processing == 1:
        cmd = ['python', 'shape2osm.py', '-l', outFile, landFile]
        #print(cmd)
        subprocess.run(cmd)
    TileCount += 1

#sys.exit()

print('\n\n# Generate sea')
TileCount = 1
for tile in country:
    outFile = os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'sea.osm')
    if not os.path.isfile(outFile) or Force_Processing == 1:
        print(f'# Generate sea {TileCount} of {len(country)} for Coordinates: {tile["x"]} {tile["y"]}')
        with open('sea.osm') as f:
            sea_data = f.read()

            sea_data = sea_data.replace('$LEFT', f'{tile["left"]-0.1:.6f}')
            sea_data = sea_data.replace('$BOTTOM',f'{tile["bottom"]-0.1:.6f}')
            sea_data = sea_data.replace('$RIGHT',f'{tile["right"]+0.1:.6f}')
            sea_data = sea_data.replace('$TOP',f'{tile["top"]+0.1:.6f}')

            with open(outFile, 'w') as of:
                of.write(sea_data)
    TileCount += 1

print('\n\n# Split filtered country files to tiles')
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
    TileCount += 1

print('\n\n# Merge splitted tiles with land an sea')
TileCount = 1
for tile in country:
    print(f'\n\n# Merging tiles for tile {TileCount} of {len(country)} for Coordinates: {tile["x"]},{tile["y"]}')
    outFile = os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'merged.osm.pbf')
    if not os.path.isfile(outFile) or Force_Processing == 1:
        cmd = [os.path.join (CurDir, 'Osmosis', 'bin', 'osmosis.bat')]
        loop=0
        for c in tile['countries']:
            cmd.append('--rbf')
            cmd.append(os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'split-{c}.osm.pbf'))
            cmd.append('workers='+workers)
            if loop > 0:
                cmd.append('--merge')
            cmd.append('--rbf')
            cmd.append(os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'split-{c}Names.osm.pbf'))
            cmd.append('workers='+workers)
            cmd.append('--merge')
            loop+=1
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
        cmd.append('threads='+threads)
        #cmd.append('simplification-factor=5')
        cmd.append('tag-conf-file=' + os.path.join (CurDir, 'tag-wahoo.xml'))
        #cmd.append('tag-conf-file=tag-mapping.xml')
        # print(cmd)
        result = subprocess.run(cmd)
        if result.returncode != 0:
            print(f'Error in Osmosis with country: {c}')
            sys.exit()        

        print('\n# compress .map file')
        cmd = ['lzma', 'e', outFile, outFile+'.lzma', f'-mt{threads}', '-d27', '-fb273', '-eos']
        # print(cmd)
        subprocess.run(cmd)
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
        Print (f'Error copying tiles of country {wanted_map}')
        sys.exit()
        
    src = os.path.join(f'{OUT_PATH}', f'{tile["x"]}', f'{tile["y"]}.map')
    dst = os.path.join(f'{OUT_PATH}', f'{wanted_map}-maps', f'{tile["x"]}', f'{tile["y"]}.map')
    outdir = os.path.join(f'{OUT_PATH}', f'{wanted_map}-maps', f'{tile["x"]}')
    if not os.path.isdir(outdir):
        os.makedirs(outdir)
    try:
        shutil.copy2(src, dst)
    except:
        Print (f'Error copying map tiles of country {wanted_map}')
        sys.exit()

cmd = ['7za', 'a', '-tzip', wanted_map, os.path.join(f'{OUT_PATH}', f'{wanted_map}', f'*')]
subprocess.run(cmd, check=True, cwd=OUT_PATH)

cmd = ['7za', 'a', '-tzip', wanted_map + '-maps.zip', os.path.join(f'{OUT_PATH}', f'{wanted_map}-maps', f'*')]
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
