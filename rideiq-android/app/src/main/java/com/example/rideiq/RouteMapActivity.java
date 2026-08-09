package com.example.rideiq;

import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.drawable.BitmapDrawable;
import android.graphics.drawable.Drawable;
import android.os.Bundle;
import android.preference.PreferenceManager;
import android.view.View;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.ProgressBar;
import android.widget.Spinner;
import android.widget.TextView;

import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;

import org.osmdroid.config.Configuration;
import org.osmdroid.events.MapEventsReceiver;
import org.osmdroid.tileprovider.tilesource.OnlineTileSourceBase;
import org.osmdroid.tileprovider.tilesource.TileSourceFactory;
import org.osmdroid.tileprovider.tilesource.XYTileSource;
import org.osmdroid.util.BoundingBox;
import org.osmdroid.util.GeoPoint;
import org.osmdroid.util.MapTileIndex;
import org.osmdroid.views.MapView;
import org.osmdroid.views.overlay.MapEventsOverlay;
import org.osmdroid.views.overlay.Marker;
import org.osmdroid.views.overlay.Polyline;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

import retrofit2.Call;
import retrofit2.Callback;
import retrofit2.Response;

/**
 * Interactive Uber-style routing on osmdroid (free OpenStreetMap tiles, no API key).
 *
 *  • Tap once to drop a GREEN pickup pin, tap again for a RED drop-off pin.
 *  • Drag either pin to fine-tune — the route, distance, ETA and fare update automatically.
 *  • Or choose two landmarks. Pinch to zoom / drag to pan. Toggle street/satellite tiles.
 */
public class RouteMapActivity extends AppCompatActivity {

    private static final int GREEN = Color.parseColor("#2E7D32");
    private static final int RED = Color.parseColor("#C62828");

    // Clean, light street basemap (CARTO Positron) — routes stand out clearly.
    private static final XYTileSource STREET = new XYTileSource(
            "CartoPositron", 0, 20, 256, ".png",
            new String[]{"https://a.basemaps.cartocdn.com/light_all/",
                         "https://b.basemaps.cartocdn.com/light_all/",
                         "https://c.basemaps.cartocdn.com/light_all/"},
            "© OpenStreetMap contributors, © CARTO");

    private static final OnlineTileSourceBase ESRI_SAT = new OnlineTileSourceBase(
            "EsriWorldImagery", 0, 19, 256, "",
            new String[]{"https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/"},
            "Esri, Maxar, Earthstar Geographics") {
        @Override public String getTileURLString(long idx) {
            return getBaseUrl() + MapTileIndex.getZoom(idx) + "/"
                    + MapTileIndex.getY(idx) + "/" + MapTileIndex.getX(idx);
        }
    };

    private MapView map;
    private TextView info;
    private ProgressBar progress;
    private Spinner fromSpinner, toSpinner;
    private final List<ApiModels.Landmark> landmarks = new ArrayList<>();

    private Marker pickupMarker, dropoffMarker;
    private Polyline routeLine;
    private boolean satellite = false;

    @Override
    protected void onCreate(Bundle s) {
        super.onCreate(s);
        Configuration.getInstance().load(this, PreferenceManager.getDefaultSharedPreferences(this));
        Configuration.getInstance().setUserAgentValue(getPackageName());

        setContentView(R.layout.activity_route_map);
        setTitle("Route map");

        info = findViewById(R.id.info);
        progress = findViewById(R.id.progress);
        fromSpinner = findViewById(R.id.fromSpinner);
        toSpinner = findViewById(R.id.toSpinner);

        map = findViewById(R.id.map);
        map.setTileSource(STREET);                      // clean light street map by default
        map.setMultiTouchControls(true);
        map.getController().setZoom(12.0);
        map.getController().setCenter(new GeoPoint(53.5461, -113.4938));  // Edmonton

        map.getOverlays().add(new MapEventsOverlay(new MapEventsReceiver() {
            @Override public boolean singleTapConfirmedHelper(GeoPoint p) { onMapTap(p); return true; }
            @Override public boolean longPressHelper(GeoPoint p) { return false; }
        }));

        ((Button) findViewById(R.id.resetBtn)).setOnClickListener(v -> resetMap());
        ((Button) findViewById(R.id.routeLmBtn)).setOnClickListener(v -> routeLandmarks());
        ((Button) findViewById(R.id.satBtn)).setOnClickListener(v -> toggleSatellite());

        info.setText("Tap the map to set your PICKUP (green).");
        loadLandmarks();
    }

