package com.example.rideiq;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import java.util.Random;

/**
 * Tests for the ride tracker's Kalman filter.
 *
 * A filter is easy to write and hard to know you got wrong: a broken one still
 * produces smooth, plausible coordinates, and on a real phone it looks fine
 * because the truth is not available to compare against. So these tests generate
 * a track where the truth IS known -- a bus driving a straight line, a bus
 * turning, a bus entering a tunnel -- add realistic GPS noise, and assert that
 * the filtered estimate is measurably closer to the truth than the raw fixes it
 * was built from. If it is not, the filter is doing nothing worth its battery.
 */
public class RideKalmanFilterTest {

    private static final double M_PER_DEG_LAT = 6371000.0 * Math.PI / 180.0;
    private static final double LAT0 = 53.5445, LON0 = -113.4909;

    /** Metres north/east of the origin -> a coordinate. */
    private static double[] toLatLon(double north, double east) {
        double lat = LAT0 + north / M_PER_DEG_LAT;
        double lon = LON0 + east / (M_PER_DEG_LAT * Math.cos(Math.toRadians(LAT0)));
        return new double[]{lat, lon};
    }

    private static double metresBetween(double lat1, double lon1, double lat2, double lon2) {
        return TransitProgress.metres(lat1, lon1, lat2, lon2);
    }

    // ── does it actually reduce error ──────────────────────────────────────

    @Test public void smoothsANoisyStraightRunBetterThanTheRawFixes() {
        // A bus at 12 m/s (43 km/h) up a street, sampled every 5 s for 3 minutes,
        // with 25 m of GPS noise -- a normal urban fix.
        //
        // Averaged over 50 tracks, not measured on one. A single seeded track is
        // a coin toss dressed as evidence: the first version of this test picked
        // a lucky seed, asserted a 30% improvement and passed, when the true
        // expected improvement is 27%. It would have gone green today and failed
        // on someone else's change for no reason connected to that change.
        double rawTotal = 0, filteredTotal = 0;
        int n = 0;
        for (int seed = 0; seed < 50; seed++) {
            Random rng = new Random(seed);
            RideKalmanFilter f = new RideKalmanFilter();
            for (int i = 0; i < 36; i++) {
                long t = i * 5000L;
                double trueNorth = 12.0 * (t / 1000.0);
                double[] truth = toLatLon(trueNorth, 0);
                double[] noisy = toLatLon(trueNorth + rng.nextGaussian() * 25,
                                          rng.nextGaussian() * 25);
                f.update(noisy[0], noisy[1], 25, t);
                if (i >= 6) {                   // let it converge before scoring
                    rawTotal += metresBetween(truth[0], truth[1], noisy[0], noisy[1]);
                    filteredTotal += metresBetween(truth[0], truth[1],
                                                   f.latitude(), f.longitude());
                    n++;
                }
            }
        }
        double raw = rawTotal / n, filtered = filteredTotal / n;
        assertTrue("filter made it worse: raw " + Math.round(raw) + " m, filtered "
                        + Math.round(filtered) + " m", filtered < raw);
        // Measured at 0.73 of the raw error over these conditions. The bound is
        // set with room for ordinary variation but is still far from 1.0, so a
        // filter that quietly stopped filtering would fail here.
        assertTrue("expected a clear improvement, got raw " + Math.round(raw)
                        + " m vs filtered " + Math.round(filtered) + " m",
                filtered < raw * 0.80);
    }

    @Test public void learnsTheSpeedAndHeadingOfTheVehicle() {
        RideKalmanFilter f = new RideKalmanFilter();
        Random rng = new Random(9);
        for (int i = 0; i < 40; i++) {
            long t = i * 5000L;
            double[] p = toLatLon(10.0 * (t / 1000.0) + rng.nextGaussian() * 15,
                                  rng.nextGaussian() * 15);
            f.update(p[0], p[1], 15, t);
        }
        assertEquals("speed estimate is off", 10.0, f.speedMps(), 2.0);
        assertEquals("heading should be due north", 0.0,
                Math.min(f.bearingDegrees(), 360 - f.bearingDegrees()), 12.0);
    }

