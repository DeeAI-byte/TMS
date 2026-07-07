import streamlit as st
import pandas as pd
import plotly.express as px
import sys
import os

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from utils.data_loader import (
    load_locations, load_db_capacity, load_vehicle_database,
    load_db_target, build_master_table, get_month_options
)

st.set_page_config(page_title="Logistics TMS | Overview", page_icon="🚛", layout="wide")
st.markdown("<style>.block-container { padding-top: 1rem; padding-bottom: 0.5rem; }</style>", unsafe_allow_html=True)

st.caption("🥤 Coca-Cola - SLMG")
st.title("🚛 Transport Management System — Overview")
st.caption("Distributor network, fleet & monthly target snapshot")
st.write("---")

# ---------------- LOAD DATA ----------------
try:
    loc = load_locations()
    cap = load_db_capacity()
    veh = load_vehicle_database()
    tgt = load_db_target()
    master = build_master_table()
except Exception as e:
    st.error(f"⚠️ Could not load data files: {e}")
    st.stop()

MONTHS = get_month_options()

# ---------------- SIDEBAR SLICERS ----------------
st.sidebar.header("🔎 Filters")

districts = sorted(master["District"].dropna().unique())
sel_districts = st.sidebar.multiselect("District", districts, default=[])

towns_pool = master[master["District"].isin(sel_districts)]["Town"] if sel_districts else master["Town"]
towns = sorted(towns_pool.dropna().unique())
sel_towns = st.sidebar.multiselect("Town / Area", towns, default=[])

channels = sorted(master["Channel"].dropna().unique())
sel_channels = st.sidebar.multiselect("Channel (Dsd/Hub/MT/LKA)", channels, default=[])

transporters = sorted(veh["Transporter Name"].dropna().unique())
sel_transporters = st.sidebar.multiselect("Transporter", transporters, default=[])

sel_month = st.sidebar.selectbox("Target Month", MONTHS, index=MONTHS.index("JULY"))

st.sidebar.write("---")
search_term = st.sidebar.text_input("🔍 Search distributor by name / code", "")

# ---------------- APPLY FILTERS ----------------
f = master.copy()
if sel_districts:
    f = f[f["District"].isin(sel_districts)]
if sel_towns:
    f = f[f["Town"].isin(sel_towns)]
if sel_channels:
    f = f[f["Channel"].isin(sel_channels)]
if search_term:
    s = search_term.lower()
    f = f[f["Distributor"].str.lower().str.contains(s) | f["DBR CODE"].astype(str).str.lower().str.contains(s)]

veh_f = veh.copy()
if sel_transporters:
    veh_f = veh_f[veh_f["Transporter Name"].isin(sel_transporters)]

# ---------------- KPI CARDS ----------------
total_distributors = f["Distributor"].nunique()
total_month_volume = f[sel_month].sum()
total_own_trucks = veh_f[veh_f["OwnershipType"] == "Own"].shape[0]
total_fixed_trucks = veh_f[veh_f["OwnershipType"] == "Fixed"].shape[0]
total_fleet_tonnage = veh_f["CapacityTonnage"].sum()
avg_max_vehicle = f["MaxVehicleTonnage"].mean()

k1, k2, k3, k4, k5, k6 = st.columns(6)
k1.metric("Distributors", f"{total_distributors:,}")
k2.metric(f"{sel_month.title()} Volume (cases)", f"{int(round(total_month_volume)):,}")
k3.metric("Own Trucks", f"{total_own_trucks}")
k4.metric("Fixed/3rd-Party Trucks", f"{total_fixed_trucks}")
k5.metric("Total Fleet Capacity (T)", f"{int(round(total_fleet_tonnage)):,}")
k6.metric("Avg Max Vehicle Allowed (T)", f"{int(round(avg_max_vehicle)):,}" if pd.notna(avg_max_vehicle) else "—")

st.write("")

# ---------------- CHARTS ----------------
c1, c2 = st.columns(2)

with c1:
    st.subheader(f"📊 {sel_month.title()} Volume by District")
    dist_vol = f.groupby("District", as_index=False)[sel_month].sum().sort_values(sel_month, ascending=False)
    if not dist_vol.empty:
        fig = px.bar(dist_vol, x="District", y=sel_month, color=sel_month, color_continuous_scale="Blues")
        fig.update_layout(showlegend=False, height=380, margin=dict(t=10))
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No data for the selected filters.")

with c2:
    st.subheader("🚚 Fleet Mix by Ownership")
    own_mix = veh_f["OwnershipType"].value_counts().reset_index()
    own_mix.columns = ["OwnershipType", "Count"]
    if not own_mix.empty:
        fig2 = px.pie(own_mix, names="OwnershipType", values="Count", hole=0.45)
        fig2.update_layout(height=380, margin=dict(t=10))
        st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info("No fleet data for the selected filters.")

c3, c4 = st.columns(2)

with c3:
    st.subheader("🏭 Fleet Count by Base Location")
    loc_fleet = veh_f["Location"].value_counts().reset_index()
    loc_fleet.columns = ["Location", "Trucks"]
    if not loc_fleet.empty:
        fig3 = px.bar(loc_fleet, x="Location", y="Trucks", color="Trucks", color_continuous_scale="Oranges")
        fig3.update_layout(showlegend=False, height=380, margin=dict(t=10))
        st.plotly_chart(fig3, use_container_width=True)
    else:
        st.info("No fleet data for the selected filters.")

with c4:
    st.subheader("📈 Monthly Volume Trend (selected distributors)")
    trend = f[MONTHS].sum().reset_index()
    trend.columns = ["Month", "Volume"]
    fig4 = px.line(trend, x="Month", y="Volume", markers=True)
    fig4.update_layout(height=380, margin=dict(t=10))
    st.plotly_chart(fig4, use_container_width=True)

st.write("---")

# ---------------- DISTRIBUTOR / SEARCH TABLE ----------------
st.subheader("📋 Distributor Details")
show_cols = ["DBR CODE", "Distributor", "Town", "District", "Channel", "MaxVehicleTonnage"] + MONTHS
available_cols = [c for c in show_cols if c in f.columns]
display_table = f[available_cols].sort_values(sel_month, ascending=False, na_position="last").copy()
numeric_cols = ["MaxVehicleTonnage"] + [m for m in MONTHS if m in display_table.columns]
for c in numeric_cols:
    display_table[c] = display_table[c].round(0)
st.dataframe(
    display_table.style.format({c: "{:,.0f}" for c in numeric_cols}, na_rep="—"),
    use_container_width=True, height=380
)

st.caption(
    "Tip: use the sidebar filters to slice by district/town/channel/transporter, "
    "or search a distributor by name or DBR code. Head to the other pages for route "
    "planning and truck-requirement calculators."
)
