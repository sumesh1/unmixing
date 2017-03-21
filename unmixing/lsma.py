'''
Adjustments to the pysptools library to support linear spectral mixture
analysis (LSMA). Includes functions:

* `AbstractAbundanceMapper`
* `AbstractExtractor`
* `ATGP`
* `FIPPI`
* `PPI`
* `NFINDR`
* `FCLSAbundance`
* `NNLSAbundance`
* `combine_endmembers_and_normalize()`
* `convex_hull_graham()`
* `endmembers_by_maximum_angle()`
* `endmembers_by_maximum_area()`
* `endmembers_by_maximum_volume()`
* `endmembers_by_query()`
* `hall_rectification()`
* `iterate_endmember_combinations()`
* `predict_spectra_from_abundance()`
* `point_to_pixel_geometry()`
* `ravel_and_filter()`
* `report_raster_dynamic_range()`
* `subtract_endmember_and_normalize()`
* `validate_abundance_by_forward_model()`
'''

import itertools
import json
import os
import re
import numpy as np
from functools import reduce
from unmixing.utils import array_to_raster, as_raster, dump_raster, xy_to_pixel, pixel_to_xy, spectra_at_xy, rmse
from lxml import etree
from osgeo import gdal, ogr, osr
from pykml.factory import KML_ElementMaker as KML
import pysptools.eea as sp_extract
import pysptools.abundance_maps as sp_abundance
import pysptools.classification as sp_classify
import pysptools.material_count as sp_matcount

class AbstractAbundanceMapper(object):
    pass


class AbstractExtractor(object):
    def get_idx_as_kml(self, path, gt, wkt, data_dict=None):
        '''
        Exports a KML file containing the locations of the extracted endmembers
        as point markers.
        '''
        # Despite that the HSI cube is the transpose of our raster array, the
        #   coordinates returned by `get_idx()` are already in the right order
        #   (longitude, latitude) because the (m by n) == (y by x) order
        #   transposed is (n by m) == (x by y); the row index is the latitude
        #   and the column index is the longitude.
        coords = pixel_to_xy(self.get_idx(), gt=gt, wkt=wkt, dd=True)

        if any(map(lambda x: x[0] == 0 and x[1] == 0, self.get_idx())):
            print('Warning: Target endmember chosen at (0,0)')
            print('One or more endmembers may be photometric shade')

        if data_dict is None:
            data_dict = {
                'wavelength': range(1, len(coords) + 1),
                'wavelength units': 'MNF Component',
                'z plot titles': ['', '']
            }

        ico = 'http://maps.google.com/mapfiles/kml/paddle/%i.png'
        pmarks = []
        for i, pair in enumerate(coords):
            pmarks.append(KML.Placemark(
                KML.Style(
                    KML.IconStyle(
                        KML.Icon(KML.href(ico % (i + 1))))),
                KML.name(data_dict['wavelength units'] + ' %d' % (i + 1)),
                KML.Point(KML.coordinates('%f,%f' % pair))))

        doc = KML.kml(KML.Folder(*pmarks))
        with open(path, 'wb') as source:
            source.write(etree.tostring(doc, pretty_print=True))

    def get_idx_as_shp(self, path, gt, wkt):
        '''
        Exports a Shapefile containing the locations of the extracted
        endmembers. Assumes the coordinates are in decimal degrees.
        '''
        coords = pixel_to_xy(self.get_idx(), gt=gt, wkt=wkt, dd=True)

        driver = ogr.GetDriverByName('ESRI Shapefile')
        ds = driver.CreateDataSource(path)
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)

        layer = ds.CreateLayer(path.split('.')[0], srs, ogr.wkbPoint)
        for pair in coords:
            feature = ogr.Feature(layer.GetLayerDefn())

            # Create the point from the Well Known Text
            point = ogr.CreateGeometryFromWkt('POINT(%f %f)' %  pair)
            feature.SetGeometry(point) # Set the feature geometry
            layer.CreateFeature(feature) # Create the feature in the layer
            feature.Destroy() # Destroy the feature to free resources

        # Destroy the data source to free resources
        ds.Destroy()


