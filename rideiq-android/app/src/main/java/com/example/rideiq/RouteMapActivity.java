package com.example.rideiq;

import android.Manifest;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.DashPathEffect;
import android.graphics.Paint;
import android.graphics.drawable.BitmapDrawable;
import android.graphics.drawable.Drawable;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.os.Bundle;
import android.preference.PreferenceManager;
import android.speech.tts.TextToSpeech;
import android.speech.tts.Voice;
import android.view.Menu;
import android.view.MenuItem;
import android.view.View;
import android.view.inputmethod.InputMethodManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;

import com.google.android.material.bottomnavigation.BottomNavigationView;

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
import java.util.Arrays;
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
    private EditText startField, destField;
    private ProgressBar progress;
    private Button startBtn;

    private Marker pickupMarker, dropoffMarker;
    private Polyline routeLine, pickupConnector, dropoffConnector;
    private MyLocationNewOverlay myLocation;
    private boolean satellite = false;
    private String travelMode = "drive";     // "drive" or "walk"
    private Button modeBtn;

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
        startField = findViewById(R.id.startField);
        destField = findViewById(R.id.destField);

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
        ((Button) findViewById(R.id.goBtn)).setOnClickListener(v -> onGoTapped());
        modeBtn = findViewById(R.id.modeBtn);
        modeBtn.setOnClickListener(v -> toggleMode());
        NavBar.setup(this, (BottomNavigationView) findViewById(R.id.bottomNav), R.id.nav_map);

        lm = (LocationManager) getSystemService(LOCATION_SERVICE);
        tts = new TextToSpeech(this, status -> {
            if (status == TextToSpeech.SUCCESS) {
                tts.setLanguage(Locale.US);
                tts.setSpeechRate(1.2f);     // faster
                tts.setPitch(0.85f);         // deeper, more male-sounding
                selectMaleVoice();
                ttsReady = true;
            }
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

    private void toggleMode() {
        travelMode = travelMode.equals("drive") ? "walk" : "drive";
        modeBtn.setText(travelMode.equals("walk") ? R.string.mode_walk : R.string.mode_drive);
        if (pickupMarker != null && dropoffMarker != null) routeFromMarkers("Route");  // re-route in new mode
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

    private void setPickup(GeoPoint p) { setPickup(p, null); }

    /** knownAddr != null skips the reverse-geocode lookup (we already know the address). */
    private void setPickup(GeoPoint p, String knownAddr) {
        if (pickupMarker == null) {
            pickupMarker = makeMarker(p, "Pickup", GREEN);
            map.getOverlays().add(pickupMarker);
        } else {
            pickupMarker.setPosition(p);
        }
        if (knownAddr != null && !knownAddr.isEmpty()) pickupText.setText("Pickup: " + knownAddr);
        else geocode(p, pickupText, "Pickup");
        if (dropoffMarker != null) routeFromMarkers("Route");
        map.invalidate();
    }

    private void setDropoff(GeoPoint p) { setDropoff(p, null); }

    private void setDropoff(GeoPoint p, String knownAddr) {
        if (dropoffMarker == null) {
            dropoffMarker = makeMarker(p, "Drop-off", RED);
            map.getOverlays().add(dropoffMarker);
        } else {
            dropoffMarker.setPosition(p);
        }
        if (knownAddr != null && !knownAddr.isEmpty()) dropoffText.setText("Drop-off: " + knownAddr);
        else geocode(p, dropoffText, "Drop-off");
        if (pickupMarker != null) routeFromMarkers("Route");
        map.invalidate();
    }

    // ───────────────────────── manual address entry (forward geocoding) ─────────────────────────
    private void onGoTapped() {
        String sTxt = startField.getText().toString().trim();
        String dTxt = destField.getText().toString().trim();
        if (sTxt.isEmpty() && dTxt.isEmpty()) {
            Toast.makeText(this, "Type a start and/or destination address.", Toast.LENGTH_SHORT).show();
            return;
        }
        hideKeyboard();
        if (!sTxt.isEmpty()) geocodeToPin(sTxt, true);
        if (!dTxt.isEmpty()) geocodeToPin(dTxt, false);
    }

    /** Look up a typed address and drop the matching pin (isStart -> pickup, else drop-off). */
    private void geocodeToPin(final String query, final boolean isStart) {
        progress.setVisibility(View.VISIBLE);
        info.setText("Finding \"" + query + "\"…");
        ApiClient.get().geocode(query).enqueue(new Callback<ApiModels.ReverseGeocodeResponse>() {
            @Override public void onResponse(@NonNull Call<ApiModels.ReverseGeocodeResponse> c,
                                             @NonNull Response<ApiModels.ReverseGeocodeResponse> r) {
                progress.setVisibility(View.GONE);
                if (!r.isSuccessful() || r.body() == null) {
                    info.setText("Couldn't find \"" + query + "\" in Edmonton. Try a more specific address.");
                    return;
                }
                ApiModels.ReverseGeocodeResponse b = r.body();
                GeoPoint gp = new GeoPoint(b.lat, b.lon);
                String label = (b.shortLabel != null && !b.shortLabel.isEmpty()) ? b.shortLabel : query;
                boolean bothSetAfter;
                if (isStart) { setPickup(gp, label); bothSetAfter = dropoffMarker != null; }
                else { setDropoff(gp, label); bothSetAfter = pickupMarker != null; }
                if (!bothSetAfter) { map.getController().animateTo(gp); map.getController().setZoom(15.0); }
            }
            @Override public void onFailure(@NonNull Call<ApiModels.ReverseGeocodeResponse> c,
                                            @NonNull Throwable t) {
                progress.setVisibility(View.GONE);
                info.setText("Can't reach server for address lookup.");
            }
        });
    }

    private void hideKeyboard() {
        InputMethodManager imm = getSystemService(InputMethodManager.class);
        View f = getCurrentFocus();
        if (imm != null && f != null) imm.hideSoftInputFromWindow(f.getWindowToken(), 0);
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
                        target.setText(label + ": " + (addr != null && !addr.isEmpty() ? addr : "pinned location"));
                        syncField(label, addr);   // keep the Start/Destination text boxes in sync with the pins
                    }
                    @Override public void onFailure(@NonNull Call<ApiModels.ReverseGeocodeResponse> c,
                                                    @NonNull Throwable t) {
                        target.setText(label + ": pinned location");
                    }
                });
    }

    // ───────────────────────── routing ─────────────────────────
    private void routeFromMarkers(String label) {
        GeoPoint a = pickupMarker.getPosition(), b = dropoffMarker.getPosition();
        progress.setVisibility(View.VISIBLE);
        if (!navigating) info.setText("Routing…");
        ApiModels.RouteLatLonRequest req = new ApiModels.RouteLatLonRequest(
                a.getLatitude(), a.getLongitude(), b.getLatitude(), b.getLongitude(),
                18, 0, 0.6, 1.2);
        req.mode = travelMode;
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

        // Dashed connectors bridge the gap between the actual pins and where the route
        // snapped to the nearest road/path node (so the line visibly reaches each pin).
        removeConnectors();
        if (pickupMarker != null)
            pickupConnector = addConnector(pickupMarker.getPosition(), routePts.get(0));
        if (dropoffMarker != null)
            dropoffConnector = addConnector(routePts.get(routePts.size() - 1), dropoffMarker.getPosition());

        if (pickupMarker != null) { map.getOverlays().remove(pickupMarker); map.getOverlays().add(pickupMarker); }
        if (dropoffMarker != null) { map.getOverlays().remove(dropoffMarker); map.getOverlays().add(dropoffMarker); }

        // store turn-by-turn steps for navigation
        navSteps = (b.steps != null) ? b.steps : new ArrayList<>();
        startBtn.setEnabled(!navSteps.isEmpty());
        if (navigating) { currentStep = 0; preAnnounced.clear(); offRouteCount = 0; }

        if (!navigating) {
            boolean walk = "walk".equals(b.mode);
            String head = walk
                    ? String.format(Locale.US, "%s\n%.1f km  ·  %.0f min walk  ·  %d turns", label, b.distanceKm, b.etaMin, b.turns)
                    : String.format(Locale.US, "%s\n%.1f km  ·  ETA %.0f min  ·  $%.2f  ·  %d turns", label, b.distanceKm, b.etaMin, b.fareUsd, b.turns);
            if (b.modeFallback)
                head += "\n(Walking map not loaded yet — showing the driving route.)";
            else
                head += "\nTap “Start trip” for voice navigation.";
            info.setText(head);
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

    /** Prefer an English male voice if the device has one (best-effort; deeper pitch is the fallback). */
    private void selectMaleVoice() {
        try {
            if (tts.getVoices() == null) return;
            for (Voice v : tts.getVoices()) {
                String n = v.getName() == null ? "" : v.getName().toLowerCase(Locale.US);
                if (v.getLocale() != null && "en".equals(v.getLocale().getLanguage())
                        && n.contains("male") && !n.contains("female")) {
                    tts.setVoice(v);
                    return;
                }
            }
        } catch (Exception ignored) { }
    }

    /** Fill the Start / Destination text box to match a resolved pin address (two-way sync). */
    private void syncField(String label, String addr) {
        if (addr == null || addr.isEmpty()) return;
        if ("Pickup".equals(label) && startField != null) startField.setText(addr);
        else if ("Drop-off".equals(label) && destField != null) destField.setText(addr);
    }


    /** A short dashed line bridging a pin to the nearest routable node (null if the gap is tiny). */
    private Polyline addConnector(GeoPoint a, GeoPoint b) {
        if (distMeters(a.getLatitude(), a.getLongitude(), b.getLatitude(), b.getLongitude()) < 10f) return null;
        Polyline c = new Polyline();
        c.setPoints(Arrays.asList(a, b));
        c.getOutlinePaint().setColor(Color.parseColor("#1E6FEB"));
        c.getOutlinePaint().setStrokeWidth(7f);
        c.getOutlinePaint().setPathEffect(new DashPathEffect(new float[]{16f, 12f}, 0f));
        map.getOverlays().add(c);
        return c;
    }

    private void removeConnectors() {
        if (pickupConnector != null) { map.getOverlays().remove(pickupConnector); pickupConnector = null; }
        if (dropoffConnector != null) { map.getOverlays().remove(dropoffConnector); dropoffConnector = null; }
    }

    private void resetMap() {
        if (navigating) stopTrip();
        if (routeLine != null) { map.getOverlays().remove(routeLine); routeLine = null; }
        removeConnectors();
        if (dropoffMarker != null) { map.getOverlays().remove(dropoffMarker); dropoffMarker = null; }
        navSteps = new ArrayList<>();
        startBtn.setEnabled(false);
        dropoffText.setText(R.string.dropoff_hint);
        info.setText("Drop-off cleared. Tap the map to set a new one.");
        map.invalidate();
    }
}
