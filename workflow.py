#!/usr/bin/env python3
"""
QuakeScope Pick Downloader

Downloads picks from QuakeScope and formats them for PyOcto.

Steps:
1. Gets stations within geographic bounds (using FDSN)
2. Downloads picks from QuakeScope for those stations
3. Formats picks for PyOcto
4. Saves picks organized by station/day

Usage:
  python workflow.py --config config.yaml
  python workflow.py --resume ./quakescope_output
  python workflow.py --plot  ./quakescope_output

Author: Grant
Date: 2025-01-29
"""

# ---------------------------------------------------------------------------
# PROJ database auto-discovery
# Must run before any pyproj / PyOcto imports so the library can locate
# proj.db.  On HPC clusters the conda env is often not fully activated, so
# PROJ_DATA / PROJ_LIB are unset even though the files are present on disk.
# ---------------------------------------------------------------------------
import os as _os


def _setup_proj_data() -> None:
    """Set PROJ_DATA and PROJ_LIB to the conda env's share/proj directory.

    Only skips if an existing env var already points at a valid proj.db so
    that a stale / wrong PROJ_DATA set by the HPC module system is overridden.
    Sets both PROJ_DATA (PROJ 9+) and PROJ_LIB (PROJ 8 and earlier) for
    maximum compatibility.
    """
    def _has_proj_db(path: str) -> bool:
        return bool(path) and _os.path.isfile(_os.path.join(path, 'proj.db'))

    # Honour existing env vars only when they actually point at a valid proj.db.
    if _has_proj_db(_os.environ.get('PROJ_DATA', '')) or \
            _has_proj_db(_os.environ.get('PROJ_LIB', '')):
        return

    import sys as _sys
    import importlib.util as _ilu

    candidates = []

    # 1. sys.prefix is the conda / venv environment root – most direct path.
    candidates.append(_os.path.join(_sys.prefix, 'share', 'proj'))

    # 2. CONDA_PREFIX is set when the environment is properly activated.
    conda_prefix = _os.environ.get('CONDA_PREFIX', '')
    if conda_prefix:
        candidates.append(_os.path.join(conda_prefix, 'share', 'proj'))

    # 3. Walk up from the pyproj package directory (site-packages → … → envroot).
    #    Locate pyproj without importing it so PROJ_DATA is set before the first
    #    pyproj import (find_spec does not execute the module).
    spec = _ilu.find_spec('pyproj')
    if spec is not None:
        locs = getattr(spec, 'submodule_search_locations', None)
        pkg_dir = list(locs)[0] if locs else _os.path.dirname(spec.origin or '')
        current = pkg_dir
        for _ in range(8):
            candidates.append(_os.path.join(current, 'share', 'proj'))
            current = _os.path.dirname(current)

    for proj_dir in candidates:
        if _has_proj_db(proj_dir):
            _os.environ['PROJ_DATA'] = proj_dir  # PROJ 9+
            _os.environ['PROJ_LIB'] = proj_dir   # PROJ 8 and earlier
            break


_setup_proj_data()
del _setup_proj_data
# ---------------------------------------------------------------------------

import json
import logging
import sys
from pathlib import Path
import argparse

import pandas as pd
import yaml

# Import our helper modules
from get_stations import get_stations_in_bounds, get_stations_with_metadata, create_pyocto_stations_df
from quakescope_to_pyocto import QuakeScopePicksDownloader


# ---------------------------------------------------------------------------
# YAML config helpers
# ---------------------------------------------------------------------------

