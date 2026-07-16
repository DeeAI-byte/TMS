"""
Central data loading / cleaning utilities for the Transport Management System (TMS).

All four dashboards (Overview, Route Creation, Fleet Calculator - Daily Load,
Fleet Calculator - Frequency Based) read from the SAME cleaned tables produced
here, so numbers stay consistent across pages.
"""

import os
import math
import itertools
import numpy as np
import pandas as pd
import streamlit as st

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
LOCATIONS_FILE = os.path.join(DATA_DIR, "locations.xlsx")
CAPACITY_FILE = os.path.join(DATA_DIR, "Distributor_wise_Max_Vehicle_Capacity.xlsx")

MONTH_COLS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUNE", "JULY", "AUGUST",
              "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"]


# --------------------------------------------------------------------------------------
# LOCATIONS (Warehouses / Plants / DBR points)
# --------------------------------------------------------------------------------------
@st.cache_data
def load_locations():
    df = pd.read_excel(LOCATIONS_FILE)
    df.columns = df.columns.str.strip()
    cols = list(df.columns)
    name_col = cols[0]
    type_col = cols[1]
    lat_col = next((c for c in cols if 'lat' in c.lower()), cols[2])
    lon_col = next((c for c in cols if 'lon' in c.lower() or 'long' in c.lower()), cols[3])
    df = df.rename(columns={name_col: "Name", type_col: "Type", lat_col: "Latitude", lon_col: "Longitude"})
    df["Name"] = df["Name"].astype(str).str.strip()
    return df


# --------------------------------------------------------------------------------------
# DB CAPACITY  (Distributor -> Area/Town -> District -> Max Capacity Vehicle)
# --------------------------------------------------------------------------------------
@st.cache_data
def load_db_capacity():
    raw = pd.read_excel(CAPACITY_FILE, sheet_name="DB capacity", header=2)
    raw.columns = raw.columns.str.strip()
    if "Unnamed: 0" in raw.columns:
        raw = raw.drop(columns=["Unnamed: 0"])
    raw = raw.dropna(subset=["Agency / Route"]).copy()
    for c in ["Agency / Route", "Area / Town", "District", "Dsd & Hub"]:
        raw[c] = raw[c].astype(str).str.strip()
    raw["Max Capacity Vehicle"] = pd.to_numeric(raw["Max Capacity Vehicle"], errors="coerce")
    raw = raw.rename(columns={
        "Agency / Route": "Distributor",
        "Area / Town": "Town",
        "Dsd & Hub": "Channel",
        "Max Capacity Vehicle": "MaxVehicleTonnage",
    })
    return raw.reset_index(drop=True)


# --------------------------------------------------------------------------------------
# VEHICLE DATABASE (own + fixed fleet)
# --------------------------------------------------------------------------------------
@st.cache_data
def load_vehicle_database():
    df = pd.read_excel(CAPACITY_FILE, sheet_name="Vehicle Database")
    df.columns = df.columns.str.strip()
    df = df.rename(columns={
        "Capicity in Tonnage": "CapacityTonnage",
        "Palletized/ Non Palletized": "PalletType",
        "Vehicles Type": "OwnershipType",
    })
    df["Transporter Name"] = df["Transporter Name"].astype(str).str.strip()
    df["Location"] = df["Location"].astype(str).str.strip()
    df["OwnershipType"] = df["OwnershipType"].astype(str).str.strip()
    return df


