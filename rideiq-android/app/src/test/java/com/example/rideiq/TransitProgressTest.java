package com.example.rideiq;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import java.util.ArrayList;
import java.util.List;

/**
 * The first tests in the Android app.
 *
 * They target TransitProgress because it decides the only thing riding guidance
 * exists to say -- when to stand up -- and because it fails silently. A mistake
 * here does not crash: it counts down to the wrong stop, confidently, and the
 * rider believes it. That is exactly the shape of bug a test catches and a human
 * staring at an emulator does not.
 *
 * Plain JVM tests, no instrumentation, because TransitProgress carries no Android
 * imports on purpose.
 */
public class TransitProgressTest {

    // A real fragment of ETS bus 523 along 34 Avenue, with real coordinates, so
    // the distance maths is exercised on the geometry it will actually see.
    private static ApiModels.TransitStop stop(String name, double lat, double lon) {
        ApiModels.TransitStop s = new ApiModels.TransitStop();
        s.name = name; s.lat = lat; s.lon = lon; s.code = "x";
        return s;
    }

    private static ApiModels.TransitLeg walk() {
        ApiModels.TransitLeg l = new ApiModels.TransitLeg();
        l.mode = "WALK"; l.fromName = "Origin"; l.toName = "58 Street & 34 Avenue";
        return l;
    }

    private static ApiModels.TransitLeg bus523() {
        ApiModels.TransitLeg l = new ApiModels.TransitLeg();
        l.mode = "BUS"; l.modeLabel = "Bus"; l.route = "523";
        l.headsign = "Mill Woods"; l.depart = "08:48";
        l.stops = new ArrayList<>();
        l.stops.add(stop("58 Street & 34 Avenue", 53.465572, -113.428895));
        l.stops.add(stop("66 Street & 34 Avenue", 53.465600, -113.440000));
        l.stops.add(stop("91 Street & 34 Avenue", 53.465700, -113.455700));
        l.stops.add(stop("99 Street & 42 Avenue", 53.478267, -113.485962));
        l.stops.add(stop("99 Street & 44 Avenue", 53.482114, -113.485953));
        return l;
    }

    private static ApiModels.TransitLeg bus055() {
        ApiModels.TransitLeg l = new ApiModels.TransitLeg();
        l.mode = "BUS"; l.modeLabel = "Bus"; l.route = "055";
        l.headsign = "West Edmonton Mall";
        l.stops = new ArrayList<>();
        l.stops.add(stop("99 Street & Whitemud Drive", 53.480843, -113.487243));
        l.stops.add(stop("Gateway Boulevard & Whitemud", 53.481000, -113.500000));
        l.stops.add(stop("West Edmonton Mall Transit Centre", 53.525139, -113.622021));
        return l;
    }

    private static ApiModels.Itinerary trip(ApiModels.TransitLeg... legs) {
        ApiModels.Itinerary it = new ApiModels.Itinerary();
        it.legs = new ArrayList<>();
        for (ApiModels.TransitLeg l : legs) it.legs.add(l);
        return it;
    }

    private static ApiModels.Itinerary oneBus() {
        return trip(walk(), bus523());
    }

    // ── the countdown ──────────────────────────────────────────────────────

    @Test public void atTheBoardingStop_countsEveryStopAhead() {
        ApiModels.Itinerary it = oneBus();
        TransitProgress.State st = TransitProgress.evaluate(it, 53.465572, -113.428895, 0);
        assertEquals(4, st.stopsRemaining);
        assertEquals("99 Street & 44 Avenue", st.alightStop.name);
        assertEquals("66 Street & 34 Avenue", st.nextStop.name);
    }

    @Test public void partWayAlong_theCountdownDecreases() {
        TransitProgress.State st =
                TransitProgress.evaluate(oneBus(), 53.465700, -113.455700, 0);
        assertEquals(2, st.stopsRemaining);
        assertTrue(st.onBoard);
    }

    @Test public void oneStopOut_raisesTheAlertThatMatters() {
        TransitProgress.State st =
                TransitProgress.evaluate(oneBus(), 53.478267, -113.485962, 0);
        assertEquals(1, st.stopsRemaining);
        assertNotNull("one stop out must interrupt the rider", st.alert);
        assertTrue(st.alert.contains("next stop"));
        assertTrue(st.alert.contains("99 Street & 44 Avenue"));
    }

    @Test public void headlineSaysStopNotStops_whenExactlyOneRemains() {
        TransitProgress.State st =
                TransitProgress.evaluate(oneBus(), 53.478267, -113.485962, 0);
        assertTrue("expected singular in: " + st.headline, st.headline.endsWith("1 stop to go"));
    }

    @Test public void twoStopsOut_staysQuiet() {
        TransitProgress.State st =
                TransitProgress.evaluate(oneBus(), 53.465700, -113.455700, 0);
        assertNull("a companion that talks every stop gets muted", st.alert);
    }

    // ── arriving and transferring ──────────────────────────────────────────

    @Test public void atTheAlightStopOfTheLastRide_isArrival() {
        TransitProgress.State st =
                TransitProgress.evaluate(oneBus(), 53.482114, -113.485953, 0);
        assertEquals(0, st.stopsRemaining);
        assertTrue(st.arrived);
        assertTrue(st.headline.startsWith("Get off here"));
    }

