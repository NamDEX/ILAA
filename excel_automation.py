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
from difflib import SequenceMatcher

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
TOR_SOURCE_SHEET_NAME = "Sheet 1"

# Row Range
START_ROW = 5
END_ROW = 50

# ==================================================================================================
# END OF CONFIGURATION AREA
# ==================================================================================================


def find_best_match_file(directory, entity_name):
    """
    Finds the file in the given directory that best matches the entity name.
    It uses a sequence matching algorithm to find the most similar filename.
    A threshold is used to prevent obviously wrong matches.

    Args:
        directory (str): The path to the directory to search in.
        entity_name (str): The name of the entity to match.

    Returns:
        str: The full path to the best matching file, or None if no suitable match is found.
    """
    best_match_filename = None
    highest_ratio = 0.6  # Match threshold

    if not os.path.isdir(directory):
        print(f"Warning: Output directory not found: {directory}")
        return None

    for filename in os.listdir(directory):
        if filename.endswith(('.xlsx', '.xlsb', '.xlsm')):
            fname_without_ext, _ = os.path.splitext(filename)
            ratio = SequenceMatcher(None, entity_name.lower(), fname_without_ext.lower()).ratio()
            if ratio > highest_ratio:
                highest_ratio = ratio
                best_match_filename = filename

    if best_match_filename:
        return os.path.join(directory, best_match_filename)
    return None


def process_tab(execution_workbook, sheet_name, global_view_file, output_dir):
    """
    Processes a single tab (LCR, NSFR, or PRA110) in the Global Execution File.

    Args:
        execution_workbook (openpyxl.Workbook): The workbook object for the execution file.
        sheet_name (str): The name of the tab to process.
        global_view_file (str): The path to the Global View file for this tab.
        output_dir (str): The path to the output directory containing entity files.
    """
    print("--------------------------------------------------")
    print(f"Processing '{sheet_name}' tab...")
    print("--------------------------------------------------")

    sheet = execution_workbook[sheet_name]

    entities_to_process = []
    for row in range(START_ROW, END_ROW + 1):
        if sheet.cell(row=row, column=5).value == 'Y':
            entity_name = sheet.cell(row=row, column=3).value
            if entity_name:
                entities_to_process.append({'row': row, 'name': entity_name})

    if not entities_to_process:
        print(f"No entities marked with 'Y' found in '{sheet_name}' tab.")
        return

    excel = None
    gv_workbook = None
    try:
        print(f"Opening Global View file: {global_view_file}")
        excel = win32com.client.Dispatch("Excel.Application")
        excel.DisplayAlerts = False
        excel.AskToUpdateLinks = False

        gv_workbook = excel.Workbooks.Open(global_view_file)

        # Paste TOR data only once when the file is first opened.
        print("Pasting TOR data...")
        try:
            tor_workbook = excel.Workbooks.Open(TOR_FILE)
            tor_sheet = tor_workbook.Sheets(TOR_SOURCE_SHEET_NAME)
            tor_sheet.Cells.Copy()

            gv_tor_sheet = gv_workbook.Sheets(TOR_SHEET_NAME)
            gv_tor_sheet.Paste(gv_tor_sheet.Range("A1"))
            tor_workbook.Close(SaveChanges=False)
            print("TOR data pasted successfully.")
        except Exception as e:
            print(f"Error during TOR paste operation: {e}")

        for entity in entities_to_process:
            row = entity['row']
            entity_name = entity['name']
            print(f"\n---> Processing Entity: {entity_name} (Row: {row})")

            entity_file_path = find_best_match_file(output_dir, entity_name)

            if entity_file_path:
                print(f"Found best match file: {entity_file_path}")

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
                            score = SequenceMatcher(None, entity_name.lower(), os.path.splitext(link_filename)[0].lower()).ratio()
                            if score > 0.6: # Similarity threshold
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
        if excel:
            excel.Quit()
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