# --------------------------------------------------------------------------------------
# ASSUMPTIONS  (Truck class -> case capacity, and MTD volume -> frequency bucket)
# --------------------------------------------------------------------------------------
@st.cache_data
def load_assumptions():
    raw = pd.read_excel(CAPACITY_FILE, sheet_name="Assumptions")
    raw.columns = [str(c).strip() for c in raw.columns]

    # Top block: Vehicle (e.g. "20T") -> Capacity (cases)
    veh_block = raw.iloc[0:4].copy()
    veh_block["Vehicle"] = veh_block["Vehicle"].astype(str).str.strip()
    veh_block["TonnageNum"] = veh_block["Vehicle"].str.extract(r"([\d.]+)").astype(float)
    veh_block["Capacity"] = pd.to_numeric(veh_block["Capacity"], errors="coerce")
    veh_block = veh_block.dropna(subset=["TonnageNum", "Capacity"]).sort_values("TonnageNum")

    # Bottom block: MTD Volume bucket -> Frequency (row 9 holds the sub-header, data starts row 10)
    freq_block = raw.iloc[10:13].copy()
    freq_block.columns = ["MTD Volume", "Frequency"]
    freq_block = freq_block.dropna(subset=["MTD Volume"])

    return veh_block[["Vehicle", "TonnageNum", "Capacity"]].reset_index(drop=True), \
        freq_block.reset_index(drop=True)


def cases_per_truck(tonnage, veh_block):
    """
    Convert a truck's tonnage capacity into an estimated case capacity by linear
    interpolation / extrapolation over the known (tonnage -> cases) points from
    the Assumptions sheet. Editable by the user in the sidebar of the calculator
    pages (pass a custom veh_block to override).
    """
    if pd.isna(tonnage):
        return np.nan
    x = veh_block["TonnageNum"].values
    y = veh_block["Capacity"].values
    return float(np.interp(tonnage, x, y))


def best_truck_for_tonnage_limit(max_tonnage, fleet_tonnages):
    """Largest available fleet truck size that still respects a distributor's max tonnage limit."""
    if pd.isna(max_tonnage):
        return max(fleet_tonnages) if len(fleet_tonnages) else np.nan
    eligible = [t for t in fleet_tonnages if t <= max_tonnage + 1e-6]
    if not eligible:
        return min(fleet_tonnages) if len(fleet_tonnages) else np.nan
    return max(eligible)


# --------------------------------------------------------------------------------------
# DB TARGET (monthly volume in cases per distributor)
# --------------------------------------------------------------------------------------
@st.cache_data
def load_db_target():
    df = pd.read_excel(CAPACITY_FILE, sheet_name="DB Target")
    df.columns = df.columns.str.strip()
    df["DISTRIBUTOR NAME"] = df["DISTRIBUTOR NAME"].astype(str).str.strip()
    for m in MONTH_COLS:
        df[m] = pd.to_numeric(df[m], errors="coerce")
    return df


# --------------------------------------------------------------------------------------
# MASTER JOINED TABLE used across all dashboards
# --------------------------------------------------------------------------------------
@st.cache_data
def build_master_table():
    cap = load_db_capacity()
    tgt = load_db_target()

    master = cap.merge(
        tgt[["DBR CODE", "DISTRIBUTOR NAME", "TOWN", "DISTRICT"] + MONTH_COLS],
        left_on="Distributor", right_on="DISTRIBUTOR NAME", how="left"
    )
    master = master.drop(columns=["DISTRIBUTOR NAME"])
    return master


def get_month_options():
    return MONTH_COLS


def get_memory_usage_mb():
    """
    Returns the CURRENT process's real memory usage in MB (RSS — actual physical memory
    in use), or None if psutil isn't available. This is the real number for this specific
    running instance, not an estimate — memory can vary run to run depending on what data
    is cached and how many browser sessions are connected.
    """
    try:
        import psutil
        process = psutil.Process(os.getpid())
        return process.memory_info().rss / (1024 * 1024)
    except Exception:
        return None


