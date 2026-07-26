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
    allocate_trucks_by_tonnage, allocate_shipments_to_fleet, load_db_capacity,
    init_session_tables, match_distributor_cap, DISPATCH_STATUS_OPTIONS
)

st.set_page_config(page_title="Live Fleet Tracker", page_icon="📡", layout="wide")
st.markdown("<style>.block-container { padding-top: 1rem; padding-bottom: 0.5rem; }</style>", unsafe_allow_html=True)

st.title("📡 Live Fleet Tracker")
st.caption("Reads the dispatch table and gate-out log straight from this session (no Google Sheet needed) — "
           "real available Own/Fixed fleet, right now, plus today's load → trucks → spot hire in one place.")
st.write("---")

init_session_tables()

try:
    veh_db = load_vehicle_database()
    own_total_default, fixed_total_default = fleet_totals_by_ownership(veh_db)
    veh_block, _ = load_assumptions()
    default_truck_capacity = int(round(veh_block["Capacity"].mean())) if len(veh_block) else 700
except Exception as e:
    st.error(f"⚠️ Could not load vehicle database: {e}")
    st.stop()

# ---------------- SIDEBAR CONFIG ----------------
st.sidebar.header("🗓️ Planning Date")
as_of_date = st.sidebar.date_input("As of date", value=date.today(), key="live_as_of_date")

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

st.sidebar.write("---")
st.sidebar.caption("📍 Dispatches and gate-outs are entered on the **🛠️ Route Creation** page — this page reads "
                    "them live from this session, updating instantly, no sheet link or refresh needed.")

# ---------------- GATE-OUT LOG (native session-state table) ----------------
log_df = st.session_state.gate_out_log.copy()
has_log_data = not log_df.empty and {"Vehicle Number", "Ownership", "Gate Out Date"}.issubset(log_df.columns)

if has_log_data:
    bad_ownership_values = validate_ownership_values(log_df)
    if bad_ownership_values:
        st.error(
            f"🚫 **Data problem in the gate-out log's Ownership column:** found value(s) {bad_ownership_values} "
            f"that aren't **Own** or **Fixed**. Those rows won't count toward availability until fixed."
        )
    processed_df, currently_out_df = process_gate_out_log(log_df, as_of_date)
else:
    currently_out_df = pd.DataFrame(columns=["Vehicle Number", "Ownership", "Days Out"])
    st.info("ℹ️ No gate-out entries yet — showing full fleet as available. Marking a dispatch row "
            "\"Dispatched\" on the Route Creation page adds one here automatically.")

fleet_status_df, unmatched_df = cross_reference_fleet(veh_db, currently_out_df)

if not unmatched_df.empty:
    st.warning(f"⚠️ **{len(unmatched_df)} gate-out entries** couldn't be matched to an operational vehicle — "
               f"see the Reason column below (typo in Vehicle Number, or the vehicle is marked "
               f"non-operational in the Vehicle Database). These are excluded from the counts below.")
    with st.expander("See unmatched entries"):
        st.dataframe(unmatched_df, use_container_width=True, hide_index=True)

with st.expander("📋 Gate-Out Log — edit Actual Return Date here to record a Gate-In"):
    st.caption("Rows appear here automatically the moment a dispatch is marked **Dispatched** on the Route "
               "Creation page. Fill in **Actual Return Date** (≤ the As of date above) once a truck is back — "
               "it flips straight back to Available below, no separate step needed.")
    gate_out_editor = st.data_editor(
        st.session_state.gate_out_log,
        use_container_width=True,
        key="gate_out_log_editor",
        column_config={
            "Ownership": st.column_config.SelectboxColumn(options=["Own", "Fixed"]),
            "Gate Out Date": st.column_config.DateColumn("Gate Out Date"),
            "Actual Return Date": st.column_config.DateColumn(
                "Actual Return Date", help="Leave blank until the vehicle is actually back."
            ),
        },
        disabled=["Vehicle Number", "Ownership", "Capacity Tonnage", "Gate Out Date", "Route / Distributor"],
    )
    st.session_state.gate_out_log = gate_out_editor
    log_df = gate_out_editor.copy()
    has_log_data = not log_df.empty and {"Vehicle Number", "Ownership", "Gate Out Date"}.issubset(log_df.columns)
    if has_log_data:
        _, currently_out_df = process_gate_out_log(log_df, as_of_date)
    else:
        currently_out_df = pd.DataFrame(columns=["Vehicle Number", "Ownership", "Days Out"])
    fleet_status_df, unmatched_df = cross_reference_fleet(veh_db, currently_out_df)