    @Override protected void onResume() { super.onResume(); map.onResume(); }
    @Override protected void onPause() { super.onPause(); map.onPause(); }

    private void toggleSatellite() {
        satellite = !satellite;
        map.setTileSource(satellite ? ESRI_SAT : STREET);
        map.invalidate();
    }

    // ───────────────────────── selection ─────────────────────────
    private void onMapTap(GeoPoint p) {
        if (pickupMarker == null) {                       // 1) set pickup
            pickupMarker = makeMarker(p, "Pickup", GREEN);
            map.getOverlays().add(pickupMarker);
            info.setText("Pickup set ✓  — now tap your DESTINATION (red).");
        } else if (dropoffMarker == null) {               // 2) set drop-off → route
            dropoffMarker = makeMarker(p, "Drop-off", RED);
            map.getOverlays().add(dropoffMarker);
            routeFromMarkers("Route");
        } else {                                          // 3) start over
            clearAll();
            pickupMarker = makeMarker(p, "Pickup", GREEN);
            map.getOverlays().add(pickupMarker);
            info.setText("Pickup set ✓  — now tap your DESTINATION (red).");
        }
        map.invalidate();
    }

    /** A draggable, colored dot marker. Dragging re-routes automatically. */
    private Marker makeMarker(GeoPoint p, String title, int color) {
        Marker m = new Marker(map);
        m.setPosition(p);
        m.setTitle(title);
        m.setIcon(dotIcon(color));
        m.setAnchor(Marker.ANCHOR_CENTER, Marker.ANCHOR_CENTER);
        m.setDraggable(true);
        m.setOnMarkerDragListener(new Marker.OnMarkerDragListener() {
            @Override public void onMarkerDragStart(Marker mk) { }
            @Override public void onMarkerDrag(Marker mk) { }
            @Override public void onMarkerDragEnd(Marker mk) {
                if (pickupMarker != null && dropoffMarker != null) routeFromMarkers("Updated route");
            }
        });
        return m;
    }