    // ── the tunnel, which is the whole point on the LRT ────────────────────

    @Test public void coastsThroughATunnelInsteadOfFreezing() {
        RideKalmanFilter f = new RideKalmanFilter();
        for (int i = 0; i < 20; i++) {
            long t = i * 5000L;
            double[] p = toLatLon(15.0 * (t / 1000.0), 0);
            f.update(p[0], p[1], 10, t);
        }
        long enteredAt = 19 * 5000L;
        double lastKnownNorth = 15.0 * (enteredAt / 1000.0);

        // 45 seconds underground at the same speed.
        f.coastTo(enteredAt + 45_000L);
        double[] truth = toLatLon(lastKnownNorth + 15.0 * 45, 0);

        double drift = metresBetween(truth[0], truth[1], f.latitude(), f.longitude());
        double frozen = metresBetween(truth[0], truth[1],
                toLatLon(lastKnownNorth, 0)[0], toLatLon(lastKnownNorth, 0)[1]);
        assertTrue("coasting (" + Math.round(drift) + " m) should beat freezing ("
                        + Math.round(frozen) + " m)", drift < frozen / 2);
    }

    @Test public void uncertaintyGrowsWhileCoastingAndNeverShrinks() {
        // The property that makes coasting safe to act on: guidance can be
        // softened and eventually withdrawn, rather than confidently wrong.
        RideKalmanFilter f = new RideKalmanFilter();
        for (int i = 0; i < 15; i++) {
            double[] p = toLatLon(12.0 * i * 5, 0);
            f.update(p[0], p[1], 10, i * 5000L);
        }
        double previous = f.uncertaintyMetres();
        long t = 15 * 5000L;
        for (int s = 10; s <= 120; s += 10) {
            f.coastTo(t + s * 1000L);
            double now = f.uncertaintyMetres();
            assertTrue("uncertainty shrank while coasting", now > previous);
            previous = now;
        }
        assertTrue("after two minutes blind the filter should admit it is lost, got "
                        + Math.round(previous) + " m", previous > 60);
    }

    @Test public void aFixAfterTheTunnelPullsItStraightBack() {
        RideKalmanFilter f = new RideKalmanFilter();
        for (int i = 0; i < 15; i++) {
            double[] p = toLatLon(12.0 * i * 5, 0);
            f.update(p[0], p[1], 10, i * 5000L);
        }
        long t = 15 * 5000L;
        f.coastTo(t + 60_000L);
        double blind = f.uncertaintyMetres();

        double[] real = toLatLon(12.0 * 14 * 5 + 12.0 * 60 + 40, 0);   // 40 m off
        f.update(real[0], real[1], 10, t + 60_000L);
        assertTrue("a good fix must collapse the uncertainty",
                f.uncertaintyMetres() < blind / 2);
        assertTrue(metresBetween(real[0], real[1], f.latitude(), f.longitude()) < 40);
    }

    // ── the failure modes a phone actually produces ────────────────────────

    @Test public void rejectsAWildMultipathJump() {
        RideKalmanFilter f = new RideKalmanFilter();
        for (int i = 0; i < 12; i++) {
            double[] p = toLatLon(10.0 * i * 5, 0);
            f.update(p[0], p[1], 8, i * 5000L);
        }
        double beforeLat = f.latitude(), beforeLon = f.longitude();

        // A reflection off a tower block: 800 m sideways in five seconds.
        double[] junk = toLatLon(10.0 * 12 * 5, 800);
        assertFalse("an 800 m sidestep should not be believed",
                f.update(junk[0], junk[1], 8, 12 * 5000L));
        assertTrue("the estimate moved anyway",
                metresBetween(beforeLat, beforeLon, f.latitude(), f.longitude()) < 100);
        assertEquals(1, f.fixesRejected());
    }

