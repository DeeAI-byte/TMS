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
    fleet_totals_by_ownership, simulate_daily_allocation
)

st.set_page_config(page_title="Fleet Calculator | Frequency Based", page_icon="📅", layout="wide")
st.markdown("<style>.block-container { padding-top: 1rem; padding-bottom: 0.5rem; }</style>", unsafe_allow_html=True)

st.title("📅 Monthly Fleet Calculator — Frequency Based")
st.caption("Delivery frequency is derived from each distributor's monthly volume bracket "
           "(per your Assumptions sheet), then truck-trips are computed per delivery cycle.")
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
edited_veh_block = st.sidebar.data_editor(
    veh_block, num_rows="dynamic", use_container_width=True, key="veh_block_freq"
)

st.sidebar.write("---")
st.sidebar.header("🔄 Fleet Priority & TAT")
own_total = st.sidebar.number_input("Total Own Fleet (trucks)", min_value=0, value=own_total_default, step=1, key="own_freq")
fixed_total = st.sidebar.number_input("Total Fixed/Bachat Fleet (trucks)", min_value=0, value=fixed_total_default, step=1, key="fixed_freq")
return_rate_pct = st.sidebar.slider("% of dispatched trucks back the NEXT day", 0, 100, 50, 5, key="rr_freq")
tat_days = st.sidebar.number_input("Full turn-around time (days until ALL are back)", min_value=1, max_value=10, value=2, step=1, key="tat_freq")

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
f["TotalTruckTripsPerMonth"] = f["TrucksPerTrip"] * f["TripsPerMonth"]

# whole numbers only — no fractional cases, trips, or trucks
for c in ["MonthlyTarget", "TripsPerMonth", "LoadPerTrip", "TruckCaseCapacity", "TrucksPerTrip", "TotalTruckTripsPerMonth"]:
    f[c] = f[c].fillna(0).round(0).astype(int)

# ---------------- KPI CARDS ----------------
total_monthly_target = f["MonthlyTarget"].sum()
total_trips_per_month = f["TripsPerMonth"].sum()
total_truck_trips = f["TotalTruckTripsPerMonth"].sum()
avg_load_per_trip = f["LoadPerTrip"].mean()

k1, k2, k3, k4 = st.columns(4)
k1.metric(f"{sel_month.title()} Target (cases)", f"{int(total_monthly_target):,}")
k2.metric("Total Delivery Trips / Month", f"{int(total_trips_per_month):,}")
k3.metric("Total Truck-Trips Needed / Month", f"{int(total_truck_trips):,}")
k4.metric("Avg Load / Trip (cases)", f"{int(round(avg_load_per_trip)):,}" if pd.notna(avg_load_per_trip) else "—")

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
    st.subheader("🚚 Truck-Trips / Month by District")
    chart_df = f.groupby("District", as_index=False)["TotalTruckTripsPerMonth"].sum().sort_values(
        "TotalTruckTripsPerMonth", ascending=False)
    if not chart_df.empty:
        fig2 = px.bar(chart_df, x="District", y="TotalTruckTripsPerMonth", color="TotalTruckTripsPerMonth",
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
    "TruckCaseCapacity", "TrucksPerTrip", "TotalTruckTripsPerMonth"
]
st.dataframe(
    f[display_cols].sort_values("MonthlyTarget", ascending=False).style.format({
        "MaxVehicleTonnage": "{:,.0f}", "MonthlyTarget": "{:,.0f}", "TripsPerMonth": "{:,.0f}",
        "LoadPerTrip": "{:,.0f}", "RecommendedTruckTonnage": "{:,.0f}",
        "TruckCaseCapacity": "{:,.0f}", "TrucksPerTrip": "{:,.0f}", "TotalTruckTripsPerMonth": "{:,.0f}"
    }, na_rep="—"),
    use_container_width=True, height=420
)

