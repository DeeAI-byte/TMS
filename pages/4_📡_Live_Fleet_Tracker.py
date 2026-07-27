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
    allocate_trucks_by_tonnage, allocate_shipments_to_fleet, already_dispatched_routes,
    load_db_capacity, load_gate_out_log_local, save_gate_out_log_local,
    append_gate_out_entries, load_vehicle_status_overrides, save_vehicle_status_overrides,
    apply_vehicle_status_overrides, REMARKS_OPTIONS, GATE_OUT_LOG_COLUMNS,
    get_distributor_list, unmatched_distributor_names
)

st.set_page_config(page_title="Live Fleet Tracker", page_icon="📡", layout="wide")
st.markdown("<style>.block-container { padding-top: 1rem; padding-bottom: 0.5rem; }</style>", unsafe_allow_html=True)

st.title("📡 Live Fleet Tracker")
st.caption("Cross-references your transport office's gate-out log against your actual Vehicle Database — "
           "real available Own/Fixed fleet, right now, plus today's load → trucks → spot hire in one place.")
st.write("---")

try:
    veh_db = load_vehicle_database()
    status_overrides_df = load_vehicle_status_overrides()
    veh_db = apply_vehicle_status_overrides(veh_db, status_overrides_df)
    own_total_default, fixed_total_default = fleet_totals_by_ownership(veh_db)
    veh_block, _ = load_assumptions()
    default_truck_capacity = int(round(veh_block["Capacity"].mean())) if len(veh_block) else 700
    distributor_list = get_distributor_list()
except Exception as e:
    st.error(f"⚠️ Could not load vehicle database: {e}")
    st.stop()

# ---------------- SIDEBAR CONFIG ----------------
DEFAULT_GATE_OUT_SHEET_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vT_DAK4PWeK2FILTKwXEpPkMMUIcj1cQ41e62bM4754358aoCNvDwxEP_RYLeihRB1A_3k3nXIB_wm7/pub?gid=0&single=true&output=csv"

st.sidebar.header("🔗 Gate-Out Log (Vehicle In/Out)")
st.sidebar.caption(
    "Auto-fills whenever you confirm an allotment in **Today's Load** below (including Spot "
    "Hire) — no more copying entries into a separate sheet by hand. Still fully editable on "
    "this page (Actual Return Date, Ownership)."
)
as_of_date = st.sidebar.date_input("As of date", value=date.today(), key="live_as_of_date")

use_external_sheet = st.sidebar.checkbox(
    "Pull in your old gate-out Google Sheet", value=True, key="use_gate_sheet",
    help="Use this to bring in the vehicles that were already logged as out on your old sheet, "
         "so nothing gets lost switching over. You can import them into the log below (one-time), "
         "or just leave this on to keep merging the sheet's rows in every time (read-only here — "
         "edit that sheet directly to fix its rows)."
)
sheet_url = ""
refresh = False
if use_external_sheet:
    sheet_url = st.sidebar.text_input(
        "Google Sheet CSV link",
        value=DEFAULT_GATE_OUT_SHEET_URL,
        placeholder="https://docs.google.com/spreadsheets/d/.../export?format=csv",
        help="Publish your sheet as CSV: File → Share → Publish to web → select the sheet → CSV format → copy link. "
             "Pre-filled with your existing gate-out sheet.",
        key="live_gate_sheet_url"
    )
    refresh = st.sidebar.button("🔄 Refresh Google Sheet", use_container_width=True)

st.sidebar.write("---")
st.sidebar.header("📦 Today's Load")

DEFAULT_LOAD_LOG_SHEET_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTb3o2Igm4sqyRwxdC4G3XA-C3M8tGp20SWKxqqxAcgJVjOdq33K5d7ARQmjIqlw9y7847Qw4qhDdVJ/pub?output=csv"
load_sheet_url = st.sidebar.text_input(
    "Load Log Sheet CSV link",
    value=DEFAULT_LOAD_LOG_SHEET_URL,
    placeholder="https://docs.google.com/spreadsheets/d/.../export?format=csv",
    help="Needs columns: Date, Route / Distributor, Total Load (Ton). One row PER SHIPMENT — "
         "multiple rows can share the same date. Pre-filled with your default sheet.",
    key="live_load_sheet_url"
)
load_refresh = st.sidebar.button("🔄 Refresh Now", use_container_width=True, key="load_refresh_btn")
with st.sidebar.expander("📋 Load Log sheet columns"):
    st.markdown("- **Date**\n- **Route / Distributor**\n- **Total Load (Ton)**\n\n"
                "One row per individual shipment, not one lump total per day — real loads are "
                "different sizes, and each needs matching to the closest available vehicle tonnage.")
    load_template = pd.DataFrame({
        "Date": [str(date.today())] * 3,
        "Route / Distributor": ["Route A", "Route B", "Route C"],
        "Total Load (Ton)": [6, 2.5, 24],
    })
    st.download_button(
        "⬇️ Download load log template (CSV)",
        load_template.to_csv(index=False).encode("utf-8"),
        file_name="load_log_template.csv",
        mime="text/csv"
    )

