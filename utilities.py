#!/usr/bin/env python
# coding: utf-8
"""
This file contains a collection of functions and constants used for generating
gridded emissions for COSMO
"""
import os
import sys
import time

import cartopy.crs as ccrs
import cartopy.io.shapereader as shpreader
import netCDF4 as nc
import numpy as np

from importlib import import_module
from multiprocessing import Pool
from shapely.geometry import Polygon

from country_code import country_codes


# constants to convert from yr -> sec
DAY_PER_YR = 365.25
SEC_PER_DAY = 86400
SEC_PER_YR = DAY_PER_YR * SEC_PER_DAY


def load_cfg(cfg_path):
    """Load config file"""
    try:
        sys.path.append(os.path.dirname(os.path.realpath(cfg_path)))
        cfg = import_module(os.path.basename(cfg_path))
    except IndexError:
        print("ERROR: no config file provided!")
        sys.exit(1)
    except ImportError:
        print(
            'ERROR: failed to import config module "%s"!'
            % os.path.basename(cfg_path)
        )
        sys.exit(1)

    return cfg


def prepare_output_file(cosmo_grid, dataset):
    """Add lat & lon dimensions and variables to the dataset, handle rotated pole

    Creates & writes dimensions and variables for longitude and latitude.
    Handles the rotated pole.

    Parameters
    ----------
    cosmo_grid : COSMODomain
        Contains information about the cosmo grid
    dataset : netCDF4.Dataset
        Writable (empty) netCDF Dataset
    """
    # Create the dimensions and the rotated pole
    if (
        cosmo_grid.pollon == 180 or cosmo_grid.pollon == 0
    ) and cosmo_grid.pollat == 90:
        lonname = "lon"
        latname = "lat"
    else:
        lonname = "rlon"
        latname = "rlat"
        var_rotpol = dataset.createVariable("rotated_pole", str)
        var_rotpol.grid_mapping_name = "rotated_latitude_longitude"
        var_rotpol.grid_north_pole_latitude = cosmo_grid.pollat
        var_rotpol.grid_north_pole_longitude = cosmo_grid.pollon
        var_rotpol.north_pole_grid_longitude = 0.0

    dataset.createDimension(lonname, cosmo_grid.nx)
    dataset.createDimension(latname, cosmo_grid.ny)

    # Create the variables associated to the dimensions
    var_lon = dataset.createVariable(lonname, "float32", lonname)
    var_lon.axis = "X"
    var_lon.units = "degrees_east"
    var_lon.standard_name = "longitude"
    var_lon[:] = cosmo_grid.lon_range()

    var_lat = dataset.createVariable(latname, "float32", latname)
    var_lat.axis = "Y"
    var_lat.units = "degrees_north"
    var_lat.standard_name = "latitude"
    var_lat[:] = cosmo_grid.lat_range()


def add_country_mask(country_mask, dataset):
    """Create and write the country mask to the dataset.

    Parameters
    ----------
    country_mask : np.array(dtype=int)
        Contains the country mask, so has the shape (lon, lat)
    dataset : netCDF4.Dataset
        Writable netCDF Dataset
    """
    if "rotated_pole" in dataset.variables:
        var = dataset.createVariable("country_ids", "short", ("rlat", "rlon"))
        var.grid_mapping = "rotated_pole"
    else:
        var = dataset.createVariable("country_ids", "short", ("lat", "lon"))

    var.long_name = "EMEP_country_code"
    # Transpose the country mask to conform with the storage of netcdf
    # python: (lon, lat), FORTRAN: (lat, lon)
    var[:] = country_mask.T


##################################
##  Regarding the country mask  ##
##################################
def check_country(country, points):
    """For a given country, return if the grid cell defined by points is within the country
    input : 
       - country
       - points : the (latitude, longitude) of the four corners of a cell
    output :
       - True if the grid cell is within the country
    """
    bounds = country.bounds  # (minx, miny, maxx, maxy)
    if (
        (bounds[0] > max([k[0] for k in points]))
        or (bounds[2] < min([k[0] for k in points]))
        or (bounds[1] > max([k[1] for k in points]))
        or (bounds[3] < min([k[1] for k in points]))
    ):
        return False
    else:
        return True


