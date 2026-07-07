"""
Central data loading / cleaning utilities for the Transport Management System (TMS).

All four dashboards (Overview, Route Creation, Fleet Calculator - Daily Load,
Fleet Calculator - Frequency Based) read from the SAME cleaned tables produced
here, so numbers stay consistent across pages.
"""

import os
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
                               return_rate=0.5, tat_days=2):
    """
    daily_requirements: list of ints/whole numbers — total trucks required each day, in order.

    Priority order every day: Own fleet first -> Fixed/Bachat fleet next -> Spot Hire (market)
    covers whatever is still short. Spot-hire trucks are arranged fresh from the market each
    day and are NOT tracked as "in transit" (unlimited same-day market supply, by definition).

    Own/Fixed vehicles used on a given day become unavailable ("in transit"); `return_rate`
    fraction of that day's dispatch is expected back the very next day, and the remaining
    fraction is back in the yard by `tat_days` days after dispatch.

    Returns a list of dict rows (one per day) — all values are whole numbers.
    """
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
            frac = fraction_still_out(gap, return_rate, tat_days)
            if frac > 0:
                own_still_out += own_dispatch_history[past_day] * frac
                fixed_still_out += fixed_dispatch_history[past_day] * frac

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
