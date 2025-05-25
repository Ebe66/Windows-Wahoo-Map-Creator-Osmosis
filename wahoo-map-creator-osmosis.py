"""Generates maps for the Wahoo line of bike computers"""
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
keep_folders = 0
generate_elevation = True
integrate_Wandrer = False
integrate_Routes = True
x_y_processing_mode = False
Wanted_X = 131
Wanted_Y = 84
Process_Routing = False

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

def get_tile_level(idv):
    """Get tile level"""
    return idv & LEVEL_MASK

def get_tile_index(idv):
    """Get tile index"""
    return (idv >> LEVEL_BITS) & TILE_INDEX_MASK

def get_index(idv):
    """Get index"""
    return (idv >> (LEVEL_BITS + TILE_INDEX_BITS)) & ID_INDEX_MASK

def tiles_for_bounding_box(left, bottom, right, top):
    """Find Valhalla tiles needed to cover the bounding box """
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
    """ Get Valhalla tile id """
    level = list(filter(lambda x: x['level'] == tile_level, valhalla_tiles))[0]
    width = int(360 / level['size'])
    return int((lat + 90) / level['size']) * width + int((lon + 180) / level['size'])

def get_ll(idv):
    """ Get something for Valhalla ;-) """
    tile_level = get_tile_level(idv)
    tile_index = get_tile_index(idv)
    level = list(filter(lambda x: x['level'] == tile_level, valhalla_tiles))[0]
    width = int(360 / level['size'])
    return int(tile_index / width) * level['size'] - 90, (tile_index % width) * level['size'] - 180

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
# Do not download the countries below because they are comprised of sub-regions. Downloading those sub-regions instead results in faster processing
geofabrik_regions = ['africa', 'asia', 'australia-oceania', 'baden-wuerttemberg',
                     'bayern', 'brazil', 'california', 'canada', 'england', 'europe', 'france',
                     'germany', 'india', 'indonesia', 'italy', 'japan', 'netherlands',
                     'nordrhein-westfalen', 'north-america', 'poland', 'russia', 'south-america', 'spain', 'united-kingdom' , 'us']

# List of regions to block. these regions are "collections" of other countries/regions/states
block_download = ['africa', 'alps', 'asia', 'australia-oceania', 'britain-and-ireland', 'canada', 'china', 'dach', 'europe', 'great-britain', 'india', 'indonesia', 'japan', 'norcal' ,'north-america',
                  'russia','socal', 'south-africa-and-lesotho', 'south-america', 'us', 'us-midwest', 'us-northeast', 'us-pacific', 'us-south', 'us-west']

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
objects_to_keep_with_name = 'access=private \
    admin_level=2 =4\
    aeroway=aerodrome =airport =apron =gate =hangar =helipad =runway =taxiway =terminal \
    amenity=atm =bar =bench =bicycle_rental =biergarten =bus_station =cafe =cinema =college =drinking_water =fast_food =fire_station =fountain =fuel =hospital =ice_cream =kindergarten =library =parking =pharmacy =police =post_box =pub =recycling =restaurant =shelter =school =telephone =theatre =toilets =university =water_point \
    area=yes \
    barrier=block =bollard =gate =lift_gate =retaining_wall \
    bicycle= \
    boundary=national_park \
    bridge= \
    building=church =cathedral \
    denomination= \
    emergency=phone \
    foot=yes =designated \
    highway=abandoned =bus_guideway =bus_stop =disused =bridleway =byway =construction =cycleway =footway =living_street =motorway =motorway_link =path =pedestrian =primary =primary_link =residential =road =secondary =secondary_link =service =steps =tertiary =tertiary_link =track =trunk =trunk_link =unclassified \
    historic=ruins =castle =memorial =monument\
    landuse=allotments =building =cemetery =commercial =conservation =farm =farmland =farmyard =forest =grass =greenhouse_horticulture =heath =industrial =meadow =military =nature_reserve =orchard =plant_nursery =quarry =railway =recreation_ground =residential =reservoir =retail =track =urban =vineyard =village_green \
    leisure=common =garden =golf_course =miniature_golf =picnic_table =pitch =playground =sports_centre =stadium =swimming_pool =water_park =park =nature_reserve \
    man_made=cutline =drinking_fountain =pier =water_tap\
    mountain_pass= \
    mtb:scale= \
    mtb:scale:uphill= \
    natural=cave_entrance =coastline =nosea =sea =issea =beach =forest =glacier =grassland =heath =land =marsh =mud =sand =scrub =water =wetland =wood =spring =peak =volcano \
    oneway=yes \
    place=isolated_dwelling =islet =square =city =country =hamlet =island =locality =neighbourhood =suburb =town =village \
    railway=abandoned =bus_guideway =crossing =disused =funicular =halt =level_crossing =light_rail =miniature =monorail =narrow_gauge =platform =preserved =rail =spur =station =stop =subway =turntable =tram \
    religion= \
    route=ferry \
    shelter_type=picnic_shelter \
    shop=bakery =bicycle =convenience =laundry =mall =organic =supermarket \
    station=light_rail =subway =halt =stop \
    surface= \
    tourism=museum =zoo =alpine_hut =attraction =camp_site =caravan_site =hostel =hotel =information =picnic_site =viewpoint \
    tracktype= \
    tunnel= \
    wandrer= \
    waterway=dam =ditch =dock =drain =lock =stream =riverbank =weir =canal =river \
    wood=deciduous'

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

