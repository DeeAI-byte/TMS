import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import sys
import os
import math
from datetime import date

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.data_loader import (
    load_vehicle_database, fleet_totals_by_ownership, load_assumptions,
    process_gate_out_log, cross_reference_fleet, validate_ownership_values,
    allocate_trucks_by_tonnage, allocate_shipments_to_fleet
)

st.set_page_config(page_title="Live Fleet Tracker", page_icon="📡", layout="wide")
st.markdown("<style>.block-container { padding-top: 1rem; padding-bottom: 0.5rem; }</style>", unsafe_allow_html=True)

st.title("📡 Live Fleet Tracker")
st.caption("Cross-references your transport office's gate-out log against your actual Vehicle Database — "
           "real available Own/Fixed fleet, right now, plus today's load → trucks → spot hire in one place.")
st.write("---")

try:
    veh_db = load_vehicle_database()
    own_total_default, fixed_total_default = fleet_totals_by_ownership(veh_db)
    veh_block, _ = load_assumptions()
    default_truck_capacity = int(round(veh_block["Capacity"].mean())) if len(veh_block) else 700
except Exception as e:
    st.error(f"⚠️ Could not load vehicle database: {e}")
    st.stop()

# ---------------- SIDEBAR CONFIG ----------------
st.sidebar.header("🔗 Gate-Out Log Source")

DEFAULT_GATE_OUT_SHEET_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vT_DAK4PWeK2FILTKwXEpPkMMUIcj1cQ41e62bM4754358aoCNvDwxEP_RYLeihRB1A_3k3nXIB_wm7/pub?gid=0&single=true&output=csv"

sheet_url = st.sidebar.text_input(
    "Google Sheet CSV link",
    value=DEFAULT_GATE_OUT_SHEET_URL,
    placeholder="https://docs.google.com/spreadsheets/d/.../export?format=csv",
    help="Publish your sheet as CSV: File → Share → Publish to web → select the sheet → CSV format → copy link. "
         "Pre-filled with your default sheet — change it here if you ever need a different one.",
    key="live_gate_sheet_url"
)
as_of_date = st.sidebar.date_input("As of date", value=date.today(), key="live_as_of_date")
refresh = st.sidebar.button("🔄 Refresh Now", use_container_width=True)

with st.sidebar.expander("📋 Required gate-out log columns"):
    st.markdown("""
    - **Vehicle Number** — must match your Vehicle Database
    - **Ownership** — `Own` or `Fixed`
    - **Gate Out Date**
    - **Actual Return Date** — leave blank until it's back
    - *Expected Return Date*, *Route / Distributor* (optional)
    """)
    template = pd.DataFrame({
        "Vehicle Number": [veh_db.iloc[0]["Vehicle Number"] if len(veh_db) else "UP32AB1234",
                           veh_db.iloc[1]["Vehicle Number"] if len(veh_db) > 1 else "UP32CD5678"],
        "Ownership": ["Own", "Fixed"],
        "Gate Out Date": [str(date.today()), str(date.today())],
        "Expected Return Date": ["", ""],
        "Actual Return Date": ["", ""],
        "Route / Distributor": ["", ""],
    })
    st.download_button(
        "⬇️ Download sheet template (CSV)",
        template.to_csv(index=False).encode("utf-8"),
        file_name="gate_out_log_template.csv",
        mime="text/csv"
    )

st.sidebar.write("---")
st.sidebar.header("📦 Today's Load")
load_mode = st.sidebar.radio("Load input method", ["Manual Entry", "From Google Sheet"], key="live_load_mode")

load_sheet_url = None
if load_mode == "From Google Sheet":
    DEFAULT_LOAD_LOG_SHEET_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTb3o2Igm4sqyRwxdC4G3XA-C3M8tGp20SWKxqqxAcgJVjOdq33K5d7ARQmjIqlw9y7847Qw4qhDdVJ/pub?output=csv"
    load_sheet_url = st.sidebar.text_input(
        "Load Log Sheet CSV link",
        value=DEFAULT_LOAD_LOG_SHEET_URL,
        placeholder="https://docs.google.com/spreadsheets/d/.../export?format=csv",
        help="Needs columns: Date, Route / Distributor, Total Load (cases). One row PER SHIPMENT — "
             "multiple rows can share the same date. Pre-filled with your default sheet.",
        key="live_load_sheet_url"
    )
    with st.sidebar.expander("📋 Load Log sheet columns"):
        st.markdown("- **Date**\n- **Route / Distributor**\n- **Total Load (cases)**\n\n"
                    "One row per individual shipment, not one lump total per day — real loads are "
                    "different sizes, and each needs matching to the right truck.")
        load_template = pd.DataFrame({
            "Date": [str(date.today())] * 3,
            "Route / Distributor": ["Route A", "Route B", "Route C"],
            "Total Load (cases)": [600, 250, 3000],
        })
        st.download_button(
            "⬇️ Download load log template (CSV)",
            load_template.to_csv(index=False).encode("utf-8"),
            file_name="load_log_template.csv",
            mime="text/csv"
        )