    @Test public void atATransfer_itIsNotArrivalAndTheConnectionIsNamed() {
        ApiModels.Itinerary it = trip(walk(), bus523(), walk(), bus055());
        TransitProgress.State st =
                TransitProgress.evaluate(it, 53.482114, -113.485953, 0);
        assertFalse("a transfer is not the end of the trip", st.arrived);
        // Naming the connection is the whole point: "the following leg" sends the
        // rider back into the app at the moment they are standing on a kerb.
        assertTrue("expected the next route named, got: " + st.detail,
                st.detail.contains("055"));
        assertTrue(st.alert.contains("055"));
    }

    @Test public void afterTheTransfer_itTracksTheSecondLeg() {
        ApiModels.Itinerary it = trip(walk(), bus523(), walk(), bus055());
        TransitProgress.State st =
                TransitProgress.evaluate(it, 53.480843, -113.487243, 3);
        assertEquals(3, st.legIndex);
        assertEquals("West Edmonton Mall Transit Centre", st.alightStop.name);
        assertEquals(2, st.stopsRemaining);
    }

    // ── the failure modes that matter on a real phone ──────────────────────

    @Test public void furthestLegStopsItSnappingBackwards() {
        // A trip whose second leg passes near the first leg's stops. Standing at
        // the 523's boarding stop but already known to be on leg 3 must not drag
        // the rider back to leg 1 -- that would restart the countdown mid-ride.
        ApiModels.Itinerary it = trip(walk(), bus523(), walk(), bus055());
        TransitProgress.State st =
                TransitProgress.evaluate(it, 53.465572, -113.428895, 3);
        assertEquals("must not fall back to an earlier leg", 3, st.legIndex);
    }

    @Test public void anEmptyOrNullItineraryDoesNotThrow() {
        assertEquals(-1, TransitProgress.evaluate(null, 53.5, -113.5, 0).legIndex);
        ApiModels.Itinerary empty = new ApiModels.Itinerary();
        assertEquals(-1, TransitProgress.evaluate(empty, 53.5, -113.5, 0).legIndex);
    }

    @Test public void aWalkOnlyPlanFallsBackInsteadOfCrashing() {
        // No ride legs at all, so no stop list to match against. This is exactly
        // what OTP returns when walking beats waiting.
        ApiModels.Itinerary it = trip(walk());
        TransitProgress.State st = TransitProgress.evaluate(it, 53.5, -113.5, 0);
        assertNull(st.leg);
        assertFalse(st.headline.isEmpty());
    }

    @Test public void aRideLegWithNoStopListIsSkipped() {
        // Defensive: an older backend, or a feed with no stopCalls, must degrade
        // rather than throw on the rider's phone mid-journey.
        ApiModels.TransitLeg bare = new ApiModels.TransitLeg();
        bare.mode = "BUS"; bare.route = "999";
        TransitProgress.State st =
                TransitProgress.evaluate(trip(walk(), bare), 53.5, -113.5, 0);
        assertNull(st.leg);
    }

    // ── am I on the right bus? ─────────────────────────────────────────────

    private static ApiModels.Vehicle vehicle(double lat, double lon) {
        ApiModels.Vehicle v = new ApiModels.Vehicle();
        v.lat = lat; v.lon = lon; v.vehicleId = "1:2503"; v.label = "4752";
        return v;
    }

    @Test public void nearAReportedVehicle_isNotAWrongBus() {
        List<ApiModels.Vehicle> vs = new ArrayList<>();
        vs.add(vehicle(53.478300, -113.486000));      // ~10 m away
        assertFalse(TransitProgress.looksLikeWrongVehicle(vs, 53.478267, -113.485962));
    }

    @Test public void farFromEveryVehicle_looksLikeAWrongBus() {
        List<ApiModels.Vehicle> vs = new ArrayList<>();
        vs.add(vehicle(53.544000, -113.492000));      // several km north
        assertTrue(TransitProgress.looksLikeWrongVehicle(vs, 53.465572, -113.428895));
    }

    @Test public void noVehicleData_neverAccusesTheRider() {
        // The single most important case. ETS reports ~98% of vehicles, not 100%,
        // and the feed can drop entirely. Absent evidence is not evidence -- a
        // companion that cries wolf gets ignored at the moment it matters.
        assertFalse(TransitProgress.looksLikeWrongVehicle(null, 53.5, -113.5));
        assertFalse(TransitProgress.looksLikeWrongVehicle(
                new ArrayList<ApiModels.Vehicle>(), 53.5, -113.5));
        assertEquals(-1, TransitProgress.metresToNearestVehicle(null, 53.5, -113.5), 0.001);
    }

    @Test public void theNearestVehicleIsTheOneMeasured() {
        List<ApiModels.Vehicle> vs = new ArrayList<>();
        vs.add(vehicle(53.544000, -113.492000));      // far
        vs.add(vehicle(53.478300, -113.486000));      // near
        double d = TransitProgress.metresToNearestVehicle(vs, 53.478267, -113.485962);
        assertTrue("expected the near vehicle, got " + d + " m", d < 100);
    }

    // ── the geometry underneath it all ─────────────────────────────────────

    @Test public void metresMatchesAKnownDistance() {
        // 99 Street & 42 Ave to 99 Street & 44 Ave: two blocks, roughly 430 m.
        double d = TransitProgress.metres(53.478267, -113.485962, 53.482114, -113.485953);
        assertTrue("expected ~430 m, got " + d, d > 380 && d < 480);
    }

    @Test public void metresIsZeroForTheSamePoint() {
        assertEquals(0.0, TransitProgress.metres(53.5, -113.5, 53.5, -113.5), 0.001);
    }
}