if integrate_Wandrer:
    # Check for new Wandrer kmz files in the maps directory. Format must be wandrer*.kmz
    wandrerkmz_files = glob.glob(f'{MAP_PATH}/wandrer*.kmz')
    if wandrerkmz_files:
        print('Unpacking Wandrer KMZ file(s) to KML')
        for file in wandrerkmz_files:
            # Unpack to kml
            cmd = ['7za', 'e', '-y', file]
            subprocess.run(cmd, check=True, cwd=MAP_PATH)

        # Find KML files (could be more then one in a KMZ?)
        wandrerkml_files = glob.glob(f'{MAP_PATH}/wandrer*.kml')
        if wandrerkml_files:
            print('Converting Wandrer KML file(s) to OSM. (This can take a while!)')
            for file in wandrerkml_files:
                # Call gpsbabel to convert to osm example gpsbabel -w -r -t -i kml -f file-in -o osm,tag=wandrer:untraveled,tagnd=wandrer:untraveled -F file-out
                cmd = ['gpsbabel', '-w', '-r', '-t', '-i', 'kml', '-f', file, '-o',
                       'osm,tag=wandrer:untraveled,tagnd=wandrer:untraveled', '-F', file.replace(".kml", ".osm")]
                subprocess.run(cmd, check=True, cwd=MAP_PATH)

        wandrerosm_files = glob.glob(f'{MAP_PATH}/wandrer*.osm')
        if wandrerosm_files:
            print(
                'Replacing negative ID\'s with Large positive ones and converting to .osm.pbf.')
            for file in wandrerosm_files:
                # Convert negative ID's to large positive numbers
                with open(file, encoding='utf8') as f:
                    osm_data = f.read()
                    f.close()

                    osm_data = osm_data.replace("\"-", "\"20000000000")

                    with open(file, 'w', encoding='utf8') as of:
                        of.write(osm_data)
                        of.close()

                # Convert to osm.pbf
                cmd = ['osmconvert']
                cmd.extend(['-v', '--hash-memory=2500', '--complete-ways', '--complete-multipolygons',
                           '--complete-boundaries', '--drop-author', '--drop-version'])
                cmd.append(file)
                cmd.append('-o='+file.replace(".osm", ".osm.pbf"))
                # print(cmd)
                result = subprocess.run(cmd, check=True)
                if result.returncode != 0:
                    print(f'Error in OSMConvert with Wandrer file: {file}')

        try:
            print('Removing intermediate files and renaming processed input files')
            for file in wandrerkmz_files:
                oldbasename = os.path.basename(file)
                os.rename(file, file.replace(
                    oldbasename, "Processed-"+oldbasename))

            for file in wandrerkml_files:
                os.remove(file)

            for file in wandrerosm_files:
                os.remove(file)
        except:
            pass

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
    pass
    # Force_Processing = 1

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

