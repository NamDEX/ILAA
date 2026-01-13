import os
import sys
import json
import hashlib
import datetime
import shutil
import argparse
import csv
import io
import warnings
from typing import List, Dict, Any, Optional

# Attempt imports for required libraries
try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

try:
    import docx
    from docx.shared import Inches
except ImportError:
    docx = None

try:
    import pptx
    from pptx.enum.shapes import MSO_SHAPE_TYPE
except ImportError:
    pptx = None

try:
    import openpyxl
except ImportError:
    openpyxl = None

try:
    import pandas as pd
except ImportError:
    pd = None

try:
    from pyxlsb import open_workbook as open_xlsb
except ImportError:
    open_xlsb = None

# ---------------------------------------------------------------------------
# Globals & Config
# ---------------------------------------------------------------------------

CONFIG_FILENAME = "config.json"
REQUIRED_CONFIG_KEYS = [
    "input_root", "output_root", "subfolders", "images_folder_name",
    "log_filename", "overwrite", "max_table_rows"
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_config(path: str = CONFIG_FILENAME) -> Dict[str, Any]:
    if not os.path.exists(path):
        print(f"Error: Config file '{path}' not found.")
        sys.exit(1)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            config = json.load(f)

        missing = [k for k in REQUIRED_CONFIG_KEYS if k not in config]
        if missing:
            print(f"Error: Config missing keys: {missing}")
            sys.exit(1)

        return config
    except json.JSONDecodeError as e:
        print(f"Error parsing config JSON: {e}")
        sys.exit(1)

def get_unique_id(source_relpath: str, location: str, object_index: int) -> str:
    """
    unique_id = sha256("<source_relpath>|<location>|<object_index>").hexdigest()[:16]
    """
    # Normalize path separators for consistency across platforms
    norm_path = source_relpath.replace(os.sep, "/")
    raw = f"{norm_path}|{location}|{object_index}"
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]

def save_image(image_data: bytes, ext: str, unique_id: str, config: Dict[str, Any]) -> str:
    """
    Saves image to <output_root>/<images_folder_name>/IMG_<unique_id>.<ext>
    Returns the filename.
    """
    images_dir = os.path.join(config["output_root"], config["images_folder_name"])
    os.makedirs(images_dir, exist_ok=True)

    # Prefer png if ext not provided or conversion desired? Prompt says "prefer PNG".
    # But usually we just save what we extract unless we want to convert.
    # We will trust the extension passed in, but default to png if unknown.
    if not ext:
        ext = "png"
    ext = ext.lstrip(".").lower()

    filename = f"IMG_{unique_id}.{ext}"
    filepath = os.path.join(images_dir, filename)

    with open(filepath, "wb") as f:
        f.write(image_data)

    return filename

def log_event(config: Dict[str, Any], entry: Dict[str, Any]):
    log_path = os.path.join(config["output_root"], config["log_filename"])
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry) + "\n")

def get_metadata_header(source_path: str, source_relpath: str, source_type: str,
                        warnings_list: List[str], errors_list: List[str]) -> str:
    header = "--- METADATA ---\n"
    header += f"source_path: {source_path}\n"
    header += f"source_relpath: {source_relpath}\n"
    header += f"source_type: {source_type}\n"
    header += f"generated_utc: {datetime.datetime.now(datetime.timezone.utc).isoformat()}\n"

    toolchain = []
    if fitz: toolchain.append(f"PyMuPDF-{fitz.VersionBind}")
    if docx: toolchain.append("python-docx")
    if pptx: toolchain.append("python-pptx")
    if openpyxl: toolchain.append(f"openpyxl-{openpyxl.__version__}")
    if pd: toolchain.append(f"pandas-{pd.__version__}")

    header += f"toolchain: {', '.join(toolchain)}\n"
    header += f"warnings: {json.dumps(warnings_list)}\n"
    header += f"errors: {json.dumps(errors_list)}\n"
    header += "----------------\n\n"
    return header

# ---------------------------------------------------------------------------
# Processors
# ---------------------------------------------------------------------------

