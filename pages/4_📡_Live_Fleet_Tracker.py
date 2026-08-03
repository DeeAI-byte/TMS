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
    apply_vehicle_status_overrides, REMARKS_OPTIONS,
    get_distributor_list, unmatched_distributor_names, format_truck_size,
    load_allocation_state, save_allocation_state, upsert_allocation_state,
    ALLOCATION_STATUS_OPTIONS, github_persistence_enabled
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
st.sidebar.header("🔗 Vehicle Out (Gate-Out Log)")
st.sidebar.caption(
    "Fills in automatically as you move shipments to **Dispatched** in Today's Load below — "
    "one single table, no separate Google Sheet needed anymore."
)
as_of_date = st.sidebar.date_input("As of date", value=date.today(), key="live_as_of_date")

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

# ---------------- LOAD LOG SHEET FETCH ----------------
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


# ---------------- LOAD LOCAL "VEHICLE OUT" LOG (single source of truth, no sheet dependency) ----------------
# The editable table itself now lives further down, next to Available Vehicles — this just
# loads the persisted data so the allocation/cross-reference logic below has it to work with.
base_log_df = load_gate_out_log_local()
log_df = base_log_df

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

    # Deterministic ordering + drop blank/zero loads FIRST, then build a stable ShipmentKey
    # per row (Date|Distributor|Load|dup-seq) — this is what lets a shipment carry a
    # Pending → Allotted → Dispatched status across reruns, and lets a duplicate shipment
    # (same distributor + same tonnage, same day) still get its own independent key.
    today_str = str(as_of_date)
    if shipments_to_plan_df is not None and not shipments_to_plan_df.empty:
        shipments_to_plan_df = shipments_to_plan_df[
            pd.to_numeric(shipments_to_plan_df["Load (Ton)"], errors="coerce") > 0
        ].copy()
        shipments_to_plan_df = shipments_to_plan_df.sort_values(
            ["Route / Distributor", "Load (Ton)"], kind="mergesort"
        ).reset_index(drop=True)
        _norm_dist = shipments_to_plan_df["Route / Distributor"].astype(str).str.strip()
        _load_key = pd.to_numeric(shipments_to_plan_df["Load (Ton)"], errors="coerce").round(3).astype(str)
        _dup_seq = shipments_to_plan_df.groupby([_norm_dist, _load_key]).cumcount().astype(str)
        shipments_to_plan_df["ShipmentKey"] = today_str + "|" + _norm_dist + "|" + _load_key + "|" + _dup_seq
    else:
        shipments_to_plan_df = pd.DataFrame(columns=["Route / Distributor", "Load (Ton)", "ShipmentKey"])

    # --- Recognize shipments already executed some OTHER way, even if Dispatch Status
    # wasn't flipped here --- e.g. someone manually added a gate-out row for that
    # distributor today without going through Allot/Dispatch below. Shipments we're
    # ALREADY tracking via our own Pending/Allotted/Dispatched state are exempt from this
    # heuristic — their real status comes from that state, not a guess.
    alloc_state_all = load_allocation_state()
    alloc_state_today = (
        alloc_state_all[alloc_state_all["Date"] == today_str].copy()
        if not alloc_state_all.empty else alloc_state_all
    )
    tracked_keys_today = set(alloc_state_today["ShipmentKey"]) if len(alloc_state_today) else set()

    already_dispatched = already_dispatched_routes(log_df, as_of_date) if has_log_data else set()
    excluded_rows = pd.DataFrame()
    if not shipments_to_plan_df.empty and already_dispatched:
        norm_route = shipments_to_plan_df["Route / Distributor"].astype(str).str.strip().str.casefold()
        is_already_out = norm_route.isin(already_dispatched) & ~shipments_to_plan_df["ShipmentKey"].isin(tracked_keys_today)
        excluded_rows = shipments_to_plan_df[is_already_out]
        shipments_to_plan_df = shipments_to_plan_df[~is_already_out].copy()

    shipment_distributors = shipments_to_plan_df["Route / Distributor"].tolist()
    total_load_today = round(float(shipments_to_plan_df["Load (Ton)"].sum()), 1) if len(shipments_to_plan_df) else 0.0

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

    PLAN_COLS = ["ShipmentKey", "Vehicle Number", "Truck Size", "Load (Ton)", "Source", "Distributor", "Status"]

    # --- Shipments already Allotted or Dispatched today: pull their FIXED vehicle from
    # the saved state — never recomputed, never reassigned to a different shipment.
    locked_state = (
        alloc_state_today[alloc_state_today["Status"].isin(["Allotted", "Dispatched"])]
        if len(alloc_state_today) else alloc_state_today
    )
    if len(locked_state):
        locked_display_df = locked_state.rename(columns={"Truck Size (T)": "Truck Size"})[PLAN_COLS].copy()
        locked_display_df["Load (Ton)"] = pd.to_numeric(locked_display_df["Load (Ton)"], errors="coerce")
    else:
        locked_display_df = pd.DataFrame(columns=PLAN_COLS)
    locked_keys = set(locked_display_df["ShipmentKey"])
    locked_vehicle_numbers = set(
        locked_display_df.loc[locked_display_df["Source"].isin(["Own", "Fixed"]), "Vehicle Number"]
    )

    # --- Still-Pending shipments: suggest a vehicle now, one shipment at a time, pulling
    # any vehicle already locked to an Allotted/Dispatched shipment (above) OR to an
    # earlier shipment in this same pass out of the pool first — so the same real vehicle
    # is never suggested to two different shipments.
    pending_rows_df = shipments_to_plan_df[~shipments_to_plan_df["ShipmentKey"].isin(locked_keys)].copy()
    current_fleet_pool = fleet_status_df.copy()
    if locked_vehicle_numbers:
        _lock_mask = current_fleet_pool["Vehicle Number"].astype(str).str.strip().str.upper().isin(
            {str(v).strip().upper() for v in locked_vehicle_numbers}
        )
        current_fleet_pool.loc[_lock_mask, "Status"] = "Locked"

    suggested_rows = []
    for _, srow in pending_rows_df.iterrows():
        single_cap = _dist_cap_lookup.get(str(srow["Route / Distributor"]).strip().casefold())
        result = allocate_shipments_to_fleet(
            [srow["Load (Ton)"]], current_fleet_pool, veh_block_tons,
            buffer=buffer_tons_live, max_tonnage=max_tonnage_live if max_tonnage_live > 0 else None,
            distributors=[srow["Route / Distributor"]], max_tonnages=[single_cap]
        )
        for r in result:
            r["ShipmentKey"] = srow["ShipmentKey"]
            r["Status"] = "Pending"
            suggested_rows.append(r)
            if r["Source"] in ("Own", "Fixed"):
                vn = str(r["Vehicle Number"]).strip().upper()
                _mask = current_fleet_pool["Vehicle Number"].astype(str).str.strip().str.upper() == vn
                current_fleet_pool = current_fleet_pool.copy()
                current_fleet_pool.loc[_mask, "Status"] = "Locked"

    suggested_df = pd.DataFrame(suggested_rows)
    if not suggested_df.empty:
        suggested_df = suggested_df.rename(columns={"Load": "Load (Ton)"})[PLAN_COLS]
    else:
        suggested_df = pd.DataFrame(columns=PLAN_COLS)

    plan_df = pd.concat([locked_display_df, suggested_df], ignore_index=True)

    trucks_needed_today = len(plan_df)
    own_used_today = int((plan_df["Source"] == "Own").sum()) if not plan_df.empty else 0
    fixed_used_today = int((plan_df["Source"] == "Fixed").sum()) if not plan_df.empty else 0
    spot_needed_today = int((plan_df["Source"] == "Spot Hire").sum()) if not plan_df.empty else 0
    dispatched_today = int((plan_df["Status"] == "Dispatched").sum()) if not plan_df.empty else 0

    st.caption(f"Load source: **{load_source_note}** · {len(shipments_to_plan_df)} shipments totaling "
               f"{total_load_today:,} tons (with a {buffer_tons_live}-ton/truck buffer) → "
               f"**{trucks_needed_today:,} trucks needed today**, matched against your real available fleet.")

    d1, d2, d3, d4, d5 = st.columns(5)
    d1.metric("Trucks Needed", f"{trucks_needed_today:,}")
    d2.metric("🟦 Own Used", f"{own_used_today:,}")
    d3.metric("🟧 Fixed Used", f"{fixed_used_today:,}")
    d4.metric("🟥 Spot Hire Needed NOW", f"{spot_needed_today:,}")
    d5.metric("✅ Dispatched", f"{dispatched_today:,}")

    if trucks_needed_today > 0:
        st.caption("Change **Status** below: **Allotted** fixes that suggested vehicle to this shipment "
                   "(no more re-suggesting it elsewhere); **Dispatched** logs it into the Vehicle Out log "
                   "above right away. Includes Spot Hire (shown as **market** in Vehicle Number).")
        edited_plan_df = st.data_editor(
            plan_df, use_container_width=True, hide_index=True, key="dispatch_status_editor",
            column_order=["Distributor", "Load (Ton)", "Truck Size", "Vehicle Number", "Source", "Status"],
            disabled=["Distributor", "Load (Ton)", "Truck Size", "Vehicle Number", "Source"],
            column_config={"Status": st.column_config.SelectboxColumn(options=ALLOCATION_STATUS_OPTIONS)}
        )
        if spot_needed_today > 0:
            st.warning(f"⚠️ Arrange **{spot_needed_today} spot hire vehicles** today — your Own/Fixed fleet "
                       f"doesn't have enough AVAILABLE vehicles of the right size for {spot_needed_today} "
                       f"of these shipments (not just a raw count shortfall — checked vehicle-by-vehicle "
                       f"against actual tonnage on hand).")
        else:
            st.success("✅ Own + Fixed availability — with the right tonnage on hand — covers today's need. "
                       "No spot hire required.")

        if st.button("✅ Apply Status Changes", use_container_width=True, key="apply_status_btn"):
            new_state_rows = []
            gate_out_new_entries = []
            revert_keys = []
            blocked_any = False
            for _, row in edited_plan_df.iterrows():
                key = row["ShipmentKey"]
                _prev = alloc_state_today.loc[alloc_state_today["ShipmentKey"] == key, "Status"] if len(alloc_state_today) else pd.Series(dtype=str)
                prev_status = _prev.iloc[0] if len(_prev) else "Pending"
                new_status = row["Status"]
                if new_status == prev_status:
                    continue
                if prev_status == "Dispatched":
                    blocked_any = True
                    continue
                if new_status == "Pending":
                    revert_keys.append(key)
                    continue
                new_state_rows.append({
                    "ShipmentKey": key, "Date": today_str, "Distributor": row["Distributor"],
                    "Load (Ton)": row["Load (Ton)"], "Vehicle Number": row["Vehicle Number"],
                    "Truck Size (T)": row["Truck Size"], "Source": row["Source"], "Status": new_status,
                })
                if new_status == "Dispatched":
                    gate_out_new_entries.append({
                        "Vehicle Number": row["Vehicle Number"], "Ownership": row["Source"],
                        "Truck Size (T)": row["Truck Size"], "Gate Out Date": today_str,
                        "Actual Return Date": "", "Route / Distributor": row["Distributor"],
                        "Load (Ton)": row["Load (Ton)"],
                    })

            if revert_keys:
                save_allocation_state(alloc_state_all[~alloc_state_all["ShipmentKey"].isin(revert_keys)])
            if new_state_rows:
                upsert_allocation_state(pd.DataFrame(new_state_rows))
            if gate_out_new_entries:
                # dedupe=False: Spot Hire rows can legitimately repeat the same "(market)"
                # vehicle number + size + distributor for genuinely separate trucks, which
                # a blanket duplicate-row check would wrongly collapse.
                append_gate_out_entries(pd.DataFrame(gate_out_new_entries), dedupe=False)

            if blocked_any:
                st.warning("Some rows were already Dispatched and can't be changed here — edit the "
                           "Vehicle Out log above directly if you need to correct one.")
            if new_state_rows or gate_out_new_entries or revert_keys:
                st.success("Updated.")
                st.rerun()
            else:
                st.info("No changes to apply.")

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
    if "CapacityTonnage" in avail_show.columns:
        avail_show["Truck Size"] = avail_show["CapacityTonnage"].apply(format_truck_size)
        avail_show = avail_show.drop(columns=["CapacityTonnage"])
        avail_cols = [c for c in avail_cols if c != "CapacityTonnage"] + ["Truck Size"]
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
    st.subheader("🚚 Vehicle Out — Gate-Out / Return Log")
    st.caption(
        "Fills in automatically as soon as you move a shipment to **Dispatched** in Today's Load "
        "above. Edit **Return Date** (calendar) once a truck is back, or fix **Ownership** — this "
        "is the one and only Vehicle Out table."
    )
    if github_persistence_enabled():
        st.caption("✅ Auto-saved to your GitHub repo on every change — survives app reboots/redeploys.")
    else:
        st.warning(
            "⚠️ **Not reboot-safe yet.** This data currently lives only on the app's local disk, which "
            "resets on every redeploy/restart. Add `github_token`, `github_repo` (and optionally "
            "`github_branch`) in Settings → Secrets to make every save/dispatch here permanent — see "
            "the *How this works* section below for the exact steps."
        )

    log_distributor_options = sorted(set(distributor_list) | {""} | set(
        str(v).strip() for v in base_log_df["Route / Distributor"].dropna() if str(v).strip()
    ))
    # Real calendar pickers need actual date values, not text — blank cells become NaT
    # (shown as an empty, pickable calendar field) rather than an unparsed string.
    display_log_df = base_log_df.copy()
    display_log_df["Gate Out Date"] = pd.to_datetime(display_log_df["Gate Out Date"], errors="coerce")
    display_log_df["Actual Return Date"] = pd.to_datetime(display_log_df["Actual Return Date"], errors="coerce")
    display_log_df["Load (Ton)"] = pd.to_numeric(display_log_df["Load (Ton)"], errors="coerce")

    # Days Out is computed fresh every render, never stored — still out → days since Gate
    # Out up to today; already back → the completed duration up to its Return Date.
    _as_of_ts = pd.Timestamp(as_of_date)
    def _days_out(r):
        god = r["Gate Out Date"]
        if pd.isna(god):
            return pd.NA
        ard = r["Actual Return Date"]
        end = ard if pd.notna(ard) else _as_of_ts
        return max((end - god).days, 0)
    display_log_df["Days Out"] = display_log_df.apply(_days_out, axis=1).astype("Int64")

    edited_log_df = st.data_editor(
        display_log_df, num_rows="dynamic", use_container_width=True, height=320, key="gate_out_log_editor",
        column_order=["Vehicle Number", "Ownership", "Truck Size (T)", "Gate Out Date",
                      "Actual Return Date", "Days Out", "Route / Distributor", "Load (Ton)"],
        disabled=["Days Out"],
        column_config={
            "Ownership": st.column_config.SelectboxColumn(options=["Own", "Fixed", "Spot Hire"]),
            "Truck Size (T)": st.column_config.TextColumn(label="Truck Size"),
            "Gate Out Date": st.column_config.DateColumn(label="Gate Out", format="DD-MM-YYYY"),
            "Actual Return Date": st.column_config.DateColumn(label="Return Date", format="DD-MM-YYYY"),
            "Days Out": st.column_config.NumberColumn(label="Days Out", help="Since Gate Out — up to today if still out, or up to Return Date once back"),
            "Route / Distributor": st.column_config.SelectboxColumn(label="Distributor", options=log_distributor_options),
            "Load (Ton)": st.column_config.NumberColumn(label="Load (Ton)", format="%.1f"),
        }
    )
    save_col1, save_col2 = st.columns([1, 3])
    with save_col1:
        if st.button("💾 Save log changes", use_container_width=True, key="save_gate_log_btn"):
            to_save = edited_log_df.copy()
            to_save["Gate Out Date"] = pd.to_datetime(to_save["Gate Out Date"], errors="coerce").dt.strftime("%Y-%m-%d").fillna("")
            to_save["Actual Return Date"] = pd.to_datetime(to_save["Actual Return Date"], errors="coerce").dt.strftime("%Y-%m-%d").fillna("")
            save_gate_out_log_local(to_save)
            st.success("Saved.")
            st.rerun()
    with save_col2:
        st.caption("⚠️ Saved locally on this deployment (may reset on redeploy).")
    st.download_button("⬇️ Download Vehicle Out log (CSV)", edited_log_df.to_csv(index=False).encode("utf-8"),
                        file_name=f"vehicle_out_log_{as_of_date}.csv", mime="text/csv")

