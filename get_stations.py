#!/usr/bin/env python3
"""
Station List Helper for QuakeScope Downloads

Gets station lists within geographic bounds using FDSN web services.
This is necessary because QuakeScope queries by station ID, not by lat/lon.

Author: Grant
Date: 2025-01-29
"""

from obspy.clients.fdsn import Client
from obspy import UTCDateTime
from typing import List, Optional, Tuple
import pandas as pd
import numpy as np


def get_stations_in_bounds(
    minlat: float,
    maxlat: float,
    minlon: float,
    maxlon: float,
    networks: Optional[List[str]] = None,
    channels: Optional[str] = 'HH?,EH?,BH?',
    starttime: Optional[str] = None,
    endtime: Optional[str] = None,
    client_name: str = 'IRIS'
) -> List[str]:
    """
    Get station trace IDs within geographic bounds using FDSN web services.
    
    Returns unique station IDs (one per station, not per channel).
    
    Parameters:
    -----------
    minlat, maxlat : float
        Latitude bounds (decimal degrees)
    minlon, maxlon : float
        Longitude bounds (decimal degrees)
    networks : list of str, optional
        Network codes to query (e.g., ['UW', 'CC', 'CN'])
        If None, queries all networks
    channels : str, optional
        Channel codes (wildcards allowed, e.g., 'HH?,EH?')
    starttime, endtime : str, optional
        Time bounds (ISO format: YYYY-MM-DDTHH:MM:SS)
        Stations must be operating during this time
    client_name : str
        FDSN client to use ('IRIS', 'NCEDC', 'SCEDC', etc.)
        
    Returns:
    --------
    list of str
        Unique trace IDs in format "NET.STA.LOC"
        
    Examples:
    ---------
    >>> # Get UW network stations in Western Washington
    >>> stations = get_stations_in_bounds(
    ...     minlat=46.0, maxlat=49.0,
    ...     minlon=-125.0, maxlon=-120.0,
    ...     networks=['UW']
    ... )
    """
    print(f"Querying {client_name} for stations...")
    print(f"Bounds: ({minlat}, {minlon}) to ({maxlat}, {maxlon})")
    
    # Initialize FDSN client
    client = Client(client_name)
    
    # Prepare query parameters
    kwargs = {
        'minlatitude': minlat,
        'maxlatitude': maxlat,
        'minlongitude': minlon,
        'maxlongitude': maxlon,
        'channel': channels,
        'level': 'station'  # Changed to station level to get unique stations
    }
    
    if networks:
        kwargs['network'] = ','.join(networks)
    
    if starttime:
        kwargs['starttime'] = UTCDateTime(starttime)
    
    if endtime:
        kwargs['endtime'] = UTCDateTime(endtime)
    
    # Get inventory
    try:
        inventory = client.get_stations(**kwargs)
    except Exception as e:
        print(f"Error querying FDSN: {e}")
        return []
    
    # Extract unique station trace IDs
    trace_ids = set()
    
    for network in inventory:
        net_code = network.code
        for station in network:
            sta_code = station.code
            # Most stations have empty location code
            # For simplicity, use empty location code
            tid = f"{net_code}.{sta_code}."
            trace_ids.add(tid)
    
    trace_ids = sorted(list(trace_ids))
    print(f"Found {len(trace_ids)} unique stations")
    
    return trace_ids