with st.expander("ℹ️ Methodology"):
    st.markdown(f"""
    1. **Frequency** is looked up from the MTD-volume bracket (Assumptions sheet) each
       distributor's monthly target falls into — editable in the sidebar.
    2. **Trips/Month** = Trips/Week × Weeks in Month ({weeks_in_month}).
    3. **Load/Trip** = Monthly Target ÷ Trips/Month.
    4. **Recommended Truck** = largest truck ≤ the distributor's Max Capacity Vehicle limit.
    5. **Trucks/Trip** = ROUNDUP( Load/Trip ÷ Truck Case Capacity ).
    6. **Total Truck-Trips/Month** = Trucks/Trip × Trips/Month.

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
st.header("🔄 Fleet Allocation — Priority + Turn-Around-Time (TAT)")

working_days_in_month = int(round(working_days_per_week * weeks_in_month))
avg_truck_trips_per_day = int(np.ceil(total_truck_trips / working_days_in_month)) if working_days_in_month > 0 else 0

st.caption(f"Delivery days aren't identical across distributors under a frequency model, so this spreads the "
           f"**{total_truck_trips:,} total truck-trips/month** evenly across **{working_days_in_month} working "
           f"days** (≈{avg_truck_trips_per_day:,}/day) as a starting point — edit any day below for a more "
           f"realistic pattern. Priority: **Own fleet → Fixed/Bachat → Spot Hire (market)**, with TAT-based returns.")

if working_days_in_month != int(st.session_state.get("_wd_cached_freq", -1)):
    st.session_state.freq_alloc_df = pd.DataFrame({
        "Day": list(range(1, working_days_in_month + 1)),
        "Trucks Required": [avg_truck_trips_per_day] * working_days_in_month
    })
    st.session_state._wd_cached_freq = working_days_in_month

edited_freq_alloc_df = st.data_editor(
    st.session_state.freq_alloc_df, num_rows="fixed", use_container_width=True,
    key="freq_alloc_editor", hide_index=True,
    column_config={
        "Day": st.column_config.NumberColumn(disabled=True),
        "Trucks Required": st.column_config.NumberColumn(min_value=0, step=1, format="%d"),
    }
)
daily_requirements_f = [int(round(x)) for x in edited_freq_alloc_df["Trucks Required"].tolist()]

alloc_rows_f = simulate_daily_allocation(daily_requirements_f, int(own_total), int(fixed_total),
                                          return_rate=return_rate_pct / 100.0, tat_days=int(tat_days))
alloc_df_f = pd.DataFrame(alloc_rows_f)
for c in alloc_df_f.columns:
    if c != "Day":
        alloc_df_f[c] = alloc_df_f[c].round(0).astype(int)

total_need_f = int(alloc_df_f["Trucks Required"].sum())
total_own_used_f = int(alloc_df_f["Own Used"].sum())
total_fixed_used_f = int(alloc_df_f["Fixed Used"].sum())
total_spot_used_f = int(alloc_df_f["Spot Hire Used"].sum())

b1, b2, b3, b4 = st.columns(4)
b1.metric("Total Truck-Trips Needed", f"{total_need_f:,}")
b2.metric("Own Fleet Used", f"{total_own_used_f:,}")
b3.metric("Fixed/Bachat Used", f"{total_fixed_used_f:,}")
b4.metric("Spot Hire Needed", f"{total_spot_used_f:,}")

if total_spot_used_f > 0:
    st.warning(f"⚠️ Spot hire required on {int((alloc_df_f['Spot Hire Used'] > 0).sum())} of "
               f"{working_days_in_month} working days — {total_spot_used_f:,} truck-trips to arrange from the market.")
else:
    st.success("✅ Own + Fixed fleet fully covers demand across all working days — no spot hire needed.")

chart_alloc_f = alloc_df_f.melt(id_vars=["Day"], value_vars=["Own Used", "Fixed Used", "Spot Hire Used"],
                                 var_name="Source", value_name="Trucks")
fig_alloc_f = px.bar(chart_alloc_f, x="Day", y="Trucks", color="Source", barmode="stack",
                      color_discrete_map={"Own Used": "#1b4fd2", "Fixed Used": "#f2a20c", "Spot Hire Used": "#d93838"})
fig_alloc_f.update_layout(height=380, margin=dict(t=10))
st.plotly_chart(fig_alloc_f, use_container_width=True)

st.dataframe(alloc_df_f, use_container_width=True, height=380)

with st.expander("ℹ️ Allocation Methodology"):
    st.markdown(f"""
    **Priority every day:** Own fleet → Fixed/Bachat fleet → Spot Hire (uncapped, market-arranged same-day).

    **Returns logic:** a truck dispatched today is unavailable today. **{return_rate_pct}%** of that day's
    dispatched batch is assumed back and available again the next day; the remaining share is back in the
    yard by day **{tat_days}** after dispatch. Spot hire only kicks in once Own + Fixed availability
    (idle trucks + expected returns) is exhausted.

    All figures are whole truck counts.
    """)

st.download_button(
    "⬇️ Download allocation table as CSV",
    alloc_df_f.to_csv(index=False).encode("utf-8"),
    file_name=f"fleet_allocation_frequency_{sel_month.lower()}.csv",
    mime="text/csv"
)
