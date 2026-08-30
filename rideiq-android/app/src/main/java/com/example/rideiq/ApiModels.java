package com.example.rideiq;

import com.google.gson.annotations.SerializedName;

/** DTOs for the RideIQ API. Analytics endpoints are read as a generic Map elsewhere. */
public class ApiModels {

    public static class QuoteRequest {
        public double distance;
        public int hour;
        public int weather;        // 0 clear, 1 rain
        public double traffic;     // 0..1
        public double surge;       // >= 1.0
        public double rider_rating;
        public QuoteRequest(double distance, int hour, int weather,
                            double traffic, double surge, double rider_rating) {
            this.distance = distance; this.hour = hour; this.weather = weather;
            this.traffic = traffic; this.surge = surge; this.rider_rating = rider_rating;
        }
    }

    public static class QuoteResponse {
        @SerializedName("eta_min") public double etaMin;
        @SerializedName("fare_usd") public double fareUsd;
        @SerializedName("cancellation_risk") public double cancellationRisk;
        public String instance;
    }

    // ---- map / routing ----
    public static class Node {
        public int id;
        public double x;
        public double y;
    }
    public static class GraphResponse {
        public java.util.List<Node> nodes;
        public java.util.List<java.util.List<Integer>> edges;  // [[a,b], ...]
    }

    public static class RouteRequest {
        public double ax, ay, bx, by;
        public int hour, weather;
        public double traffic, surge;
        public RouteRequest(double ax, double ay, double bx, double by,
                            int hour, int weather, double traffic, double surge) {
            this.ax = ax; this.ay = ay; this.bx = bx; this.by = by;
            this.hour = hour; this.weather = weather; this.traffic = traffic; this.surge = surge;
        }
    }
    public static class RouteResponse {
        public java.util.List<java.util.List<Double>> polyline;  // [[x,y], ...] normalized
        @SerializedName("polyline_latlon")
        public java.util.List<java.util.List<Double>> polylineLatlon;  // [[lat,lon], ...]
        @SerializedName("distance_km") public double distanceKm;
        @SerializedName("eta_min") public double etaMin;
        @SerializedName("fare_usd") public double fareUsd;
        public int turns;
        public java.util.List<Step> steps;   // turn-by-turn maneuvers for voice navigation
        public String mode;                  // "drive" or "walk" (which graph served this)
        @SerializedName("mode_fallback") public boolean modeFallback;  // true if walk fell back to drive
        public java.util.List<RouteResponse> alternatives;   // slower alternative routes
    }

    /** One turn-by-turn maneuver: where to act, what to do, and distance to it. */
    public static class Step {
        public double lat;
        public double lon;
        public String maneuver;      // left / right / slight_right / sharp_left / uturn / arrive
        public String instruction;   // e.g. "Turn left", "You have arrived…"
        @SerializedName("dist_from_prev_m") public double distFromPrevM;
    }

    // ---- landmarks ----
    public static class Landmark {
        public String name;
        public double lat;
        public double lon;
    }
    public static class LandmarksResponse {
        public java.util.List<Landmark> landmarks;
        @SerializedName("real_city") public boolean realCity;
        public String city;
    }
    // ---- reverse geocoding (coords -> address) ----
    public static class ReverseGeocodeResponse {
        @SerializedName("display_name") public String displayName;
        @SerializedName("short") public String shortLabel;   // 'short' is a Java keyword
        public double lat;
        public double lon;
    }

    // ---- autocomplete search (text -> list of matches) ----
    public static class SearchResponse {
        public java.util.List<ReverseGeocodeResponse> results;
    }

    // ---- public transit (GET /transit -> OpenTripPlanner behind the backend) ----