# --------------------------------------------------------------------------------------
# CLOSEST-TONNAGE TRUCK ALLOCATION (with overload buffer)
# --------------------------------------------------------------------------------------
def allocate_trucks_by_tonnage(load, veh_block, max_tonnage=None, buffer=0, max_combo_size=6):
    """
    Picks the best-fit truck(s) for a given case load:
    - Only considers truck sizes <= max_tonnage (the distributor/route's vehicle limit),
      falling back to all sizes if none qualify.
    - `buffer` cases of tolerated overload let a SINGLE truck absorb a small overage —
      e.g. a 1000-case truck with a 100-case buffer covers an 1100-case load as ONE truck
      instead of forcing a second truck for the last 100 cases.
    - If no single truck (+ buffer) covers the load, searches combinations of 2 or more
      trucks — ANY sizes, not just the largest — for the tightest-fitting combination
      (least wasted capacity). E.g. a 1200-case load prefers two 9T trucks (600 each =
      1200, exact fit) over two 20T trucks, if 9T trucks are an available size.
    - For loads far beyond what a few combined trucks can cover, falls back to as many
      of the largest truck as needed (searching every combination stops being practical
      at that scale, and repeated largest-truck is already near-optimal for bulk volume).

    Returns (plan_dict, total_trucks) where plan_dict maps a truck label (e.g. "20T") to
    how many of that size are needed.
    """
    if load is None or pd.isna(load) or load <= 0 or veh_block is None or len(veh_block) == 0:
        return {}, 0

    options = veh_block.copy()
    if max_tonnage is not None and pd.notna(max_tonnage):
        eligible = options[options["TonnageNum"] <= max_tonnage + 1e-6]
        if len(eligible) > 0:
            options = eligible
    # Stable sort (mergesort) so identical inputs always produce the same truck-size
    # ordering, even when two sizes share the same TonnageNum — matches the same
    # determinism guarantee already used for the vehicle pool sort below.
    options = options.sort_values("TonnageNum", kind="mergesort")

    # Step 1: does a single truck (+ buffer) cover it alone?
    for _, row in options.iterrows():
        cap = row["Capacity"] + buffer
        if load <= cap:
            return {row["Vehicle"]: 1}, 1

    # Step 2: search increasing truck counts for the tightest-fitting combination of ANY sizes
    sizes = list(zip(options["Vehicle"], options["Capacity"]))
    for count in range(2, max_combo_size + 1):
        best_combo, best_excess = None, None
        for combo in itertools.combinations_with_replacement(sizes, count):
            total_cap = sum(cap for _, cap in combo)
            if total_cap >= load:
                excess = total_cap - load
                if best_excess is None or excess < best_excess:
                    best_combo, best_excess = combo, excess
        if best_combo is not None:
            plan = {}
            for label, _ in best_combo:
                plan[label] = plan.get(label, 0) + 1
            return plan, count

    # Step 3: fallback for very large loads — as many of the largest truck as needed
    largest = options.iloc[-1]
    cap = largest["Capacity"] + buffer
    count = int(math.ceil(load / cap)) if cap > 0 else 0
    return {largest["Vehicle"]: count}, count


