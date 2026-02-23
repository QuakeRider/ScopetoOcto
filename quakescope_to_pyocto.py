#!/usr/bin/env python3
"""
QuakeScope to PyOcto Picks Downloader

Downloads phase picks from QuakeScope database and formats them for PyOcto association.
Organizes picks by station and day for efficient processing.

Author: Grant
Date: 2025-01-29
"""

import json
import os
import pandas as pd
import numpy as np
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import requests
from io import StringIO
import time


# ---------------------------------------------------------------------------
# Module-level helpers — must be top-level so ProcessPoolExecutor can pickle them
# ---------------------------------------------------------------------------

def _dedup_picks(
    picks_df: pd.DataFrame,
    time_threshold: float,
    channel_priority: List[str],
) -> pd.DataFrame:
    """Core cross-location deduplication logic (picklable, no class dependency).

    Called by both QuakeScopePicksDownloader.deduplicate_picks_cross_location
    and the parallel worker _dedup_day_dir.
    """
    if picks_df.empty:
        return picks_df.copy()

    priority_map = {ch: i for i, ch in enumerate(channel_priority)}
    n_fallback = len(channel_priority)

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

        group_ids = np.zeros(len(times), dtype=int)
        for i in range(1, len(times)):
            group_ids[i] = group_ids[i - 1] + (
                1 if (times[i] - times[i - 1]) > time_threshold else 0
            )
        phase_picks['_group'] = group_ids

        kept_idx = phase_picks.groupby('_group')['_priority'].idxmin()
        result_pieces.append(phase_picks.loc[kept_idx])

    if not result_pieces:
        return pd.DataFrame(columns=picks_df.columns)

    deduped = pd.concat(result_pieces, ignore_index=True)
    deduped = deduped.drop(columns=['_priority', '_group'])

    deduped['station'] = deduped['station'].apply(
        lambda s: '.'.join(s.split('.')[:2]) + '.'
    )
    return deduped.sort_values('time').reset_index(drop=True)