st.sidebar.write("---")
st.sidebar.write("**Truck sizes (tons)** (editable) — the TonnageNum column is what Today's Load "
                 "matches shipments against; Capacity (cases) is only used elsewhere.")
edited_veh_block_live = st.sidebar.data_editor(
    veh_block, num_rows="dynamic", use_container_width=True, key="veh_block_live"
)
buffer_tons_live = st.sidebar.number_input(
    "Overload buffer per truck (tons)", min_value=0.0, value=1.0, step=0.5, key="buffer_live",
    help="Extra tons a truck can carry beyond its rated tonnage before a second truck is added."
)
max_tonnage_live = st.sidebar.number_input(
    "Max tonnage available today (optional cap)", min_value=0, value=0, step=1, key="max_tonnage_live",
    help="Leave at 0 for no cap — uses the largest size in the table above if needed."
)

# ---------------- LOAD GATE-OUT LOG ----------------
@st.cache_data(ttl=60, show_spinner=False)
def fetch_gate_out_sheet(url):
    return pd.read_csv(url)

@st.cache_data(ttl=60, show_spinner=False)
def fetch_load_sheet(url):
    return pd.read_csv(url)


def filter_daily_load_rows(load_log_df, planning_date):
    load_log_df = load_log_df.copy()
    load_log_df = load_log_df.replace(r'^\s*$', pd.NA, regex=True)
    load_log_df = load_log_df.dropna(how="all")
    load_log_df.columns = [str(c).strip() for c in load_log_df.columns]

    # Find the load column flexibly — sheets in the wild use "Total Load (Ton)",
    # "Total Load (Tons)", "Load (Ton)", "Total Load (MT)", different casing/spacing, etc.
    load_col = None
    for c in load_log_df.columns:
        key = " ".join(str(c).strip().lower().replace("(", " ").replace(")", " ").split())
        if key in ("total load ton", "total load tons", "load ton", "load tons",
                   "total load mt", "load mt", "total load"):
            load_col = c
            break
    if load_col is not None and load_col != "Load (Ton)":
        load_log_df = load_log_df.rename(columns={load_col: "Load (Ton)"})

    date_col = None
    for c in load_log_df.columns:
        key = str(c).strip().lower()
        if key in ("date", "ship date", "shipment date", "order date", "dispatch date"):
            date_col = c
            break
    if date_col is None:
        raise ValueError("Load Log sheet must contain a Date column (e.g. 'Date').")
    if date_col != "Date":
        load_log_df = load_log_df.rename(columns={date_col: "Date"})

    load_log_df["Date"] = pd.to_datetime(load_log_df["Date"], errors="coerce", dayfirst=True).dt.date
    if "Route / Distributor" not in load_log_df.columns:
        load_log_df["Route / Distributor"] = [f"Row {i+1}" for i in range(len(load_log_df))]

    daily_rows = load_log_df[load_log_df["Date"] == planning_date].copy()
    if "Load (Ton)" not in daily_rows.columns:
        raise ValueError("Load Log sheet must contain a load-in-tons column, e.g. 'Total Load (Ton)'.")

    # Find the status column flexibly — sheets in the wild use "Dispatch Status",
    # "Status", "Dispatched?", extra spacing, different casing, etc. Matching only
    # the exact literal header "Dispatch Status" silently fails on any of those and
    # falls back to labeling every row "Pending" regardless of the real cell value.
    status_col = None
    for c in daily_rows.columns:
        key = " ".join(str(c).strip().lower().replace("?", "").split())
        if key in ("dispatch status", "status", "dispatched status", "delivery status"):
            status_col = c
            break

    if status_col is not None:
        normalized_status = (
            daily_rows[status_col]
            .astype(str)
            .replace(r'^\s*$', "Pending", regex=True)
            .replace(r'(?i)^(nan|none)$', "Pending", regex=True)
            .str.strip()
            .str.title()
        )
        if status_col != "Status":
            daily_rows = daily_rows.drop(columns=[status_col])
        daily_rows["Status"] = normalized_status
    else:
        daily_rows["Status"] = "Pending"

    # Extract the numeric portion regardless of a trailing unit — sheets in the wild write
    # "4T", "3.3T", "4 Ton", plain "4", etc. A straight pd.to_numeric() chokes on any of the
    # unit-suffixed forms and silently turns the whole column to NaN, which then filters out
    # EVERY row here even though the sheet clearly has valid loads for today.
    daily_rows["Load (Ton)"] = pd.to_numeric(
        daily_rows["Load (Ton)"].astype(str).str.extract(r'([\d]*\.?[\d]+)')[0],
        errors="coerce"
    )
    daily_rows = daily_rows[daily_rows["Load (Ton)"] > 0]
    return daily_rows[["Route / Distributor", "Load (Ton)", "Status"]].reset_index(drop=True)