def allocate_shipments_to_fleet(loads, fleet_status_df, veh_block, buffer=0, max_tonnage=None, distributors=None):
    """
    Matches a list of INDIVIDUAL shipment loads (one per distributor/route — not one lump
    total) against your ACTUAL available fleet, vehicle by vehicle. This is what catches
    cases like "we need 11x 20T trucks but Own doesn't actually own any 20T vehicles" —
    a pure count-based check (do we have >= 11 vehicles available?) would miss that entirely.

    For each shipment: figures out the smallest truck size that covers it (+ buffer), then
    tries to assign the smallest AVAILABLE real vehicle of at least that tonnage — Own pool
    first, then Fixed, then Spot Hire if neither has a suitable vehicle left. Assigned
    vehicles are removed from the pool so they aren't double-counted for the next shipment.

    Args:
        loads: list of case loads (one per shipment)
        fleet_status_df: available vehicles dataframe
        veh_block: vehicle size ↔ capacity lookup table
        buffer: overload buffer in cases
        max_tonnage: optional tonnage cap
        distributors: optional list of distributor/route names (one per load) to include in results

    Returns a list of per-shipment dicts: {"Vehicle Number", "Truck Size", "Load (cases)",
    "Source", "Distributor"}.
    """
    avail = fleet_status_df[fleet_status_df["Status"] == "Available"].copy()
    avail["OwnershipType"] = avail["OwnershipType"].astype(str).str.title()
    avail["Vehicle Number"] = avail["Vehicle Number"].astype(str).str.strip().str.upper()
    avail["OwnershipOrder"] = avail["OwnershipType"].map({"Own": 0, "Fixed": 1}).fillna(2)
    avail = avail.sort_values([
        "OwnershipOrder", "CapacityTonnage", "Vehicle Number"
    ], kind="mergesort").drop(columns=["OwnershipOrder"])

    own_pool = avail[avail["OwnershipType"] == "Own"].to_dict("records")
    fixed_pool = avail[avail["OwnershipType"] == "Fixed"].to_dict("records")
    tonnage_lookup = dict(zip(veh_block["Vehicle"], veh_block["TonnageNum"]))

    results = []
    for i, load in enumerate(loads):
        if load is None or pd.isna(load) or load <= 0:
            continue
        distributor_label = distributors[i] if distributors and i < len(distributors) else ""
        plan, _ = allocate_trucks_by_tonnage(load, veh_block, max_tonnage, buffer)
        for label, n in plan.items():
            tonnage_needed = tonnage_lookup.get(label, 0)
            for _ in range(n):
                assigned_vehicle = None
                source = None
                idx = next((i for i, v in enumerate(own_pool) if v["CapacityTonnage"] >= tonnage_needed - 1e-6), None)
                if idx is not None:
                    assigned_vehicle = own_pool.pop(idx)
                    source = "Own"
                else:
                    idx = next((i for i, v in enumerate(fixed_pool) if v["CapacityTonnage"] >= tonnage_needed - 1e-6), None)
                    if idx is not None:
                        assigned_vehicle = fixed_pool.pop(idx)
                        source = "Fixed"
                    else:
                        source = "Spot Hire"
                results.append({
                    "Vehicle Number": assigned_vehicle["Vehicle Number"] if assigned_vehicle else "(market)",
                    "Truck Size": label,
                    "Load (cases)": int(round(load)),
                    "Source": source,
                    "Distributor": distributor_label,
                })
    return results


# --------------------------------------------------------------------------------------
# LIVE FLEET TRACKER — process a gate-out log (e.g. from a Google Sheet) into
# currently-out / available counts, for real intraday spot-hire planning.
# --------------------------------------------------------------------------------------
def process_gate_out_log(df, as_of_date):
    """
    df must have columns: 'Vehicle Number', 'Ownership', 'Gate Out Date',
    optionally 'Actual Return Date' and 'Route / Distributor'.

    A vehicle is "currently out" as of as_of_date if its Gate Out Date <= as_of_date
    AND (Actual Return Date is blank OR Actual Return Date > as_of_date).

    Dates are parsed day-first (DD-MM-YYYY), matching Indian date conventions.

    Returns (processed_df, currently_out_df) — processed_df has a boolean 'Currently Out'
    and integer 'Days Out' column; currently_out_df is filtered to only vehicles still out.
    """
    d = df.copy()
    d.columns = [str(c).strip() for c in d.columns]
    d = d.replace(r'^\s*$', pd.NA, regex=True)

    if d.empty:
        return d, d.copy()

    d["Vehicle Number"] = d["Vehicle Number"].astype(str).str.strip().str.upper()
    d["Ownership"] = d["Ownership"].astype(str).str.strip().str.title()
    d["Gate Out Date"] = pd.to_datetime(d["Gate Out Date"], errors="coerce", dayfirst=True)
    if "Actual Return Date" in d.columns:
        d["Actual Return Date"] = pd.to_datetime(d["Actual Return Date"], errors="coerce", dayfirst=True)
    else:
        d["Actual Return Date"] = pd.NaT

    d = d.dropna(how="all")
    if d.empty:
        return d, d.copy()

    as_of_timestamp = pd.to_datetime(as_of_date)
    d["Currently Out"] = (
        d["Gate Out Date"].notna() &
        (d["Gate Out Date"] <= as_of_timestamp) &
        (
            d["Actual Return Date"].isna() |
            (d["Actual Return Date"] > as_of_timestamp)
        )
    )

    d["Days Out"] = None
    if d["Currently Out"].any():
        d.loc[d["Currently Out"], "Days Out"] = (
            (as_of_timestamp - d.loc[d["Currently Out"], "Gate Out Date"]).dt.days
        )

    currently_out_df = d[d["Currently Out"]].copy()
    return d, currently_out_df


