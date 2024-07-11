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
# keep_folders = 0
keep_folders = 1
generate_elevation = True
integrate_Wandrer = False
x_y_processing_mode = False
Wanted_X = 131
Wanted_Y = 84

# End of Configurable Parameters

# Valhalla (routing) vars and functions
valhalla_tiles = [{'level': 2, 'size': 0.25}]
LEVEL_BITS = 3
TILE_INDEX_BITS = 22
ID_INDEX_BITS = 21
LEVEL_MASK = (2**LEVEL_BITS) - 1
TILE_INDEX_MASK = (2**TILE_INDEX_BITS) - 1
ID_INDEX_MASK = (2**ID_INDEX_BITS) - 1
INVALID_ID = (ID_INDEX_MASK << (TILE_INDEX_BITS + LEVEL_BITS)
              ) | (TILE_INDEX_MASK << LEVEL_BITS) | LEVEL_MASK


def get_tile_level(id):
    return id & LEVEL_MASK


def get_tile_index(id):
    return (id >> LEVEL_BITS) & TILE_INDEX_MASK


def get_index(id):
    return (id >> (LEVEL_BITS + TILE_INDEX_BITS)) & ID_INDEX_MASK


def tiles_for_bounding_box(left, bottom, right, top):
    # if this is crossing the anti meridian split it up and combine
    if left > right:
        east = tiles_for_bounding_box(left, bottom, 180.0, top)
        west = tiles_for_bounding_box(-180.0, bottom, right, top)
        return east + west
    # move these so we can compute percentages
    left += 180
    right += 180
    bottom += 90
    top += 90
    tiles = []
    # for each size of tile
    for tile_set in valhalla_tiles:
        # for each column
        for x in range(int(left/tile_set['size']), int(right/tile_set['size']) + 1):
            # for each row
            for y in range(int(bottom/tile_set['size']), int(top/tile_set['size']) + 1):
                # give back the level and the tile index
                tiles.append((tile_set['level'], int(
                    y * (360.0/tile_set['size']) + x)))
    return tiles
# End Valhalla


def get_tile_id(tile_level, lat, lon):
    level = list(filter(lambda x: x['level'] == tile_level, valhalla_tiles))[0]
    width = int(360 / level['size'])
    return int((lat + 90) / level['size']) * width + int((lon + 180) / level['size'])


def get_ll(id):
    tile_level = get_tile_level(id)
    tile_index = get_tile_index(id)
    level = list(filter(lambda x: x['level'] == tile_level, valhalla_tiles))[0]
    width = int(360 / level['size'])
    return int(tile_index / width) * level['size'] - 90, (tile_index % width) * level['size'] - 180

# Convert lon./lat. to tile numbers


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

# Get the Geofabrik outline of the desired country/region from the Geofabrik json file and the
# download url of the map.
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
        # print (props.get('urls', ''))
        wurls = props.get('urls', '')
        return (x.geometry, wurls.get('pbf', ''))
    return None, None

# Get the map download url from a region with the already loaded json data


def find_geofbrik_url(name, geofabrik_json):
    for x in geofabrik_json.features:
        props = x.properties
        id = props.get('id', '')
        if id != name:
            continue
        # print (props.get('urls', ''))
        wurls = props.get('urls', '')
        return (wurls.get('pbf', ''))
    return None

# Get the parent map/region of a region from the already loaded json data


def find_geofbrik_parent(name, geofabrik_json):
    for x in geofabrik_json.features:
        props = x.properties
        id = props.get('id', '')
        if id != name:
            continue
        return (props.get('parent', ''), props.get('id', ''))
    return None, None

CurDir = os.getcwd()  # Get Current Directory

MAP_PATH = os.path.join(CurDir, 'Maps')
OUT_PATH = os.path.join(CurDir, 'Output')
land_polygons_file = os.path.join(
    CurDir, 'land-polygons-split-4326', 'land_polygons.shp')
geofabrik_json_file = os.path.join(CurDir, 'geofabrik.json')
geofabrik_regions = ['africa', 'antarctica', 'asia', 'australia-oceania', 'baden-wuerttemberg', 
                     'bayern', 'brazil', 'california', 'canada', 'europe', 'france',
                     'germany', 'india', 'indonesia', 'italy', 'japan', 'netherlands', 
                     'nordrhein-westfalen', 'north-america', 'poland', 'russia', 'south-america', 'spain', 'united-kingdom' , 'us']