class ATGP(sp_extract.ATGP, AbstractExtractor):
    pass


class FIPPI(sp_extract.FIPPI, AbstractExtractor):
    pass


class PPI(sp_extract.PPI, AbstractExtractor):
    pass


class NFINDR(sp_extract.NFINDR, AbstractExtractor):
    pass


class FCLSAbundance(sp_abundance.FCLS, AbstractAbundanceMapper):
    pass


class NNLSAbundance(sp_abundance.NNLS, AbstractAbundanceMapper):
    pass


def combine_endmembers_and_normalize(abundances, es=(1, 2), at_end=True, nodata=-9999):
    '''
    Combines two endmembers from a fraction image into a single endmember.
    If the original endmember abundances summed to one, they will sum to one
    in the resulting image as well. Arguments:
        abundances  The raster array of endmember abundances
        es          A two-element tuple of the endmembers indices to combine
        at_end      Place the combined endmembers at the end of the array?
        nodata      The NoData value to ignore
    '''
    shp = abundances.shape
    rast = abundances.copy() # Copy raster array
    rast[rast == nodata] = 0 # Replace NoData values
    c0 = rast[es[0], ...] # Get the endmembers to be combined
    c1 = rast[es[1], ...]

    # Stack the remaining bands
    abunds = []
    for e in range(0, shp[0]):
        if e not in es:
            abunds.append(rast[e, ...])

    if at_end:
        comps = (abunds, c0 + c1.reshape(1, shp[1], shp[2]))

    else:
        comps = (c0 + c1.reshape(1, shp[1], shp[2]), abunds)

    rast = None
    return np.vstack(comps)


def convex_hull_graham(points, indices=False):
    '''
    Returns points on convex hull of an array of points in CCW order according
    to Graham's scan algorithm. By Tom Switzer <thomas.switzer@gmail.com>.
    Arguments:
        points      The points for which a convex hull is sought
        indices     True to return a tuple of (indices, hull)
    '''
    TURN_LEFT, TURN_RIGHT, TURN_NONE = (1, -1, 0)

    def cmp(a, b):
        return (a > b) - (a < b)

    def turn(p, q, r):
        return cmp((q[0] - p[0])*(r[1] - p[1]) - (r[0] - p[0])*(q[1] - p[1]), 0)

    def keep_left(hull, r):
        while len(hull) > 1 and turn(hull[-2], hull[-1], r) != TURN_LEFT:
            hull.pop()
        if not len(hull) or hull[-1] != r:
            hull.append(r)
        return hull

    pts_sorted = sorted(points)
    l = reduce(keep_left, pts_sorted, [])
    u = reduce(keep_left, reversed(pts_sorted), [])
    hull = l.extend(u[i] for i in range(1, len(u) - 1)) or l

    if indices:
        return ([points.index(h) for h in hull], hull)

    return hull


