package com.example.rideiq;

import java.util.Map;

import retrofit2.Call;
import retrofit2.http.Body;
import retrofit2.http.GET;
import retrofit2.http.POST;
import retrofit2.http.Query;

public interface ApiService {

    @POST("quote")
    Call<ApiModels.QuoteResponse> quote(@Body ApiModels.QuoteRequest body);

    // Analytics endpoints all return { ...summary fields..., "plot_png_base64": "..." }.
    // Read them generically so one handler covers all four.
    @GET("surge-zones")
    Call<Map<String, Object>> surgeZones();

    @GET("rider-segments")
    Call<Map<String, Object>> riderSegments();

    @GET("driver-shift")
    Call<Map<String, Object>> driverShift();

    @GET("cancellation-causes")
    Call<Map<String, Object>> cancellationCauses();

    @GET("graph")
    Call<ApiModels.GraphResponse> graph();

    @POST("route")
    Call<ApiModels.RouteResponse> route(@Body ApiModels.RouteRequest body);

    @GET("landmarks")
    Call<ApiModels.LandmarksResponse> landmarks();

    @POST("route-latlon")
    Call<ApiModels.RouteResponse> routeLatLon(@Body ApiModels.RouteLatLonRequest body);

    // Coordinates -> street address (for the pickup / drop-off labels).
    @GET("reverse-geocode")
    Call<ApiModels.ReverseGeocodeResponse> reverseGeocode(@Query("lat") double lat,
                                                          @Query("lon") double lon);

    // Address / place text -> coordinates (for typing a start / destination).
    @GET("geocode")
    Call<ApiModels.ReverseGeocodeResponse> geocode(@Query("q") String q);

    // Autocomplete: up to 5 matching places for a partial query.
    @GET("search")
    Call<ApiModels.SearchResponse> search(@Query("q") String q);

    // Public transit. Unlike routeLatLon this is time-dependent -- the answer
    // depends on when you ask, because you have to catch the bus. A 503 here means
    // the transit engine is down, not that the trip is impossible.
    @GET("transit")
    Call<ApiModels.TransitResponse> transit(@Query("lat1") double lat1,
                                            @Query("lon1") double lon1,
                                            @Query("lat2") double lat2,
                                            @Query("lon2") double lon2,
                                            @Query("max_walk_m") int maxWalkM,
                                            @Query("want") int want);
}