st.sidebar.write("---")
st.sidebar.write("**Truck size ↔ Case capacity** (editable)")
edited_veh_block_live = st.sidebar.data_editor(
    veh_block, num_rows="dynamic", use_container_width=True, key="veh_block_live"
)
buffer_cases_live = st.sidebar.number_input(
    "Overload buffer per truck (cases)", min_value=0, value=100, step=10, key="buffer_live",
    help="Extra cases a truck can carry beyond its rated capacity before a second truck is added."
)
max_tonnage_live = st.sidebar.number_input(
    "Max tonnage available today (optional cap)", min_value=0, value=0, step=1, key="max_tonnage_live",
    help="Leave at 0 for no cap — uses the largest size in the table above if needed."
)

# ---------------- LOAD GATE-OUT LOG ----------------
@st.cache_data(ttl=60, show_spinner=False)
def fetch_sheet(url):
    return pd.read_csv(url)

log_df = None
data_source_label = None

if sheet_url:
    if refresh:
        fetch_sheet.clear()
    try:
        log_df = fetch_sheet(sheet_url)
        data_source_label = "🟢 Live — Google Sheet"
    except Exception as e:
        st.error(
            f"⚠️ Couldn't read that sheet ({e}). Make sure it's **published to the web as CSV** "
            f"(File → Share → Publish to web → CSV) and the link is the export CSV link. "
            f"Using manual entry below in the meantime."
        )

LOCAL_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "local_data")
LOCAL_LOG_PATH = os.path.join(LOCAL_LOG_DIR, "manual_gate_out_log.csv")

if log_df is None:
    st.info("💡 No Google Sheet connected yet — enter today's gate-outs manually below (saved locally so "
             "it's remembered day to day), or add a sheet link in the sidebar for the more reliable option.")
    data_source_label = "🟡 Manual entry (locally saved)"
    if "manual_log_df" not in st.session_state:
        if os.path.exists(LOCAL_LOG_PATH):
            try:
                st.session_state.manual_log_df = pd.read_csv(LOCAL_LOG_PATH)
            except Exception:
                st.session_state.manual_log_df = pd.DataFrame({
                    "Vehicle Number": [], "Ownership": [], "Gate Out Date": [],
                    "Expected Return Date": [], "Actual Return Date": [], "Route / Distributor": []
                })
        else:
            st.session_state.manual_log_df = pd.DataFrame({
                "Vehicle Number": [], "Ownership": [], "Gate Out Date": [],
                "Expected Return Date": [], "Actual Return Date": [], "Route / Distributor": []
            })
    log_df = st.data_editor(
        st.session_state.manual_log_df, num_rows="dynamic", use_container_width=True,
        key="manual_log_editor",
        column_config={"Ownership": st.column_config.SelectboxColumn(options=["Own", "Fixed"])}
    )
    save_col1, save_col2 = st.columns([1, 4])
    with save_col1:
        if st.button("💾 Save entries", use_container_width=True):
            os.makedirs(LOCAL_LOG_DIR, exist_ok=True)
            log_df.to_csv(LOCAL_LOG_PATH, index=False)
            st.session_state.manual_log_df = log_df
            st.success("Saved — this will be remembered next time you open this page.")
    with save_col2:
        st.caption("⚠️ Locally saved entries persist across page visits on this deployment, but may reset "
                   "if the app restarts/redeploys. A Google Sheet is the more durable option for daily use.")

st.caption(f"Gate-out data source: **{data_source_label}**" + (" — refreshes automatically every 60s" if sheet_url else ""))

# ---------------- PROCESS LOG + CROSS-REFERENCE ----------------
required_cols = {"Vehicle Number", "Ownership", "Gate Out Date"}
has_log_data = (log_df is not None and not log_df.empty and
                required_cols.issubset(set(str(c).strip() for c in log_df.columns)))

