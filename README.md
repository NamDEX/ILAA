# MI Presenter

A standalone CLI tool that generates MI Excel tables from a machine-readable JSONL dataset.

## Project Structure
```
root/
├── Input/              # Place dataset.jsonl files here
├── Configs/            # layout.xlsx is stored here
├── Output/             # Generated Excel files and logs
├── mi_presenter/       # Source code package
├── requirements.txt
└── README.md
```

## Installation
1. Ensure Python 3.8+ is installed.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Usage
Run the tool from the project root:

```bash
python -m mi_presenter \
  --input "Input\dataset_latest.jsonl" \
  --configs "Configs" \
  --outdir "Output" \
  [--verbose]
```

### First Run
If `Configs\layout.xlsx` does not exist, the tool will automatically generate a default template containing:
- **TILES**: The main configuration sheet.
- **SETTINGS**: Global settings (Missing Value behavior, Grouping structure).
- **CATALOG_SOURCES**: Read-only list of available files/sheets in the dataset.
- **CATALOG_LABELS**: Read-only list of available row/column labels.

## Configuration (layout.xlsx)

### Sheet: TILES
Define your MI tables (tiles) here.

| Column | Description |
|--------|-------------|
| **Enabled (Y/N)** | Set to `Y` to include this tile. |
| **Tile Order** | Number to determine sequence in output. |
| **Title** | Header title for the tile. |
| **File Alias** | Must match `file_alias` in dataset. |
| **Sheet Name** | Must match `sheet_name` in dataset. |
| **Row Label** | The row to extract (must match `row_label`). |
| **Column Labels** | Comma-separated list (e.g., `DEC-25, NOV-25`) or `*` for all. |
| **Layout Mode** | `COLUMNS` (Standard), `ROWS` (Transposed), `SHEETS` (One sheet per col). |
| **Output Mode** | `VALUES` (Static data), `LINKS` (Excel formulas to source). |
| **Output Sheet Name** | Destination sheet in the generated workbook (ignored if PER_SOURCE structure). |

### Sheet: SETTINGS
| Setting | Options | Default | Description |
|---------|---------|---------|-------------|
| **missing_value** | `BLANK`, `0` | `BLANK` | content when status != ok |
| **output_structure** | `PER_TILE`, `PER_SOURCE` | `PER_TILE` | Grouping strategy. |

## Logic Notes
- **Duplicate Handling**: If the dataset contains duplicate records for the same position, the one with the latest `extracted_as_of` timestamp is used.
- **Link Verification**: The tool checks if source files exist. If not, it warns in the log but still generates the link.
- **Safe Mode**: Missing columns or empty tiles are skipped with a warning, ensuring the process completes.
