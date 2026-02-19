#!/usr/bin/env python3
"""
QuakeScope to PyOcto Picks Downloader

Downloads phase picks from QuakeScope database and formats them for PyOcto association.
Organizes picks by station and day for efficient processing.

Author: Grant
Date: 2025-01-29
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
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
        
        # QuakeScope API endpoint
        self.picks_url = "https://dasway.ess.washington.edu/quakescope/service/picks/query"
        
        # Maximum picks per query (QuakeScope limit)
        self.max_limit = 10000
    
    def download_picks_for_station(self,
                                   tid: str,
                                   start_time: str,
                                   end_time: str,
                                   station_start: Optional[str] = None,
                                   station_end: Optional[str] = None,
                                   phases: Optional[List[str]] = None,
                                   min_score: float = 0.0,
                                   max_score: float = 1.0,
                                   channel: Optional[str] = None) -> pd.DataFrame:
        """
        Download picks for a single station from QuakeScope.
        
        Queries are chunked by day to avoid hitting the 10,000 pick limit.
        Only queries days when the station was active.
        
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
        
        all_picks = []
        
        # Chunk by day
        current_dt = start_dt
        while current_dt < end_dt:
            next_dt = min(current_dt + timedelta(days=1), end_dt)
            
            # Format times for this chunk
            chunk_start = current_dt.strftime('%Y-%m-%dT%H:%M:%S')
            chunk_end = next_dt.strftime('%Y-%m-%dT%H:%M:%S')
            
            # If phases specified, query each phase separately
            phase_list = phases if phases else [None]
            
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
                        
                        # Warn if we hit the limit even with daily chunks
                        if len(df) >= self.max_limit:
                            print(f"  WARNING: Hit limit ({self.max_limit}) for {tid} on {current_dt.date()}, phase={phase}")
                    
                except requests.RequestException as e:
                    print(f"  ERROR downloading {tid} on {current_dt.date()}, phase={phase}: {e}")
                except pd.errors.EmptyDataError:
                    # No data for this query
                    pass
                
                # Be nice to the server
                time.sleep(0.05)
            
            current_dt = next_dt
        
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
                           channel: Optional[str] = None) -> pd.DataFrame:
        """
        Download picks for multiple stations.
        
        Queries are automatically chunked by day to avoid hitting limits.
        Only queries days when each station was operational.
        
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
        
        print(f"Downloading picks for {len(stations_df)} stations...")
        print(f"Requested time range: {start_time} to {end_time} ({n_days} days)")
        print(f"Phases: {phases if phases else 'all'}")
        print(f"Score range: {min_score} to {max_score}")
        print(f"NOTE: Queries are chunked by day and limited to each station's operational period")
        
        all_picks = []
        
        for i, (_, row) in enumerate(stations_df.iterrows(), 1):
            tid = row['tid']
            sta_start = row.get('start_date')
            sta_end = row.get('end_date')
            
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
                channel=channel
            )
            if not picks.empty:
                all_picks.append(picks)
                print(f"  Downloaded {len(picks)} picks")
            else:
                print(f"  No picks found")
        
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
            Picks in PyOcto format plus amplitude
        """
        if picks_df.empty:
            return pd.DataFrame(columns=['station', 'time', 'phase', 'probability', 'amplitude'])
        
        # Use trace_id as station identifier
        pyocto_picks = pd.DataFrame({
            'station': picks_df['trace_id'],
            'time': pd.to_datetime(picks_df['peak_time']).astype(np.int64) / 1e9,  # Convert to Unix timestamp
            'phase': picks_df['phase'].str.upper(),  # Ensure uppercase P/S
            'probability': picks_df['confidence'],  # Pick confidence score
            'amplitude': picks_df['amplitude']  # Keep amplitude for reference
        })
        
        # Remove any NaN values in required columns
        pyocto_picks = pyocto_picks.dropna(subset=['station', 'time', 'phase', 'probability'])
        
        # Sort by time for efficiency
        pyocto_picks = pyocto_picks.sort_values('time').reset_index(drop=True)
        
        return pyocto_picks
    
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
            if format == 'csv':
                filepath = self.output_dir / f"{key}_picks.csv"
                df.to_csv(filepath, index=False)
            elif format == 'parquet':
                filepath = self.output_dir / f"{key}_picks.parquet"
                df.to_parquet(filepath, index=False)
            elif format == 'pickle':
                filepath = self.output_dir / f"{key}_picks.pkl"
                df.to_pickle(filepath)
            else:
                raise ValueError(f"Unknown format: {format}")
            
            saved_files.append(filepath)
        
        print(f"\nSaved {len(saved_files)} pick files to {self.output_dir}")
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
            output_format: str = 'csv') -> List[Path]:
        """
        Complete workflow: download, format, organize, and save picks.
        
        Parameters:
        -----------
        stations_df : pd.DataFrame
            Station metadata with columns: tid, start_date, end_date
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
            If True, organize into separate files per station/day.
            If False, save all picks in one file.
        output_format : str
            Output format ('csv', 'parquet', 'pickle')
            
        Returns:
        --------
        list of Path
            List of saved file paths
        """
        # Download
        raw_picks = self.download_picks_bulk(
            stations_df=stations_df,
            start_time=start_time,
            end_time=end_time,
            phases=phases,
            min_score=min_score,
            max_score=max_score,
            channel=channel
        )
        
        if raw_picks.empty:
            print("No picks found for given criteria")
            return []
        
        # Format for PyOcto
        pyocto_picks = self.format_for_pyocto(raw_picks)
        print(f"Formatted {len(pyocto_picks)} picks for PyOcto")
        
        # Save
        if organize_by_day:
            # Organize by station/day
            organized = self.organize_by_station_day(pyocto_picks)
            print(f"Organized into {len(organized)} station-day files")
            saved_files = self.save_picks(organized, format=output_format)
        else:
            # Save all in one file
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
