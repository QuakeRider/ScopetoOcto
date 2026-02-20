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

import json
import sys
from pathlib import Path
import argparse

import pandas as pd

# Import our helper modules
from get_stations import get_stations_in_bounds, get_stations_with_metadata, create_pyocto_stations_df
from quakescope_to_pyocto import QuakeScopePicksDownloader


def save_run_config(output_dir: Path, config: dict) -> Path:
    """Save run parameters to ``run_config.json`` inside *output_dir*.

    Called at the start of every fresh (non-resume) run so that an interrupted
    run can be restarted with ``--resume`` without having to re-specify all the
    original arguments.
    """
    config_file = output_dir / 'run_config.json'
    with open(config_file, 'w') as f:
        json.dump(config, f, indent=2)
    print(f"Saved run configuration to: {config_file}")
    return config_file


def load_run_config(output_dir: Path) -> dict:
    """Load run parameters from ``run_config.json`` inside *output_dir*.

    Raises ``FileNotFoundError`` if the file does not exist (i.e. the directory
    was not created by ScopetoOcto or the config was never saved).
    """
    config_file = output_dir / 'run_config.json'
    if not config_file.exists():
        raise FileNotFoundError(
            f"No run_config.json found in {output_dir}.\n"
            f"Cannot resume: this directory was not created by ScopetoOcto, "
            f"or it predates the resume feature."
        )
    with open(config_file) as f:
        return json.load(f)


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
    organize_by_day: bool = True,
    resume: bool = False,
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
    resume : bool
        When True, load station metadata from the existing output directory
        and skip any stations that were already fully downloaded in a previous
        run (determined by ``picks/download_progress.json``).
    """
    print("="*80)
    print(" QuakeScope Pick Downloader" + (" (RESUMING)" if resume else ""))
    print("="*80)

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    crs = None

    if resume:
        # ── Resume path ────────────────────────────────────────────────────
        # Load the station list that was saved during the original run rather
        # than re-querying FDSN.  The station metadata file must exist.
        print(f"\nResuming interrupted download from: {output_path}")
        stations_file = output_path / 'station_metadata.csv'
        if not stations_file.exists():
            print(f"ERROR: Cannot resume — no station_metadata.csv found in {output_path}")
            return None
        stations_df = pd.read_csv(stations_file)
        print(f"Loaded {len(stations_df)} station entries from {stations_file}")

        picks_dir = output_path / 'picks'
        progress_file = picks_dir / 'download_progress.json'
        if progress_file.exists():
            with open(progress_file) as pf:
                prog = json.load(pf)
            n_done = len(prog.get('completed_tids', []))
            print(f"Progress file found: {n_done}/{len(stations_df)} station(s) "
                  f"previously completed")
        else:
            print("No progress file found — will attempt all stations "
                  "(already-written files will be appended to, not overwritten)")

    else:
        # ── Normal (fresh) run ─────────────────────────────────────────────
        # Save the run configuration so it can be reloaded by --resume.
        config = {
            'minlat': minlat,
            'maxlat': maxlat,
            'minlon': minlon,
            'maxlon': maxlon,
            'start': start_time,
            'end': end_time,
            'networks': networks,
            'phases': phases,
            'min_score': min_score,
            'max_score': max_score,
            'channels': channel_filter,
            'fdsn_client': fdsn_client,
            'organize_by_day': organize_by_day,
            'output_dir': str(output_path.absolute()),
        }
        save_run_config(output_path, config)

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

    # Step 2 (or Step 1 on resume): Download picks from QuakeScope
    step_num = "1" if resume else "2"
    print(f"\nStep {step_num}: Downloading picks from QuakeScope")
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
        output_format='csv',
        resume=resume,
    )
    
    # Summary
    print(f"\n{'='*80}")
    print(" Download Complete!")
    print("="*80)
    print(f"\nOutput directory: {output_path}")
    print(f"  - Station metadata: station_metadata.csv")
    if (output_path / 'pyocto_stations.csv').exists():
        print(f"  - PyOcto stations:  pyocto_stations.csv")
    if crs:
        print(f"  - CRS information:  crs_info.json")
    print(f"  - Pick files:       {len(pick_files)} files in picks/YYYY-MM-DD/ subdirectories")

    if crs:
        print(f"\nCoordinate Reference System:")
        print(f"  {crs}")

    return {
        'stations_df': stations_df,
        'pick_files': pick_files,
        'output_dir': output_path,
        'crs': crs,
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

  # Resume an interrupted download
  python workflow.py --resume ./quakescope_output
        """
    )

    # Resume mode — mutually exclusive with a fresh run's required arguments.
    parser.add_argument(
        '--resume', metavar='OUTPUT_DIR',
        help=(
            'Resume an interrupted download. Pass the output directory of the '
            'original run. All parameters are read from the run_config.json '
            'saved there; no other flags are needed (or used).'
        )
    )

    # Required arguments (not needed when --resume is given)
    parser.add_argument('--minlat', type=float, help='Minimum latitude')
    parser.add_argument('--maxlat', type=float, help='Maximum latitude')
    parser.add_argument('--minlon', type=float, help='Minimum longitude')
    parser.add_argument('--maxlon', type=float, help='Maximum longitude')
    parser.add_argument('--start', help='Start time (ISO format: YYYY-MM-DDTHH:MM:SS)')
    parser.add_argument('--end', help='End time (ISO format: YYYY-MM-DDTHH:MM:SS)')

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

    # ── Resume mode ────────────────────────────────────────────────────────
    if args.resume:
        resume_dir = Path(args.resume)
        try:
            config = load_run_config(resume_dir)
        except FileNotFoundError as e:
            print(f"ERROR: {e}")
            sys.exit(1)

        result = download_and_format_picks(
            minlat=config['minlat'],
            maxlat=config['maxlat'],
            minlon=config['minlon'],
            maxlon=config['maxlon'],
            start_time=config['start'],
            end_time=config['end'],
            networks=config.get('networks'),
            phases=config.get('phases', ['P', 'S']),
            min_score=config.get('min_score', 0.3),
            max_score=config.get('max_score', 1.0),
            channel_filter=config.get('channels', 'HH?,EH?,BH?'),
            output_dir=str(resume_dir),
            fdsn_client=config.get('fdsn_client', 'IRIS'),
            organize_by_day=config.get('organize_by_day', True),
            resume=True,
        )
        if result is None:
            sys.exit(1)
        return

    # ── Fresh run mode ─────────────────────────────────────────────────────
    # Validate that the required positional arguments were provided.
    missing = [f'--{f}' for f in ('minlat', 'maxlat', 'minlon', 'maxlon', 'start', 'end')
               if getattr(args, f.replace('-', '_')) is None]
    if missing:
        parser.error(
            f"the following arguments are required for a fresh run: "
            f"{', '.join(missing)}\n"
            f"(To resume an interrupted run, use --resume OUTPUT_DIR instead.)"
        )

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
        organize_by_day=not args.no_organize_by_day,
        resume=False,
    )

    if result is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
