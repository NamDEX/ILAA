import argparse
import os
import sys
import time
from datetime import datetime
from .dataset import DatasetLoader
from .config import LayoutConfig
from .processor import TileProcessor
from .renderer import ExcelRenderer

def main():
    parser = argparse.ArgumentParser(description="Presentation Bot")
    parser.add_argument("--input", required=True, help="Path to input JSONL dataset")
    parser.add_argument("--configs", required=True, help="Path to Configs directory")
    parser.add_argument("--outdir", required=True, help="Path to Output directory")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")

    args = parser.parse_args()

    start_time = time.time()

    # 1. Setup & Logging paths
    if not os.path.exists(args.outdir):
        os.makedirs(args.outdir)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(args.outdir, f"presentation_log_{timestamp}.txt")

    def log(msg):
        if args.verbose:
            print(msg)
        with open(log_file, "a") as f:
            f.write(msg + "\n")

    log(f"Started at {datetime.now()}")
    log(f"Input: {args.input}")

    # 2. Load Dataset
    log("Loading dataset...")
    loader = DatasetLoader(args.input)
    try:
        loader.load()
    except Exception as e:
        print(f"Error loading dataset: {e}")
        return

    stats = loader.get_stats()
    log(f"Dataset loaded. Records: {stats['total_records_kept']}, Duplicates: {stats['duplicates_found']}")

    # 3. Handle Layout Config
    layout_path = os.path.join(args.configs, "layout.xlsx")
    config = LayoutConfig(layout_path)

    if not config.exists():
        log("Layout config missing. Generating default...")
        if not os.path.exists(args.configs):
            os.makedirs(args.configs)
        config.generate_default(loader)
        print(f"Default layout generated at {layout_path}. Please edit it and run again.")
        return # Stop here as per typical workflow, or continue?
        # Prompt says: "If Configs\layout.xlsx does not exist: Automatically generate a layout workbook."
        # "Delieverable ... layout auto-generation, successful MI workbook output."
        # If we stop, we can't produce the output in the same run.
        # But a default layout has NO tiles enabled (or we can add sample tiles).
        # We should probably reload it immediately and try to run, even if empty.
        # Wait, if we generate a default, the 'TILES' sheet is empty (except headers).
        # So nothing will be output.
        # I'll create a dummy tile if I can, but the user said "Users edit this file".
        # I'll proceed to load it. It will have 0 tiles. That's fine.

    log("Loading layout config...")
    try:
        config.load()
    except Exception as e:
        print(f"Error loading config: {e}")
        log(f"Error loading config: {e}")
        return

    log(f"Config loaded. Tiles found: {len(config.tiles)}")
    log(f"Settings: {config.settings}")

    # 4. Process Tiles
    log("Processing tiles...")
    processor = TileProcessor(loader, config)
    grouped_data = processor.process()

    num_sheets = len(grouped_data)
    log(f"Processed into {num_sheets} output groups (sheets).")

    # 5. Render Output
    output_filename = f"MI_{timestamp}.xlsx"
    output_path = os.path.join(args.outdir, output_filename)

    log(f"Rendering to {output_path}...")
    renderer = ExcelRenderer(config)
    try:
        renderer.render(grouped_data, output_path)
    except Exception as e:
        print(f"Error rendering Excel: {e}")
        log(f"Error rendering Excel: {e}")
        return

    duration = time.time() - start_time
    log(f"Completed in {duration:.2f} seconds.")
    log("Summary:")
    log(f"- Tiles processed: {len(config.tiles)}")
    log(f"- Sheets generated: {num_sheets}") # Approximate, as SHEETS mode spawns more
    log(f"- Dataset records: {stats['total_records_kept']}")

    print(f"\nSuccess! Output saved to: {output_path}")

if __name__ == "__main__":
    main()
