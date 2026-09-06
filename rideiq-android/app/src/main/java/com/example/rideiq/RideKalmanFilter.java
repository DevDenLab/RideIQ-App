package com.example.rideiq;

/**
 * A constant-velocity Kalman filter over a stream of GPS fixes.
 *
 * Why the ride tracker needs one
 * ------------------------------
 * TransitProgress trusts each raw fix completely: it snaps to the nearest stop
 * and counts. That is robust to a wandering fix in the sense that it will not
 * crash, but it is not robust in the sense that matters -- a fix that drifts
 * eighty metres down the road can advance the countdown a stop early, and the
 * rider stands up at the wrong place. The existing three-strike counter guards
 * the "wrong vehicle" warning only; nothing smooths position itself.
 *
 * And the tracker had no answer at all for the case Edmonton guarantees: the LRT
 * runs underground through downtown. Fixes stop. The old tracker simply froze on
 * its last reading, which is worst possible behaviour precisely where the rider
 * most needs to be told to get off. A filter that carries a velocity estimate can
 * keep going -- dead reckoning through the tunnel, with its uncertainty honestly
 * growing the whole way, so the caller knows how much to trust it.
 *
 * The model
 * ---------
 * State is position and velocity. Acceleration is not modelled, it is treated as
 * process noise -- which is the right call for a bus: it accelerates in ways no
 * model of ours will predict, so the honest thing is to admit that as uncertainty
 * rather than to pretend to track it.
 *
 * Because a constant-velocity model has no cross-axis coupling, north/south and
 * east/west are two independent two-state filters rather than one four-state
 * matrix problem. That is mathematically identical here and needs no matrix code
 * at all, which on a phone with no linear algebra library is worth a great deal.
 *
 * Deliberately plain Java with no Android imports, exactly like TransitProgress,
 * so the estimation can be tested on a JVM against synthetic tracks rather than
 * by walking around with a phone.
 */
public final class RideKalmanFilter {

    /**
     * Process noise: how much velocity we expect to be wrong about, per second.
     *
     * This is the one number that decides whether the filter is a filter or a
     * delay. Too small and it stops believing the GPS and sails serenely past
     * your stop; too large and it just repeats the raw fixes back to you. 1.2
     * m^2/s^3 corresponds to roughly 1.1 m/s of unmodelled acceleration over a
     * second, which is a bus pulling away from a stop.
     */
    private static final double ACCEL_NOISE = 1.2;

    /** Used when a fix arrives with no accuracy figure at all. */
    private static final double DEFAULT_ACCURACY_M = 30.0;

    /**
     * Never trust a fix as better than this, however confident it claims to be.
     * Phones routinely report 3 m in a street canyon while being 30 m out, and a
     * filter that believes them snaps to every reflection.
     */
    private static final double MIN_ACCURACY_M = 5.0;

    /**
     * Reject a fix this many standard deviations away from where we expect to be.
     *
     * 4 sigma is loose on purpose. The job is to discard the occasional wild
     * multipath jump, not to defend a belief -- a gate tight enough to be clever
     * is tight enough to lock the filter onto a wrong track and refuse every
     * correction that would fix it.
     */
    private static final double GATE_SIGMA = 4.0;

    /**
     * After this many consecutive rejections, believe the fixes instead.
     *
     * If reality and the filter disagree persistently, reality is right. Without
     * this, one bad update could wedge the filter permanently -- the classic way
     * a gated filter fails, and it fails silently.
     */
    private static final int MAX_REJECTS = 3;

    /** Metres per degree of latitude, spherical earth. Matches the backend. */
    private static final double M_PER_DEG_LAT = 6371000.0 * Math.PI / 180.0;

    /** One axis: position and velocity, with a 2x2 covariance. */
    private static final class Axis {
        double p, v;
        double p00 = 1e6, p01 = 0, p11 = 1e4;   // start knowing essentially nothing

        void predict(double dt, double q) {
            p += v * dt;
            // P = F P F' + Q, with F = [[1, dt], [0, 1]] and Q the continuous
            // white-noise-acceleration form. Written out rather than looped:
            // it is four lines, and four lines beat a matrix class here.
            double dt2 = dt * dt, dt3 = dt2 * dt;
            double n00 = p00 + 2 * dt * p01 + dt2 * p11 + q * dt3 / 3.0;
            double n01 = p01 + dt * p11 + q * dt2 / 2.0;
            double n11 = p11 + q * dt;
            p00 = n00; p01 = n01; p11 = n11;
        }

        /** Innovation for a measurement, and its variance. */
        double innovation(double z) { return z - p; }

        double innovationVar(double r) { return p00 + r; }

        void update(double z, double r) {
            double s = p00 + r;
            double k0 = p00 / s, k1 = p01 / s;
            double y = z - p;
            p += k0 * y;
            v += k1 * y;
            // P = (I - K H) P, H = [1 0]. Old values on the right-hand side.
            double o00 = p00, o01 = p01, o11 = p11;
            p00 = (1 - k0) * o00;
            p01 = (1 - k0) * o01;
            p11 = o11 - k1 * o01;
        }
    }

