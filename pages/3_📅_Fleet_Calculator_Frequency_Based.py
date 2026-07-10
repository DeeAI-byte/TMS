import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import sys
import os
import re

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.data_loader import (
    build_master_table, load_assumptions, load_vehicle_database,
    cases_per_truck, best_truck_for_tonnage_limit, get_month_options,
    fleet_totals_by_ownership, simulate_daily_allocation, compute_frequency_daily_schedule
)

st.set_page_config(page_title="Fleet Calculator | Frequency Based", page_icon="📅", layout="wide")
st.markdown("<style>.block-container { padding-top: 1rem; padding-bottom: 0.5rem; }</style>", unsafe_allow_html=True)

st.title("📅 Monthly Fleet Calculator — Frequency Based")
st.caption("Delivery frequency is derived from each distributor's monthly volume bracket "
           "(per your Assumptions sheet), then vehicles needed are computed per delivery cycle.")
st.write("---")

try:
    master = build_master_table()
    veh_block, freq_block = load_assumptions()
    veh_db = load_vehicle_database()
except Exception as e:
    st.error(f"⚠️ Could not load data: {e}")
    st.stop()

MONTHS = get_month_options()
fleet_tonnages = sorted(veh_db["CapacityTonnage"].dropna().unique())
own_total_default, fixed_total_default = fleet_totals_by_ownership(veh_db)

# ---------------- FREQUENCY BRACKET PARSING ----------------
DEFAULT_FREQ_MAP = {
    "Twice / Week": 2,
    "Thrice / Week": 3,
    "Daily": 6,
}

def parse_bracket(label):
    """Turn '< 40,000' / '40,000 – 100,000' / '> 100,000' into (low, high)."""
    label = str(label).replace(",", "").strip()
    nums = [float(n) for n in re.findall(r"\d+\.?\d*", label)]
    if label.startswith("<"):
        return 0, nums[0]
    if label.startswith(">"):
        return nums[0], np.inf
    if len(nums) == 2:
        return nums[0], nums[1]
    return 0, np.inf

freq_block = freq_block.copy()
freq_block["Low"], freq_block["High"] = zip(*freq_block["MTD Volume"].apply(parse_bracket))
freq_block["Trips/Week"] = freq_block["Frequency"].map(DEFAULT_FREQ_MAP).fillna(6)

# ---------------- SIDEBAR CONFIG ----------------
st.sidebar.header("⚙️ Calculator Settings")
sel_month = st.sidebar.selectbox("Target Month", MONTHS, index=MONTHS.index("JULY"))
working_days_per_week = st.sidebar.number_input("Working days per week", min_value=1, max_value=7, value=6)
weeks_in_month = st.sidebar.number_input("Weeks in month", min_value=1.0, max_value=6.0, value=4.33, step=0.01)

st.sidebar.write("---")
st.sidebar.write("**MTD Volume ➜ Frequency brackets** (editable)")
edited_freq_block = st.sidebar.data_editor(
    freq_block[["MTD Volume", "Frequency", "Low", "High", "Trips/Week"]],
    num_rows="dynamic", use_container_width=True, key="freq_block_edit"
)

st.sidebar.write("---")
st.sidebar.write("**Truck size ↔ Case capacity** (editable)")
st.sidebar.caption("June actuals suggest real trucks may carry closer to ~810 cases on average (vs the "
                    "~747 implied by these defaults) — worth nudging up if this consistently under-predicts.")
edited_veh_block = st.sidebar.data_editor(
    veh_block, num_rows="dynamic", use_container_width=True, key="veh_block_freq"
)

st.sidebar.write("---")
st.sidebar.header("🔄 Fleet Priority & TAT")
st.sidebar.caption("Defaults below are calibrated against real June actuals (see Methodology).")
own_registered = st.sidebar.number_input("Total Own Fleet — registered (trucks)", min_value=0, value=own_total_default, step=1, key="own_freq")
own_util_pct = st.sidebar.slider("Own fleet — effective utilization %", 0, 100, 90, 5, key="own_util_freq",
                                  help="Not every registered truck is genuinely poolable every day. June actuals showed ~90% for Own.")
own_return_rate_pct = st.sidebar.slider("Own — % back the NEXT day", 0, 100, 50, 5, key="rr_freq")
own_tat_days = st.sidebar.number_input("Own — full turn-around time (days)", min_value=1, max_value=14, value=2, step=1, key="tat_freq")

