#!/usr/bin/python
#-*- coding:utf-8 -*-

import geojson
import glob
import os
import os.path
import requests
import sys
import time

# List of regions to block. these regions are "collections" of other countries/regions/states 
Block_Download = ['africa', 'alps', 'asia', 'australia-oceania', 'britain-and-ireland', 'canada', 'dach', 'europe', 'great-britain', 'norcal',
                'north-america', 'russia', 'socal', 'south-africa-and-lesotho' , 'south-america', 'us', 'us-midwest', 'us-northeast', 'us-pacific', 'us-south', 'us-west']

Download_Missing_Maps = 1
Max_Days_Old = 30
#region = 'africa'
#region = 'antarctica'
#region = 'asia'
#region = 'australia-oceania'
#region = 'central-america'
#region = 'europe'
#region = 'north-america'
region = 'russia'
#region = 'south-america'


url = ''

# Find the maps to download from Geofabrik for a given range of tiles
# arguments are 
#   - list of tiles of the desired region bounding box
#   - name of desired region as used in Geofabrik json file
#   - polygon of desired region as present in the Geofabrik json file
def Build_Country_List(region_to_download):
    output = []
    must_download_maps = []
    must_download_urls = []
    
    with open('geofabrik.json', encoding='utf8') as f:
        geofabrik_json_data = geojson.load(f)
    f.close()

    # itterate through countries/regions in the geofabrik json file
    for regions in geofabrik_json_data.features:
        props = regions.properties
        regionname = props.get('id', '')
        parent = props.get ('parent', '')
        rurls = props.get('urls', '')
        rurl = rurls.get('pbf', '')
        
        #if regionname not in must_download_maps and 'europe' in rurl:
        #if regionname not in must_download_maps and parent == 'africa':
        #if regionname not in must_download_maps and parent == 'asia':
        #if regionname not in must_download_maps and parent == 'australia-oceania':   
        #if regionname not in must_download_maps and parent == 'central-america': 
        #if regionname not in must_download_maps:
        if regionname not in must_download_maps and region_to_download in rurl:
            must_download_maps.append (regionname)
            must_download_urls.append (rurl)
        
    # first replace any forward slashes with underscores (us/texas to us_texas)
    must_download_maps = [sub.replace('/', '_') for sub in must_download_maps]
    output.append ({'countries':must_download_maps, 'urls':must_download_urls})
    return output

CurDir = os.getcwd() # Get Current Directory
geofabrik_json_file = os.path.join (CurDir, 'geofabrik.json')

# is geofabrik json file present and not older then Max_Days_Old?
now = time.time()
To_Old = now - 60 * 60 * 24 * Max_Days_Old
try:
    FileCreation = os.path.getmtime(geofabrik_json_file)
    if FileCreation < To_Old:
        print (f'# Deleting old Geofabriks json file')
        os.remove(os.path.join (CurDir, 'geofabrik.json'))
except:
    pass
    
# if not present download Geofabriks json file
if not os.path.exists(geofabrik_json_file) or not os.path.isfile(geofabrik_json_file):
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
    
List_of_Countries = Build_Country_List(region)
#print (f'List_of_Countries = {List_of_Countries}')
#sys.exit()

if Download_Missing_Maps == 1:
    for Line in List_of_Countries:
        i = 0
        for Country in Line['countries']: # we want this osm.pbf
            if Country not in Block_Download:
                try:
                    FileCreation = os.path.getmtime(f'maps\\{Country}-latest.osm.pbf')
                    if FileCreation < To_Old:
                        print (f'# Deleting old {Country}-latest.osm.pbf file')
                        #os.remove(os.path.join (CurDir, f'{Country}-latest.osm.pbf'))
                except:
                    pass
                print(f'# Checking mapfile for {Country}')
                map_files = glob.glob(f'maps\\{Country}-latest.osm.pbf') # Search for it in the map folder
                if len(map_files) != 1 or not os.path.isfile(map_files[0]): # not found so download it
                    print(f'# Trying to download missing map of {Country}.')
                    url = Line['urls'][i]
                    r = requests.get(url, allow_redirects=True, stream = True)
                    if r.status_code != 200:
                        print(f'failed to find or download country: {Country}')
                        sys.exit()
                    Download=open(os.path.join (f'maps\\{Country}' + '-latest.osm.pbf'), 'wb')
                    for chunk in r.iter_content(chunk_size=10240):
                        Download.write(chunk)
                    Download.close()
                    map_files = [os.path.join (f'maps\\{Country}' + '-latest.osm.pbf')]
                    print(f'# Map of {Country} downloaded.')
            i += 1
        
# append custom maps
#List_of_Countries.append ({'countries':'planet-210906', 'urls':''})

outFile = os.path.join(f'generate-{region}.bat')
with open(outFile, 'w') as of:
    for Line in List_of_Countries:
        for Country in Line['countries']:
            if Country not in Block_Download:
                of.write (f'python wahoo-map-creator-osmosis.py {Country}\n')
    of.close()
    
outFile = os.path.join(f'generate-{region}-routing.bat')
with open(outFile, 'w') as of:
    for Line in List_of_Countries:
        for Country in Line['countries']:
            if Country not in Block_Download:
                of.write (f'python wahoo-map-creator-osmosis-route.py {Country}\n')
    of.close()

outFile = os.path.join(f'maps\\update-{region}.sh')
with open(outFile, 'w', newline='') as of:
    of.write (f'error=1\n')
    of.write (f'while [ $error -gt 0 ]; do\n')
    of.write (f'error=0\n')
    for Line in List_of_Countries:
        for Country in Line['countries']:
            if Country not in Block_Download:
                of.write (f'date\n')
                of.write (f'echo "processing {Country}"\n')
                of.write (f'status=1  # we want more data\n')
                #of.write (f'while [ $status -eq 1 ]; do\n')
                of.write (f'while [ $status -gt 0 ]; do\n')
                of.write (f'  pyosmium-up-to-date -v -v -v --force-update-of-old-planet {Country}-latest.osm.pbf\n')
                of.write (f'  # save the return code\n')
                of.write (f'  status=$?\n')
                of.write (f'  echo "Status = $status"\n')
                of.write (f'  if [ $status -gt 0 ];\n')
                of.write (f'  then\n')
                of.write (f'    sleep 5\n')
                of.write (f'  fi\n')
                of.write (f'done\n')
                of.write (f'date\n')
                of.write (f'if [ $status -gt 0 ];\n')
                of.write (f'then\n')
                of.write (f'  echo "Problem with {Country}? status = $status" >> "update.log"\n')
                of.write (f'  ((error=error+1))\n')
                of.write (f'fi\n')
 #               of.write (f'if [ $status -gt 2 ];\n')
 #               of.write (f'then\n')
 #               of.write (f'    echo "Problem with {Country}? status = $status"\n')
 #               of.write (f'    read -p "Press any key to continue... " -n1 -s\n')
 #               of.write (f'fi\n')
                of.write (f'echo ""\n')
    of.write (f'    echo "Errors = $error" >> "update.log"\n')
    of.write (f'done\n')
        
    of.close()