def already_dispatched_routes(log_df, as_of_date):
    """
    Returns the set of normalized Route/Distributor names that ALREADY have a real
    gate-out recorded (in the Vehicle/gate-out log) with Gate Out Date == as_of_date.

    Why this exists: the Load Log's own 'Dispatch Status' column is the primary
    signal that a shipment is done, but it depends on someone remembering to flip
    it in a SECOND sheet the moment they fill in the gate-out log. If that step
    lags (or is skipped), the shipment would otherwise still show as 'Pending' and
    get re-planned against whatever vehicles happen to still be available — handing
    it a DIFFERENT vehicle than the one that was actually just dispatched for it.

    Cross-checking the gate-out log's own Route/Distributor + Gate Out Date closes
    that gap using data the Transport Team is already recording, without writing
    anything back to either sheet. A shipment is treated as executed as soon as
    EITHER signal (Dispatch Status = Dispatched, OR a matching gate-out entry for
    today) says so.

    Matching is normalized (strip + casefold) and blank/missing Route/Distributor
    values are ignored (never treated as a match), so real shipments are never
    "eaten" by unrelated fleet log entries.
    """
    if log_df is None or len(log_df) == 0:
        return set()
    d = log_df.copy()
    d.columns = [str(c).strip() for c in d.columns]
    if "Route / Distributor" not in d.columns or "Gate Out Date" not in d.columns:
        return set()

    d = d.replace(r'^\s*$', pd.NA, regex=True)
    d["Gate Out Date"] = pd.to_datetime(d["Gate Out Date"], errors="coerce", dayfirst=True)
    as_of_timestamp = pd.to_datetime(as_of_date)

    matched = d[
        d["Route / Distributor"].notna() &
        d["Gate Out Date"].notna() &
        (d["Gate Out Date"].dt.normalize() == as_of_timestamp.normalize())
    ]
    routes = (
        matched["Route / Distributor"]
        .astype(str).str.strip().str.casefold()
    )
    return set(r for r in routes if r)


def validate_ownership_values(df):
    """
    Returns the set of distinct 'Ownership' values in the log that DON'T match
    'Own' or 'Fixed' (case-insensitive) — a common data-entry mistake (e.g. someone
    typing the column header 'Ownership' into the cells instead of an actual value).
    """
    if df is None or df.empty or "Ownership" not in df.columns:
        return []
    vals = df["Ownership"].astype(str).str.strip().str.title().unique().tolist()
    return [v for v in vals if v not in ("Own", "Fixed")]