with st.expander("🗓️ Look up the gate-out log by date range (e.g. a full month) instead of a single day"):
    st.caption("This is a separate lookup into the raw log — the 'As of date' above still drives the "
               "Available/Out status and planning sections below, since those are inherently a "
               "single-day snapshot.")
    range_default_start = as_of_date - pd.Timedelta(days=6)
    picked_range = st.date_input(
        "Date range", value=(range_default_start, as_of_date), key="live_gate_date_range"
    )
    if not has_log_data:
        st.info("No gate-out entries yet to look up.")
    elif not (isinstance(picked_range, (list, tuple)) and len(picked_range) == 2):
        st.info("Pick both a start and an end date to see results.")
    else:
        range_start, range_end = picked_range
        range_df = log_df.copy()
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
               "more history you build up, the more useful this trend becomes for planning tomorrow.")
else:
    st.info("Once you have a few days of gate-out entries (with dates), this will show the trend "
             "automatically — no extra setup needed.")

st.write("---")

# ---------------- TODAY'S DISPATCH TABLE (from Route Creation, live) ----------------
dispatch_all = st.session_state.dispatch_table.copy()
if not dispatch_all.empty:
    dispatch_all["Date"] = pd.to_datetime(dispatch_all["Date"], errors="coerce", dayfirst=True).dt.date
    todays_dispatch = dispatch_all[
        (dispatch_all["Date"] == as_of_date) & (dispatch_all["Dispatch Status"] != "Cancelled")
    ].copy()
    todays_dispatch["Total Load (Ton)"] = pd.to_numeric(todays_dispatch["Total Load (Ton)"], errors="coerce")
    todays_dispatch = todays_dispatch[todays_dispatch["Total Load (Ton)"] > 0].reset_index(drop=True)
else:
    todays_dispatch = dispatch_all

# ---------------- LIVE LOAD STATUS SUMMARY ----------------
st.header(f"📦 Live Load Status — {as_of_date.strftime('%d %b %Y')}")
if not todays_dispatch.empty:
    _load_numeric = todays_dispatch["Total Load (Ton)"]
    _total_orders = len(todays_dispatch)
    _total_load = round(float(_load_numeric.sum()), 1)
    _dispatched_mask = todays_dispatch["Dispatch Status"] == "Dispatched"
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
    sl5.metric("🕒 Pending/Alloted Orders", f"{_pending_orders:,}")
    sl6.metric("🕒 Pending/Alloted Load (Ton)", f"{_pending_load:,}")
else:
    st.info(f"No shipments entered yet for {as_of_date.strftime('%d %b %Y')} — add rows on the "
            "🛠️ Route Creation page.")

st.write("---")