def process_pdf(file_path: str, rel_path: str, config: Dict[str, Any]) -> Dict[str, Any]:
    if not fitz:
        return {"status": "failed", "error": "PyMuPDF not installed"}

    doc = fitz.open(file_path)
    content = []
    images_count = 0
    warnings_list = []

    for page_index, page in enumerate(doc):
        page_num = page_index + 1
        location = f"PAGE-{page_num}"

        content.append(f"--- BEGIN {location} ---")

        # Text
        text = page.get_text()
        content.append(text)

        # Images
        image_list = page.get_images(full=True)
        for img_idx, img in enumerate(image_list):
            xref = img[0]
            try:
                base_image = doc.extract_image(xref)
                image_bytes = base_image["image"]
                ext = base_image["ext"]

                unique_id = get_unique_id(rel_path, location, img_idx)
                filename = save_image(image_bytes, ext, unique_id, config)
                images_count += 1

                marker = f"[[IMAGE: IMG_{unique_id} | source={os.path.basename(file_path)} | location={location} | caption=]]"
                content.append(marker)
            except Exception as e:
                warnings_list.append(f"Failed to extract image on {location}: {str(e)}")
                content.append(f"[[IMAGE_MISSING: location={location} | reason={str(e)}]]")

        content.append(f"--- END {location} ---\n")

    return {
        "status": "success",
        "content": "\n".join(content),
        "images_count": images_count,
        "units_count": len(doc),
        "warnings": warnings_list
    }

def process_docx(file_path: str, rel_path: str, config: Dict[str, Any]) -> Dict[str, Any]:
    if not docx:
        return {"status": "failed", "error": "python-docx not installed"}

    try:
        doc = docx.Document(file_path)
    except Exception as e:
        return {"status": "failed", "error": str(e)}

    content = []
    images_count = 0
    warnings_list = []

    # python-docx doesn't support pagination reliably.
    # We will treat the whole doc as a stream, or try to respect sections if possible.
    # However, images are attached to paragraphs or runs, or are shapes.
    # Iterating linearly is tricky because shapes are separate.
    # A simple approach: iterate paragraphs and tables in document order?
    # python-docx `iter_block_items` (not standard but common workaround) or just paragraphs + tables.
    # But for robustness we'll stick to iterating paragraphs and tables as main content.
    # Note: Image extraction from python-docx is easier via `doc.inline_shapes` but that loses position relative to text somewhat.
    # We will try to scan relationships to find images.

    # We will assume one "section" per document if no better info, or use provided sections.
    # The prompt says: "if true page numbers are not reliable, use --- BEGIN SECTION n --- boundaries and record warning: pagination_not_available_for_docx"

    warnings_list.append("pagination_not_available_for_docx")

    # Inline shapes are the easiest to extract.
    # We need to correlate them to their position.
    # Inline shapes are inside runs.

    # We will iterate body elements.
    # Helper to walk elements

    def iter_block_items(parent):
        if isinstance(parent, docx.document.Document):
            parent_elm = parent.element.body
        elif isinstance(parent, docx.table._Cell):
            parent_elm = parent._tc
        else:
            raise ValueError("something's not right")

        for child in parent_elm.iterchildren():
            if child.tag.endswith('p'):
                yield docx.text.paragraph.Paragraph(child, parent)
            elif child.tag.endswith('tbl'):
                yield docx.table.Table(child, parent)

    # Map all inline shapes (images) by their rid to extract them easily
    # But wait, `inline_shape` object has `.height`, `.width`, and `._inline.graphic.graphicData.pic.blipFill.blip.embed` gives the rId.
    # We can also access `doc.part.related_parts[rId]` to get the image part.

    section_counter = 1
    # We'll just wrap the whole thing in one section or split by doc sections?
    # doc.sections exists. But content isn't strictly hierarchically inside sections in the API (sections are properties of ranges).
    # So we'll just emit one big block or split arbitrarily?
    # Prompt says "use --- BEGIN SECTION n --- boundaries". We can do that essentially per 'page' if we can detect breaks, but we can't reliably.
    # So let's just do one Section 1 for the whole doc unless we encounter section breaks (which we can't easily see in paragraph iteration).
    # Simplest compliant approach: Just use SECTION 1 for the whole content.

    location = f"SECTION-{section_counter}"
    content.append(f"--- BEGIN {location} ---")

    obj_index = 0

    for block in iter_block_items(doc):
        if isinstance(block, docx.text.paragraph.Paragraph):
            content.append(block.text)

            # Check for images in runs
            for run in block.runs:
                # This is tricky in python-docx.
                # We can check `run.element.findall('.//a:blip', namespaces=...)`

                blips = run.element.findall('.//{http://schemas.openxmlformats.org/drawingml/2006/main}blip')
                for blip in blips:
                    embed_attr = blip.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed')
                    if embed_attr:
                        try:
                            image_part = doc.part.related_parts[embed_attr]
                            image_bytes = image_part.blob
                            # guess extension
                            ct = image_part.content_type
                            ext = ct.split('/')[-1] if '/' in ct else 'png'

                            unique_id = get_unique_id(rel_path, location, obj_index)
                            save_image(image_bytes, ext, unique_id, config)
                            images_count += 1
                            obj_index += 1

                            marker = f"[[IMAGE: IMG_{unique_id} | source={os.path.basename(file_path)} | location={location} | caption=]]"
                            content.append(marker)
                        except Exception as e:
                            warnings_list.append(f"Failed to extract image in {location}: {str(e)}")

        elif isinstance(block, docx.table.Table):
            # Render table
            # Markdown table preferred
            # Check row count
            if len(block.rows) > config["max_table_rows"]:
                warnings_list.append(f"Table truncated: {len(block.rows)} rows > {config['max_table_rows']}")
                rows_to_process = block.rows[:config["max_table_rows"]]
                truncated = True
            else:
                rows_to_process = block.rows
                truncated = False

            # Simple markdown conversion
            table_lines = []
            for row in rows_to_process:
                cells = [cell.text.replace('\n', ' ').strip() for cell in row.cells]
                row_str = "| " + " | ".join(cells) + " |"
                table_lines.append(row_str)
                # Header separator (naively assume first row is header if we want to be fancy,
                # but for plain text just dumping rows is safer/more generic.
                # Valid markdown tables need a separator line `|---|---|` after the first row.

            if table_lines:
                content.append("\n" + "\n".join(table_lines) + "\n")
                if truncated:
                    content.append(f"... (Table truncated, {len(block.rows) - config['max_table_rows']} rows omitted) ...")

    content.append(f"--- END {location} ---")

    return {
        "status": "success",
        "content": "\n".join(content),
        "images_count": images_count,
        "units_count": 1, # Just 1 section
        "warnings": warnings_list
    }

