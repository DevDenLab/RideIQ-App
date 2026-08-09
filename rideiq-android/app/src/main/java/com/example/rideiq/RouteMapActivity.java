package com.example.rideiq;

import android.Manifest;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.drawable.BitmapDrawable;
import android.graphics.drawable.Drawable;
import android.os.Bundle;
import android.preference.PreferenceManager;
import android.view.View;
import android.widget.Button;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;

import org.osmdroid.config.Configuration;
import org.osmdroid.events.MapEventsReceiver;
import org.osmdroid.tileprovider.tilesource.OnlineTileSourceBase;
import org.osmdroid.tileprovider.tilesource.XYTileSource;
import org.osmdroid.util.BoundingBox;
import org.osmdroid.util.GeoPoint;
import org.osmdroid.util.MapTileIndex;
import org.osmdroid.views.MapView;
import org.osmdroid.views.overlay.MapEventsOverlay;
import org.osmdroid.views.overlay.Marker;
import org.osmdroid.views.overlay.Polyline;
import org.osmdroid.views.overlay.mylocation.GpsMyLocationProvider;
import org.osmdroid.views.overlay.mylocation.MyLocationNewOverlay;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

import retrofit2.Call;
import retrofit2.Callback;
import retrofit2.Response;

/**
 * Uber-style routing on osmdroid (free OpenStreetMap tiles, no API key).
 *
 *  • Pickup is auto-set to your GPS location; its address is filled in automatically.
 *  • Tap the map (or drag the red pin) to set the drop-off — its address fills in too.
 *  • The route, distance, ETA and fare update whenever a pin moves.
 */
public class RouteMapActivity extends AppCompatActivity {

    private static final int GREEN = Color.parseColor("#2E7D32");
    private static final int RED = Color.parseColor("#C62828");
    private static final int REQ_LOCATION = 101;

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
    private TextView info, pickupText, dropoffText;
    private ProgressBar progress;

    private Marker pickupMarker, dropoffMarker;
    private Polyline routeLine;
    private MyLocationNewOverlay myLocation;
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
        pickupText = findViewById(R.id.pickupText);
        dropoffText = findViewById(R.id.dropoffText);

        map = findViewById(R.id.map);
        map.setTileSource(STREET);
        map.setMultiTouchControls(true);
        map.getController().setZoom(12.0);
        map.getController().setCenter(new GeoPoint(53.5461, -113.4938));  // Edmonton

        map.getOverlays().add(new MapEventsOverlay(new MapEventsReceiver() {
            @Override public boolean singleTapConfirmedHelper(GeoPoint p) { onMapTap(p); return true; }
            @Override public boolean longPressHelper(GeoPoint p) { return false; }
        }));

        ((Button) findViewById(R.id.resetBtn)).setOnClickListener(v -> resetMap());
        ((Button) findViewById(R.id.myLocBtn)).setOnClickListener(v -> onMyLocationTapped());
        ((Button) findViewById(R.id.satBtn)).setOnClickListener(v -> toggleSatellite());

        setupMyLocation();