# ---------------- TODAY'S LOAD → TRUCKS → SPOT HIRE ----------------
with st.container(border=True):
    st.subheader("🚚 Today's Load → Trucks Needed → Spot Hire")
    st.caption("Every shipment entered on the Route Creation page for this date, in TONS. Pending shipments "
               "are matched live to the closest available vehicle tonnage; Alloted/Dispatched ones show their "
               "LOCKED vehicle — never recomputed once assigned.")

    if todays_dispatch.empty:
        st.info("Nothing to plan yet — add today's shipments on the 🛠️ Route Creation page.")
        alloc_results_df = pd.DataFrame()
        trucks_needed_today = own_used_today = fixed_used_today = spot_needed_today = 0
    else:
        display_cols = ["Route / Distributor", "Total Load (Ton)", "Dispatch Status", "Assigned Vehicle"]
        st.dataframe(todays_dispatch[display_cols], use_container_width=True, hide_index=True)

        locked_df = todays_dispatch[
            (todays_dispatch["Dispatch Status"].isin(["Alloted", "Dispatched"])) &
            (todays_dispatch["Assigned Vehicle"].astype(str).str.strip() != "")
        ].copy()
        pending_df = todays_dispatch[
            (todays_dispatch["Dispatch Status"] == "Pending") &
            (todays_dispatch["Assigned Vehicle"].astype(str).str.strip() == "")
        ].copy()

        # Deterministic ordering for the live-planned (Pending) shipments — same set of
        # inputs always allocates the same way, regardless of row order in the table.
        if not pending_df.empty:
            pending_df = pending_df.sort_values(
                ["Route / Distributor", "Total Load (Ton)"], kind="mergesort"
            ).reset_index(drop=True)

        veh_block_tons = edited_veh_block_live[["Vehicle", "TonnageNum"]].copy()
        veh_block_tons["TonnageNum"] = pd.to_numeric(veh_block_tons["TonnageNum"], errors="coerce")
        veh_block_tons = veh_block_tons.dropna(subset=["TonnageNum"])
        veh_block_tons["Capacity"] = veh_block_tons["TonnageNum"]

        db_capacity_live = load_db_capacity()

        # --- Build results for LOCKED (Alloted/Dispatched) shipments: show the vehicle
        # that's already assigned, looked up against the Vehicle Database for its real
        # size/ownership — never re-run through the allocator. ---
        veh_lookup = {}
        for _, v in veh_db.iterrows():
            vnum = str(v.get("Vehicle Number", "")).strip().upper()
            if vnum:
                veh_lookup[vnum] = v

        locked_rows = []
        for _, row in locked_df.iterrows():
            vnum = str(row["Assigned Vehicle"]).strip().upper()
            info = veh_lookup.get(vnum)
            if vnum == "(MARKET)" or vnum == "(market)".upper():
                source, truck_size = "Spot Hire", "—"
            elif info is not None:
                source = str(info.get("OwnershipType", "")).strip().title()
                truck_size = f"{info.get('CapacityTonnage'):g}T" if pd.notna(info.get("CapacityTonnage")) else "—"
            else:
                source, truck_size = "Unknown", "—"
            locked_rows.append({
                "Vehicle Number": vnum, "Truck Size": truck_size,
                "Load (Ton)": round(float(row["Total Load (Ton)"]), 2),
                "Source": source, "Distributor": row["Route / Distributor"],
                "Gate Out Date": as_of_date if row["Dispatch Status"] == "Dispatched" else "",
            })

        # --- Live-plan the remaining PENDING shipments — excluding any vehicle already
        # locked in above so nothing gets double-booked for two different shipments. ---
        reserved_vehicles = set(
            locked_df["Assigned Vehicle"].astype(str).str.strip().str.upper()
        ) - {"(MARKET)"}
        live_fleet_status_df = fleet_status_df
        if reserved_vehicles:
            live_fleet_status_df = fleet_status_df[
                ~fleet_status_df["Vehicle Number"].astype(str).str.strip().str.upper().isin(reserved_vehicles)
            ].copy()

        shipment_loads = pending_df["Total Load (Ton)"].tolist() if not pending_df.empty else []
        shipment_distributors = pending_df["Route / Distributor"].tolist() if not pending_df.empty else []
        shipment_max_tonnages = [match_distributor_cap(d, db_capacity_live) for d in shipment_distributors]

        capped_shipments = [
            (dist, cap) for dist, cap in zip(shipment_distributors, shipment_max_tonnages) if cap is not None
        ]
        if capped_shipments:
            cap_notes = ", ".join(f"{dist} (max {cap:g}T)" for dist, cap in sorted(set(capped_shipments)))
            st.caption(f"🚧 Distributor max-vehicle limits applied: {cap_notes}.")

        live_results = allocate_shipments_to_fleet(
            shipment_loads, live_fleet_status_df, veh_block_tons,
            buffer=buffer_tons_live, max_tonnage=max_tonnage_live if max_tonnage_live > 0 else None,
            distributors=shipment_distributors, max_tonnages=shipment_max_tonnages
        )
        for r in live_results:
            r["Gate Out Date"] = ""
        live_results_df = pd.DataFrame(live_results)
        if not live_results_df.empty:
            live_results_df = live_results_df.rename(columns={"Load": "Load (Ton)"})

        alloc_results_df = pd.concat(
            [pd.DataFrame(locked_rows), live_results_df], ignore_index=True
        ) if (locked_rows or not live_results_df.empty) else pd.DataFrame()
        if not alloc_results_df.empty:
            alloc_results_df = alloc_results_df[
                ["Vehicle Number", "Truck Size", "Load (Ton)", "Source", "Distributor", "Gate Out Date"]
            ]

        total_load_today = round(float(sum(shipment_loads)) + float(locked_df["Total Load (Ton)"].sum() if not locked_df.empty else 0), 1)
        trucks_needed_today = len(alloc_results_df)
        own_used_today = int((alloc_results_df["Source"] == "Own").sum()) if not alloc_results_df.empty else 0
        fixed_used_today = int((alloc_results_df["Source"] == "Fixed").sum()) if not alloc_results_df.empty else 0
        spot_needed_today = int((alloc_results_df["Source"] == "Spot Hire").sum()) if not alloc_results_df.empty else 0

        st.caption(f"{len(todays_dispatch)} shipments totaling {total_load_today:,} tons "
                   f"({len(locked_df)} locked, {len(pending_df)} live-planned, "
                   f"buffer {buffer_tons_live}T/truck) → **{trucks_needed_today:,} trucks needed today**.")

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
    avail_cols = [c for c in ["Vehicle Number", "OwnershipType", "Location", "Transporter Name", "CapacityTonnage", "Remarks"]
                  if c in fleet_status_df.columns]
    avail_show = fleet_status_df[fleet_status_df["Status"] == "Available"][avail_cols].copy()
    if "Remarks" in avail_show.columns:
        avail_show["Remarks"] = avail_show["Remarks"].replace("", "—")
    st.dataframe(avail_show, use_container_width=True, height=320, hide_index=True)
    st.download_button("⬇️ Download available vehicles (CSV)", avail_show.to_csv(index=False).encode("utf-8"),
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
    1. Dispatches are entered as rows on the **🛠️ Route Creation** page (Date, Route/Distributor, Total
       Load in Tons, Dispatch Status). No Google Sheet — it's a native table, shared instantly across pages
       in this session.
    2. Moving a row from **Pending → Alloted** locks in a real vehicle immediately (closest available
       tonnage, Own → Fixed → Spot Hire) — that vehicle never changes again for this shipment.
    3. Moving a row to **Dispatched** automatically creates its entry in the **Gate-Out Log** above
       (Vehicle Number, Ownership, Tonnage, Gate Out Date). Fill in **Actual Return Date** there once it's
       back — the vehicle flips straight back to Available.
    4. This page cross-references the Gate-Out Log against your **actual Vehicle Database**, so every truck
       is tracked individually — not just as an aggregate count. **Available = registered fleet − currently
       out**, giving real intraday status.
    5. Remaining **Pending** shipments are matched live to the closest AVAILABLE real vehicle tonnage
       (+ your overload buffer) — Own first (including any size only Own has), then Fixed, then Spot
       Hire — checked vehicle-by-vehicle against actual tonnage on hand, and never against a vehicle
       already locked in for another shipment today.
    """)
