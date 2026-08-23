package com.example.rideiq;

import android.os.Bundle;
import android.widget.ArrayAdapter;
import android.widget.ListView;
import android.widget.TextView;

import androidx.appcompat.app.AppCompatActivity;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * Full turn-by-turn directions list for the selected route (Google-Maps-style).
 * The steps + summary are handed over via static fields to avoid serializing the models.
 */
public class DirectionsActivity extends AppCompatActivity {

    public static List<ApiModels.Step> STEPS;   // set by RouteMapActivity before launch
    public static String SUMMARY;
    /**
     * Pre-written lines, used for transit. A transit plan has no maneuvers to turn
     * at -- it is "walk here, board the 8 at 5:48, get off at Coliseum" -- so the
     * backend composes the text and this screen just lists it.
     */
    public static List<String> LINES;
    /** Live service alerts for the shown itinerary (detours, closures). May be null. */
    public static List<ApiModels.TransitAlert> ALERTS;

    @Override
    protected void onCreate(Bundle s) {
        super.onCreate(s);
        setContentView(R.layout.activity_directions);
        setTitle("Directions");

        TextView summary = findViewById(R.id.summary);
        ListView list = findViewById(R.id.stepsList);

        summary.setText(SUMMARY != null ? SUMMARY : "");

        List<String> lines = new ArrayList<>();
        if (LINES != null && !LINES.isEmpty()) {
            setTitle("Transit plan");
            int last = LINES.size() - 1;
            for (int i = 0; i < LINES.size(); i++) {
                // The backend always ends the list with the arrival line.
                lines.add(i == last ? "🏁  " + LINES.get(i) : (i + 1) + ".  " + LINES.get(i));
            }
        } else if (STEPS != null) {
            int n = 1;
            for (ApiModels.Step st : STEPS) {
                String dist = st.distFromPrevM >= 1000
                        ? String.format(Locale.US, "%.1f km", st.distFromPrevM / 1000.0)
                        : Math.round(st.distFromPrevM) + " m";
                if ("arrive".equals(st.maneuver)) {
                    lines.add("🏁  " + st.instruction);          // 🏁 arrival
                } else {
                    lines.add(n++ + ".  " + st.instruction + "   (" + dist + ")");
                }
            }
        }
        // Alerts go last, after the plan. They are context for a trip you have
        // already decided on, not a reason to hide the steps behind a warning.
        if (ALERTS != null && !ALERTS.isEmpty()) {
            lines.add("—— Service alerts ——");
            for (ApiModels.TransitAlert a : ALERTS) {
                String body = (a.description != null && !a.description.isEmpty()
                        && !a.description.equals(a.header)) ? "\n" + a.description : "";
                lines.add("⚠  " + a.header + body);
            }
        }

        if (lines.isEmpty()) lines.add("No steps available. Set a destination and route first.");

        list.setAdapter(new ArrayAdapter<>(this, android.R.layout.simple_list_item_1, lines));
    }
}