# ---------------- LOAD LOCAL "VEHICLE OUT" LOG (auto-populated) + OPTIONAL SHEET ----------------
local_log_df = load_gate_out_log_local()

external_log_df = None
if use_external_sheet and sheet_url:
    if refresh:
        fetch_gate_out_sheet.clear()
    try:
        external_log_df = fetch_gate_out_sheet(sheet_url)
        if external_log_df is not None and not external_log_df.empty:
            # Treat rows that are completely blank or whitespace-only as missing data,
            # so an empty Google Sheet with only headers doesn't produce phantom out vehicles.
            external_log_df = external_log_df.replace(r'^\s*$', pd.NA, regex=True)
            external_log_df = external_log_df.dropna(how="all")
    except Exception as e:
        st.error(
            f"⚠️ Couldn't read that sheet ({e}). Make sure it's **published to the web as CSV** "
            f"(File → Share → Publish to web → CSV) and the link is the export CSV link. "
            f"Continuing with just your auto-logged entries below."
        )

if external_log_df is not None and not external_log_df.empty:
    external_log_df.columns = [str(c).strip() for c in external_log_df.columns]
    for c in ["Vehicle Number", "Ownership", "Truck Size (T)", "Gate Out Date",
              "Actual Return Date", "Route / Distributor"]:
        if c not in external_log_df.columns:
            external_log_df[c] = ""
    st.info(
        f"📥 Your old gate-out sheet has **{len(external_log_df)}** row(s) — currently just merged "
        f"in for viewing below. Click **Import into backend** once to copy them permanently into "
        f"this app's own log (so the vehicles already out won't get lost as you move off the sheet)."
    )
    _bad_log_dist_names = unmatched_distributor_names(external_log_df["Route / Distributor"])
    if _bad_log_dist_names:
        st.warning(
            f"⚠️ **{len(_bad_log_dist_names)} distributor name(s) in that sheet don't match your master "
            f"distributor list**: {', '.join(_bad_log_dist_names[:10])}" +
            (", …" if len(_bad_log_dist_names) > 10 else "") +
            " — worth a quick cleanup in the sheet so they line up consistently once imported."
        )
    if st.button("📥 Import these rows into the backend (one-time)", key="import_sheet_btn"):
        append_gate_out_entries(external_log_df[GATE_OUT_LOG_COLUMNS])
        st.success(
            f"Imported {len(external_log_df)} row(s) into the local Vehicle Out log — you can turn "
            f"off the Google Sheet checkbox in the sidebar now if you don't need it anymore."
        )
        st.rerun()
    base_log_df = pd.concat([local_log_df, external_log_df], ignore_index=True, sort=False)
    data_source_label = "🟢 Auto-logged + Google Sheet (merged)"
else:
    base_log_df = local_log_df.copy()
    data_source_label = "🟦 Auto-logged from allotments"

st.write("---")
st.subheader("🚚 Vehicle Out — Gate-Out / Return Log")
st.caption(
    "Fills in automatically as soon as you confirm an allotment in **Today's Load** below "
    "(Vehicle Number, Ownership, Truck Size, Gate Out Date, Distributor — including Spot Hire, "
    "shown as **market** in Vehicle Number). Edit **Actual Return Date** once a truck is back, "
    "or fix **Ownership** — same as manual entry before, just seeded for you instead of starting blank."
)
log_distributor_options = sorted(set(distributor_list) | {""} | set(
    str(v).strip() for v in base_log_df["Route / Distributor"].dropna() if str(v).strip()
))
edited_log_df = st.data_editor(
    base_log_df, num_rows="dynamic", use_container_width=True, key="gate_out_log_editor",
    column_config={
        "Ownership": st.column_config.SelectboxColumn(options=["Own", "Fixed", "Spot Hire"]),
        "Gate Out Date": st.column_config.TextColumn(help="e.g. 2026-07-27"),
        "Actual Return Date": st.column_config.TextColumn(help="Leave blank until it's back"),
        "Route / Distributor": st.column_config.SelectboxColumn(options=log_distributor_options),
    }
)
save_col1, save_col2 = st.columns([1, 4])
with save_col1:
    if st.button("💾 Save log changes", use_container_width=True, key="save_gate_log_btn"):
        save_gate_out_log_local(edited_log_df)
        st.success("Saved.")
        st.rerun()
with save_col2:
    st.caption("⚠️ Saved locally on this deployment (may reset on redeploy). If you merged in a "
               "Google Sheet, edits here only update your local copy — fix that sheet's own rows "
               "at the source, or use the one-time Import above to bring them in permanently.")



log_df = edited_log_df
st.caption(f"Gate-out data source: **{data_source_label}**" +
           (" — Google Sheet refreshes automatically every 60s" if (use_external_sheet and sheet_url) else ""))