# List of regions to block. these regions are "collections" of other countries/regions/states
block_download = ['africa', 'alps', 'asia', 'australia-oceania', 'britain-and-ireland', 'canada', 'dach', 'europe', 'great-britain' , 'norcal' ,'north-america',
                  'socal', 'south-africa-and-lesotho', 'south-america', 'us', 'us-midwest', 'us-northeast', 'us-pacific', 'us-south', 'us-west']


url = ''
Map_File_Deleted = 0

if len(sys.argv) != 2:
    print(f'Usage: {sys.argv[0]} Geofabrik Country or Region name.')
    sys.exit()

wanted_map = sys.argv[1]
# replace spaces in wanted_map with geofabrik minuses
wanted_map = wanted_map.replace(" ", "-")
wanted_map = wanted_map.lower()

# is geofabrik json file present and not older then Max_Days_Old?
now = time.time()
To_Old = now - 60 * 60 * 24 * Max_Days_Old
try:
    FileCreation = os.path.getmtime(geofabrik_json_file)
    if FileCreation < To_Old:
        print(f'# Deleting old Geofabriks json file')
        os.remove(os.path.join(CurDir, 'geofabrik.json'))
        # Force_Processing = 1
except:
    pass
    # Force_Processing = 1

# if not present download Geofabriks json file
if not os.path.exists(geofabrik_json_file) or not os.path.isfile(geofabrik_json_file) or Force_Processing == 1:
    print('# Downloading Geofabrik json file')
    url = 'https://download.geofabrik.de/index-v1.json'
    r = requests.get(url, allow_redirects=True, stream=True)
    if r.status_code != 200:
        print(f'failed to find or download Geofabrik json file')
        sys.exit()
    Download = open(os.path.join(CurDir, 'geofabrik.json'), 'wb')
    for chunk in r.iter_content(chunk_size=10240):
        Download.write(chunk)
    Download.close()

if not x_y_processing_mode:
    # Check if wanted_map is in the json file and if so get the polygon (shape)
    wanted_map_geom, wanted_url = geom(wanted_map)
    if not wanted_map_geom:
        # try to prepend us\ to the wanted_map
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
    for x in range(top_x, bot_x + 1):
        for y in range(top_y, bot_y + 1):
            (tile_top, tile_left) = num2deg(x, y)
            (tile_bottom, tile_right) = num2deg(x+1, y+1)
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
            bbox_tiles.append({'x': x, 'y': y, 'tile_left': tile_left, 'tile_top': tile_top,
                              'tile_right': tile_right, 'tile_bottom': tile_bottom})

    # Get a list of the Geofabrik maps needed to process these tiles
    #print(f'\nSearching for needed maps, this can take a while.\n')
    #country = find_needed_countries(bbox_tiles, wanted_map)
    # print (f'Country= {country}')
    # sys.exit()
else:
    top_x = Wanted_X
    bot_x = Wanted_X
    top_y = Wanted_Y
    bot_y = Wanted_Y

    # Build list of tiles from the bounding box
    bbox_tiles = []
    tile_top = tile_bottom = tile_left = tile_right = None
    for x_value in range(top_x, bot_x + 1):
        for y_value in range(top_y, bot_y + 1):
            (tile_top, tile_left) = num2deg(x_value, y_value)
            (tile_bottom, tile_right) = num2deg(x_value+1, y_value+1)
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
            bbox_tiles.append({'x': x_value, 'y': y_value, 'tile_left': tile_left,
                               'tile_top': tile_top, 'tile_right': tile_right,
                               'tile_bottom': tile_bottom})

    coords = []
    coords.append((tile_top, tile_left))
    coords.append((tile_top, tile_right))
    coords.append((tile_bottom, tile_right))
    coords.append((tile_bottom, tile_left))
    coords.append((tile_top, tile_left))
    # print(f'Coords= {coords}')
    p = Polygon(coords)
    # print(f'p= {p}')
    wanted_region = shape(p)
    # print(f'wanted_region= {wanted_region}')
    (bbox_left, bbox_bottom, bbox_right, bbox_top) = wanted_region.bounds

    # wanted_region = bbox_tiles
    #print(f'\nSearching for needed maps, this can take a while.\n')
    #country = find_needed_countries(bbox_tiles, None, xy_mode=True)
    # print (f'Country= {country}')