def load_yaml_config(config_path: str) -> dict:
    """Load and return a YAML configuration file.

    Raises ``FileNotFoundError`` if the file does not exist and
    ``ValueError`` if the file cannot be parsed as YAML.
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    try:
        with open(path) as f:
            config = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ValueError(f"Could not parse config file {config_path}: {e}")
    if config is None:
        config = {}
    return config


def _validate_yaml_config(config: dict) -> None:
    """Raise ``ValueError`` for any missing required fields."""
    required = ['minlat', 'maxlat', 'minlon', 'maxlon', 'start', 'end']
    missing = [k for k in required if config.get(k) is None]
    if missing:
        raise ValueError(
            f"The following required fields are missing from the config file: "
            f"{', '.join(missing)}"
        )


# ---------------------------------------------------------------------------
# Run-config persistence (enables --resume)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Core workflow
# ---------------------------------------------------------------------------

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
    output_format: str = 'csv',
    dedup_time_threshold: float = 0.5,
    channel_priority: list = None,
    use_pyocto_projection: bool = True,
    resume: bool = False,
    download_chunk_size: str = 'month',
    workers: int = 0,
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
        Channel codes for station query (FDSN wildcards allowed)
    output_dir : str
        Output directory
    fdsn_client : str
        FDSN client name
    organize_by_day : bool
        When True, write one file per station per day and run the
        cross-location deduplication pass.  When False, save all picks
        in a single combined file with no deduplication.
    output_format : str
        Output file format: 'csv', 'parquet', or 'pickle'.
    dedup_time_threshold : float
        Maximum time separation in seconds between picks from different
        location codes to be considered duplicates.  Only used when
        organize_by_day is True.
    channel_priority : list of str, optional
        Ordered 2-letter channel prefixes, best first.  None uses the
        built-in default inside QuakeScopePicksDownloader.
    use_pyocto_projection : bool
        When True (recommended), use PyOcto's CRS projection for station
        coordinates.  When False, fall back to a simple lat/lon approximation.
    resume : bool
        When True, load station metadata from the existing output directory
        and skip any stations that were already fully downloaded in a previous
        run (determined by ``picks/download_progress.json``).
    download_chunk_size : str
        Time window downloaded in a single API call per station: ``'day'``,
        ``'month'`` (default), or ``'year'``.  Larger values mean fewer API
        round-trips and faster downloads.  Automatic fallback to finer
        granularity occurs when the 10 000-pick limit is hit.
    workers : int
        Number of parallel worker processes for the deduplication step.
        0 = auto-detect (uses all available CPU cores).
        1 = sequential (no sub-processes).
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
            'output_format': output_format,
            'dedup_time_threshold': dedup_time_threshold,
            'channel_priority': channel_priority,
            'use_pyocto_projection': use_pyocto_projection,
            'download_chunk_size': download_chunk_size,
            'workers': workers,
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
        pyocto_stations, crs = create_pyocto_stations_df(
            stations_df, use_pyocto_projection=use_pyocto_projection
        )
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
        output_format=output_format,
        dedup_time_threshold=dedup_time_threshold,
        channel_priority=channel_priority,
        resume=resume,
        chunk_size=download_chunk_size,
        dedup_workers=workers,
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
    print(f"  - Pick files:       {len(pick_files)} files in picks/YYYY/MM/DD/ subdirectories")

    if crs:
        print(f"\nCoordinate Reference System:")
        print(f"  {crs}")

    return {
        'stations_df': stations_df,
        'pick_files': pick_files,
        'output_dir': output_path,
        'crs': crs,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    """Command-line interface."""
    parser = argparse.ArgumentParser(
        description='Download QuakeScope picks and format for PyOcto',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Fresh run — all parameters are set in the config file
  python workflow.py --config config.yaml

  # Resume an interrupted download
  python workflow.py --resume ./quakescope_output

  # Generate diagnostic plots and summaries from a completed download
  python workflow.py --plot ./quakescope_output

See config.yaml for a fully annotated example of all available options.
        """
    )

    parser.add_argument(
        '--config', metavar='CONFIG_FILE',
        help=(
            'Path to a YAML configuration file. Required for a fresh run. '
            'See config.yaml for a documented example.'
        )
    )
    parser.add_argument(
        '--resume', metavar='OUTPUT_DIR',
        help=(
            'Resume an interrupted download. Pass the output directory of the '
            'original run. All parameters are read from the run_config.json '
            'saved there; no other flags are needed (or used).'
        )
    )
    parser.add_argument(
        '--plot', metavar='OUTPUT_DIR',
        help=(
            'Generate diagnostic figures and summary statistics from a '
            'completed download. Pass the output directory used during the '
            'original run. Produces per-station figures, network heatmaps, '
            'geographic and temporal summary plots, and CSV/text summaries. '
            'Does not re-download any data.'
        )
    )

    args = parser.parse_args()

    # Enforce mutual exclusivity
    modes = [m for m in ('config', 'resume', 'plot') if getattr(args, m)]
    if len(modes) > 1:
        parser.error(
            f'--{modes[0]} and --{modes[1]} are mutually exclusive. '
            'Specify exactly one mode.'
        )
    if not modes:
        parser.error('One of --config, --resume, or --plot is required.')

    # ── Plot-only mode ──────────────────────────────────────────────────────
    if args.plot:
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s  %(levelname)-8s  %(message)s',
            datefmt='%H:%M:%S',
        )
        plot_dir = Path(args.plot)
        if not plot_dir.exists():
            print(f'ERROR: Directory not found: {plot_dir}')
            sys.exit(1)
        # Deferred import keeps matplotlib's Agg backend out of download runs
        from plotting import run_plotting
        print('=' * 80)
        print(' QuakeScope Pick Plotter')
        print('=' * 80)
        print(f'\nOutput directory : {plot_dir}')
        run_plotting(str(plot_dir))
        return

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
            output_format=config.get('output_format', 'csv'),
            dedup_time_threshold=config.get('dedup_time_threshold', 0.5),
            channel_priority=config.get('channel_priority'),
            use_pyocto_projection=config.get('use_pyocto_projection', True),
            download_chunk_size=config.get('download_chunk_size', 'month'),
            workers=config.get('workers', 0),
            resume=True,
        )
        if result is None:
            sys.exit(1)
        return

    # ── Config (fresh run) mode ────────────────────────────────────────────
    try:
        config = load_yaml_config(args.config)
        _validate_yaml_config(config)
    except (FileNotFoundError, ValueError) as e:
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
        output_dir=config.get('output_dir', './quakescope_output'),
        fdsn_client=config.get('fdsn_client', 'IRIS'),
        organize_by_day=config.get('organize_by_day', True),
        output_format=config.get('output_format', 'csv'),
        dedup_time_threshold=config.get('dedup_time_threshold', 0.5),
        channel_priority=config.get('channel_priority'),
        use_pyocto_projection=config.get('use_pyocto_projection', True),
        download_chunk_size=config.get('download_chunk_size', 'month'),
        workers=config.get('workers', 0),
        resume=False,
    )

    if result is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