def cross_reference_fleet(veh_db, currently_out_df):
    """
    Matches the gate-out log's Vehicle Number against the actual Vehicle Database so the
    Live Tracker works at individual-vehicle granularity (not just aggregate counts).

    Returns:
      - fleet_status_df: every known vehicle from veh_db with a 'Status' column
        ('Available' or 'Out') and, if out, 'Days Out'.
      - unmatched_df: rows in the gate-out log whose Vehicle Number isn't in veh_db
        (data-entry mismatches to flag, e.g. typos or vehicles outside the registered fleet).
    """
    fleet = veh_db.copy()
    fleet["Vehicle Number"] = fleet["Vehicle Number"].astype(str).str.strip().str.upper()
    fleet["OwnershipType"] = fleet["OwnershipType"].astype(str).str.title()
    fleet["OwnershipOrder"] = fleet["OwnershipType"].map({"Own": 0, "Fixed": 1}).fillna(2)
    fleet = fleet.sort_values([
        "OwnershipOrder", "CapacityTonnage", "Vehicle Number"
    ], kind="mergesort").drop(columns=["OwnershipOrder"])

    out_lookup = {}
    if currently_out_df is not None and not currently_out_df.empty:
        for _, r in currently_out_df.iterrows():
            out_lookup[r["Vehicle Number"]] = r.get("Days Out")

    fleet["Status"] = fleet["Vehicle Number"].apply(lambda v: "Out" if v in out_lookup else "Available")
    fleet["Days Out"] = fleet["Vehicle Number"].apply(lambda v: out_lookup.get(v))

    unmatched_df = pd.DataFrame()
    if currently_out_df is not None and not currently_out_df.empty:
        known = set(fleet["Vehicle Number"])
        unmatched_df = currently_out_df[~currently_out_df["Vehicle Number"].isin(known)].copy()

    return fleet, unmatched_df


# --------------------------------------------------------------------------------------
# FREQUENCY-BASED DAY SCHEDULING — the key fix: distributors visited less often should
# genuinely NOT need a truck on their off-days, instead of averaging total need flatly
# across every working day (which erases the benefit of the frequency model).
# --------------------------------------------------------------------------------------
def compute_frequency_daily_schedule(f, working_days_per_week, weeks_in_month):
    """
    Assigns each distributor to specific day-of-week slots based on its TripsPerWeek
    (e.g. 2/week -> serviced on 2 of the working days each week, not all of them), then
    sums TrucksPerTrip only on the days each distributor is actually scheduled.

    Distributors sharing the same frequency are staggered across different day-of-week
    offsets (like real route planning assigns different "Monday routes", "Tuesday routes",
    etc.) so lower-frequency distributors spread evenly across the week instead of all
    piling onto the exact same 2 days and creating artificial demand spikes.

    Returns a list of whole-number daily truck requirements, one per working day of
    the month (length = round(working_days_per_week * weeks_in_month)).
    """
    wdw = max(1, int(round(working_days_per_week)))
    total_days = max(1, int(round(wdw * weeks_in_month)))
    daily_totals = [0.0] * total_days

    for i, (_, row) in enumerate(f.iterrows()):
        trucks_per_trip = row.get("TrucksPerTrip", None)
        trips_per_week = row.get("TripsPerWeek", None)
        if pd.isna(trucks_per_trip) or pd.isna(trips_per_week):
            continue
        trucks_per_trip = float(trucks_per_trip)
        k = max(1, min(wdw, int(round(trips_per_week))))

        # stagger the starting offset per distributor so same-frequency distributors
        # don't all land on the identical days — spreads route load across the week
        offset = i % wdw
        day_slots = sorted(set((offset + int(round(j * wdw / k))) % wdw for j in range(k)))

        for day_idx in range(total_days):
            if (day_idx % wdw) in day_slots:
                daily_totals[day_idx] += trucks_per_trip

    return [int(round(x)) for x in daily_totals]


# --------------------------------------------------------------------------------------
# FLEET TOTALS (Own vs Fixed/Bachat) — used by the priority + TAT allocation simulator
# --------------------------------------------------------------------------------------
def fleet_totals_by_ownership(veh_db):
    own_total = int((veh_db["OwnershipType"] == "Own").sum())
    fixed_total = int((veh_db["OwnershipType"] == "Fixed").sum())
    return own_total, fixed_total