# ---------------- PROCESS LOG + CROSS-REFERENCE ----------------
required_cols = {"Vehicle Number", "Ownership", "Gate Out Date"}
has_log_data = (log_df is not None and not log_df.empty and
                required_cols.issubset(set(str(c).strip() for c in log_df.columns)))

if has_log_data:
    bad_ownership_values = validate_ownership_values(log_df)
    if bad_ownership_values:
        st.error(
            f"🚫 **Data problem in your sheet's Ownership column:** found value(s) {bad_ownership_values} "
            f"that aren't **Own**, **Fixed**, or **Spot Hire**. Those rows won't count toward availability "
            f"until fixed — a common mistake is typing the column header text into the cells by accident. "
            f"Please correct the Ownership column to say exactly `Own`, `Fixed`, or `Spot Hire`."
        )
    # Spot Hire rows are logged for the record (they show in the Vehicle Out table above) but
    # aren't real registered vehicles, so they're excluded here — cross-referencing them against
    # the Vehicle Database would otherwise flag every "(market)" row as an unmatched typo.
    fleet_log_df = log_df[log_df["Ownership"].astype(str).str.strip().str.title() != "Spot Hire"].copy()
    processed_df, currently_out_df = process_gate_out_log(fleet_log_df, as_of_date)
else:
    currently_out_df = pd.DataFrame(columns=["Vehicle Number", "Ownership", "Days Out"])
    st.info("ℹ️ No gate-out entries yet — showing full fleet as available.")

fleet_status_df, unmatched_df = cross_reference_fleet(veh_db, currently_out_df)

if not unmatched_df.empty:
    st.warning(f"⚠️ **{len(unmatched_df)} gate-out entries** couldn't be matched to an operational vehicle — "
               f"see the Reason column below (typo in Vehicle Number, or the vehicle is marked "
               f"non-operational in the Vehicle Database). These are excluded from the counts below.")
    with st.expander("See unmatched entries"):
        st.dataframe(unmatched_df, use_container_width=True, hide_index=True)

with st.expander("🗓️ Look up the gate-out log by date range (e.g. a full month) instead of a single day"):
    st.caption("This is a separate lookup into the raw log — the 'As of date' above still drives the "
               "Available/Out status and planning sections below, since those are inherently a "
               "single-day snapshot.")
    range_default_start = as_of_date - pd.Timedelta(days=6)
    picked_range = st.date_input(
        "Date range", value=(range_default_start, as_of_date), key="live_gate_date_range"
    )
    if not has_log_data:
        st.info("Connect a gate-out log sheet (or add manual entries above) to look up a date range.")
    elif not (isinstance(picked_range, (list, tuple)) and len(picked_range) == 2):
        st.info("Pick both a start and an end date to see results.")
    else:
        range_start, range_end = picked_range
        range_df = log_df.copy()
        range_df.columns = [str(c).strip() for c in range_df.columns]
        range_df["Gate Out Date"] = pd.to_datetime(range_df["Gate Out Date"], errors="coerce", dayfirst=True)
        in_range = range_df[
            range_df["Gate Out Date"].dt.date.between(range_start, range_end)
        ].copy()
        st.caption(
            f"**{len(in_range)}** gate-out entries between **{range_start.strftime('%d %b %Y')}** and "
            f"**{range_end.strftime('%d %b %Y')}**."
        )
        if not in_range.empty:
            show_cols = [c for c in ["Vehicle Number", "Ownership", "Gate Out Date", "Actual Return Date",
                                      "Route / Distributor"] if c in in_range.columns]
            display_range_df = in_range[show_cols].sort_values("Gate Out Date", kind="mergesort")
            st.dataframe(display_range_df, use_container_width=True, hide_index=True)
            st.download_button(
                "⬇️ Download this range (CSV)",
                display_range_df.to_csv(index=False).encode("utf-8"),
                file_name=f"gate_out_log_{range_start}_{range_end}.csv",
                mime="text/csv",
                key="download_gate_range"
            )
        else:
            st.info("No gate-out entries fall in that range.")

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
        _, out_d = process_gate_out_log(fleet_log_df, d)
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

# ---------------- LIVE LOAD STATUS SUMMARY ----------------
# A lightweight, read-only look at today's shipments (any status) — separate from, and
# purely additive to, the Today's Load box below (which still does its own fetch/manual
# fallback/planning exactly as before).
if load_refresh:
    fetch_load_sheet.clear()

_summary_shipments_df = None
_summary_fetch_error = None
_summary_raw_df = None
if load_sheet_url:
    try:
        _summary_raw_df = fetch_load_sheet(load_sheet_url)
        _summary_shipments_df = filter_daily_load_rows(_summary_raw_df, as_of_date)
    except Exception as e:
        _summary_fetch_error = str(e)

