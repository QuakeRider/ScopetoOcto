#!/usr/bin/env python3
"""
QuakeScope to PyOcto Picks Downloader

Downloads phase picks from QuakeScope database and formats them for PyOcto association.
Organizes picks by station and day for efficient processing.

Author: Grant
Date: 2025-01-29
"""

import json
import pandas as pd
import numpy as np
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import requests
from io import StringIO
import time


class QuakeScopePicksDownloader:
    """
    Download picks from QuakeScope and format for PyOcto association.
    
    Note: QuakeScope queries by station, not by spatial bounds. You must provide
    a list of station IDs (trace IDs) to query.
    """
    
    def __init__(self, output_dir: str = "./pyocto_picks"):
        """
        Initialize the downloader.
        
        Parameters:
        -----------
        output_dir : str
            Directory to store output pick files
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # QuakeScope API endpoints
        self.picks_url = "https://dasway.ess.washington.edu/quakescope/service/picks/query"
        self.picks_record_url = "https://dasway.ess.washington.edu/quakescope/service/picks_record/query"

        # Maximum picks per query (QuakeScope limit)
        self.max_limit = 10000

        # File tracking which station tids have been fully downloaded
        self._progress_file = self.output_dir / 'download_progress.json'

    def _load_completed_tids(self) -> set:
        """Return the set of station tids that have been fully downloaded.

        Reads ``download_progress.json`` from the output directory.  Returns an
        empty set if the file does not exist or cannot be parsed.
        """
        if not self._progress_file.exists():
            return set()
        try:
            with open(self._progress_file) as f:
                data = json.load(f)
            return set(data.get('completed_tids', []))
        except Exception as e:
            print(f"WARNING: Could not read progress file {self._progress_file}: {e}")
            return set()

    def _mark_tid_complete(self, tid: str) -> None:
        """Record *tid* as fully downloaded in ``download_progress.json``."""
        completed = self._load_completed_tids()
        completed.add(tid)
        data = {
            'completed_tids': sorted(completed),
            'last_updated': datetime.utcnow().isoformat(),
        }
        try:
            with open(self._progress_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"WARNING: Could not write progress file {self._progress_file}: {e}")

    def create_day_directories(self, start_time: str, end_time: str) -> List[Path]:
        """
        Pre-create one subdirectory per calendar day for the full timeframe.

        Directories are created immediately so they exist before any downloads
        begin.  Days that end up having no picks will simply remain empty.

        Parameters:
        -----------
        start_time : str
            Start of the timeframe (ISO format: YYYY-MM-DDTHH:MM:SS)
        end_time : str
            End of the timeframe (ISO format: YYYY-MM-DDTHH:MM:SS)

        Returns:
        --------
        list of Path
            Sorted list of day directory paths that were created.
        """
        from datetime import datetime, timedelta

        def _parse_naive_utc(s):
            return datetime.fromisoformat(s.replace('Z', '+00:00')).replace(tzinfo=None)

        start_dt = _parse_naive_utc(start_time)
        end_dt = _parse_naive_utc(end_time)

        day_dirs = []
        current = start_dt.date()
        end_date = end_dt.date()

        while current <= end_date:
            day_dir = self.output_dir / str(current)
            day_dir.mkdir(parents=True, exist_ok=True)
            day_dirs.append(day_dir)
            current += timedelta(days=1)

        print(f"Created {len(day_dirs)} day director{'y' if len(day_dirs) == 1 else 'ies'} "
              f"under {self.output_dir}")
        return day_dirs
    
    def get_active_days_for_station(self,
                                    tid: str,
                                    start_dt: datetime,
                                    end_dt: datetime,
                                    channel: Optional[str] = None) -> List[date]:
        """
        Query the picks_record API to discover which days have picks for a station.

        The picks_record endpoint returns a lightweight daily summary of pick
        counts per station — one row per day that has picks — so it is far
        cheaper than querying the picks endpoint directly.  Every date that
        appears in the response has at least one pick and will be queried in
        detail by download_picks_for_station; days absent from the response are
        skipped entirely.

        Parameters:
        -----------
        tid : str
            Trace ID (format: "NET.STA.LOC" or "NET.STA.")
        start_dt : datetime
            Start of the timeframe (naive UTC)
        end_dt : datetime
            End of the timeframe (naive UTC)
        channel : str, optional
            Two-letter channel prefix (e.g. 'HH', 'EH').  When provided the
            picks_record query is filtered to that channel so only days with
            picks on the requested channel are returned.

        Returns:
        --------
        list of datetime.date
            Sorted list of dates that have at least one pick.
            Returns an empty list if no picks exist or the request fails.
        """
        params = {
            'tid': tid,
            'start_time': start_dt.strftime('%Y-%m-%d'),
            'end_time': end_dt.strftime('%Y-%m-%d'),
        }
        if channel:
            params['channel'] = channel

        try:
            response = requests.get(self.picks_record_url, params=params, timeout=30)
            response.raise_for_status()

            if not response.text.strip():
                return []

            df = pd.read_csv(StringIO(response.text), delimiter='|')
            if df.empty:
                return []

            # Identify the date column — picks_record returns one row per day.
            # Try an exact match first, then a case-insensitive substring search,
            # then fall back to trying to parse each column as dates.
            date_col = None
            col_names_lower = [c.lower().strip() for c in df.columns]

            for candidate in ('date', 'day', 'start_time', 'time'):
                if candidate in col_names_lower:
                    date_col = df.columns[col_names_lower.index(candidate)]
                    break

            if date_col is None:
                for col in df.columns:
                    if 'date' in col.lower() or 'time' in col.lower():
                        date_col = col
                        break

            if date_col is None:
                # Last resort: find the first column that parses as dates
                for col in df.columns:
                    parsed = pd.to_datetime(df[col], errors='coerce')
                    if parsed.notna().all():
                        date_col = col
                        break

            if date_col is None:
                print(f"  WARNING: Could not identify a date column in picks_record "
                      f"response for {tid}. Columns: {df.columns.tolist()}")
                return []

            dates = sorted(pd.to_datetime(df[date_col]).dt.date.dropna().unique())
            return dates

        except requests.RequestException as e:
            print(f"  WARNING: Could not fetch picks_record for {tid}: {e}. "
                  f"Falling back to querying all days.")
        except pd.errors.EmptyDataError:
            pass

        return []

    def download_picks_for_station(self,
                                   tid: str,
                                   start_time: str,
                                   end_time: str,
                                   station_start: Optional[str] = None,
                                   station_end: Optional[str] = None,
                                   phases: Optional[List[str]] = None,
                                   min_score: float = 0.0,
                                   max_score: float = 1.0,
                                   channel: Optional[str] = None,
                                   write_immediately: bool = False) -> pd.DataFrame:
        """
        Download picks for a single station from QuakeScope.

        First queries the picks_record endpoint for the full timeframe to
        discover which days have picks (see get_active_days_for_station).  Only
        those days are then queried against the picks endpoint, one day at a
        time, to stay within the 10,000-pick limit.  Days with no picks and
        days outside the station's operational window are skipped entirely.
        
        Parameters:
        -----------
        tid : str
            Trace ID (format: "NET.STA.LOC" or "NET.STA.")
        start_time : str
            Start time (ISO format: YYYY-MM-DDTHH:MM:SS)
        end_time : str
            End time (ISO format: YYYY-MM-DDTHH:MM:SS)
        station_start : str, optional
            Station operational start date (ISO format). If None, uses start_time.
        station_end : str, optional
            Station operational end date (ISO format). If None, uses end_time.
        phases : list of str, optional
            Phase types to download (e.g., ['P', 'S']). If None, gets all phases.
        min_score, max_score : float
            Score/probability range (0.0 to 1.0)
        channel : str, optional
            Channel code filter (e.g., 'EH', 'HH', 'BH')
        write_immediately : bool, optional
            If True, format and write each day's picks to a CSV file as soon as
            that day's data is successfully downloaded, rather than holding all
            picks in memory until the end.  Written paths are appended to
            self._immediate_files (if that attribute exists).

        Returns:
        --------
        pd.DataFrame
            Raw picks data from QuakeScope
        """
        from datetime import datetime, timedelta

        def _parse_naive_utc(s):
            """Parse an ISO datetime string to a naive UTC datetime.

            Handles both timezone-aware strings (e.g. ending in 'Z' or '+00:00',
            as produced by ObsPy UTCDateTime) and naive strings (e.g. the CLI
            args '2002-01-01T00:00:00').  All values are treated as UTC, so
            tzinfo is stripped after parsing to allow consistent comparisons.
            """
            return datetime.fromisoformat(s.replace('Z', '+00:00')).replace(tzinfo=None)

        # Parse start and end times
        start_dt = _parse_naive_utc(start_time)
        end_dt = _parse_naive_utc(end_time)

        # Constrain to station operational period
        if station_start:
            station_start_dt = _parse_naive_utc(station_start)
            start_dt = max(start_dt, station_start_dt)

        if station_end:
            station_end_dt = _parse_naive_utc(station_end)
            end_dt = min(end_dt, station_end_dt)
        
        # If station wasn't active during requested period, return empty
        if start_dt >= end_dt:
            return pd.DataFrame()
        
        # Discover which days actually have picks before issuing per-day requests.
        # A single full-range overview request is made (no phase/channel/score filters)
        # so we can skip days that are empty and avoid unnecessary API calls.
        n_total_days = max((end_dt - start_dt).days, 1)
        active_days = self.get_active_days_for_station(tid, start_dt, end_dt, channel=channel)
        time.sleep(0.05)  # Be nice to the server after the overview request

        if not active_days:
            return pd.DataFrame()

        print(f"  Active days with picks: {len(active_days)}/{n_total_days} "
              f"(skipping {n_total_days - len(active_days)} empty day(s))")

        all_picks = []

        # Only iterate over days that are known to have picks
        for active_date in active_days:
            day_start = datetime(active_date.year, active_date.month, active_date.day)
            day_end = day_start + timedelta(days=1)

            # Clamp to the requested range (handles partial first/last days)
            chunk_start_dt = max(day_start, start_dt)
            chunk_end_dt = min(day_end, end_dt)

            chunk_start = chunk_start_dt.strftime('%Y-%m-%dT%H:%M:%S')
            chunk_end = chunk_end_dt.strftime('%Y-%m-%dT%H:%M:%S')

            # If phases specified, query each phase separately
            phase_list = phases if phases else [None]

            day_picks = []  # Raw picks collected for this single day (all phases)

            for phase in phase_list:
                params = {
                    'tid': tid,
                    'start_time': chunk_start,
                    'end_time': chunk_end,
                    'min_score': min_score,
                    'max_score': max_score,
                    'limit': self.max_limit
                }

                if phase:
                    params['phase'] = phase
                if channel:
                    params['channel'] = channel

                try:
                    response = requests.get(self.picks_url, params=params, timeout=30)
                    response.raise_for_status()

                    # Parse pipe-delimited CSV
                    if response.text.strip():
                        df = pd.read_csv(StringIO(response.text), delimiter='|')
                        if not df.empty:
                            all_picks.append(df)
                            day_picks.append(df)

                        # Warn if we hit the limit even with daily chunks
                        if len(df) >= self.max_limit:
                            print(f"  WARNING: Hit limit ({self.max_limit}) for {tid} on {active_date}, phase={phase}")

                except requests.RequestException as e:
                    print(f"  ERROR downloading {tid} on {active_date}, phase={phase}: {e}")
                except pd.errors.EmptyDataError:
                    # No data for this query
                    pass

                # Be nice to the server
                time.sleep(0.05)

            # Write this day's picks to disk immediately after all phases are done
            if write_immediately and day_picks:
                day_df = pd.concat(day_picks, ignore_index=True)
                formatted_day = self.format_for_pyocto(day_df)
                if not formatted_day.empty:
                    # Derive a clean station name from the picks data itself
                    station_name = formatted_day['station'].iloc[0]
                    station_clean = station_name.replace('.', '_').replace(' ', '_')
                    # Place the file inside the pre-created date subdirectory
                    day_dir = self.output_dir / str(active_date)
                    day_dir.mkdir(parents=True, exist_ok=True)
                    filepath = day_dir / f"{station_clean}_picks.csv"
                    # If a file already exists for this station-day (e.g. from a
                    # previous partial run), append and re-sort rather than clobber
                    if filepath.exists():
                        existing = pd.read_csv(filepath)
                        formatted_day = pd.concat(
                            [existing, formatted_day], ignore_index=True
                        ).sort_values('time').reset_index(drop=True)
                    formatted_day.to_csv(filepath, index=False)
                    print(f"  Wrote {len(formatted_day)} picks → {active_date}/{filepath.name}")
                    if hasattr(self, '_immediate_files'):
                        self._immediate_files.append(filepath)

        if all_picks:
            return pd.concat(all_picks, ignore_index=True)
        else:
            return pd.DataFrame()
    
    def download_picks_bulk(self,
                           stations_df: pd.DataFrame,
                           start_time: str,
                           end_time: str,
                           phases: Optional[List[str]] = None,
                           min_score: float = 0.0,
                           max_score: float = 1.0,
                           channel: Optional[str] = None,
                           write_immediately: bool = False,
                           skip_completed: bool = False) -> pd.DataFrame:
        """
        Download picks for multiple stations.

        For each station the picks_record endpoint is queried first for the full
        timeframe to get a lightweight daily summary of pick counts.  Only the
        days that appear in that summary (i.e. days that have picks) are then
        queried in detail against the picks endpoint, one day at a time.  Days
        with no picks and days outside each station's operational window are
        skipped entirely, minimising unnecessary API calls.
        
        Parameters:
        -----------
        stations_df : pd.DataFrame
            Station metadata with columns: tid, start_date, end_date
        start_time : str
            Start time (ISO format: YYYY-MM-DDTHH:MM:SS)
        end_time : str
            End time (ISO format: YYYY-MM-DDTHH:MM:SS)
        phases : list of str, optional
            Phase types to download (e.g., ['P', 'S'])
        min_score, max_score : float
            Score/probability range
        channel : str, optional
            Channel code filter
        write_immediately : bool, optional
            Passed through to download_picks_for_station().  When True, each
            day's picks are written to a CSV file immediately after download.
        skip_completed : bool, optional
            When True, load the progress file and skip any station tid that
            has already been fully downloaded in a previous run.  Progress is
            saved incrementally so a fresh interruption only loses the
            partially-downloaded station currently in flight.

        Returns:
        --------
        pd.DataFrame
            Combined picks from all stations
        """
        from datetime import datetime

        # Calculate time range
        start_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
        end_dt = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
        n_days = (end_dt - start_dt).days + 1

        # Resume support: load the set of already-completed station tids.
        completed_tids: set = self._load_completed_tids() if skip_completed else set()
        n_completed = len(completed_tids)
        n_remaining = len(stations_df) - sum(
            1 for _, row in stations_df.iterrows() if row['tid'] in completed_tids
        )

        print(f"Downloading picks for {len(stations_df)} stations...")
        if completed_tids:
            print(f"Resuming: {n_completed} station(s) already complete, "
                  f"{n_remaining} remaining")
        print(f"Requested time range: {start_time} to {end_time} ({n_days} days)")
        print(f"Phases: {phases if phases else 'all'}")
        print(f"Score range: {min_score} to {max_score}")
        print(f"NOTE: picks_record is queried per station to identify days with picks; "
              f"only those days are then fetched from the picks endpoint")

        all_picks = []

        for i, (_, row) in enumerate(stations_df.iterrows(), 1):
            tid = row['tid']
            sta_start = row.get('start_date')
            sta_end = row.get('end_date')

            # Skip stations that were fully downloaded in a previous run.
            if tid in completed_tids:
                print(f"\n[{i}/{len(stations_df)}] {tid} - SKIPPED (already downloaded)")
                continue
            
            # Show operational period - convert UTCDateTime to string if needed
            if pd.notna(sta_start) and pd.notna(sta_end):
                # Convert to string if it's a UTCDateTime object
                start_str = str(sta_start)[:10] if hasattr(sta_start, 'strftime') else str(sta_start)[:10]
                end_str = str(sta_end)[:10] if hasattr(sta_end, 'strftime') else str(sta_end)[:10]
                print(f"\n[{i}/{len(stations_df)}] {tid} (active: {start_str} to {end_str})")
            else:
                print(f"\n[{i}/{len(stations_df)}] {tid}")
            
            picks = self.download_picks_for_station(
                tid=tid,
                start_time=start_time,
                end_time=end_time,
                station_start=str(sta_start) if pd.notna(sta_start) else None,
                station_end=str(sta_end) if pd.notna(sta_end) else None,
                phases=phases,
                min_score=min_score,
                max_score=max_score,
                channel=channel,
                write_immediately=write_immediately
            )
            if not picks.empty:
                all_picks.append(picks)
                print(f"  Downloaded {len(picks)} picks")
            else:
                print(f"  No picks found")

            # Record this tid as complete so a future --resume can skip it.
            self._mark_tid_complete(tid)
        
        if all_picks:
            combined = pd.concat(all_picks, ignore_index=True)
            print(f"\n{'='*60}")
            print(f"Downloaded {len(combined)} total picks")
            print(f"{'='*60}")
            return combined
        else:
            print("\nNo picks found")
            return pd.DataFrame()
    
    def format_for_pyocto(self, picks_df: pd.DataFrame) -> pd.DataFrame:
        """
        Convert QuakeScope picks to PyOcto format.
        
        PyOcto requires:
        - station: station identifier
        - time: Unix timestamp (float, seconds since epoch)
        - phase: 'P' or 'S' (uppercase)
        - probability: confidence score (0-1)
        
        Additional columns kept:
        - amplitude: pick amplitude (not used by PyOcto but useful for analysis)
        
        QuakeScope columns:
        - trace_id, network_code, station_code, location_code, channel
        - start_time, peak_time, end_time
        - confidence, amplitude, phase

        Parameters:
        -----------
        picks_df : pd.DataFrame
            Raw picks from QuakeScope

        Returns:
        --------
        pd.DataFrame
            Picks in PyOcto format with columns:
            station, time, phase, probability, amplitude, channel, location_code
            - station: full trace ID (NET.STA.LOC) kept here; normalized to
              NET.STA. after cross-location deduplication.
            - channel: 2-letter channel prefix (e.g. 'HH', 'BH') for dedup.
            - location_code: original location code (e.g. '00', '60') for dedup.
        """
        if picks_df.empty:
            return pd.DataFrame(columns=[
                'station', 'time', 'phase', 'probability', 'amplitude',
                'channel', 'location_code',
            ])

        # Derive 2-letter channel prefix; fall back gracefully if column absent.
        if 'channel' in picks_df.columns:
            channel_prefix = picks_df['channel'].astype(str).str[:2]
        else:
            channel_prefix = pd.Series([''] * len(picks_df), index=picks_df.index)

        loc_col = picks_df['location_code'] if 'location_code' in picks_df.columns \
            else pd.Series([''] * len(picks_df), index=picks_df.index)

        pyocto_picks = pd.DataFrame({
            'station': picks_df['trace_id'],
            'time': pd.to_datetime(picks_df['peak_time']).astype(np.int64) / 1e9,
            'phase': picks_df['phase'].str.upper(),
            'probability': picks_df['confidence'],
            'amplitude': picks_df['amplitude'],
            'channel': channel_prefix,
            'location_code': loc_col.fillna(''),
        })

        pyocto_picks = pyocto_picks.dropna(subset=['station', 'time', 'phase', 'probability'])
        pyocto_picks = pyocto_picks.sort_values('time').reset_index(drop=True)

        return pyocto_picks
    
    # Default channel priority: lower index = higher preference.
    # Covers every channel type present in the QuakeScope database.
    DEFAULT_CHANNEL_PRIORITY: List[str] = [
        'HH', 'BH', 'EH', 'SH', 'HN', 'CN', 'EL', 'SL', 'EP', 'DP',
    ]

    def deduplicate_picks_cross_location(
        self,
        picks_df: pd.DataFrame,
        time_threshold: float = 0.5,
        channel_priority: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        """
        Merge picks from multiple location codes at the same physical station
        and remove duplicates using a channel-type hierarchy.

        Algorithm
        ---------
        For each phase type separately:
          1. Sort picks by time.
          2. Assign a sequential group ID that increments whenever the gap
             between consecutive picks exceeds *time_threshold* seconds.
             (Picks within the same group arrived too close together to be
             from distinct events, so only one should be kept.)
          3. Within each group keep the pick with the highest channel priority.
             Picks that form a group of one (no near neighbour on any other
             location code) are always kept regardless of priority.
        Finally normalise the ``station`` field to ``NET.STA.`` (empty
        location code) so the merged file matches the single PyOcto station
        entry for the physical site.

        Parameters
        ----------
        picks_df : pd.DataFrame
            Formatted picks (from format_for_pyocto) for *one physical station*
            covering potentially several location codes.  Must contain columns:
            station, time, phase, probability, amplitude, channel, location_code.
        time_threshold : float
            Maximum separation in seconds between two picks that are considered
            duplicates of the same arrival.  Default 0.5 s.
        channel_priority : list of str, optional
            Ordered list of 2-letter channel prefixes, best first.
            Defaults to DEFAULT_CHANNEL_PRIORITY.

        Returns
        -------
        pd.DataFrame
            Deduplicated picks with ``station`` normalised to ``NET.STA.``.
        """
        if picks_df.empty:
            return picks_df.copy()

        if channel_priority is None:
            channel_priority = self.DEFAULT_CHANNEL_PRIORITY

        priority_map = {ch: i for i, ch in enumerate(channel_priority)}
        n_fallback = len(channel_priority)  # rank for unknown channel types

        picks_df = picks_df.copy()
        picks_df['_priority'] = (
            picks_df['channel']
            .map(priority_map)
            .fillna(n_fallback)
            .astype(int)
        )

        result_pieces = []

        for phase, phase_picks in picks_df.groupby('phase'):
            phase_picks = phase_picks.sort_values('time').reset_index(drop=True)
            times = phase_picks['time'].to_numpy()

            # Assign group IDs: new group whenever gap > threshold.
            group_ids = np.zeros(len(times), dtype=int)
            for i in range(1, len(times)):
                group_ids[i] = group_ids[i - 1] + (
                    1 if (times[i] - times[i - 1]) > time_threshold else 0
                )
            phase_picks['_group'] = group_ids

            # Within each group keep the pick with the lowest priority number
            # (= highest channel quality).
            kept_idx = phase_picks.groupby('_group')['_priority'].idxmin()
            result_pieces.append(phase_picks.loc[kept_idx])

        if not result_pieces:
            return pd.DataFrame(columns=picks_df.columns)

        deduped = pd.concat(result_pieces, ignore_index=True)
        deduped = deduped.drop(columns=['_priority', '_group'])

        # Normalise station to NET.STA. (physical station id, empty location).
        # The location code and channel are preserved in their own columns.
        deduped['station'] = deduped['station'].apply(
            lambda s: '.'.join(s.split('.')[:2]) + '.'
        )

        return deduped.sort_values('time').reset_index(drop=True)

    def merge_and_deduplicate_station_day_files(
        self,
        stations_df: pd.DataFrame,
        time_threshold: float = 0.5,
        channel_priority: Optional[List[str]] = None,
    ) -> int:
        """
        Post-processing pass: merge per-location-code pick files into one
        deduplicated file per physical station per day, then remove the
        per-location source files.

        This is called automatically by run() after all downloads finish.
        For each calendar-day directory under self.output_dir the method:
          1. Groups pick files that belong to the same physical station
             (identified by ``physical_station`` in *stations_df*).
          2. Loads, merges, and deduplicates them with
             deduplicate_picks_cross_location().
          3. Writes a single ``NET_STA__picks.csv`` (double underscore =
             empty location code) per physical station per day.
          4. Deletes the per-location-code source files.

        Parameters
        ----------
        stations_df : pd.DataFrame
            Station metadata (output of get_stations_with_metadata).
            Must contain ``tid`` and ``physical_station`` columns.
        time_threshold : float
            Passed to deduplicate_picks_cross_location().
        channel_priority : list of str, optional
            Passed to deduplicate_picks_cross_location().

        Returns
        -------
        int
            Number of merged station-day files written.
        """
        if 'physical_station' not in stations_df.columns:
            print("WARNING: stations_df missing 'physical_station' column; "
                  "skipping merge/dedup pass.")
            return 0

        # Build a mapping from per-location filename stem → physical station id.
        # File names are like  IU_CCM_60_picks.csv  (tid with dots→underscores).
        tid_to_phys: dict = {}
        for _, row in stations_df.iterrows():
            tid_clean = row['tid'].replace('.', '_')
            tid_to_phys[tid_clean] = row['physical_station']

        merged_count = 0

        # Walk each day directory.
        for day_dir in sorted(self.output_dir.iterdir()):
            if not day_dir.is_dir():
                continue

            # Group pick files by physical station.
            phys_groups: dict = {}  # physical_station → list of Path
            for fpath in day_dir.glob('*_picks.csv'):
                stem = fpath.stem.replace('_picks', '')  # e.g. IU_CCM_60
                phys = tid_to_phys.get(stem)
                if phys is None:
                    # File may already be a merged file (IU_CCM_) — skip.
                    continue
                phys_groups.setdefault(phys, []).append(fpath)

            for phys_sta, fpaths in phys_groups.items():
                if not fpaths:
                    continue

                # Load and merge all location-code files for this station-day.
                dfs = []
                for fp in fpaths:
                    try:
                        df = pd.read_csv(fp)
                        if not df.empty:
                            dfs.append(df)
                    except Exception as e:
                        print(f"  WARNING: could not read {fp}: {e}")

                if not dfs:
                    for fp in fpaths:
                        fp.unlink(missing_ok=True)
                    continue

                merged = pd.concat(dfs, ignore_index=True)
                deduped = self.deduplicate_picks_cross_location(
                    merged, time_threshold=time_threshold,
                    channel_priority=channel_priority,
                )

                # Write merged file: NET_STA__picks.csv (double underscore =
                # empty location code, mirrors tid format NET.STA.)
                phys_clean = phys_sta.replace('.', '_')
                out_path = day_dir / f"{phys_clean}__picks.csv"

                n_src = len(merged)
                deduped.to_csv(out_path, index=False)

                print(f"  {day_dir.name}/{out_path.name}: "
                      f"{n_src} picks → {len(deduped)} after dedup "
                      f"({len(fpaths)} location file(s) merged)")

                # Delete per-location source files.  Skip the output file
                # itself in the (rare) case where a station with an empty
                # location code produced a source filename identical to the
                # merged output filename.
                for fp in fpaths:
                    if fp.resolve() != out_path.resolve():
                        fp.unlink(missing_ok=True)

                merged_count += 1

        print(f"\nMerge/dedup complete: {merged_count} station-day file(s) written.")
        return merged_count

    def organize_by_station_day(self, picks_df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        """
        Organize picks into separate DataFrames per station per day.
        
        Parameters:
        -----------
        picks_df : pd.DataFrame
            PyOcto-formatted picks
            
        Returns:
        --------
        dict
            Dictionary with keys like "STATION_YYYY-MM-DD" and DataFrames as values
        """
        if picks_df.empty:
            return {}
        
        # Add date column
        picks_df = picks_df.copy()
        picks_df['date'] = pd.to_datetime(picks_df['time'], unit='s').dt.date
        
        # Group by station and date
        organized = {}
        for (station, date), group in picks_df.groupby(['station', 'date']):
            # Clean station name for filename (replace dots with underscores)
            station_clean = station.replace('.', '_').replace(' ', '_')
            key = f"{station_clean}_{date}"
            # Drop the date column for PyOcto format
            organized[key] = group.drop('date', axis=1).reset_index(drop=True)
        
        return organized
    
    def save_picks(self, organized_picks: Dict[str, pd.DataFrame], 
                   format: str = 'csv') -> List[Path]:
        """
        Save organized picks to files.
        
        Parameters:
        -----------
        organized_picks : dict
            Dictionary of DataFrames organized by station/day
        format : str
            Output format ('csv', 'parquet', 'pickle')
            
        Returns:
        --------
        list of Path
            List of saved file paths
        """
        saved_files = []

        for key, df in organized_picks.items():
            # Key format: "{station_clean}_{YYYY-MM-DD}"
            # Extract the trailing date (always 10 chars: YYYY-MM-DD) to build
            # the date subdirectory, leaving the station name for the filename.
            date_str = key[-10:]
            station_part = key[:-11]  # strip "_{date}"
            day_dir = self.output_dir / date_str
            day_dir.mkdir(parents=True, exist_ok=True)

            if format == 'csv':
                filepath = day_dir / f"{station_part}_picks.csv"
                df.to_csv(filepath, index=False)
            elif format == 'parquet':
                filepath = day_dir / f"{station_part}_picks.parquet"
                df.to_parquet(filepath, index=False)
            elif format == 'pickle':
                filepath = day_dir / f"{station_part}_picks.pkl"
                df.to_pickle(filepath)
            else:
                raise ValueError(f"Unknown format: {format}")

            saved_files.append(filepath)
        
        print(f"\nSaved {len(saved_files)} pick files to {self.output_dir} (organised by date)")
        return saved_files
    
    def save_combined(self, picks_df: pd.DataFrame, filename: str = "all_picks.csv") -> Path:
        """
        Save all picks to a single file (alternative to station/day organization).
        
        Parameters:
        -----------
        picks_df : pd.DataFrame
            PyOcto-formatted picks
        filename : str
            Output filename
            
        Returns:
        --------
        Path
            Path to saved file
        """
        filepath = self.output_dir / filename
        picks_df.to_csv(filepath, index=False)
        print(f"Saved {len(picks_df)} picks to {filepath}")
        return filepath
    
    def run(self,
            stations_df: pd.DataFrame,
            start_time: str,
            end_time: str,
            phases: Optional[List[str]] = None,
            min_score: float = 0.0,
            max_score: float = 1.0,
            channel: Optional[str] = None,
            organize_by_day: bool = True,
            output_format: str = 'csv',
            dedup_time_threshold: float = 0.5,
            channel_priority: Optional[List[str]] = None,
            resume: bool = False) -> List[Path]:
        """
        Complete workflow: download, format, organize, deduplicate, and save picks.

        When *organize_by_day* is True (the default) the workflow is:
          1. Download picks per station/location-code tid, writing one
             ``NET_STA_LOC_picks.csv`` file per tid per day as each day
             arrives (no large in-memory accumulation).
          2. After all downloads finish, run a merge/dedup pass that groups
             the per-location files by physical station (NET.STA), deduplicates
             cross-location picks using *channel_priority* and
             *dedup_time_threshold*, and writes a single merged
             ``NET_STA__picks.csv`` per physical station per day.  The
             per-location source files are then removed so the output
             directory is clean.

        Parameters:
        -----------
        stations_df : pd.DataFrame
            Station metadata with columns: tid, start_date, end_date.
            If it also contains ``physical_station`` (produced by
            get_stations_with_metadata) the merge/dedup pass runs
            automatically; otherwise it is skipped.
        start_time : str
            Start time (ISO format)
        end_time : str
            End time (ISO format)
        phases : list of str, optional
            Phases to download
        min_score, max_score : float
            Score range
        channel : str, optional
            Channel filter
        organize_by_day : bool
            If True, organize into separate files per station/day and run
            the cross-location dedup pass.
            If False, save all picks in one combined file (no dedup).
        output_format : str
            Output format ('csv', 'parquet', 'pickle')
        dedup_time_threshold : float
            Time window in seconds for cross-location duplicate detection.
            Default 0.5 s.  Only used when organize_by_day=True and
            stations_df contains 'physical_station'.
        channel_priority : list of str, optional
            Ordered 2-letter channel prefixes, best first.  Defaults to
            DEFAULT_CHANNEL_PRIORITY.  Only used during dedup.
        resume : bool, optional
            When True, load ``download_progress.json`` from the output
            directory and skip any station tids that were already fully
            downloaded in a previous run.  Use this when restarting an
            interrupted download via ``--resume``.

        Returns:
        --------
        list of Path
            List of saved file paths (merged station-day files when dedup
            runs, otherwise per-tid files or a single combined file).
        """
        # Track files written during this run (populated by download_picks_for_station
        # when write_immediately=True)
        self._immediate_files = []

        # Pre-create one subdirectory per calendar day so the directory tree is
        # fully visible before any downloads begin.  Empty day directories (no
        # picks) are left in place; they cause no harm and are informative.
        self.create_day_directories(start_time, end_time)

        # Download — when organize_by_day is True each day's picks are written
        # to disk as soon as they arrive, so nothing is held in memory until the end
        raw_picks = self.download_picks_bulk(
            stations_df=stations_df,
            start_time=start_time,
            end_time=end_time,
            phases=phases,
            min_score=min_score,
            max_score=max_score,
            channel=channel,
            write_immediately=organize_by_day,
            skip_completed=resume,
        )

        if organize_by_day:
            # On resume, existing pick files may not be in _immediate_files
            # (they were written in a prior run).  Check disk for any files.
            existing_files = sorted(self.output_dir.rglob('*_picks.csv'))
            if not self._immediate_files and not existing_files:
                print("No picks found for given criteria")
                return []

            if self._immediate_files:
                print(f"Downloaded {len(self._immediate_files)} station-day file(s) "
                      f"across all location codes")

            # Cross-location dedup pass — runs only when stations_df has the
            # 'physical_station' column produced by get_stations_with_metadata.
            if 'physical_station' in stations_df.columns:
                print(f"\nRunning cross-location dedup "
                      f"(threshold={dedup_time_threshold}s)...")
                self.merge_and_deduplicate_station_day_files(
                    stations_df=stations_df,
                    time_threshold=dedup_time_threshold,
                    channel_priority=channel_priority,
                )
                # Return the merged output files that now exist on disk.
                merged_files = sorted(self.output_dir.rglob('*__picks.csv'))
                print(f"\nSaved {len(merged_files)} merged station-day file(s) "
                      f"to {self.output_dir}")
                return merged_files
            else:
                all_files = self._immediate_files or existing_files
                print(f"\nSaved {len(all_files)} pick file(s) to "
                      f"{self.output_dir}")
                return all_files

        # organize_by_day=False: save all picks in a single combined file
        if raw_picks.empty:
            print("No picks found for given criteria")
            return []

        pyocto_picks = self.format_for_pyocto(raw_picks)
        print(f"Formatted {len(pyocto_picks)} picks for PyOcto")
        saved_files = [self.save_combined(pyocto_picks,
                                          f"picks_{start_time[:10]}_{end_time[:10]}.csv")]
        return saved_files


def get_pnsn_stations_example() -> List[str]:
    """
    Example function to get PNSN (Pacific Northwest Seismic Network) station list.
    
    This is just an example - you would need to implement actual station lookup
    based on lat/lon bounds, possibly using ObsPy or FDSN web services.
    
    Returns:
    --------
    list of str
        Example station trace IDs
    """
    # Example PNSN stations (UW network)
    return [
        "UW.SHW.",
        "UW.RATT.",
        "UW.FISH.",
        "UW.TDH.",
        "UW.STAR.",
    ]


# Example usage
if __name__ == "__main__":
    
    # Initialize downloader
    downloader = QuakeScopePicksDownloader(output_dir="./quakescope_picks")
    
    # Example: Create a simple station DataFrame
    # In practice, this would come from get_stations_with_metadata()
    import pandas as pd
    example_stations = pd.DataFrame({
        'tid': ['UW.SHW.', 'UW.RATT.', 'UW.FISH.'],
        'start_date': ['2020-01-01T00:00:00', '2020-01-01T00:00:00', '2020-06-01T00:00:00'],
        'end_date': ['2024-12-31T23:59:59', '2024-12-31T23:59:59', '2024-12-31T23:59:59']
    })
    
    # Run the download and formatting
    files = downloader.run(
        stations_df=example_stations,
        start_time="2024-01-01T00:00:00",
        end_time="2024-01-02T00:00:00",
        phases=['P', 'S'],  # Only P and S phases
        min_score=0.3,      # Minimum pick score
        max_score=1.0,      # Maximum pick score
        channel='HH',       # HH channels only (optional)
        organize_by_day=True,  # Separate files per station/day
        output_format='csv'    # CSV output
    )
    
    print(f"\n{'='*60}")
    print(f"Download complete!")
    print(f"Saved {len(files)} files")
    print(f"{'='*60}")
    
    # Example of how to load these picks for PyOcto
    if files:
        print("\nExample: Loading picks for PyOcto:")
        print("=" * 60)
        
        # Load all pick files
        all_picks = []
        for f in files[:3]:  # Just show first 3 files
            df = pd.read_csv(f)
            all_picks.append(df)
            print(f"\nLoaded: {f.name}")
            print(f"  Picks: {len(df)}")
        
        if all_picks:
            combined_picks = pd.concat(all_picks, ignore_index=True)
            print(f"\nTotal picks loaded: {len(combined_picks)}")
            print("\nPick DataFrame columns:", combined_picks.columns.tolist())
            print("\nFirst few picks:")
            print(combined_picks.head(10))
            print("\nPick statistics:")
            print(f"  Phases: {combined_picks['phase'].value_counts().to_dict()}")
            print(f"  Probability range: {combined_picks['probability'].min():.3f} to {combined_picks['probability'].max():.3f}")
            print(f"  Unique stations: {combined_picks['station'].nunique()}")
        
        print("\n" + "="*60)
        print("These picks are now ready for PyOcto association!")
        print("="*60)
