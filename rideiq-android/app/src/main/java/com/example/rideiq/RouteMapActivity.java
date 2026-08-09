package com.example.rideiq;

import android.Manifest;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.drawable.BitmapDrawable;
import android.graphics.drawable.Drawable;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.os.Bundle;
import android.preference.PreferenceManager;
import android.speech.tts.TextToSpeech;
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
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

import retrofit2.Call;
import retrofit2.Callback;
import retrofit2.Response;

/**
 * Uber/Google-Maps-style routing + turn-by-turn navigation on osmdroid (free OSM, no API key).
 *
 *  • Pickup auto-sets to your GPS; drop-off is a tap or drag. Addresses fill in automatically.
 *  • "Start trip" enters navigation: the map follows your GPS and each turn is spoken and shown
 *    ("In 200 meters, turn left"), just like Google Maps. Announces arrival; reroutes if you drift.
 */
public class RouteMapActivity extends AppCompatActivity {

    private static final int GREEN = Color.parseColor("#2E7D32");
    private static final int RED = Color.parseColor("#C62828");
    private static final int REQ_LOCATION = 101;

    // Navigation tuning (metres).
    private static final float ANNOUNCE_AHEAD_M = 200f;   // pre-warn distance
    private static final float MANEUVER_HIT_M = 30f;      // "do it now" distance
    private static final float OFF_ROUTE_M = 80f;         // trigger reroute beyond this

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
    private TextView info, pickupText, dropoffText, navBanner;
    private ProgressBar progress;
    private Button startBtn;

    private Marker pickupMarker, dropoffMarker;
    private Polyline routeLine;
    private MyLocationNewOverlay myLocation;
    private boolean satellite = false;

    // navigation state
    private LocationManager lm;
    private TextToSpeech tts;
    private boolean ttsReady = false;
    private boolean navigating = false;
    private List<ApiModels.Step> navSteps = new ArrayList<>();
    private List<GeoPoint> routePts = new ArrayList<>();
    private final Set<Integer> preAnnounced = new HashSet<>();
    private int currentStep = 0;
    private int offRouteCount = 0;

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
        navBanner = findViewById(R.id.navBanner);
        startBtn = findViewById(R.id.startBtn);

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
        startBtn.setOnClickListener(v -> { if (navigating) stopTrip(); else startTrip(); });

        lm = (LocationManager) getSystemService(LOCATION_SERVICE);
        tts = new TextToSpeech(this, status -> {
            if (status == TextToSpeech.SUCCESS) { tts.setLanguage(Locale.US); ttsReady = true; }
        });

