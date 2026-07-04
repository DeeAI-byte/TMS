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
    cases_per_truck, best_truck_for_tonnage_limit, get_month_options
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

freqs = f["MonthlyTarget"].apply(assign_frequency)
f["Frequency"] = freqs.apply(lambda x: x[0])
f["TripsPerWeek"] = freqs.apply(lambda x: x[1])
f["TripsPerMonth"] = f["TripsPerWeek"] * weeks_in_month
f["LoadPerTrip"] = f["MonthlyTarget"] / f["TripsPerMonth"]

f["RecommendedTruckTonnage"] = f["MaxVehicleTonnage"].apply(
    lambda x: best_truck_for_tonnage_limit(x, fleet_tonnages)
)
f["TruckCaseCapacity"] = f["RecommendedTruckTonnage"].apply(
    lambda t: cases_per_truck(t, edited_veh_block)
)
f["TrucksPerTrip"] = np.ceil(f["LoadPerTrip"] / f["TruckCaseCapacity"]).replace([np.inf, -np.inf], np.nan)
f["TotalTruckTripsPerMonth"] = f["TrucksPerTrip"] * f["TripsPerMonth"]

# ---------------- KPI CARDS ----------------
total_monthly_target = f["MonthlyTarget"].sum()
total_trips_per_month = f["TripsPerMonth"].sum()
total_truck_trips = f["TotalTruckTripsPerMonth"].sum()
avg_load_per_trip = f["LoadPerTrip"].mean()

k1, k2, k3, k4 = st.columns(4)
k1.metric(f"{sel_month.title()} Target (cases)", f"{total_monthly_target:,.0f}")
k2.metric("Total Delivery Trips / Month", f"{total_trips_per_month:,.0f}")
k3.metric("Total Truck-Trips Needed / Month", f"{total_truck_trips:,.0f}")
k4.metric("Avg Load / Trip (cases)", f"{avg_load_per_trip:,.0f}" if pd.notna(avg_load_per_trip) else "—")

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
st.subheader("📋 Distributor-wise Frequency & Truck Requirement")

display_cols = [
    "DBR CODE", "Distributor", "Town", "District", "MaxVehicleTonnage", "MonthlyTarget",
    "Frequency", "TripsPerMonth", "LoadPerTrip", "RecommendedTruckTonnage",
    "TruckCaseCapacity", "TrucksPerTrip", "TotalTruckTripsPerMonth"
]
st.dataframe(
    f[display_cols].sort_values("MonthlyTarget", ascending=False).style.format({
        "MonthlyTarget": "{:,.0f}", "TripsPerMonth": "{:,.1f}", "LoadPerTrip": "{:,.0f}",
        "TruckCaseCapacity": "{:,.0f}", "TrucksPerTrip": "{:,.1f}", "TotalTruckTripsPerMonth": "{:,.1f}"
    }),
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
    "⬇️ Download this table as CSV",
    f[display_cols].to_csv(index=False).encode("utf-8"),
    file_name=f"fleet_calculator_frequency_{sel_month.lower()}.csv",
    mime="text/csv"
)
