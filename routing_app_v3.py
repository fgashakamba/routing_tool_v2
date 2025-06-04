import shiny
from shiny import App, render, ui, reactive
import pandas as pd
import geopandas as gpd
import openrouteservice as ors
import folium
from folium import MacroElement
from jinja2 import Template
import os
import time
import tempfile
from caculate_optimal_route import optimal_route
from dotenv import load_dotenv

# Set OpenRouteService API key
load_dotenv()
ors_api_key = os.environ.get('OPENROUTESERVICE_KEY')
if not ors_api_key:
    raise ValueError("OPENROUTESERVICE_KEY environment variable not set.")
ors_client = ors.Client(key=ors_api_key)

# Load auxiliary layers
lakes = gpd.read_file(os.path.join(os.path.dirname(__file__),  'data_wgs84', 'RW_lakes.gpkg'))
np = gpd.read_file(os.path.join(os.path.dirname(__file__), 'data_wgs84', 'RW_national_parks.gpkg'))
country = gpd.read_file(os.path.join(os.path.dirname(__file__), 'data_wgs84', 'RW_country.gpkg'))

# Load pre-defined destinations database
# The data is provided as a CSV and has these columns: name, latitude, longitude, category (optional)
try:
    destinations_db = pd.read_csv(os.path.join(os.path.dirname(__file__), 'data', 'destinations_database.csv'))
    destinations_db.columns = destinations_db.columns.str.lower().str.replace(' ', '_')
    # Ensure required columns exist
    required_cols = ['name', 'latitude', 'longitude']
    if not all(col in destinations_db.columns for col in required_cols):
        raise ValueError(f"Destinations database must contain columns: {required_cols}")
    destinations_available = True

    # Create choices for the select inputs (name as both key and value, index as internal reference)
    destination_choices = {str(idx): row['name'] for idx, row in destinations_db.iterrows()}

    # Group by category if available for better organization
    if 'category' in destinations_db.columns:
        categories = destinations_db['category'].unique()
        category_choices = {cat: cat for cat in sorted(categories)}
    else:
        categories = None
        category_choices = {}

except (FileNotFoundError, Exception) as e:
    print(f"Warning: Could not load destinations database: {e}")
    destinations_available = False
    destination_choices = {}
    categories = None
    category_choices = {}