def endmembers_by_maximum_angle(rast, targets, ref_target, gt=None, wkt=None, dd=False):
    '''
    Locates endmembers in (2-dimensional) feature space as the triad (3-corner
    simplex) that maximizes the angle formed with a reference endmember target.
    Returns the endmember coordinates in feature (not geographic) space.
    Arguments:
        rast        The raster that describes the feature space
        ref_target  The coordinates (in feature space) of a point held fixed
        targets     The coordinates (in feature space) of all other points
        gt          The GDAL GeoTransform
        wkt         The GDAL WKT projection
        dd          True for coordinates in decimal degrees

    Angle calculation from:
    http://stackoverflow.com/questions/2827393/angles-between-two-n-dimensional-vectors-in-python/13849249#13849249
    '''
    def unit_vector(vector):
        # Returns the unit vector of the vector.
        return vector / np.linalg.norm(vector)

    def angle_between(v1, v2):
        # Returns the angle in radians between vectors 'v1' and 'v2'
        v1_u = unit_vector(v1)
        v2_u = unit_vector(v2)
        return np.arccos(np.clip(np.dot(v1_u, v2_u), -1.0, 1.0))

    # Can accept either a gdal.Dataset or numpy.array instance
    if not isinstance(rast, np.ndarray):
        rastr = rast.ReadAsArray()
        gt = rast.GetGeoTransform()
        wkt = rast.GetProjection()

    else:
        assert gt is not None and wkt is not None, 'gt and wkt arguments required'
        rastr = rast.copy()

    # Get the spectra for these targets; this works in two dimensions only
    ref_spec = spectra_at_xy(rast, (ref_target,), gt, wkt)[...,0:2].reshape((2,))
    target_specs = spectra_at_xy(rast, targets, gt, wkt)[...,0:2]

    # All combinations of 2 of the targets
    combos = list(itertools.combinations(range(max(target_specs.shape)), 2))
    spec_map = [
        [target_specs[i,:] for i in triad] for triad in combos
    ]
    coord_map = [
        [targets[i] for i in triad] for triad in combos
    ]

    # Find vectors from ref_spec, not from origin (by vector subtraction)
    #   If (cx) is the ref_spec vector (line from origin to ref_spec),
    #   and (ca) and (cb) are the vectors to the points that form the angle
    #   (axb), then [(cx) - (ca)] and [(cx) - (cb)] are the vectors from point
    #   x to the points a and b, respectively.
    vectors = [(ref_spec - a, ref_spec - b) for a, b in spec_map]
    angles = [angle_between(v1, v2) for v1, v2 in vectors]
    idx = angles.index(max(angles))
    specs = spec_map[idx] # The optimized spectra
    locs = coord_map[idx] # The optimized coordinates
    specs.insert(0, ref_spec) # Add the reference target
    locs.insert(0, ref_target) # Add the reference coordinates
    return (np.array(specs), locs)


def endmembers_by_maximum_area(rast, targets, area_dim=2, gt=None, wkt=None, dd=False):
    '''
    Find up to four (4) endmembers by findng the maximum volume of the mixing
    space defined by every possible combination of endmembers where each
    endmember type appears only once. Arguments:
        rast        The raster whose mixing space is to be investigated
        targets     The pseudo-invariant feature sets, a dictionary
        area_dim    The number of dimensions to use in area calculation;
                    only 2 or 3 dimensions are supported
        gt          The GDAL GeoTransform
        wkt         The GDAL WKT projection
        dd          True for coordinates in decimal degrees
    '''
    def area(a, b, c) :
        # Courtesy of Corentin Lapeyre
        # (http://code.activestate.com/recipes/576896-3-point-area-finder/)
        return 0.5 * np.linalg.norm(np.cross(b-a, c-a))

    spec_map, coord_map = iterate_endmember_combinations(rast, targets,
        ref_target=None, ndim=3, gt=gt, wkt=wkt, dd=dd)

    area_totals = [area(*[f[0:area_dim] for f in each]) for each in spec_map]
    idx = area_totals.index(max(area_totals)) # Which set is optimal?
    specs = spec_map[idx] # The optimized spectra
    locs = list(coord_map[idx])
    return (np.array(specs), locs)


def endmembers_by_maximum_volume(rast, targets, ref_target=None, ndim=3, gt=None, wkt=None, dd=False):
    '''
    Arguments:
        rast        The raster that describes the feature space
        targets     The coordinates (in feature space) of all other points
        ref_target  (Optional) Can constrain the volume formed by one point
        gt          The GDAL GeoTransform
        wkt         The GDAL WKT projection
        dd          True for coordinates in decimal degrees

    Angle calculation from:
    http://stackoverflow.com/questions/2827393/angles-between-two-n-dimensional-vectors-in-python/13849249#13849249
    '''
    def calc_volume(spec_map):
        # Calculate the absolute volume of the determinant of the volume spanned
        #   by the construction vectors; this is proportional to the true volume
        #   of the mixing space spanned by these endmembers
        return list(map(np.abs, map(np.linalg.det, spec_map)))

    spec_map, coord_map = iterate_endmember_combinations(rast, targets, ref_target, ndim, gt, wkt, dd)

    volumes = calc_volume(spec_map)
    idx = volumes.index(max(volumes))
    specs = spec_map[idx] # The optimized spectra
    locs = list(coord_map[idx]) # The optimized coordinates

    if ref_target is not None:
        locs = list(locs)
        locs.insert(0, ref_target)

    return (np.array(specs), locs)


