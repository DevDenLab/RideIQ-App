package com.example.rideiq;

import androidx.lifecycle.ViewModel;

import java.util.ArrayList;
import java.util.List;

/**
 * Holds the plan across a configuration change.
 *
 * RouteMapActivity kept everything it had computed in its own fields, so rotating
 * the phone destroyed the activity and took the route with it: pins, itineraries,
 * selected option, travel mode, all gone, and the user starts again. On a screen
 * whose whole job is "I looked something up", that is the worst possible thing to
 * lose.
 *
 * This is deliberately a data holder and not a rewrite. The audit calls for moving
 * the activity's logic into a ViewModel, and that is a real refactor of a
 * 1,400-line class with no UI tests behind it. Fixing the user-visible symptom is
 * separable from that, far lower risk, and worth having now -- so this stores
 * state and nothing else. The behaviour stays exactly where it is.
 */
public class RouteState extends ViewModel {

    /** True once a plan has been stored, so a fresh activity knows to restore. */
    public boolean hasPlan = false;

    public String travelMode = "drive";

    // Drive / walk
    public List<ApiModels.RouteResponse> routeOptions = new ArrayList<>();
    public int selectedOption = 0;
    public String lastRouteLabel = "Route";

    // Transit
    public List<ApiModels.Itinerary> transitOptions = new ArrayList<>();
    public int selectedItinerary = 0;
    public String transitWhen = "now";
    public boolean transitArriveBy = false;
    public boolean wheelchair = false;

    // Where the pins are, so they can be dropped back on the map.
    public double pickupLat, pickupLon, dropoffLat, dropoffLon;
    public boolean hasPickup = false, hasDropoff = false;

    public void clear() {
        hasPlan = false;
        routeOptions = new ArrayList<>();
        transitOptions = new ArrayList<>();
        selectedOption = 0;
        selectedItinerary = 0;
        hasPickup = hasDropoff = false;
    }
}