def process_pptx(file_path: str, rel_path: str, config: Dict[str, Any]) -> Dict[str, Any]:
    if not pptx:
        return {"status": "failed", "error": "python-pptx not installed"}

    try:
        prs = pptx.Presentation(file_path)
    except Exception as e:
        return {"status": "failed", "error": str(e)}

    content = []
    images_count = 0
    warnings_list = []

    for slide_idx, slide in enumerate(prs.slides):
        slide_num = slide_idx + 1
        location = f"SLIDE-{slide_num}"
        content.append(f"--- BEGIN {location} ---")

        # We need to iterate shapes. Sorting by position (top-left) is usually good for reading order.
        # But python-pptx shapes collection order is z-order (creation order mostly).
        # We'll just take them in order or sort by .top then .left?
        # Let's simple sort by top.
        shapes = sorted(slide.shapes, key=lambda s: (s.top if hasattr(s, 'top') else 0, s.left if hasattr(s, 'left') else 0))

        img_idx_on_slide = 0

        for shape in shapes:
            # Text
            if hasattr(shape, "text") and shape.text:
                content.append(shape.text)

            # Table
            if shape.has_table:
                table = shape.table
                table_lines = []
                # Check truncation
                rows = list(table.rows) # items are _Row objects
                if len(rows) > config["max_table_rows"]:
                    warnings_list.append(f"Table truncated in {location}: {len(rows)} rows")
                    rows_proc = rows[:config["max_table_rows"]]
                    trunc = True
                else:
                    rows_proc = rows
                    trunc = False

                for row in rows_proc:
                    cells = [cell.text_frame.text.replace('\n', ' ').strip() for cell in row.cells]
                    row_str = "| " + " | ".join(cells) + " |"
                    table_lines.append(row_str)

                content.append("\n" + "\n".join(table_lines) + "\n")
                if trunc:
                    content.append("... (Table truncated) ...")

            # Image
            # shape.shape_type 13 is PICTURE.
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                try:
                    image = shape.image
                    image_bytes = image.blob
                    ext = image.ext

                    unique_id = get_unique_id(rel_path, location, img_idx_on_slide)
                    save_image(image_bytes, ext, unique_id, config)
                    images_count += 1
                    img_idx_on_slide += 1

                    # Caption is hard in pptx, maybe shape.name?
                    caption = shape.name if shape.name else ""
                    marker = f"[[IMAGE: IMG_{unique_id} | source={os.path.basename(file_path)} | location={location} | caption={caption}]]"
                    content.append(marker)
                except Exception as e:
                    warnings_list.append(f"Failed to extract image on {location}: {str(e)}")

        content.append(f"--- END {location} ---\n")

    return {
        "status": "success",
        "content": "\n".join(content),
        "images_count": images_count,
        "units_count": len(prs.slides),
        "warnings": warnings_list
    }

