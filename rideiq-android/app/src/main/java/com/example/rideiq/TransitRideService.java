package com.example.rideiq;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.os.Build;
import android.os.Bundle;
import android.os.IBinder;
import android.speech.tts.TextToSpeech;

import androidx.annotation.NonNull;
import androidx.core.app.NotificationCompat;
import androidx.core.content.ContextCompat;

import java.util.HashSet;
import java.util.Locale;
import java.util.Set;

/**
 * Rides along with you and says when to get off.
 *
 * This is a foreground service rather than work inside the activity, because the
 * entire point is that it keeps going with the phone in a pocket and the screen
 * off. An activity-scoped location listener is stopped by the system within
 * minutes of the app going to the background, which is exactly when a rider needs
 * "get off at the next stop".
 *
 * It shows one ongoing notification that it rewrites as you travel - next stop,
 * stops remaining, where to get off - and escalates to a heads-up notification
 * plus a spoken line at the two moments that matter: time to board, and time to
 * stand up.
 *
 * The itinerary is handed over through a static field rather than serialised into
 * the Intent. That mirrors how DirectionsActivity already receives its data, and
 * avoids making the whole model graph Parcelable for a single in-process handoff.
 */
public class TransitRideService extends Service {

    /** Set by RouteMapActivity immediately before startForegroundService(). */
    public static ApiModels.Itinerary ITINERARY;
    /** True while the service is running, so the activity can show the right button. */
    public static boolean RUNNING = false;

    public static final String ACTION_STOP = "com.example.rideiq.STOP_RIDE";

    private static final String CHANNEL_ID = "transit_ride";
    private static final int NOTIFICATION_ID = 42;

    private LocationManager lm;
    private TextToSpeech tts;
    private boolean ttsReady = false;
    private ApiModels.Itinerary itinerary;
    private int furthestLeg = 0;
    private final Set<String> announced = new HashSet<>();

    // Live vehicle tracking. Positions are refreshed on a slower cadence than GPS
    // fixes -- OTP itself only polls the feed every 30 s, so asking faster buys
    // nothing and costs battery and quota.
    private static final long VEHICLE_REFRESH_MS = 30_000L;
    private java.util.List<ApiModels.Vehicle> vehicles = null;
    private String vehiclePattern = null;
    private long vehiclesFetchedAt = 0;
    // One stray fix should never accuse someone of being on the wrong bus.
    private int offVehicleStreak = 0;
    private static final int OFF_VEHICLE_STRIKES = 3;

    private final LocationListener listener = new LocationListener() {
        @Override public void onLocationChanged(@NonNull Location loc) { onFix(loc); }
        @Override public void onStatusChanged(String p, int s, Bundle e) { }
        @Override public void onProviderEnabled(@NonNull String p) { }
        @Override public void onProviderDisabled(@NonNull String p) { }
    };

    @Override public IBinder onBind(Intent intent) { return null; }

    @Override
    public void onCreate() {
        super.onCreate();
        createChannel();
        lm = (LocationManager) getSystemService(Context.LOCATION_SERVICE);
        tts = new TextToSpeech(this, status -> {
            if (status == TextToSpeech.SUCCESS) {
                tts.setLanguage(Locale.US);
                tts.setSpeechRate(1.1f);
                ttsReady = true;
            }
        });
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            stopSelf();
            return START_NOT_STICKY;
        }
        itinerary = ITINERARY;
        RUNNING = true;

        String route = "your trip";
        if (itinerary != null && itinerary.routes != null && !itinerary.routes.isEmpty()) {
            route = android.text.TextUtils.join(" -> ", itinerary.routes);
        }
        startForeground(NOTIFICATION_ID,
                build("Riding " + route, "Waiting for a GPS fix...", false));

