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

    # Top block: Vehicle (e.g. "20T") -> Capacity (cases). 5 rows: 20T/18T/15T/9T/4T.
    veh_block = raw.iloc[0:5].copy()
    veh_block["Vehicle"] = veh_block["Vehicle"].astype(str).str.strip()
    veh_block["TonnageNum"] = veh_block["Vehicle"].str.extract(r"([\d.]+)").astype(float)
    veh_block["Capacity"] = pd.to_numeric(veh_block["Capacity"], errors="coerce")
    veh_block = veh_block.dropna(subset=["TonnageNum", "Capacity"]).sort_values("TonnageNum")

    # Bottom block: MTD Volume bucket -> Frequency (row 12 holds the sub-header, data starts row 13)
    freq_block = raw.iloc[11:14].copy()
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


def allocate_shipments_to_fleet(loads, fleet_status_df, veh_block, buffer=0, max_tonnage=None, distributors=None, max_tonnages=None):
    """
    Matches a list of INDIVIDUAL shipment loads (one per distributor/route — not one lump
    total) against your ACTUAL available fleet, vehicle by vehicle. This is what catches
    cases like "we need 11x 20T trucks but Own doesn't actually own any 20T vehicles" —
    a pure count-based check (do we have >= 11 vehicles available?) would miss that entirely.

    Ownership-tier waterfall, per shipment:
      1. OWN first — tries the full truck-size range in veh_block (including any size only
         Own actually has, e.g. a 20T/1200-case vehicle). Whatever Own's real available
         vehicles can genuinely cover gets assigned; anything they can't (a size needed but
         no matching Own vehicle free) rolls forward as still-unserved load.
      2. FIXED next, for whatever's left — capped at the largest tonnage any real Fixed
         vehicle actually is (derived from the fleet itself; falls back to 18T if there are
         no Fixed vehicles on record at all). Fixed never gets offered a size class it
         doesn't physically have, e.g. it's never asked to supply a 20T.
      3. SPOT HIRE ("market") absorbs whatever's still left — same size ceiling as Fixed
         (the market doesn't deal in a size your own Fixed fleet doesn't have either), but
         unlimited supply, so it always finishes covering the shipment.

    Every tier additionally respects that shipment's own max tonnage cap — the smaller of
    the global `max_tonnage` (if any) and that shipment's entry in `max_tonnages` (if any,
    e.g. a distributor's max allowed vehicle size due to road/gate access). A distributor
    capped at 4T will only ever be offered 4T (or smaller) trucks, however many it takes —
    never a bigger truck just because one happens to be free.

    Args:
        loads: list of case/ton loads (one per shipment)
        fleet_status_df: available vehicles dataframe
        veh_block: vehicle size ↔ capacity lookup table (Own's full range)
        buffer: overload buffer
        max_tonnage: optional global tonnage cap applied to every shipment
        distributors: optional list of distributor/route names (one per load) to include in results
        max_tonnages: optional list, same length as loads — a per-shipment tonnage cap
            (e.g. that distributor's max allowed vehicle size). None entries mean "no
            distributor-specific cap for this shipment".

    Returns a list of per-shipment dicts: {"Vehicle Number", "Truck Size", "Load",
    "Source", "Distributor"}. "Load" is whatever unit was passed in via `loads` (cases,
    tons, etc.) — this function doesn't care which, it just compares against veh_block's
    "Capacity" column in the same unit.
    """
    avail = fleet_status_df[fleet_status_df["Status"] == "Available"].copy()
    if "IsOperational" in avail.columns:
        avail = avail[avail["IsOperational"]].copy()
    avail["OwnershipType"] = avail["OwnershipType"].astype(str).str.title()
    avail["Vehicle Number"] = avail["Vehicle Number"].astype(str).str.strip().str.upper()
    avail["OwnershipOrder"] = avail["OwnershipType"].map({"Own": 0, "Fixed": 1}).fillna(2)
    avail = avail.sort_values([
        "OwnershipOrder", "CapacityTonnage", "Vehicle Number"
    ], kind="mergesort").drop(columns=["OwnershipOrder"])

    own_pool = avail[avail["OwnershipType"] == "Own"].to_dict("records")
    fixed_pool = avail[avail["OwnershipType"] == "Fixed"].to_dict("records")

    # Fixed & Spot Hire ("market") vehicles physically top out at whatever the largest real
    # OPERATIONAL Fixed vehicle in the WHOLE fleet is (not just currently-available ones —
    # this is a structural size ceiling, not an availability question; a non-operational
    # vehicle's spec shouldn't inflate what's realistically deployable). Falls back to 18T
    # if there's no operational Fixed vehicle on record at all.
    all_fixed = fleet_status_df[fleet_status_df["OwnershipType"].astype(str).str.title() == "Fixed"]
    if "IsOperational" in all_fixed.columns:
        all_fixed = all_fixed[all_fixed["IsOperational"]]
    fixed_spot_cap_tonnage = float(all_fixed["CapacityTonnage"].max()) if len(all_fixed) else 18.0

    capped_veh_block = veh_block[veh_block["TonnageNum"] <= fixed_spot_cap_tonnage + 1e-6].copy()
    if capped_veh_block.empty:
        capped_veh_block = veh_block.copy()

    tonnage_lookup = dict(zip(veh_block["Vehicle"], veh_block["TonnageNum"]))

    def real_size_label(vehicle):
        """Display label from the REAL assigned vehicle's own tonnage (e.g. '18T'), not the
        abstract size class used to search for it — these can differ for multi-truck combos,
        and showing the vehicle's actual size is what matters once a real vehicle is picked."""
        t = vehicle.get("CapacityTonnage")
        if t is None or pd.isna(t):
            return "?"
        return f"{t:g}T"

    results = []
    for i, load in enumerate(loads):
        if load is None or pd.isna(load) or load <= 0:
            continue
        distributor_label = distributors[i] if distributors and i < len(distributors) else ""

        # Effective cap for THIS shipment = the tighter of the global cap and this
        # shipment's own cap (e.g. a distributor's max allowed vehicle tonnage — road
        # width, gate access, etc.). A distributor capped at 4T only ever gets 4T-or-
        # smaller trucks, however many it takes — never a bigger one just because it's
        # free.
        shipment_cap = max_tonnage if (max_tonnage is not None and pd.notna(max_tonnage)) else None
        if max_tonnages is not None and i < len(max_tonnages):
            dist_cap = max_tonnages[i]
            if dist_cap is not None and pd.notna(dist_cap):
                shipment_cap = dist_cap if shipment_cap is None else min(shipment_cap, dist_cap)

        if shipment_cap is not None:
            shipment_fixed_spot_cap = min(shipment_cap, fixed_spot_cap_tonnage)
        else:
            shipment_fixed_spot_cap = fixed_spot_cap_tonnage

        def assign_from_real_pool(remaining_load, pool, source_label, tier_max_tonnage, tier_veh_block):
            """Covers remaining_load using this tier's REAL vehicle pool — matches directly
            against actual vehicle tonnages on hand (e.g. a real 6T, 6.5T or 7T vehicle can
            serve a 6-ton load) rather than quantizing to an abstract size bucket first.

            Avoids nickel-and-diming a shipment across many small vehicles when a better
            option plausibly exists elsewhere: compares against the THEORETICAL ideal truck
            count (using the tier's full size range, ignoring what's actually available
            right now). If that ideal is small (<=2 trucks), a compact real combo should
            exist somewhere — if this tier's real inventory can't match it, this tier
            contributes NOTHING and the whole load rolls to the next tier (e.g. Own having
            only small trucks defers a 22-ton load to Fixed's 18T, rather than sending 5
            small Own trucks). But if the ideal ITSELF needs 3+ trucks — a tight distributor
            tonnage cap, or a genuinely huge load — there's no better option to defer to, so
            this tier uses as many of its own available vehicles as it takes, rather than
            leaving a perfectly good real vehicle idle and sending everything to market."""
            if remaining_load <= 1e-6:
                return 0.0

            def eligible_idxs():
                return [i for i, v in enumerate(pool)
                        if tier_max_tonnage is None or v["CapacityTonnage"] <= tier_max_tonnage + 1e-6]

            # Try ONE real vehicle first — the smallest eligible one whose own tonnage
            # (+ buffer) fully covers the whole remaining load. This is the "closest
            # tonnage vehicle available" match for the common single-truck shipment.
            idxs = eligible_idxs()
            idx = next((i for i in idxs if pool[i]["CapacityTonnage"] + buffer >= remaining_load - 1e-6), None)
            if idx is not None:
                v = pool.pop(idx)
                results.append({
                    "Vehicle Number": v["Vehicle Number"],
                    "Truck Size": real_size_label(v),
                    "Load": round(float(load), 2),
                    "Source": source_label,
                    "Distributor": distributor_label,
                })
                return 0.0

            _, ideal_trucks = allocate_trucks_by_tonnage(remaining_load, tier_veh_block, tier_max_tonnage, buffer)
            truck_budget = max(2, ideal_trucks)

            if truck_budget <= 4:
                # Small enough to brute-force the tightest-fitting real combo.
                best_combo, best_excess = None, None
                for k in range(2, truck_budget + 1):
                    idxs = eligible_idxs()
                    if len(idxs) < k:
                        break
                    for combo in itertools.combinations(idxs, k):
                        total_cap = sum(pool[i]["CapacityTonnage"] + buffer for i in combo)
                        if total_cap >= remaining_load - 1e-6:
                            excess = total_cap - remaining_load
                            if best_excess is None or excess < best_excess:
                                best_combo, best_excess = combo, excess
                    if best_combo is not None:
                        break  # prefer the smallest k that has a valid combo
                if best_combo is not None:
                    for i in sorted(best_combo, reverse=True):  # highest index first, keeps remaining indices valid
                        v = pool.pop(i)
                        results.append({
                            "Vehicle Number": v["Vehicle Number"],
                            "Truck Size": real_size_label(v),
                            "Load": round(float(load), 2),
                            "Source": source_label,
                            "Distributor": distributor_label,
                        })
                    return 0.0
                if truck_budget <= 2:
                    # The ideal itself was compact and this tier's real inventory can't
                    # match it — a better option likely exists in the next tier.
                    return remaining_load
                # else: ideal needed 3-4 trucks but no combo of exactly that many exists
                # in the real pool (could still be short a vehicle or two) — fall through
                # to the greedy fallback below rather than giving up.

            # Reached only when the theoretical ideal itself needs several trucks (a tight
            # tonnage cap, or a genuinely huge load) — no better single/dual-truck option
            # exists anywhere, so use as many of this tier's own real vehicles as it takes.
            # Greedily take the LARGEST eligible vehicle each round (fewest trucks for the
            # capacity available), repeatedly, until covered or this tier's pool runs out.
            remaining = remaining_load
            while remaining > 1e-6:
                idxs = eligible_idxs()
                if not idxs:
                    break
                idx = max(idxs, key=lambda i: pool[i]["CapacityTonnage"])
                v = pool.pop(idx)
                results.append({
                    "Vehicle Number": v["Vehicle Number"],
                    "Truck Size": real_size_label(v),
                    "Load": round(float(load), 2),
                    "Source": source_label,
                    "Distributor": distributor_label,
                })
                remaining -= (v["CapacityTonnage"] + buffer)
            return max(0.0, remaining)

        remaining = float(load)
        remaining = assign_from_real_pool(remaining, own_pool, "Own", shipment_cap, veh_block)
        remaining = assign_from_real_pool(remaining, fixed_pool, "Fixed", shipment_fixed_spot_cap, capped_veh_block)

        if remaining > 1e-6:
            # Spot Hire — no real fleet to check against, so it falls back to the standard
            # size classes (veh_block), same size ceiling as Fixed (further capped to this
            # shipment's own limit). Unlimited supply, always finishes the job.
            plan, _ = allocate_trucks_by_tonnage(remaining, capped_veh_block, shipment_fixed_spot_cap, buffer)
            for label, n in sorted(plan.items(), key=lambda kv: tonnage_lookup.get(kv[0], 0)):
                for _ in range(n):
                    results.append({
                        "Vehicle Number": "(market)",
                        "Truck Size": label,
                        "Load": round(float(load), 2),
                        "Source": "Spot Hire",
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
    if "Route / Distributor" in d.columns:
        d["Route / Distributor"] = d["Route / Distributor"].astype(str).str.strip()
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


def _operational_mask(remarks_series, remarks_col_present):
    """True = operational.

    Strict whitelist per business rule: a vehicle is only usable for gate-out matching /
    availability if its Remarks explicitly says "operational" (case/whitespace-insensitive).
    Anything else — blank, "Vehicle Breakdown", "Driver not avaialble", or any other note —
    is excluded. Guards against "non operational"/"not operational" accidentally matching
    via the plain "operational" substring.

    If the Vehicle Database has NO Remarks column at all (older sheets, before this was
    tracked), every vehicle is treated as operational — that's a structural absence of the
    column, not a real "not operational" signal, so it shouldn't exclude anyone.
    """
    if not remarks_col_present:
        return pd.Series(True, index=remarks_series.index)
    normalized = remarks_series.astype(str).str.strip().str.lower()
    is_negated = normalized.str.contains(r'(?:non|not)[\s-]*operational', regex=True, na=False)
    says_operational = normalized.str.contains(r'operational', regex=True, na=False)
    return says_operational & ~is_negated


def cross_reference_fleet(veh_db, currently_out_df):
    """
    Matches the gate-out log's Vehicle Number against the actual Vehicle Database so the
    Live Tracker works at individual-vehicle granularity (not just aggregate counts).

    Only OPERATIONAL vehicles are considered — per business rule, a vehicle must have
    Remarks explicitly saying "operational" to be a real dispatch candidate; anything else
    (blank, under maintenance, driver unavailable, breakdown, etc.) is excluded entirely
    from both Available and Out. If the Vehicle Database has no Remarks column at all,
    every vehicle is treated as operational (unchanged, backward-compatible behavior).

    Every known vehicle is included in fleet_status_df (Available or Out) regardless of
    operational status — non-operational vehicles stay fully visible (with their Remarks)
    so you can see your whole fleet, not just the dispatchable subset. The 'IsOperational'
    column marks which ones are real dispatch candidates; allocate_shipments_to_fleet uses
    it to only draw from operational vehicles when assigning loads, without hiding the
    rest from these tables.

    Returns:
      - fleet_status_df: every known vehicle from veh_db with a 'Status' column
        ('Available' or 'Out'), 'Days Out' if out, 'Distributor' — the
        Route/Distributor recorded against that vehicle's gate-out entry, if the log
        includes that column — 'Remarks' (blank if the Vehicle Database has none), and
        'IsOperational' (True if the Vehicle Database has no Remarks column at all).
      - unmatched_df: gate-out log rows whose Vehicle Number isn't in the Vehicle
        Database at all (likely a typo) — flagged with a 'Reason' column.
    """
    fleet = veh_db.copy()
    fleet["Vehicle Number"] = fleet["Vehicle Number"].astype(str).str.strip().str.upper()
    fleet["OwnershipType"] = fleet["OwnershipType"].astype(str).str.title()

    remarks_col_present = "Remarks" in fleet.columns
    if remarks_col_present:
        fleet["Remarks"] = fleet["Remarks"].astype(str).str.strip().replace({"nan": "", "None": ""})
    else:
        fleet["Remarks"] = ""
    fleet["IsOperational"] = _operational_mask(fleet["Remarks"], remarks_col_present)

    fleet["OwnershipOrder"] = fleet["OwnershipType"].map({"Own": 0, "Fixed": 1}).fillna(2)
    fleet = fleet.sort_values([
        "OwnershipOrder", "CapacityTonnage", "Vehicle Number"
    ], kind="mergesort").drop(columns=["OwnershipOrder"])

    out_lookup = {}
    distributor_lookup = {}
    has_distributor_col = (
        currently_out_df is not None and not currently_out_df.empty
        and "Route / Distributor" in currently_out_df.columns
    )
    if currently_out_df is not None and not currently_out_df.empty:
        for _, r in currently_out_df.iterrows():
            out_lookup[r["Vehicle Number"]] = r.get("Days Out")
            if has_distributor_col:
                dist_val = r.get("Route / Distributor")
                if pd.notna(dist_val) and str(dist_val).strip():
                    distributor_lookup[r["Vehicle Number"]] = str(dist_val).strip()

    fleet["Status"] = fleet["Vehicle Number"].apply(lambda v: "Out" if v in out_lookup else "Available")
    fleet["Days Out"] = fleet["Vehicle Number"].apply(lambda v: out_lookup.get(v))
    fleet["Distributor"] = fleet["Vehicle Number"].apply(lambda v: distributor_lookup.get(v))

    unmatched_df = pd.DataFrame()
    if currently_out_df is not None and not currently_out_df.empty:
        known = set(fleet["Vehicle Number"])
        truly_unmatched = currently_out_df[~currently_out_df["Vehicle Number"].isin(known)].copy()
        if not truly_unmatched.empty:
            truly_unmatched["Reason"] = "Vehicle Number not found in Vehicle Database"
            unmatched_df = truly_unmatched

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