if not x_y_processing_mode:
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
    print('\nSearching for needed maps, this can take a while.\n')
    country = find_needed_countries(bbox_tiles, None, xy_mode=True)
    # print (f'Country= {country}')

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

# If land polygons file does not exist or or Force_Processing active, (re)download it
if not os.path.exists(land_polygons_file) or not os.path.isfile(land_polygons_file) or Force_Processing == 1:
    print('# Downloading land polygons file')
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

# Filter out any keys/values we are not going to use. this prevents out-of-memory errors when creating the map files and speeds up the splitting out of tiles
# Smaller files spilt faster!!
print('\n\n# filter tags from country osm.pbf files')
for key, val in border_countries.items():
    # print(key, val)
    outFileo5m = os.path.join(OUT_PATH, f'outFile-{key}.o5m')
    outFileo5mFiltered = os.path.join(OUT_PATH, f'outFileFiltered-{key}.o5m')

    # Convert osm.pbf file to o5m for processing with osmfilter
    # print(outFile)
    if not os.path.isfile(outFileo5mFiltered) or Force_Processing == 1:
        print(f'\n\n# Converting map of {key} to o5m format')
        cmd = ['osmconvert']
        cmd.extend(['--hash-memory=2500', '--drop-author', '--drop-version'])
        cmd.append(val['map_file'])
        cmd.append('-o='+outFileo5m)
        # print(cmd)
        result = subprocess.run(cmd, check=True)
        if result.returncode != 0:
            print(f'Error in OSMConvert with country: {key}')
            sys.exit()

        # Keep keys/values we want with the name key (cities etc)
        cmd = ['osmfilter']
        #cmd.append(outFileo5m)
        cmd.append(outFileo5m)
        #cmd.append('--verbose')
        cmd.append('--keep='+objects_to_keep_with_name)
        cmd.append('--keep-tags=all type= name= layer= ele= ' +
                   objects_to_keep_with_name)
        #cmd.append('--drop-relations')
        cmd.append('-o='+outFileo5mFiltered)
        # print(cmd)
        result = subprocess.run(cmd, check=True)
        if result.returncode != 0:
            print(f'Error in OSMFilter with country: {key}')
            sys.exit()

        os.remove(outFileo5m)

    border_countries[key]['filtered_file'] = outFileo5mFiltered

#sys.exit()