def compute_country_mask(cfg):
    """Compute the code of the country for each cosmo grid cell and store it.

    For each grid cell of the output domain, a single country code is determined.

    If for a given grid cell, no country is found (Ocean for example),
    the country code 0 is assigned.

    The resulting matrix is stored in cfg.output_path/country_mask.npy

    input :
       - cfg : config file
    """
    start = time.time()
    print("Creating the country mask")
    natural_earth = True
    if natural_earth:
        shpfilename = shpreader.natural_earth(
            resolution="110m", category="cultural", name="admin_0_countries"
        )
        iso3 = "ADM0_A3"
    else:
        shpfilename = "/usr/local/exelis/idl82/resource/maps/shape/cntry08.shp"
        iso3 = "ISO_3DIGIT"

    reader = shpreader.Reader(shpfilename)

    country_mask = np.empty((cfg.cosmo_grid.nx, cfg.cosmo_grid.ny))

    cosmo_xlocs = np.arange(
        cfg.cosmo_grid.xmin,
        cfg.cosmo_grid.xmin + cfg.cosmo_grid.dx * cfg.cosmo_grid.nx,
        cfg.cosmo_grid.dx,
    )
    cosmo_ylocs = np.arange(
        cfg.cosmo_grid.ymin,
        cfg.cosmo_grid.ymin + cfg.cosmo_grid.dy * cfg.cosmo_grid.ny,
        cfg.cosmo_grid.dy,
    )
    """
    Be careful with numpy.arange(). Floating point numbers are not exactly
    represented. Thus, the length of the generated list could have one entry
    too much.
    See: https://stackoverflow.com/questions/47243190/numpy-arange-how-to-make-precise-array-of-floats
    """
    if len(cosmo_xlocs) == cfg.cosmo_grid.nx + 1:
        cosmo_xlocs = cosmo_xlocs[:-1]
    if len(cosmo_ylocs) == cfg.cosmo_grid.ny + 1:
        cosmo_ylocs = cosmo_ylocs[:-1]

    transform = ccrs.RotatedPole(
        pole_longitude=cfg.cosmo_grid.pollon,
        pole_latitude=cfg.cosmo_grid.pollat,
    )
    incr = 0
    no_country_code = []

    european = []
    non_euro = []
    for country in reader.records():
        if country.attributes["CONTINENT"] == "Europe":
            european.append(country)
        else:
            non_euro.append(country)

    for (a, x) in enumerate(cosmo_xlocs):
        for (b, y) in enumerate(cosmo_ylocs):
            """Progress bar"""
            incr += 1
            sys.stdout.write("\r")
            sys.stdout.write(
                " {:.1f}%".format(
                    (
                        100
                        / ((cfg.cosmo_grid.nx * cfg.cosmo_grid.ny) - 1)
                        * ((a * cfg.cosmo_grid.ny) + b)
                    )
                )
            )
            sys.stdout.flush()

            mask = []

            """Get the corners of the cell in lat/lon coord"""
            # TO CHECK : is it indeed the bottom left corner ?
            # cosmo_cell_x = [x+cfg.dx,x+cfg.dx,x,x]
            # cosmo_cell_y = [y+cfg.dy,y,y,y+cfg.dy]
            # Or the center of the cell
            cosmo_cell_x = np.array(
                [
                    x + cfg.cosmo_grid.dx / 2,
                    x + cfg.cosmo_grid.dx / 2,
                    x - cfg.cosmo_grid.dx / 2,
                    x - cfg.cosmo_grid.dx / 2,
                ]
            )
            cosmo_cell_y = np.array(
                [
                    y + cfg.cosmo_grid.dy / 2,
                    y - cfg.cosmo_grid.dy / 2,
                    y - cfg.cosmo_grid.dy / 2,
                    y + cfg.cosmo_grid.dy / 2,
                ]
            )

            points = ccrs.PlateCarree().transform_points(
                transform, cosmo_cell_x, cosmo_cell_y
            )
            polygon_cosmo = Polygon(points)

            """To be faster, only check european countries at first"""
            for country in european:  # reader.records():
                if check_country(country, points):
                    # if x+cfg.dx<bounds[0] or y+cfg.dy<bounds[1] or x>bounds[2] or y>bounds[3]:
                    #     continue
                    if polygon_cosmo.intersects(country.geometry):
                        mask.append(country.attributes[iso3])

            """If not found among the european countries, check elsewhere"""
            if len(mask) == 0:
                for country in non_euro:  # reader.records():
                    if check_country(country, points):
                        # if x+cfg.dx<bounds[0] or y+cfg.dy<bounds[1] or x>bounds[2] or y>bounds[3]:
                        #     continue
                        if polygon_cosmo.intersects(country.geometry):
                            mask.append(country.attributes[iso3])

            """If more than one country, assign the one which has the greatest area"""
            if len(mask) > 1:
                area = 0
                for country in [
                    rec
                    for rec in reader.records()
                    if rec.attributes[iso3] in mask
                ]:
                    new_area = polygon_cosmo.intersection(country.geometry).area
                    if area < new_area:
                        area = new_area
                        new_mask = [country.attributes[iso3]]
                mask = new_mask

            """Convert the name to ID"""
            if len(mask) == 1:
                try:
                    mask = [country_codes[mask[0]]]
                except KeyError:
                    no_country_code.append(mask[0])
                    mask = [-1]

            # If no country (ocean), then assign the ID 0
            if len(mask) == 0:
                mask = [0]

            country_mask[a, b] = mask[0]

    print("\nCountry mask is done")
    end = time.time()
    print("it took", end - start, "seconds")
    if len(no_country_code) > 0:
        print(
            "The following countries were found, but didn't have a corresponding code"
        )
        print(set(no_country_code))

    np.save(os.path.join(cfg.output_path, "country_mask.npy"), country_mask)