def _dedup_day_dir(
    day_dir: Path,
    tid_to_phys: dict,
    time_threshold: float,
    channel_priority: List[str],
) -> tuple:
    """Process one calendar-day directory in a worker process.

    Merges per-location pick files for each physical station, deduplicates
    them, writes a single merged output file, and deletes the source files.

    Returns
    -------
    tuple[int, list[str]]
        (number of station-day files written, list of log/warning strings)
    """
    merged_count = 0
    messages = []

    phys_groups: dict = {}
    for fpath in day_dir.glob('*_picks.csv'):
        stem = fpath.stem.replace('_picks', '')
        phys = tid_to_phys.get(stem)
        if phys is None:
            continue
        phys_groups.setdefault(phys, []).append(fpath)

    for phys_sta, fpaths in phys_groups.items():
        if not fpaths:
            continue

        dfs = []
        for fp in fpaths:
            try:
                df = pd.read_csv(fp)
                if not df.empty:
                    dfs.append(df)
            except Exception as e:
                messages.append(f"  WARNING: could not read {fp}: {e}")

        if not dfs:
            for fp in fpaths:
                fp.unlink(missing_ok=True)
            continue

        merged = pd.concat(dfs, ignore_index=True)
        deduped = _dedup_picks(merged, time_threshold=time_threshold,
                               channel_priority=channel_priority)

        phys_clean = phys_sta.replace('.', '_')
        out_path = day_dir / f"{phys_clean}__picks.csv"

        n_src = len(merged)
        deduped.to_csv(out_path, index=False)

        messages.append(
            f"  {day_dir.parent.parent.name}/{day_dir.parent.name}/{day_dir.name}/{out_path.name}: "
            f"{n_src} picks → {len(deduped)} after dedup "
            f"({len(fpaths)} location file(s) merged)"
        )

        for fp in fpaths:
            if fp.resolve() != out_path.resolve():
                fp.unlink(missing_ok=True)

        merged_count += 1

    return merged_count, messages


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
            day_dir = self.output_dir / str(current.year) / f"{current.month:02d}" / f"{current.day:02d}"
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

    def _group_days_into_chunks(
        self,
        active_days: List[date],
        chunk_size: str,
    ) -> List[Tuple[datetime, datetime, List[date]]]:
        """Group a list of active days into time chunks for bulk downloading.

        Parameters
        ----------
        active_days : list of datetime.date
            Sorted list of days known to have picks.
        chunk_size : str
            ``'day'``, ``'month'``, or ``'year'``.

        Returns
        -------
        list of (chunk_start, chunk_end, days_in_chunk)
            *chunk_start* and *chunk_end* are naive UTC datetimes that span the
            entire calendar period (e.g. the full month), **before** clamping to
            the user-requested range.  *days_in_chunk* lists the active days that
            fall inside the period and is used for recursive fallback.
        """
        if not active_days:
            return []

        if chunk_size == 'day':
            result = []
            for d in active_days:
                start = datetime(d.year, d.month, d.day)
                end = start + timedelta(days=1)
                result.append((start, end, [d]))
            return result

        if chunk_size == 'month':
            groups: Dict[Tuple[int, int], List[date]] = {}
            for d in active_days:
                key = (d.year, d.month)
                groups.setdefault(key, []).append(d)
            result = []
            for (year, month) in sorted(groups):
                chunk_start = datetime(year, month, 1)
                if month == 12:
                    chunk_end = datetime(year + 1, 1, 1)
                else:
                    chunk_end = datetime(year, month + 1, 1)
                result.append((chunk_start, chunk_end, groups[(year, month)]))
            return result

        if chunk_size == 'year':
            groups_y: Dict[int, List[date]] = {}
            for d in active_days:
                groups_y.setdefault(d.year, []).append(d)
            result = []
            for year in sorted(groups_y):
                chunk_start = datetime(year, 1, 1)
                chunk_end = datetime(year + 1, 1, 1)
                result.append((chunk_start, chunk_end, groups_y[year]))
            return result

        raise ValueError(
            f"Invalid chunk_size '{chunk_size}'. Must be 'day', 'month', or 'year'."
        )

    def _download_phase_with_fallback(
        self,
        tid: str,
        chunk_start: datetime,
        chunk_end: datetime,
        chunk_size: str,
        active_days_in_chunk: List[date],
        phase: Optional[str],
        min_score: float,
        max_score: float,
        channel: Optional[str],
    ) -> pd.DataFrame:
        """Download picks for one phase over a time chunk, with automatic fallback.

        If the API returns exactly *max_limit* rows the response is truncated.
        The method warns and retries at the next finer granularity:
        ``year → month → day``.  At the ``day`` level it warns but returns the
        truncated data (consistent with the previous single-day behaviour).

        Parameters
        ----------
        tid : str
            Trace ID.
        chunk_start, chunk_end : datetime
            Already-clamped start and end of the window to query.
        chunk_size : str
            Current granularity (``'day'``, ``'month'``, or ``'year'``).
        active_days_in_chunk : list of date
            Active days within this chunk, used for recursive sub-chunking on
            fallback.
        phase : str or None
            Phase filter passed to the API.
        min_score, max_score : float
            Score filter.
        channel : str or None
            Channel filter.

        Returns
        -------
        pd.DataFrame
            Raw picks from the API (may be empty).
        """
        phase_label = f"phase={phase}" if phase else "all phases"
        chunk_label = f"{chunk_start.date()} → {chunk_end.date()}"

        params = {
            'tid': tid,
            'start_time': chunk_start.strftime('%Y-%m-%dT%H:%M:%S'),
            'end_time': chunk_end.strftime('%Y-%m-%dT%H:%M:%S'),
            'min_score': min_score,
            'max_score': max_score,
            'limit': self.max_limit,
        }
        if phase:
            params['phase'] = phase
        if channel:
            params['channel'] = channel

        try:
            response = requests.get(self.picks_url, params=params, timeout=30)
            response.raise_for_status()

            if not response.text.strip():
                return pd.DataFrame()

            df = pd.read_csv(StringIO(response.text), delimiter='|')
            if df.empty:
                return pd.DataFrame()

            if len(df) >= self.max_limit:
                # Response was truncated — try a finer granularity if possible.
                fallback_map = {'year': 'month', 'month': 'day'}
                if chunk_size in fallback_map:
                    next_size = fallback_map[chunk_size]
                    print(
                        f"  WARNING: Hit {self.max_limit}-pick limit for {tid} "
                        f"[{chunk_label}] ({phase_label}). "
                        f"Falling back from '{chunk_size}' to '{next_size}' chunks."
                    )
                    sub_chunks = self._group_days_into_chunks(
                        active_days_in_chunk, next_size
                    )
                    sub_dfs = []
                    for sub_start, sub_end, sub_days in sub_chunks:
                        sub_df = self._download_phase_with_fallback(
                            tid, sub_start, sub_end, next_size, sub_days,
                            phase, min_score, max_score, channel,
                        )
                        if not sub_df.empty:
                            sub_dfs.append(sub_df)
                        time.sleep(0.05)
                    return pd.concat(sub_dfs, ignore_index=True) if sub_dfs else pd.DataFrame()
                else:
                    # Already at 'day' — warn and return truncated data.
                    print(
                        f"  WARNING: Hit {self.max_limit}-pick limit at day granularity "
                        f"for {tid} on {chunk_start.date()} ({phase_label}). "
                        f"Data may be incomplete. Consider tightening min_score."
                    )

            return df

        except requests.RequestException as e:
            print(f"  ERROR downloading {tid} [{chunk_label}] ({phase_label}): {e}")
            return pd.DataFrame()
        except pd.errors.EmptyDataError:
            return pd.DataFrame()

    def _write_chunk_by_day(self, formatted_df: pd.DataFrame) -> None:
        """Write a formatted picks DataFrame to per-day files, split by date.

        Each calendar day found in *formatted_df* is written to
        ``output_dir/YYYY/MM/DD/{station_clean}_picks.csv``.  If a file
        already exists for a station-day it is **skipped** (not overwritten or
        appended to), so an interrupted run can be resumed safely by simply
        re-running — days already on disk are left untouched.
        """
        df = formatted_df.copy()
        df['_date'] = pd.to_datetime(df['time'], unit='s').dt.date

        for day_date, day_group in df.groupby('_date'):
            day_df = day_group.drop(columns=['_date']).reset_index(drop=True)
            station_name = day_df['station'].iloc[0]
            station_clean = station_name.replace('.', '_').replace(' ', '_')

            day_dir = self.output_dir / str(day_date.year) / f"{day_date.month:02d}" / f"{day_date.day:02d}"
            day_dir.mkdir(parents=True, exist_ok=True)
            filepath = day_dir / f"{station_clean}_picks.csv"

            if filepath.exists():
                # Already written in a previous (partial) run — skip.
                continue

            day_df.to_csv(filepath, index=False)
            print(f"  Wrote {len(day_df)} picks → {day_date.year}/{day_date.month:02d}/{day_date.day:02d}/{filepath.name}")
            if hasattr(self, '_immediate_files'):
                self._immediate_files.append(filepath)

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
                                   station_channels: Optional[List[str]] = None,
                                   write_immediately: bool = False,
                                   chunk_size: str = 'month') -> pd.DataFrame:
        """
        Download picks for a single station from QuakeScope.

        First queries the picks_record endpoint for the full timeframe to
        discover which days have picks (see get_active_days_for_station).
        Those active days are then grouped into time chunks according to
        *chunk_size* and each chunk is fetched in a single API call.  If a
        chunk response hits the 10 000-pick limit the method automatically
        retries it at the next finer granularity (year → month → day), logging
        a warning each time.

        When *write_immediately* is True each chunk's picks are formatted and
        written to ``output_dir/YYYY-MM-DD/`` as soon as they arrive; existing
        files for a station-day are skipped so interrupted runs resume safely.

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
        station_channels : list of str, optional
            All 2-letter channel prefixes known to exist at this station/location
            (e.g. ['HH', 'BH', 'EH']).  When provided and *channel* is None,
            picks_record is queried once per channel prefix and the results are
            unioned.  This ensures days are not missed when the QuakeScope
            picks_record endpoint returns fewer results without a channel filter
            — a common occurrence for stations whose sensor type changed over
            time (the old and new sensor eras are indexed under different channel
            prefixes).
        write_immediately : bool, optional
            If True, format and write each chunk's picks to per-day CSV files as
            soon as the chunk is downloaded.  Written paths are appended to
            self._immediate_files (if that attribute exists).
        chunk_size : str, optional
            Time window per API call: ``'day'``, ``'month'`` (default), or
            ``'year'``.  Larger chunks mean fewer API calls and faster downloads.
            Automatic fallback to finer granularity occurs if the 10 000-pick
            limit is hit.

        Returns:
        --------
        pd.DataFrame
            Raw picks data from QuakeScope
        """
        from datetime import datetime, timedelta

        def _parse_naive_utc(s):
            """Parse an ISO datetime string to a naive UTC datetime."""
            return datetime.fromisoformat(s.replace('Z', '+00:00')).replace(tzinfo=None)

        # Parse start and end times
        start_dt = _parse_naive_utc(start_time)
        end_dt = _parse_naive_utc(end_time)

        # Constrain to station operational period
        if station_start:
            start_dt = max(start_dt, _parse_naive_utc(station_start))
        if station_end:
            end_dt = min(end_dt, _parse_naive_utc(station_end))

        # If station wasn't active during requested period, return empty
        if start_dt >= end_dt:
            return pd.DataFrame()

        # Discover which days actually have picks.
        # When the station's channel types are known and no global channel filter
        # is set, query picks_record once per channel prefix and union the results.
        # This guards against picks_record returning an empty or incomplete day
        # list when called without a channel filter — which happens for stations
        # whose sensor type changed over time, because each sensor era's picks are
        # indexed under a different channel prefix in the QuakeScope database.
        n_total_days = max((end_dt - start_dt).days, 1)
        if station_channels and channel is None:
            all_active_days: set = set()
            for ch in station_channels:
                days = self.get_active_days_for_station(tid, start_dt, end_dt, channel=ch)
                all_active_days.update(days)
                time.sleep(0.05)
            active_days = sorted(all_active_days)
        else:
            active_days = self.get_active_days_for_station(tid, start_dt, end_dt, channel=channel)
            time.sleep(0.05)

        if not active_days:
            return pd.DataFrame()

        print(f"  Active days with picks: {len(active_days)}/{n_total_days} "
              f"(skipping {n_total_days - len(active_days)} empty day(s))")

        # Group active days into download chunks
        chunks = self._group_days_into_chunks(active_days, chunk_size)
        if chunk_size != 'day':
            print(f"  Downloading in {len(chunks)} {chunk_size} chunk(s) "
                  f"(chunk_size='{chunk_size}')")

        phase_list = phases if phases else [None]
        all_picks = []

        for chunk_start, chunk_end, chunk_active_days in chunks:
            # Clamp chunk boundaries to the user-requested range
            clamped_start = max(chunk_start, start_dt)
            clamped_end = min(chunk_end, end_dt)

            chunk_picks_raw = []

            for phase in phase_list:
                df = self._download_phase_with_fallback(
                    tid, clamped_start, clamped_end, chunk_size,
                    chunk_active_days, phase, min_score, max_score, channel,
                )
                if not df.empty:
                    all_picks.append(df)
                    chunk_picks_raw.append(df)
                time.sleep(0.05)

            # Write this chunk's picks to disk split by calendar day
            if write_immediately and chunk_picks_raw:
                chunk_df = pd.concat(chunk_picks_raw, ignore_index=True)
                formatted_chunk = self.format_for_pyocto(chunk_df)
                if not formatted_chunk.empty:
                    self._write_chunk_by_day(formatted_chunk)

        if all_picks:
            return pd.concat(all_picks, ignore_index=True)
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
                           skip_completed: bool = False,
                           chunk_size: str = 'month') -> pd.DataFrame:
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
            chunk's picks are written to per-day CSV files immediately.
        skip_completed : bool, optional
            When True, load the progress file and skip any station tid that
            has already been fully downloaded in a previous run.  Progress is
            saved incrementally so a fresh interruption only loses the
            partially-downloaded station currently in flight.
        chunk_size : str, optional
            Time window per API call: ``'day'``, ``'month'`` (default), or
            ``'year'``.  Passed through to download_picks_for_station().

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
        print(f"Download chunk size : {chunk_size} "
              f"(picks_record used to discover active days; "
              f"bulk {chunk_size} queries issued per station)")

        all_picks = []

        for i, (_, row) in enumerate(stations_df.iterrows(), 1):
            tid = row['tid']
            sta_start = row.get('start_date')
            sta_end = row.get('end_date')

            # Extract per-station channel types for targeted picks_record queries.
            # The 'channels' column (e.g. "BH,EH,HH") is set by
            # get_stations_with_metadata and lists every 2-letter channel prefix
            # present at this location code.  Passing these to
            # download_picks_for_station lets it query picks_record once per
            # channel prefix so that days indexed under any channel are found.
            sta_channels_raw = row.get('channels', '')
            sta_channels = (
                [c.strip() for c in str(sta_channels_raw).split(',') if c.strip()]
                if sta_channels_raw and str(sta_channels_raw).strip()
                else None
            )

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
                station_channels=sta_channels,
                write_immediately=write_immediately,
                chunk_size=chunk_size,
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
            'time': pd.to_datetime(picks_df['peak_time'], format='mixed').astype(np.int64) / 1e9,
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
        if channel_priority is None:
            channel_priority = self.DEFAULT_CHANNEL_PRIORITY
        return _dedup_picks(picks_df, time_threshold=time_threshold,
                            channel_priority=channel_priority)

    def merge_and_deduplicate_station_day_files(
        self,
        stations_df: pd.DataFrame,
        time_threshold: float = 0.5,
        channel_priority: Optional[List[str]] = None,
        workers: int = 0,
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

        Day directories are processed in parallel when *workers* > 1 or when
        *workers* is 0 and more than one CPU is available.

        Parameters
        ----------
        stations_df : pd.DataFrame
            Station metadata (output of get_stations_with_metadata).
            Must contain ``tid`` and ``physical_station`` columns.
        time_threshold : float
            Passed to deduplicate_picks_cross_location().
        channel_priority : list of str, optional
            Passed to deduplicate_picks_cross_location().
        workers : int
            Number of worker processes for the dedup pass.
            0 = auto-detect (uses all available CPU cores).
            1 = sequential (no sub-processes).

        Returns
        -------
        int
            Number of merged station-day files written.
        """
        if 'physical_station' not in stations_df.columns:
            print("WARNING: stations_df missing 'physical_station' column; "
                  "skipping merge/dedup pass.")
            return 0

        # Resolve workers: 0 = auto-detect.
        effective_workers = workers if workers > 0 else (os.cpu_count() or 1)

        # Resolve channel_priority before passing to worker processes so they
        # don't need access to the class constant.
        if channel_priority is None:
            channel_priority = self.DEFAULT_CHANNEL_PRIORITY

        # Build a mapping from per-location filename stem → physical station id.
        # File names are like  IU_CCM_60_picks.csv  (tid with dots→underscores).
        tid_to_phys: dict = {}
        for _, row in stations_df.iterrows():
            tid_clean = row['tid'].replace('.', '_')
            tid_to_phys[tid_clean] = row['physical_station']

        day_dirs = sorted(d for d in self.output_dir.glob("*/*/*") if d.is_dir())
        merged_count = 0

        if effective_workers == 1 or len(day_dirs) <= 1:
            # Sequential path — avoids subprocess overhead for small runs.
            for day_dir in day_dirs:
                count, messages = _dedup_day_dir(
                    day_dir, tid_to_phys, time_threshold, channel_priority
                )
                for msg in messages:
                    print(msg)
                merged_count += count
        else:
            print(f"  Using {effective_workers} worker process(es) "
                  f"across {len(day_dirs)} day director(ies)...")
            with ProcessPoolExecutor(max_workers=effective_workers) as executor:
                futures = {
                    executor.submit(
                        _dedup_day_dir, day_dir, tid_to_phys,
                        time_threshold, channel_priority
                    ): day_dir
                    for day_dir in day_dirs
                }
                for future in as_completed(futures):
                    day_dir = futures[future]
                    try:
                        count, messages = future.result()
                        for msg in messages:
                            print(msg)
                        merged_count += count
                    except Exception as e:
                        print(f"  WARNING: failed to process {day_dir.name}: {e}")

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
            resume: bool = False,
            chunk_size: str = 'month',
            dedup_workers: int = 0) -> List[Path]:
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
        chunk_size : str, optional
            Time window per API call: ``'day'``, ``'month'`` (default), or
            ``'year'``.  Larger values mean fewer round-trips and faster
            downloads.  Automatic fallback to finer granularity occurs when
            the 10 000-pick limit is hit.

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
            chunk_size=chunk_size,
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
                    workers=dedup_workers,
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
