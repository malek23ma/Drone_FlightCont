/* Local tangent plane - see geo.h for why this file has no dependencies.
 * Host-tested by tools/geo_test.py. */

#include "geo.h"

/* WGS84. */
#define GEO_A 6378137.0                 /* semi-major axis, m */
#define GEO_E2 0.00669437999014         /* first eccentricity squared */
#define GEO_DEG2RAD 0.017453292519943295
#define GEO_1E7_TO_DEG 1.0e-7

/* sin and cos by series, valid over the latitude range [-pi/2, pi/2].
 *
 * math.h is not available here (see geo.h), and these run exactly once
 * per home latch, so a series is both sufficient and honest. Horner form,
 * terms to x^11 / x^10: the worst case is the pole, where the truncation
 * error is around 1e-9 - about 6 nanometres of scale factor. At the
 * latitudes this aircraft flies it is far below that. */
static double geo_sin(double x)
{
    const double x2 = x * x;
    return x * (1.0 + x2 * (-1.0 / 6.0 + x2 * (1.0 / 120.0 +
           x2 * (-1.0 / 5040.0 + x2 * (1.0 / 362880.0 +
           x2 * (-1.0 / 39916800.0))))));
}

/* cos, with the argument reduced to [0, pi/4].
 *
 * Neither series alone holds RELATIVE accuracy across a full quadrant,
 * and both failures were measured rather than guessed:
 *   - a direct cos series is good to ~4e-7 absolute, but cos is itself
 *     small near the pole, so at 85 deg that became 3e-6 relative and the
 *     longitude scale factor came out 26 mm per degree wrong;
 *   - computing cos as sin(pi/2 - x) everywhere fixes the pole and then
 *     misses at the EQUATOR, where the sin series is evaluated at pi/2,
 *     its own worst point, for 6 mm per degree.
 * Reducing to the octant means each series is only ever evaluated where
 * its truncation error is around 1e-10, and the small answer always comes
 * from a small argument. cos is even, hence the |x|. */
static double geo_cos(double x)
{
    const double ax = (x < 0.0) ? -x : x;
    if (ax > 0.7853981633974483) return geo_sin(1.5707963267948966 - ax);
    const double x2 = ax * ax;
    return 1.0 + x2 * (-0.5 + x2 * (1.0 / 24.0 + x2 * (-1.0 / 720.0 +
           x2 * (1.0 / 40320.0 + x2 * (-1.0 / 3628800.0)))));
}

void geo_home_clear(geo_home_t *h)
{
    h->lat0_1e7 = 0;
    h->lon0_1e7 = 0;
    h->alt0_mm = 0;
    h->m_per_1e7_lat = 0.0;
    h->m_per_1e7_lon = 0.0;
    h->valid = 0;
}

void geo_set_home(geo_home_t *h, int32_t lat_1e7, int32_t lon_1e7,
                  int32_t alt_mm)
{
    h->lat0_1e7 = lat_1e7;
    h->lon0_1e7 = lon_1e7;
    h->alt0_mm = alt_mm;

    const double phi = (double)lat_1e7 * GEO_1E7_TO_DEG * GEO_DEG2RAD;
    const double s = geo_sin(phi);
    const double c = geo_cos(phi);

    /* 1 - e^2 sin^2(phi), the term both radii of curvature hang off. */
    double w2 = 1.0 - GEO_E2 * s * s;
    if (w2 < 1.0e-12) w2 = 1.0e-12;          /* cannot happen; not a crash */
    const double w = __builtin_sqrt(w2);

    /* Meridional radius M = a(1-e^2) / w^3  -> metres per radian of lat.
     * Normal radius     N = a / w           -> metres per radian of lon,
     *                                          scaled by cos(phi). */
    const double m_rad = GEO_A * (1.0 - GEO_E2) / (w2 * w);
    const double n_rad = GEO_A / w;

    const double per_1e7 = GEO_1E7_TO_DEG * GEO_DEG2RAD;
    h->m_per_1e7_lat = m_rad * per_1e7;
    h->m_per_1e7_lon = n_rad * c * per_1e7;
    h->valid = 1;
}

int geo_to_ned(const geo_home_t *h, int32_t lat_1e7, int32_t lon_1e7,
               int32_t alt_mm, float *n, float *e, float *d)
{
    if (!h->valid) return 0;

    /* int64 for the differences. Longitude spans -1800000000..1800000000,
     * so a subtraction of two int32 longitudes can reach 3.6e9 and
     * overflow int32 - which would silently flip the sign of the position
     * for any pair straddling the meridian. */
    int64_t dlat = (int64_t)lat_1e7 - (int64_t)h->lat0_1e7;
    int64_t dlon = (int64_t)lon_1e7 - (int64_t)h->lon0_1e7;

    /* Wrap the longitude difference into +-180 deg so a home point beside
     * the antimeridian does not read as most of the way round the planet.
     * Irrelevant in Istanbul; costs three lines and removes a whole class of
     * nonsense number. */
    if (dlon > 1800000000LL) dlon -= 3600000000LL;
    else if (dlon < -1800000000LL) dlon += 3600000000LL;

    *n = (float)((double)dlat * h->m_per_1e7_lat);
    *e = (float)((double)dlon * h->m_per_1e7_lon);
    /* Down is positive, and altitude is up, so the sign flips here. */
    *d = (float)(-(double)((int64_t)alt_mm - (int64_t)h->alt0_mm) * 0.001);
    return 1;
}