def endmembers_by_query(rast, query, gt, wkt, dd=False):
    '''
    Returns a list of endmember locations based on a provided query, e.g.:
    > query = rast[1,...] < -25 # Band 2 should be less than -25
    > endmembers_by_query(rast, query, gt, wkt)
    Arguments:
        rast    The raster array to find endmembers within
        query   A NumPy boolean array representing a query in the feature space
        gt      The GDAL GeoTransform
        wkt     The GDAL WKT projection
        dd      True for coordinates in decimal degrees
    '''
    assert isinstance(rast, np.ndarray), 'Requires a NumPy array'
    shp = rast.shape
    idx = np.indices((shp[-2], shp[-1]))

    # Execute query on the indices (pixel locations), then return the coordinates
    return list(pixel_to_xy([
        (x, y) for y, x in idx[:,query].T
    ], gt, wkt, dd=dd))


def hall_rectification(reference, subject, out_path, ref_set, sub_set, dd=False, nodata=-9999,
    keys=('High/Bright', 'Low/Dark')):
    '''
    Performs radiometric rectification after Hall et al. (1991) in Remote
    Sensing of Environment. Assumes first raster is the reference image and
    that none of the targets are NoData pixels in the reference image (they
    are filtered out in the subject images). Arguments:
        reference   The reference image, a gdal.Dataset
        subject     The subject image, a gdal.Dataset
        out_path    Path to a directory where the rectified images should be stored
        ref_set     Sequence of two sequences: "bright" radiometric control set,
                    then "dark" radiometric control set for reference image
        sub_set     As with ref_set, a sequence of sequences (e.g., list of two
                    lists): [[<bright targets>], [<dark targets]]
        dd          Coordinates are in decimal degrees?
        nodata      The NoData value to use fo all the rasters
        keys        The names of the dictionary keys for the bright, dark sets,
                    respectively
    '''
    # Unpack bright, dark control sets for subject image
    bright_targets, dark_targets = (sub_set[keys[0]], sub_set[keys[1]])

    # Calculate the mean reflectance in each band for bright, dark targets
    bright_ref = spectra_at_xy(reference, ref_set[keys[0]], dd=dd).mean(axis=0)
    dark_ref = spectra_at_xy(reference, ref_set[keys[1]], dd=dd).mean(axis=0)

    # Calculate transformation for the target image
    brights = spectra_at_xy(subject, bright_targets, dd=dd) # Prepare to filter NoData pixels
    darks = spectra_at_xy(subject, dark_targets, dd=dd)
    # Get the "subject" image means for each radiometric control set
    mean_bright = brights[
        np.sum(brights, axis=1) != (nodata * brights.shape[1])
    ].mean(axis=0)
    mean_dark = darks[
        np.sum(darks, axis=1) != (nodata * darks.shape[1])
    ].mean(axis=0)

    # Calculate the coefficients of the linear transformation
    m = (bright_ref - dark_ref) / (mean_bright - mean_dark)
    b = (dark_ref * mean_bright - mean_dark * bright_ref) / (mean_bright - mean_dark)

    arr = subject.ReadAsArray()
    shp = arr.shape # Remember the original shape
    mask = arr.copy() # Save the NoData value locations
    m = m.reshape((shp[0], 1))
    b = b.reshape((shp[0], 1)).T.repeat(shp[1] * shp[2], axis=0).T
    arr2 = ((arr.reshape((shp[0], shp[1] * shp[2])) * m) + b).reshape(shp)
    arr2[mask == nodata] = nodata # Re-apply NoData values

    # Dump the raster to a file
    dump_raster(array_to_raster(arr2, subject.GetGeoTransform(), subject.GetProjection()),
        os.path.join(out_path, 'rect_%s' % os.path.basename(subject.GetDescription())))