# Get the centroid of the country (use a projected CRS before calculating centroids)
centroid = country.to_crs(country.estimate_utm_crs()).geometry.centroid.to_crs('EPSG:4326').iloc[0]
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
            font-size: 16px;
        }
        /* Style for destination selection area */
        .destination-selector {
            max-height: 300px;
            overflow-y: auto;
            border: 1px solid #ddd;
            padding: 10px;
            margin: 5px 0;
        }
        .selected-destinations {
            background-color: #f8f9fa;
            padding: 10px;
            margin: 5px 0;
            border-radius: 5px;
        }
        /* Style for button pills */
        .button-pills {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-bottom: 15px;
            padding: 10px;
            background-color: #f8f9fa;
            border-radius: 8px;
        }
        """
    ),
    ui.panel_title("Tubura Route Optimization Tool, Enhanced Prototype (May, 2025)"),
    ui.layout_columns(
        ui.row(
            ui.column(3,
                # Input method selection card
                ui.card(
                    ui.card_header("Choose Input Method"),
                    ui.p(
                        ui.input_radio_buttons(
                            id="input_method",
                            label="",
                            choices={
                                "select": "Select from database" if destinations_available else "Select from database (Not available)",
                                "upload": "Upload CSV files",
                                "map_click": "Click on map"
                            },
                            selected="select" if destinations_available else "upload"
                        )
                    )
                ),

                # Route configuration card
                ui.card(
                    ui.card_header("Route Configuration"),
                    ui.p(
                        # Conditional UI based on input method
                        ui.output_ui("input_controls")
                    )
                ),

                # Destination selection panel (only shown when using database)
                ui.output_ui("destination_selection_panel"),

                # Calculate button card
                ui.card(
                    #ui.card_header("Calculate Route"),
                    ui.p(
                        ui.input_action_button("processButton", label="Calculate Optimal Route", class_="btn-primary w-100 mb-2")
                    )
                )
            ),
            ui.column(9,
                # Button pills at the top of the map
                ui.div(
                    ui.download_button(id="downloadRoute", label="Get route file", class_="btn-sm btn-secondary"),
                    ui.download_button(id="downloadRouteSegments", label="Get road segments distances", class_="btn-sm btn-secondary"),
                    ui.input_action_button("Show_Segments_Table", label="Show route segments", class_="btn-sm btn-info"),
                    ui.input_action_button("Show_Table", label="Show surface statistics", class_="btn-sm btn-info"),
                    class_="button-pills"
                ),
                ui.card(
                    #ui.card_header("Route Map"),
                    ui.p(
                        ui.output_ui("map")
                    ),
                    ui.card_footer("The optimal route is shown in Maroon. Hover to see the destination sequence in the itinerary and click to see the destination name.")
                )
            )
        )
    )
)

def server(input, output, session):
    # Reactive values for uploaded files
    uploaded_files = reactive.Value({
        'source': None,
        'final_stop': None,
        'destinations': None
    })

    # Reactive values for database selection
    selected_destinations = reactive.Value([])  # This will store all selected destination indices persistently

    dest_id_field = reactive.Value(None)
    dest_id_choices = reactive.Value([])
    result = reactive.Value(None)

    # Map click functionality
    map_click_mode = reactive.Value("none")
    map_clicked_points = reactive.Value({'source': None, 'final_stop': None, 'destinations': []})

    # Render conditional input controls
    @render.ui
    def input_controls():
        method = input.input_method()

        if method == "upload":
            return ui.div(
                ui.input_file(id="source", label="Starting point coordinates:"),
                ui.input_file(id="final_stop", label="Final point coordinates:"),
                ui.input_file(id="destinations", label="Destinations coordinates:"),
                ui.input_select(id="dest_id_field", label="Destination ID field", choices=["Select field..."]),
            )
        elif method == "map_click":
            return ui.div(
                ui.p("Click on the map to set points:", style="font-weight: bold; color: #2c5282;"),
                ui.div(
                    ui.input_action_button("set_source_mode", "Set Starting Point", class_="btn-sm btn-success me-2"),
                    ui.input_action_button("set_final_mode", "Set Final Stop", class_="btn-sm btn-warning me-2"),
                    ui.input_action_button("set_dest_mode", "Add Destinations", class_="btn-sm btn-info me-2"),
                    ui.input_action_button("clear_map_points", "Clear All Points", class_="btn-sm btn-outline-danger"),
                    style="margin-bottom: 10px;"
                ),
                ui.output_ui("click_mode_status"),
                ui.output_ui("map_points_summary")
            )
        else:  # select method
            if not destinations_available:
                return ui.div(
                    ui.p("Destinations database is not available. Please use the upload method.",
                         style="color: red; font-weight: bold;")
                )

            return ui.div(
                ui.input_select(
                    id="source_select",
                    label="Starting point:",
                    choices={"": "Select starting point", **destination_choices}
                ),
                ui.input_select(
                    id="final_stop_select",
                    label="Final stop:",
                    choices={"": "Select final stop", **destination_choices}
                ),
                # Category filter if available
                ui.input_select(
                    id="category_filter",
                    label="Filter destinations by category:",
                    choices={"": "All categories", **category_choices}
                ) if categories is not None else ui.div(),
            )

    # Render destination selection panel
    @render.ui
    def destination_selection_panel():
        method = input.input_method()

        if method == "select" and destinations_available:
            return ui.card(
                ui.card_header("Select Destinations"),
                ui.p(
                    ui.input_text(
                        id="destination_search",
                        label="Search destinations:",
                        placeholder="Type to search..."
                    ),
                    ui.output_ui("destination_checkboxes"),
                    ui.br(),
                    ui.div(
                        ui.input_action_button("clear_selections", "Clear All Selections", class_="btn-sm btn-outline-secondary"),
                        style="margin-bottom: 10px;"
                    ),
                    ui.output_ui("selected_destinations_display")
                )
            )
        else:
            return ui.div()

    # Track selections across category changes
    @reactive.Effect
    @reactive.event(input.selected_dest_checkboxes)
    def _():
        current_selections = input.selected_dest_checkboxes() or []
        if current_selections is not None:  # Changed condition to handle empty lists properly
            # Get current persistent selections
            persistent_selections = selected_destinations() or []

            # Get current filtered view destinations to handle deselections
            search_term = input.destination_search() or ""
            category_filter = input.category_filter() if hasattr(input, 'category_filter') else ""

            # Filter destinations based on search and category (same logic as in render function)
            filtered_db = destinations_db.copy()

            if category_filter:
                filtered_db = filtered_db[filtered_db['category'] == category_filter]

            if search_term:
                mask = filtered_db['name'].str.contains(search_term, case=False, na=False)
                if 'description' in filtered_db.columns:
                    mask |= filtered_db['description'].str.contains(search_term, case=False, na=False)
                filtered_db = filtered_db[mask]

            current_view_indices = [str(idx) for idx, row in filtered_db.iterrows()]

            # Remove any previous selections that are in current view (to handle deselections)
            updated_selections = [sel for sel in persistent_selections if sel not in current_view_indices]

            # Add new selections from current view
            updated_selections.extend(current_selections)

            # Remove duplicates while preserving order
            updated_selections = list(dict.fromkeys(updated_selections))

            selected_destinations.set(updated_selections)

    # Clear all selections
    @reactive.Effect
    @reactive.event(input.clear_selections)
    def _():
        selected_destinations.set([])
        # Also clear the current checkbox selections
        shiny.ui.update_checkbox_group(
            session=session,
            id="selected_dest_checkboxes",
            selected=[]
        )

    # Render destination checkboxes with search functionality
    @render.ui
    def destination_checkboxes():
        if not destinations_available:
            return ui.div()

        search_term = input.destination_search() or ""
        category_filter = input.category_filter() if hasattr(input, 'category_filter') else ""

        # Filter destinations based on search and category
        filtered_db = destinations_db.copy()

        if category_filter:
            filtered_db = filtered_db[filtered_db['category'] == category_filter]

        if search_term:
            mask = filtered_db['name'].str.contains(search_term, case=False, na=False)
            if 'description' in filtered_db.columns:
                mask |= filtered_db['description'].str.contains(search_term, case=False, na=False)
            filtered_db = filtered_db[mask]

        # Create checkbox choices
        choices = {}
        for idx, row in filtered_db.iterrows():
            display_name = row['name']
            if 'description' in row and pd.notna(row['description']):
                display_name += f" - {row['description']}"
            choices[str(idx)] = display_name

        # Get persistent selections that are in the current filtered view
        persistent_selections = selected_destinations() or []
        current_view_selections = [s for s in persistent_selections if s in choices.keys()]

        # Show count of selections in current view vs total
        total_selections = len(persistent_selections)
        view_selections = len(current_view_selections)

        info_text = ""
        if total_selections > 0:
            if view_selections < total_selections:
                info_text = f"Showing {view_selections} of {total_selections} selected destinations in current view"
            else:
                info_text = f"All {total_selections} selected destinations are shown"

        return ui.div(
            ui.p(info_text, style="font-size: 12px; color: #666; margin-bottom: 5px;") if info_text else ui.div(),
            ui.input_checkbox_group(
                id="selected_dest_checkboxes",
                label="Select destinations to visit:",
                choices=choices,
                selected=current_view_selections  # Pre-select based on persistent selections
            ),
            class_="destination-selector"
        )

    # Display selected destinations
    @render.ui
    def selected_destinations_display():
        persistent_selections = selected_destinations() or []
        if not persistent_selections or not destinations_available:
            return ui.div()

        selected_names = []
        categories_count = {}

        for idx_str in persistent_selections:
            try:
                idx = int(idx_str)
                if idx < len(destinations_db):
                    row = destinations_db.iloc[idx]
                    name = row['name']
                    category = row.get('category', 'Unknown') if 'category' in destinations_db.columns else 'Unknown'

                    # Count by category
                    categories_count[category] = categories_count.get(category, 0) + 1

                    if 'category' in row and pd.notna(row['category']):
                        name += f" ({row['category']})"
                    selected_names.append(name)
            except (ValueError, IndexError):
                continue

        if selected_names:
            # Create category summary
            category_summary = ", ".join([f"{cat}: {count}" for cat, count in sorted(categories_count.items())])

            return ui.div(
                ui.strong(f"Selected destinations ({len(selected_names)}):"),
                ui.p(f"By category: {category_summary}", style="font-size: 12px; color: #666; margin: 2px 0;"),
                ui.br(),
                ui.HTML("<br>".join(selected_names)),
                class_="selected-destinations"
            )
        return ui.div()

    # Map click mode handlers
    @reactive.Effect
    @reactive.event(input.set_source_mode)
    def _():
        map_click_mode.set("source")
        ui.notification_show("Click on the map to set the starting point", type="message")

    @reactive.Effect
    @reactive.event(input.set_final_mode)
    def _():
        map_click_mode.set("final")
        ui.notification_show("Click on the map to set the final stop", type="message")

    @reactive.Effect
    @reactive.event(input.set_dest_mode)
    def _():
        map_click_mode.set("destination")
        ui.notification_show("Click on the map to add destinations", type="message")

    @reactive.Effect
    @reactive.event(input.clear_map_points)
    def _():
        map_clicked_points.set({'source': None, 'final_stop': None, 'destinations': []})
        map_click_mode.set("none")
        ui.notification_show("All map points cleared", type="message")

    # Handle map clicks
    @reactive.Effect
    @reactive.event(input.map_clicked_coords)
    def _():
        coords = input.map_clicked_coords()
        mode = map_click_mode()
        if not coords or mode == "none":
            return

        lat, lng = coords['lat'], coords['lng']

        if mode == "source":
            ui.modal_show(ui.modal(
                ui.h4("Name the Starting Point"),
                ui.input_text("point_name_input", "Enter name:", placeholder="e.g., My Office"),
                ui.p(f"Coordinates: {lat:.6f}, {lng:.6f}", style="color: #666; font-size: 12px;"),
                footer=[ui.input_action_button("save_source_point", "Save", class_="btn-success"), ui.modal_button("Cancel")],
                easy_close=True
            ))
        elif mode == "final":
            ui.modal_show(ui.modal(
                ui.h4("Name the Final Stop"),
                ui.input_text("point_name_input", "Enter name:", placeholder="e.g., Airport"),
                ui.p(f"Coordinates: {lat:.6f}, {lng:.6f}", style="color: #666; font-size: 12px;"),
                footer=[ui.input_action_button("save_final_point", "Save", class_="btn-warning"), ui.modal_button("Cancel")],
                easy_close=True
            ))
        elif mode == "destination":
            ui.modal_show(ui.modal(
                ui.h4("Name the Destination"),
                ui.input_text("point_name_input", "Enter name:", placeholder="e.g., Tourist Site"),
                ui.p(f"Coordinates: {lat:.6f}, {lng:.6f}", style="color: #666; font-size: 12px;"),
                footer=[ui.input_action_button("save_dest_point", "Add", class_="btn-info"), ui.modal_button("Cancel")],
                easy_close=True
            ))

    # Save clicked points
    @reactive.Effect
    @reactive.event(input.save_source_point)
    def _():
        coords = input.map_clicked_coords()
        name = input.point_name_input() or "Starting Point"
        if coords:
            current_points = map_clicked_points()
            current_points['source'] = {'name': name, 'latitude': coords['lat'], 'longitude': coords['lng']}
            map_clicked_points.set(current_points)
            map_click_mode.set("none")
            ui.modal_remove()
            ui.notification_show(f"Starting point '{name}' saved!", type="success")
            rerender_trigger.set(rerender_trigger() + 1) # Trigger map update

    @reactive.Effect
    @reactive.event(input.save_final_point)
    def _():
        coords = input.map_clicked_coords()
        name = input.point_name_input() or "Final Stop"
        if coords:
            current_points = map_clicked_points()
            current_points['final_stop'] = {'name': name, 'latitude': coords['lat'], 'longitude': coords['lng']}
            map_clicked_points.set(current_points)
            map_click_mode.set("none")
            ui.modal_remove()
            ui.notification_show(f"Final stop '{name}' saved!", type="success")
            rerender_trigger.set(rerender_trigger() + 1) # Trigger map update

    @reactive.Effect
    @reactive.event(input.save_dest_point)
    def _():
        coords = input.map_clicked_coords()
        name = input.point_name_input() or f"Destination {len(map_clicked_points()['destinations']) + 1}"
        if coords:
            current_points = map_clicked_points()
            current_points['destinations'].append({'name': name, 'latitude': coords['lat'], 'longitude': coords['lng']})
            map_clicked_points.set(current_points)
            ui.modal_remove()
            ui.notification_show(f"Destination '{name}' added!", type="success")
            rerender_trigger.set(rerender_trigger() + 1) # Trigger map update

    # Rerender map display every time a new point is clicked
    rerender_trigger = reactive.Value(0)

    # Add reactive effects to handle category and search changes
    @reactive.Effect
    @reactive.event(input.category_filter)
    def _():
        # When category filter changes, we don't need to do anything special
        # The render function will automatically show the right selections
        # This effect is here to ensure reactivity works properly
        pass

    # Add a reactive effect to handle search changes
    @reactive.Effect
    @reactive.event(input.destination_search)
    def _():
        # When search term changes, we don't need to do anything special
        # The render function will automatically show the right selections
        # This effect is here to ensure reactivity works properly
        pass

    # File upload handlers (existing code)
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
        if choices and input.input_method() == "upload":
            shiny.ui.update_select(
                session=session,
                id="dest_id_field",
                choices=choices,
                selected=None
            )

    # Helper function to prepare data from database selections
    def prepare_database_data():
        if not destinations_available:
            return None, None, None, None

        # Get selected indices (they come as strings)
        source_idx = input.source_select()
        final_stop_idx = input.final_stop_select()
        dest_indices = selected_destinations() or []  # Use persistent selections instead of current checkboxes

        # Check if selections are made (empty string means no selection)
        if not source_idx or not final_stop_idx or not dest_indices:
            return None, None, None, None

        try:
            # Prepare source dataframe
            source_row = destinations_db.iloc[int(source_idx)]
            source_df = pd.DataFrame({
                'latitude': [source_row['latitude']],
                'longitude': [source_row['longitude']],
                'name': [source_row['name']]
            })

            # Prepare final stop dataframe
            final_row = destinations_db.iloc[int(final_stop_idx)]
            final_df = pd.DataFrame({
                'latitude': [final_row['latitude']],
                'longitude': [final_row['longitude']],
                'name': [final_row['name']]
            })

            # Prepare destinations dataframe
            dest_rows = []
            for idx_str in dest_indices:
                idx = int(idx_str)
                row = destinations_db.iloc[idx]
                dest_rows.append({
                    'latitude': row['latitude'],
                    'longitude': row['longitude'],
                    'name': row['name'],
                    'destination_id': row['name']
                })

            dest_df = pd.DataFrame(dest_rows)

            return source_df, final_df, dest_df, 'destination_id'

        except Exception as e:
            print(f"Error preparing database data: {e}")
            return None, None, None, None

    #  helper function to prepare map click data
    def prepare_map_click_data():
        points = map_clicked_points()
        if not points['source'] or not points['final_stop'] or not points['destinations']:
            return None, None, None, None

        try:
            source_df = pd.DataFrame({
                'latitude': [points['source']['latitude']],
                'longitude': [points['source']['longitude']],
                'name': [points['source']['name']]
            })

            final_df = pd.DataFrame({
                'latitude': [points['final_stop']['latitude']],
                'longitude': [points['final_stop']['longitude']],
                'name': [points['final_stop']['name']]
            })

            dest_rows = []
            for dest in points['destinations']:
                dest_rows.append({
                    'latitude': dest['latitude'], 'longitude': dest['longitude'],
                    'name': dest['name'], 'destination_id': dest['name']
                })
            dest_df = pd.DataFrame(dest_rows)

            return source_df, final_df, dest_df, 'destination_id'
        except Exception as e:
            print(f"Error preparing map click data: {e}")
            return None, None, None, None

    @reactive.Calc
    @reactive.event(input.processButton)
    def calculate_optimal_route():
        method = input.input_method()

        if method == "upload":
            # Use existing upload logic
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
        elif method == "map_click":
            source_df, final_df, dest_df, id_field = prepare_map_click_data()
            if source_df is not None and final_df is not None and dest_df is not None:
                try:
                    route_detailed, source, final_stop, destinations, route_segments, used_id_field = optimal_route(
                        source_df, dest_df, id_field, final_df
                    )
                    return {
                        'route_detailed': route_detailed, 'source': source, 'final_stop': final_stop,
                        'destinations': destinations, 'route_segments': route_segments, 'used_id_field': used_id_field
                    }
                except Exception as e:
                    ui.notification_show(f"Error: {str(e)}", type="error")
                    return None
        else:
            # Use database selection
            source_df, final_df, dest_df, id_field = prepare_database_data()
            if source_df is not None and final_df is not None and dest_df is not None:
                try:
                    route_detailed, source, final_stop, destinations, route_segments, used_id_field = optimal_route(
                        source_df,
                        dest_df,
                        id_field,
                        final_df
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

        return None

    @reactive.Effect
    @reactive.event(input.processButton)
    def _():
        method = input.input_method()

        # Validation based on input method
        if method == "upload":
            files = uploaded_files()
            id_field = dest_id_field()
            all_files_provided = all(df is not None and not df.empty for df in files.values())
            if not (all_files_provided and id_field):
                ui.notification_show("Please upload all files and select an ID field before processing.", type="warning")
                return
        elif method == "map_click":
            source_df, final_df, dest_df, id_field = prepare_map_click_data()
            if source_df is None or final_df is None or dest_df is None:
                ui.notification_show("Please set starting point, final stop, and at least one destination by clicking on the map.", type="warning")
                return
        else:
            source_df, final_df, dest_df, id_field = prepare_database_data()

            # Debug information
            source_selection = input.source_select()
            final_selection = input.final_stop_select()
            dest_selections = selected_destinations() or []  # Use persistent selections for debugging

            print(f"Debug - Source selection: '{source_selection}'")
            print(f"Debug - Final selection: '{final_selection}'")
            print(f"Debug - Destination selections: {dest_selections}")
            print(f"Debug - Prepared data check: source={source_df is not None}, final={final_df is not None}, dest={dest_df is not None}")

            if source_df is None or final_df is None or dest_df is None:
                ui.notification_show("Please select starting point, final stop, and at least one destination.", type="warning")
                return

        ui.notification_show("Processing data...", type="message", close_button=False)
        calculated_result = calculate_optimal_route()
        result.set(calculated_result)

        if calculated_result is not None:
            ui.notification_show("Route optimization completed successfully!", type="message")
        else:
            ui.notification_show("An error occurred during route optimization.", type="error")

    # Map rendering
    @render.ui
    def map():
        _ = rerender_trigger()  # Make the map depend on this trigger to force rerender when new points are clicked
        try:
            # Create a map centered on the country and add country boundary
            m = folium.Map(location=center_coords, zoom_start=8.5)
            folium.GeoJson(country).add_to(m)

            # Style functions (same as before)
            def style_country(feature):
                return {
                    'fillColor': '#acbbb4',
                    'color': '#3f4b46',
                    'weight': 4,
                    'fillOpacity': 0.2
                }

            def style_lakes(feature):
                return {
                    'fillColor': '#37a3bd',
                    'color': '#345a6a',
                    'weight': 1,
                    'fillOpacity': 0.6
                }

            def style_np(feature):
                return {
                    'fillColor': '#13764b',
                    'color': '#006600',
                    'weight': 2,
                    'fillOpacity': 0.6
                }

            # Add layers
            folium.GeoJson(country, style_function=style_country).add_to(m)
            folium.GeoJson(np, style_function=style_np).add_to(m)
            folium.GeoJson(lakes, style_function=style_lakes).add_to(m)

            # Create a feature group for dynamically added markers
            dynamic_markers = folium.FeatureGroup(name="Dynamic Markers").add_to(m)


            # Add map click functionality
            if input.input_method() == "map_click":
                map_name = m.get_name()
                click_macro = Template(f"""
                    {{% macro script(this, kwargs) %}}
                    function getLatLng(e) {{
                        var lat = e.latlng.lat.toFixed(6),
                            lng = e.latlng.lng.toFixed(6);
                        parent.Shiny.setInputValue('map_clicked_coords', {{
                            lat: parseFloat(lat),
                            lng: parseFloat(lng),
                            timestamp: new Date().getTime()
                        }}, {{priority: 'event'}});
                    }}
                    {map_name}.on('click', getLatLng);
                    {{% endmacro %}}
                """)

                el = MacroElement()
                el._template = click_macro
                m.get_root().add_child(el)

                # Add clicked points to map (only if no optimal route is calculated yet)
                # These markers will be cleared and replaced by the route markers after calculation
                if result() is None:
                    points = map_clicked_points()
                    if points['source']:
                        folium.Marker(
                            location=[points['source']['latitude'], points['source']['longitude']],
                            popup=f"Starting Point: {points['source']['name']}",
                            icon=folium.Icon(color='lightgreen', icon='play')
                        ).add_to(dynamic_markers)
                    if points['final_stop']:
                        folium.Marker(
                            location=[points['final_stop']['latitude'], points['final_stop']['longitude']],
                            popup=f"Final Stop: {points['final_stop']['name']}",
                            icon=folium.Icon(color='darkpurple', icon='stop')
                        ).add_to(dynamic_markers)
                    for i, dest in enumerate(points['destinations'], 1):
                        folium.Marker(
                            location=[dest['latitude'], dest['longitude']],
                            popup=f"Destination {i}: {dest['name']}",
                            tooltip=dest['name'],
                            icon=folium.Icon(color='blue', icon='info-sign')
                        ).add_to(dynamic_markers)

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

                # Add the destination points only when result_value is not None
                folium.Marker(
                    location=[float(result_value['source'].geometry.y.iloc[0]), float(result_value['source'].geometry.x.iloc[0])],
                    popup='Starting point',
                    icon=folium.Icon(color='lightgreen')
                ).add_to(m)

                # Add the final point
                folium.Marker(
                    location=[float(result_value['final_stop'].geometry.y.iloc[0]), float(result_value['final_stop'].geometry.x.iloc[0])],
                    popup='Final Stop',
                    icon=folium.Icon(color='darkpurple')
                ).add_to(m)

                # Add the destinations
                dest_id = result_value['used_id_field']
                for _, row in result_value['destinations'].iterrows():
                    popup_text = str(row[dest_id]) if dest_id in row else "Destination"
                    tooltip_text = row.get('name', row.get('name_dest', popup_text))

                    folium.Marker(
                        location=[float(row.geometry.y), float(row.geometry.x)],
                        popup=popup_text,
                        tooltip=tooltip_text
                    ).add_to(m)

            return ui.HTML(m._repr_html_())
        except Exception as e:
            print(f"Error in map function: {str(e)}")
            return "An error occurred while creating the map."

    # Route file download handler
    @render.download(filename="my_route.gpkg")
    def downloadRoute():
        result_value = result()
        if result_value is None:
            return

        route = result_value['route_detailed']

        # Create a temporary file path without creating the file itself
        tmp_dir = tempfile.gettempdir()
        tmp_filename = f"route_{os.getpid()}_{int(time.time())}.gpkg"
        tmp_path = os.path.join(tmp_dir, tmp_filename)

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

    # Route segments file handler
    @render.download(filename="route_segments.csv")
    def downloadRouteSegments():
        result_value = result()
        if result_value is None:
            return
        route_segments = result_value['route_segments']
        yield route_segments.to_csv(index=False)

    # Show route segments in a modal dialog
    @reactive.Effect
    @reactive.event(input.Show_Segments_Table)
    def _():
        result_value = result()
        if result_value is None:
            ui.notification_show("Please calculate a route first before viewing route segments.", type="warning")
            return

        route_segments = result_value['route_segments']

        # Create HTML table
        table_html = "<div style='max-height: 400px; overflow-y: auto;'>"
        table_html += "<table class='table table-striped table-sm'>"
        table_html += "<thead><tr>"

        # Add headers
        for col in route_segments.columns:
            table_html += f"<th>{col}</th>"
        table_html += "</tr></thead><tbody>"

        # Add data rows
        for _, row in route_segments.iterrows():
            table_html += "<tr>"
            for col in route_segments.columns:
                value = row[col]
                # Format numbers if they are float
                if isinstance(value, float):
                    value = f"{value:.2f}"
                table_html += f"<td>{value}</td>"
            table_html += "</tr>"

        table_html += "</tbody></table></div>"

        # Show modal with route segments
        ui.modal_show(
            ui.modal(
                ui.h3("Route Segments"),
                ui.HTML(table_html),
                size="xl",
                easy_close=True,
                footer=ui.modal_button("Close")
            )
        )

    # Show road surface statistics along the route in modal dialog
    @reactive.Effect
    @reactive.event(input.Show_Table)
    def _():
        result_value = result()
        if result_value is None:
            ui.notification_show("Please calculate a route first before viewing surface statistics.", type="warning")
            return

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

        # Create HTML table
        table_html = "<table class='table table-striped'>"
        table_html += "<thead><tr><th>Surface Type</th><th>Length (km)</th><th>Percentage</th></tr></thead><tbody>"

        for _, row in surface_stats.iterrows():
            table_html += f"<tr><td>{row['surface']}</td><td>{row['total_length_km']}</td><td>{row['percentage']}%</td></tr>"

        table_html += f"</tbody></table>"
        table_html += f"<p><strong>Total Route Length: {total_length:.2f} km</strong></p>"

        # Show modal with statistics
        ui.modal_show(
            ui.modal(
                ui.h3("Road Surface Statistics"),
                ui.HTML(table_html),
                #title="Surface Statistics",
                size="l",
                easy_close=True,
                footer=ui.modal_button("Close")
            )
        )

app = App(app_ui, server)