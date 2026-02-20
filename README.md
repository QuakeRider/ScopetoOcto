# QuakeScope to PyOcto Pick Downloader

Download phase picks from the QuakeScope database and format them for PyOcto phase association.

## Overview

This toolkit downloads picks from QuakeScope and prepares them in the format required by PyOcto:
1. Queries FDSN web services for stations within geographic bounds
2. Identifies active days per station via the QuakeScope `picks_record` endpoint (avoids redundant queries)
3. Downloads picks day-by-day from QuakeScope for those stations, writing each file immediately to disk
4. Formats picks in PyOcto format (station, time, phase, probability)
5. Formats stations in PyOcto format (station, x, y, z) using a proper Transverse Mercator projection
6. Organizes picks into per-day subdirectories (`picks/YYYY-MM-DD/`)

## Important: QuakeScope Limitation

**QuakeScope does NOT support spatial queries for picks.** You cannot query picks directly by lat/lon bounds. 

Instead, this script:
1. Gets stations within your lat/lon bounds (via FDSN)
2. Queries picks for each station individually from QuakeScope

## Installation

### Requirements

```bash
pip install pandas numpy obspy requests tqdm pyocto
```

Or use conda:
```bash
conda env create -f environment.yml
conda activate quakescope
```

## Files

- `workflow.py` - Main script (run this)
- `quakescope_to_pyocto.py` - QuakeScope download and formatting
- `get_stations.py` - FDSN station queries

## Quick Start

```bash
# Pacific Northwest example
python workflow.py \
    --minlat 46 --maxlat 49 \
    --minlon -125 --maxlon -120 \
    --networks UW \
    --start 2024-01-01T00:00:00 \
    --end 2024-01-02T00:00:00 \
    --min-score 0.3
```

## Output

The script creates:
```
quakescope_output/
├── station_metadata.csv       # Full station info from FDSN
├── pyocto_stations.csv        # Stations in PyOcto format (projected coordinates)
├── crs_info.json              # Coordinate Reference System information
└── picks/
    ├── 2024-01-01/
    │   ├── UW_STA1__2024-01-01_picks.csv
    │   ├── UW_STA2__2024-01-01_picks.csv
    │   └── ...
    ├── 2024-01-02/
    │   ├── UW_STA1__2024-01-02_picks.csv
    │   └── ...
    └── ...
```

Pick files are written to disk immediately after each station-day is downloaded, so results are available incrementally and partial runs are not lost.

### PyOcto Stations Format
Columns: `station`, `x`, `y`, `z` (coordinates in km)

**Projection**: Uses PyOcto's proper coordinate transformation with a local transverse Mercator projection centered on the station distribution. The CRS information is saved in `crs_info.json` for use when creating your associator.

### PyOcto Picks Format  
Columns: `station`, `time`, `phase`, `probability`, `amplitude`
- `station`: station trace ID
- `time`: Unix timestamp (seconds)
- `phase`: 'P' or 'S' (uppercase)
- `probability`: pick score (0-1)
- `amplitude`: pick amplitude (for reference, not used by PyOcto)

## Usage

### Command Line

```bash
python workflow.py \
    --minlat MIN_LAT --maxlat MAX_LAT \
    --minlon MIN_LON --maxlon MAX_LON \
    --start START_TIME --end END_TIME \
    [OPTIONS]
```

### Required Arguments
- `--minlat`, `--maxlat`: Latitude bounds
- `--minlon`, `--maxlon`: Longitude bounds  
- `--start`: Start time (ISO format: `YYYY-MM-DDTHH:MM:SS`)
- `--end`: End time (ISO format: `YYYY-MM-DDTHH:MM:SS`)

### Optional Arguments
- `--networks`: Network codes (e.g., `UW CC CN`)
- `--phases`: Phase types (default: `P S`)
- `--min-score`: Minimum pick score (default: 0.3)
- `--max-score`: Maximum pick score (default: 1.0)
- `--channels`: Channel filter (default: `HH?,EH?,BH?`)
- `--output-dir`: Output directory (default: `./quakescope_output`)
- `--fdsn-client`: FDSN client (default: `IRIS`)
- `--no-organize-by-day`: Save all picks in one file instead of per station/day

### Examples

**Pacific Northwest - UW network:**
```bash
python workflow.py \
    --minlat 46 --maxlat 49 --minlon -125 --maxlon -120 \
    --networks UW \
    --start 2024-01-01T00:00:00 --end 2024-01-02T00:00:00
```

**California - Multiple networks:**
```bash
python workflow.py \
    --minlat 34 --maxlat 37 --minlon -120 --maxlon -116 \
    --networks CI NC \
    --start 2024-01-01T00:00:00 --end 2024-01-01T12:00:00 \
    --min-score 0.5
```