def iterate_endmember_combinations(rast, targets, ref_target=None, ndim=3, gt=None, wkt=None, dd=False):
    '''
    Creates all possible combinations of endmembers from a common pool or from
    among groups of possible endmembers (when `targets` is a dictionary). When
    a dictionary is provided, endmember combinations will contain (only) one
    endmember from each group, where the groups are defined by dictionary keys,
    i.e., `{'group1': [(x, y), ...], 'group2': [(x, y), ...], ...}`.
    Arguments:
        rast        Input raster array
        targets     Possible endmembers; as a list or dictionary
        ref_target  (Optional) Constrain the optimization to always include
                    this endmember
        ndim        Number of dimensions to limit the search to
        gt          (Optional) A GDAL GeoTransform, required for array input
        wkt         (Optional) A GDAL WKT projection, required for array input
        dd          True if the target coordinates are in decimal degrees
    '''
    # Can accept either a gdal.Dataset or numpy.array instance
    if not isinstance(rast, np.ndarray):
        rastr = rast.ReadAsArray()
        gt = rast.GetGeoTransform()
        wkt = rast.GetProjection()

    else:
        assert gt is not None and wkt is not None, 'gt and wkt arguments required'
        rastr = rast.copy()

    # `targets` is a dictionary
    if isinstance(targets, dict):
        # Get the spectra for these targets; this works in two dimensions only
        target_specs = {}
        for label, cases in targets.items():
            target_specs[label] = spectra_at_xy(rast, targets[label], gt, wkt)[...,0:ndim]

        ncom = ndim # Determinant only defined for square matrices
        if ref_target is not None:
            assert ndim == len(targets.keys()) + 1, 'Number of groups among target endmembers should be one less than the dimensionality when ref_target is used'

            ncom -= 1 # If reference target used, form combinations with 1 fewer
            ref_spec = spectra_at_xy(rast, (ref_target,), gt, wkt,
                dd=dd)[...,0:ndim].reshape((ndim,))

        # Find all possible combinations of (ncom) of these spectra
        spec_map = list(itertools.product(*[target_specs[label] for label in target_specs.keys()]))
        coord_map = list(itertools.product(*[t[1] for t in targets.items()]))

    else:
        # Get the spectra for these targets; this works in two dimensions only
        target_specs = spectra_at_xy(rast, targets, gt, wkt)[...,0:ndim]
        ncom = ndim # Determinant only defined for square matrices
        if ref_target is not None:
            ncom -= 1 # If reference target used, form combinations with 1 fewer
            ref_spec = spectra_at_xy(rast, (ref_target,), gt, wkt,
                dd=dd)[...,0:ndim].reshape((ndim,))

        # Find all possible combinations of (ncom) of these spectra
        combos = list(itertools.combinations(range(max(target_specs.shape)), ncom))
        spec_map = [[target_specs[i,:] for i in triad] for triad in combos]
        coord_map = [[targets[i] for i in triad] for triad in combos]

    # Add the reference target to each combination
    if ref_target is not None:
        spec_map = list(map(list, spec_map))
        for spec in spec_map:
            #FIXME Cannot use insert with tuples when dictionary input is provided
            spec.insert(0, ref_spec)

    return (spec_map, coord_map)