        setupMyLocation();

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
        if (myLocation != null && !navigating) myLocation.disableMyLocation();
    }

    @Override protected void onDestroy() {
        super.onDestroy();
        if (lm != null) lm.removeUpdates(navListener);
        if (tts != null) { tts.stop(); tts.shutdown(); }
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
        if (navigating) return;                    // don't change the trip mid-navigation
        if (pickupMarker == null) setPickup(p);
        else setDropoff(p);
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

    // ───────────────────────── reverse geocoding ─────────────────────────
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
                        target.setText(label + ": " + coords(p));
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
        if (!navigating) info.setText("Routing…");
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
        routePts = new ArrayList<>();
        for (List<Double> p : b.polylineLatlon) routePts.add(new GeoPoint(p.get(0), p.get(1)));

        if (routeLine != null) map.getOverlays().remove(routeLine);
        routeLine = new Polyline();
        routeLine.setPoints(routePts);
        routeLine.getOutlinePaint().setColor(Color.parseColor("#1E6FEB"));
        routeLine.getOutlinePaint().setStrokeWidth(12f);
        map.getOverlays().add(routeLine);
        if (pickupMarker != null) { map.getOverlays().remove(pickupMarker); map.getOverlays().add(pickupMarker); }
        if (dropoffMarker != null) { map.getOverlays().remove(dropoffMarker); map.getOverlays().add(dropoffMarker); }

        // store turn-by-turn steps for navigation
        navSteps = (b.steps != null) ? b.steps : new ArrayList<>();
        startBtn.setEnabled(!navSteps.isEmpty());
        if (navigating) { currentStep = 0; preAnnounced.clear(); offRouteCount = 0; }

        if (!navigating) {
            info.setText(String.format(Locale.US,
                    "%s\n%.1f km  ·  ETA %.0f min  ·  $%.2f  ·  %d turns\nTap “Start trip” for voice navigation.",
                    label, b.distanceKm, b.etaMin, b.fareUsd, b.turns));
            map.post(() -> map.zoomToBoundingBox(BoundingBox.fromGeoPoints(routePts), true, 90));
        }
        map.invalidate();
    }

    // ───────────────────────── navigation ─────────────────────────
    private final LocationListener navListener = new LocationListener() {
        @Override public void onLocationChanged(@NonNull Location loc) { onNavLocation(loc); }
        @Override public void onStatusChanged(String provider, int status, Bundle extras) { }
        @Override public void onProviderEnabled(@NonNull String provider) { }
        @Override public void onProviderDisabled(@NonNull String provider) { }
    };

    private void startTrip() {
        if (navSteps == null || navSteps.isEmpty()) {
            Toast.makeText(this, "Set a destination first.", Toast.LENGTH_SHORT).show();
            return;
        }
        if (!hasLocationPermission()) { onMyLocationTapped(); return; }
        navigating = true;
        currentStep = 0;
        preAnnounced.clear();
        offRouteCount = 0;
        startBtn.setText(R.string.stop_trip);
        navBanner.setVisibility(View.VISIBLE);
        navBanner.setText("Starting navigation…");
        myLocation.enableMyLocation();
        map.getController().setZoom(18.0);
        speak("Starting navigation.");
        startLocationUpdates();
    }

    private void stopTrip() {
        navigating = false;
        startBtn.setText(R.string.start_trip);
        navBanner.setVisibility(View.GONE);
        if (lm != null) lm.removeUpdates(navListener);
    }

    private void startLocationUpdates() {
        if (!hasLocationPermission()) return;
        try { lm.requestLocationUpdates(LocationManager.GPS_PROVIDER, 1000, 1, navListener); }
        catch (Exception ignored) { }
        try { lm.requestLocationUpdates(LocationManager.NETWORK_PROVIDER, 1000, 1, navListener); }
        catch (Exception ignored) { }
    }

    private void onNavLocation(Location loc) {
        if (!navigating) return;
        GeoPoint here = new GeoPoint(loc.getLatitude(), loc.getLongitude());
        map.getController().animateTo(here);                 // camera follows you

        if (currentStep >= navSteps.size()) return;
        ApiModels.Step step = navSteps.get(currentStep);
        float d = distMeters(loc.getLatitude(), loc.getLongitude(), step.lat, step.lon);
        boolean arrive = "arrive".equals(step.maneuver);

        navBanner.setText(arrive
                ? String.format(Locale.US, "Arriving in %d m", (int) d)
                : String.format(Locale.US, "%s  ·  %d m", step.instruction, (int) d));

        if (!preAnnounced.contains(currentStep) && d < ANNOUNCE_AHEAD_M) {
            int rounded = Math.max(10, Math.round(d / 10f) * 10);
            speak(arrive ? "In " + rounded + " meters you will arrive."
                         : "In " + rounded + " meters, " + step.instruction + ".");
            preAnnounced.add(currentStep);
        }

        if (d < MANEUVER_HIT_M) {
            if (arrive) {
                speak("You have arrived at your destination.");
                navBanner.setText("Arrived.");
                stopTrip();
            } else {
                speak(step.instruction + " now.");
                currentStep++;
            }
            return;
        }

        // off-route detection → reroute from current position
        if (distToRoute(loc) > OFF_ROUTE_M) {
            if (++offRouteCount >= 2) { offRouteCount = 0; reroute(here); }
        } else {
            offRouteCount = 0;
        }
    }

    private void reroute(GeoPoint here) {
        speak("Rerouting.");
        navBanner.setText("Rerouting…");
        setPickup(here);   // re-routes from here; drawRoute() resets the nav steps while navigating
    }

    private float distMeters(double la1, double lo1, double la2, double lo2) {
        float[] r = new float[1];
        Location.distanceBetween(la1, lo1, la2, lo2, r);
        return r[0];
    }

    private float distToRoute(Location loc) {
        if (routePts == null || routePts.isEmpty()) return 0f;
        float best = Float.MAX_VALUE;
        for (GeoPoint p : routePts) {
            float d = distMeters(loc.getLatitude(), loc.getLongitude(), p.getLatitude(), p.getLongitude());
            if (d < best) best = d;
        }
        return best;
    }

    private void speak(String text) {
        if (tts != null && ttsReady) tts.speak(text, TextToSpeech.QUEUE_FLUSH, null, "nav");
    }

    private void resetMap() {
        if (navigating) stopTrip();
        if (routeLine != null) { map.getOverlays().remove(routeLine); routeLine = null; }
        if (dropoffMarker != null) { map.getOverlays().remove(dropoffMarker); dropoffMarker = null; }
        navSteps = new ArrayList<>();
        startBtn.setEnabled(false);
        dropoffText.setText(R.string.dropoff_hint);
        info.setText("Drop-off cleared. Tap the map to set a new one.");
        map.invalidate();
    }
}