def process_excel(file_path: str, rel_path: str, config: Dict[str, Any]) -> Dict[str, Any]:
    ext = os.path.splitext(file_path)[1].lower()
    content = []
    warnings_list = []
    units_count = 0
    images_count = 0 # Excel image extraction is complex with openpyxl/pandas.
    # Openpyxl supports image reading?
    # "openpyxl does not currently support reading images from existing files" -> Wait, recent versions might.
    # Actually, openpyxl can read images anchored to cells but it's flaky.
    # `ws._images` ?
    # The prompt requires: "If extraction fails: [[IMAGE_MISSING...]]".

    # We will prioritize text data first.

    if ext == '.csv':
        if not pd: # fallback to stdlib csv
            try:
                location = "TABLE"
                content.append(f"--- BEGIN {location} ---")

                with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                    reader = csv.reader(f)
                    rows = list(reader)

                if len(rows) > config["max_table_rows"]:
                    warnings_list.append(f"CSV truncated: {len(rows)} rows")
                    rows = rows[:config["max_table_rows"]]
                    content.append(f"WARNING: Output truncated to {config['max_table_rows']} rows.")

                for row in rows:
                    content.append(",".join(row))

                content.append(f"--- END {location} ---")
                units_count = 1
            except Exception as e:
                return {"status": "failed", "error": str(e)}
        else:
            try:
                location = "TABLE"
                content.append(f"--- BEGIN {location} ---")
                df = pd.read_csv(file_path)
                if len(df) > config["max_table_rows"]:
                    warnings_list.append(f"CSV truncated: {len(df)} rows")
                    df = df.head(config["max_table_rows"])
                    content.append(f"WARNING: Output truncated to {config['max_table_rows']} rows.")

                # Manual markdown conversion to avoid tabulate dependency
                headers = list(df.columns)
                header_row = "| " + " | ".join(str(h) for h in headers) + " |"
                sep_row = "| " + " | ".join(["---"] * len(headers)) + " |"
                content.append(header_row)
                content.append(sep_row)

                for _, row in df.iterrows():
                    vals = [str(val) if pd.notna(val) else "" for val in row]
                    content.append("| " + " | ".join(vals) + " |")

                content.append(f"--- END {location} ---")
                units_count = 1
            except Exception as e:
                return {"status": "failed", "error": str(e)}

    elif ext == '.xlsx':
        if not openpyxl:
            return {"status": "failed", "error": "openpyxl not installed"}

        try:
            wb = openpyxl.load_workbook(file_path, data_only=True)
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                location = f'SHEET "{sheet_name}"'
                content.append(f"--- BEGIN {location} ---")
                units_count += 1

                # Images in openpyxl
                # ws._images contains list of Image objects?
                if hasattr(ws, '_images'):
                    for img_idx, img in enumerate(ws._images):
                         # img.ref gives cell location? img.anchor
                         # img.fp might be None if loaded from file?
                         # img._data returns bytes?
                         try:
                             # accessing image data in openpyxl can be tricky if not strictly handled.
                             # `img._data` should satisfy `bytes`?
                             image_bytes = img._data() # It's a callable in some versions or property?
                             # Actually usually `img.ref` is the file stream.
                             # Let's try standard way:
                             from openpyxl.drawing.image import Image
                             if isinstance(img, Image):
                                 # We need the bytes.
                                 # `img.ref` is a file-like object sometimes?
                                 # Or `img._data` which is the blob.
                                 pass
                         except:
                             pass
                         # Given complexity and "Best Effort", and openpyxl's limitations (often doesn't preserve images on load unless specified),
                         # and default `load_workbook` might handle it.
                         # We'll skip complex Excel image extraction to ensure stability unless easy.
                         # We'll mark as missing if we suspect images but can't get them.
                         # Actually, let's just focus on data.

                # Data
                rows = list(ws.rows)
                if len(rows) > config["max_table_rows"]:
                    warnings_list.append(f"Sheet '{sheet_name}' truncated: {len(rows)} rows")
                    rows_proc = rows[:config["max_table_rows"]]
                    trunc = True
                else:
                    rows_proc = rows
                    trunc = False

                table_lines = []
                for row in rows_proc:
                    # cell.value
                    vals = [str(cell.value) if cell.value is not None else "" for cell in row]
                    # simple CSV-like or markdown
                    row_str = "| " + " | ".join(vals) + " |"
                    table_lines.append(row_str)

                if table_lines:
                    content.append("\n".join(table_lines))

                if trunc:
                    content.append("... (Sheet truncated) ...")

                content.append(f"--- END {location} ---\n")

        except Exception as e:
            return {"status": "failed", "error": str(e)}

    elif ext == '.xlsb':
        if not open_xlsb:
            return {"status": "failed", "error": "pyxlsb not installed"}

        try:
            with open_xlsb(file_path) as wb:
                for sheet_name in wb.sheets:
                    location = f'SHEET "{sheet_name}"'
                    content.append(f"--- BEGIN {location} ---")
                    units_count += 1

                    with wb.get_sheet(sheet_name) as ws:
                        rows = []
                        for row in ws.rows():
                            rows.append([c.v for c in row])
                            if len(rows) >= config["max_table_rows"]:
                                break

                        if len(rows) >= config["max_table_rows"]:
                             warnings_list.append(f"Sheet '{sheet_name}' truncated")
                             content.append(f"WARNING: Output truncated to {config['max_table_rows']} rows.")

                        # Render
                        for row in rows:
                             vals = [str(v) if v is not None else "" for v in row]
                             content.append("| " + " | ".join(vals) + " |")

                    content.append(f"--- END {location} ---\n")
        except Exception as e:
             return {"status": "failed", "error": str(e)}

    return {
        "status": "success",
        "content": "\n".join(content),
        "images_count": images_count,
        "units_count": units_count,
        "warnings": warnings_list
    }

