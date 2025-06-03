import shiny
from shiny import App, render, ui, reactive
import pandas as pd
import geopandas as gpd
import openrouteservice as ors
import folium
import os
import tempfile
#from pprint import pprint
from caculate_optimal_route import optimal_route

# Set OpenRouteService API key
os.environ['OPENROUTESERVICE_KEY'] = "5b3ce3597851110001cf6248c25b517341e94033b8c21d63ecda95b6"
ors_client = ors.Client(key=os.environ['OPENROUTESERVICE_KEY'])

# Load auxiliary layers (assuming you have these files)
lakes = gpd.read_file(os.path.join(os.path.dirname(__file__), '..', 'data_wgs84', 'lakes.gpkg'))
np = gpd.read_file(os.path.join(os.path.dirname(__file__), '..', 'data_wgs84', 'national_parks.gpkg'))
country = gpd.read_file(os.path.join(os.path.dirname(__file__), '..', 'data_wgs84', 'country.gpkg'))

# Get the centroid of the country
centroid = country.geometry.centroid.iloc[0]
center_coords = [centroid.y, centroid.x]

# UI
app_ui = ui.page_fluid(
        ui.tags.style(
        """
        /* apply css to control height of file input widgets */
        .shiny-input-container {
            height: 60px;
            padding: 10px 10px 60px 10px; /* follows the TRouBLe rule */
        }
        /* apply CSS to the labels of input elements */
        .shiny-input-container > label {
            font-size: 14px;
        }
        """
    ),
    #ui.panel_title("Tubura Route Optimization Tool, Prototype (June, 2024)"),
    ui.layout_columns(
        ui.row(
            ui.column(3,  
                ui.card(
                    #ui.card_header("Input Controls"),
                    ui.p(
                        ui.input_file(id="source", label="Starting point coordinates:"),
                        ui.input_file(id="final_stop", label="Final point coordinates:"),
                        ui.input_file(id="destinations", label="Destinations coordinates:"),
                        ui.input_select(id="dest_id_field", label="Destination ID field", choices=["Destination Id field"]),
                        ui.input_action_button("processButton", label="Process Files", class_="btn-primary")
                    )
                ),
                ui.card(
                    ui.p(
                    ui.download_button(id="downloadRoute", label="Get route file", class_="btn-sm btn-secondary w-100 mb-2"),
                    ui.download_button(id="downloadRouteSegments", label="Get road segments distances", class_="btn-sm btn-secondary w-100 mb-2"),
                    ui.download_button(id="downloadRoadSurfaceStats", label="Get road surface statistics", class_="btn-sm btn-secondary w-100 mb-2"),
                    )
                )
            ),
            ui.column(9, 
                ui.card(
                    #ui.card_header("Map"),
                    ui.p(
                        ui.output_ui("map")
                    ),
                    ui.card_footer("The optimal route is shown in Maroon. Hover and click on destinations to see their names.")
                )
            )
        )
    )
)

