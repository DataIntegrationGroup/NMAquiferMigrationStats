#!/usr/bin/env python3
import csv
import glob
import os

# Pattern for your input files (in the current directory)
INPUT_PATTERN = "InvalidWellData*.csv"

# Name of the combined output file
OUTPUT_FILE = "invalid_well_data/InvalidWellData_combined.csv"

def main():
    files = sorted(glob.glob(INPUT_PATTERN))
    if not files:
        print(f"No files matching pattern {INPUT_PATTERN!r} found.")
        return

    print("Found input files:")
    for f in files:
        print(f"  - {f}")

    writer = None
    total_rows = 0

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as out_f:
        for i, file_path in enumerate(files, start=1):
            with open(file_path, "r", newline="", encoding="utf-8") as in_f:
                reader = csv.reader(in_f)
                header = next(reader, None)

                # Initialize writer with header from the first file
                if writer is None:
                    writer = csv.writer(out_f)
                    if header:
                        writer.writerow(header)

                # Write all remaining rows
                for row in reader:
                    writer.writerow(row)
                    total_rows += 1

            print(f"Finished {file_path}")

    print(f"\nDone. Wrote {total_rows} data rows to {OUTPUT_FILE!r}.")

if __name__ == "__main__":
    main()