# --------------------------------------------------------------------------------------
# PRIORITY + TAT (TURN-AROUND-TIME) DAILY FLEET ALLOCATION SIMULATOR
# --------------------------------------------------------------------------------------
def fraction_still_out(gap_days, return_rate, tat_days):
    """
    gap_days = number of days since a batch of vehicles was dispatched.
    Returns the fraction of that batch still unavailable (out on trip) today.

    - gap 0  (dispatched today)      -> 100% still out
    - 1 <= gap < tat_days            -> (1 - return_rate) still out (a portion trickles back next day)
    - gap >= tat_days                -> 0% still out (fully back in the yard)
    """
    if gap_days <= 0:
        return 1.0
    if gap_days >= tat_days:
        return 0.0
    return max(0.0, 1.0 - return_rate)


def simulate_daily_allocation(daily_requirements, own_total, fixed_total,
                               return_rate=0.4, tat_days=2,
                               fixed_return_rate=None, fixed_tat_days=None):
    """
    daily_requirements: list of ints/whole numbers — total trucks required each day, in order.

    Priority order every day: Own fleet first -> Fixed/Bachat fleet next -> Spot Hire (market)
    covers whatever is still short. Spot-hire trucks are arranged fresh from the market each
    day and are NOT tracked as "in transit" (unlimited same-day market supply, by definition).

    Own/Fixed vehicles used on a given day become unavailable ("in transit"); `return_rate`
    fraction of that day's dispatch is expected back the very next day, and the remaining
    fraction is back in the yard by `tat_days` days after dispatch.

    Own and Fixed fleets can have DIFFERENT turn-around behavior (validated against real
    June actuals — Fixed/Bachat trucks showed noticeably lower effective daily utilization
    than Own, likely due to dedicated routes/contracts not visible in aggregate data). If
    fixed_return_rate/fixed_tat_days are not given, Fixed uses the same params as Own.

    Returns a list of dict rows (one per day) — all values are whole numbers.
    """
    if fixed_return_rate is None:
        fixed_return_rate = return_rate
    if fixed_tat_days is None:
        fixed_tat_days = tat_days

    n_days = len(daily_requirements)
    own_dispatch_history = [0] * n_days   # own trucks dispatched on each day index
    fixed_dispatch_history = [0] * n_days

    rows = []
    for day in range(n_days):
        # --- how many are still out from previous dispatches ---
        own_still_out = 0.0
        fixed_still_out = 0.0
        for past_day in range(0, day + 1):
            gap = day - past_day
            own_frac = fraction_still_out(gap, return_rate, tat_days)
            fixed_frac = fraction_still_out(gap, fixed_return_rate, fixed_tat_days)
            if own_frac > 0:
                own_still_out += own_dispatch_history[past_day] * own_frac
            if fixed_frac > 0:
                fixed_still_out += fixed_dispatch_history[past_day] * fixed_frac

        own_available = max(0, own_total - int(round(own_still_out)))
        fixed_available = max(0, fixed_total - int(round(fixed_still_out)))

        need = int(round(daily_requirements[day]))

        own_used = min(need, own_available)
        remaining = need - own_used
        fixed_used = min(remaining, fixed_available)
        remaining -= fixed_used
        spot_used = max(0, remaining)  # market arranged same day, no cap

        own_dispatch_history[day] = own_used
        fixed_dispatch_history[day] = fixed_used

        rows.append({
            "Day": day + 1,
            "Trucks Required": need,
            "Own Available": own_available,
            "Own Used": own_used,
            "Own Idle": own_available - own_used,
            "Fixed Available": fixed_available,
            "Fixed Used": fixed_used,
            "Fixed Idle": fixed_available - fixed_used,
            "Spot Hire Used": spot_used,
            "Own In-Transit (after today)": int(round(own_still_out)) + own_used,
            "Fixed In-Transit (after today)": int(round(fixed_still_out)) + fixed_used,
        })

    return rows
