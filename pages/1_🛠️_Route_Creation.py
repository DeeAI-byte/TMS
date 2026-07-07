import streamlit as st
import pandas as pd
import folium
from streamlit_folium import st_folium
import itertools
import os
import requests
from folium.plugins import PolyLineTextPath

# Page Layout Configurations
st.set_page_config(page_title="Interactive Logistics Router", page_icon="🛠️", layout="wide")

# Safe Formatting (No extra background color blocks)
st.markdown("<style>.block-container { padding-top: 1rem; padding-bottom: 0.5rem; }</style>", unsafe_allow_html=True)

# Streamlit Native Header
st.caption("🥤 Coca-Cola - SLMG")
st.title("🛠️ Route Creation")
st.write("---")

excel_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "locations.xlsx")

# OSRM Road Distance & Path Generator
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

if not os.path.exists(excel_file):
    st.error(f"⚠️ '{excel_file}' file nahi mili!")
else:
    df = pd.read_excel(excel_file)
    df.columns = df.columns.str.strip()
    cols = list(df.columns)

    name_col = cols[0]
    type_col = cols[1]
    lat_col = next((c for c in cols if 'lat' in c.lower()), cols[2])
    lon_col = next((c for c in cols if 'lon' in c.lower() or 'long' in c.lower()), cols[3])

    # Filter Datasets
    wh_df = df[df[type_col].isin(['Warehouse', 'Plant'])]
    dbr_df = df[df[type_col] == 'DBR']
    dbr_list = ["None"] + list(dbr_df[name_col].unique())

    # 🌟 WAREHOUSE SELECT MEIN "NONE" KA OPTION JOD DIYA HAI
    wh_list = ["None"] + list(wh_df[name_col].unique())

    # --- STATE MANAGEMENT ---
    if 'sel_wh' not in st.session_state: st.session_state.sel_wh = wh_list[1] if len(wh_list) > 1 else "None"
    if 'sel_d1' not in st.session_state: st.session_state.sel_d1 = "None"
    if 'sel_d2' not in st.session_state: st.session_state.sel_d2 = "None"
    if 'sel_d3' not in st.session_state: st.session_state.sel_d3 = "None"

    # --- SIDEBAR PANEL ---
    st.sidebar.header("🕹️ Configuration")

    if st.sidebar.button("🧹 Clear Route", use_container_width=True):
        st.session_state.sel_wh = wh_list[1] if len(wh_list) > 1 else "None"
        st.session_state.sel_d1 = "None"
        st.session_state.sel_d2 = "None"
        st.session_state.sel_d3 = "None"
        st.rerun()

    st.sidebar.write("---")

    def update_wh(): st.session_state.sel_wh = st.session_state.sb_wh
    def update_d1(): st.session_state.sel_d1 = st.session_state.sb_d1
    def update_d2(): st.session_state.sel_d2 = st.session_state.sb_d2
    def update_d3(): st.session_state.sel_d3 = st.session_state.sb_d3

    st.sidebar.selectbox("Warehouse/Plant:", wh_list, key="sb_wh", index=wh_list.index(st.session_state.sel_wh), on_change=update_wh)

    st.sidebar.write("**Selected Route DBRs:**")
    st.sidebar.selectbox("DBR 1:", dbr_list, key="sb_d1", index=dbr_list.index(st.session_state.sel_d1), on_change=update_d1)
    st.sidebar.selectbox("DBR 2:", dbr_list, key="sb_d2", index=dbr_list.index(st.session_state.sel_d2), on_change=update_d2)
    st.sidebar.selectbox("DBR 3:", dbr_list, key="sb_d3", index=dbr_list.index(st.session_state.sel_d3), on_change=update_d3)

    active_dbrs = [d for d in [st.session_state.sel_d1, st.session_state.sel_d2, st.session_state.sel_d3] if d != "None"]

    # Coordinates mapping logic based on selections
    selected_coords = []
    selected_names = []

    if st.session_state.sel_wh != "None":
        w_r = df[df[name_col] == st.session_state.sel_wh].iloc[0]
        selected_coords.append((float(w_r[lat_col]), float(w_r[lon_col])))
        selected_names.append(w_r[name_col])
        # Map centering location determination
        center_lat, center_lon = float(w_r[lat_col]), float(w_r[lon_col])
    else:
        # Default map center if Warehouse is None (taking first available row)
        center_lat, center_lon = float(df.iloc[0][lat_col]), float(df.iloc[0][lon_col])

    for d_name in active_dbrs:
        r = df[df[name_col] == d_name].iloc[0]
        selected_coords.append((float(r[lat_col]), float(r[lon_col])))
        selected_names.append(r[name_col])

    min_road_distance = 0
    best_road_geometry = None
    best_route_indices = [0] + list(range(1, len(selected_names))) + [0] if st.session_state.sel_wh != "None" else list(range(len(selected_names)))

    # Route generation logic runs only if Warehouse is selected AND DBRs exist
    if st.session_state.sel_wh != "None" and len(active_dbrs) > 0:
        min_road_distance = float('inf')
        dbr_num = len(active_dbrs)
        for perm in itertools.permutations(range(1, dbr_num + 1)):
            current_perm_indices = [0] + list(perm) + [0]
            current_perm_coords = [selected_coords[idx] for idx in current_perm_indices]
            dist, geom = get_road_route_and_distance(current_perm_coords)

            if dist is not None and dist < min_road_distance:
                min_road_distance = dist
                best_route_indices = current_perm_indices
                best_road_geometry = geom

        if not best_road_geometry:
            min_road_distance = 0
            best_road_geometry = [selected_coords[idx] for idx in best_route_indices]

    route_names = [selected_names[idx] for idx in best_route_indices] if (st.session_state.sel_wh != "None" or len(active_dbrs) > 0) else []

    stop_order_dict = {}
    for step, name in enumerate(route_names):
        if step != 0 and step != len(route_names) - 1:
            if name not in stop_order_dict:
                stop_order_dict[name] = len(stop_order_dict) + 1

    # --- UI GRID WORKSPACE ---
    c1, c2 = st.columns([1, 3])

    with c1:
        if st.session_state.sel_wh == "None":
            st.info("💡 Please select a Warehouse/Plant to start routing configuration.")
        elif len(active_dbrs) > 0:
            st.metric(label="Total Distance", value=f"{round(min_road_distance, 2)} KM")

            st.write("**Route Sequence:**")
            for step, name in enumerate(route_names):
                if step == 0:
                    st.write(f"🏭 **{step+1}. {name} (Start)**")
                elif step == len(route_names) - 1:
                    st.write(f"🏢 **{step+1}. {name} (Return)**")
                else:
                    st.write(f"📍 **{step+1}. {name}**")


    with c2:
        m = folium.Map(location=(center_lat, center_lon), zoom_start=10)

        for _, row in df.iterrows():
            loc_name = row[name_col]
            is_wh = row[type_col] in ['Warehouse', 'Plant']

            # 🌟 MAP KE SABHI WAREHOUSES PAR BADA KHOOBSURAT ICON LAGEGA
            if is_wh:
                # Custom big vector icon logic applied universally to all warehouses
                icon_html = """
                <div style="
                    background-color: #e74c3c;
                    border: 3px solid #ffffff;
                    border-radius: 50%;
                    width: 50px;
                    height: 50px;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    box-shadow: 0px 4px 10px rgba(0,0,0,0.4);
                ">
                    <span style="font-size: 24px; color: white;">🏭</span>
                </div>
                """
                marker_icon = folium.DivIcon(html=icon_html, icon_size=(50, 50), icon_anchor=(25, 25))

                # Agar ye specific warehouse current select hua hai toh extra highlighting text label dikhao
                if loc_name == st.session_state.sel_wh:
                    label_html = f"<div style='font-size: 14px; font-weight: bold; color: white; background: #e74c3c; padding: 4px 8px; border-radius: 4px; border: 2px solid white; white-space: nowrap; box-shadow: 2px 2px 5px rgba(0,0,0,0.2);'>🏭 START: {loc_name}</div>"
                    popup_html = f"<b>{loc_name}</b><br><span style='color:red; font-weight:bold;'>Selected Active Origin</span>"
                    folium.map.Marker((row[lat_col], row[lon_col]), icon=folium.features.DivIcon(html=label_html, icon_size=(0,0), icon_anchor=(-20,35))).add_to(m)
                else:
                    label_html = f"<div style='font-size: 11px; font-weight: bold; color: #333; background: #ffffff; padding: 3px 5px; border: 1px solid #999; border-radius: 3px; white-space: nowrap;'>{loc_name}</div>"
                    popup_html = f"<b>{loc_name}</b><br><span style='color:gray;'>Type: {row[type_col]}</span>"
                    folium.map.Marker((row[lat_col], row[lon_col]), icon=folium.features.DivIcon(html=label_html, icon_size=(0,0), icon_anchor=(-10,15))).add_to(m)

                folium.Marker(location=(row[lat_col], row[lon_col]), icon=marker_icon, popup=folium.Popup(popup_html, max_width=250)).add_to(m)

            else:
                # DBR Points Rendering Logic
                if loc_name in active_dbrs:
                    color, icon = 'blue', 'shopping-cart'
                    stop_num = stop_order_dict.get(loc_name, 1)
                    label_html = f"<div style='font-size: 14px; font-weight: bold; color: #d93838; background: #fff0f0; padding: 4px 8px; border-radius: 4px; border: 2.5px solid #d93838; box-shadow: 2px 2px 5px rgba(0,0,0,0.3); white-space: nowrap;'>🚨 [{stop_num}] {loc_name}</div>"
                    popup_html = f"<b>{loc_name}</b><br><span style='color:green;'>Active Destination Point</span>"
                else:
                    color, icon = 'gray', 'info-sign'
                    label_html = f"<div style='font-size: 11px; font-weight: bold; color: #333; background: #ffffff; padding: 3px 5px; border: 1px solid #999; border-radius: 3px; white-space: nowrap;'>{loc_name}</div>"
                    popup_html = f"<b>{loc_name}</b><br><span style='color:gray;'>Type: {row[type_col]}</span>"

                folium.Marker(location=(row[lat_col], row[lon_col]), icon=folium.Icon(color=color, icon=icon), popup=folium.Popup(popup_html, max_width=250)).add_to(m)
                folium.map.Marker((row[lat_col], row[lon_col]), icon=folium.features.DivIcon(html=label_html, icon_size=(0,0), icon_anchor=(-10,15))).add_to(m)

        # --- PATH ARROWS WITH SPACED OUT ORIENTATION ---
        if st.session_state.sel_wh != "None" and best_road_geometry:
            line = folium.PolyLine(best_road_geometry, color="#1b4fd2", weight=6, opacity=0.85).add_to(m)

            PolyLineTextPath(
                line,
                '                ►                ',
                repeat=True,
                offset=8,
                attributes={'fill': '#ffffff', 'font-weight': 'bold', 'font-size': '15px'}
            ).add_to(m)

        st_folium(m, width=980, height=600, key="road_geometry_fixed_map", returned_objects=[])