    private final Axis north = new Axis();      // metres north of the origin
    private final Axis east = new Axis();       // metres east of the origin

    private double originLat, originLon, lonScale;
    private boolean started = false;
    private long lastMs = 0;
    private long lastFixMs = 0;
    private int rejects = 0;

    private int fixCount = 0, rejectedCount = 0;

    /** True once a first fix has been taken and the estimate means something. */
    public boolean isReady() { return started; }

    public int fixesAccepted() { return fixCount; }

    public int fixesRejected() { return rejectedCount; }

    /**
     * Fold in one GPS fix.
     *
     * @param accuracyM the fix's own horizontal accuracy in metres, or <= 0 if
     *                  the platform did not supply one
     * @return false if the fix was rejected as an outlier
     */
    public boolean update(double lat, double lon, double accuracyM, long timeMs) {
        if (!started) {
            originLat = lat;
            originLon = lon;
            lonScale = Math.cos(Math.toRadians(lat));
            north.p = 0; east.p = 0;
            double r = variance(accuracyM);
            north.p00 = r; east.p00 = r;
            started = true;
            lastMs = lastFixMs = timeMs;
            fixCount = 1;
            return true;
        }

        double dt = (timeMs - lastMs) / 1000.0;
        if (dt < 0) {
            // Fixes out of order. GPS and network providers interleave and the
            // network one is often stale; folding it in as if it were new would
            // drag the estimate backwards along the route.
            return false;
        }
        if (dt > 0) {
            north.predict(dt, ACCEL_NOISE);
            east.predict(dt, ACCEL_NOISE);
            lastMs = timeMs;
        }

        double zNorth = (lat - originLat) * M_PER_DEG_LAT;
        double zEast = (lon - originLon) * M_PER_DEG_LAT * lonScale;
        double r = variance(accuracyM);

        // Gate on the normalised innovation, jointly across both axes.
        double dn = north.innovation(zNorth), de = east.innovation(zEast);
        double nis = (dn * dn) / north.innovationVar(r) + (de * de) / east.innovationVar(r);
        if (nis > GATE_SIGMA * GATE_SIGMA && rejects < MAX_REJECTS) {
            rejects++;
            rejectedCount++;
            return false;
        }
        rejects = 0;

        north.update(zNorth, r);
        east.update(zEast, r);
        lastFixMs = timeMs;
        fixCount++;
        return true;
    }

    /**
     * Advance the estimate to a time with no measurement -- the tunnel case.
     *
     * Uncertainty grows every time this is called and is never reduced, which is
     * the property that makes coasting safe to act on: uncertaintyMetres() tells
     * the caller exactly how much the estimate has decayed, so guidance can be
     * softened and eventually withdrawn rather than confidently wrong.
     */
    public void coastTo(long timeMs) {
        if (!started) return;
        double dt = (timeMs - lastMs) / 1000.0;
        if (dt <= 0) return;
        north.predict(dt, ACCEL_NOISE);
        east.predict(dt, ACCEL_NOISE);
        lastMs = timeMs;
    }

    public double latitude() {
        return started ? originLat + north.p / M_PER_DEG_LAT : 0;
    }

    public double longitude() {
        return started ? originLon + east.p / (M_PER_DEG_LAT * lonScale) : 0;
    }

    /** Ground speed in metres per second. */
    public double speedMps() {
        return started ? Math.hypot(north.v, east.v) : 0;
    }

    /** Direction of travel in degrees clockwise from north, or -1 if barely moving. */
    public double bearingDegrees() {
        if (!started || speedMps() < 0.5) return -1;
        double b = Math.toDegrees(Math.atan2(east.v, north.v));
        return (b + 360) % 360;
    }

    /** One-sigma position uncertainty in metres. Grows while coasting. */
    public double uncertaintyMetres() {
        if (!started) return Double.MAX_VALUE;
        return Math.sqrt(Math.max(0, north.p00) + Math.max(0, east.p00));
    }

    /** Milliseconds since the last accepted fix. */
    public long millisSinceFix(long nowMs) {
        return started ? nowMs - lastFixMs : Long.MAX_VALUE;
    }

    /** Start over -- a new trip, or a filter that has drifted past usefulness. */
    public void reset() {
        started = false;
        rejects = 0;
        fixCount = rejectedCount = 0;
        north.p = north.v = 0; north.p00 = 1e6; north.p01 = 0; north.p11 = 1e4;
        east.p = east.v = 0; east.p00 = 1e6; east.p01 = 0; east.p11 = 1e4;
    }

    private static double variance(double accuracyM) {
        double a = accuracyM > 0 ? accuracyM : DEFAULT_ACCURACY_M;
        a = Math.max(a, MIN_ACCURACY_M);
        return a * a;
    }
}