st.header(f"📦 Live Load Status — {as_of_date.strftime('%d %b %Y')}")
if _summary_shipments_df is not None and not _summary_shipments_df.empty:
    _bad_dist_names = unmatched_distributor_names(_summary_shipments_df["Route / Distributor"])
    if _bad_dist_names:
        st.warning(
            f"⚠️ **{len(_bad_dist_names)} distributor name(s) in the Load Log sheet don't match your "
            f"master distributor list** — their max-vehicle-tonnage cap won't apply until fixed at the "
            f"source: {', '.join(_bad_dist_names[:10])}" + (", …" if len(_bad_dist_names) > 10 else "")
        )
    _load_numeric = pd.to_numeric(_summary_shipments_df["Load (Ton)"], errors="coerce").fillna(0)
    _total_orders = len(_summary_shipments_df)
    _total_load = round(float(_load_numeric.sum()), 1)
    if "Status" in _summary_shipments_df.columns:
        _dispatched_mask = _summary_shipments_df["Status"] == "Dispatched"
    else:
        _dispatched_mask = pd.Series(False, index=_summary_shipments_df.index)
    _pending_mask = ~_dispatched_mask
    _dispatched_orders = int(_dispatched_mask.sum())
    _dispatched_load = round(float(_load_numeric[_dispatched_mask].sum()), 1)
    _pending_orders = int(_pending_mask.sum())
    _pending_load = round(float(_load_numeric[_pending_mask].sum()), 1)

    sl1, sl2, sl3, sl4, sl5, sl6 = st.columns(6)
    sl1.metric("Total Orders", f"{_total_orders:,}")
    sl2.metric("Total Load (Ton)", f"{_total_load:,}")
    sl3.metric("✅ Dispatched Orders", f"{_dispatched_orders:,}")
    sl4.metric("✅ Dispatched Load (Ton)", f"{_dispatched_load:,}")
    sl5.metric("🕒 Pending Orders", f"{_pending_orders:,}")
    sl6.metric("🕒 Pending Load (Ton)", f"{_pending_load:,}")
elif load_sheet_url:
    if _summary_fetch_error:
        st.info(f"⚠️ Couldn't read the Load Log sheet ({_summary_fetch_error}) — see below.")
    else:
        st.info(f"No shipments found for {as_of_date.strftime('%d %b %Y')} in the Load Log sheet yet.")
else:
    st.info("Connect a Load Log sheet in the sidebar to see a live load summary here — "
             "or check the manual entry table below.")

st.write("---")

