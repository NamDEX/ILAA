import fitz # pymupdf
import os

def ingest_file(filepath):
    if not os.path.exists(filepath):
        return None, "File not found."

    ext = os.path.splitext(filepath)[1].lower()

    if ext == ".pdf":
        return ingest_pdf(filepath)
    elif ext in [".txt", ".md", ".json", ".py", ".csv", ".html"]:
        return ingest_text(filepath)
    else:
        return None, f"Unsupported file type: {ext}"

def ingest_pdf(filepath):
    try:
        doc = fitz.open(filepath)
        text = ""
        pages_read = 0
        images_detected = 0
        tables_detected = 0

        for page in doc:
            pages_read += 1
            text += f"\n--- Page {pages_read} ---\n"
            text += page.get_text()

            # Count images
            images_detected += len(page.get_images())

            # Count tables (if supported)
            if hasattr(page, "find_tables"):
                tables = page.find_tables()
                tables_detected += len(tables.tables)
                # We could extract table content here

        report = {
            "filename": os.path.basename(filepath),
            "pages_read": pages_read,
            "images_detected": images_detected,
            "tables_detected": tables_detected,
            "status": "Complete"
        }

        return text, report
    except Exception as e:
        return None, f"PDF Error: {e}"

def ingest_text(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        report = {
            "filename": os.path.basename(filepath),
            "type": "text",
            "size_chars": len(content),
            "status": "Complete"
        }
        return content, report
    except Exception as e:
        return None, f"Read Error: {e}"