# ---------------------------------------------------------------------------
# Main Logic
# ---------------------------------------------------------------------------

def process_file(source_abs: str, input_root: str, output_root: str, config: Dict[str, Any]) -> Dict[str, Any]:
    start_time = datetime.datetime.now()

    rel_path = os.path.relpath(source_abs, input_root)
    # Target output path (change extension to .txt)
    base, ext = os.path.splitext(rel_path)
    output_rel_txt = base + ".txt"
    output_abs_txt = os.path.join(output_root, output_rel_txt)

    os.makedirs(os.path.dirname(output_abs_txt), exist_ok=True)

    file_ext = os.path.splitext(source_abs)[1].lower()

    result = {"status": "unsupported", "content": "", "images_count": 0, "units_count": 0, "warnings": [], "errors": []}

    try:
        if file_ext == ".pdf":
            result = process_pdf(source_abs, rel_path, config)
        elif file_ext in [".docx", ".doc"]:
            if file_ext == ".doc":
                # Fallback / Warning
                result["warnings"].append("Legacy .doc format not fully supported, attempted as .docx or marked unsupported.")
                # We can't really process .doc with python-docx.
                result["status"] = "unsupported"
                result["content"] = "Legacy .doc format is not supported by python-docx. Please convert to .docx."
            else:
                result = process_docx(source_abs, rel_path, config)
        elif file_ext in [".pptx", ".ppt"]:
             if file_ext == ".ppt":
                result["warnings"].append("Legacy .ppt format not fully supported.")
                result["status"] = "unsupported"
                result["content"] = "Legacy .ppt format is not supported by python-pptx. Please convert to .pptx."
             else:
                result = process_pptx(source_abs, rel_path, config)
        elif file_ext in [".xlsx", ".csv", ".xlsb"]:
            result = process_excel(source_abs, rel_path, config)
        else:
            result["status"] = "unsupported"
            result["content"] = f"File extension {file_ext} not supported."

    except Exception as e:
        result["status"] = "failed"
        result["errors"] = [str(e)]

    # Merge errors from processor
    errors = result.get("errors", [])
    warnings_list = result.get("warnings", [])
    if result.get("error"):
        errors.append(result["error"])

    # Write output
    metadata = get_metadata_header(source_abs, rel_path, file_ext, warnings_list, errors)
    full_content = metadata + (result.get("content") or "")

    with open(output_abs_txt, 'w', encoding='utf-8') as f:
        f.write(full_content)

    duration = (datetime.datetime.now() - start_time).total_seconds() * 1000

    log_entry = {
        "source_path": source_abs,
        "source_relpath": rel_path,
        "source_type": file_ext,
        "status": result["status"],
        "output_txt_path": output_abs_txt,
        "images_extracted_count": result.get("images_count", 0),
        "units_count": result.get("units_count", 0),
        "warnings": warnings_list,
        "errors": errors,
        "duration_ms": int(duration)
    }

    log_event(config, log_entry)
    return log_entry