def server(input, output, session):
    uploaded_files = reactive.Value({
        'source': None,
        'final_stop': None,
        'destinations': None
    })
    dest_id_field = reactive.Value(None)
    dest_id_choices = reactive.Value([])
    result = reactive.Value(None)

    @reactive.Effect
    @reactive.event(input.source)
    def _():
        file = input.source()
        if file and len(file) > 0:
            df = pd.read_csv(file[0]['datapath'])
            uploaded_files.set({**uploaded_files(), 'source': df})

    @reactive.Effect
    @reactive.event(input.final_stop)
    def _():
        file = input.final_stop()
        if file and len(file) > 0:
            df = pd.read_csv(file[0]['datapath'])
            uploaded_files.set({**uploaded_files(), 'final_stop': df})

    @reactive.Effect
    @reactive.event(input.destinations)
    def _():
        file = input.destinations()
        if file and len(file) > 0:
            df = pd.read_csv(file[0]['datapath'])
            df.columns = df.columns.str.lower().str.replace(' ', '_')
            if 'name' in df.columns:
                df = df.rename(columns={'name': 'name_dest'})
            uploaded_files.set({**uploaded_files(), 'destinations': df})
            dest_id_choices.set(df.columns.tolist())

    @reactive.Effect
    @reactive.event(input.dest_id_field)
    def _():
        dest_id_field.set(input.dest_id_field())

    @reactive.Effect
    def _():
        choices = dest_id_choices()
        if choices:
            shiny.ui.update_select(
                session=session,
                id="dest_id_field",
                choices=choices,
                selected=None
            )

    @reactive.Calc
    @reactive.event(input.processButton)
    def calculate_optimal_route():
        files = uploaded_files()
        id_field = dest_id_field()
        if all(df is not None and not df.empty for df in files.values()) and id_field:
            try:
                route_detailed, source, final_stop, destinations, route_segments, used_id_field = optimal_route(
                    files['source'],
                    files['destinations'],
                    id_field,
                    files['final_stop']
                )
                return {
                    'route_detailed': route_detailed,
                    'source': source,
                    'final_stop': final_stop,
                    'destinations': destinations,
                    'route_segments': route_segments,
                    'used_id_field': used_id_field
                }
            except Exception as e:
                ui.notification_show(f"Error: {str(e)}", type="error")
                return None
        else:
            return None

    @reactive.Effect
    @reactive.event(input.processButton)
    def _():
        files = uploaded_files()
        id_field = dest_id_field()
        all_files_provided = all(
            df is not None and not df.empty for df in files.values()
        )
        if all_files_provided and id_field:
            ui.notification_show("Processing data...", type="message", close_button=False)
            calculated_result = calculate_optimal_route()
            result.set(calculated_result)  
            if calculated_result is not None:
                ui.notification_show("Route optimization completed successfully!", type="message")
            else:
                ui.notification_show("An error occurred during route optimization.", type="error")
        else:
            ui.notification_show("Please upload all files and select an ID field before processing.", type="warning")
    
    @output
    @render.ui
    def map():
        try:        
            # Create a map centered on the country and add country boundary
            m = folium.Map(location=center_coords, zoom_start=8.5)
            folium.GeoJson(country).add_to(m)    

            # Style function for the country
            def style_country(feature):
                return {
                    'fillColor': '#acbbb4', 
                    'color': '#3f4b46',   
                    'weight': 4,     
                    'fillOpacity': 0.2
                }
        
            # Style function for lakes
            def style_lakes(feature):
                return {
                    'fillColor': '#37a3bd', 
                    'color': '#345a6a',   
                    'weight': 1,     
                    'fillOpacity': 0.6
                }

            # Style function for national parks
            def style_np(feature):
                return {
                    'fillColor': '#13764b',  
                    'color': '#006600',   
                    'weight': 2,     
                    'fillOpacity': 0.6
                }

            # Add lakes layer with style
            folium.GeoJson(country, style_function=style_country).add_to(m)  
            folium.GeoJson(np, style_function=style_np).add_to(m)
            folium.GeoJson(lakes, style_function=style_lakes).add_to(m)  
            

            # Get the result value
            result_value = result()
            if result_value is not None:
                # Add the route to the map
                route_detailed = result_value['route_detailed']    
                coordinates = []
                for _, row in route_detailed.iterrows():
                    if row.geometry.geom_type == 'LineString':
                        coordinates.extend([(coord[1], coord[0]) for coord in row.geometry.coords])
                    elif row.geometry.geom_type == 'MultiLineString':
                        for line in row.geometry:
                            coordinates.extend([(coord[1], coord[0]) for coord in line.coords])
                
                # Add the route as a PolyLine
                folium.PolyLine(coordinates, color='#800000', weight=4, opacity=0.8).add_to(m)
 

                # add the origin point
                folium.Marker(
                    location=[float(result_value['source'].geometry.y.iloc[0]), float(result_value['source'].geometry.x.iloc[0])],
                    popup='Starting point',
                    icon=folium.Icon(color='lightgreen')
                ).add_to(m)  

                # add the final point
                folium.Marker(
                    location=[float(result_value['final_stop'].geometry.y.iloc[0]), float(result_value['final_stop'].geometry.x.iloc[0])],
                    popup='Final Stop',
                    icon=folium.Icon(color='darkpurple')
                ).add_to(m)  

                # add the destinations
                dest_id = result_value['used_id_field']
                for _, row in result_value['destinations'].iterrows():
                    folium.Marker(
                        location=[float(row.geometry.y), float(row.geometry.x)],
                        popup=row[dest_id],
                        tooltip=row['name']
                    ).add_to(m)

            return ui.HTML(m._repr_html_())
        except Exception as e:
            print(f"Error in map function: {str(e)}")
            return "An error occurred while creating the map."
        
    @render.download(filename="my_route.gpkg")
    def downloadRoute():
        result_value = result()
        route = result_value['route_detailed']
        
        # Create a temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.gpkg') as tmp:
            tmp_path = tmp.name
        
        try:
            # Write to the temporary file
            route.to_file(tmp_path, layer="optimal_route", driver="GPKG")
            
            # Read the file back into memory
            with open(tmp_path, 'rb') as f:
                content = f.read()
            
            yield content

        finally:
            # Clean up the temporary file
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    @render.download(filename="route_segments.csv")
    def downloadRouteSegments():
        result_value = result()
        route_segments = result_value['route_segments']
        yield  route_segments.to_csv(index=False)

    @render.download(filename="road_surface_stats.csv")
    def downloadRoadSurfaceStats():
        result_value = result()
        route_detailed = result_value['route_detailed']
        
        # Convert GeoDataFrame to regular DataFrame
        df = pd.DataFrame(route_detailed.drop(columns='geometry'))
        
        # Summarize the length of each surface category
        surface_stats = df.groupby('surface')['segment_length'].sum().reset_index()
        surface_stats = surface_stats.rename(columns={'segment_length': 'total_length_m'})
        
        # Convert total length from meters to kilometers and calculate percentage
        surface_stats['total_length_km'] = surface_stats['total_length_m'] / 1000
        total_length = surface_stats['total_length_km'].sum()
        surface_stats['percentage'] = (surface_stats['total_length_km'] / total_length) * 100
        surface_stats['total_length_km'] = surface_stats['total_length_km'].round(2)
        surface_stats['percentage'] = surface_stats['percentage'].round(2)
        surface_stats = surface_stats.sort_values('total_length_km', ascending=False)
        surface_stats = surface_stats.drop(columns=['total_length_m', 'total_length_km'])
        
        yield  surface_stats.to_csv(index=False)

app = App(app_ui, server)

