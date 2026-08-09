package com.example.rideiq;

import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.os.Bundle;
import android.text.TextUtils;
import android.util.Base64;
import android.view.View;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.ImageView;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;

import java.util.Locale;
import java.util.Map;

import retrofit2.Call;
import retrofit2.Callback;
import retrofit2.Response;

public class MainActivity extends AppCompatActivity {

    private EditText distance, hour, traffic, surge, rating;
    private CheckBox raining;
    private TextView quoteText, analyticsText;
    private ImageView plotView;
    private ProgressBar progress;

    @Override
    protected void onCreate(Bundle s) {
        super.onCreate(s);
        setContentView(R.layout.activity_main);

        distance = findViewById(R.id.distance);
        hour = findViewById(R.id.hour);
        traffic = findViewById(R.id.traffic);
        surge = findViewById(R.id.surge);
        rating = findViewById(R.id.rating);
        raining = findViewById(R.id.raining);
        quoteText = findViewById(R.id.quoteText);
        analyticsText = findViewById(R.id.analyticsText);
        plotView = findViewById(R.id.plotView);
        progress = findViewById(R.id.progress);

        ((Button) findViewById(R.id.quoteBtn)).setOnClickListener(v -> getQuote());
        ((Button) findViewById(R.id.mapBtn)).setOnClickListener(v ->
                startActivity(new android.content.Intent(this, RouteMapActivity.class)));
        ((Button) findViewById(R.id.surgeBtn)).setOnClickListener(v ->
                analytics(ApiClient.get().surgeZones(), "Surge zones"));
        ((Button) findViewById(R.id.segBtn)).setOnClickListener(v ->
                analytics(ApiClient.get().riderSegments(), "Rider segments"));
        ((Button) findViewById(R.id.driverBtn)).setOnClickListener(v ->
                analytics(ApiClient.get().driverShift(), "Driver shift"));
        ((Button) findViewById(R.id.causesBtn)).setOnClickListener(v ->
                analytics(ApiClient.get().cancellationCauses(), "Cancellation causes"));
    }

    private double d(EditText e, double def) {
        try { return Double.parseDouble(e.getText().toString().trim()); }
        catch (Exception ex) { return def; }
    }

    private void getQuote() {
        busy(true);
        quoteText.setText("Getting quote…");
        ApiModels.QuoteRequest req = new ApiModels.QuoteRequest(
                d(distance, 8), (int) d(hour, 8), raining.isChecked() ? 1 : 0,
                d(traffic, 0.5), d(surge, 1.0), d(rating, 4.6));
        ApiClient.get().quote(req).enqueue(new Callback<ApiModels.QuoteResponse>() {
            @Override public void onResponse(@NonNull Call<ApiModels.QuoteResponse> c,
                                             @NonNull Response<ApiModels.QuoteResponse> r) {
                busy(false);
                if (!r.isSuccessful() || r.body() == null) { quoteText.setText("Server error " + r.code()); return; }
                ApiModels.QuoteResponse q = r.body();
                quoteText.setText(String.format(Locale.US,
                        "ETA: %.0f min\nFare: $%.2f\nCancellation risk: %.0f%%   (served by %s)",
                        q.etaMin, q.fareUsd, q.cancellationRisk * 100, q.instance));
            }
            @Override public void onFailure(@NonNull Call<ApiModels.QuoteResponse> c, @NonNull Throwable t) {
                busy(false);
                quoteText.setText("Can't reach server. Is the backend running and BASE_URL correct?");
            }
        });
    }

    private void analytics(Call<Map<String, Object>> call, String label) {
        busy(true);
        analyticsText.setText(label + "…");
        call.enqueue(new Callback<Map<String, Object>>() {
            @Override public void onResponse(@NonNull Call<Map<String, Object>> c,
                                             @NonNull Response<Map<String, Object>> r) {
                busy(false);
                if (!r.isSuccessful() || r.body() == null) { analyticsText.setText("Server error " + r.code()); return; }
                Map<String, Object> body = r.body();
                Object plot = body.remove("plot_png_base64");
                StringBuilder sb = new StringBuilder(label + "\n");
                for (Map.Entry<String, Object> e : body.entrySet()) {
                    if ("instance".equals(e.getKey()) || "cached".equals(e.getKey())) continue;
                    sb.append("• ").append(e.getKey()).append(": ").append(compact(e.getValue())).append("\n");
                }
                analyticsText.setText(sb.toString().trim());
                if (plot instanceof String) showPlot((String) plot);
            }
            @Override public void onFailure(@NonNull Call<Map<String, Object>> c, @NonNull Throwable t) {
                busy(false);
                analyticsText.setText("Can't reach server.");
            }
        });
    }

    private String compact(Object v) {
        String s = String.valueOf(v);
        return s.length() > 120 ? s.substring(0, 117) + "…" : s;
    }

    private void showPlot(String b64) {
        if (TextUtils.isEmpty(b64)) return;
        byte[] bytes = Base64.decode(b64, Base64.DEFAULT);
        Bitmap bmp = BitmapFactory.decodeByteArray(bytes, 0, bytes.length);
        plotView.setImageBitmap(bmp);
    }

    private void busy(boolean b) { progress.setVisibility(b ? View.VISIBLE : View.GONE); }
}
