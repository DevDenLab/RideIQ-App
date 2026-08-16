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
