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
pip install pandas numpy obspy requests tqdm pyocto pyyaml
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
- `config.yaml` - Configuration file template

## Quick Start

1. Edit `config.yaml` with your region, time range, and settings.
2. Run the workflow:

```bash
python workflow.py --config config.yaml
```

To resume an interrupted download:

```bash
python workflow.py --resume ./quakescope_output
```

## Configuration

All settings are controlled through a YAML config file. Copy and edit `config.yaml`:

```yaml
# Geographic Bounds (required)
minlat: 46.0
maxlat: 49.0
minlon: -125.0
maxlon: -120.0

# Time Range (required)
start: "2024-01-01T00:00:00"
end:   "2024-01-02T00:00:00"

# Station Filtering
networks: null           # null = all networks. Example: [UW, CC, CN]
channels: "HH?,EH?,BH?" # Channel filter (wildcards allowed)
fdsn_client: "IRIS"      # FDSN client (e.g. IRIS, NCEDC, SCEDC)

# Pick Filtering
phases: [P, S]    # Phase types. null = all phases.
min_score: 0.3    # Minimum pick confidence (0.0–1.0)
max_score: 1.0    # Maximum pick confidence (0.0–1.0)

# Output Settings
output_dir: "./quakescope_output"  # Results directory
organize_by_day: true              # true = one file per station per day (recommended)
output_format: "csv"               # csv, parquet, or pickle

# Processing Settings
dedup_time_threshold: 0.5  # Seconds; picks from different location codes within
                            # this window are treated as duplicates (keep best channel)
channel_priority: null      # null uses built-in default: [HH, BH, EH, SH, HN, CN, EL, SL, EP, DP]
use_pyocto_projection: true # true = PyOcto CRS projection (recommended)
```

### Configuration Options

#### Geographic Bounds (required)
- `minlat`, `maxlat`: Latitude bounds (decimal degrees)
- `minlon`, `maxlon`: Longitude bounds (decimal degrees)

#### Time Range (required)
- `start`, `end`: ISO format times (`YYYY-MM-DDTHH:MM:SS`)

#### Station Filtering
- `networks`: List of network codes, or `null` for all networks
- `channels`: Channel filter string passed to FDSN (wildcards allowed)
- `fdsn_client`: FDSN client name (`IRIS`, `NCEDC`, `SCEDC`, etc.)

#### Pick Filtering
- `phases`: List of phase types (`[P, S]`), or `null` for all phases
- `min_score`: Minimum pick confidence score (default: `0.3`)
- `max_score`: Maximum pick confidence score (default: `1.0`)

#### Output Settings
- `output_dir`: Directory where results are written (default: `./quakescope_output`)
- `organize_by_day`: Write one file per station per day (recommended; enables deduplication)
- `output_format`: File format for pick files — `csv`, `parquet`, or `pickle`

#### Processing Settings
- `dedup_time_threshold`: Maximum time difference (seconds) between picks from different location codes at the same physical station to be considered duplicates. The pick from the highest-priority channel is kept. Only applies when `organize_by_day` is `true`.
- `channel_priority`: Ordered list of 2-letter channel prefixes (best first). `null` uses the built-in default: `[HH, BH, EH, SH, HN, CN, EL, SL, EP, DP]`.
- `use_pyocto_projection`: Use PyOcto's proper CRS coordinate transformation. Set to `false` to fall back to a simple lat/lon approximation (not recommended for production).

## Output

The script creates:
```
quakescope_output/
├── station_metadata.csv       # Full station info from FDSN
├── pyocto_stations.csv        # Stations in PyOcto format (projected coordinates)
├── crs_info.json              # Coordinate Reference System information
├── run_config.json            # Saved run parameters (used by --resume)
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
- `phase`: `'P'` or `'S'` (uppercase)
- `probability`: pick score (0–1)
- `amplitude`: pick amplitude (for reference, not used by PyOcto)

## Resuming Interrupted Downloads

If a download is interrupted, resume it without re-downloading completed stations:

```bash
python workflow.py --resume ./quakescope_output
```

The `run_config.json` file in the output directory stores the original parameters, and `download_progress.json` tracks which stations have already been completed.

## Examples

**Pacific Northwest - UW network** (`config.yaml`):
```yaml
minlat: 46.0
maxlat: 49.0
minlon: -125.0
maxlon: -120.0
start: "2024-01-01T00:00:00"
end:   "2024-01-02T00:00:00"
networks: [UW]
min_score: 0.3
```

**California - Multiple networks:**
```yaml
minlat: 34.0
maxlat: 37.0
minlon: -120.0
maxlon: -116.0
start: "2024-01-01T00:00:00"
end:   "2024-01-01T12:00:00"
networks: [CI, NC]
min_score: 0.5
```

**High quality picks only:**
```yaml
minlat: 40.0
maxlat: 42.0
minlon: -122.0
maxlon: -120.0
start: "2024-01-01T00:00:00"
end:   "2024-01-03T00:00:00"
networks: [NC]
channels: "HH?"
min_score: 0.7
output_format: "parquet"
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
    'station': str,       # Station identifier
    'time': float,        # Unix timestamp (seconds)
    'phase': str,         # 'P' or 'S' (uppercase)
    'probability': float, # Confidence (0-1)
    'amplitude': float    # Pick amplitude (optional, for reference)
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
- Increase `min_score` to reduce picks

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

Grant Clark - 2025-01-29
