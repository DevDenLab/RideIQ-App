package com.example.rideiq;

import android.content.Context;
import android.content.SharedPreferences;

import com.google.gson.Gson;
import com.google.gson.JsonSyntaxException;

/**
 * Keeps the last transit plan on disk, so losing signal does not lose the trip.
 *
 * This matters most in exactly the place a transit app is used: underground on
 * the LRT, in a dead zone, on a bus with no data. Before this, reopening the app
 * without a connection showed nothing at all -- the plan you were literally in
 * the middle of following simply vanished.
 *
 * Only the transit plan is stored. A driving route is cheap to recompute and
 * rarely something you are mid-way through following; a transit itinerary is the
 * thing you actually need to consult again at the moment you cannot reach the
 * network.
 */
public final class PlanStore {

    private static final String PREFS = "rideiq_plan";
    private static final String KEY_PLAN = "last_transit_plan";
    private static final String KEY_SAVED_AT = "saved_at";
    private static final String KEY_FROM = "from_label";
    private static final String KEY_TO = "to_label";

    /**
     * How long a saved plan is worth showing. Transit plans are timetable-bound,
     * so one from yesterday is not stale information, it is wrong information --
     * and offering it as a fallback would be worse than offering nothing.
     */
    private static final long MAX_AGE_MS = 6 * 60 * 60 * 1000L;   // 6 hours

    /**
     * Tighter window for restoring a plan UNPROMPTED on launch. Six hours is a
     * reasonable thing to fall back to when a request fails; it is far too long to
     * assume someone is still on the trip. Two hours says "you are probably still
     * travelling" without hijacking tomorrow morning's app launch.
     */
    private static final long AUTO_RESTORE_MS = 2 * 60 * 60 * 1000L;

    private PlanStore() { }

    private static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    public static void save(Context c, ApiModels.Itinerary it, String from, String to) {
        if (it == null) return;
        prefs(c).edit()
                .putString(KEY_PLAN, new Gson().toJson(it))
                .putLong(KEY_SAVED_AT, System.currentTimeMillis())
                .putString(KEY_FROM, from == null ? "" : from)
                .putString(KEY_TO, to == null ? "" : to)
                .apply();
    }

    /** The last plan, or null if there is none or it is too old to trust. */
    public static ApiModels.Itinerary load(Context c) {
        SharedPreferences p = prefs(c);
        String json = p.getString(KEY_PLAN, null);
        if (json == null) return null;
        if (System.currentTimeMillis() - p.getLong(KEY_SAVED_AT, 0) > MAX_AGE_MS) return null;
        try {
            return new Gson().fromJson(json, ApiModels.Itinerary.class);
        } catch (JsonSyntaxException e) {
            // A model change can make an old blob unreadable. Losing a cached plan
            // is a shrug; crashing on launch because of one is not.
            return null;
        }
    }

    /** "Downtown to West Edmonton Mall, saved 14 min ago", for the banner. */
    public static String describe(Context c) {
        SharedPreferences p = prefs(c);
        long age = System.currentTimeMillis() - p.getLong(KEY_SAVED_AT, 0);
        long mins = Math.max(0, age / 60000);
        String when = mins < 1 ? "just now"
                : mins < 60 ? mins + " min ago"
                : (mins / 60) + " h ago";
        String from = p.getString(KEY_FROM, "");
        String to = p.getString(KEY_TO, "");
        if (from.isEmpty() && to.isEmpty()) return "Saved plan, " + when;
        return from + " to " + to + ", saved " + when;
    }

    /** Recent enough that the user is probably still on this trip. */
    public static boolean isFreshEnoughToRestore(Context c) {
        long saved = prefs(c).getLong(KEY_SAVED_AT, 0);
        return saved > 0 && System.currentTimeMillis() - saved < AUTO_RESTORE_MS;
    }

    public static void clear(Context c) {
        prefs(c).edit().clear().apply();
    }
}