if has_log_data:
    bad_ownership_values = validate_ownership_values(log_df)
    if bad_ownership_values:
        st.error(
            f"🚫 **Data problem in your sheet's Ownership column:** found value(s) {bad_ownership_values} "
            f"that aren't **Own** or **Fixed**. Those rows won't count toward availability until fixed — "
            f"a common mistake is typing the column header text into the cells by accident. Please correct "
            f"the Ownership column in your Google Sheet to say exactly `Own` or `Fixed`."
        )
    processed_df, currently_out_df = process_gate_out_log(log_df, as_of_date)
else:
    currently_out_df = pd.DataFrame(columns=["Vehicle Number", "Ownership", "Days Out"])
    st.info("ℹ️ No gate-out entries yet — showing full fleet as available.")

fleet_status_df, unmatched_df = cross_reference_fleet(veh_db, currently_out_df)

if not unmatched_df.empty:
    st.warning(f"⚠️ **{len(unmatched_df)} gate-out entries** have a Vehicle Number that doesn't match your "
               f"Vehicle Database — check for typos. These are excluded from the counts below.")
    with st.expander("See unmatched entries"):
        st.dataframe(unmatched_df, use_container_width=True, hide_index=True)

own_available_df = fleet_status_df[(fleet_status_df["OwnershipType"] == "Own") & (fleet_status_df["Status"] == "Available")]
fixed_available_df = fleet_status_df[(fleet_status_df["OwnershipType"] == "Fixed") & (fleet_status_df["Status"] == "Available")]
own_out_df = fleet_status_df[(fleet_status_df["OwnershipType"] == "Own") & (fleet_status_df["Status"] == "Out")]
fixed_out_df = fleet_status_df[(fleet_status_df["OwnershipType"] == "Fixed") & (fleet_status_df["Status"] == "Out")]

own_available = len(own_available_df)
fixed_available = len(fixed_available_df)
own_out = len(own_out_df)
fixed_out = len(fixed_out_df)

st.write("---")
st.header(f"📊 Live Fleet Status — {as_of_date.strftime('%d %b %Y')}")

k1, k2, k3, k4 = st.columns(4)
k1.metric("🟦 Own Available", f"{own_available:,} / {own_available + own_out:,}",
          delta=f"-{own_out} out" if own_out else None, delta_color="inverse")
k2.metric("🟧 Fixed/Bachat Available", f"{fixed_available:,} / {fixed_available + fixed_out:,}",
          delta=f"-{fixed_out} out" if fixed_out else None, delta_color="inverse")
k3.metric("Total Available Now", f"{own_available + fixed_available:,}")
k4.metric("Vehicles Currently Out", f"{own_out + fixed_out:,}")

c1, c2 = st.columns(2)
with c1:
    st.subheader("🚦 Available vs Out")
    mix_df = pd.DataFrame({
        "Ownership": ["Own", "Own", "Fixed", "Fixed"],
        "Status": ["Available", "Out", "Available", "Out"],
        "Count": [own_available, own_out, fixed_available, fixed_out],
    })
    fig = px.bar(mix_df, x="Ownership", y="Count", color="Status", barmode="stack",
                 color_discrete_map={"Available": "#2ecc71", "Out": "#d93838"})
    fig.update_layout(height=340, margin=dict(t=10))
    st.plotly_chart(fig, use_container_width=True)

with c2:
    st.subheader("📍 Available Vehicles by Location")
    avail_all = fleet_status_df[fleet_status_df["Status"] == "Available"]
    if not avail_all.empty:
        loc_df = avail_all.groupby(["Location", "OwnershipType"]).size().reset_index(name="Available")
        fig2 = px.bar(loc_df, x="Location", y="Available", color="OwnershipType", barmode="stack")
        fig2.update_layout(height=340, margin=dict(t=10))
        st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info("No vehicles currently available.")

st.write("---")

# ---------------- DAY-OVER-DAY HISTORY TREND ----------------
st.subheader("📈 Fleet Availability Trend")
st.caption("Computed from your full gate-out log history — so today's plan is informed by what actually "
           "happened on previous days, not just a single snapshot.")