# Check for expired land polygons file and delete it
print('\n\n# check land_polygons.shp file')
now = time.time()
To_Old = now - 60 * 60 * 24 * Max_Days_Old
try:
    FileCreation = os.path.getmtime(land_polygons_file)
    if FileCreation < To_Old:
        print(f'# Deleting old land polygons file')
        # Keep pregenerated files to reduce processing time
        # os.remove(os.path.join (CurDir, 'land-polygons-split-4326', 'land_polygons.shp'))
        # Force_Processing = 1
except:
    pass
    # Force_Processing = 1

# If land polygons file does not exist or or Force_Processing active, (re)download it
if not os.path.exists(land_polygons_file) or not os.path.isfile(land_polygons_file) or Force_Processing == 1:
    print('# Downloading land polygons file')
#    url = 'https://osmdata.openstreetmap.de/download/land-polygons-split-4326.zip'
    url = 'https://osmdata.openstreetmap.de/download/land-polygons-split-4326.zip'
    r = requests.get(url, allow_redirects=True, stream=True)
    if r.status_code != 200:
        print(f'failed to find or download land polygons file')
        sys.exit()
    Download = open(os.path.join(CurDir, 'land-polygons-split-4326.zip'), 'wb')
    for chunk in r.iter_content(chunk_size=10240):
        Download.write(chunk)
    Download.close()
    # unpack it
    cmd = ['7za', 'x', '-y',
           os.path.join(CurDir, 'land-polygons-split-4326.zip')]
    # print(cmd)
    result = subprocess.run(cmd)
    os.remove(os.path.join(CurDir, 'land-polygons-split-4326.zip'))
    if result.returncode != 0:
        print(f'Error unpacking land polygons file')
        sys.exit()

# Init vars
border_countries = {}
border_countries_urls = {}

# Process routing tiles if present
IN_R_PATH = os.path.join(CurDir, f'valhalla_tiles', f'2', f'000')
#IN_R_PATH = os.path.join(CurDir, f'valhalla_tiles', f'2', f'dummy')
rtile = None
if os.path.isdir(IN_R_PATH):
    # Calculate which routing tiles are needed
    routing_tiles = tiles_for_bounding_box(
        bbox_left, bbox_bottom, bbox_right, bbox_top)
    # print (f'Routing Tiles = {routing_tiles}')

    for rtile in routing_tiles:
        # print (f'rtile = {rtile[1]}')
        print('\n# compress routing tile file')
        inRFile = os.path.join(
            IN_R_PATH, f'{str(rtile[1])[0:3]}', f'{str(rtile[1])[3:6]}.gph')
        print(f'inRFile = {inRFile}')
        outRFile = os.path.join(OUT_PATH, f'{wanted_map}-routing', f'routing',
                                f'2', f'000', f'{str(rtile[1])[0:3]}', f'{str(rtile[1])[3:6]}.gph')
        print(f'outRFile = {outRFile}')

        outdir = os.path.join(f'{OUT_PATH}', f'{wanted_map}-routing',
                              f'routing', f'2', f'000', f'{str(rtile[1])[0:3]}')
        if not os.path.isdir(outdir):
            os.makedirs(outdir)
            # print(f'outdir = {outdir}')

        if os.path.isfile(inRFile):
            try:
                shutil.copy2(inRFile, outRFile)
            except:
                print(f'Error copying routing tile of country {wanted_map}')
                sys.exit()

            cmd = ['lzma', 'e', outRFile, outRFile+'.lzma',
                   f'-mt{threads}', '-d27', '-fb273', '-eos']
            # print(cmd)
            subprocess.run(cmd)

            # Create "tile present" file
            f = open(outRFile + '.lzma.18', 'wb')
            f.close()

            # Remove copied routing tile
            try:
                os.remove(outRFile)
            except OSError as e:
                print(f'Error, could not delete routing tile ' + f'{outRFile}')

# Compress routing tiles
if rtile:
    cmd = ['7za', 'a', '-tzip', wanted_map+f'-routing',
           os.path.join(f'{OUT_PATH}', f'{wanted_map}-routing', f'*')]
    subprocess.run(cmd, check=True, cwd=OUT_PATH)
