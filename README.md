This is my, mostly, Windows version of a map generator for the Wahoo line of bicycle computers.

The whole reason for it's existence is the very, very low update rate from Wahoo itself.
Other than that it also includes features that are not in the Wahoo maps like extra/other landuse rendering and a lot of different POI's

Before reading further, if you are just looking for new map and routing files have a look here: http://wahoomaps.higli.de/
Here you'll find map files that I try to update every 3 months, routing files that are updated either every 3 to 6 months and some sample themes that make use of all the extra map features and POI's that I include but generally are not in the Wahoo maps.

It started out as a Windows port of a Linux version by Henk (Intyre on Github) and evolved from there.

Treee111 has made a more user friendly version of my work here https://github.com/treee111/wahooMapsCreator but unfortunately he is not very active on this project anymore.
And subsequently the version here has some features not found in Treee111's version.
Also look at his repo for instructions on how to install the maps etc. as it has extensive documentation.

However I would still very much encourage people to use his version over my version here. 
It takes a lot of, undocumented, work to get this working whereas Treee111's version has an installer for example.
If you still want to try my version here, time allowing I will try to help people. 
The best place to reach me and others trying to help people with the Wahoo devices in general is the telegram group here https://t.me/+Mor244hPZw85YTVk

!!! The information below is severely outdated !!!

Version history:
V1.0 2021-04-09 Initial adaption from Henk's Osmium version
V1.1 2021-04-10 Switched to osmconvert for tile extraction for speed. 
V1.2 2021-04-25 Attempt at auto map downloading, changes in command line, 
                bit of error handling and working on it ;-)
               
V2.0 2021-08-26 Switched to using Geofabrik for the json country/region information 
                Tried creating Wahoo v12 maps but gave up because of the huge amount of inconsistancies in the maps
                and in valus used in different maps.


Firstly, this is the windows "port" of wahoo-map-creator-osmium.py by Henk.

- Minimal usage instructions:
Run wahoo-map-creator-osmosis from the commandline like this:
python wahoo-map-creator-osmosis.py <country-name or region name> or "<country-name or region name>"
or
python3 wahoo-map-creator-osmosis.py <country-name or region name> or "<country-name or region name>"
Example: python3 wahoo-map-creator-osmosis.py netherlands
or python3 wahoo-map-creator-osmosis.py "united kingdom"

You can get the country/region names to use from this url: https://download.geofabrik.de/
For the correct name to use if it's not clear, look at the names of the files to download.
For Australia and Oceania for example the filename of the download is
australia-oceania-latest.osm.pbf
So australia-oceania will work as well as "australia oceania"
 
Depending on your python installation. If you need to use python3 then
you also need to edit wahoo-map-creator-osmosis.py and change line 102 from
cmd = ['python', 'shape2osm.py', '-l', outFile, landFile]
to
cmd = ['python3', 'shape2osm.py', '-l', outFile, landFile]
You need Python 3.5 or higher.

That should be it for running the program itself.
Not unimportant, after completion of a map the output map files as well as the .map.lzma files
needed for the Wahoo devices and a zip file containing them all is
located in the output folder. 

To be able to run the program you need to have Python > 3.5 installed. 
Furthermore you need to have ogr2ogr installed. 
I downloaded gdal (which contains ogr2ogr) from:
http://www.gisinternals.com/release.php
Following the guide here: 
https://sandbox.idre.ucla.edu/sandbox/tutorials/installing-gdal-for-windows
This guide is good but misses a needed environment variable: 
PROJ_LIB "C:\Program Files\GDAL\projlib"
See: 
https://stackoverflow.com/questions/56764046/gdal-ogr2ogr-cannot-find-proj-db-error

I myself have Python 3.7.4 installed and used gdal-302-1928-core.msi 
and GDAL-3.2.1.win32-py3.7.msi 
The later looks like it wants to install Python but it is actually asking 
you for the location where Python is installed.

Update 22-10-2022:
There is an error(??) in the python bindings. You need to change the line import osr in the file ogr.py, 
for me located in c:\Users\[username]\AppData\Local\Programs\Python\Python39\Lib\site-packages\osgeo\ 
to from osgeo import osr for shape2osm to work.

Two additional Phython modules are needed shapely and geojson.
It depends on your setup, but I could install them with
pip install geojson and pip install shapely from the commandprompt

It is highly recommended to use the 64bit version of java and increase the java memory heap to more then 3Gb.
See the osmosis.bat file in the osmosis\bin directory.

Hope this is somewhat complete, if not, let me know here:
https://groups.google.com/g/wahoo-elemnt-users/c/PSrdapfWLUE