st.sidebar.write("")
fixed_registered = st.sidebar.number_input("Total Fixed/Bachat Fleet — registered (trucks)", min_value=0, value=fixed_total_default, step=1, key="fixed_freq")
fixed_util_pct = st.sidebar.slider("Fixed/Bachat — effective utilization %", 0, 100, 70, 5, key="fixed_util_freq",
                                    help="Fixed/Bachat trucks are often tied to dedicated routes/contracts. June actuals showed ~70%.")
fixed_return_rate_pct = st.sidebar.slider("Fixed/Bachat — % back the NEXT day", 0, 100, 30, 5, key="rr_fixed_freq")
fixed_tat_days = st.sidebar.number_input("Fixed/Bachat — full turn-around time (days)", min_value=1, max_value=14, value=2, step=1, key="tat_fixed_freq")

own_total = int(round(own_registered * own_util_pct / 100))
fixed_total = int(round(fixed_registered * fixed_util_pct / 100))
st.sidebar.caption(f"→ Effective pool used in calculations: **{own_total} Own** + **{fixed_total} Fixed/Bachat**")

st.sidebar.write("---")
districts = sorted(master["District"].dropna().unique())
sel_districts = st.sidebar.multiselect("Filter: District", districts, default=[])
search_term = st.sidebar.text_input("🔍 Search distributor", "")

# ---------------- FILTER ----------------
f = master.copy()
if sel_districts:
    f = f[f["District"].isin(sel_districts)]
if search_term:
    s = search_term.lower()
    f = f[f["Distributor"].str.lower().str.contains(s) | f["DBR CODE"].astype(str).str.lower().str.contains(s)]

f = f.dropna(subset=[sel_month]).copy()
f["MonthlyTarget"] = f[sel_month]

# ---------------- ASSIGN FREQUENCY BASED ON VOLUME BRACKET ----------------
def assign_frequency(vol):
    for _, row in edited_freq_block.iterrows():
        if row["Low"] <= vol < row["High"]:
            return row["Frequency"], row["Trips/Week"]
    return edited_freq_block.iloc[-1]["Frequency"], edited_freq_block.iloc[-1]["Trips/Week"]

f["MonthlyTarget"] = f["MonthlyTarget"].round(0)

freqs = f["MonthlyTarget"].apply(assign_frequency)
f["Frequency"] = freqs.apply(lambda x: x[0])
f["TripsPerWeek"] = freqs.apply(lambda x: x[1])
f["TripsPerMonth"] = np.ceil(f["TripsPerWeek"] * weeks_in_month)
f["LoadPerTrip"] = np.ceil(f["MonthlyTarget"] / f["TripsPerMonth"])

f["RecommendedTruckTonnage"] = f["MaxVehicleTonnage"].apply(
    lambda x: best_truck_for_tonnage_limit(x, fleet_tonnages)
)
f["TruckCaseCapacity"] = f["RecommendedTruckTonnage"].apply(
    lambda t: round(cases_per_truck(t, edited_veh_block))
)
f["TrucksPerTrip"] = np.ceil(f["LoadPerTrip"] / f["TruckCaseCapacity"]).replace([np.inf, -np.inf], np.nan)
f["TotalVehiclesPerMonth"] = f["TrucksPerTrip"] * f["TripsPerMonth"]

# whole numbers only — no fractional cases, trips, or trucks
for c in ["MonthlyTarget", "TripsPerMonth", "LoadPerTrip", "TruckCaseCapacity", "TrucksPerTrip", "TotalVehiclesPerMonth"]:
    f[c] = f[c].fillna(0).round(0).astype(int)

# ---------------- KPI CARDS ----------------
total_monthly_target = f["MonthlyTarget"].sum()
total_trips_per_month = f["TripsPerMonth"].sum()
total_truck_trips = f["TotalVehiclesPerMonth"].sum()
avg_load_per_trip = f["LoadPerTrip"].mean()

k1, k2, k3, k4 = st.columns(4)
k1.metric(f"{sel_month.title()} Target (cases)", f"{int(total_monthly_target):,}")
k2.metric("Total Delivery Trips / Month", f"{int(total_trips_per_month):,}")
k3.metric("Total Delivery-Vehicle Instances / Month", f"{int(total_truck_trips):,}")
k4.metric("Avg Load / Trip (cases)", f"{int(round(avg_load_per_trip)):,}" if pd.notna(avg_load_per_trip) else "—")
st.caption("ℹ️ 'Delivery-Vehicle Instances' counts every distributor visit separately and does **not** "
           "account for the same truck being reused across multiple distributors/days. For the actual "
           "number of vehicles to arrange, see the **Fleet Allocation** section below.")

st.write("")

