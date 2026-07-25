import streamlit as st
import pandas as pd
import folium
from streamlit_folium import st_folium
import itertools
import os
import requests
from folium.plugins import PolyLineTextPath
from datetime import date
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.data_loader import (
    load_locations, load_vehicle_database, load_assumptions,
    cross_reference_fleet, allocate_shipments_to_fleet
)

# Page Layout Configurations
st.set_page_config(page_title="Interactive Logistics Router", page_icon="🛠️", layout="wide")
st.markdown("<style>.block-container { padding-top: 1rem; padding-bottom: 0.5rem; }</style>", unsafe_allow_html=True)

st.title("🛠️ Route Creation & Dispatch Management")
st.caption("Punch dispatch details directly below — replaces external Google Sheets with live fleet locking & routing.")
st.write("---")

# Load Locations & Vehicle Database
try:
    df_loc = load_locations()
    wh_df = df_loc[df_loc["Type"].isin(["Warehouse", "Plant"])]
    dbr_df = df_loc[df_loc["Type"] == "DBR"]
    
    plant_list = sorted(wh_df["Name"].dropna().unique())
    dbr_list = sorted(dbr_df["Name"].dropna().unique())
    veh_db = load_vehicle_database()
    veh_block, _ = load_assumptions()
except Exception as e:
    st.error(f"⚠️ Could not load database files: {e}")
    st.stop()

# Initialize Session State Data Tables
if "dispatch_table" not in st.session_state:
    st.session_state.dispatch_table = pd.DataFrame({
        "Date": [date.today()],
        "Plant": [plant_list[0] if plant_list else "Plant 1"],
        "Route / Distributor": [dbr_list[0] if dbr_list else "None"],
        "Total Load (Ton)": [10.0],
        "Dispatch Status": ["Pending"],
        "Assigned Vehicle": ["Unassigned"]
    })

if "gate_out_log" not in st.session_state:
    st.session_state.gate_out_log = pd.DataFrame(columns=[
        "Vehicle Number", "Tonnage", "Ownership", "Gate Out Date",
        "Actual Return Date", "Route / Distributor"
    ])

# Helper Function: OSRM Road Distance & Geometry
def get_road_route_and_distance(coords_list):
    loc_string = ";".join([f"{lon},{lat}" for lat, lon in coords_list])
    url = f"http://router.project-osrm.org/route/v1/driving/{loc_string}?overview=full&geometries=geojson"
    try:
        response = requests.get(url, timeout=5).json()
        if response.get("code") == "Ok":
            route = response["routes"][0]
            distance_km = route["distance"] / 1000.0
            road_geometry = [(lat, lon) for lon, lat in route["geometry"]["coordinates"]]
            return distance_km, road_geometry
    except Exception:
        pass
    return None, None

# ---------------- INTERACTIVE DISPATCH TABLE ----------------
st.subheader("📋 Route & Load Entry Table")

edited_df = st.data_editor(
    st.session_state.dispatch_table,
    num_rows="dynamic",
    use_container_width=True,
    key="dispatch_table_editor",
    column_config={
        "Date": st.column_config.DateColumn("Date", default=date.today()),
        "Plant": st.column_config.SelectboxColumn("Plant / Warehouse", options=plant_list, required=True),
        "Route / Distributor": st.column_config.SelectboxColumn("Route / Distributor", options=dbr_list, required=True),
        "Total Load (Ton)": st.column_config.NumberColumn("Total Load (Ton)", min_value=0.0, step=0.5, format="%.1f"),
        "Dispatch Status": st.column_config.SelectboxColumn(
            "Dispatch Status", 
            options=["Pending", "Alloted", "Dispatched", "Cancelled"], 
            default="Pending"
        ),
        "Assigned Vehicle": st.column_config.TextColumn("Assigned Vehicle", disabled=True)
    }
)

# ---------------- STATE TRANSITION & VEHICLE LOCKING LOGIC ----------------
processed_df = edited_df.copy()
currently_out_df = st.session_state.gate_out_log.copy()
fleet_status_df, _ = cross_reference_fleet(veh_db, currently_out_df)

veh_block_tons = veh_block[["Vehicle", "TonnageNum"]].copy()
veh_block_tons["TonnageNum"] = pd.to_numeric(veh_block_tons["TonnageNum"], errors="coerce")
veh_block_tons = veh_block_tons.dropna(subset=["TonnageNum"])
veh_block_tons["Capacity"] = veh_block_tons["TonnageNum"]

gate_out_rows = list(st.session_state.gate_out_log.to_dict("records"))