    @Test public void butStopsRejectingWhenRealityDisagreesPersistently() {
        RideKalmanFilter f = new RideKalmanFilter();
        for (int i = 0; i < 12; i++) {
            double[] p = toLatLon(10.0 * i * 5, 0);
            f.update(p[0], p[1], 8, i * 5000L);
        }
        // The rider got off and into a car going the other way. Every fix now
        // disagrees with the filter. A gate that never yields would wedge here
        // permanently and keep reporting the bus route -- silently.
        boolean acceptedEventually = false;
        for (int i = 12; i < 20; i++) {
            double[] p = toLatLon(-500.0 * (i - 11), 0);
            if (f.update(p[0], p[1], 8, i * 5000L)) {
                acceptedEventually = true;
            }
        }
        assertTrue("the filter wedged: it rejected every fix forever",
                acceptedEventually);
    }

    @Test public void ignoresAStaleFixThatArrivesOutOfOrder() {
        // GPS and network providers interleave, and the network one is often
        // seconds old. Folding it in as new drags the estimate back down the road.
        RideKalmanFilter f = new RideKalmanFilter();
        for (int i = 0; i < 10; i++) {
            double[] p = toLatLon(10.0 * i * 5, 0);
            f.update(p[0], p[1], 8, i * 5000L);
        }
        double lat = f.latitude(), lon = f.longitude();
        double[] stale = toLatLon(10.0 * 5 * 5, 0);
        assertFalse(f.update(stale[0], stale[1], 8, 5 * 5000L));
        assertEquals(lat, f.latitude(), 1e-9);
        assertEquals(lon, f.longitude(), 1e-9);
    }

    @Test public void doesNotBelieveAnImpossiblyPreciseFix() {
        // Phones report 3 m in a street canyon while being 30 m out. A filter
        // that believes them snaps to every reflection.
        RideKalmanFilter f = new RideKalmanFilter();
        double[] a = toLatLon(0, 0);
        f.update(a[0], a[1], 1.0, 0);
        assertTrue("accuracy should be floored, not taken at face value",
                f.uncertaintyMetres() >= 5.0);
    }

    // ── the boring but load-bearing cases ──────────────────────────────────

    @Test public void isNotReadyBeforeTheFirstFix() {
        RideKalmanFilter f = new RideKalmanFilter();
        assertFalse(f.isReady());
        assertEquals(0.0, f.speedMps(), 1e-9);
        assertEquals(Long.MAX_VALUE, f.millisSinceFix(1000));
        f.coastTo(50_000L);                      // must not throw
        assertFalse(f.isReady());
    }

    @Test public void theFirstFixIsTakenAtFaceValue() {
        RideKalmanFilter f = new RideKalmanFilter();
        double[] p = toLatLon(0, 0);
        assertTrue(f.update(p[0], p[1], 12, 0));
        assertTrue(f.isReady());
        assertEquals(p[0], f.latitude(), 1e-6);
        assertEquals(p[1], f.longitude(), 1e-6);
    }

    @Test public void aFixWithNoAccuracyIsStillUsable() {
        RideKalmanFilter f = new RideKalmanFilter();
        double[] p = toLatLon(0, 0);
        assertTrue(f.update(p[0], p[1], 0, 0));         // 0 = platform gave none
        assertTrue(f.isReady());
        assertTrue(f.uncertaintyMetres() > 5.0);
    }

    @Test public void resetStartsANewTripCleanly() {
        RideKalmanFilter f = new RideKalmanFilter();
        for (int i = 0; i < 10; i++) {
            double[] p = toLatLon(10.0 * i * 5, 0);
            f.update(p[0], p[1], 8, i * 5000L);
        }
        f.reset();
        assertFalse(f.isReady());
        assertEquals(0, f.fixesAccepted());
        double[] p = toLatLon(5000, 5000);
        assertTrue(f.update(p[0], p[1], 8, 0));
        assertEquals(p[0], f.latitude(), 1e-6);
    }

    @Test public void aStationaryVehicleReportsNoHeading() {
        RideKalmanFilter f = new RideKalmanFilter();
        Random rng = new Random(2);
        for (int i = 0; i < 20; i++) {
            double[] p = toLatLon(rng.nextGaussian() * 3, rng.nextGaussian() * 3);
            f.update(p[0], p[1], 8, i * 5000L);
        }
        assertTrue("a parked bus should not claim a direction of travel",
                f.speedMps() < 2.0);
    }
}
