import pandas as pd
import geopandas as gpd
import openrouteservice as ors
from openrouteservice.optimization import Vehicle, Job
from shapely.geometry import Point, LineString

# Initialize OpenRouteService client
ors_client = ors.Client(key="5b3ce3597851110001cf6248c25b517341e94033b8c21d63ecda95b6")

# Define the optimal_route function
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

    # Get the optimized itinerary
    opt = ors_client.optimization(jobs=jobs, vehicles=[vehicle], geometry=True)
    steps = opt['routes'][0]['steps'] # get the steps

    # get the job sequence
    job_sequence = pd.DataFrame([
        {'job': item.get('job'), 'distance': item.get('distance'), 'location': item['location']} 
        for item in steps 
        if item.get('job') is not None
    ])
    # Sort the job_sequence dataframe by distance and convert the index into rank
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

    # Drop the original location column
    gdf = gdf.drop(columns=['location', 'lat', 'lon'])

    # add a small buffer to allow for overlay operation
    gdf = gdf.to_crs(gdf.estimate_utm_crs()) # transform to the most appropriate utm projection
    gdf['geometry'] = gdf['geometry'].buffer(10)
    gdf = gdf.to_crs('EPSG:4326')

    # join job sequence geodf to the destinations dataframe
    destinations = gpd.sjoin(destinations, gdf, how="left", predicate="intersects")
    destinations['name'] = 'Destination ' + destinations['rank'].astype(str) # add the "name" column
    cols = ['name'] + [col for col in destinations if col != 'name'] # Relocate 'name' to the beginning
    destinations = destinations[cols]
    destinations = destinations.sort_values(by='rank') # Arrange by 'job'
    destinations = destinations[['name', input_dest_id_field, 'distance', 'geometry']] # select needed fields

    # Get the route segments and their lengths
    #-----------------------------------------------
    locations = pd.DataFrame(steps, columns=['type', 'job', 'location', 'distance'])
    locations = locations.sort_values( by='distance') # Sort the dataframe by distance
    locations = locations.reset_index(drop=False).rename(columns={'index': 'rank'})

    locations[['lon', 'lat']] = pd.DataFrame(locations['location'].tolist(), index=locations.index) # separate location column
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

    # calculate the actual segment length from the cumulative distance
    route_segments['distance'] = route_segments['distance'].diff()
    route_segments['distance'] = (route_segments['distance']/1000).round(2) #connvert to Km
    route_segments = route_segments.iloc[1:].reset_index(drop=True)

    # from the destinations dataframe, select only the name and tubr_st columns
    dest_names = destinations[['name', input_dest_id_field]]
    dest_names = dest_names.assign(name=dest_names['name'].str.lower())
    dest_names = dest_names.set_index('name')[input_dest_id_field].to_dict()

    def replace_names(text, name_list):
        for key, value in name_list.items():
            text = text.replace(key, value)
        return text

    route_segments['segment_name'] = route_segments['segment_name'].apply(lambda x: replace_names(x, dest_names))

    # Get road detailed information (surface, slope, and osm_id)
    #-----------------------------------------------
    directions = ors_client.directions(
        coordinates=ordered_coords,
        profile='driving-car',
        extra_info=['surface'],
        format='geojson'
    )

    # Extract road surface information from the directions response
    surface_details = directions['features'][0]['properties']['extras']['surface']['values']
    surface_summary = directions['features'][0]['properties']['extras']['surface']['summary']
    route_geom = directions['features'][0]['geometry']

    # split the route geometry into its constituent segments and append its corresponding surface ID
    def add_road_surface_id(route_geometry, details):
        coords = route_geometry['coordinates'] # the route geometry is a dictionary representing a GeoJSON LineString.
        route = LineString(coords)
        gdf = gpd.GeoDataFrame(geometry=[route], crs='EPSG:4326')
        
        # Split the linestring into its constituent line segments
        def split_line(line):
            return [LineString([line.coords[i], line.coords[i+1]]) 
                    for i in range(len(line.coords) - 1)]
        
        gdf['segments'] = gdf.geometry.apply(split_line)
        
        # Explode the GeoDataFrame so each row represents a single segment
        gdf = gdf.explode('segments')
        gdf = gdf.reset_index(drop=True)
        gdf['geometry'] = gdf['segments']
        gdf = gdf.drop(columns=['segments'])
        
        # Add road surface IDs based on the details list
        gdf['surface'] = None
        for start, end, surface_id in details:
            gdf.loc[start:end, 'surface'] = surface_id
        
        return gdf

    route_detailed = add_road_surface_id(route_geom, surface_details)


    # Replace surface ids with their corresponding names
    surface_codes = {
        0: "Unknown", 1: "Paved", 2: "Unpaved", 3: "Asphalt", 4: "Concrete",
        6: "Metal", 7: "Wood", 8: "Compacted Gravel", 10: "Gravel", 11: "Dirt",
        12: "Ground", 13: "Ice", 14: "Paving Stones", 15: "Sand", 17: "Grass",
        18: "Grass Paver"
    }
    route_detailed['surface'] = route_detailed['surface'].map(lambda x: surface_codes.get(x, "Unknown"))

    # Calculate segment lengths
    route_detailed = route_detailed.to_crs(route_detailed.estimate_utm_crs()) # Transform to UTM for accurate length calculation
    route_detailed['segment_length'] = route_detailed.geometry.length
    
    # rearange the columns so that the geometry column is the last
    cols = [col for col in route_detailed if col != 'geometry'] + ['geometry']
    route_detailed = route_detailed[cols]
    route_detailed = route_detailed.to_crs('EPSG:4326') # Transform back to WGS84 for display with folium

    # Verify my numbers using the summary 
    #surface_summary_calc = route_detailed.groupby('surface')['segment_length'].sum().reset_index()
    surface_summary_api = pd.DataFrame(surface_summary)
    surface_summary_api['value'] = surface_summary_api['value'].astype(int)
    surface_summary_api['surface'] = surface_summary_api['value'].map(lambda x: surface_codes.get(x, "Unknown"))

    return route_detailed, source, final_stop, destinations, route_segments, input_dest_id_field