def get_stations_with_metadata(
    minlat: float,
    maxlat: float,
    minlon: float,
    maxlon: float,
    networks: Optional[List[str]] = None,
    channels: Optional[str] = 'HH?,EH?,BH?',
    starttime: Optional[str] = None,
    endtime: Optional[str] = None,
    client_name: str = 'IRIS'
) -> pd.DataFrame:
    """
    Get stations with full metadata (coordinates, elevation, etc.).
    Useful for creating PyOcto station DataFrames.
    
    Returns one row per unique station (not per channel), with channel types
    aggregated as comma-separated 2-letter prefixes (e.g., "HH,EH").
    
    Parameters are the same as get_stations_in_bounds().
    
    Returns:
    --------
    pd.DataFrame
        Station metadata with columns: network, station, location, channels,
        latitude, longitude, elevation, start_date, end_date, tid
        Note: 'channels' contains unique 2-letter channel prefixes (e.g., "HH,BH")
    """
    print(f"Querying {client_name} for station metadata...")
    
    # Initialize FDSN client
    client = Client(client_name)
    
    # Prepare query parameters
    kwargs = {
        'minlatitude': minlat,
        'maxlatitude': maxlat,
        'minlongitude': minlon,
        'maxlongitude': maxlon,
        'channel': channels,
        'level': 'channel'
    }
    
    if networks:
        kwargs['network'] = ','.join(networks)
    
    if starttime:
        kwargs['starttime'] = UTCDateTime(starttime)
    
    if endtime:
        kwargs['endtime'] = UTCDateTime(endtime)
    
    # Get inventory
    try:
        inventory = client.get_stations(**kwargs)
    except Exception as e:
        print(f"Error querying FDSN: {e}")
        return pd.DataFrame()
    
    # Extract station information — one row per (network, station, location_code).
    # Each location code gets its own operational window and channel type list so
    # that retired location codes (e.g. IU.CCM.60, active 1999-2018) are tracked
    # separately from currently active ones (IU.CCM.00, IU.CCM.10, …).
    stations_data = []

    for network in inventory:
        net_code = network.code
        for station in network:
            sta_code = station.code
            sta_lat = station.latitude
            sta_lon = station.longitude
            sta_elev = station.elevation

            # Group channels by location code so each location gets its own row.
            loc_data: dict = {}  # loc_code -> {prefixes, earliest_start, latest_end, is_active}

            for channel in station:
                loc_code = channel.location_code if channel.location_code else ''
                chan_code = channel.code
                start_date = channel.start_date
                end_date = channel.end_date

                if loc_code not in loc_data:
                    loc_data[loc_code] = {
                        'prefixes': set(),
                        'earliest_start': None,
                        'latest_end': None,
                        # True once any channel epoch has end_date=None (still active).
                        # When True, the location code is open-ended regardless of any
                        # other channel's retirement date.
                        'is_active': False,
                    }

                d = loc_data[loc_code]
                if len(chan_code) >= 2:
                    d['prefixes'].add(chan_code[:2])

                if d['earliest_start'] is None or (start_date and start_date < d['earliest_start']):
                    d['earliest_start'] = start_date

                # Track the latest end date correctly across multiple channel epochs.
                # A channel with end_date=None means that epoch is still running, so
                # the location code is currently active.  Once we know the location is
                # active we stop updating latest_end — an open-ended station must not
                # have its end_date pinned to a retired channel epoch's close date.
                if end_date is None:
                    d['is_active'] = True
                elif not d['is_active']:
                    if d['latest_end'] is None or end_date > d['latest_end']:
                        d['latest_end'] = end_date

            # Emit one row per location code found at this station.
            physical_station = f"{net_code}.{sta_code}"
            for loc_code, d in loc_data.items():
                tid = f"{net_code}.{sta_code}.{loc_code}"
                stations_data.append({
                    'network': net_code,
                    'station': sta_code,
                    'location': loc_code,
                    'physical_station': physical_station,
                    'channels': ','.join(sorted(d['prefixes'])),
                    'latitude': sta_lat,
                    'longitude': sta_lon,
                    'elevation': sta_elev,
                    'start_date': d['earliest_start'],
                    # None signals open-ended (currently active); only use the
                    # latest channel end date when no epoch is still running.
                    'end_date': None if d['is_active'] else d['latest_end'],
                    'tid': tid,
                })

    df = pd.DataFrame(stations_data)

    # Deduplicate by tid (each NET.STA.LOC should already be unique, but guard
    # against edge cases where FDSN returns the same location code twice).
    df = df.drop_duplicates(subset='tid')

    if not df.empty:
        n_phys = df['physical_station'].nunique()
        print(f"Found {len(df)} unique station/location combinations "
              f"across {n_phys} physical stations")
        print(f"Channel types found: {sorted(set(','.join(df['channels'].unique()).split(',')))}")
    else:
        print("No stations found")

    return df