if has_log_data:
    trend_days = st.slider("Days to show", 3, 30, 14, key="trend_days")
    trend_rows = []
    for i in range(trend_days - 1, -1, -1):
        d = as_of_date - pd.Timedelta(days=i)
        _, out_d = process_gate_out_log(log_df, d)
        status_d, _ = cross_reference_fleet(veh_db, out_d)
        own_avail_d = len(status_d[(status_d["OwnershipType"] == "Own") & (status_d["Status"] == "Available")])
        fixed_avail_d = len(status_d[(status_d["OwnershipType"] == "Fixed") & (status_d["Status"] == "Available")])
        trend_rows.append({"Date": d, "Own Available": own_avail_d, "Fixed Available": fixed_avail_d})
    trend_df = pd.DataFrame(trend_rows)
    trend_melted = trend_df.melt(id_vars="Date", value_vars=["Own Available", "Fixed Available"],
                                  var_name="Fleet", value_name="Available")
    fig3 = px.line(trend_melted, x="Date", y="Available", color="Fleet", markers=True)
    fig3.update_layout(height=340, margin=dict(t=10))
    st.plotly_chart(fig3, use_container_width=True)
    st.caption("💡 This is why keeping every gate-out (and its return) logged with a date matters — the "
               "more history in your sheet, the more useful this trend becomes for planning tomorrow.")
else:
    st.info("Once you have a few days of gate-out entries logged (with dates), this will show the trend "
             "automatically — no extra setup needed.")

st.write("---")

# ---------------- TODAY'S LOAD → TRUCKS → SPOT HIRE ----------------
with st.container(border=True):
    st.subheader("🚚 Today's Load → Trucks Needed → Spot Hire")
    st.caption("Enter each distributor/route's load separately — real dispatch is many different-sized "
               "shipments, not one lump total, and each needs its own appropriately-sized truck matched "
               "against what you actually have available (not just a total capacity assumption).")

    shipments_df = None
    load_source_note = ""

    if load_mode == "Manual Entry":
        if "manual_shipments_df" not in st.session_state:
            st.session_state.manual_shipments_df = pd.DataFrame({
                "Route / Distributor": [""], "Load (cases)": [0]
            })
        shipments_df = st.data_editor(
            st.session_state.manual_shipments_df, num_rows="dynamic", use_container_width=True,
            key="manual_shipments_editor",
            column_config={"Load (cases)": st.column_config.NumberColumn(min_value=0, step=100)}
        )
        load_source_note = "manual entry"
    else:
        if load_sheet_url:
            try:
                load_log_df = fetch_sheet(load_sheet_url)
                load_log_df.columns = [str(c).strip() for c in load_log_df.columns]
                load_log_df["Date"] = pd.to_datetime(load_log_df["Date"], errors="coerce", dayfirst=True).dt.date
                match = load_log_df[load_log_df["Date"] == as_of_date]
                if not match.empty:
                    rename_map = {"Total Load (cases)": "Load (cases)"}
                    match = match.rename(columns=rename_map)
                    if "Route / Distributor" not in match.columns:
                        match["Route / Distributor"] = [f"Row {i+1}" for i in range(len(match))]
                    shipments_df = match[["Route / Distributor", "Load (cases)"]].reset_index(drop=True)
                    load_source_note = "Google Sheet"
                    st.dataframe(shipments_df, use_container_width=True, hide_index=True)
                else:
                    st.warning(f"⚠️ No rows found for {as_of_date} in the Load Log sheet — enter manually below.")
            except Exception as e:
                st.error(f"⚠️ Couldn't read Load Log sheet ({e}).")
        else:
            st.info("Add a Load Log Sheet link in the sidebar, or switch to Manual Entry.")

        if shipments_df is None:
            if "fallback_shipments_df" not in st.session_state:
                st.session_state.fallback_shipments_df = pd.DataFrame({
                    "Route / Distributor": [""], "Load (cases)": [0]
                })
            shipments_df = st.data_editor(
                st.session_state.fallback_shipments_df, num_rows="dynamic", use_container_width=True,
                key="fallback_shipments_editor",
                column_config={"Load (cases)": st.column_config.NumberColumn(min_value=0, step=100)}
            )
            load_source_note = "manual fallback"

    shipment_loads = [x for x in shipments_df["Load (cases)"].tolist() if pd.notna(x) and x > 0] if shipments_df is not None else []
    total_load_today = int(sum(shipment_loads))

    alloc_results = allocate_shipments_to_fleet(
        shipment_loads, fleet_status_df, edited_veh_block_live,
        buffer=buffer_cases_live, max_tonnage=max_tonnage_live if max_tonnage_live > 0 else None
    )
    alloc_results_df = pd.DataFrame(alloc_results)

    trucks_needed_today = len(alloc_results)
    own_used_today = int((alloc_results_df["Source"] == "Own").sum()) if not alloc_results_df.empty else 0
    fixed_used_today = int((alloc_results_df["Source"] == "Fixed").sum()) if not alloc_results_df.empty else 0
    spot_needed_today = int((alloc_results_df["Source"] == "Spot Hire").sum()) if not alloc_results_df.empty else 0

    st.caption(f"Load source: **{load_source_note}** · {len(shipment_loads)} shipments totaling "
               f"{total_load_today:,} cases (with a {buffer_cases_live}-case/truck buffer) → "
               f"**{trucks_needed_today:,} trucks needed today**, matched against your real available fleet.")

    d1, d2, d3, d4 = st.columns(4)
    d1.metric("Trucks Needed", f"{trucks_needed_today:,}")
    d2.metric("🟦 Own Used", f"{own_used_today:,}")
    d3.metric("🟧 Fixed Used", f"{fixed_used_today:,}")
    d4.metric("🟥 Spot Hire Needed NOW", f"{spot_needed_today:,}")

    if trucks_needed_today > 0:
        st.dataframe(alloc_results_df, use_container_width=True, hide_index=True)
        if spot_needed_today > 0:
            st.warning(f"⚠️ Arrange **{spot_needed_today} spot hire vehicles** today — your Own/Fixed fleet "
                       f"doesn't have enough AVAILABLE vehicles of the right size for {spot_needed_today} "
                       f"of these shipments (not just a raw count shortfall — checked vehicle-by-vehicle "
                       f"against actual tonnage on hand).")
        else:
            st.success("✅ Own + Fixed availability — with the right tonnage on hand — covers today's need. "
                       "No spot hire required.")

