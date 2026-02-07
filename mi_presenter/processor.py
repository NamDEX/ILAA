from collections import defaultdict

class TileProcessor:
    def __init__(self, dataset, config):
        self.dataset = dataset
        self.config = config

    def process(self):
        grouped_data = defaultdict(list)
        structure_mode = self.config.settings["output_structure"]

        for tile_conf in self.config.tiles:
            tile_data = self._resolve_tile_data(tile_conf)

            # Skip empty tiles if configured to do so safely (as per prompt "Empty tiles must be skipped safely")
            # But let's verify if we extracted anything.
            # Actually, even if data is missing, we might want to show the headers?
            # Prompt says "Empty tiles must be skipped safely."
            # We'll treat "Empty" as "No columns found".
            if not tile_data["columns"]:
                print(f"Warning: Skipping empty tile '{tile_conf.get('title')}' - No columns found.")
                continue

            # Determine grouping key (Destination Sheet Name)
            if structure_mode == "PER_SOURCE":
                # Override: Group by Source File + Sheet
                # We need to clean this name for Excel compatibility later, but for grouping we use the raw strings
                dest_sheet = f"{tile_conf['file_alias']}_{tile_conf['sheet_name']}"
            else:
                # PER_TILE: Use configured output sheet name
                dest_sheet = tile_conf['output_sheet_name']

            grouped_data[dest_sheet].append(tile_data)

        return grouped_data

    def _resolve_tile_data(self, tile_conf):
        file_alias = tile_conf["file_alias"]
        sheet_name = tile_conf["sheet_name"]
        row_label = tile_conf["row_label"]
        col_conf = str(tile_conf["column_labels"]).strip()

        # Resolve Columns
        if col_conf == "*":
            columns = self.dataset.get_columns_for_row(file_alias, sheet_name, row_label)
        else:
            # Parse comma list
            columns = [c.strip() for c in col_conf.split(',') if c.strip()]

        # Extract Data
        # We need a matrix.
        # For COLUMNS mode: 1 Row (the row_label) x N Cols.
        # For ROWS mode: N Cols (acting as rows) x 1 Value.

        # Let's standardize on a list of dicts or a grid.
        # Each cell needs: value, status, link info.

        row_data = []
        for col in columns:
            record = self.dataset.get_record(file_alias, sheet_name, row_label, col)
            row_data.append(record)

        return {
            "config": tile_conf,
            "columns": columns, # The column headers
            "data": row_data    # The list of records corresponding to columns
        }