c1, c2 = st.columns(2)
with c1:
    st.subheader("📅 Distributor Count by Frequency Bucket")
    freq_mix = f["Frequency"].value_counts().reset_index()
    freq_mix.columns = ["Frequency", "Distributors"]
    if not freq_mix.empty:
        fig = px.bar(freq_mix, x="Frequency", y="Distributors", color="Frequency")
        fig.update_layout(showlegend=False, height=380, margin=dict(t=10))
        st.plotly_chart(fig, use_container_width=True)

with c2:
    st.subheader("🚚 Delivery-Vehicle Instances / Month by District")
    chart_df = f.groupby("District", as_index=False)["TotalVehiclesPerMonth"].sum().sort_values(
        "TotalVehiclesPerMonth", ascending=False)
    if not chart_df.empty:
        fig2 = px.bar(chart_df, x="District", y="TotalVehiclesPerMonth", color="TotalVehiclesPerMonth",
                      color_continuous_scale="Purples")
        fig2.update_layout(showlegend=False, height=380, margin=dict(t=10))
        st.plotly_chart(fig2, use_container_width=True)

st.write("---")

top_cap_row_f = f.loc[f["MaxVehicleTonnage"].idxmax()] if f["MaxVehicleTonnage"].notna().any() else None
if top_cap_row_f is not None:
    st.info(f"🏆 **Highest max-capacity distributor:** {top_cap_row_f['Distributor']} "
            f"({top_cap_row_f['Town']}) — up to a **{int(top_cap_row_f['MaxVehicleTonnage'])}T** truck can reach this point.")

st.subheader("📋 Distributor-wise Frequency & Truck Requirement")

display_cols = [
    "DBR CODE", "Distributor", "Town", "District", "MaxVehicleTonnage", "MonthlyTarget",
    "Frequency", "TripsPerMonth", "LoadPerTrip", "RecommendedTruckTonnage",
    "TruckCaseCapacity", "TrucksPerTrip", "TotalVehiclesPerMonth"
]
st.dataframe(
    f[display_cols].sort_values("MonthlyTarget", ascending=False).style.format({
        "MaxVehicleTonnage": "{:,.0f}", "MonthlyTarget": "{:,.0f}", "TripsPerMonth": "{:,.0f}",
        "LoadPerTrip": "{:,.0f}", "RecommendedTruckTonnage": "{:,.0f}",
        "TruckCaseCapacity": "{:,.0f}", "TrucksPerTrip": "{:,.0f}", "TotalVehiclesPerMonth": "{:,.0f}"
    }, na_rep="—"),
    use_container_width=True, height=420
)

with st.expander("🔍 Diagnostic — why is the truck requirement high for some distributors?"):
    st.caption("If a distributor's frequency bucket gives them few trips/month but they have high volume, "
               "their per-trip load can require MULTIPLE trucks in a single visit. This is a calibration "
               "question: either your real trucks carry more cases than the assumed capacity below, or "
               "high-volume distributors need a higher frequency than their volume bracket currently assigns.")
    trip_dist = f["TrucksPerTrip"].value_counts().sort_index().reset_index()
    trip_dist.columns = ["Trucks Needed / Single Visit", "Number of Distributors"]
    st.dataframe(trip_dist, use_container_width=True, hide_index=True)
    multi_truck_count = int((f["TrucksPerTrip"] > 1).sum())
    if multi_truck_count > 0:
        st.warning(f"⚠️ **{multi_truck_count} of {len(f)} distributors** need more than 1 truck for a single "
                   f"visit this month. Raise the Truck size ↔ Case capacity table in the sidebar if your real "
                   f"trucks carry more cases per load, or increase frequency for high-volume distributors.")

with st.expander("ℹ️ Methodology"):
    st.markdown(f"""
    1. **Frequency** is looked up from the MTD-volume bracket (Assumptions sheet) each
       distributor's monthly target falls into — editable in the sidebar.
    2. **Trips/Month** = Trips/Week × Weeks in Month ({weeks_in_month}).
    3. **Load/Trip** = Monthly Target ÷ Trips/Month.
    4. **Recommended Truck** = largest truck ≤ the distributor's Max Capacity Vehicle limit.
    5. **Trucks/Trip** = ROUNDUP( Load/Trip ÷ Truck Case Capacity ).
    6. **Total Vehicles Needed/Month** = Trucks/Trip × Trips/Month.

    Adjust *Working days/week*, *Weeks in month*, the frequency brackets, or the truck
    case-capacity table in the sidebar to match your actual operating calendar.
    """)

