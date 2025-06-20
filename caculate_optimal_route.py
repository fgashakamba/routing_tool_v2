import pandas as pd
import geopandas as gpd
import openrouteservice as ors
from openrouteservice import exceptions
import re
from openrouteservice.optimization import Vehicle, Job
from shapely.geometry import Point, LineString
import os
from dotenv import load_dotenv

# Set OpenRouteService API key
load_dotenv()
ors_api_key = os.environ.get('OPENROUTESERVICE_KEY')
if not ors_api_key:
    raise ValueError("OPENROUTESERVICE_KEY environment variable not set.")
ors_client = ors.Client(key=ors_api_key)

def optimal_route(input_source, input_destinations, input_dest_id_field, input_final_stop):  
    # Clean column names and prepare data
    source = input_source.copy()
    source['name'] = "Starting point"
    final_stop = input_final_stop.copy()
    final_stop['name'] = "Final stop"
    destinations = input_destinations.copy()

    # Harmonize coordinate column names
    for df in [source, final_stop, destinations]:
        df.columns = df.columns.str.lower()
        df.rename(columns={'latitude': 'lat', 'longitude': 'lon', 'y': 'lat', 'x': 'lon'}, inplace=True)

    # Convert to GeoDataFrames
    source = gpd.GeoDataFrame(source, geometry=gpd.points_from_xy(source.lon, source.lat), crs="EPSG:4326")
    final_stop = gpd.GeoDataFrame(final_stop, geometry=gpd.points_from_xy(final_stop.lon, final_stop.lat), crs="EPSG:4326")
    destinations = gpd.GeoDataFrame(destinations, geometry=gpd.points_from_xy(destinations.lon, destinations.lat), crs="EPSG:4326")

    # Create vehicle and jobs objects
    home_base = source.geometry.iloc[0].coords[0]
    final_dest = final_stop.geometry.iloc[0].coords[0]
    stops = destinations.geometry.apply(lambda geom: geom.coords[0]).tolist()

    vehicle = Vehicle(id=1, profile="driving-hgv", start=home_base, end=final_dest)
    jobs = [Job(id=i+1, location=loc, priority=1) for i, loc in enumerate(stops)]

    # Create a lookup list of all points before the API call
    all_points_for_lookup = []
    source_name = input_source.get('name', pd.Series(['Starting point'])).iloc[0]
    all_points_for_lookup.append({'name': source_name, 'lon': source.geometry.x.iloc[0], 'lat': source.geometry.y.iloc[0]})
    
    final_stop_name = input_final_stop.get('name', pd.Series(['Final stop'])).iloc[0]
    all_points_for_lookup.append({'name': final_stop_name, 'lon': final_stop.geometry.x.iloc[0], 'lat': final_stop.geometry.y.iloc[0]})

    for _, row in destinations.iterrows():
        point_name = row[input_dest_id_field] if input_dest_id_field in row else "Unnamed Destination"
        all_points_for_lookup.append({'name': point_name, 'lon': row.geometry.x, 'lat': row.geometry.y})
    
    try:
        # Get the optimized itinerary
        opt = ors_client.optimization(jobs=jobs, vehicles=[vehicle], geometry=True)

    # Handle various error types given the returned error messages from the API
    except exceptions.ApiError as e:
        error_message = str(e)
        lon_err, lat_err = None, None

        # Handle "Could not find routable point" error
        if 'Could not find routable point' in error_message:
            match = re.search(r"coordinate \d+: (-?\d+\.?\d*)\s+(-?\d+\.?\d*)", error_message)
            if match:
                lon_err, lat_err = float(match.group(1)), float(match.group(2))

        # Handle "Unfound route(s) from location" for start/end/multiple destinations
        elif 'Unfound route(s) from location' in error_message:
            # This error uses [lon,lat] format
            match = re.search(r"location \[(-?\d+\.?\d*),(-?\d+\.?\d*)\]", error_message)
            if match:
                lon_err, lat_err = float(match.group(1)), float(match.group(2))

        # If we successfully parsed coordinates from any known error format, find the point name
        if lon_err is not None and lat_err is not None:
            for point in all_points_for_lookup:
                if abs(point['lon'] - lon_err) < 0.0001 and abs(point['lat'] - lat_err) < 0.0001:
                    # Raise a new, more descriptive error for the Shiny app to display
                    raise ValueError(
                        f"Unroutable Location: The point named '{point['name']}' could not be reached. "
                        "Places that are more than 500m from a road are considered to be unreachable!"
                    )
        
        # If the error message is not handled under the two cases above or parsing failed, re-raise the original error
        raise e

    steps = opt['routes'][0]['steps'] 
    
    job_sequence = pd.DataFrame([
        {'job': item.get('job'), 'distance': item.get('distance'), 'location': item['location']} 
        for item in steps 
        if item.get('job') is not None
    ])
    job_sequence = job_sequence.sort_values( by='distance') 
    job_sequence = job_sequence.reset_index(drop=False).rename(columns={'index': 'rank'})
    job_sequence['rank'] = job_sequence['rank'] + 1

    job_sequence['lon'] = job_sequence['location'].apply(lambda loc: loc[0])
    job_sequence['lat'] = job_sequence['location'].apply(lambda loc: loc[1])

    gdf = gpd.GeoDataFrame(
        job_sequence, 
        geometry=[Point(xy) for xy in zip(job_sequence['lon'], job_sequence['lat'])],
        crs="EPSG:4326"
    )
    
    gdf = gdf.drop(columns=['location', 'lat', 'lon'])

    gdf = gdf.to_crs(gdf.estimate_utm_crs()) 
    gdf['geometry'] = gdf['geometry'].buffer(10)
    gdf = gdf.to_crs('EPSG:4326')

    destinations = gpd.sjoin(destinations, gdf, how="left", predicate="intersects")
    destinations['name'] = 'Destination ' + destinations['rank'].astype(str)
    cols = ['name'] + [col for col in destinations if col != 'name']
    destinations = destinations[cols]
    destinations = destinations.sort_values(by='rank')
    destinations = destinations[['name', input_dest_id_field, 'distance', 'geometry']]

    locations = pd.DataFrame(steps, columns=['type', 'job', 'location', 'distance'])
    locations = locations.sort_values( by='distance')
    locations = locations.reset_index(drop=False).rename(columns={'index': 'rank'})

    locations[['lon', 'lat']] = pd.DataFrame(locations['location'].tolist(), index=locations.index)
    locations.rename(columns={'type': 'name'}, inplace=True)
    locations['name'] = locations.apply(lambda row: 
                                        f'destination {int(row["rank"])}' 
                                        if row['name'] == 'job' else ('home_base' 
                                                                    if row['name'] == 'start' else 'final_stop'), axis=1)
    ordered_coords = locations['location'].tolist()
    locations.drop(columns=['rank', 'job', 'location'], inplace=True)

    route_segments = pd.DataFrame({
        'segment_name': locations['name'].shift(1, fill_value='home_base') + ' to ' + locations['name'],
        'origin_lon': locations['lon'].shift(1),
        'origin_lat': locations['lat'].shift(1),
        'end_lon': locations['lon'],
        'end_lat': locations['lat'],
        'distance': locations['distance']
    })

    route_segments['distance'] = route_segments['distance'].diff()
    route_segments['distance'] = (route_segments['distance']/1000).round(2)
    route_segments = route_segments.iloc[1:].reset_index(drop=True)

    dest_names = destinations[['name', input_dest_id_field]]
    dest_names = dest_names.assign(name=dest_names['name'].str.lower())
    dest_names = dest_names.set_index('name')[input_dest_id_field].to_dict()

    def replace_names(text, name_list):
        for key, value in name_list.items():
            text = text.replace(key, value)
        return text

    route_segments['segment_name'] = route_segments['segment_name'].apply(lambda x: replace_names(x, dest_names))

    directions = ors_client.directions(
        coordinates=ordered_coords,
        profile='driving-car',
        extra_info=['surface'],
        format='geojson'
    )

    surface_details = directions['features'][0]['properties']['extras']['surface']['values']
    surface_summary = directions['features'][0]['properties']['extras']['surface']['summary']
    route_geom = directions['features'][0]['geometry']
    
    def add_road_surface_id(route_geometry, details):
        coords = route_geometry['coordinates']
        route = LineString(coords)
        gdf = gpd.GeoDataFrame(geometry=[route], crs='EPSG:4326')
        
        def split_line(line):
            return [LineString([line.coords[i], line.coords[i+1]]) 
                    for i in range(len(line.coords) - 1)]
        
        gdf['segments'] = gdf.geometry.apply(split_line)
        
        gdf = gdf.explode('segments')
        gdf = gdf.reset_index(drop=True)
        gdf['geometry'] = gdf['segments']
        gdf = gdf.drop(columns=['segments'])
        
        gdf['surface'] = None
        for start, end, surface_id in details:
            gdf.loc[start:end, 'surface'] = surface_id
        
        return gdf

    route_detailed = add_road_surface_id(route_geom, surface_details)

    surface_codes = {
        0: "Unknown", 1: "Paved", 2: "Unpaved", 3: "Asphalt", 4: "Concrete",
        6: "Metal", 7: "Wood", 8: "Compacted Gravel", 10: "Gravel", 11: "Dirt",
        12: "Ground", 13: "Ice", 14: "Paving Stones", 15: "Sand", 17: "Grass",
        18: "Grass Paver"
    }
    route_detailed['surface'] = route_detailed['surface'].map(lambda x: surface_codes.get(x, "Unknown"))

    route_detailed = route_detailed.to_crs(route_detailed.estimate_utm_crs())
    route_detailed['segment_length'] = route_detailed.geometry.length
    
    cols = [col for col in route_detailed if col != 'geometry'] + ['geometry']
    route_detailed = route_detailed[cols]
    route_detailed = route_detailed.to_crs('EPSG:4326')

    surface_summary_api = pd.DataFrame(surface_summary)
    surface_summary_api['value'] = surface_summary_api['value'].astype(int)
    surface_summary_api['surface'] = surface_summary_api['value'].map(lambda x: surface_codes.get(x, "Unknown"))

    return route_detailed, source, final_stop, destinations, route_segments, input_dest_id_field