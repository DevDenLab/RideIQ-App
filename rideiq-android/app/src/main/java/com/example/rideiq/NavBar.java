package com.example.rideiq;

import android.content.Intent;

import androidx.appcompat.app.AppCompatActivity;

import com.google.android.material.bottomnavigation.BottomNavigationView;

/**
 * Shared bottom-navigation wiring for the three top-level screens
 * (Map = home, Insights = ML analytics, Profile = settings).
 *
 * Uses REORDER_TO_FRONT so tapping a tab brings the existing screen forward
 * (preserving its state) instead of stacking new copies.
 */
public final class NavBar {
    private NavBar() { }

    public static void setup(final AppCompatActivity activity, BottomNavigationView nav, int selectedId) {
        if (nav == null) return;
        nav.setSelectedItemId(selectedId);                 // set BEFORE the listener so it doesn't fire
        nav.setOnItemSelectedListener(item -> {
            int id = item.getItemId();
            if (id == selectedId) return true;             // already here
            Intent i = null;
            if (id == R.id.nav_map) i = new Intent(activity, RouteMapActivity.class);
            else if (id == R.id.nav_insights) i = new Intent(activity, MainActivity.class);
            else if (id == R.id.nav_profile) i = new Intent(activity, ProfileActivity.class);
            if (i != null) {
                i.addFlags(Intent.FLAG_ACTIVITY_REORDER_TO_FRONT | Intent.FLAG_ACTIVITY_SINGLE_TOP);
                activity.startActivity(i);
                activity.overridePendingTransition(0, 0);
                return true;
            }
            return false;
        });
    }
}
