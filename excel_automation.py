#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Automates an Excel-based process for LCR, NSFR, and PRA110 reporting.

This script reads an execution file to identify entities to be processed,
then for each entity, it updates a corresponding "Global View" Excel file.
The process involves:
1.  Copying data from a TOR.xlsx file.
2.  Finding the latest entity-specific output file.
3.  Updating external links in the Global View file to point to the new output file.
4.  Recalculating all formulas.
5.  Updating the execution file with the status of the process.
"""

import os
from datetime import datetime

import openpyxl
import win32com.client

# ==================================================================================================
# CONFIGURATION AREA
# ==================================================================================================
# All paths to files and other settings should be configured here.

# File Paths
GLOBAL_EXECUTION_FILE = r"Z:\PRA NSFR tool\MI & Analytics\Consolidated process\Global View\1. Global Execution File.xlsx"
TOR_FILE = r"Z:\PRA NSFR tool\MI & Analytics\Consolidated process\TOR.xlsx"

LCR_GLOBAL_VIEW_FILE = r"Z:\PRA NSFR tool\MI & Analytics\Consolidated process\Global View\LCR - Global VIews.xlsx"
NSFR_GLOBAL_VIEW_FILE = r"Z:\PRA NSFR tool\MI & Analytics\Consolidated process\Global View\NSFR - Global VIews.xlsx"
PRA110_GLOBAL_VIEW_FILE = r"Z:\PRA NSFR tool\MI & Analytics\Consolidated process\Global View\PRA 110 - Global VIews.xlsx"

LCR_OUTPUT_DIR = r"Z:\PRA NSFR tool\MI & Analytics\Consolidated process\OUTPUT\LCR"
NSFR_OUTPUT_DIR = r"Z:\PRA NSFR tool\MI & Analytics\Consolidated process\OUTPUT\NSFR"
PRA110_OUTPUT_DIR = r"Z:\PRA NSFR tool\MI & Analytics\Consolidated process\OUTPUT\PRA110"

# Sheet Names
LCR_SHEET_NAME = "LCR"
NSFR_SHEET_NAME = "NSFR"
PRA110_SHEET_NAME = "PRA110"
TOR_SHEET_NAME = "TOR"
TOR_SOURCE_SHEET_NAME = "Sheet1"

# Row Range
START_ROW = 5
END_ROW = 50

# ==================================================================================================
# END OF CONFIGURATION AREA
# ==================================================================================================


def find_exact_match_file(directory, entity_name):
    """
    Finds a file in the given directory that exactly matches the entity name,
    ignoring the file extension. The match is case-insensitive.

    Args:
        directory (str): The path to the directory to search in.
        entity_name (str): The name of the entity to match.

    Returns:
        str: The full path to the matching file, or None if no match is found.
    """
    if not os.path.isdir(directory):
        print(f"Warning: Output directory not found: {directory}")
        return None

    for filename in os.listdir(directory):
        if filename.endswith(('.xlsx', '.xlsb', '.xlsm')):
            fname_without_ext, _ = os.path.splitext(filename)
            if fname_without_ext.lower() == entity_name.lower():
                return os.path.join(directory, filename)
    return None


def process_tab(execution_workbook, sheet_name, global_view_file, output_dir):
    """
    Processes a single tab (LCR, NSFR, or PRA110) in the Global Execution File.
    First, it checks if there are any entities to process before opening any files.
    """
    print("--------------------------------------------------")
    print(f"Scanning '{sheet_name}' tab for entities to process...")
    print("--------------------------------------------------")

    sheet = execution_workbook[sheet_name]

    entities_to_process = []
    for row in range(START_ROW, END_ROW + 1):
        if sheet.cell(row=row, column=5).value == 'Y':
            entity_name = sheet.cell(row=row, column=3).value
            if entity_name:
                entities_to_process.append({'row': row, 'name': entity_name})

    if not entities_to_process:
        print(f"No entities marked with 'Y' to process in '{sheet_name}' tab. Skipping.")
        return

    print(f"Found {len(entities_to_process)} entities to process. Starting Excel operations...")
    excel = None
    gv_workbook = None
    try:
        print(f"Opening Global View file: {global_view_file}")
        excel = win32com.client.Dispatch("Excel.Application")
        excel.DisplayAlerts = False
        excel.AskToUpdateLinks = False

        gv_workbook = excel.Workbooks.Open(global_view_file)

        # Paste TOR data, preserving formatting.
        print("Pasting TOR data...")
        try:
            tor_workbook = excel.Workbooks.Open(TOR_FILE)
            tor_sheet = tor_workbook.Sheets(TOR_SOURCE_SHEET_NAME)

            gv_tor_sheet = gv_workbook.Sheets(TOR_SHEET_NAME)
            gv_tor_sheet.Cells.ClearContents() # Clear the sheet before pasting

            # Copy the entire source sheet
            tor_sheet.Cells.Copy()

            # Activate the destination sheet and paste
            gv_tor_sheet.Activate()
            gv_tor_sheet.Range("A1").Select()
            gv_tor_sheet.Paste()

            tor_workbook.Close(SaveChanges=False)

            # Clear the clipboard
            excel.Application.CutCopyMode = False

            print("TOR data pasted successfully.")
        except Exception as e:
            print(f"Error during TOR paste operation: {e}")

        for entity in entities_to_process:
            row = entity['row']
            entity_name = entity['name']
            print(f"\n---> Processing Entity: {entity_name} (Row: {row})")

            entity_file_path = find_exact_match_file(output_dir, entity_name)

            if entity_file_path:
                print(f"Found exact match file: {entity_file_path}")

                # Update external links
                print("Searching for and replacing external links...")
                try:
                    links = gv_workbook.LinkSources(Type=1)  # Type 1 for Excel links
                    if links:
                        link_replaced = False
                        for link in links:
                            # Heuristic: assume the link to replace contains a filename
                            # similar to the entity name.
                            link_filename = os.path.basename(link)
                            if os.path.splitext(link_filename)[0].lower() == entity_name.lower():
                                print(f"Found link to replace: {link}")
                                gv_workbook.ChangeLink(link, entity_file_path, Type=1)
                                print(f"Link successfully replaced with: {entity_file_path}")
                                link_replaced = True
                                break # Assume one link per entity
                        if not link_replaced:
                            print("Could not find a suitable link to replace for this entity.")
                    else:
                        print("No external links found in this workbook.")

                except Exception as e:
                    print(f"An error occurred during link replacement: {e}")

                # Update status in the execution file
                sheet.cell(row=row, column=6).value = 'Y'
                sheet.cell(row=row, column=7).value = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                sheet.cell(row=row, column=8).value = '' # Clear previous error message
            else:
                print(f"File not found for entity: {entity_name}")
                sheet.cell(row=row, column=6).value = 'N'
                sheet.cell(row=row, column=8).value = 'File not found'

        # Recalculate, Save, and Close after all entities for the tab are processed
        print("\nRecalculating all formulas in the Global View file...")
        gv_workbook.RefreshAll()
        excel.Calculate()
        print("Recalculation complete.")

        print(f"Saving and closing '{global_view_file}'...")
        gv_workbook.Close(SaveChanges=True)
        print("File saved and closed.")

    except Exception as e:
        print(f"An error occurred while processing '{sheet_name}': {e}")
    finally:
        # Ensure Excel is properly closed and objects are released
        if gv_workbook:
            del gv_workbook
        if excel:
            excel.Quit()
            del excel
        print(f"Finished processing '{sheet_name}' tab.")


def main():
    """
    Main function to orchestrate the automation process.
    """
    print("==================================================")
    print("Starting the Excel Automation Process")
    print("==================================================")

    try:
        print(f"Loading Global Execution File: {GLOBAL_EXECUTION_FILE}")
        execution_workbook = openpyxl.load_workbook(GLOBAL_EXECUTION_FILE)
    except FileNotFoundError:
        print(f"FATAL ERROR: The Global Execution File was not found at '{GLOBAL_EXECUTION_FILE}'")
        return
    except Exception as e:
        print(f"FATAL ERROR: Could not open Global Execution File. Error: {e}")
        return

    # Process each tab as defined in the configuration
    process_configs = [
        (LCR_SHEET_NAME, LCR_GLOBAL_VIEW_FILE, LCR_OUTPUT_DIR),
        (NSFR_SHEET_NAME, NSFR_GLOBAL_VIEW_FILE, NSFR_OUTPUT_DIR),
        (PRA110_SHEET_NAME, PRA110_GLOBAL_VIEW_FILE, PRA110_OUTPUT_DIR),
    ]

    for sheet_name, global_view_file, output_dir in process_configs:
        process_tab(execution_workbook, sheet_name, global_view_file, output_dir)

    # Save the updated Global Execution File
    try:
        print("\n--------------------------------------------------")
        print(f"Saving changes to '{GLOBAL_EXECUTION_FILE}'...")
        execution_workbook.save(GLOBAL_EXECUTION_FILE)
        print("Global Execution File saved successfully.")
    except Exception as e:
        print(f"Error saving the Global Execution File: {e}")

    print("\n==================================================")
    print("Automation process completed.")
    print("==================================================")

if __name__ == "__main__":
    main()
