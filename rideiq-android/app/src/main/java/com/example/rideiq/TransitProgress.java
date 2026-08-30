package com.example.rideiq;

import java.util.List;
import java.util.Locale;

/**
 * Works out where you are inside a transit itinerary, from nothing but a GPS fix.
 *
 * Deliberately plain Java with no Android imports: the "which stop am I at, and
 * should I be standing up yet" decision is the part worth reasoning about on its
 * own, so it lives apart from the service that renders it.
 *
 * The approach is nearest-stop matching rather than dead reckoning along the
 * polyline. A bus stop sequence is a naturally ordered, well-spaced list, and
 * snapping to the nearest one is robust to the things that actually go wrong on a
 * phone: a GPS fix drifting fifty metres, a tunnel, a route that doubles back on
 * itself. Distance along a line is more precise in theory and much easier to get
 * badly wrong in practice.
 */
public final class TransitProgress {

    /** How close counts as "you are at this stop" rather than approaching it. */
    private static final double AT_STOP_M = 60;

    /** Where the rider is now, and what they should be told. */
    public static class State {
        public int legIndex = -1;
        public ApiModels.TransitLeg leg;
        /** Index into leg.stops of the stop just passed or currently at. */
        public int stopIndex = -1;
        public ApiModels.TransitStop nextStop;
        public ApiModels.TransitStop alightStop;
        /** Stops still to go before you get off. 0 means get off now. */
        public int stopsRemaining = -1;
        public boolean onBoard;
        public boolean arrived;

        /** One line for the notification title. */
        public String headline = "";
        /** One line for the notification body. */
        public String detail = "";
        /**
         * Set when this state deserves interrupting the rider - spoken aloud and
         * given a heads-up notification. Null the rest of the time, which is most
         * of the time: a companion that talks every 30 seconds gets muted.
         */
        public String alert;
        /** Stable key for the alert, so the same one is not repeated. */
        public String alertKey;
    }

    private TransitProgress() { }

    public static double metres(double lat1, double lon1, double lat2, double lon2) {
        double dLat = Math.toRadians(lat2 - lat1);
        double dLon = Math.toRadians(lon2 - lon1);
        double a = Math.sin(dLat / 2) * Math.sin(dLat / 2)
                + Math.cos(Math.toRadians(lat1)) * Math.cos(Math.toRadians(lat2))
                * Math.sin(dLon / 2) * Math.sin(dLon / 2);
        return 6371000.0 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
    }

    /**
     * @param itinerary the plan being ridden
     * @param lat,lon   the current fix
     * @param furthestLeg highest leg index reached so far; prevents snapping
     *                    backwards onto an earlier leg that passes nearby
     */
    public static State evaluate(ApiModels.Itinerary itinerary, double lat, double lon,
                                 int furthestLeg) {
        State st = new State();
        if (itinerary == null || itinerary.legs == null || itinerary.legs.isEmpty()) {
            return st;
        }

        // Find the nearest stop across every ride leg at or after the furthest leg
        // we have already reached. A trip that returns along the same corridor
        // would otherwise jump back to the outbound leg.
        double best = Double.MAX_VALUE;
        for (int li = Math.max(0, furthestLeg); li < itinerary.legs.size(); li++) {
            ApiModels.TransitLeg leg = itinerary.legs.get(li);
            if (leg.stops == null || leg.stops.isEmpty()) continue;
            for (int si = 0; si < leg.stops.size(); si++) {
                ApiModels.TransitStop s = leg.stops.get(si);
                double d = metres(lat, lon, s.lat, s.lon);
                if (d < best) {
                    best = d;
                    st.legIndex = li;
                    st.leg = leg;
                    st.stopIndex = si;
                }
            }
        }

        if (st.leg == null) {
            // A walk-only plan, or one whose ride legs carried no stop list.
            st.headline = "On your way";
            st.detail = "Follow the route to your destination";
            return st;
        }

        List<ApiModels.TransitStop> stops = st.leg.stops;
        st.alightStop = stops.get(stops.size() - 1);
        st.stopsRemaining = (stops.size() - 1) - st.stopIndex;
        st.nextStop = st.stopIndex + 1 < stops.size() ? stops.get(st.stopIndex + 1) : null;
        boolean atStop = best <= AT_STOP_M;
        st.onBoard = st.stopIndex > 0 && st.stopsRemaining > 0;

        String ride = ((st.leg.modeLabel != null ? st.leg.modeLabel : st.leg.mode)
                + " " + (st.leg.route == null ? "" : st.leg.route)).trim();

        // Arrived at the last stop of the last ride leg.
        boolean lastRide = true;
        for (int li = st.legIndex + 1; li < itinerary.legs.size(); li++) {
            if (itinerary.legs.get(li).isRide()) { lastRide = false; break; }
        }

        if (st.stopsRemaining <= 0) {
            st.arrived = lastRide;
            st.headline = "Get off here - " + st.alightStop.name;
            st.detail = lastRide
                    ? "Then walk to your destination"
                    : "Transfer next: continue to the following leg";
            st.alert = "Get off now at " + st.alightStop.name;
            st.alertKey = "off:" + st.legIndex;
            return st;
        }

        if (st.stopIndex == 0 && !atStop) {
            // Heading for the boarding stop but not there yet.
            st.headline = "Walk to " + st.leg.stops.get(0).name;
            st.detail = String.format(Locale.US, "Board %s at %s%s",
                    ride, st.leg.depart == null ? "the stop" : st.leg.depart,
                    st.leg.headsign == null || st.leg.headsign.isEmpty()
                            ? "" : " toward " + st.leg.headsign);
            return st;
        }

        st.headline = String.format(Locale.US, "%s - %d stop%s to go",
                ride, st.stopsRemaining, st.stopsRemaining == 1 ? "" : "s");
        st.detail = (st.nextStop != null ? "Next: " + st.nextStop.name + "  -  " : "")
                + "Get off at " + st.alightStop.name;

        // The one moment that actually matters: you need to be standing up.
        if (st.stopsRemaining == 1) {
            st.alert = "Get off at the next stop - " + st.alightStop.name;
            st.alertKey = "next:" + st.legIndex;
        } else if (st.stopIndex == 0 && atStop) {
            st.alert = "Board " + ride
                    + (st.leg.headsign == null || st.leg.headsign.isEmpty()
                       ? "" : " toward " + st.leg.headsign);
            st.alertKey = "board:" + st.legIndex;
        }
        return st;
    }
}