st.write("---")

# ---------------- VEHICLE-LEVEL DETAIL ----------------
c1, c2 = st.columns(2)
with c1:
    st.subheader("✅ Available Vehicles (ready to dispatch)")
    avail_cols = [c for c in ["Vehicle Number", "OwnershipType", "Location", "Transporter Name", "CapacityTonnage"]
                  if c in fleet_status_df.columns]
    avail_show = fleet_status_df[fleet_status_df["Status"] == "Available"][avail_cols]
    st.dataframe(avail_show, use_container_width=True, height=320, hide_index=True)
    st.download_button("⬇️ Download available vehicles (CSV)", avail_show.to_csv(index=False).encode("utf-8"),
                        file_name=f"available_vehicles_{as_of_date}.csv", mime="text/csv")

with c2:
    st.subheader("🚫 Vehicles Currently Out")
    out_cols = [c for c in ["Vehicle Number", "OwnershipType", "Location", "Transporter Name", "Days Out"]
                if c in fleet_status_df.columns]
    out_show = fleet_status_df[fleet_status_df["Status"] == "Out"][out_cols]
    if "Days Out" in out_show.columns:
        out_show = out_show.sort_values("Days Out", ascending=False)
    st.dataframe(out_show, use_container_width=True, height=320, hide_index=True)

with st.expander("ℹ️ How this works"):
    st.markdown("""
    1. Your transport office logs every gate-out in a shared Google Sheet (Vehicle Number, Ownership,
       Gate Out Date), filling in **Actual Return Date** once a truck is back.
    2. This page cross-references those Vehicle Numbers against your **actual Vehicle Database**, so
       every truck is tracked individually — not just as an aggregate count. Unrecognized vehicle
       numbers (typos, unregistered trucks) are flagged separately.
    3. **Available = registered fleet − currently out**, giving real intraday status.
    4. For **Today's Load**, enter each shipment separately (route/distributor + cases) rather than one
       lump total — real dispatch is many different-sized loads, not one uniform number. Each shipment
       is matched to the smallest truck size that covers it (+ your overload buffer), then assigned to
       the smallest AVAILABLE real vehicle of at least that size — Own first, then Fixed, then Spot Hire
       — checked **vehicle by vehicle against actual tonnage on hand**, not just a headcount. This catches
       cases a simple count could miss, like needing 20T trucks that Own doesn't actually own any of.

    **Publishing a Google Sheet as CSV:** open the sheet → File → Share → Publish to web → choose the
    correct tab → format **Comma-separated values (.csv)** → Publish → copy the link into the sidebar.
    """)