        requestUpdates();
        // START_NOT_STICKY: if the system kills this, silently resurrecting it
        // without an itinerary would leave a permanent dead notification.
        return START_NOT_STICKY;
    }

    private void requestUpdates() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
                != PackageManager.PERMISSION_GRANTED) {
            update("Location permission needed", "Grant location access to track the ride", false);
            return;
        }
        try {
            // 5 s / 20 m: a bus stop spacing is hundreds of metres, so this is
            // ample, and far kinder to the battery than the 1 Hz driving loop.
            lm.requestLocationUpdates(LocationManager.GPS_PROVIDER, 5000, 20, listener);
        } catch (Exception ignored) { }
        try {
            lm.requestLocationUpdates(LocationManager.NETWORK_PROVIDER, 5000, 20, listener);
        } catch (Exception ignored) { }
    }

    private void onFix(Location loc) {
        if (itinerary == null) return;
        TransitProgress.State st = TransitProgress.evaluate(
                itinerary, loc.getLatitude(), loc.getLongitude(), furthestLeg);
        if (st.legIndex > furthestLeg) furthestLeg = st.legIndex;

        checkVehicle(st, loc);

        boolean urgent = st.alert != null && !announced.contains(st.alertKey);
        if (urgent) {
            announced.add(st.alertKey);
            speak(st.alert);
        }
        update(st.headline, st.detail, urgent);

        if (st.arrived) {
            speak("You have arrived. Enjoy your trip.");
            update("Arrived", "Get off at " + st.alightStop.name, true);
            stopSelf();
        }
    }

    /**
     * Compare the rider against the live positions of the route they think they
     * are on, and say something if they are nowhere near any of them.
     *
     * Deliberately conservative. Absent vehicle data is not evidence of anything,
     * and one bad GPS fix is not either -- it takes three consecutive readings
     * beyond the threshold before this speaks. The cost of a false alarm here is
     * that the rider stops trusting the app entirely.
     */
    private void checkVehicle(TransitProgress.State st, Location loc) {
        if (st.leg == null || !st.onBoard || st.leg.patternCode == null) {
            offVehicleStreak = 0;
            return;
        }
        long now = System.currentTimeMillis();
        boolean stale = now - vehiclesFetchedAt > VEHICLE_REFRESH_MS;
        if (!st.leg.patternCode.equals(vehiclePattern) || stale) {
            vehiclePattern = st.leg.patternCode;
            vehiclesFetchedAt = now;
            ApiClient.get().transitVehicles(st.leg.patternCode).enqueue(
                    new retrofit2.Callback<ApiModels.VehiclesResponse>() {
                        @Override public void onResponse(
                                retrofit2.Call<ApiModels.VehiclesResponse> c,
                                retrofit2.Response<ApiModels.VehiclesResponse> r) {
                            if (r.isSuccessful() && r.body() != null) {
                                vehicles = r.body().vehicles;
                            }
                        }
                        @Override public void onFailure(
                                retrofit2.Call<ApiModels.VehiclesResponse> c, Throwable t) {
                            // Losing the feed must not produce a warning: it is
                            // missing evidence, not evidence of a missed bus.
                            vehicles = null;
                        }
                    });
            return;                       // judge on the next fix, once it lands
        }

        if (TransitProgress.looksLikeWrongVehicle(vehicles, loc.getLatitude(),
                                                  loc.getLongitude())) {
            offVehicleStreak++;
        } else {
            offVehicleStreak = 0;
        }

        if (offVehicleStreak == OFF_VEHICLE_STRIKES) {
            String route = st.leg.route == null ? "this route" : st.leg.route;
            speak("You may not be on the " + route + ". Check the vehicle.");
            update("Are you on the right vehicle?",
                    "No " + route + " is reporting near you. If you boarded a "
                    + "different bus, replan from where you are.", true);
        }
    }

    private void speak(String text) {
        if (ttsReady && text != null) {
            tts.speak(text, TextToSpeech.QUEUE_ADD, null, "ride");
        }
    }

    private void update(String title, String body, boolean urgent) {
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (nm != null) nm.notify(NOTIFICATION_ID, build(title, body, urgent));
    }

    private Notification build(String title, String body, boolean urgent) {
        Intent open = new Intent(this, RouteMapActivity.class)
                .setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent openPi = PendingIntent.getActivity(this, 0, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        Intent stop = new Intent(this, TransitRideService.class).setAction(ACTION_STOP);
        PendingIntent stopPi = PendingIntent.getService(this, 1, stop,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        return new NotificationCompat.Builder(this, CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_nav_map)
                .setContentTitle(title)
                .setContentText(body)
                .setStyle(new NotificationCompat.BigTextStyle().bigText(body))
                .setContentIntent(openPi)
                .addAction(0, "End ride", stopPi)
                .setOngoing(true)
                .setOnlyAlertOnce(!urgent)
                .setPriority(urgent ? NotificationCompat.PRIORITY_HIGH
                                    : NotificationCompat.PRIORITY_LOW)
                .setCategory(NotificationCompat.CATEGORY_NAVIGATION)
                .build();
    }

    private void createChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (nm == null || nm.getNotificationChannel(CHANNEL_ID) != null) return;
        // IMPORTANCE_HIGH so "get off at the next stop" can surface over whatever
        // the rider is actually looking at. setOnlyAlertOnce keeps the routine
        // per-stop updates quiet; only the real alerts make a sound.
        NotificationChannel ch = new NotificationChannel(CHANNEL_ID, "Transit ride guidance",
                NotificationManager.IMPORTANCE_HIGH);
        ch.setDescription("Next stop and when to get off, while you are riding");
        nm.createNotificationChannel(ch);
    }

    @Override
    public void onDestroy() {
        RUNNING = false;
        if (lm != null) lm.removeUpdates(listener);
        if (tts != null) { tts.stop(); tts.shutdown(); }
        super.onDestroy();
    }
}
