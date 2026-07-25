import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import sys
import os
from datetime import date

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.data_loader import (
    load_vehicle_database, fleet_totals_by_ownership, load_assumptions,
    process_gate_out_log, cross_reference_fleet, allocate_shipments_to_fleet
)

st.set_page_config(page_title="Live Fleet Tracker", page_icon="📡", layout="wide")
st.markdown("<style>.block-container { padding-top: 1rem; padding-bottom: 0.5rem; }</style>", unsafe_allow_html=True)

st.title("📡 Live Fleet Tracker & Dispatch Overview")
st.caption("Live sync with Route Creation — tracks vehicle dispatches, gate-outs, returns, and spot hire needs in real time.")
st.write("---")

try:
    veh_db = load_vehicle_database()
    own_total_default, fixed_total_default = fleet_totals_by_ownership(veh_db)
    veh_block, _ = load_assumptions()
except Exception as e:
    st.error(f"⚠️ Could not load vehicle database: {e}")
    st.stop()

as_of_date = st.sidebar.date_input("As of date", value=date.today(), key="live_as_of_date")

# Sync Gate-Out Log State
if "gate_out_log" not in st.session_state:
    st.session_state.gate_out_log = pd.DataFrame(columns=[
        "Vehicle Number", "Tonnage", "Ownership", "Gate Out Date",
        "Actual Return Date", "Route / Distributor"
    ])

# ---------------- GATE-OUT LOG & FLEET CROSS REFERENCE ----------------
raw_gate_log = st.session_state.gate_out_log
processed_gate_df, currently_out_df = process_gate_out_log(raw_gate_log, as_of_date)
fleet_status_df, unmatched_df = cross_reference_fleet(veh_db, currently_out_df)

own_available_df = fleet_status_df[(fleet_status_df["OwnershipType"] == "Own") & (fleet_status_df["Status"] == "Available")]
fixed_available_df = fleet_status_df[(fleet_status_df["OwnershipType"] == "Fixed") & (fleet_status_df["Status"] == "Available")]
own_out_df = fleet_status_df[(fleet_status_df["OwnershipType"] == "Own") & (fleet_status_df["Status"] == "Out")]
fixed_out_df = fleet_status_df[(fleet_status_df["OwnershipType"] == "Fixed") & (fleet_status_df["Status"] == "Out")]

own_available, fixed_available = len(own_available_df), len(fixed_available_df)
own_out, fixed_out = len(own_out_df), len(fixed_out_df)

# ---------------- LIVE FLEET METRICS ----------------
st.header(f"📊 Live Fleet Status — {as_of_date.strftime('%d %b %Y')}")

k1, k2, k3, k4 = st.columns(4)
k1.metric("🟦 Own Available", f"{own_available:,} / {own_available + own_out:,}", delta=f"-{own_out} out" if own_out else None, delta_color="inverse")
k2.metric("🟧 Fixed Available", f"{fixed_available:,} / {fixed_available + fixed_out:,}", delta=f"-{fixed_out} out" if fixed_out else None, delta_color="inverse")
k3.metric("Total Available Now", f"{own_available + fixed_available:,}")
k4.metric("Vehicles Currently Out", f"{own_out + fixed_out:,}")

st.write("---")

# ---------------- LIVE LOAD SUMMARY (FROM ROUTE CREATION) ----------------
st.header("📦 Today's Load & Dispatch Summary")

dispatch_df = st.session_state.get("dispatch_table", pd.DataFrame())

if not dispatch_df.empty:
    dispatch_df["Load_Num"] = pd.to_numeric(dispatch_df["Total Load (Ton)"], errors="coerce").fillna(0)
    
    total_orders = len(dispatch_df)
    total_load = round(float(dispatch_df["Load_Num"].sum()), 1)
    
    dispatched_mask = dispatch_df["Dispatch Status"] == "Dispatched"
    pending_mask = dispatch_df["Dispatch Status"].isin(["Pending", "Alloted"])
    
    dispatched_orders = int(dispatched_mask.sum())
    dispatched_load = round(float(dispatch_df.loc[dispatched_mask, "Load_Num"].sum()), 1)
    pending_orders = int(pending_mask.sum())
    pending_load = round(float(dispatch_df.loc[pending_mask, "Load_Num"].sum()), 1)

    sl1, sl2, sl3, sl4, sl5, sl6 = st.columns(6)
    sl1.metric("Total Orders", f"{total_orders:,}")
    sl2.metric("Total Load (Ton)", f"{total_load:,}")
    sl3.metric("✅ Dispatched Orders", f"{dispatched_orders:,}")
    sl4.metric("✅ Dispatched Load (Ton)", f"{dispatched_load:,}")
    sl5.metric("🕒 Pending/Alloted Orders", f"{pending_orders:,}")
    sl6.metric("🕒 Pending/Alloted Load (Ton)", f"{pending_load:,}")

    st.subheader("📋 Active Dispatches")
    st.dataframe(dispatch_df[["Date", "Plant", "Route / Distributor", "Total Load (Ton)", "Dispatch Status", "Assigned Vehicle"]], use_container_width=True, hide_index=True)
else:
    st.info("No dispatches entered yet. Head to the Route Creation page to punch today's orders.")

st.write("---")

# ---------------- EDITABLE GATE-OUT & GATE-IN LOG ----------------
st.header("🚛 Gate-Out & Gate-In Log")
st.caption("Punched dispatches automatically appear here. Type an **Actual Return Date** to return a vehicle back to the Available pool.")

if not raw_gate_log.empty:
    edited_gate_log = st.data_editor(
        raw_gate_log,
        num_rows="dynamic",
        use_container_width=True,
        key="gate_out_editor",
        column_config={
            "Vehicle Number": st.column_config.TextColumn("Vehicle Number", disabled=True),
            "Tonnage": st.column_config.NumberColumn("Tonnage (T)", disabled=True),
            "Ownership": st.column_config.TextColumn("Ownership", disabled=True),
            "Gate Out Date": st.column_config.TextColumn("Gate Out Date", disabled=True),
            "Actual Return Date": st.column_config.DateColumn("Actual Return Date (Gate-In)", default=None),
            "Route / Distributor": st.column_config.TextColumn("Route / Distributor", disabled=True)
        }
    )
    st.session_state.gate_out_log = edited_gate_log
else:
    st.info("No vehicles currently dispatched.")

st.write("---")

# ---------------- AVAILABLE VS OUT VEHICLE TABLES ----------------
c1, c2 = st.columns(2)
with c1:
    st.subheader("✅ Available Vehicles")
    avail_cols = [c for c in ["Vehicle Number", "OwnershipType", "Location", "Transporter Name", "CapacityTonnage"] if c in fleet_status_df.columns]
    avail_show = fleet_status_df[fleet_status_df["Status"] == "Available"][avail_cols]
    st.dataframe(avail_show, use_container_width=True, height=300, hide_index=True)

with c2:
    st.subheader("🚫 Vehicles Currently Out")
    out_cols = [c for c in ["Vehicle Number", "OwnershipType", "Location", "Transporter Name", "Distributor", "Days Out"] if c in fleet_status_df.columns]
    out_show = fleet_status_df[fleet_status_df["Status"] == "Out"][out_cols]
    st.dataframe(out_show, use_container_width=True, height=300, hide_index=True)