def create_pyocto_stations_df(
    stations_df: pd.DataFrame,
    use_pyocto_projection: bool = True
) -> Tuple[pd.DataFrame, Optional[object]]:
    """
    Convert FDSN station metadata to PyOcto station format using proper projection.
    
    This function uses PyOcto's built-in coordinate transformation methods
    to properly project lat/lon to a local Cartesian coordinate system.
    
    Parameters:
    -----------
    stations_df : pd.DataFrame
        Station metadata from get_stations_with_metadata()
    use_pyocto_projection : bool
        If True, use PyOcto's projection. If False, uses simple approximation.
        
    Returns:
    --------
    tuple: (pd.DataFrame, CRS or None)
        - Stations in PyOcto format with columns: station (tid), x, y, z
        - Coordinate Reference System (CRS) object if created, None otherwise
        
    Notes:
    ------
    The returned CRS object should be used when creating your actual associator
    for pick association to ensure coordinates are consistent.
    """
    if stations_df.empty:
        return pd.DataFrame(columns=['station', 'x', 'y', 'z']), None
    
    try:
        from pyocto import OctoAssociator
        import pyproj
        from pyproj import CRS
    except ImportError:
        print("ERROR: PyOcto not installed. Install with: pip install pyocto")
        print("Falling back to simple approximation (NOT RECOMMENDED)")
        return _create_stations_simple_approx(stations_df), None

    # Explicitly set the PROJ data directory via pyproj's Python API.
    # Setting PROJ_DATA env vars alone is unreliable on HPC clusters because
    # the PROJ C library caches its context path at load time; calling
    # pyproj.datadir.set_data_dir() after import correctly updates that context.
    import os as _os, sys as _sys, importlib.util as _ilu
    _pyproj_spec = _ilu.find_spec('pyproj')
    _pyproj_pkg_dir = ''
    if _pyproj_spec is not None:
        _locs = getattr(_pyproj_spec, 'submodule_search_locations', None)
        _pyproj_pkg_dir = list(_locs)[0] if _locs else _os.path.dirname(_pyproj_spec.origin or '')
    for _proj_candidate in (
        _os.environ.get('PROJ_DATA', ''),
        _os.environ.get('PROJ_LIB', ''),
        _os.path.join(_sys.prefix, 'share', 'proj'),
        _os.path.join(_pyproj_pkg_dir, 'proj_dir', 'share', 'proj'),
    ):
        if _proj_candidate and _os.path.isfile(_os.path.join(_proj_candidate, 'proj.db')):
            pyproj.datadir.set_data_dir(_proj_candidate)
            break
    del _os, _sys, _ilu, _pyproj_spec, _pyproj_pkg_dir, _proj_candidate
    
    # Collapse to one entry per physical station (NET.STA) for the PyOcto
    # coordinate frame.  When stations_df was produced by get_stations_with_metadata
    # it now contains one row per location code; we deduplicate on the physical
    # station key so that PyOcto sees a single coordinate per site.
    # Use the earliest start / latest end across all location codes for the
    # ObsPy Station object (only used for inventory_to_df projection).
    if 'physical_station' in stations_df.columns:
        unique_stations = (
            stations_df
            .sort_values('start_date')
            .drop_duplicates(subset='physical_station', keep='first')
            .copy()
        )
        # Recover the latest end date across all location codes for this station.
        # Use a custom aggregation with Python's native max() to avoid pandas/numpy
        # converting UTCDateTime objects to floats via __float__ during groupby.max().
        def _utc_max(s):
            vals = [v for v in s if v is not None and v == v]  # v==v rejects NaN floats
            return max(vals) if vals else None
        latest_ends = stations_df.groupby('physical_station')['end_date'].agg(_utc_max)
        unique_stations['end_date'] = unique_stations['physical_station'].map(latest_ends)
        # Use NET.STA. (empty location code) as the canonical tid for PyOcto.
        unique_stations['tid'] = unique_stations['physical_station'] + '.'
    else:
        # Fallback for DataFrames produced by older versions of get_stations_with_metadata.
        unique_stations = stations_df.drop_duplicates(subset='tid')

    # Create ObsPy inventory from the station data
    from obspy import Inventory
    from obspy.core.inventory import Network, Station
    from obspy import UTCDateTime

    def _to_utc(val):
        """Convert val to UTCDateTime, handling UTCDateTime objects, floats (epoch
        seconds, as produced when pandas converts UTCDateTime via __float__), ISO
        strings (as read back from CSV), and None/NaN."""
        if val is None:
            return None
        if isinstance(val, UTCDateTime):
            return val
        try:
            if pd.isna(val):
                return None
        except (TypeError, ValueError):
            pass
        try:
            return UTCDateTime(float(val))
        except (ValueError, TypeError):
            return UTCDateTime(str(val))

    networks = {}
    for _, row in unique_stations.iterrows():
        net_code = row['network']
        sta_code = row['station']

        if net_code not in networks:
            networks[net_code] = Network(code=net_code)

        # Check if station already exists in network
        sta_exists = False
        for sta in networks[net_code]:
            if sta.code == sta_code:
                sta_exists = True
                break

        if not sta_exists:
            sta = Station(
                code=sta_code,
                latitude=row['latitude'],
                longitude=row['longitude'],
                elevation=row['elevation'],
                start_date=_to_utc(row['start_date']),
                end_date=_to_utc(row['end_date'])
            )
            networks[net_code].stations.append(sta)
    
    inventory = Inventory(networks=list(networks.values()), source="FDSN")
    
    # Get bounds for creating CRS
    min_lat = unique_stations['latitude'].min()
    max_lat = unique_stations['latitude'].max()
    min_lon = unique_stations['longitude'].min()
    max_lon = unique_stations['longitude'].max()
    
    print(f"Creating PyOcto coordinate system...")
    print(f"  Latitude range: {min_lat:.4f} to {max_lat:.4f}")
    print(f"  Longitude range: {min_lon:.4f} to {max_lon:.4f}")
    
    # Create CRS using PyOcto's get_crs method
    try:
        # Create a fake stations DataFrame for PyOcto's get_crs
        fake_stations = pd.DataFrame({
            'latitude': [min_lat, max_lat],
            'longitude': [min_lon, max_lon]
        })
        
        crs = OctoAssociator.get_crs(fake_stations)
        
        # Create a temporary associator to use inventory_to_df
        temp_assoc = OctoAssociator(
            xlim=(-1000, 1000),  # Dummy values
            ylim=(-1000, 1000),
            zlim=(-5, 50),
            velocity_model='homogeneous',
            time_before=10.0,
            crs=crs
        )
        
        # Use PyOcto's inventory_to_df method to get properly projected coordinates
        pyocto_stations = temp_assoc.inventory_to_df(inventory)
        
        # Rename 'id' column to 'station' to match our format
        pyocto_stations = pyocto_stations.rename(columns={'id': 'station'})
        
        # Keep only required columns
        pyocto_stations = pyocto_stations[['station', 'x', 'y', 'z']].dropna()
        
        print(f"Successfully projected {len(pyocto_stations)} stations using PyOcto")
        center_lat = (min_lat + max_lat) / 2
        center_lon = (min_lon + max_lon) / 2
        print(f"  CRS: Transverse Mercator centered at ({center_lat:.4f}, {center_lon:.4f})")
        print(f"  X range: {pyocto_stations['x'].min():.2f} to {pyocto_stations['x'].max():.2f} km")
        print(f"  Y range: {pyocto_stations['y'].min():.2f} to {pyocto_stations['y'].max():.2f} km")
        print(f"  Z range: {pyocto_stations['z'].min():.2f} to {pyocto_stations['z'].max():.2f} km")
        
        return pyocto_stations, crs
        
    except Exception as e:
        print(f"ERROR using PyOcto projection: {e}")
        print("Falling back to simple approximation")
        return _create_stations_simple_approx(stations_df), None


