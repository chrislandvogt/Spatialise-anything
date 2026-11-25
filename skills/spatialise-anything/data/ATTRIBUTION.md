# Bundled data

`gazetteer.sqlite` is derived from the Australian Bureau of Statistics,
Australian Statistical Geography Standard (ASGS) Edition 3 digital boundary
files, GDA2020:

- Suburbs and Localities (SAL) 2021
- Postal Areas (POA) 2021
- Local Government Areas (LGA) 2025

Source: Australian Bureau of Statistics. Licensed under Creative Commons
Attribution 4.0 International (CC BY 4.0),
https://creativecommons.org/licenses/by/4.0/.

Changes made: boundaries simplified (about 200 m for localities and postal
areas, 400 m for LGAs), parts smaller than about 0.02 km2 removed,
coordinates rounded to 1e-4 degrees, label points and equal-area radii
computed, and each locality assigned the LGA containing its label point.
The data is not endorsed by the ABS.

Postal Areas are an ABS approximation of Australia Post postcodes built from
mesh blocks; they do not match delivery areas exactly.

Rebuild with `tools/build_gazetteer.py`.
