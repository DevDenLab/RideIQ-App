package com.example.rideiq;

import android.Manifest;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.DashPathEffect;
import android.graphics.Paint;
import android.graphics.Typeface;
import android.graphics.drawable.BitmapDrawable;
import android.graphics.drawable.Drawable;
import android.graphics.drawable.GradientDrawable;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.preference.PreferenceManager;
import android.speech.tts.TextToSpeech;
import android.speech.tts.Voice;
import android.text.Editable;
import android.text.TextWatcher;
import android.view.Menu;
import android.view.MenuItem;
import android.view.View;
import android.view.ViewGroup;
import android.view.inputmethod.InputMethodManager;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.HorizontalScrollView;
import android.widget.ImageButton;
import android.widget.LinearLayout;
import android.widget.ListView;
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
    private static final int REQ_NOTIFICATIONS = 102;

    // Navigation tuning (metres).
    private static final float ANNOUNCE_AHEAD_M = 200f;   // pre-warn distance
    private static final float MANEUVER_HIT_M = 30f;      // "do it now" distance
    private static final float OFF_ROUTE_M = 80f;         // trigger reroute beyond this

    // OpenStreetMap's own tiles. We used CARTO's free raster basemap until it began
    // requiring an API key in August 2026 -- and it does not fail loudly, it returns
    // HTTP 200 with every tile watermarked "API KEY REQUIRED", so nothing in the app
    // errors or logs. Getting a key would only buy time: CARTO is retiring the raster
    // endpoint in favour of vector tiles, so this moves off it entirely rather than
    // onto a deprecated path.
    private static final XYTileSource STREET = new XYTileSource(
            "OpenStreetMap", 0, 19, 256, ".png",
            new String[]{"https://tile.openstreetmap.org/"},
            "© OpenStreetMap contributors");

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
    private String travelMode = "drive";     // "drive", "walk" or "transit"
    private Button driveBtn, walkBtn, transitModeBtn;

    // route options (alternatives)
    private HorizontalScrollView optionsScroll;
    private LinearLayout optionsRow;
    private List<ApiModels.RouteResponse> routeOptions = new ArrayList<>();
    private int selectedOption = 0;
    private String lastRouteLabel = "Route";

    // transit itineraries. A transit trip is several legs in different modes, so it
    // needs its own overlays rather than the single routeLine a car trip draws.
    private List<ApiModels.Itinerary> transitOptions = new ArrayList<>();
    private int selectedItinerary = 0;
    private final List<Polyline> transitLines = new ArrayList<>();
    private final List<Marker> transitStops = new ArrayList<>();

    // Transit query options. "now" means leave now; otherwise an ISO local time,
    // which transitArriveBy turns from "leave at" into "be there by".
    private String transitWhen = "now";
    private boolean transitArriveBy = false;
    private boolean wheelchair = false;
    private LinearLayout transitOptionsRow;
    private Button whenBtn, accessBtn;

    // The two floating cards cover the top and bottom of the map, and in transit
    // mode they grow: the info line runs to several lines and the itinerary cards
    // appear. Collapsing them is the only way to actually look at the route.
    private View searchCard, routePanel;
    private ImageButton mapToggleBtn;
    private boolean panelsHidden = false;

    /** Fallback colours for routes whose agency publishes no route_color. */
    private static final String[] LEG_COLORS = {"#1E6FEB", "#E65100", "#6A1B9A", "#00838F"};
    private static final int WALK_LEG = Color.parseColor("#757575");

    // address autocomplete
    private ListView suggestionsList;
    private ArrayAdapter<String> suggestAdapter;
    private final List<ApiModels.ReverseGeocodeResponse> suggestions = new ArrayList<>();
    private final Handler searchHandler = new Handler(Looper.getMainLooper());
    private Runnable searchRunnable;
    private EditText activeField;
    private boolean suppressAutocomplete = false;

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
        // OSM's tile usage policy requires a User-Agent that identifies the app,
        // not a generic library default -- anonymous bulk clients get blocked.
        Configuration.getInstance().setUserAgentValue("RideIQ/1.0 (" + getPackageName() + ")");

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
        optionsScroll = findViewById(R.id.optionsScroll);
        optionsRow = findViewById(R.id.optionsRow);
        suggestionsList = findViewById(R.id.suggestionsList);
        suggestAdapter = new ArrayAdapter<>(this, android.R.layout.simple_list_item_1, new ArrayList<>());
        suggestionsList.setAdapter(suggestAdapter);
        suggestionsList.setOnItemClickListener((parent, view, position, id) -> onSuggestionPicked(position));
        attachAutocomplete(startField);
        attachAutocomplete(destField);

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
        ((Button) findViewById(R.id.directionsBtn)).setOnClickListener(v -> openDirections());
        ((Button) findViewById(R.id.streetViewBtn)).setOnClickListener(v -> openStreetView());
        ((Button) findViewById(R.id.transitBtn)).setOnClickListener(v -> openTransit());
        driveBtn = findViewById(R.id.driveBtn);
        walkBtn = findViewById(R.id.walkBtn);
        transitModeBtn = findViewById(R.id.transitModeBtn);
        driveBtn.setOnClickListener(v -> setMode("drive"));
        walkBtn.setOnClickListener(v -> setMode("walk"));
        transitModeBtn.setOnClickListener(v -> setMode("transit"));
        transitOptionsRow = findViewById(R.id.transitOptionsRow);
        whenBtn = findViewById(R.id.whenBtn);
        accessBtn = findViewById(R.id.accessBtn);
        whenBtn.setOnClickListener(v -> pickTransitTime());
        accessBtn.setOnClickListener(v -> toggleWheelchair());
        searchCard = findViewById(R.id.searchCard);
        routePanel = findViewById(R.id.routePanel);
        mapToggleBtn = findViewById(R.id.mapToggleBtn);
        mapToggleBtn.setOnClickListener(v -> togglePanels());
        updateModePills();
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
        // A ride can outlive this screen, so the button has to reflect the
        // service, not a local flag.
        if (TransitRideService.RUNNING) {
            startBtn.setText(R.string.stop_trip);
            startBtn.setEnabled(true);
        }
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

    private void setMode(String mode) {
        if (mode.equals(travelMode)) return;
        travelMode = mode;
        updateModePills();
        updateLocationMarker();   // swap car <-> person icon
        if (pickupMarker != null && dropoffMarker != null) routeFromMarkers("Route");  // re-route in new mode
    }

    /** Segmented Drive|Walk|Transit pills: the selected one is filled purple. */
    private void updateModePills() {
        stylePill(driveBtn, "drive".equals(travelMode));
        stylePill(walkBtn, "walk".equals(travelMode));
        stylePill(transitModeBtn, "transit".equals(travelMode));
        boolean transit = "transit".equals(travelMode);
        if (transitOptionsRow != null) {
            transitOptionsRow.setVisibility(transit ? View.VISIBLE : View.GONE);
            stylePill(whenBtn, !"now".equals(transitWhen));
            stylePill(accessBtn, wheelchair);
        }
    }

    /**
     * "Leave now", "Depart at…", or "Arrive by…". Arrive-by is the one that earns
     * its keep for transit: it walks the whole plan backwards from when you have
     * to be somewhere, which is how people actually think about catching a bus.
     */
    private void pickTransitTime() {
        new android.app.AlertDialog.Builder(this)
                .setTitle("When?")
                .setItems(new String[]{"Leave now", "Depart at…", "Arrive by…"}, (d, which) -> {
                    if (which == 0) {
                        transitWhen = "now";
                        transitArriveBy = false;
                        whenBtn.setText(R.string.transit_leave_now);
                        updateModePills();
                        if (pickupMarker != null && dropoffMarker != null) routeTransit();
                        return;
                    }
                    askForClock(which == 2);
                })
                .show();
    }

    private void askForClock(boolean arriveBy) {
        java.util.Calendar now = java.util.Calendar.getInstance();
        new android.app.TimePickerDialog(this, (view, hour, minute) -> {
            java.util.Calendar when = java.util.Calendar.getInstance();
            when.set(java.util.Calendar.HOUR_OF_DAY, hour);
            when.set(java.util.Calendar.MINUTE, minute);
            when.set(java.util.Calendar.SECOND, 0);
            // A time already past today means tomorrow -- otherwise asking for
            // "arrive by 8am" in the afternoon silently returns nothing.
            if (when.getTimeInMillis() < System.currentTimeMillis())
                when.add(java.util.Calendar.DAY_OF_MONTH, 1);
            transitWhen = new java.text.SimpleDateFormat(
                    "yyyy-MM-dd'T'HH:mm:ss", Locale.US).format(when.getTime());
            transitArriveBy = arriveBy;
            whenBtn.setText(String.format(Locale.US, "%s %02d:%02d",
                    arriveBy ? "Arrive by" : "Depart", hour, minute));
            updateModePills();
            if (pickupMarker != null && dropoffMarker != null) routeTransit();
        }, now.get(java.util.Calendar.HOUR_OF_DAY), now.get(java.util.Calendar.MINUTE), true).show();
    }

    /** Step-free routing: OTP drops stops and vehicles the feed marks inaccessible. */
    private void toggleWheelchair() {
        wheelchair = !wheelchair;
        updateModePills();
        Toast.makeText(this, wheelchair ? "Step-free routing on" : "Step-free routing off",
                Toast.LENGTH_SHORT).show();
        if (pickupMarker != null && dropoffMarker != null && "transit".equals(travelMode))
            routeTransit();
    }

    private void stylePill(Button b, boolean selected) {
        if (b == null) return;
        float d = getResources().getDisplayMetrics().density;
        GradientDrawable bg = new GradientDrawable();
        bg.setCornerRadius(20 * d);
        bg.setColor(Color.parseColor(selected ? "#534AB7" : "#EEEDFE"));
        b.setBackground(bg);
        b.setTextColor(Color.parseColor(selected ? "#FFFFFF" : "#3C3489"));
    }

    /**
     * Show the whole map, or bring the panels back.
     *
     * The button never hides with them - it is the only route back, and a map tap
     * cannot serve as one because a single tap already drops the destination pin.
     */
    private void togglePanels() {
        panelsHidden = !panelsHidden;
        int vis = panelsHidden ? View.GONE : View.VISIBLE;
        searchCard.setVisibility(vis);
        routePanel.setVisibility(vis);
        // Collapsing while the keyboard is up over a search field would leave the
        // keyboard covering the map it was just asked to reveal.
        if (panelsHidden) {
            hideKeyboard();
            suggestionsList.setVisibility(View.GONE);
        }
        mapToggleBtn.setImageResource(
                panelsHidden ? R.drawable.ic_collapse_map : R.drawable.ic_expand_map);
        mapToggleBtn.setContentDescription(getString(
                panelsHidden ? R.string.map_toggle_collapse : R.string.map_toggle_expand));
    }

    /** Bring the panels back if something needs to be read there. */
    private void revealPanels() {
        if (panelsHidden) togglePanels();
    }

    // ───────────────────────── my location ─────────────────────────
    private void setupMyLocation() {
        myLocation = new MyLocationNewOverlay(new GpsMyLocationProvider(this), map);
        map.getOverlays().add(myLocation);
        updateLocationMarker();
        if (hasLocationPermission()) myLocation.enableMyLocation();
    }

    /** Show a car (driving) or a person (walking) as the "you are here" marker, not a triangle. */
    private void updateLocationMarker() {
        if (myLocation == null) return;
        // A transit trip starts and ends on foot, so the person icon is right there too.
        boolean walk = "walk".equals(travelMode) || "transit".equals(travelMode);
        Bitmap bmp = vectorToBitmap(walk ? R.drawable.ic_marker_person : R.drawable.ic_marker_car,
                walk ? Color.parseColor("#2E7D32") : Color.parseColor("#1E6FEB"));
        if (bmp != null) {
            myLocation.setDirectionArrow(bmp, bmp);   // sets both the stationary and moving icons
        }
    }

    private Bitmap vectorToBitmap(int resId, int color) {
        Drawable d = ContextCompat.getDrawable(this, resId);
        if (d == null) return null;
        int size = (int) (40 * getResources().getDisplayMetrics().density);
        Bitmap bmp = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888);
        Canvas cv = new Canvas(bmp);
        d.setBounds(0, 0, size, size);
        d.setTint(color);
        d.draw(cv);
        return bmp;
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
        } else if (req == REQ_NOTIFICATIONS) {
            if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) {
                startTransitRide();          // the user came back to say yes; carry on
            } else {
                // Refusing is legitimate, but a muted ride companion is worth
                // saying out loud rather than letting them wonder why nothing
                // ever appeared.
                Toast.makeText(this, "Without notifications the ride can still be "
                        + "tracked, but you won't be told when to get off.",
                        Toast.LENGTH_LONG).show();
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
        if ("transit".equals(travelMode)) { routeTransit(); return; }
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
                revealPanels();
                if (!r.isSuccessful() || r.body() == null) {
                    info.setText("Couldn't route between those points (they may be outside Edmonton).");
                    return;
                }
                ApiModels.RouteResponse body = r.body();
                if (navigating) { drawRoute(body, label); return; }
                List<ApiModels.RouteResponse> opts = new ArrayList<>();
                opts.add(body);
                if (body.alternatives != null) opts.addAll(body.alternatives);
                showRouteOptions(opts, label);
            }
            @Override public void onFailure(@NonNull Call<ApiModels.RouteResponse> c, @NonNull Throwable t) {
                progress.setVisibility(View.GONE);
                revealPanels();
                info.setText("Can't reach server. Is the backend running and BASE_URL correct?");
            }
        });
    }

    // ───────────────────────── public transit ─────────────────────────
    /**
     * Ask the backend for transit itineraries. This is the one routing call that is
     * time-dependent: the answer depends on when you ask, because you have to be at
     * the stop before the bus leaves. So there is no "alternatives" list of the same
     * shape — each itinerary is a different departure with its own clock.
     */
    private void routeTransit() {
        GeoPoint a = pickupMarker.getPosition(), b = dropoffMarker.getPosition();
        progress.setVisibility(View.VISIBLE);
        info.setText("Finding transit…");
        ApiClient.get().transit(a.getLatitude(), a.getLongitude(),
                        b.getLatitude(), b.getLongitude(), 1200, 3,
                        transitWhen, transitArriveBy, wheelchair)
                .enqueue(new Callback<ApiModels.TransitResponse>() {
                    @Override public void onResponse(@NonNull Call<ApiModels.TransitResponse> c,
                                                     @NonNull Response<ApiModels.TransitResponse> r) {
                        progress.setVisibility(View.GONE);
                        revealPanels();
                        if (r.code() == 503) {
                            // The engine is a separate service. Down is not the same as
                            // "no such trip", so say so and offer the old handoff.
                            info.setText("Transit routing is offline right now.\n"
                                    + "Tap “Maps” to open this trip in Google Maps instead.");
                            return;
                        }
                        if (r.code() == 404) {
                            info.setText("No transit trip found — this may be outside the "
                                    + "service area, or outside service hours.");
                            return;
                        }
                        if (!r.isSuccessful() || r.body() == null
                                || r.body().itineraries == null || r.body().itineraries.isEmpty()) {
                            info.setText("No transit itineraries came back for those points.");
                            return;
                        }
                        showTransitOptions(r.body().itineraries);
                    }
                    @Override public void onFailure(@NonNull Call<ApiModels.TransitResponse> c,
                                                    @NonNull Throwable t) {
                        progress.setVisibility(View.GONE);
                        revealPanels();
                        info.setText("Can't reach server. Is the backend running and BASE_URL correct?");
                    }
                });
    }

    private void showTransitOptions(List<ApiModels.Itinerary> its) {
        transitOptions = its;
        selectedItinerary = 0;
        routeOptions = new ArrayList<>();     // the car-route cards do not apply here
        buildTransitCards();
        optionsScroll.setVisibility(View.VISIBLE);
        drawItinerary(its.get(0));
    }

    private void selectItinerary(int i) {
        if (transitOptions == null || i < 0 || i >= transitOptions.size()) return;
        selectedItinerary = i;
        buildTransitCards();
        drawItinerary(transitOptions.get(i));
    }

    /** Cards read "24 min · 1 transfer · 5:48 → 6:12", with the routes you ride. */
    private void buildTransitCards() {
        optionsRow.removeAllViews();
        if (transitOptions == null || transitOptions.isEmpty()) return;
        float density = getResources().getDisplayMetrics().density;
        int pad = (int) (10 * density);
        for (int i = 0; i < transitOptions.size(); i++) {
            final int idx = i;
            ApiModels.Itinerary it = transitOptions.get(i);
            boolean sel = (i == selectedItinerary);

            LinearLayout card = new LinearLayout(this);
            card.setOrientation(LinearLayout.VERTICAL);
            card.setPadding(pad, pad, pad, pad);
            GradientDrawable bg = new GradientDrawable();
            bg.setCornerRadius(12 * density);
            bg.setColor(Color.parseColor(sel ? "#EEEDFE" : "#FFFFFF"));
            bg.setStroke((int) ((sel ? 2 : 1) * density), Color.parseColor(sel ? "#534AB7" : "#DDDDDD"));
            card.setBackground(bg);
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                    (int) (168 * density), ViewGroup.LayoutParams.WRAP_CONTENT);
            lp.setMargins(0, 0, (int) (6 * density), 0);
            card.setLayoutParams(lp);

            TextView top = new TextView(this);
            top.setText(routeSummary(it));
            top.setTextSize(11);
            top.setTextColor(Color.parseColor("#3C3489"));
            card.addView(top);

            TextView dur = new TextView(this);
            dur.setText(String.format(Locale.US, "%d min", it.durationMin));
            dur.setTextSize(18);
            dur.setTypeface(null, Typeface.BOLD);
            dur.setTextColor(Color.parseColor("#1A1A1A"));
            card.addView(dur);

            TextView sub = new TextView(this);
            String transfers = it.transfers == 0 ? "No transfers"
                    : it.transfers + (it.transfers == 1 ? " transfer" : " transfers");
            // The fare already accounts for transfer rules, so three buses inside
            // the transfer window shows one fare, not three.
            sub.setText(it.fare == null ? transfers
                    : String.format(Locale.US, "%s · $%.2f", transfers, it.fare.amount));
            sub.setTextSize(12);
            sub.setTextColor(Color.parseColor("#666666"));
            card.addView(sub);

            // The clock times are the point of a transit card -- "24 min" is useless
            // if you do not know the bus leaves in two minutes.
            TextView when = new TextView(this);
            when.setText(String.format(Locale.US, "%s → %s", it.departTime, it.arriveTime));
            when.setTextSize(11);
            when.setTextColor(Color.parseColor("#888888"));
            card.addView(when);

            // Only claim punctuality when a live feed actually said so. ETS covers
            // buses far better than the LRT, so plenty of real itineraries carry no
            // realtime at all -- those show no badge rather than "on time", which
            // would be a guess dressed up as a fact.
            if (it.realtime && it.status != null) {
                TextView live = new TextView(this);
                live.setText("● Live · " + it.status);
                live.setTextSize(11);
                live.setTextColor(Color.parseColor(
                        it.status.contains("late") ? "#C62828" : "#2E7D32"));
                card.addView(live);
            }

            card.setOnClickListener(v -> selectItinerary(idx));
            optionsRow.addView(card);
        }
    }

    private String routeSummary(ApiModels.Itinerary it) {
        if (it.routes == null || it.routes.isEmpty()) return "Walk only";
        StringBuilder sb = new StringBuilder();
        for (String r : it.routes) {
            if (sb.length() > 0) sb.append(" → ");
            sb.append(r);
        }
        return sb.toString();
    }

    /** Draw each leg in its own colour: walk legs dashed grey, each ride solid. */
    private void drawItinerary(ApiModels.Itinerary it) {
        clearRouteOverlays();
        if (it.legs == null) return;

        List<GeoPoint> all = new ArrayList<>();
        int rideIndex = 0;
        for (ApiModels.TransitLeg leg : it.legs) {
            if (leg.polylineLatlon == null || leg.polylineLatlon.isEmpty()) continue;
            List<GeoPoint> pts = new ArrayList<>();
            for (List<Double> p : leg.polylineLatlon) pts.add(new GeoPoint(p.get(0), p.get(1)));
            all.addAll(pts);

            Polyline line = new Polyline();
            line.setPoints(pts);
            boolean ride = leg.isRide();
            line.getOutlinePaint().setColor(ride ? legColor(leg, rideIndex) : WALK_LEG);
            line.getOutlinePaint().setStrokeWidth(ride ? 12f : 9f);
            line.getOutlinePaint().setPathEffect(
                    ride ? null : new DashPathEffect(new float[]{18f, 12f}, 0f));
            map.getOverlays().add(line);
            transitLines.add(line);

            if (ride) {
                // Mark where you get on and off -- the two moments you can get wrong.
                addStopDot(pts.get(0), legColor(leg, rideIndex),
                        boardLabel(leg), leg.fromName);
                addStopDot(pts.get(pts.size() - 1), legColor(leg, rideIndex),
                        "Get off " + (leg.arrive != null ? "· " + leg.arrive : ""), leg.toName);
                rideIndex++;
            }
        }

        if (pickupMarker != null) { map.getOverlays().remove(pickupMarker); map.getOverlays().add(pickupMarker); }
        if (dropoffMarker != null) { map.getOverlays().remove(dropoffMarker); map.getOverlays().add(dropoffMarker); }

        // There are no turn-by-turn maneuvers on a transit plan, so navSteps stays
        // empty - but "Start trip" is now enabled, because riding guidance is a
        // different job from turn announcements: it tracks which stop you are at
        // and tells you when to get off.
        navSteps = new ArrayList<>();
        startBtn.setEnabled(true);

        StringBuilder head = new StringBuilder(String.format(Locale.US,
                "%s\n%d min  ·  depart %s, arrive %s  ·  %s",
                routeSummary(it), it.durationMin, it.departTime, it.arriveTime,
                it.transfers == 0 ? "no transfers"
                        : it.transfers + (it.transfers == 1 ? " transfer" : " transfers")));
        if (it.fare != null)
            head.append(String.format(Locale.US, "  ·  $%.2f %s", it.fare.amount, it.fare.medium));
        if (it.realtime && it.status != null) head.append("  ·  ● live, ").append(it.status);
        // Surface at most one alert here; the rest are on the Steps screen. A wall
        // of detour notices on the map panel buries the plan itself.
        if (it.alerts != null && !it.alerts.isEmpty())
            head.append("\n⚠ ").append(it.alerts.get(0).header);
        head.append("\nTap “Steps” for the leg-by-leg plan.");
        info.setText(head.toString());

        if (!all.isEmpty())
            map.post(() -> map.zoomToBoundingBox(BoundingBox.fromGeoPoints(all), true, 90));
        map.invalidate();
    }

    private String boardLabel(ApiModels.TransitLeg leg) {
        String what = (leg.modeLabel != null ? leg.modeLabel : leg.mode)
                + (leg.route != null && !leg.route.isEmpty() ? " " + leg.route : "");
        String head = (leg.headsign != null && !leg.headsign.isEmpty()) ? " toward " + leg.headsign : "";
        return "Board " + what + head + (leg.depart != null ? " · " + leg.depart : "");
    }

    /** An agency colour when there is one, otherwise a stable colour per ride leg. */
    private int legColor(ApiModels.TransitLeg leg, int rideIndex) {
        if (leg.color != null && leg.color.length() >= 4) {
            try { return Color.parseColor(leg.color); } catch (IllegalArgumentException ignored) { }
        }
        return Color.parseColor(LEG_COLORS[rideIndex % LEG_COLORS.length]);
    }

    private void addStopDot(GeoPoint p, int color, String title, String snippet) {
        Marker m = new Marker(map);
        m.setPosition(p);
        m.setAnchor(Marker.ANCHOR_CENTER, Marker.ANCHOR_CENTER);
        m.setIcon(new BitmapDrawable(getResources(), stopDot(color)));
        m.setTitle(title);
        m.setSubDescription(snippet != null ? snippet : "");
        map.getOverlays().add(m);
        transitStops.add(m);
    }

    /** A white-ringed dot, so a boarding point reads clearly against any leg colour. */
    private Bitmap stopDot(int color) {
        float d = getResources().getDisplayMetrics().density;
        int size = (int) (16 * d);
        Bitmap bmp = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888);
        Canvas cv = new Canvas(bmp);
        Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
        paint.setColor(Color.WHITE);
        cv.drawCircle(size / 2f, size / 2f, size / 2f, paint);
        paint.setColor(color);
        cv.drawCircle(size / 2f, size / 2f, size / 2f - 3 * d, paint);
        return bmp;
    }

    /** Remove whatever the last route drew, car or transit, before drawing the next. */
    private void clearRouteOverlays() {
        if (routeLine != null) { map.getOverlays().remove(routeLine); routeLine = null; }
        removeConnectors();
        clearTransitOverlays();
    }

    private void clearTransitOverlays() {
        for (Polyline l : transitLines) map.getOverlays().remove(l);
        transitLines.clear();
        for (Marker m : transitStops) map.getOverlays().remove(m);
        transitStops.clear();
    }

    private void drawRoute(ApiModels.RouteResponse b, String label) {
        if (b.polylineLatlon == null || b.polylineLatlon.isEmpty()) {
            info.setText("No route geometry returned."); return;
        }
        transitOptions = new ArrayList<>();
        clearTransitOverlays();
        routePts = new ArrayList<>();
        for (List<Double> p : b.polylineLatlon) routePts.add(new GeoPoint(p.get(0), p.get(1)));

        if (routeLine != null) map.getOverlays().remove(routeLine);
        boolean walkRoute = "walk".equals(b.mode);
        routeLine = new Polyline();
        routeLine.setPoints(routePts);
        // Driving = solid blue; walking = dashed green (Google-Maps convention).
        routeLine.getOutlinePaint().setColor(Color.parseColor(walkRoute ? "#2E7D32" : "#1E6FEB"));
        routeLine.getOutlinePaint().setStrokeWidth(walkRoute ? 10f : 12f);
        routeLine.getOutlinePaint().setPathEffect(
                walkRoute ? new DashPathEffect(new float[]{20f, 14f}, 0f) : null);
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

    // ───────────────────────── route options (alternatives) ─────────────────────────
    private void showRouteOptions(List<ApiModels.RouteResponse> opts, String label) {
        routeOptions = opts;
        selectedOption = 0;
        lastRouteLabel = label;
        buildOptionCards();
        optionsScroll.setVisibility(opts.size() > 1 ? View.VISIBLE : View.GONE);
        drawRoute(opts.get(0), label);
    }

    private void selectOption(int i) {
        if (routeOptions == null || i < 0 || i >= routeOptions.size()) return;
        selectedOption = i;
        buildOptionCards();
        drawRoute(routeOptions.get(i), lastRouteLabel);
    }

    private void buildOptionCards() {
        optionsRow.removeAllViews();
        if (routeOptions == null || routeOptions.isEmpty()) return;
        float density = getResources().getDisplayMetrics().density;
        double baseEta = routeOptions.get(0).etaMin;
        int pad = (int) (10 * density);
        for (int i = 0; i < routeOptions.size(); i++) {
            final int idx = i;
            ApiModels.RouteResponse o = routeOptions.get(i);
            boolean sel = (i == selectedOption);
            boolean walk = "walk".equals(o.mode);

            LinearLayout card = new LinearLayout(this);
            card.setOrientation(LinearLayout.VERTICAL);
            card.setPadding(pad, pad, pad, pad);
            GradientDrawable bg = new GradientDrawable();
            bg.setCornerRadius(12 * density);
            bg.setColor(Color.parseColor(sel ? "#EEEDFE" : "#FFFFFF"));
            bg.setStroke((int) ((sel ? 2 : 1) * density), Color.parseColor(sel ? "#534AB7" : "#DDDDDD"));
            card.setBackground(bg);
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                    (int) (152 * density), ViewGroup.LayoutParams.WRAP_CONTENT);
            lp.setMargins(0, 0, (int) (6 * density), 0);
            card.setLayoutParams(lp);

            TextView top = new TextView(this);
            if (i == 0) { top.setText("Recommended"); top.setTextColor(Color.parseColor("#3C3489")); }
            else {
                int delta = (int) Math.round(o.etaMin - baseEta);
                top.setText(delta > 0 ? "+" + delta + " min" : "Alternative");
                top.setTextColor(Color.parseColor("#888888"));
            }
            top.setTextSize(11);
            card.addView(top);

            TextView eta = new TextView(this);
            eta.setText(String.format(Locale.US, "%.0f min", o.etaMin));
            eta.setTextSize(18);
            eta.setTypeface(null, Typeface.BOLD);
            eta.setTextColor(Color.parseColor("#1A1A1A"));
            card.addView(eta);

            TextView sub = new TextView(this);
            String fare = walk ? "walk" : String.format(Locale.US, "$%.2f", o.fareUsd);
            sub.setText(String.format(Locale.US, "%.1f km · %s", o.distanceKm, fare));
            sub.setTextSize(12);
            sub.setTextColor(Color.parseColor("#666666"));
            card.addView(sub);

            TextView arr = new TextView(this);
            arr.setText("Arrive " + arrivalTime(o.etaMin));
            arr.setTextSize(11);
            arr.setTextColor(Color.parseColor("#888888"));
            card.addView(arr);

            card.setOnClickListener(v -> selectOption(idx));
            optionsRow.addView(card);
        }
    }

    private String arrivalTime(double etaMin) {
        long ms = System.currentTimeMillis() + (long) (etaMin * 60000);
        return new java.text.SimpleDateFormat("h:mm a", Locale.US).format(new java.util.Date(ms));
    }

    // ───────────────────────── navigation ─────────────────────────
    private final LocationListener navListener = new LocationListener() {
        @Override public void onLocationChanged(@NonNull Location loc) { onNavLocation(loc); }
        @Override public void onStatusChanged(String provider, int status, Bundle extras) { }
        @Override public void onProviderEnabled(@NonNull String provider) { }
        @Override public void onProviderDisabled(@NonNull String provider) { }
    };

    /**
     * Hand the selected itinerary to the foreground service and let it ride along.
     *
     * Guidance has to survive the screen going off and the app going to the
     * background - that is the whole point - so the work happens in a service, not
     * here.
     */
    private void startTransitRide() {
        if (transitOptions == null || transitOptions.isEmpty()) {
            Toast.makeText(this, "Plan a transit trip first.", Toast.LENGTH_SHORT).show();
            return;
        }
        if (!hasLocationPermission()) { onMyLocationTapped(); return; }
        // Android 13+ will silently drop every notification without this, which
        // would leave the service running and completely mute.
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.TIRAMISU
                && ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                   != PackageManager.PERMISSION_GRANTED) {
            ActivityCompat.requestPermissions(this,
                    new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_NOTIFICATIONS);
            return;
        }

        ApiModels.Itinerary it = transitOptions.get(
                Math.min(selectedItinerary, transitOptions.size() - 1));
        TransitRideService.ITINERARY = it;
        ContextCompat.startForegroundService(this,
                new android.content.Intent(this, TransitRideService.class));

        startBtn.setText(R.string.stop_trip);
        navBanner.setVisibility(View.VISIBLE);
        navBanner.setText("Riding " + routeSummary(it) + " - watch for the notification");
        Toast.makeText(this, "Ride guidance on. You can lock the screen.",
                Toast.LENGTH_LONG).show();
    }

    private void startTrip() {
        if ("transit".equals(travelMode)) { startTransitRide(); return; }
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
        optionsScroll.setVisibility(View.GONE);   // hide the option cards while navigating
        navBanner.setText("Starting navigation…");
        myLocation.enableMyLocation();
        map.getController().setZoom(18.0);
        speak("Starting navigation.");
        startLocationUpdates();
    }

    private void stopTrip() {
        if (TransitRideService.RUNNING) {
            stopService(new android.content.Intent(this, TransitRideService.class));
            startBtn.setText(R.string.start_trip);
            navBanner.setVisibility(View.GONE);
            return;
        }
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
        suppressAutocomplete = true;   // don't let the programmatic setText trigger a search
        if ("Pickup".equals(label) && startField != null) startField.setText(addr);
        else if ("Drop-off".equals(label) && destField != null) destField.setText(addr);
        suppressAutocomplete = false;
    }

    // ───────────────────────── address autocomplete ─────────────────────────
    private void attachAutocomplete(final EditText field) {
        field.addTextChangedListener(new TextWatcher() {
            @Override public void beforeTextChanged(CharSequence s, int a, int b, int c) { }
            @Override public void onTextChanged(CharSequence s, int a, int b, int c) { }
            @Override public void afterTextChanged(Editable e) {
                if (suppressAutocomplete) return;
                activeField = field;
                String q = e.toString().trim();
                if (searchRunnable != null) searchHandler.removeCallbacks(searchRunnable);
                if (q.length() < 3) { hideSuggestions(); return; }
                searchRunnable = () -> runSearch(q);
                searchHandler.postDelayed(searchRunnable, 300);   // debounce keystrokes
            }
        });
    }

    private void runSearch(final String q) {
        ApiClient.get().search(q).enqueue(new Callback<ApiModels.SearchResponse>() {
            @Override public void onResponse(@NonNull Call<ApiModels.SearchResponse> c,
                                             @NonNull Response<ApiModels.SearchResponse> r) {
                if (!r.isSuccessful() || r.body() == null || r.body().results == null) { hideSuggestions(); return; }
                suggestions.clear();
                List<String> labels = new ArrayList<>();
                for (ApiModels.ReverseGeocodeResponse res : r.body().results) {
                    suggestions.add(res);
                    labels.add(res.shortLabel != null && !res.shortLabel.isEmpty() ? res.shortLabel : res.displayName);
                }
                if (suggestions.isEmpty()) { hideSuggestions(); return; }
                suggestAdapter.clear();
                suggestAdapter.addAll(labels);
                suggestAdapter.notifyDataSetChanged();
                suggestionsList.setVisibility(View.VISIBLE);
            }
            @Override public void onFailure(@NonNull Call<ApiModels.SearchResponse> c, @NonNull Throwable t) {
                hideSuggestions();
            }
        });
    }

    private void onSuggestionPicked(int position) {
        if (position < 0 || position >= suggestions.size()) return;
        ApiModels.ReverseGeocodeResponse res = suggestions.get(position);
        String label = (res.shortLabel != null && !res.shortLabel.isEmpty()) ? res.shortLabel : res.displayName;
        GeoPoint gp = new GeoPoint(res.lat, res.lon);
        boolean isStart = (activeField == startField);
        suppressAutocomplete = true;
        (isStart ? startField : destField).setText(label);
        suppressAutocomplete = false;
        hideSuggestions();
        hideKeyboard();
        if (isStart) setPickup(gp, label); else setDropoff(gp, label);
        if (pickupMarker == null || dropoffMarker == null) {
            map.getController().animateTo(gp);
            map.getController().setZoom(15.0);
        }
    }

    private void hideSuggestions() {
        if (suggestionsList != null) suggestionsList.setVisibility(View.GONE);
    }

    // ───────────────────────── directions list / street view / transit ─────────────────────────
    private void openDirections() {
        // A transit plan has no maneuvers to turn at -- the backend hands us the
        // leg-by-leg lines ready to read, so show those instead of turn steps.
        if ("transit".equals(travelMode) && transitOptions != null && !transitOptions.isEmpty()) {
            ApiModels.Itinerary it = transitOptions.get(
                    Math.min(selectedItinerary, transitOptions.size() - 1));
            DirectionsActivity.STEPS = null;
            DirectionsActivity.LINES = it.instructions;
            DirectionsActivity.ALERTS = it.alerts;
            DirectionsActivity.SUMMARY = String.format(Locale.US,
                    "%s\n%d min · depart %s, arrive %s · %.0f m walking%s",
                    routeSummary(it), it.durationMin, it.departTime, it.arriveTime,
                    it.walkDistanceM,
                    it.fare == null ? "" : "  ·  " + it.fare.text);
            startActivity(new android.content.Intent(this, DirectionsActivity.class));
            return;
        }
        if (navSteps == null || navSteps.isEmpty()) {
            Toast.makeText(this, "Set a destination and route first.", Toast.LENGTH_SHORT).show();
            return;
        }
        DirectionsActivity.LINES = null;
        DirectionsActivity.ALERTS = null;
        DirectionsActivity.STEPS = navSteps;
        String summary = "Turn-by-turn directions";
        if (routeOptions != null && selectedOption < routeOptions.size()) {
            ApiModels.RouteResponse o = routeOptions.get(selectedOption);
            boolean walk = "walk".equals(o.mode);
            summary = String.format(Locale.US, "%.1f km · %.0f min%s", o.distanceKm, o.etaMin,
                    walk ? " walk" : String.format(Locale.US, "  ·  $%.2f", o.fareUsd));
        }
        DirectionsActivity.SUMMARY = summary;
        startActivity(new android.content.Intent(this, DirectionsActivity.class));
    }

    /** Opens Google Street View at the drop-off via a web link (no API key needed to open a URL). */
    private void openStreetView() {
        GeoPoint p = (dropoffMarker != null) ? dropoffMarker.getPosition()
                : (pickupMarker != null ? pickupMarker.getPosition() : null);
        if (p == null) { Toast.makeText(this, "Set a point on the map first.", Toast.LENGTH_SHORT).show(); return; }
        openUrl("https://www.google.com/maps/@?api=1&map_action=pano&viewpoint="
                + p.getLatitude() + "," + p.getLongitude());
    }

    /** Opens Google Maps transit directions for pickup -> drop-off via a web link. */
    private void openTransit() {
        if (pickupMarker == null || dropoffMarker == null) {
            Toast.makeText(this, "Set both a pickup and a destination first.", Toast.LENGTH_SHORT).show();
            return;
        }
        GeoPoint a = pickupMarker.getPosition(), b = dropoffMarker.getPosition();
        openUrl("https://www.google.com/maps/dir/?api=1&travelmode=transit"
                + "&origin=" + a.getLatitude() + "," + a.getLongitude()
                + "&destination=" + b.getLatitude() + "," + b.getLongitude());
    }

    private void openUrl(String url) {
        try {
            startActivity(new android.content.Intent(android.content.Intent.ACTION_VIEW,
                    android.net.Uri.parse(url)));
        } catch (Exception e) {
            Toast.makeText(this, "No app available to open the link.", Toast.LENGTH_SHORT).show();
        }
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
        clearRouteOverlays();
        if (dropoffMarker != null) { map.getOverlays().remove(dropoffMarker); dropoffMarker = null; }
        navSteps = new ArrayList<>();
        routeOptions = new ArrayList<>();
        transitOptions = new ArrayList<>();
        optionsScroll.setVisibility(View.GONE);
        startBtn.setEnabled(false);
        dropoffText.setText(R.string.dropoff_hint);
        info.setText("Drop-off cleared. Tap the map to set a new one.");
        map.invalidate();
    }
}