def _create_stations_simple_approx(stations_df: pd.DataFrame) -> pd.DataFrame:
    """
    Fallback: Simple lat/lon to km approximation (NOT RECOMMENDED).
    This is only used if PyOcto projection fails.
    """
    import numpy as np

    if 'physical_station' in stations_df.columns:
        unique_stations = stations_df.drop_duplicates(subset='physical_station').copy()
        unique_stations['tid'] = unique_stations['physical_station'] + '.'
    else:
        unique_stations = stations_df.drop_duplicates(subset='tid')
    
    reference_lat = unique_stations['latitude'].mean()
    reference_lon = unique_stations['longitude'].mean()
    
    print(f"WARNING: Using simple approximation (NOT RECOMMENDED FOR PRODUCTION)")
    print(f"Reference point: ({reference_lat:.4f}, {reference_lon:.4f})")
    
    # Simple approximation: degrees to km
    lat_to_km = 111.0
    lon_to_km = 111.0 * np.cos(np.radians(reference_lat))
    
    pyocto_stations = pd.DataFrame({
        'station': unique_stations['tid'],
        'x': (unique_stations['longitude'] - reference_lon) * lon_to_km,
        'y': (unique_stations['latitude'] - reference_lat) * lat_to_km,
        'z': -unique_stations['elevation'] / 1000.0  # Convert m to km, negative for depth
    })
    
    return pyocto_stations


