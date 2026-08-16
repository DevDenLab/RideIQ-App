package com.example.rideiq;

import android.os.Bundle;

import androidx.appcompat.app.AppCompatActivity;

import com.google.android.material.bottomnavigation.BottomNavigationView;

/** Profile / settings screen. Part of the bottom-navigation trio (Map, Insights, Profile). */
public class ProfileActivity extends AppCompatActivity {
    @Override
    protected void onCreate(Bundle s) {
        super.onCreate(s);
        setContentView(R.layout.activity_profile);
        setTitle("Profile");
        BottomNavigationView nav = findViewById(R.id.bottomNav);
        NavBar.setup(this, nav, R.id.nav_profile);
    }
}
