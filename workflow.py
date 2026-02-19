#!/usr/bin/env python3
"""
QuakeScope Pick Downloader

Downloads picks from QuakeScope and formats them for PyOcto.

Steps:
1. Gets stations within geographic bounds (using FDSN)
2. Downloads picks from QuakeScope for those stations
3. Formats picks for PyOcto
4. Saves picks organized by station/day

Author: Grant
Date: 2025-01-29
"""

import sys
from pathlib import Path
import argparse

# Import our helper modules
from get_stations import get_stations_in_bounds, get_stations_with_metadata, create_pyocto_stations_df
from quakescope_to_pyocto import QuakeScopePicksDownloader


def download_and_format_picks(
    minlat: float,
    maxlat: float,
    minlon: float,
    maxlon: float,
    start_time: str,
    end_time: str,
    networks: list = None,
    phases: list = None,
    min_score: float = 0.3,
    max_score: float = 1.0,
    channel_filter: str = 'HH?,EH?,BH?',
    output_dir: str = './quakescope_output',
    fdsn_client: str = 'IRIS',
    organize_by_day: bool = True
):
    """
    Download picks from QuakeScope and format for PyOcto.
    
    Parameters:
    -----------
    minlat, maxlat : float
        Latitude bounds
    minlon, maxlon : float
        Longitude bounds
    start_time : str
        Start time (ISO format: YYYY-MM-DDTHH:MM:SS)
    end_time : str
        End time (ISO format: YYYY-MM-DDTHH:MM:SS)
    networks : list of str, optional
        Network codes (e.g., ['UW', 'CC'])
    phases : list of str, optional
        Phase types (e.g., ['P', 'S'])
    min_score, max_score : float
        Pick score range
    channel_filter : str
        Channel codes for station query
    output_dir : str
        Output directory
    fdsn_client : str
        FDSN client name
    organize_by_day : bool
        Whether to organize picks by station/day
    """
    print("="*80)
    print(" QuakeScope Pick Downloader")
    print("="*80)
    
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    # Step 1: Get stations within bounds
    print(f"\nStep 1: Getting stations within geographic bounds")
    print("-"*80)
    
    stations_df = get_stations_with_metadata(
        minlat=minlat,
        maxlat=maxlat,
        minlon=minlon,
        maxlon=maxlon,
        networks=networks,
        channels=channel_filter,
        starttime=start_time,
        endtime=end_time,
        client_name=fdsn_client
    )
    
    if stations_df.empty:
        print("ERROR: No stations found in the specified bounds.")
        return None
    
    print(f"\nFound {len(stations_df)} unique stations")
    
    # Save station information
    stations_file = output_path / 'station_metadata.csv'
    stations_df.to_csv(stations_file, index=False)
    print(f"Saved station metadata to: {stations_file}")
    
    # Create PyOcto stations format with proper projection
    pyocto_stations, crs = create_pyocto_stations_df(stations_df)
    pyocto_stations_file = output_path / 'pyocto_stations.csv'
    pyocto_stations.to_csv(pyocto_stations_file, index=False)
    print(f"Saved PyOcto stations to: {pyocto_stations_file}")
    
    # Save CRS information if available
    if crs:
        import json
        crs_file = output_path / 'crs_info.json'
        crs_info = {
            'crs_wkt': crs.to_wkt(),
            'crs_proj4': crs.to_proj4(),
            'bounds': {
                'min_lat': float(stations_df['latitude'].min()),
                'max_lat': float(stations_df['latitude'].max()),
                'min_lon': float(stations_df['longitude'].min()),
                'max_lon': float(stations_df['longitude'].max())
            }
        }
        with open(crs_file, 'w') as f:
            json.dump(crs_info, f, indent=2)
        print(f"Saved CRS information to: {crs_file}")
    else:
        print("WARNING: Using approximate coordinates (PyOcto not available or failed)")
        crs = None
    
    # Step 2: Download picks from QuakeScope
    print(f"\nStep 2: Downloading picks from QuakeScope")
    print("-"*80)
    
    picks_dir = output_path / 'picks'
    downloader = QuakeScopePicksDownloader(output_dir=str(picks_dir))
    
    # Extract channel prefix for QuakeScope query
    if channel_filter and '?' not in channel_filter:
        channel_query = channel_filter[:2] if len(channel_filter) >= 2 else None
    else:
        channel_query = None
    
    pick_files = downloader.run(
        stations_df=stations_df,
        start_time=start_time,
        end_time=end_time,
        phases=phases,
        min_score=min_score,
        max_score=max_score,
        channel=channel_query,
        organize_by_day=organize_by_day,
        output_format='csv'
    )
    
    # Summary
    print(f"\n{'='*80}")
    print(" Download Complete!")
    print("="*80)
    print(f"\nOutput directory: {output_path}")
    print(f"  - Station metadata: {stations_file.name}")
    print(f"  - PyOcto stations:  {pyocto_stations_file.name}")
    if crs:
        print(f"  - CRS information:  crs_info.json")
    print(f"  - Pick files:       {len(pick_files)} files in picks/")
    
    if crs:
        print(f"\nCoordinate Reference System:")
        print(f"  {crs}")
    
    return {
        'stations_df': stations_df,
        'pyocto_stations': pyocto_stations,
        'pick_files': pick_files,
        'output_dir': output_path,
        'crs': crs
    }