def point_to_pixel_geometry(points, source_epsg=None, target_epsg=None, pixel_side_length=30):
    '''
    Where points is a list of X,Y tuples and X and Y are coordinates in
    meters, returns a series of OGR Polygons where each Polygon is the
    pixel extent with a given point at its center. Assumes square pixels.
    Arguments:
        points      Sequence of X,Y numeric pairs or OGR Point geometries
        source_epsg The EPSG code of the source projection (Optional)
        target_epsg The EPSG code of the target projection (Optional)
        pixel_side_length   The length of one side of the intended pixel
    '''
    polys = []
    # Convert points to atomic X,Y pairs if necessary
    if isinstance(points[0], ogr.Geometry):
        points = [(p.GetX(), p.GetY()) for p in points]

    source_ref = target_ref = None
    if all((source_epsg, target_epsg)):
        source_ref = osr.SpatialReference()
        target_ref = osr.SpatialReference()
        source_ref.ImportFromEPSG(source_epsg)
        target_ref.ImportFromEPSG(target_epsg)
        transform = osr.CoordinateTransformation(source_ref, target_ref)

    for p in points:
        r = pixel_side_length / 2 # Half the pixel width
        ring = ogr.Geometry(ogr.wkbLinearRing) # Create a ring
        vertices = [
            (p[0] - r, p[1] + r), # Top-left
            (p[0] + r, p[1] + r), # Top-right, clockwise from here...
            (p[0] + r, p[1] - r),
            (p[0] - r, p[1] - r),
            (p[0] - r, p[1] + r)  # Add top-left again to close ring
        ]

        # Coordinate transformation
        if all((source_ref, target_ref)):
            vertices = [transform.TransformPoint(*v)[0:2] for v in vertices]

        for vert in vertices:
            ring.AddPoint(*vert)
        poly = ogr.Geometry(ogr.wkbPolygon)
        poly.AddGeometry(ring)
        polys.append(poly)

    return polys


def predict_spectra_from_abundance(abundances, endmembers):
    '''
    Predicts the mixed image from the endmember spectra and the fractional
    abundances, i.e. R = MA + e, where R is the reflectance of a given pixel,
    M is the matrix of endmember spectra, and A is the matrix of abundances.
    Endmembers should be in (p x k) form, where each column corresponds to the
    spectra of a given endmember. Arguments:
        abundances  The estimate abundances from LSMA
        endmembers  The endmember spectra (as a NumPy array)
    '''
    shp = abundances.shape
    return np.dot(endmembers, abundances.reshape((shp[0], shp[1]*shp[2])))\
        .reshape((endmembers.shape[0], shp[1], shp[2]))


def ravel_and_filter(arr, filter=True, nodata=-9999):
    '''
    Reshapes a (p, m, n) array to ((m*n), p) where p is the number of
    dimensions and, optionally, filters out the NoData values. Assumes the
    first axis is the shortest. Arguments:
        arr     A NumPy array with shape (p, m, n)
        filter  True to filter out NoData values (otherwise, only ravels)
        nodata  The NoData value; only used in filtering
    '''
    shp = arr.shape

    # If the array has already been raveled
    if len(shp) == 1 and filter:
        return arr[arr != nodata]

    # If a "single-band" image
    if len(shp) == 2:
        arr = arr.reshape(1, shp[-2]*shp[-1]).swapaxes(0, 1)
        if filter:
            return arr[arr != nodata]

    # For multi-band images
    else:
        arr = arr.reshape(shp[0], shp[1]*shp[2]).swapaxes(0, 1)
        if filter:
            return arr[arr[:,0] != nodata]

    return arr