def get_country_mask(cfg):
    """Calculate the country mask"""
    add_country_mask = True
    cmask_path = os.path.join(cfg.output_path, "country_mask.npy")
    cmask_path_nc = os.path.join(cfg.output_path, "country_mask.nc")

    if os.path.isfile(cmask_path_nc):
        print(
            "Do you want to use the country mask found in %s ?" % cmask_path_nc
        )
        s = input("[y]/n \n")
        if s == "y" or s == "":
            with nc.Dataset(cmask_path_nc, "r") as inf:
                return inf.variables["country_mask"][:]
    elif os.path.isfile(cmask_path):
        print(
            "Do you wanna overwite the country mask found in %s ?" % cmask_path
        )
        s = input("y/[n] \n")
        add_country_mask = s == "y"

    if add_country_mask:
        # TODO: return country mask, store it here
        compute_country_mask(cfg)

    return np.load(cmask_path)


def cell_corners(lon_var, lat_var, inv_name, i, j, cfg):
    if inv_name == "tno":
        x_tno = lon_var[i]
        y_tno = lat_var[j]
        cell_x = np.array(
            [
                x_tno + cfg.tno_dx / 2,
                x_tno + cfg.tno_dx / 2,
                x_tno - cfg.tno_dx / 2,
                x_tno - cfg.tno_dx / 2,
            ]
        )
        cell_y = np.array(
            [
                y_tno + cfg.tno_dy / 2,
                y_tno - cfg.tno_dy / 2,
                y_tno - cfg.tno_dy / 2,
                y_tno + cfg.tno_dy / 2,
            ]
        )
        proj = ccrs.PlateCarree()
    elif inv_name == "vprm":
        globe = ccrs.Globe(
            ellipse=None, semimajor_axis=6370000, semiminor_axis=6370000
        )
        lambert = ccrs.LambertConformal(
            central_longitude=12.5,
            central_latitude=51.604,
            standard_parallels=[51.604],
            globe=globe,
        )

        center_lambert = lambert.transform_point(
            lon_var[j, i], lat_var[j, i], ccrs.PlateCarree()
        )
        cell_x = np.array(
            [
                center_lambert[0] + cfg.tno_dx / 2,
                center_lambert[0] + cfg.tno_dx / 2,
                center_lambert[0] - cfg.tno_dx / 2,
                center_lambert[0] - cfg.tno_dx / 2,
            ]
        )
        cell_y = np.array(
            [
                center_lambert[1] + cfg.tno_dy / 2,
                center_lambert[1] - cfg.tno_dy / 2,
                center_lambert[1] - cfg.tno_dy / 2,
                center_lambert[1] + cfg.tno_dy / 2,
            ]
        )
        proj = lambert
    elif inv_name == "edgar":
        x_tno = lon_var[i]
        y_tno = lat_var[j]
        cell_x = np.array(
            [x_tno + cfg.edgar_dx, x_tno + cfg.edgar_dx, x_tno, x_tno]
        )
        cell_y = np.array(
            [y_tno + cfg.edgar_dy, y_tno - cfg.edgar_dy, y_tno, y_tno]
        )
        proj = ccrs.PlateCarree()
    elif (
        inv_name == "meteotest"
        or inv_name == "maiolica"
        or inv_name == "carbocount"
    ):
        x1_ch, y1_ch = swiss2wgs84(lat_var[j], lon_var[i])  # i-lon, j-lat
        x2_ch, y2_ch = swiss2wgs84(lat_var[j] + 200, lon_var[i] + 200)
        cell_x = np.array([x2_ch, x2_ch, x1_ch, x1_ch])
        cell_y = np.array([y2_ch, y1_ch, y1_ch, y2_ch])
        proj = ccrs.PlateCarree()
    else:
        print(
            "Inventory %s is not supported yet. Consider defining your own or using tno or vprm."
            % inv_name
        )

    return cell_x, cell_y, proj