st.download_button(
    "⬇️ Download distributor table as CSV",
    f[display_cols].to_csv(index=False).encode("utf-8"),
    file_name=f"fleet_calculator_frequency_{sel_month.lower()}.csv",
    mime="text/csv"
)

# =====================================================================================
# FLEET ALLOCATION SIMULATION — Own → Fixed/Bachat → Spot Hire, with TAT-based returns
# =====================================================================================
st.write("---")
st.header(f"🔄 Fleet Allocation — {sel_month.title()} — Priority + Turn-Around-Time (TAT)")
st.caption("Priority every working day: **Own fleet first → Fixed/Bachat next → Spot Hire (market)** "
           "covers any shortfall, accounting for trucks still in transit and expected returns.")

working_days_in_month = int(round(working_days_per_week * weeks_in_month))

# --- Real schedule: each distributor only needs a truck on ITS actual visit days ---
default_daily_requirements_f = compute_frequency_daily_schedule(f, working_days_per_week, weeks_in_month)
if len(default_daily_requirements_f) == 0:
    default_daily_requirements_f = [0] * working_days_in_month

default_alloc_rows_f = simulate_daily_allocation(
    default_daily_requirements_f, int(own_total), int(fixed_total),
    return_rate=own_return_rate_pct / 100.0, tat_days=int(own_tat_days),
    fixed_return_rate=fixed_return_rate_pct / 100.0, fixed_tat_days=int(fixed_tat_days)
)
default_alloc_df_f = pd.DataFrame(default_alloc_rows_f)
for c in default_alloc_df_f.columns:
    if c != "Day":
        default_alloc_df_f[c] = default_alloc_df_f[c].round(0).astype(int)

m_vol_f = int(total_monthly_target)
avg_own_per_day_f = int(round(default_alloc_df_f["Own Used"].mean()))
avg_fixed_per_day_f = int(round(default_alloc_df_f["Fixed Used"].mean()))
avg_spot_per_day_f = int(round(default_alloc_df_f["Spot Hire Used"].mean()))
avg_total_per_day_f = avg_own_per_day_f + avg_fixed_per_day_f + avg_spot_per_day_f
total_spot_month_f = int(default_alloc_df_f["Spot Hire Used"].sum())
spot_days_f = int((default_alloc_df_f["Spot Hire Used"] > 0).sum())
max_spot_single_day_f = int(default_alloc_df_f["Spot Hire Used"].max()) if len(default_alloc_df_f) else 0
min_need_day_f = int(default_alloc_df_f["Trucks Required"].min()) if len(default_alloc_df_f) else 0
max_need_day_f = int(default_alloc_df_f["Trucks Required"].max()) if len(default_alloc_df_f) else 0

with st.container(border=True):
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Monthly Volume (cases)", f"{m_vol_f:,}")
    s2.metric("🟦 Own Fleet (capacity)", f"{int(own_total):,}")
    s3.metric("🟧 Fixed/Bachat (capacity)", f"{int(fixed_total):,}")
    s4.metric("🟥 Spot Hire Needed This Month", f"{total_spot_month_f:,}")

    st.write(
        f"Distributors are now scheduled on their **actual visit days** (Twice/Week, Thrice/Week, or Daily) "
        f"instead of assuming everyone needs a truck every day — so daily need genuinely swings between "
        f"**{min_need_day_f} and {max_need_day_f} trucks/day** across {sel_month.title()}. Your "
        f"**Own ({int(own_total):,})** and **Fixed/Bachat ({int(fixed_total):,})** fleets, reused daily, "
        f"cover an average of **~{avg_own_per_day_f:,} Own + {avg_fixed_per_day_f:,} Fixed/Bachat** vehicles "
        f"a day. To fully cover the month, you'll need to arrange **~{total_spot_month_f:,} Spot Hire vehicle "
        f"bookings** from the market — needed on **{spot_days_f} of {working_days_in_month}** working days, "
        f"up to **{max_spot_single_day_f} vehicles** on the busiest day."
    )
    if total_spot_month_f == 0:
        st.success("✅ Own + Fixed fleet fully covers this month's demand — no spot hire needed.")

st.caption("ℹ️ Each distributor is scheduled on evenly-spaced visit days based on its frequency bucket "
           "(e.g. Twice/Week distributors get 2 of your working days per week, not all of them). "
           "Use 'Advanced' below to fine-tune the day-by-day pattern manually.")