def report_raster_dynamic_range(path, bands=(1,2,3,4,5,7), tpl='HDF4_EOS:EOS_GRID:"%s":Grid:sr_band%d', lj=40):
    '''
    Prints out the dynamic range of a given raster, averaged across the bands.
    Arguments:
        path    The file path to a raster
        bands   The bands within which dynamic range should be calculated
        tpl     The template for the GDAL subdataset names
        lj      The left justification (for text output)
    '''
    def dr(stats):
        return stats[1] - stats[0]

    dr_by_band = [] # Dynamic range by band
    rast, gt0, wkt = as_raster(path)

    # If the dataset is an HDF or some other linked dataset
    if rast.GetRasterBand(1) is None:
        for b in bands:
            subrast = gdal.Open(tpl % (path, b))
            try:
                stats = subrast.GetRasterBand(1).ComputeStatistics(False)

            except AttributeError:
                pass

            dr_by_band.append(dr(stats))

    # If, instead, the dataset is a flat stack like a GeoTIFF
    else:
        for i, b in enumerate(bands):
            stats = rast.GetRasterBand(i + 1).ComputeStatistics(False)
            dr_by_band.append(dr(stats))

    print('{:s} -- {:.2f} ({:.0f} s.d.)'.format(os.path.basename(path).ljust(lj),
        np.mean(dr_by_band), sum(dr_by_band)))


def subtract_endmember_and_normalize(abundances, e):
    '''
    Subtract the endmember at index e from the raster cube and normalize
    the remaining endmembers so that they sum to one. Arguments:
        abundances  A raster array cube of abundance estimates
        e           The index of the endmember to subtract
    '''
    f = e + 1 # Index after endmember index
    shp = abundances.shape
    stack = np.vstack((abundances[0:e, ...], abundances[f:shp[0], ...]))\
        .reshape((shp[0] - 1, shp[1]*shp[2]))

    return np.apply_along_axis(lambda x: x / x.sum(), 0, stack)\
        .reshape((shp[0] - 1, shp[1], shp[2]))


def validate_abundance_by_forward_model(reference, points, *abundances, r=10000, as_pct=True, dd=True, nodata=-9999):
    '''
    Validates abundance through a forward model that predicts the
    reflectance at each pixel from the reflectance of each of
    the endmembers and their fractional abundances. Arguments:
        reference   The reference image (e.g., TM/ETM+ reflectance), an np.ndarray or gdal.Dataset
        points      The XY points at which the endmembers are located
        abundances  One or more abundance maps, as file paths
        r           The number of random samples to take from the forward model
        as_pct      Report RMSE as a percentage?
        dd          XY points are in decimal degrees?
        nodata      The NoData value
    '''
    # Can accept either a gdal.Dataset or numpy.array instance
    if not isinstance(reference, np.ndarray):
        rastr = reference.ReadAsArray()

    else:
        rastr = reference.copy()

    # Get the spectra for each endmember from the reference dataset
    spectra = spectra_at_xy(rastr, points, gt = reference.GetGeoTransform(),
        wkt = reference.GetProjection(), dd = dd)

    # Convert the NoData values to zero reflectance; reshape the array
    rasrt[rastr==nodata] = 0
    spectra[spectra==nodata] = 0
    shp = rastr.shape
    rast = rastr.reshape((shp[0], shp[1]*shp[2]))

    # Generate 1000 random sampling indices
    idx = np.random.choice(np.arange(0, rast.shape[1]), r)

    # Predict the reflectances!
    stats = []
    for path in abundances:
        abundance_map, gt, wkt = as_array(path)

        # Get the predicted reflectances
        preds = predict_spectra_from_abundance(abundance_map, spectra.T)

        # Reshape the abundance map to match the reference reflectance dataset
        preds = preds.reshape(rast.shape)

        # Take the mean RMSE (sum of RMSE divided by number of pixels), after
        #   the residuals are normalized by the number of endmembers
        rmse_value = rmse(rast, preds, idx, n = spectra.shape[0]).sum() / r

        norm = 1
        if as_pct:
            # Divide by the range of the measured data; minimum is zero
            norm = rast.max()

        stats.append(rmse_value / norm)

    for i, p, in enumerate(abundances):
        if as_pct:
            print('%s%% -- [%s]' % (str(round(stats[i] * 100, 2)).rjust(15),
                os.path.basename(p)))

        else:
            print('%s -- [%s]' % (str(round(stats[i], 2)).rjust(15),
                os.path.basename(p)))

    return stats