# ---------------- TODAY'S LOAD → TRUCKS → SPOT HIRE ----------------
with st.container(border=True):
    st.subheader("🚚 Today's Load → Trucks Needed → Spot Hire")
    st.caption("Enter each distributor/route's load separately, in TONS — real dispatch is many "
               "different-sized shipments, not one lump total, and each is matched to the closest "
               "available vehicle tonnage (not just a total capacity assumption).")

    shipments_df = None
    load_source_note = ""

    if load_sheet_url:
        if _summary_fetch_error:
            st.error(f"⚠️ Couldn't read Load Log sheet ({_summary_fetch_error}).")
        else:
            shipments_df = _summary_shipments_df
            load_source_note = f"Google Sheet ({as_of_date.strftime('%d %b %Y')})"
            if shipments_df is not None and not shipments_df.empty:
                st.dataframe(shipments_df, use_container_width=True, hide_index=True)
            else:
                st.warning(
                    f"⚠️ No shipments found for {as_of_date.strftime('%d %b %Y')} in the Load Log sheet — "
                    "enter today's rows in the sheet or use the manual fallback below."
                )
                with st.expander("🔍 Why is this empty? (diagnostic)"):
                    if _summary_raw_df is not None:
                        raw_preview = _summary_raw_df.copy()
                        raw_preview.columns = [str(c).strip() for c in raw_preview.columns]
                        st.write(f"**{len(raw_preview)}** total row(s) read from the sheet just now.")
                        st.write(f"**Columns found:** {list(raw_preview.columns)}")
                        date_col = next((c for c in raw_preview.columns if c.strip().lower() == "date"), None)
                        if date_col:
                            parsed_dates = pd.to_datetime(raw_preview[date_col], errors="coerce", dayfirst=True).dt.date
                            unique_dates = sorted(set(d for d in parsed_dates.dropna()))
                            st.write(f"**Comparing against 'As of date':** {as_of_date}")
                            if unique_dates:
                                shown = ", ".join(str(d) for d in unique_dates[-15:])
                                st.write(f"**Dates found in the sheet (parsed):** {shown}")
                            else:
                                st.error("None of the Date values in the sheet could be parsed at all — "
                                         "check the sheet's date format/column.")
                            unparsed = int(parsed_dates.isna().sum())
                            if unparsed:
                                st.warning(f"{unparsed} row(s) had a Date value that couldn't be parsed.")
                        else:
                            st.error("No 'Date' column found at all in this sheet — check the header spelling.")

                        load_col_guess = next((c for c in raw_preview.columns
                                                if "load" in c.strip().lower()), None)
                        if load_col_guess:
                            parsed_loads = pd.to_numeric(
                                raw_preview[load_col_guess].astype(str).str.extract(r'([\d]*\.?[\d]+)')[0],
                                errors="coerce"
                            )
                            bad_loads = int(parsed_loads.isna().sum())
                            if bad_loads:
                                st.warning(f"{bad_loads} row(s) had a '{load_col_guess}' value with no parseable "
                                           f"number in it at all (checked separately from any unit text like 'T').")

                        st.dataframe(raw_preview.head(10), use_container_width=True, hide_index=True)
                        st.caption(
                            "💡 If the data above looks correct but this is still empty, Google Sheets' own "
                            "**\"Publish to web\"** CSV link updates on its own schedule and can lag a few minutes "
                            "behind your edits — separate from this app's 60-second cache and the Refresh Now "
                            "button. Try opening the CSV link itself in a new browser tab to see exactly what "
                            "Google is serving right now."
                        )
    else:
        st.info("Add a Load Log Sheet link in the sidebar.")

    if shipments_df is None:
        if "fallback_shipments_df" not in st.session_state:
            st.session_state.fallback_shipments_df = pd.DataFrame({
                "Route / Distributor": [""], "Load (Ton)": [0.0]
            })
        fallback_options = sorted(set(distributor_list) | {""} | set(
            str(v).strip() for v in st.session_state.fallback_shipments_df["Route / Distributor"].dropna()
            if str(v).strip()
        ))
        shipments_df = st.data_editor(
            st.session_state.fallback_shipments_df, num_rows="dynamic", use_container_width=True,
            key="fallback_shipments_editor",
            column_config={
                "Load (Ton)": st.column_config.NumberColumn(min_value=0.0, step=0.5, format="%.1f"),
                "Route / Distributor": st.column_config.SelectboxColumn(options=fallback_options),
            }
        )
        load_source_note = "manual fallback"

    # The table above shows every shipment for today, any status — but only Pending
    # shipments should ever be planned/allocated a vehicle. Dispatched ones stay visible
    # for full-picture visibility without being re-recommended a truck.
    shipments_to_plan_df = shipments_df
    if shipments_to_plan_df is not None and "Status" in shipments_to_plan_df.columns:
        shipments_to_plan_df = shipments_to_plan_df[shipments_to_plan_df["Status"] == "Pending"].copy()

    # --- Recognize shipments already executed, even if Dispatch Status wasn't flipped ---
    # shipments_to_plan_df above already excludes anything marked "Dispatched" in the Load
    # Log. This second check catches the gap where the Transport Team has recorded the
    # actual gate-out (Vehicle Number + date) in the gate-out log for a distributor, but
    # hasn't (yet, or ever) updated that shipment's status cell in the separate Load Log
    # sheet. Without this, that shipment would still look "Pending" and get re-planned
    # against whatever vehicles are still free — recommending a DIFFERENT vehicle than the
    # one that was actually just dispatched for it.
    already_dispatched = already_dispatched_routes(log_df, as_of_date) if has_log_data else set()

    excluded_rows = pd.DataFrame()
    if shipments_to_plan_df is not None and not shipments_to_plan_df.empty and already_dispatched:
        norm_route = shipments_to_plan_df["Route / Distributor"].astype(str).str.strip().str.casefold()
        is_already_out = norm_route.isin(already_dispatched)
        excluded_rows = shipments_to_plan_df[is_already_out]
        shipments_to_plan_df = shipments_to_plan_df[~is_already_out].copy()

    # Deterministic ordering: sort by the shipment's own content (mergesort — stable),
    # not by whatever row order the source sheet happens to have. This guarantees the
    # same set of pending shipments always allocates to the same vehicles, even if
    # someone manually reorders/sorts rows in the Google Sheet without changing the
    # actual data.
    if shipments_to_plan_df is not None and not shipments_to_plan_df.empty:
        shipments_to_plan_df = shipments_to_plan_df.sort_values(
            ["Route / Distributor", "Load (Ton)"], kind="mergesort"
        ).reset_index(drop=True)
        # Keep loads/distributors/caps all aligned by filtering once, here, rather than
        # filtering shipment_loads separately below (which previously could desync the
        # two lists if a row had a blank/zero load).
        shipments_to_plan_df = shipments_to_plan_df[
            pd.to_numeric(shipments_to_plan_df["Load (Ton)"], errors="coerce") > 0
        ].reset_index(drop=True)

    shipment_loads = shipments_to_plan_df["Load (Ton)"].tolist() if shipments_to_plan_df is not None and not shipments_to_plan_df.empty else []
    shipment_distributors = shipments_to_plan_df["Route / Distributor"].tolist() if shipments_to_plan_df is not None and not shipments_to_plan_df.empty else []
    total_load_today = round(float(sum(shipment_loads)), 1)

    # Each distributor can only physically receive up to its own max allowed vehicle size
    # (road width, gate access, etc.) — from the Distributor-wise Max Vehicle Capacity
    # sheet. A 52-ton order for a distributor capped at 4T should become several 4T (or
    # smaller) trucks, never a single bigger one just because it's free.
    db_capacity_live = load_db_capacity()
    _dist_cap_lookup = {}
    for _, r in db_capacity_live.iterrows():
        name = str(r.get("Distributor", "")).strip().casefold()
        cap = r.get("MaxVehicleTonnage")
        if name and pd.notna(cap):
            _dist_cap_lookup[name] = float(cap)
    shipment_max_tonnages = [
        _dist_cap_lookup.get(str(d).strip().casefold()) for d in shipment_distributors
    ]

    if not excluded_rows.empty:
        st.caption(
            f"✅ {len(excluded_rows)} shipment(s) already have a matching gate-out logged today "
            f"for that distributor — excluded from re-planning: "
            f"{', '.join(sorted(set(excluded_rows['Route / Distributor'].astype(str))))}."
        )

    capped_shipments = [
        (dist, cap) for dist, cap in zip(shipment_distributors, shipment_max_tonnages) if cap is not None
    ]
    if capped_shipments:
        cap_notes = ", ".join(f"{dist} (max {cap:g}T)" for dist, cap in sorted(set(capped_shipments)))
        st.caption(f"🚧 Distributor max-vehicle limits applied: {cap_notes}.")

    # Today's Load is now measured directly in tons — match each shipment to the closest
    # available vehicle TONNAGE (Own's full range, then Fixed/Spot Hire capped at their
    # real max size) rather than converting through a cases-per-truck capacity table.
    veh_block_tons = edited_veh_block_live[["Vehicle", "TonnageNum"]].copy()
    veh_block_tons["TonnageNum"] = pd.to_numeric(veh_block_tons["TonnageNum"], errors="coerce")
    veh_block_tons = veh_block_tons.dropna(subset=["TonnageNum"])
    veh_block_tons["Capacity"] = veh_block_tons["TonnageNum"]

    alloc_results = allocate_shipments_to_fleet(
        shipment_loads, fleet_status_df, veh_block_tons,
        buffer=buffer_tons_live, max_tonnage=max_tonnage_live if max_tonnage_live > 0 else None,
        distributors=shipment_distributors, max_tonnages=shipment_max_tonnages
    )
    alloc_results_df = pd.DataFrame(alloc_results)

    if not alloc_results_df.empty:
        alloc_results_df = alloc_results_df.rename(columns={"Load": "Load (Ton)"})
        alloc_results_df["Gate Out Date"] = np.where(
            alloc_results_df["Source"].isin(["Own", "Fixed"]), as_of_date, ""
        )
        alloc_results_df = alloc_results_df[
            ["Vehicle Number", "Truck Size", "Load (Ton)", "Source", "Distributor",
             "Gate Out Date"]
        ]

    trucks_needed_today = len(alloc_results)
    own_used_today = int((alloc_results_df["Source"] == "Own").sum()) if not alloc_results_df.empty else 0
    fixed_used_today = int((alloc_results_df["Source"] == "Fixed").sum()) if not alloc_results_df.empty else 0
    spot_needed_today = int((alloc_results_df["Source"] == "Spot Hire").sum()) if not alloc_results_df.empty else 0

    st.caption(f"Load source: **{load_source_note}** · {len(shipment_loads)} shipments totaling "
               f"{total_load_today:,} tons (with a {buffer_tons_live}-ton/truck buffer) → "
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

        allotted_rows = alloc_results_df[alloc_results_df["Source"].isin(["Own", "Fixed", "Spot Hire"])].copy()
        if not allotted_rows.empty:
            st.write("")
            if st.button(
                f"✅ Confirm Allotment & Log Gate-Out ({len(allotted_rows)} vehicle{'s' if len(allotted_rows) != 1 else ''})",
                use_container_width=True, key="confirm_allot_btn"
            ):
                new_entries = pd.DataFrame({
                    "Vehicle Number": allotted_rows["Vehicle Number"].values,
                    "Ownership": allotted_rows["Source"].values,
                    "Truck Size (T)": allotted_rows["Truck Size"].values,
                    "Gate Out Date": str(as_of_date),
                    "Actual Return Date": "",
                    "Route / Distributor": allotted_rows["Distributor"].values,
                })
                # dedupe=False: once confirmed, that distributor's shipment is excluded from
                # re-planning on the next rerun (see already_dispatched_routes below), so this
                # can't be double-logged by re-clicking — and Spot Hire rows can legitimately
                # repeat the same "(market)" vehicle number + size + distributor for genuinely
                # separate trucks, which a blanket duplicate-row check would wrongly collapse.
                append_gate_out_entries(new_entries, dedupe=False)
                st.success(
                    f"Logged {len(new_entries)} vehicle(s) as gated-out today — see the Vehicle Out "
                    f"log above (Actual Return Date is editable there once each truck is back)."
                )
                st.rerun()
            st.caption("Includes Spot Hire arrangements too (shown as **market** in Vehicle Number) — "
                       "so today's Vehicle Out log is a complete record, not just your own fleet.")

st.write("---")

# ---------------- VEHICLE-LEVEL DETAIL ----------------
c1, c2 = st.columns(2)
with c1:
    st.subheader("✅ Available Vehicles (ready to dispatch)")
    st.caption("Change **Remarks** right here — no need to edit the source Excel. Only vehicles "
               "marked **Operational** count as available for planning above.")
    avail_cols = [c for c in ["Vehicle Number", "OwnershipType", "Location", "Transporter Name", "CapacityTonnage", "Remarks"]
                  if c in fleet_status_df.columns]
    avail_show = fleet_status_df[fleet_status_df["Status"] == "Available"][avail_cols].copy()
    if "Remarks" in avail_show.columns:
        avail_show.loc[~avail_show["Remarks"].isin(REMARKS_OPTIONS), "Remarks"] = "Operational"
        edited_avail = st.data_editor(
            avail_show, use_container_width=True, height=320, hide_index=True, key="avail_vehicles_editor",
            disabled=[c for c in avail_cols if c != "Remarks"],
            column_config={"Remarks": st.column_config.SelectboxColumn(options=REMARKS_OPTIONS)}
        )
        if st.button("💾 Save status changes", key="save_vehicle_status_btn"):
            changed = edited_avail[["Vehicle Number", "Remarks"]].copy()
            merged_overrides = pd.concat([status_overrides_df, changed], ignore_index=True)
            save_vehicle_status_overrides(merged_overrides)
            st.success("Saved — statuses updated.")
            st.rerun()
    else:
        edited_avail = avail_show
        st.dataframe(avail_show, use_container_width=True, height=320, hide_index=True)
    st.download_button("⬇️ Download available vehicles (CSV)", edited_avail.to_csv(index=False).encode("utf-8"),
                        file_name=f"available_vehicles_{as_of_date}.csv", mime="text/csv")

with c2:
    st.subheader("🚫 Vehicles Currently Out")
    out_cols = [c for c in ["Vehicle Number", "OwnershipType", "Location", "Transporter Name", "Distributor", "Days Out", "Remarks"]
                if c in fleet_status_df.columns]
    out_show = fleet_status_df[fleet_status_df["Status"] == "Out"][out_cols].copy()
    if "Distributor" in out_show.columns:
        out_show["Distributor"] = out_show["Distributor"].fillna("—")
    if "Remarks" in out_show.columns:
        out_show["Remarks"] = out_show["Remarks"].replace("", "—")
    if "Days Out" in out_show.columns:
        out_show = out_show.sort_values("Days Out", ascending=False)
    st.dataframe(out_show, use_container_width=True, height=320, hide_index=True)

with st.expander("ℹ️ How this works"):
    st.markdown("""
    1. For **Today's Load**, enter each shipment separately (route/distributor + tons) rather than one
       lump total — real dispatch is many different-sized loads, not one uniform number. Each shipment
       is matched to the closest AVAILABLE real vehicle tonnage that still covers it (+ your overload
       buffer) — Own first (including any size only Own has), then Fixed, then Spot Hire — checked
       **vehicle by vehicle against actual tonnage on hand**, not just a headcount. Fixed and Spot Hire
       are capped at whatever the largest real Fixed vehicle in your fleet actually is, so neither is
       ever offered a size they don't physically have.
    2. Click **✅ Confirm Allotment & Log Gate-Out** on the suggested allocation to record it as gated-out
       today — this fills the **Vehicle Out** log above automatically (Vehicle Number, Ownership, Truck
       Size, Gate Out Date, Distributor), including Spot Hire (shown as **market** in Vehicle Number), so
       it's a complete daily record, not just your own fleet. No manual copy-paste into a separate sheet.
       Edit **Actual Return Date** in that same log once a real truck is back.
    3. This page cross-references every real Vehicle Number in that log (Own/Fixed) against your **actual
       Vehicle Database**, so each truck is tracked individually — not just as an aggregate count.
       Unrecognized vehicle numbers (typos, unregistered trucks) are flagged separately. Spot Hire rows
       stay visible in the log for the record but aren't checked against the Vehicle Database, since
       they're not your registered vehicles.
    4. **Available = registered fleet − currently out − marked non-operational.** Use the dropdown in the
       **✅ Available Vehicles** table to flip a vehicle's status (Operational / Non-Operational /
       Maintenance / Driver Not Available) any time — no need to touch the source Excel; only
       **Operational** vehicles are offered for planning above.
    5. Still using your old gate-out Google Sheet? Turn on **"Pull in your old gate-out Google Sheet"** in
       the sidebar to see its rows merged in, then click **📥 Import into the backend** once to copy the
       vehicles already logged as out into this app's own log permanently — after that you can leave the
       sheet checkbox off.

    **Publishing a Google Sheet as CSV:** open the sheet → File → Share → Publish to web → choose the
    correct tab → format **Comma-separated values (.csv)** → Publish → copy the link into the sidebar.
    """)
