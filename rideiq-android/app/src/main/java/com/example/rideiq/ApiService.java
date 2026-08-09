package com.example.rideiq;

import java.util.Map;

import retrofit2.Call;
import retrofit2.http.Body;
import retrofit2.http.GET;
import retrofit2.http.POST;

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
}