        // Auto-set the pickup to the user's location as soon as we're allowed.
        if (hasLocationPermission()) {
            setPickupToMyLocation();
        } else {
            ActivityCompat.requestPermissions(this,
                    new String[]{Manifest.permission.ACCESS_FINE_LOCATION,
                                 Manifest.permission.ACCESS_COARSE_LOCATION}, REQ_LOCATION);
        }
    }

    @Override protected void onResume() {
        super.onResume();
        map.onResume();
        if (myLocation != null && hasLocationPermission()) myLocation.enableMyLocation();
    }

    @Override protected void onPause() {
        super.onPause();
        map.onPause();
        if (myLocation != null) myLocation.disableMyLocation();   // stop GPS to save battery
    }

    private void toggleSatellite() {
        satellite = !satellite;
        map.setTileSource(satellite ? ESRI_SAT : STREET);
        map.invalidate();
    }

    // ───────────────────────── my location ─────────────────────────
    private void setupMyLocation() {
        myLocation = new MyLocationNewOverlay(new GpsMyLocationProvider(this), map);
        map.getOverlays().add(myLocation);
        if (hasLocationPermission()) myLocation.enableMyLocation();
    }

    private boolean hasLocationPermission() {
        return ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
                == PackageManager.PERMISSION_GRANTED;
    }

    private void onMyLocationTapped() {
        if (!hasLocationPermission()) {
            ActivityCompat.requestPermissions(this,
                    new String[]{Manifest.permission.ACCESS_FINE_LOCATION,
                                 Manifest.permission.ACCESS_COARSE_LOCATION}, REQ_LOCATION);
            return;
        }
        setPickupToMyLocation();
    }

    /** Center on the GPS fix and use it as the pickup point. */
    private void setPickupToMyLocation() {
        myLocation.enableMyLocation();
        myLocation.enableFollowLocation();
        pickupText.setText("Pickup: finding your location…");
        myLocation.runOnFirstFix(() -> runOnUiThread(() -> {
            GeoPoint here = myLocation.getMyLocation();
            if (here != null) {
                map.getController().animateTo(here);
                map.getController().setZoom(16.0);
                setPickup(here);
                info.setText("Pickup set to your location. Tap the map to set your drop-off.");
            }
        }));
    }

    @Override
    public void onRequestPermissionsResult(int req, @NonNull String[] perms, @NonNull int[] results) {
        super.onRequestPermissionsResult(req, perms, results);
        if (req == REQ_LOCATION) {
            if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) {
                setPickupToMyLocation();
            } else {
                Toast.makeText(this, R.string.location_needed, Toast.LENGTH_LONG).show();
                info.setText("Tap the map to set your pickup, then tap again for the drop-off.");
            }
        }
    }

    // ───────────────────────── selection ─────────────────────────
    private void onMapTap(GeoPoint p) {
        if (pickupMarker == null) setPickup(p);   // no location yet → first tap is pickup
        else setDropoff(p);                        // otherwise every tap sets/moves the drop-off
    }

    private void setPickup(GeoPoint p) {
        if (pickupMarker == null) {
            pickupMarker = makeMarker(p, "Pickup", GREEN);
            map.getOverlays().add(pickupMarker);
        } else {
            pickupMarker.setPosition(p);
        }
        geocode(p, pickupText, "Pickup");
        if (dropoffMarker != null) routeFromMarkers("Route");
        map.invalidate();
    }

    private void setDropoff(GeoPoint p) {
        if (dropoffMarker == null) {
            dropoffMarker = makeMarker(p, "Drop-off", RED);
            map.getOverlays().add(dropoffMarker);
        } else {
            dropoffMarker.setPosition(p);
        }
        geocode(p, dropoffText, "Drop-off");
        if (pickupMarker != null) routeFromMarkers("Route");
        map.invalidate();
    }

    /** A draggable, colored dot marker. Dragging re-geocodes that pin and re-routes. */
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
                if (mk == pickupMarker) geocode(mk.getPosition(), pickupText, "Pickup");
                else if (mk == dropoffMarker) geocode(mk.getPosition(), dropoffText, "Drop-off");
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

    // ───────────────────────── reverse geocoding (coords → address) ─────────────────────────
    private void geocode(final GeoPoint p, final TextView target, final String label) {
        target.setText(label + ": …");
        ApiClient.get().reverseGeocode(p.getLatitude(), p.getLongitude())
                .enqueue(new Callback<ApiModels.ReverseGeocodeResponse>() {
                    @Override public void onResponse(@NonNull Call<ApiModels.ReverseGeocodeResponse> c,
                                                     @NonNull Response<ApiModels.ReverseGeocodeResponse> r) {
                        String addr = (r.isSuccessful() && r.body() != null) ? r.body().shortLabel : null;
                        target.setText(label + ": " + (addr != null && !addr.isEmpty() ? addr : coords(p)));
                    }
                    @Override public void onFailure(@NonNull Call<ApiModels.ReverseGeocodeResponse> c,
                                                    @NonNull Throwable t) {
                        target.setText(label + ": " + coords(p));   // fall back to raw coordinates
                    }
                });
    }

    private String coords(GeoPoint p) {
        return String.format(Locale.US, "%.5f, %.5f", p.getLatitude(), p.getLongitude());
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
                    info.setText("Couldn't route between those points (they may be outside Edmonton).");
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

    private void resetMap() {
        if (routeLine != null) { map.getOverlays().remove(routeLine); routeLine = null; }
        if (dropoffMarker != null) { map.getOverlays().remove(dropoffMarker); dropoffMarker = null; }
        dropoffText.setText(R.string.dropoff_hint);
        info.setText("Drop-off cleared. Tap the map to set a new one.");
        map.invalidate();
    }
}