def main():
    """Command-line interface."""
    parser = argparse.ArgumentParser(
        description='Download QuakeScope picks and format for PyOcto',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Pacific Northwest (UW network)
  python workflow.py --minlat 46 --maxlat 49 --minlon -125 --maxlon -120 \\
      --networks UW --start 2024-01-01T00:00:00 --end 2024-01-02T00:00:00

  # California (multiple networks)
  python workflow.py --minlat 34 --maxlat 37 --minlon -120 --maxlon -116 \\
      --networks CI NC --start 2024-01-01T00:00:00 --end 2024-01-01T12:00:00 \\
      --min-score 0.5
        """
    )
    
    # Required arguments
    parser.add_argument('--minlat', type=float, required=True,
                       help='Minimum latitude')
    parser.add_argument('--maxlat', type=float, required=True,
                       help='Maximum latitude')
    parser.add_argument('--minlon', type=float, required=True,
                       help='Minimum longitude')
    parser.add_argument('--maxlon', type=float, required=True,
                       help='Maximum longitude')
    parser.add_argument('--start', required=True,
                       help='Start time (ISO format: YYYY-MM-DDTHH:MM:SS)')
    parser.add_argument('--end', required=True,
                       help='End time (ISO format: YYYY-MM-DDTHH:MM:SS)')
    
    # Optional arguments
    parser.add_argument('--networks', nargs='+',
                       help='Network codes (e.g., UW CC CN)')
    parser.add_argument('--phases', nargs='+', default=['P', 'S'],
                       help='Phase types (default: P S)')
    parser.add_argument('--min-score', type=float, default=0.3,
                       help='Minimum pick score (default: 0.3)')
    parser.add_argument('--max-score', type=float, default=1.0,
                       help='Maximum pick score (default: 1.0)')
    parser.add_argument('--channels', default='HH?,EH?,BH?',
                       help='Channel filter (default: HH?,EH?,BH?)')
    parser.add_argument('--output-dir', default='./quakescope_output',
                       help='Output directory (default: ./quakescope_output)')
    parser.add_argument('--fdsn-client', default='IRIS',
                       help='FDSN client (default: IRIS)')
    parser.add_argument('--no-organize-by-day', action='store_true',
                       help='Do not organize picks by station/day')
    
    args = parser.parse_args()
    
    # Run download and formatting
    result = download_and_format_picks(
        minlat=args.minlat,
        maxlat=args.maxlat,
        minlon=args.minlon,
        maxlon=args.maxlon,
        start_time=args.start,
        end_time=args.end,
        networks=args.networks,
        phases=args.phases,
        min_score=args.min_score,
        max_score=args.max_score,
        channel_filter=args.channels,
        output_dir=args.output_dir,
        fdsn_client=args.fdsn_client,
        organize_by_day=not args.no_organize_by_day
    )
    
    if result is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
