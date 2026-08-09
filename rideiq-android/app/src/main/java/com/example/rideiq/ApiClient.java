package com.example.rideiq;

import java.util.concurrent.TimeUnit;

import okhttp3.OkHttpClient;
import okhttp3.logging.HttpLoggingInterceptor;
import retrofit2.Retrofit;
import retrofit2.converter.gson.GsonConverterFactory;

public class ApiClient {

    // Production: EC2 nginx edge on port 80 (public DNS). Works on a real phone over the internet.
    // Local dev fallbacks:
    //   emulator + local stack  -> "http://10.0.2.2:8080/"
    //   physical phone on LAN    -> "http://<your-PC-LAN-IP>:8080/"
    public static final String BASE_URL = "http://ec2-3-99-66-9.ca-central-1.compute.amazonaws.com/";

    private static ApiService service;

    public static ApiService get() {
        if (service == null) {
            HttpLoggingInterceptor log = new HttpLoggingInterceptor();
            log.setLevel(HttpLoggingInterceptor.Level.BASIC);
            OkHttpClient http = new OkHttpClient.Builder()
                    .addInterceptor(log)
                    .connectTimeout(10, TimeUnit.SECONDS)
                    .readTimeout(40, TimeUnit.SECONDS)
                    .build();
            service = new Retrofit.Builder()
                    .baseUrl(BASE_URL)
                    .client(http)
                    .addConverterFactory(GsonConverterFactory.create())
                    .build()
                    .create(ApiService.class);
        }
        return service;
    }
}
