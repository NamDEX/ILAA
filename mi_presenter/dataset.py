import json
import os
from datetime import datetime
from collections import defaultdict

class DatasetLoader:
    def __init__(self, filepath):
        self.filepath = filepath
        self.records = {} # Key: (file_alias, sheet_name, row_label, column_label)
        self.duplicates = []
        self.summary = {
            "sources": set(), # (file_alias, sheet_name)
            "labels": set(), # (row_label, column_label)
            "files": set(),
            "sheets": set(),
            "row_labels": set(),
            "column_labels": set()
        }

    def load(self):
        if not os.path.exists(self.filepath):
            raise FileNotFoundError(f"Dataset not found: {self.filepath}")

        with open(self.filepath, 'r', encoding='utf-8') as f:
            for line_number, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    self._process_record(record, line_number)
                except json.JSONDecodeError:
                    print(f"Warning: Skipping invalid JSON on line {line_number}")

    def _process_record(self, record, line_number):
        # Validate required fields
        required = ['file_alias', 'sheet_name', 'row_label', 'column_label']
        for field in required:
            if field not in record:
                # Warning logged, but we skip this record to be safe
                return

        key = (
            record['file_alias'],
            record['sheet_name'],
            record['row_label'],
            record['column_label']
        )

        # Update summary
        self.summary["sources"].add((record['file_alias'], record['sheet_name']))
        self.summary["labels"].add((record['row_label'], record['column_label']))
        self.summary["files"].add(record['file_alias'])
        self.summary["sheets"].add(record['sheet_name'])
        self.summary["row_labels"].add(record['row_label'])
        self.summary["column_labels"].add(record['column_label'])

        # Uniqueness logic
        if key in self.records:
            existing = self.records[key]
            existing_ts = existing.get('extracted_as_of')
            new_ts = record.get('extracted_as_of')

            # Log duplicate
            self.duplicates.append(key)

            # Determine winner
            if new_ts and existing_ts:
                try:
                    # ISO format string comparison usually works, but full parsing is safer
                    # However, assuming ISO 8601, string comparison is sufficient and faster
                    if new_ts > existing_ts:
                        self.records[key] = record
                except:
                    # If comparison fails, assume overwrite (last record wins logic fallback)
                    self.records[key] = record
            else:
                # If timestamp missing, last record wins
                self.records[key] = record
        else:
            self.records[key] = record

    def get_record(self, file_alias, sheet_name, row_label, column_label):
        return self.records.get((file_alias, sheet_name, row_label, column_label))

    def get_columns_for_row(self, file_alias, sheet_name, row_label):
        """Used for wildcard expansion."""
        cols = set()
        for (fa, sn, rl, cl) in self.records.keys():
            if fa == file_alias and sn == sheet_name and rl == row_label:
                cols.add(cl)
        return sorted(list(cols))

    def get_stats(self):
        return {
            "total_records_kept": len(self.records),
            "duplicates_found": len(self.duplicates),
            "unique_files": len(self.summary["files"]),
            "unique_sheets": len(self.summary["sheets"]),
        }