for i, row in processed_df.iterrows():
    status = row["Dispatch Status"]
    current_assigned = row["Assigned Vehicle"]
    distributor = row["Route / Distributor"]
    load_ton = row["Total Load (Ton)"]
    dispatch_date = row["Date"]

    # 1. Handle ALLOTED status (Auto-match and lock vehicle)
    if status == "Alloted" and (current_assigned in ["Unassigned", "", None] or current_assigned.startswith("(market)")):
        alloc_res = allocate_shipments_to_fleet(
            loads=[load_ton],
            fleet_status_df=fleet_status_df,
            veh_block=veh_block_tons,
            buffer=1.0,
            distributors=[distributor]
        )
        if alloc_res:
            assigned_v = alloc_res[0]["Vehicle Number"]
            processed_df.at[i, "Assigned Vehicle"] = assigned_v
            # Temporarily set vehicle as Out so next row won't double-book it
            if assigned_v != "(market)":
                fleet_status_df.loc[fleet_status_df["Vehicle Number"] == assigned_v, "Status"] = "Out"

    # 2. Handle DISPATCHED status (Lock vehicle & Trigger Gate-Out record)
    elif status == "Dispatched":
        v_num = current_assigned
        if v_num in ["Unassigned", "", None]:
            alloc_res = allocate_shipments_to_fleet(
                loads=[load_ton],
                fleet_status_df=fleet_status_df,
                veh_block=veh_block_tons,
                buffer=1.0,
                distributors=[distributor]
            )
            v_num = alloc_res[0]["Vehicle Number"] if alloc_res else "(market)"
            processed_df.at[i, "Assigned Vehicle"] = v_num

        # Check if already logged in Gate-Out
        already_logged = any(
            g["Vehicle Number"] == v_num and str(g["Route / Distributor"]) == str(distributor)
            for g in gate_out_rows
        )
        if not already_logged and v_num != "(market)":
            v_info = veh_db[veh_db["Vehicle Number"] == v_num]
            v_tonnage = v_info.iloc[0]["CapacityTonnage"] if not v_info.empty else 0.0
            v_ownership = v_info.iloc[0]["OwnershipType"] if not v_info.empty else "Own"

            gate_out_rows.append({
                "Vehicle Number": v_num,
                "Tonnage": v_tonnage,
                "Ownership": v_ownership,
                "Gate Out Date": str(dispatch_date),
                "Actual Return Date": "",
                "Route / Distributor": distributor
            })

st.session_state.dispatch_table = processed_df
st.session_state.gate_out_log = pd.DataFrame(gate_out_rows)

st.write("---")

# ---------------- MAP & ROUTE OPTIMIZATION ----------------
row_options = [
    f"Row {i+1} — {r['Plant']} ➔ {r['Route / Distributor']} ({r['Dispatch Status']} | {r['Assigned Vehicle']})" 
    for i, r in processed_df.iterrows()
]

if row_options:
    selected_idx = st.selectbox("📌 Select a row from the table to map & inspect route:", range(len(row_options)), format_func=lambda x: row_options[x])
    selected_row = processed_df.iloc[selected_idx]

    sel_plant = selected_row["Plant"]
    sel_dbr = selected_row["Route / Distributor"]
    total_load = selected_row["Total Load (Ton)"]
    dispatch_status = selected_row["Dispatch Status"]
    assigned_v = selected_row["Assigned Vehicle"]

    # Coordinates calculation
    plant_row = df_loc[df_loc["Name"] == sel_plant]
    dbr_row = df_loc[df_loc["Name"] == sel_dbr]

    if not plant_row.empty and not dbr_row.empty:
        p_coords = (float(plant_row.iloc[0]["Latitude"]), float(plant_row.iloc[0]["Longitude"]))
        d_coords = (float(dbr_row.iloc[0]["Latitude"]), float(dbr_row.iloc[0]["Longitude"]))

        dist_km, road_geom = get_road_route_and_distance([p_coords, d_coords, p_coords])

        c1, c2 = st.columns([1, 3])
        with c1:
            st.subheader("📊 Route Overview")
            st.metric("Total Load", f"{total_load} Ton")
            st.metric("Status", dispatch_status)
            st.metric("Assigned Truck", assigned_v)
            if dist_km:
                st.metric("Road Distance", f"{round(dist_km, 2)} KM")
            st.write(f"🏭 **Origin:** {sel_plant}")
            st.write(f"📍 **Destination:** {sel_dbr}")

        with c2:
            m = folium.Map(location=p_coords, zoom_start=9)
            folium.Marker(p_coords, popup=f"Plant: {sel_plant}", icon=folium.Icon(color="red", icon="industry", prefix="fa")).add_to(m)
            folium.Marker(d_coords, popup=f"DBR: {sel_dbr}", icon=folium.Icon(color="blue", icon="shopping-cart")).add_to(m)

            if road_geom:
                line = folium.PolyLine(road_geom, color="#1b4fd2", weight=5, opacity=0.85).add_to(m)
                PolyLineTextPath(line, '  ►  ', repeat=True, offset=8, attributes={'fill': '#ffffff', 'font-weight': 'bold'}).add_to(m)

            st_folium(m, width=900, height=500, key="route_map", returned_objects=[])