# Example usage
if __name__ == "__main__":
    import numpy as np
    
    print("="*60)
    print("Station List Helper - Example Usage")
    print("="*60)
    
    # Example 1: Get station trace IDs for Pacific Northwest
    print("\nExample 1: Get UW network stations in Western Washington")
    print("-"*60)
    
    station_ids = get_stations_in_bounds(
        minlat=46.0,
        maxlat=49.0,
        minlon=-125.0,
        maxlon=-120.0,
        networks=['UW'],
        channels='HH?,EH?',  # HH and EH channels
        client_name='IRIS'
    )
    
    print(f"\nFound stations:")
    for tid in station_ids[:10]:  # Print first 10
        print(f"  {tid}")
    if len(station_ids) > 10:
        print(f"  ... and {len(station_ids) - 10} more")
    
    # Example 2: Get stations with full metadata
    print("\n" + "="*60)
    print("Example 2: Get station metadata")
    print("-"*60)
    
    stations_df = get_stations_with_metadata(
        minlat=46.0,
        maxlat=49.0,
        minlon=-125.0,
        maxlon=-120.0,
        networks=['UW'],
        channels='HH?',
        client_name='IRIS'
    )
    
    if not stations_df.empty:
        print("\nStation metadata (first 10 unique stations):")
        print(stations_df.head(10))
        print(f"\nTotal unique stations: {len(stations_df)}")
        
        # Example 3: Create PyOcto station format
        print("\n" + "="*60)
        print("Example 3: Create PyOcto stations DataFrame")
        print("-"*60)
        
        pyocto_stations, crs = create_pyocto_stations_df(stations_df)
        print("\nPyOcto stations format:")
        print(pyocto_stations.head())
        
        if crs:
            print(f"\nCoordinate Reference System (CRS): {crs}")
        
        # Save station lists
        print("\n" + "="*60)
        print("Saving station lists...")
        print("-"*60)
        
        # Save trace IDs (for QuakeScope queries)
        with open('station_trace_ids.txt', 'w') as f:
            for tid in station_ids:
                f.write(f"{tid}\n")
        print(f"Saved {len(station_ids)} trace IDs to 'station_trace_ids.txt'")
        
        # Save full metadata
        stations_df.to_csv('station_metadata.csv', index=False)
        print(f"Saved station metadata to 'station_metadata.csv'")
        
        # Save PyOcto stations
        pyocto_stations.to_csv('pyocto_stations.csv', index=False)
        print(f"Saved PyOcto stations to 'pyocto_stations.csv'")
        
        # Save CRS info if available
        if crs:
            import json
            crs_info = {
                'crs_wkt': crs.to_wkt(),
                'crs_proj4': crs.to_proj4()
            }
            with open('crs_info.json', 'w') as f:
                json.dump(crs_info, f, indent=2)
            print(f"Saved CRS information to 'crs_info.json'")
    
    print("\n" + "="*60)
    print("Complete!")
    print("="*60)