print('\n\n# Generate land')
TileCount = 1
for tile in country:
    landFile = os.path.join(
        OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', 'land.shp')
    outFile = os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', 'land')

    if not os.path.isfile(landFile) or Force_Processing == 1:
        print(
            f'\n\n# Generate land {TileCount} of {len(country)} for Coordinates: {tile["x"]} {tile["y"]}')
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
        subprocess.run(cmd, check=True)

    if not os.path.isfile(outFile+'1.osm') or Force_Processing == 1:
        cmd = ['python', 'shape2osm.py', '-l', outFile, landFile]
        # print(cmd)
        subprocess.run(cmd, check=True)
    TileCount += 1

print('\n\n# Generate sea')
TileCount = 1
for tile in country:
    outFile = os.path.join(
        OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', 'sea.osm')
    if not os.path.isfile(outFile) or Force_Processing == 1:
        print(
            f'# Generate sea {TileCount} of {len(country)} for Coordinates: {tile["x"]} {tile["y"]}')
        with open('sea.osm', encoding='utf8') as f:
            sea_data = f.read()
            f.close()

            # Try to prevent getting outside of the +/-180 and +/- 90 degrees borders. Normally the +/- 0.1 are there to prevent white lines at tile borders
            if tile["x"] == 255 or tile["y"] == 255 or tile["x"] == 0 or tile["y"] == 0:
                sea_data = sea_data.replace('$LEFT', f'{tile["left"]:.6f}')
                sea_data = sea_data.replace('$BOTTOM', f'{tile["bottom"]:.6f}')
                sea_data = sea_data.replace('$RIGHT', f'{tile["right"]:.6f}')
                sea_data = sea_data.replace('$TOP', f'{tile["top"]:.6f}')
            else:
                sea_data = sea_data.replace('$LEFT', f'{tile["left"]-0.1:.6f}')
                sea_data = sea_data.replace(
                    '$BOTTOM', f'{tile["bottom"]-0.1:.6f}')
                sea_data = sea_data.replace(
                    '$RIGHT', f'{tile["right"]+0.1:.6f}')
                sea_data = sea_data.replace('$TOP', f'{tile["top"]+0.1:.6f}')

            with open(outFile, 'w', encoding='utf8') as of:
                of.write(sea_data)
                of.close()
    TileCount += 1

if generate_elevation == 1:
    print('\n\n# Generate elevation data PBF')
    TileCount = 1
    for tile in country:
        outFile = os.path.join(
            OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', 'elevation')
        elevation_files = glob.glob(os.path.join(
            OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', 'elevation*.pbf'))
        # print (f'\nElevation_files = {elevation_files}')
        #if not elevation_files or Force_Processing == 1:
        if not elevation_files:
            print(
                f'# Generate elevation {TileCount} of {len(country)} for Coordinates: {tile["x"]} {tile["y"]}')
            cmd = ['phyghtmap']
            cmd.append('-a '+f'{tile["left"]}' + ':' + f'{tile["bottom"]}' +
                       ':' + f'{tile["right"]}' + ':' + f'{tile["top"]}')
            # Old initial version. Used as a fallback now for if the wsl version could not create an elevation file
            cmd.extend(['-o', f'{outFile}', '-s 10', '-c 100,50', '--source=view3', '--simplifyContoursEpsilon=0.00001', '--pbf', '--jobs=15', '--start-node-id=20000000000','--max-nodes-per-tile=0',
                       '--max-nodes-per-way=200', '--start-way-id=21000000000', '--write-timestamp', '--no-zero-contour'])
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
    #sys.exit()

print('\n\n# Split filtered country files to tiles')

# Check if there is a wandrer map
doWandrer = None
inWandrer_files = list()
if integrate_Wandrer:
    inWandrer_files = glob.glob(os.path.join(MAP_PATH, 'wandrer*.osm.pbf'))
    if inWandrer_files and integrate_Wandrer:
        doWandrer = True
    else:
        doWandrer = False
TileCount = 1
for tile in country:
    for c in tile['countries']:
        print(
            f'\n\n# Splitting tile {TileCount} of {len(country)} for Coordinates: {tile["x"]},{tile["y"]} from map of {c}')
        outFile = os.path.join(
            OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'split-{c}.osm.pbf')
        outMerged = os.path.join(
            OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', 'merged.osm.pbf')
        if not os.path.isfile(outMerged) or Force_Processing == 1:
            cmd = ['osmconvert', '--hash-memory=2500']
            cmd.append('-b='+f'{tile["left"]}' + ',' + f'{tile["bottom"]}' +
                       ',' + f'{tile["right"]}' + ',' + f'{tile["top"]}')
            cmd.extend(
                ['--complete-ways', '--complete-multipolygons', '--complete-boundaries'])
            cmd.append(border_countries[c]['filtered_file'])
            cmd.append('-o='+outFile)
            # print(cmd)
            result = subprocess.run(cmd, check=True)
            if result.returncode != 0:
                print(f'Error in Osmconvert with country: {c}')
                sys.exit()
            # print(border_countries[c]['filtered_file'])

        if doWandrer:
            for wandrer_map in inWandrer_files:
                outWandrer = os.path.join(
                    OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'split-{os.path.basename(wandrer_map)}')
                if not os.path.isfile(outWandrer) or Force_Processing == 1:
                    cmd = ['osmconvert', '--hash-memory=2500']
                    cmd.append('-b='+f'{tile["left"]}' + ',' + f'{tile["bottom"]}' +
                               ',' + f'{tile["right"]}' + ',' + f'{tile["top"]}')
                    cmd.extend(
                        ['--complete-ways', '--complete-multipolygons', '--complete-boundaries'])
                    cmd.append(wandrer_map)
                    cmd.append('-o='+outWandrer)
                    # print(cmd)
                    result = subprocess.run(cmd, check=True)
                    if result.returncode != 0:
                        print('Error in Osmconvert while processing Wandrer file')
                        sys.exit()

        # If we want routes? Note: commented out because splitting from an osm file without nodes does not work
        #if integrate_Routes:
        #    # Is there a source routes file
        #    if os.path.isfile(os.path.join(OUT_PATH, f'routes-{c}.osm')):
        #        outRoute = os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'split-routes-{c}.osm.pbf')
        #        # If the split file is not there yet
        #        if not os.path.isfile(outRoute) or Force_Processing == 1:
        #            cmd = ['osmconvert', '--hash-memory=2500']
        #            cmd.append('-b='+f'{tile["left"]}' + ',' + f'{tile["bottom"]}' +
        #                       ',' + f'{tile["right"]}' + ',' + f'{tile["top"]}')
        #            cmd.extend(
        #                ['--complete-ways', '--complete-multipolygons', '--complete-boundaries'])
        #            cmd.append(os.path.join(OUT_PATH, f'routes-{c}.osm'))
        #            cmd.append('-o='+outRoute)
        #            #print(cmd)
        #            result = subprocess.run(cmd, check=True)
        #            if result.returncode != 0:
        #                print('Error in Osmconvert while processing Bike Routes file')
        #                sys.exit()
        #            #sys.exit()
    TileCount += 1

print('\n\n# Merge splitted tiles with land, sea and elevation')
TileCount = 1
for tile in country:
    print(
        f'\n\n# Merging tiles for tile {TileCount} of {len(country)} for Coordinates: {tile["x"]},{tile["y"]}')
    outFile = os.path.join(
        OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', 'merged.osm.pbf')
    # Check if there are "split*.osm.pbf" files to merge. If not, skip tile
    files_to_merge = glob.glob(os.path.join(
        OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', 'split-*.osm.pbf'))
    if files_to_merge:
        if not os.path.isfile(outFile) or Force_Processing == 1:
            cmd = [os.path.join(CurDir, 'Osmosis', 'bin', 'osmosis.bat')]
            loop = 0
            c = None
            for c in tile['countries']:
                cmd.append('--rbf')
                cmd.append(os.path.join(
                    OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'split-{c}.osm.pbf'))
                cmd.append('workers='+workers)
                if loop > 0:
                    cmd.append('--merge')
                loop += 1
            if generate_elevation == 1:
                elevation_files = glob.glob(os.path.join(
                    OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', 'elevation*.pbf'))
                for elevation in elevation_files:
                    cmd.append('--rbf')
                    cmd.append(os.path.join(
                        OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'{elevation}'))
                    cmd.append('workers='+workers)
                    cmd.append('--merge')
            if doWandrer:
                wandrer_files = glob.glob(os.path.join(
                    OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', 'split-wandrer*.osm.pbf'))
                for wandrer in wandrer_files:
                    cmd.append('--rbf')
                    cmd.append(os.path.join(
                        OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'{wandrer}'))
                    cmd.append('workers='+workers)
                    cmd.append('--merge')
            if integrate_Routes:
                #for c in tile['countries']:
                #    cmd.append('--rbf')
                #    cmd.append(os.path.join(
                #        OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', f'split-routes-{c}.osm.pbf'))
                #    cmd.append('workers='+workers)
                #    cmd.append('--merge')
                # Instead of merging with splitted route files (does not work) merge the whole routes file, the bounding box
                # is done by Osmosis/mapwriter
                for c in tile['countries']:
                    cmd.append('--rbf')
                    cmd.append(os.path.join(
                        OUT_PATH, f'routes-{c}.osm.pbf'))
                    cmd.append('workers='+workers)
                    cmd.append('--merge')

            land_files = glob.glob(os.path.join(
                OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', 'land*.osm'))
            for land in land_files:
                cmd.extend(['--rx', 'file='+os.path.join(OUT_PATH,
                           f'{tile["x"]}', f'{tile["y"]}', f'{land}'), '--s', '--m'])
            cmd.extend(['--rx', 'file='+os.path.join(OUT_PATH,
                       f'{tile["x"]}', f'{tile["y"]}', 'sea.osm'), '--s', '--m'])
            cmd.extend(['--tag-transform', 'file=' + os.path.join(CurDir,
                       'tunnel-transform.xml'), '--buffer', '--wb', outFile, 'omitmetadata=true'])
            # print(cmd)
            result = subprocess.run(cmd, check=True)
            if result.returncode != 0:
                print(f'Error in Osmosis with country: {c}')
                sys.exit()
    TileCount += 1

print('\n\n# Creating .map files')
TileCount = 1
for tile in country:
    print(
        f'\n\nCreating map file for tile {TileCount} of {len(country)} for Coordinates: {tile["x"]}, {tile["y"]}')
    outFile = os.path.join(OUT_PATH, f'{tile["x"]}', f'{tile["y"]}.map')
    mergedFile = os.path.join(
        OUT_PATH, f'{tile["x"]}', f'{tile["y"]}', 'merged.osm.pbf')
    # Check if there is a merged.osm.pbf file to generate a map of
    if os.path.isfile(mergedFile):
        if not os.path.isfile(outFile+'.lzma') or Force_Processing == 1:
            cmd = [os.path.join(CurDir, 'Osmosis', 'bin', 'osmosis.bat'), '--rbf',
                   mergedFile, 'workers='+workers, '--buffer', 'bufferCapacity=120000', '--mw', 'file='+outFile]
            cmd.append(
                f'bbox={tile["bottom"]:.6f},{tile["left"]:.6f},{tile["top"]:.6f},{tile["right"]:.6f}')
            #cmd.append('zoom-interval-conf=10,0,17')
            cmd.append('zoom-interval-conf=12,0,17')
            #cmd.append('zoom-interval-conf=5,0,6,8,7,9,11,10,12,15,13,17') # Openandromaps setting
            cmd.append('threads='+threads)
            cmd.append('skip-invalid-relations=true')
            #cmd.append('type=hd')
            cmd.append('tag-conf-file=' +
                       #os.path.join(CurDir, 'tag-wahoo.xml'))
                       os.path.join(CurDir, 'tag-wahoo-z13.xml'))
            print(cmd)
            result = subprocess.run(cmd, check=True)
            if result.returncode != 0:
                print(f'Error in Osmosis with tile: {tile["x"]}, {tile["y"]}')
                sys.exit()

            print('\n# compress .map file')
            cmd = ['lzma', 'e', outFile, outFile+'.lzma',
                   f'-mt{threads}', '-d27', '-fb273', '-eos']
            # print(cmd)
            subprocess.run(cmd, check=True)

            # Create "tile present" file
            f = open(outFile + '.lzma.20', 'wb')
            f.close()

    TileCount += 1

print('\n# zip .map.lzma files')

try:
    res = wanted_map.index('/')
    wanted_map = wanted_map[res+1:]
except:
    pass

# copy the needed tiles to the country folder
print('Copying Wahoo and map tiles to output folders')
for tile in country:
    src = os.path.join(f'{OUT_PATH}', f'{tile["x"]}', f'{tile["y"]}.map.lzma')
    # Check if source map.lzma file is available to copy
    if os.path.isfile(src):
        dst = os.path.join(f'{OUT_PATH}', f'{wanted_map}',
                           f'{tile["x"]}', f'{tile["y"]}.map.lzma')
        outdir = os.path.join(f'{OUT_PATH}', f'{wanted_map}', f'{tile["x"]}')
        if not os.path.isdir(outdir):
            os.makedirs(outdir)
        try:
            shutil.copy2(src, dst)
        except:
            print(f'Error copying tiles of country {wanted_map}')
            sys.exit()

    src = os.path.join(
        f'{OUT_PATH}', f'{tile["x"]}', f'{tile["y"]}.map.lzma.20')
    # Check if source map.lzma.20 file is available to copy
    if os.path.isfile(src):
        dst = os.path.join(f'{OUT_PATH}', f'{wanted_map}',
                           f'{tile["x"]}', f'{tile["y"]}.map.lzma.20')
        outdir = os.path.join(f'{OUT_PATH}', f'{wanted_map}', f'{tile["x"]}')
        if not os.path.isdir(outdir):
            os.makedirs(outdir)
        try:
            shutil.copy2(src, dst)
        except:
            print(f'Error copying precense files of country {wanted_map}')
            sys.exit()

    src = os.path.join(f'{OUT_PATH}', f'{tile["x"]}', f'{tile["y"]}.map')
    # Check if source map file is available to copy
    if os.path.isfile(src):
        dst = os.path.join(f'{OUT_PATH}', f'{wanted_map}-maps',
                           f'{tile["x"]}', f'{tile["y"]}.map')
        outdir = os.path.join(
            f'{OUT_PATH}', f'{wanted_map}-maps', f'{tile["x"]}')
        if not os.path.isdir(outdir):
            os.makedirs(outdir)
        try:
            shutil.copy2(src, dst)
        except:
            print(f'Error copying map tiles of country {wanted_map}')
            sys.exit()

# Process routing tiles if present
IN_R_PATH = os.path.join(CurDir, 'valhalla_tiles', '2', '000')
rtile = None
if os.path.isdir(IN_R_PATH) and Process_Routing is True:
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
        outRFile = os.path.join(OUT_PATH, f'{wanted_map}-routing', 'routing',
                                '2', '000', f'{str(rtile[1])[0:3]}', f'{str(rtile[1])[3:6]}.gph')
        print(f'outRFile = {outRFile}')

        outdir = os.path.join(f'{OUT_PATH}', f'{wanted_map}-routing',
                              'routing', '2', '000', f'{str(rtile[1])[0:3]}')
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
            subprocess.run(cmd, check=True)

            # Create "tile present" file
            f = open(outRFile + '.lzma.20', 'wb')
            f.close()

            # Remove copied routing tile
            try:
                os.remove(outRFile)
            except OSError as e:
                print('Error, could not delete routing tile ' + f'{outRFile}')


cmd = ['7za', 'a', '-tzip', wanted_map,
       os.path.join(f'{OUT_PATH}', f'{wanted_map}', '*')]
subprocess.run(cmd, check=True, cwd=OUT_PATH)

cmd = ['7za', 'a', '-tzip', wanted_map + '-maps.zip',
       os.path.join(f'{OUT_PATH}', f'{wanted_map}-maps', '*')]
subprocess.run(cmd, check=True, cwd=OUT_PATH)

# Compress routing tiles
if rtile:
    cmd = ['7za', 'a', '-tzip', wanted_map+'-routing',
           os.path.join(f'{OUT_PATH}', f'{wanted_map}-routing', '*')]
    subprocess.run(cmd, check=True, cwd=OUT_PATH)

# if desired, delete the Wahoo and map folders after compression
if keep_folders == 0:
    try:
        shutil.rmtree(os.path.join(f'{OUT_PATH}', f'{wanted_map}'))
    except OSError as e:
        print('Error, could not delete folder ' +
              os.path.join(f'{OUT_PATH}', f'{wanted_map}'))
    try:
        shutil.rmtree(os.path.join(f'{OUT_PATH}', f'{wanted_map}-maps'))
    except OSError as e:
        print('Error, could not delete folder ' +
              os.path.join(f'{OUT_PATH}', f'{wanted_map}-maps'))
    if rtile:
        try:
            shutil.rmtree(os.path.join(f'{OUT_PATH}', f'{wanted_map}-routing'))
        except OSError as e:
            print('Error, could not delete folder ' +
                  os.path.join(f'{OUT_PATH}', f'{wanted_map}-routing'))