    /** One stage of a transit trip: a walk to a stop, or a ride on one route. */
    public static class TransitLeg {
        public String mode;              // WALK, BUS, TRAM (= LRT here), RAIL, ...
        @SerializedName("mode_label") public String modeLabel;   // rider-facing: "Bus", "LRT"
        @SerializedName("from") public String fromName;
        @SerializedName("to") public String toName;
        public String route;             // "8", "Capital" -- null on a walk leg
        public String headsign;          // "toward Abbottsfield"
        public String color;             // "#..." when the agency publishes one
        public String depart;            // "17:48" local -- already delay-adjusted
        public String arrive;            // "18:05" local
        public boolean realtime;         // a live feed backed this leg's times
        public String status;            // "on time" / "3 min late" -- null if scheduled only
        @SerializedName("distance_m") public double distanceM;
        @SerializedName("duration_min") public int durationMin;
        @SerializedName("polyline_latlon")
        public java.util.List<java.util.List<Double>> polylineLatlon;
        @SerializedName("pattern_code") public String patternCode;  // live-vehicle key
        @SerializedName("trip_id") public String tripId;
        /** Every stop this vehicle calls at: boarding first, alighting last. */
        public java.util.List<TransitStop> stops;
        @SerializedName("stop_count") public int stopCount;

        /** True when you are riding something rather than walking to it. */
        public boolean isRide() { return mode != null && !"WALK".equals(mode); }
    }

    /** One stop a vehicle calls at along a ride leg. */
    public static class TransitStop {
        public String name;
        public String code;      // the number on the pole, e.g. "3850"
        public double lat;
        public double lon;
    }

    /**
     * What the trip costs. ETS publishes GTFS Fares V2, so this already accounts
     * for transfer rules -- three buses inside the transfer window is one fare.
     */
    public static class TransitFare {
        public double amount;
        public String currency;      // "CAD"
        public String medium;        // "Arc", "Cash", "cEMV" -- the cheapest one
        public String text;          // "CAD 3.00 (Arc)", ready to display
    }

    /** A live service alert riding along with an itinerary. */
    public static class TransitAlert {
        public String header;
        public String description;
        public String severity;
        public String effect;
    }

    /** One complete door-to-door option: walk, ride, maybe transfer, walk. */
    public static class Itinerary {
        @SerializedName("duration_min") public int durationMin;
        @SerializedName("depart_time") public String departTime;   // "17:42"
        @SerializedName("arrive_time") public String arriveTime;   // "18:16"
        public int transfers;
        @SerializedName("walk_distance_m") public double walkDistanceM;
        public java.util.List<String> routes;         // ["8", "Capital"] for the card
        @SerializedName("walk_only") public boolean walkOnly;  // no vehicle at all
        public boolean realtime;                      // any ride leg had live data
        public String status;                         // first live punctuality, if any
        public java.util.List<TransitAlert> alerts;   // de-duplicated across legs
        public TransitFare fare;                      // null if the feed prices nothing
        public java.util.List<TransitLeg> legs;
        public java.util.List<String> instructions;   // ready-made lines for the list
    }

    /** A vehicle reported live on a pattern. */
    public static class Vehicle {
        @SerializedName("vehicle_id") public String vehicleId;
        public String label;          // the fleet number painted on the bus
        public double lat;
        public double lon;
    }

    public static class VehiclesResponse {
        public String pattern;
        public String route;
        public java.util.List<Vehicle> vehicles;
    }

    public static class TransitResponse {
        public java.util.List<Itinerary> itineraries;
    }

    public static class RouteLatLonRequest {
        public double lat1, lon1, lat2, lon2;
        public int hour, weather;
        public double traffic, surge;
        public String mode = "drive";        // "drive" or "walk"
        public RouteLatLonRequest(double lat1, double lon1, double lat2, double lon2,
                                  int hour, int weather, double traffic, double surge) {
            this.lat1 = lat1; this.lon1 = lon1; this.lat2 = lat2; this.lon2 = lon2;
            this.hour = hour; this.weather = weather; this.traffic = traffic; this.surge = surge;
        }
    }
}
