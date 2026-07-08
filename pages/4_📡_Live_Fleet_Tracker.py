import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import sys
import os
import io
from datetime import date

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.data_loader import load_vehicle_database, fleet_totals_by_ownership, process_gate_out_log

st.set_page_config(page_title="Live Fleet Tracker", page_icon="📡", layout="wide")
st.markdown("<style>.block-container { padding-top: 1rem; padding-bottom: 0.5rem; }</style>", unsafe_allow_html=True)

st.title("📡 Live Fleet Tracker")
st.caption("Reads your transport office's gate-out log (a Google Sheet) to show real available Own/Fixed "
           "fleet right now — for same-day spot hire decisions instead of theoretical TAT assumptions.")
st.write("---")

try:
    veh_db = load_vehicle_database()
    own_total_default, fixed_total_default = fleet_totals_by_ownership(veh_db)
except Exception as e:
    st.error(f"⚠️ Could not load vehicle database: {e}")
    st.stop()

# ---------------- SIDEBAR CONFIG ----------------
st.sidebar.header("⚙️ Fleet Settings")
own_total = st.sidebar.number_input("Total Own Fleet (trucks)", min_value=0, value=own_total_default, step=1, key="own_live")
fixed_total = st.sidebar.number_input("Total Fixed/Bachat Fleet (trucks)", min_value=0, value=fixed_total_default, step=1, key="fixed_live")

st.sidebar.write("---")
st.sidebar.header("🔗 Data Source")
sheet_url = st.sidebar.text_input(
    "Google Sheet CSV link",
    placeholder="https://docs.google.com/spreadsheets/d/.../export?format=csv",
    help="Publish your sheet as CSV: File → Share → Publish to web → select the sheet → CSV format → copy link."
)
as_of_date = st.sidebar.date_input("As of date", value=date.today())
refresh = st.sidebar.button("🔄 Refresh Now", use_container_width=True)

st.sidebar.write("---")
with st.sidebar.expander("📋 Required sheet columns"):
    st.markdown("""
    - **Vehicle Number**
    - **Ownership** — `Own` or `Fixed`
    - **Gate Out Date** — when the truck left
    - **Actual Return Date** — leave blank until it's back
    - *Expected Return Date* (optional)
    - *Route / Distributor* (optional, for reference)
    """)
    template = pd.DataFrame({
        "Vehicle Number": ["UP32AB1234", "UP32CD5678"],
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

# ---------------- LOAD LIVE DATA ----------------
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
            f"(File → Share → Publish to web → CSV) and the link is the export CSV link, not the normal "
            f"share link. Using manual entry below in the meantime."
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
        column_config={
            "Ownership": st.column_config.SelectboxColumn(options=["Own", "Fixed"]),
        }
    )

st.caption(f"Data source: **{data_source_label}**" + (f" — refreshes automatically every 60s" if sheet_url else ""))

# ---------------- PROCESS LOG ----------------
required_cols = {"Vehicle Number", "Ownership", "Gate Out Date"}
if log_df is None or log_df.empty or not required_cols.issubset(set(str(c).strip() for c in log_df.columns)):
    st.warning("⚠️ Waiting for gate-out data with at least: Vehicle Number, Ownership, Gate Out Date.")
    st.stop()

processed_df, currently_out_df = process_gate_out_log(log_df, as_of_date)

own_out = int((currently_out_df["Ownership"] == "Own").sum())
fixed_out = int((currently_out_df["Ownership"] == "Fixed").sum())
own_available = max(0, int(own_total) - own_out)
fixed_available = max(0, int(fixed_total) - fixed_out)

st.write("---")
st.header(f"📊 Live Fleet Status — {as_of_date.strftime('%d %b %Y')}")

k1, k2, k3, k4 = st.columns(4)
k1.metric("🟦 Own Available", f"{own_available:,}", delta=f"-{own_out} out" if own_out else None, delta_color="inverse")
k2.metric("🟧 Fixed/Bachat Available", f"{fixed_available:,}", delta=f"-{fixed_out} out" if fixed_out else None, delta_color="inverse")
k3.metric("Total Available Now", f"{own_available + fixed_available:,}")
k4.metric("Vehicles Currently Out", f"{len(currently_out_df):,}")

st.write("")

# ---------------- LIVE SPOT HIRE DECISION ----------------
with st.container(border=True):
    st.subheader("🚦 Same-Day Spot Hire Decision")
    trucks_needed_today = st.number_input("Trucks needed today (from your dispatch plan)", min_value=0, value=0, step=1)
    own_used_today = min(trucks_needed_today, own_available)
    remaining = trucks_needed_today - own_used_today
    fixed_used_today = min(remaining, fixed_available)
    remaining -= fixed_used_today
    spot_needed_today = max(0, remaining)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Trucks Needed", f"{trucks_needed_today:,}")
    c2.metric("🟦 Own Used", f"{own_used_today:,}")
    c3.metric("🟧 Fixed Used", f"{fixed_used_today:,}")
    c4.metric("🟥 Spot Hire Needed NOW", f"{spot_needed_today:,}")

    if trucks_needed_today > 0:
        if spot_needed_today > 0:
            st.warning(f"⚠️ Arrange **{spot_needed_today} spot hire vehicles** today — Own + Fixed fleet "
                       f"is short based on current gate-out status.")
        else:
            st.success("✅ Own + Fixed availability covers today's need — no spot hire required.")

st.write("---")

# ---------------- CURRENTLY OUT DETAIL ----------------
c1, c2 = st.columns(2)
with c1:
    st.subheader("🚚 Vehicles Currently Out")
    if not currently_out_df.empty:
        show_cols = [c for c in ["Vehicle Number", "Ownership", "Gate Out Date", "Days Out",
                                  "Expected Return Date", "Route / Distributor"] if c in currently_out_df.columns]
        st.dataframe(currently_out_df[show_cols].sort_values("Days Out", ascending=False),
                     use_container_width=True, height=350, hide_index=True)
    else:
        st.info("No vehicles currently out as of this date.")

with c2:
    st.subheader("📈 Days-Out Distribution")
    if not currently_out_df.empty:
        fig = px.histogram(currently_out_df, x="Days Out", color="Ownership", barmode="stack", nbins=10)
        fig.update_layout(height=350, margin=dict(t=10))
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Nothing to chart yet.")

with st.expander("ℹ️ How this works"):
    st.markdown("""
    1. Your transport office logs every gate-out in a shared Google Sheet (Vehicle Number, Ownership,
       Gate Out Date), and fills in **Actual Return Date** once a truck is back.
    2. This page reads that sheet live (auto-refreshing every 60 seconds, or on demand via **Refresh Now**)
       and marks a vehicle **"Currently Out"** if it left on/before the selected date and hasn't returned yet.
    3. **Available = Total Fleet − Currently Out**, giving you real intraday fleet status instead of a
       theoretical turn-around-time estimate.
    4. Enter today's total truck requirement in the **Same-Day Spot Hire Decision** box to get an immediate
       Own → Fixed → Spot Hire breakdown for right now.

    **Publishing your Google Sheet as CSV:** open the sheet → File → Share → Publish to web → choose the
    correct tab → format **Comma-separated values (.csv)** → Publish → copy the link into the sidebar.
    """)