def get_dim_var(inv, inv_name, cfg):
    if inv_name == "tno":
        lon_dim = inv.dimensions["longitude"].size
        lat_dim = inv.dimensions["latitude"].size
        lon_var = inv["longitude"][:]
        lat_var = inv["latitude"][:]
    elif inv_name == "vprm":
        lon_dim = inv.dimensions["west_east"].size
        lat_dim = inv.dimensions["south_north"].size
        lon_var = inv["lon"][:]
        lat_var = inv["lat"][:]
    elif inv_name == "edgar":
        lon_var = np.arange(cfg.edgar_xmin, cfg.edgar_xmax, cfg.edgar_dx)
        lat_var = np.arange(cfg.edgar_ymin, cfg.edgar_ymax, cfg.edgar_dy)
        lon_dim = len(lon_var)
        lat_dim = len(lat_var)
    elif (
        inv_name == "meteotest"
        or inv_name == "maiolica"
        or inv_name == "carbocount"
    ):
        lon_var = np.array(
            [cfg.ch_xll + i * cfg.ch_cell for i in range(0, cfg.ch_xn)]
        )
        lat_var = np.array(
            [cfg.ch_yll + i * cfg.ch_cell for i in range(0, cfg.ch_yn)]
        )
        lon_dim = np.shape(lon_var)[0]
        lat_dim = np.shape(lat_var)[0]
    else:
        print(
            "Inventory %s is not supported yet. Consider defining your own or using tno or vprm."
            % inv_name
        )

    return lon_dim, lat_dim, lon_var, lat_var


def compute_map_from_inventory_to_cosmo(cosmo_grid, inv_grid, nprocs):
    """Compute the mapping from inventory to cosmo grid.

    Loop over all inventory cells and determine which cosmo cells they overlap
    with. This is done in parallel.

    The result is a 2d array, where for each inventory cell a list is stored.
    That list contains triplets (i, j, r), where i, j are the indices of
    cosmo cells. r is the ratio of the overlap between the inventory and the
    cosmo cell and the total area of the inventory cell.

    Parameters
    ----------
    cosmo_grid : grids.COSMOGrid
        Contains all necessary information about the cosmo grid
    inv_grid : grids.InventoryGrid
        Contains all necessary information about the inventory grid
    nprocs : int
        Number of processes used to compute the mapping in parallel
    """
    print(
        "Retrieving the interpolation between the cosmo and the inventory grids"
    )
    start = time.time()

    lon_size = len(inv_grid.lon_range())
    lat_size = len(inv_grid.lat_range())

    # This is the interpolation that will be returned
    mapping = np.empty((lon_size, lat_size), dtype=object)

    # Projection used to convert from the inventory coordinate system
    inv_projection = inv_grid.get_projection()

    # Projection used to convert to the cosmo grid
    cosmo_projection = cosmo_grid.get_projection()

    with Pool(nprocs) as pool:
        for i in range(lon_size):
            print("ongoing :", i)
            cells = []
            for j in range(lat_size):
                inv_cell_corners_x, inv_cell_corners_y = inv_grid.cell_corners(
                    i, j
                )
                cell_in_cosmo_projection = cosmo_projection.transform_points(
                    inv_projection, inv_cell_corners_x, inv_cell_corners_y
                )
                cells.append(cell_in_cosmo_projection)

            mapping[i, :] = pool.map(cosmo_grid.intersected_cells, cells)

    end = time.time()
    print("\nInterpolation is over")
    print("it took ", end - start, "seconds")

    return mapping


def get_gridmapping(output_path, cosmo_grid, inv_grid, nprocs):
    """retrieve the interpolation between the tno and cosmo grids."""
    make_map = True
    mapping_path = os.path.join(output_path, "mapping.npy")
    if os.path.isfile(mapping_path):
        print("Do you wanna overwite the mapping found in %s ?" % mapping_path)
        answer = input("y/[n] \n")
        make_map = answer == "y"

    if make_map:
        mapping = compute_map_from_inventory_to_cosmo(
            cosmo_grid, inv_grid, nprocs
        )

        np.save(mapping_path, mapping)
    else:
        mapping = np.load(mapping_path)

    return mapping


def swiss2wgs84(x, y):
    """
    Convert Swiss LV03 coordinates (x easting and y northing) to WGS 84 based
    on swisstopo approximated soluation (0.1" accuracy).

    remove the first digit of x,y
    """
    x = (x - 200000.0) / 1000000.0
    y = (y - 600000.0) / 1000000.0

    lon = (
        2.6779094
        + 4.728982 * y
        + 0.791484 * y * x
        + 0.1306 * y * x ** 2
        - 0.0436 * y ** 3
    ) / 0.36

    lat = (
        16.9023892
        + 3.238272 * x
        - 0.270978 * y ** 2
        - 0.002528 * x ** 2
        - 0.0447 * y ** 2 * x
        - 0.0140 * x ** 3
    ) / 0.36

    return lon, lat