with st.expander("ℹ️ How this works"):
    st.markdown("""
    1. For **Today's Load**, enter each shipment separately (route/distributor + tons) rather than one
       lump total — real dispatch is many different-sized loads, not one uniform number. Each Pending
       shipment is matched to the closest AVAILABLE real vehicle tonnage that still covers it (+ your
       overload buffer) — Own first (including any size only Own has), then Fixed, then Spot Hire —
       checked **vehicle by vehicle against actual tonnage on hand**, not just a headcount. Fixed and
       Spot Hire are capped at whatever the largest real Fixed vehicle in your fleet actually is, so
       neither is ever offered a size they don't physically have.
    2. Each shipment has its own **Status**: **Pending → Allotted → Dispatched**.
       - Set it to **Allotted** to fix that suggested vehicle to this shipment — it's locked and won't
         be re-suggested to a different shipment on the next refresh.
       - Set it to **Dispatched** once the truck actually leaves — this logs it into the **Vehicle Out**
         log above automatically (Vehicle Number, Ownership, Truck Size, Gate Out Date, Distributor),
         including Spot Hire (shown as **market** in Vehicle Number). No manual copy-paste needed.
       - Click **✅ Apply Status Changes** to save whichever rows you've updated. A Dispatched row can't
         be changed back here — edit the Vehicle Out log directly if you need to correct one.
    3. This page cross-references every real Vehicle Number in the Vehicle Out log (Own/Fixed) against
       your **actual Vehicle Database**, so each truck is tracked individually — not just as an aggregate
       count. Unrecognized vehicle numbers (typos, unregistered trucks) are flagged separately. Spot Hire
       rows stay visible in the log for the record but aren't checked against the Vehicle Database, since
       they're not your registered vehicles.
    4. **Available = registered fleet − currently out − marked non-operational.** Use the dropdown in the
       **✅ Available Vehicles** table to flip a vehicle's status (Operational / Non-Operational /
       Maintenance / Driver Not Available) any time — no need to touch the source Excel; only
       **Operational** vehicles are offered for planning above.
    5. The Vehicle Out log, vehicle status overrides, and dispatch status are all single local tables —
       no Google Sheet dependency. **To make them survive an app reboot/redeploy** (Streamlit resets local
       disk on every restart otherwise), connect the app to your GitHub repo:
       - Create a **fine-grained personal access token** at github.com → Settings → Developer settings →
         Personal access tokens, scoped to just this repo, with **Contents: Read and write** permission.
       - In your Streamlit app's **Settings → Secrets**, add:
         ```
         github_token = "your-token-here"
         github_repo = "your-username/your-repo-name"
         github_branch = "main"
         ```
       - Save — no redeploy needed, it takes effect on the next run. You'll see a green "Auto-saved to
         your GitHub repo" note above the Vehicle Out table once it's working instead of the yellow warning.
       - From then on, every save/dispatch here also commits straight into `local_data/` in your repo, so
         a fresh boot reads back exactly what was last saved.

    **Publishing the Load Log Sheet as CSV:** open the sheet → File → Share → Publish to web → choose the
    correct tab → format **Comma-separated values (.csv)** → Publish → copy the link into the sidebar.
    """)