# --- Advanced: let the user customize day-by-day if the real pattern differs ---
with st.expander("🛠️ Advanced — customize day-by-day requirement (if actual delivery days vary)"):
    st.caption("Defaults to the schedule computed above (each distributor only on its visit days). "
               "Edit any day to reflect real variation — the chart & table below will update.")

    _cache_key_freq = (sel_month, working_days_in_month, tuple(default_daily_requirements_f))
    if st.session_state.get("_cache_key_freq") != _cache_key_freq:
        st.session_state.freq_alloc_df = pd.DataFrame({
            "Day": list(range(1, working_days_in_month + 1)),
            "Trucks Required": default_daily_requirements_f
        })
        st.session_state._cache_key_freq = _cache_key_freq

    edited_freq_alloc_df = st.data_editor(
        st.session_state.freq_alloc_df, num_rows="fixed", use_container_width=True,
        key="freq_alloc_editor", hide_index=True,
        column_config={
            "Day": st.column_config.NumberColumn(disabled=True),
            "Trucks Required": st.column_config.NumberColumn(min_value=0, step=1, format="%d"),
        }
    )
    daily_requirements_f = [int(round(x)) for x in edited_freq_alloc_df["Trucks Required"].tolist()]

    alloc_rows_f = simulate_daily_allocation(
        daily_requirements_f, int(own_total), int(fixed_total),
        return_rate=own_return_rate_pct / 100.0, tat_days=int(own_tat_days),
        fixed_return_rate=fixed_return_rate_pct / 100.0, fixed_tat_days=int(fixed_tat_days)
    )
    alloc_df_f = pd.DataFrame(alloc_rows_f)
    for c in alloc_df_f.columns:
        if c != "Day":
            alloc_df_f[c] = alloc_df_f[c].round(0).astype(int)

    n_days_f = len(alloc_df_f)
    avg_own_f = round(alloc_df_f["Own Used"].mean()) if n_days_f else 0
    avg_fixed_f = round(alloc_df_f["Fixed Used"].mean()) if n_days_f else 0
    avg_spot_f = round(alloc_df_f["Spot Hire Used"].mean()) if n_days_f else 0
    total_spot_f = int(alloc_df_f["Spot Hire Used"].sum())

    st.caption("Averages per day across this custom table (matches the units in the summary card above).")
    b1, b2, b3, b4 = st.columns(4)
    b1.metric("Own Fleet / Day (avg)", f"{avg_own_f:,}")
    b2.metric("Fixed/Bachat / Day (avg)", f"{avg_fixed_f:,}")
    b3.metric("Spot Hire / Day (avg)", f"{avg_spot_f:,}")
    b4.metric("Spot Hire — Total This Month", f"{total_spot_f:,}")

    chart_alloc_f = alloc_df_f.melt(id_vars=["Day"], value_vars=["Own Used", "Fixed Used", "Spot Hire Used"],
                                     var_name="Source", value_name="Trucks")
    fig_alloc_f = px.bar(chart_alloc_f, x="Day", y="Trucks", color="Source", barmode="stack",
                          color_discrete_map={"Own Used": "#1b4fd2", "Fixed Used": "#f2a20c", "Spot Hire Used": "#d93838"})
    fig_alloc_f.update_layout(height=380, margin=dict(t=10))
    st.plotly_chart(fig_alloc_f, use_container_width=True)

    st.dataframe(alloc_df_f, use_container_width=True, height=380)

    st.download_button(
        "⬇️ Download customized allocation table as CSV",
        alloc_df_f.to_csv(index=False).encode("utf-8"),
        file_name=f"fleet_allocation_frequency_{sel_month.lower()}.csv",
        mime="text/csv"
    )

with st.expander("ℹ️ Allocation Methodology"):
    st.markdown(f"""
    **Scheduling:** each distributor is assigned evenly-spaced day-of-week visit slots based on its
    frequency bucket (e.g. a Twice/Week distributor is only scheduled on 2 of your
    {int(working_days_per_week)} working days each week). A day's total requirement is the sum of
    Trucks/Trip across only the distributors scheduled that day — so lower-frequency distributors
    genuinely reduce daily truck need on their off-days, instead of being smoothed into a flat average.

    **Priority every day:** Own fleet → Fixed/Bachat fleet → Spot Hire (uncapped, market-arranged same-day).

    **Returns logic:** a truck dispatched today is unavailable today. For Own, **{own_return_rate_pct}%** of
    that day's dispatch is back the next day, fully back by day **{own_tat_days}**. Fixed/Bachat uses
    **{fixed_return_rate_pct}%**/**{fixed_tat_days} days** — modeled independently since June actuals showed
    they behave differently. Spot hire only kicks in once Own + Fixed availability
    (idle trucks + expected returns) is exhausted.

    All figures are whole truck counts.
    """)