**High quality picks only:**
```bash
python workflow.py \
    --minlat 40 --maxlat 42 --minlon -122 --maxlon -120 \
    --networks NC \
    --start 2024-01-01T00:00:00 --end 2024-01-03T00:00:00 \
    --min-score 0.7 \
    --channels HH?
```

## Python API

You can also use the classes directly in Python:

```python
from get_stations import get_stations_in_bounds, get_stations_with_metadata
from quakescope_to_pyocto import QuakeScopePicksDownloader

# Get stations in region
station_ids = get_stations_in_bounds(
    minlat=46.0, maxlat=49.0,
    minlon=-125.0, maxlon=-120.0,
    networks=['UW']
)

# Download and format picks
downloader = QuakeScopePicksDownloader(output_dir='./picks')
pick_files = downloader.run(
    station_ids=station_ids,
    start_time='2024-01-01T00:00:00',
    end_time='2024-01-02T00:00:00',
    phases=['P', 'S'],
    min_score=0.3
)
```

## QuakeScope API Details

The script uses two QuakeScope endpoints:

### picks endpoint (pick download)
- **URL**: `https://dasway.ess.washington.edu/quakescope/service/picks/query`
- **Format**: Pipe-delimited CSV
- **Limit**: 10,000 picks per query (script warns if limit is hit)
- **Query parameters**:
  - `tid`: Trace ID (`NET.STA.LOC`)
  - `channel`: 2-letter channel code
  - `start_time`, `end_time`: ISO format times
  - `phase`: P or S
  - `min_score`, `max_score`: Confidence score range (0–1)
  - `limit`: Max results per query

### picks_record endpoint (active-day detection)
- **URL**: `https://dasway.ess.washington.edu/quakescope/service/picks_record/query`
- **Purpose**: Returns a lightweight record of which calendar days have picks for a given station. Used by the script to skip days with no data before issuing full pick queries, significantly reducing API calls for sparse regions.

### QuakeScope Response Columns (picks)
```
trace_id | network_code | station_code | location_code | channel |
start_time | peak_time | end_time | confidence | amplitude | phase
```

Column mapping to PyOcto format:
- `trace_id` → `station`
- `peak_time` → `time` (converted to Unix timestamp)
- `phase` → `phase` (forced to uppercase)
- `confidence` → `probability`

## PyOcto Format Details

### Picks DataFrame
Required columns for PyOcto (plus amplitude for reference):
```python
{
    'station': str,      # Station identifier  
    'time': float,       # Unix timestamp (seconds)
    'phase': str,        # 'P' or 'S' (uppercase)
    'probability': float,# Confidence (0-1)
    'amplitude': float   # Pick amplitude (optional, for reference)
}
```

### Stations DataFrame
Required columns for PyOcto:
```python
{
    'station': str,  # Station identifier  
    'x': float,      # X coordinate (km, properly projected)
    'y': float,      # Y coordinate (km, properly projected)
    'z': float       # Z coordinate (km, negative for elevation)
}
```

**Projection**: The station coordinates are properly projected using PyOcto's built-in coordinate transformation (`OctoAssociator.from_area()` and `pyocto.inventory_to_df()`). The Coordinate Reference System (CRS) is saved in `crs_info.json` and should be used when creating your associator for pick association.

## Troubleshooting

**No stations found:**
- Check your lat/lon bounds are correct
- Try different network codes
- Check the time range (stations must be operating during this time)
- Try a different FDSN client (IRIS, NCEDC, SCEDC, etc.)

**Hit QuakeScope limit (10,000 picks):**
- Use shorter time windows
- Filter by channel type
- Increase min_score to reduce picks

**PyOcto not installed:**
- The script will fall back to simple approximation
- Install PyOcto with: `pip install pyocto`
- For production use, PyOcto is required for proper coordinate projection

## Citation

If you use this tool in your research, please cite the QuakeScope database:

> Ni, Y., Denolle, M., Thomas, A., Hamilton, A., Münchmeyer, J., Wang, Y., Bachelot, L.,
> Trabant, C., & Mencin, D. (2025). A Global-scale Database of Seismic Phases from
> Cloud-based Picking at Petabyte Scale. *Seismica*, 4(2).
> https://doi.org/10.26443/seismica.v4i2.1738

QuakeScope produced 4.3 billion P- and S-wave picks from 1.3 PB of continuous seismic data
spanning 47,354 stations (2002–2025), using the PhaseNet deep-learning picker via SeisBench
on AWS cloud infrastructure. Picks are publicly queryable at
`https://dasway.ess.washington.edu/quakescope`.

## License

MIT

## Author

Grant - 2025-01-29