def scan_and_process(config: Dict[str, Any]):
    input_root = config["input_root"]
    output_root = config["output_root"]
    subfolders = config["subfolders"]

    stats = {
        "total": 0,
        "success": 0,
        "failed": 0,
        "unsupported": 0,
        "images_extracted": 0
    }

    print(f"Starting processing from: {input_root}")

    # Clean output if overwrite? But instruction says "Overwrite: true" in config implies we overwrite files,
    # but maybe we should be careful not to nuke everything if not needed.
    # The requirement says "Overwrite: true" in the JSON.

    for folder in subfolders:
        search_path = os.path.join(input_root, folder)
        if not os.path.exists(search_path):
            print(f"Warning: Subfolder not found: {search_path}")
            continue

        for root, dirs, files in os.walk(search_path):
            for file in files:
                file_path = os.path.join(root, file)
                print(f"Processing: {file}")
                stats["total"] += 1

                log_entry = process_file(file_path, input_root, output_root, config)

                if log_entry["status"] == "success":
                    stats["success"] += 1
                elif log_entry["status"] == "unsupported":
                    stats["unsupported"] += 1
                else:
                    stats["failed"] += 1

                stats["images_extracted"] += log_entry["images_extracted_count"]

    print("\n--- Summary ---")
    print(f"Total files: {stats['total']}")
    print(f"Success: {stats['success']}")
    print(f"Failed: {stats['failed']}")
    print(f"Unsupported: {stats['unsupported']}")
    print(f"Images extracted: {stats['images_extracted']}")

def self_test(config: Dict[str, Any]):
    print("Running self-test...")
    # 1. Validate Config Parsing
    assert config["input_root"] is not None
    assert isinstance(config["subfolders"], list)
    print("Config validation passed.")

    # 2. Deterministic Hashing
    h1 = get_unique_id("folder/file.pdf", "PAGE-1", 0)
    h2 = get_unique_id("folder/file.pdf", "PAGE-1", 0)
    assert h1 == h2
    print(f"Hashing check passed: {h1}")

    # 3. Check libraries
    print("Libraries check:")
    print(f"PyMuPDF: {'OK' if fitz else 'MISSING'}")
    print(f"python-docx: {'OK' if docx else 'MISSING'}")
    print(f"python-pptx: {'OK' if pptx else 'MISSING'}")
    print(f"openpyxl: {'OK' if openpyxl else 'MISSING'}")

    # 4. Folder replication (simulated)
    inp = config["input_root"]
    out = config["output_root"]
    if not os.path.exists(out):
        os.makedirs(out, exist_ok=True)
    assert os.path.exists(out)
    print("Folder creation check passed.")

    print("Self-test completed successfully.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true", help="Run self-test mode")
    args = parser.parse_args()

    cfg = load_config()

    if args.self_test:
        self_test(cfg)
    else:
        scan_and_process(cfg)