    private Drawable dotIcon(int color) {
        int size = (int) (26 * getResources().getDisplayMetrics().density);
        Bitmap bmp = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888);
        Canvas cv = new Canvas(bmp);
        Paint fill = new Paint(Paint.ANTI_ALIAS_FLAG);
        fill.setColor(color);
        cv.drawCircle(size / 2f, size / 2f, size / 2.6f, fill);
        Paint ring = new Paint(Paint.ANTI_ALIAS_FLAG);
        ring.setColor(Color.WHITE);
        ring.setStyle(Paint.Style.STROKE);
        ring.setStrokeWidth(size * 0.14f);
        cv.drawCircle(size / 2f, size / 2f, size / 2.6f, ring);
        return new BitmapDrawable(getResources(), bmp);
    }

    // ───────────────────────── landmarks ─────────────────────────
    private void loadLandmarks() {
        ApiClient.get().landmarks().enqueue(new Callback<ApiModels.LandmarksResponse>() {
            @Override public void onResponse(@NonNull Call<ApiModels.LandmarksResponse> c,
                                             @NonNull Response<ApiModels.LandmarksResponse> r) {
                if (r.body() == null) return;
                landmarks.clear();
                List<String> names = new ArrayList<>();
                for (ApiModels.Landmark l : r.body().landmarks) { landmarks.add(l); names.add(l.name); }
                ArrayAdapter<String> ad = new ArrayAdapter<>(RouteMapActivity.this,
                        android.R.layout.simple_spinner_dropdown_item, names);
                fromSpinner.setAdapter(ad); toSpinner.setAdapter(ad);
                if (names.size() > 1) toSpinner.setSelection(1);
                if (!r.body().realCity) {
                    info.setText("Map works, but routing needs the real Edmonton graph — "
                            + "run build_city_graph.py, then restart the backend.");
                }
            }
            @Override public void onFailure(@NonNull Call<ApiModels.LandmarksResponse> c, @NonNull Throwable t) { }
        });
    }

    private void routeLandmarks() {
        if (landmarks.size() < 2) { info.setText("Landmarks not loaded yet."); return; }
        ApiModels.Landmark a = landmarks.get(fromSpinner.getSelectedItemPosition());
        ApiModels.Landmark b = landmarks.get(toSpinner.getSelectedItemPosition());
        clearAll();
        pickupMarker = makeMarker(new GeoPoint(a.lat, a.lon), a.name, GREEN);
        dropoffMarker = makeMarker(new GeoPoint(b.lat, b.lon), b.name, RED);
        map.getOverlays().add(pickupMarker);
        map.getOverlays().add(dropoffMarker);
        routeFromMarkers(a.name + " → " + b.name);
    }

    // ───────────────────────── routing ─────────────────────────
    private void routeFromMarkers(String label) {
        GeoPoint a = pickupMarker.getPosition(), b = dropoffMarker.getPosition();
        progress.setVisibility(View.VISIBLE);
        info.setText("Routing…");
        ApiModels.RouteLatLonRequest req = new ApiModels.RouteLatLonRequest(
                a.getLatitude(), a.getLongitude(), b.getLatitude(), b.getLongitude(),
                18, 0, 0.6, 1.2);
        ApiClient.get().routeLatLon(req).enqueue(new Callback<ApiModels.RouteResponse>() {
            @Override public void onResponse(@NonNull Call<ApiModels.RouteResponse> c,
                                             @NonNull Response<ApiModels.RouteResponse> r) {
                progress.setVisibility(View.GONE);
                if (!r.isSuccessful() || r.body() == null) {
                    info.setText("Routing needs the real Edmonton map "
                            + "(run build_city_graph.py, restart backend).");
                    return;
                }
                drawRoute(r.body(), label);
            }
            @Override public void onFailure(@NonNull Call<ApiModels.RouteResponse> c, @NonNull Throwable t) {
                progress.setVisibility(View.GONE);
                info.setText("Can't reach server. Is the backend running and BASE_URL correct?");
            }
        });
    }

    private void drawRoute(ApiModels.RouteResponse b, String label) {
        if (b.polylineLatlon == null || b.polylineLatlon.isEmpty()) {
            info.setText("No route geometry returned."); return;
        }
        List<GeoPoint> pts = new ArrayList<>();
        for (List<Double> p : b.polylineLatlon) pts.add(new GeoPoint(p.get(0), p.get(1)));

        if (routeLine != null) map.getOverlays().remove(routeLine);
        routeLine = new Polyline();
        routeLine.setPoints(pts);
        routeLine.getOutlinePaint().setColor(Color.parseColor("#1E6FEB"));
        routeLine.getOutlinePaint().setStrokeWidth(12f);
        map.getOverlays().add(routeLine);
        // re-add the pins so they draw on top of the route line
        if (pickupMarker != null) { map.getOverlays().remove(pickupMarker); map.getOverlays().add(pickupMarker); }
        if (dropoffMarker != null) { map.getOverlays().remove(dropoffMarker); map.getOverlays().add(dropoffMarker); }

        info.setText(String.format(Locale.US,
                "%s\n%.1f km  ·  ETA %.0f min  ·  $%.2f  ·  %d turns\nDrag a pin to adjust — it re-routes.",
                label, b.distanceKm, b.etaMin, b.fareUsd, b.turns));

        map.post(() -> map.zoomToBoundingBox(BoundingBox.fromGeoPoints(pts), true, 90));
        map.invalidate();
    }

    private void clearAll() {
        if (routeLine != null) { map.getOverlays().remove(routeLine); routeLine = null; }
        if (pickupMarker != null) { map.getOverlays().remove(pickupMarker); pickupMarker = null; }
        if (dropoffMarker != null) { map.getOverlays().remove(dropoffMarker); dropoffMarker = null; }
        map.invalidate();
    }

    private void resetMap() {
        clearAll();
        info.setText("Tap the map to set your PICKUP (green).");
    }
}
