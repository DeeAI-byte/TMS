import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import sys
import os
import math

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.data_loader import (
    build_master_table, load_assumptions, load_vehicle_database,
    cases_per_truck, best_truck_for_tonnage_limit, get_month_options
)

st.set_page_config(page_title="Fleet Calculator | Daily Load", page_icon="📦", layout="wide")
st.markdown("<style>.block-container { padding-top: 1rem; padding-bottom: 0.5rem; }</style>", unsafe_allow_html=True)

st.title("📦 Monthly Fleet Calculator — Daily Load Basis")
st.caption("Use this when you already know (or want to plan) each distributor's **daily** dispatch load — "
           "no delivery-frequency assumption is applied.")
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

# ---------------- SIDEBAR CONFIG ----------------
st.sidebar.header("⚙️ Calculator Settings")
sel_month = st.sidebar.selectbox("Target Month", MONTHS, index=MONTHS.index("JULY"))
working_days = st.sidebar.number_input("Working days in month", min_value=1, max_value=31, value=26)

st.sidebar.write("---")
st.sidebar.write("**Truck size ↔ Case capacity** (editable)")
edited_veh_block = st.sidebar.data_editor(
    veh_block, num_rows="dynamic", use_container_width=True, key="veh_block_daily"
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

# ---------------- CORE CALCULATION ----------------
f["MonthlyTarget"] = f[sel_month]
f["DailyAvgLoad"] = f["MonthlyTarget"] / working_days

f["RecommendedTruckTonnage"] = f["MaxVehicleTonnage"].apply(
    lambda x: best_truck_for_tonnage_limit(x, fleet_tonnages)
)
f["TruckCaseCapacity"] = f["RecommendedTruckTonnage"].apply(
    lambda t: cases_per_truck(t, edited_veh_block)
)
f["TrucksPerDay"] = np.ceil(f["DailyAvgLoad"] / f["TruckCaseCapacity"]).replace([np.inf, -np.inf], np.nan)
f["TrucksPerMonth (truck-trips)"] = f["TrucksPerDay"] * working_days
f["MTD Target (cumulative)"] = f["MonthlyTarget"]  # full month reference for MTD tracking table below

# ---------------- KPI CARDS ----------------
total_daily_load = f["DailyAvgLoad"].sum()
total_trucks_per_day = f["TrucksPerDay"].sum()
total_monthly_target = f["MonthlyTarget"].sum()
avg_util = (f["DailyAvgLoad"] / (f["TrucksPerDay"] * f["TruckCaseCapacity"])).replace([np.inf, -np.inf], np.nan).mean()

k1, k2, k3, k4 = st.columns(4)
k1.metric(f"{sel_month.title()} Target (cases)", f"{total_monthly_target:,.0f}")
k2.metric("Total Daily Avg Load (cases/day)", f"{total_daily_load:,.0f}")
k3.metric("Trucks Needed / Day (company-wide)", f"{total_trucks_per_day:,.0f}")
k4.metric("Avg Truck Utilization", f"{avg_util*100:,.0f}%" if pd.notna(avg_util) else "—")

st.write("")

c1, c2 = st.columns(2)
with c1:
    st.subheader("🚚 Trucks Needed / Day by District")
    chart_df = f.groupby("District", as_index=False)["TrucksPerDay"].sum().sort_values("TrucksPerDay", ascending=False)
    if not chart_df.empty:
        fig = px.bar(chart_df, x="District", y="TrucksPerDay", color="TrucksPerDay", color_continuous_scale="Teal")
        fig.update_layout(showlegend=False, height=380, margin=dict(t=10))
        st.plotly_chart(fig, use_container_width=True)

with c2:
    st.subheader("📊 Recommended Truck Size Mix")
    mix = f["RecommendedTruckTonnage"].value_counts().reset_index()
    mix.columns = ["Truck Tonnage", "Distributors"]
    if not mix.empty:
        fig2 = px.pie(mix, names="Truck Tonnage", values="Distributors", hole=0.45)
        fig2.update_layout(height=380, margin=dict(t=10))
        st.plotly_chart(fig2, use_container_width=True)

st.write("---")
st.subheader("📋 Distributor-wise Daily Load & Truck Requirement")

display_cols = [
    "DBR CODE", "Distributor", "Town", "District", "MaxVehicleTonnage",
    "MonthlyTarget", "DailyAvgLoad", "RecommendedTruckTonnage", "TruckCaseCapacity",
    "TrucksPerDay", "TrucksPerMonth (truck-trips)"
]
st.dataframe(
    f[display_cols].sort_values("DailyAvgLoad", ascending=False).style.format({
        "MonthlyTarget": "{:,.0f}", "DailyAvgLoad": "{:,.0f}", "TruckCaseCapacity": "{:,.0f}",
        "TrucksPerDay": "{:,.1f}", "TrucksPerMonth (truck-trips)": "{:,.1f}"
    }),
    use_container_width=True, height=420
)

with st.expander("ℹ️ Methodology"):
    st.markdown(f"""
    1. **Daily Avg Load** = Monthly Target ({sel_month.title()}) ÷ Working Days ({working_days})
    2. **Recommended Truck** = largest truck size in your fleet that is still ≤ the distributor's
       *Max Capacity Vehicle* limit (the biggest truck physically allowed to reach that point).
    3. **Truck Case Capacity** = interpolated from the Truck size ↔ Case capacity table in the sidebar
       (edit it if your real per-truck case capacities differ).
    4. **Trucks/Day** = ROUNDUP( Daily Avg Load ÷ Truck Case Capacity )
    5. **Trucks/Month** = Trucks/Day × Working Days (total truck-trips needed across the month)

    This page assumes **flat daily dispatch** — no delivery-frequency logic. Use the
    *Frequency Based* calculator if deliveries don't happen every working day.
    """)

st.download_button(
    "⬇️ Download this table as CSV",
    f[display_cols].to_csv(index=False).encode("utf-8"),
    file_name=f"fleet_calculator_daily_load_{sel_month.lower()}.csv",
    mime="text/csv"
)
