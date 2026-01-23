import os
import glob
import pandas as pd

from reportlab.lib.pagesizes import letter
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    Image,
)
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
from reportlab.lib.units import inch


# === CONFIG ===
CSV_PATH = r"datarequests/monica_gw_request_20251118.csv"  # adjust if needed
PHOTOS_DIR = r"\\agustin\amp\data\database\photos\Digital photos_wells"
OUTPUT_DIR = r"output_pdfs"        # folder where individual PDFs are written


def make_output_dir(path: str):
    if not os.path.exists(path):
        os.makedirs(path)


def aggregate_group_to_record(group: pd.DataFrame) -> dict:
    """
    For a group of rows with the same PointID, collapse each column into a
    single string (join unique non-null values with '; ').
    """
    record = {}
    for col in group.columns:
        if col == "PointID":
            continue
        # Unique non-empty values
        vals = [
            str(v)
            for v in group[col].dropna().unique()
            if str(v).strip() != ""
        ]
        if not vals:
            continue
        record[col] = "; ".join(vals)
    return record


def find_photos_for_point(point_id: str) -> list:
    """
    Return a list of photo file paths for the given PointID.
    PointID in CSV has hyphens (e.g. 'WL-0224'), but photos are
    stored without hyphens (e.g. 'WL0224*.jpg').
    """
    clean_id = point_id.replace("-", "")
    pattern = os.path.join(PHOTOS_DIR, f"{clean_id}*.jpg")
    return sorted(glob.glob(pattern))


from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph, Table, TableStyle, Image, Spacer
from reportlab.lib import colors
from reportlab.lib.units import inch

def build_pdf_for_point(point_id: str, group: pd.DataFrame, output_dir: str):
    """
    Create a single PDF for one PointID, including:
      - Header with PointID
      - Table of field/value pairs (wrapped text)
      - All matching photos
    """
    # Sanitize filename (in case of weird chars)
    safe_point_id = point_id.replace("/", "_").replace("\\", "_")
    pdf_path = os.path.join(output_dir, f"{safe_point_id}.pdf")

    doc = SimpleDocTemplate(pdf_path, pagesize=letter)
    styles = getSampleStyleSheet()
    elements = []

    # Title
    elements.append(Paragraph(f"Point ID: {point_id}", styles["Title"]))
    elements.append(Spacer(1, 12))

    # Aggregate field values
    record = aggregate_group_to_record(group)

    # Paragraph style for table cells (wrap long text)
    cell_style = ParagraphStyle(
        "Cell",
        parent=styles["Normal"],
        fontSize=8,
        leading=9,
        wordWrap="CJK",   # better at wrapping long strings / URLs
    )

    # Build table: Field | Value
    data = [["Field", "Value"]]  # header row

    for field_name, value in record.items():
        # Wrap the value in a Paragraph so it will line-wrap
        value_paragraph = Paragraph(str(value), cell_style)
        field_paragraph = Paragraph(str(field_name), cell_style)
        data.append([field_paragraph, value_paragraph])

    table = Table(data, colWidths=[2.0 * inch, 4.5 * inch])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 9),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ]
        )
    )

    elements.append(table)
    elements.append(Spacer(1, 18))

    # Photos
    photo_files = find_photos_for_point(point_id)
    if photo_files:
        elements.append(Paragraph("Photos", styles["Heading2"]))
        elements.append(Spacer(1, 6))

        for photo_path in photo_files:
            try:
                img = Image(photo_path)
                # Restrict size to fit page width
                img._restrictSize(6.5 * inch, 8.0 * inch)
                elements.append(img)
                elements.append(
                    Paragraph(os.path.basename(photo_path), styles["Normal"])
                )
                elements.append(Spacer(1, 12))
            except Exception as exc:
                elements.append(
                    Paragraph(
                        f"Could not load image: {os.path.basename(photo_path)} "
                        f"({exc})",
                        styles["Normal"],
                    )
                )
                elements.append(Spacer(1, 6))
    else:
        elements.append(
            Paragraph("No photos found for this PointID.", styles["Italic"])
        )

    doc.build(elements)
    print(f"Created PDF for {point_id}: {pdf_path}")



def main():
    make_output_dir(OUTPUT_DIR)

    # Read CSV
    df = pd.read_csv(CSV_PATH)

    if "PointID" not in df.columns:
        raise ValueError("CSV must contain a 'PointID' column.")

    # Group by PointID and build one PDF per group
    grouped = df.groupby("PointID", dropna=True)

    for point_id, group in grouped:
        if pd.isna(point_id):
            continue
        build_pdf_for_point(str(point_id), group, OUTPUT_DIR)


if __name__ == "__main__":
    main()
