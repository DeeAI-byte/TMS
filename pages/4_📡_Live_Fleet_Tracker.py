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
    process_gate_out_log, cross_reference_fleet
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
sheet_url = st.sidebar.text_input(
    "Google Sheet CSV link",
    placeholder="https://docs.google.com/spreadsheets/d/.../export?format=csv",
    help="Publish your sheet as CSV: File → Share → Publish to web → select the sheet → CSV format → copy link."
)
as_of_date = st.sidebar.date_input("As of date", value=date.today())
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
load_mode = st.sidebar.radio("Load input method", ["Manual Entry", "From Google Sheet"])

load_sheet_url = None
if load_mode == "From Google Sheet":
    load_sheet_url = st.sidebar.text_input(
        "Load Log Sheet CSV link",
        placeholder="https://docs.google.com/spreadsheets/d/.../export?format=csv",
        help="Needs columns: Date, Total Load (cases). One row per day."
    )
    with st.sidebar.expander("📋 Load Log sheet columns"):
        st.markdown("- **Date**\n- **Total Load (cases)**")
        load_template = pd.DataFrame({"Date": [str(date.today())], "Total Load (cases)": [50000]})
        st.download_button(
            "⬇️ Download load log template (CSV)",
            load_template.to_csv(index=False).encode("utf-8"),
            file_name="load_log_template.csv",
            mime="text/csv"
        )

truck_capacity = st.sidebar.number_input(
    "Avg truck capacity (cases/truck)", min_value=1, value=default_truck_capacity, step=10,
    help="Used to convert today's case load into trucks needed."
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

if log_df is None:
    st.info("💡 No Google Sheet connected yet — enter today's gate-outs manually below, or add a "
             "sheet link in the sidebar once your transport office sets one up.")
    data_source_label = "🟡 Manual entry"
    if "manual_log_df" not in st.session_state:
        st.session_state.manual_log_df = pd.DataFrame({
            "Vehicle Number": [], "Ownership": [], "Gate Out Date": [],
            "Expected Return Date": [], "Actual Return Date": [], "Route / Distributor": []
        })
    log_df = st.data_editor(
        st.session_state.manual_log_df, num_rows="dynamic", use_container_width=True,
        key="manual_log_editor",
        column_config={"Ownership": st.column_config.SelectboxColumn(options=["Own", "Fixed"])}
    )

st.caption(f"Gate-out data source: **{data_source_label}**" + (" — refreshes automatically every 60s" if sheet_url else ""))

# ---------------- PROCESS LOG + CROSS-REFERENCE ----------------
required_cols = {"Vehicle Number", "Ownership", "Gate Out Date"}
has_log_data = (log_df is not None and not log_df.empty and
                required_cols.issubset(set(str(c).strip() for c in log_df.columns)))

if has_log_data:
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

# ---------------- TODAY'S LOAD → TRUCKS → SPOT HIRE ----------------
with st.container(border=True):
    st.subheader("🚚 Today's Load → Trucks Needed → Spot Hire")

    load_today = 0
    load_source_note = ""
    if load_mode == "Manual Entry":
        load_today = st.number_input("Total load today (cases)", min_value=0, value=0, step=1000)
        load_source_note = "manual entry"
    else:
        if load_sheet_url:
            try:
                load_log_df = fetch_sheet(load_sheet_url)
                load_log_df.columns = [str(c).strip() for c in load_log_df.columns]
                load_log_df["Date"] = pd.to_datetime(load_log_df["Date"], errors="coerce").dt.date
                match = load_log_df[load_log_df["Date"] == as_of_date]
                if not match.empty:
                    load_today = int(match.iloc[0]["Total Load (cases)"])
                    load_source_note = "Google Sheet"
                else:
                    st.warning(f"⚠️ No row found for {as_of_date} in the Load Log sheet — enter manually below.")
                    load_today = st.number_input("Total load today (cases) — fallback", min_value=0, value=0, step=1000)
                    load_source_note = "manual fallback"
            except Exception as e:
                st.error(f"⚠️ Couldn't read Load Log sheet ({e}). Enter manually below.")
                load_today = st.number_input("Total load today (cases) — fallback", min_value=0, value=0, step=1000)
                load_source_note = "manual fallback"
        else:
            st.info("Add a Load Log Sheet link in the sidebar, or switch to Manual Entry.")
            load_today = st.number_input("Total load today (cases) — fallback", min_value=0, value=0, step=1000)
            load_source_note = "manual fallback"

    trucks_needed_today = int(math.ceil(load_today / truck_capacity)) if truck_capacity > 0 else 0
    st.caption(f"Load source: **{load_source_note}** · {load_today:,} cases ÷ {truck_capacity:,} cases/truck "
               f"→ **{trucks_needed_today:,} trucks needed today**")

    own_used_today = min(trucks_needed_today, own_available)
    remaining = trucks_needed_today - own_used_today
    fixed_used_today = min(remaining, fixed_available)
    remaining -= fixed_used_today
    spot_needed_today = max(0, remaining)

    d1, d2, d3, d4 = st.columns(4)
    d1.metric("Trucks Needed", f"{trucks_needed_today:,}")
    d2.metric("🟦 Own Used", f"{own_used_today:,}")
    d3.metric("🟧 Fixed Used", f"{fixed_used_today:,}")
    d4.metric("🟥 Spot Hire Needed NOW", f"{spot_needed_today:,}")

    if trucks_needed_today > 0:
        if spot_needed_today > 0:
            st.warning(f"⚠️ Arrange **{spot_needed_today} spot hire vehicles** today — Own + Fixed available "
                       f"fleet ({own_available + fixed_available}) is short of the {trucks_needed_today:,} needed.")
        else:
            st.success("✅ Own + Fixed availability covers today's need — no spot hire required.")

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
    4. For **Today's Load**, enter cases manually or connect a second "Load Log" Google Sheet
       (Date, Total Load (cases)) — the app converts that into trucks needed using your truck
       capacity assumption, then applies Own → Fixed → Spot Hire priority using the *live* available
       counts above (not a theoretical turn-around-time estimate).

    **Publishing a Google Sheet as CSV:** open the sheet → File → Share → Publish to web → choose the
    correct tab → format **Comma-separated values (.csv)** → Publish → copy the link into the sidebar.
    """)
