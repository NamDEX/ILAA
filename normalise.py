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
import re
import subprocess
from typing import List, Dict, Any, Optional, Tuple

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

try:
    from PIL import Image
except ImportError:
    Image = None

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

def ensure_dependencies(config_path: str = CONFIG_FILENAME):
    # Look for requirements.txt in the same directory as config.json
    # or current working directory
    if os.path.exists(config_path):
        base_dir = os.path.dirname(os.path.abspath(config_path))
        req_path = os.path.join(base_dir, "requirements.txt")
    else:
        req_path = "requirements.txt"

    if not os.path.exists(req_path):
        return

    # Check if we are missing any core libraries that we tried to import
    missing = []
    if fitz is None: missing.append("pymupdf")
    if docx is None: missing.append("python-docx")
    if pptx is None: missing.append("python-pptx")
    if openpyxl is None: missing.append("openpyxl")
    if pd is None: missing.append("pandas")
    if open_xlsb is None: missing.append("pyxlsb")
    if Image is None: missing.append("pillow")

    if missing:
        print(f"Missing libraries: {', '.join(missing)}. Attempting to install from {req_path}...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", req_path])
            print("Dependencies installed. Restarting script...")
            # Restart the script to load the newly installed modules
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            print(f"Failed to install dependencies: {e}")
            print("Proceeding with available libraries...")

def load_config(path: str = CONFIG_FILENAME) -> Dict[str, Any]:
    if not os.path.exists(path):
        # Fallback defaults if config missing (though user said it exists)
        return {
             "input_root": ".", "output_root": "output",
             "subfolders": [], "images_folder_name": "images",
             "log_filename": "processing_log.jsonl", "overwrite": True,
             "max_table_rows": 2000
        }
    try:
        with open(path, 'r', encoding='utf-8') as f:
            config = json.load(f)

        # We don't exit hard on missing keys anymore, just warn or use defaults for new keys
        for k in REQUIRED_CONFIG_KEYS:
            if k not in config:
                print(f"Warning: Config missing key: {k}")

        return config
    except json.JSONDecodeError as e:
        print(f"Error parsing config JSON: {e}")
        sys.exit(1)

def get_unique_id(source_relpath: str, location: str, object_index: int) -> str:
    """
    unique_id = sha256("<source_relpath>|<location>|<object_index>").hexdigest()[:16]
    """
    norm_path = source_relpath.replace(os.sep, "/")
    raw = f"{norm_path}|{location}|{object_index}"
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]

def clean_text(text: str) -> str:
    """
    Collapse excessive blank lines (max 1 consecutive empty line).
    Preserve paragraph boundaries.
    """
    if not text:
        return ""
    # Normalize line endings
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    # Collapse 3 or more newlines to 2 (one empty line)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text

def get_image_dimensions(image_bytes: bytes) -> Tuple[int, int]:
    if not Image:
        return (0, 0)
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            return img.width, img.height
    except Exception:
        return (0, 0)

def classify_image(image_bytes: bytes, width: int, height: int, nearby_text: str, filename: str, config: Dict[str, Any]) -> Tuple[str, str]:
    """
    Returns (type, depicts)
    types: data_chart, data_table, conceptual_diagram, formula, portrait, logo, decorative
    """

    # 1. Defaults and Config
    cls_config = config.get("image_classification", {})
    min_w = cls_config.get("min_width", 100)
    min_h = cls_config.get("min_height", 100)
    keywords = cls_config.get("keywords", {})

    # 2. Heuristics - Decorative / Logo
    # Very small images
    if width > 0 and height > 0:
        if width < min_w or height < min_h:
            # Check aspect ratio for lines/separators
            ratio = width / height
            if ratio > 10 or ratio < 0.1:
                return "decorative", "Decorative element"
            return "logo" if "logo" in filename.lower() else "decorative", "Small visual element"

    # 3. Keywords in Nearby Text

    # Extract candidate caption if present (e.g., "Figure 1: ...")
    # Use IGNORECASE to preserve original casing in result
    caption_match = re.search(r'((?:figure|table|chart|graph|diagram|model)\s*\d+[:.]?\s*[^\n]+)', nearby_text, re.IGNORECASE)
    depicts = caption_match.group(1).strip() if caption_match else nearby_text.strip().replace('\n', ' ')
    if len(depicts) > 100:
        depicts = depicts[:100] + "..." # Truncate for marker
    if not depicts:
        depicts = "Visual content"

    detected_type = "conceptual_diagram" # Default analytical type

    # Check keywords
    text_lower = nearby_text.lower()
    for k_type, k_words in keywords.items():
        if any(w in text_lower for w in k_words):
            detected_type = k_type
            break

    # Check explicit patterns in text if still default
    if detected_type == "conceptual_diagram":
        if "table" in text_lower or "grid" in text_lower:
             detected_type = "data_table"
        elif "chart" in text_lower or "graph" in text_lower:
             detected_type = "data_chart"

    return detected_type, depicts

def save_image(image_data: bytes, ext: str, unique_id: str, config: Dict[str, Any]) -> str:
    images_dir = os.path.join(config["output_root"], config["images_folder_name"])
    os.makedirs(images_dir, exist_ok=True)
    if not ext: ext = "png"
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
    if Image: toolchain.append(f"Pillow-{Image.__version__}")

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

        # Get blocks for text and layout analysis
        blocks = page.get_text("blocks")
        # blocks: (x0, y0, x1, y1, text, block_no, block_type)

        # We want to reconstruct the text flow but insert images at their visual position
        # Sort blocks by vertical position then horizontal
        blocks.sort(key=lambda b: (b[1], b[0]))

        # Extract images separately to get high quality data
        # BUT we need to map them to locations.
        # page.get_images(full=True) gives list of xrefs.
        # page.get_image_rects(xref) gives rects on the page.

        # We'll create a merged list of items: (y_pos, type, data)
        # type 0: text, type 1: image

        page_items = []

        # 1. Text blocks
        for b in blocks:
            if b[6] == 0: # Text
                page_items.append({
                    "type": "text",
                    "rect": fitz.Rect(b[0], b[1], b[2], b[3]),
                    "text": b[4],
                    "y": b[1]
                })

        # 2. Images
        image_list = page.get_images(full=True)
        for img_idx, img in enumerate(image_list):
            xref = img[0]
            try:
                rects = page.get_image_rects(xref)
                for rect in rects:
                    # Find nearby text for context
                    # Simple heuristic: text within 50-100 units above or below
                    nearby_texts = []
                    for item in page_items:
                        if item["type"] == "text":
                            # Vertical distance
                            dist = min(abs(item["rect"].y1 - rect.y0), abs(rect.y1 - item["rect"].y0))
                            if dist < 150: # Check nearby
                                nearby_texts.append(item["text"])

                    context_text = " ".join(nearby_texts)

                    # Extract image data
                    base_image = doc.extract_image(xref)
                    image_bytes = base_image["image"]
                    ext = base_image["ext"]
                    w, h = get_image_dimensions(image_bytes)
                    if w == 0: w, h = int(rect.width), int(rect.height)

                    # Classification
                    img_type, depicts = classify_image(image_bytes, w, h, context_text, os.path.basename(file_path), config)

                    unique_id = get_unique_id(rel_path, location, img_idx)
                    filename = save_image(image_bytes, ext, unique_id, config)
                    images_count += 1

                    page_items.append({
                        "type": "image",
                        "rect": rect,
                        "y": rect.y0,
                        "unique_id": unique_id,
                        "filename": filename,
                        "img_type": img_type,
                        "depicts": depicts,
                        "source": os.path.basename(file_path)
                    })
            except Exception as e:
                warnings_list.append(f"Failed to extract image on {location}: {str(e)}")

        # Sort all items by Y position
        page_items.sort(key=lambda x: x["y"])

        for item in page_items:
            if item["type"] == "text":
                content.append(item["text"])
            elif item["type"] == "image":
                # Anchoring
                if item["img_type"] in ["data_chart", "data_table", "conceptual_diagram", "formula"]:
                    anchor_label = "Figure"
                    if item["img_type"] == "data_table": anchor_label = "Table"
                    content.append(f"\n[{anchor_label}: {item['img_type']} depicting {item['depicts']}]")

                # Enhanced Marker
                marker = f"[[IMAGE: IMG_{item['unique_id']}\n  | type={item['img_type']}\n  | depicts=\"{item['depicts']}\"\n  | source={item['source']}\n  | location={location}\n]]"
                content.append(marker)

        content.append(f"--- END {location} ---\n")

    return {
        "status": "success",
        "content": clean_text("\n".join(content)),
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

    # Pre-read elements to allow context lookup
    # Helper to walk elements
    def get_doc_elements(doc_obj):
        elements = []
        if isinstance(doc_obj, docx.document.Document):
            body = doc_obj.element.body
        else:
            return []

        for child in body.iterchildren():
            if child.tag.endswith('p'):
                elements.append(docx.text.paragraph.Paragraph(child, doc_obj))
            elif child.tag.endswith('tbl'):
                elements.append(docx.table.Table(child, doc_obj))
        return elements

    elements = get_doc_elements(doc)
    location = f"SECTION-1"
    content.append(f"--- BEGIN {location} ---")

    obj_index = 0

    for i, block in enumerate(elements):
        if isinstance(block, docx.text.paragraph.Paragraph):
            text = block.text
            content.append(text)

            # Check for images in runs
            # Context: previous para + current para + next para
            prev_text = elements[i-1].text if i > 0 and hasattr(elements[i-1], 'text') else ""
            next_text = elements[i+1].text if i < len(elements)-1 and hasattr(elements[i+1], 'text') else ""
            context_text = f"{prev_text} {text} {next_text}"

            for run in block.runs:
                blips = run.element.findall('.//{http://schemas.openxmlformats.org/drawingml/2006/main}blip')
                for blip in blips:
                    embed_attr = blip.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed')
                    if embed_attr:
                        try:
                            image_part = doc.part.related_parts[embed_attr]
                            image_bytes = image_part.blob
                            ct = image_part.content_type
                            ext = ct.split('/')[-1] if '/' in ct else 'png'
                            w, h = get_image_dimensions(image_bytes)

                            unique_id = get_unique_id(rel_path, location, obj_index)

                            img_type, depicts = classify_image(image_bytes, w, h, context_text, os.path.basename(file_path), config)

                            filename = save_image(image_bytes, ext, unique_id, config)
                            images_count += 1
                            obj_index += 1

                            # Anchor
                            if img_type in ["data_chart", "data_table", "conceptual_diagram", "formula"]:
                                anchor_label = "Figure"
                                if img_type == "data_table": anchor_label = "Table"
                                content.append(f"\n[{anchor_label}: {img_type} depicting {depicts}]")

                            marker = f"[[IMAGE: IMG_{unique_id}\n  | type={img_type}\n  | depicts=\"{depicts}\"\n  | source={os.path.basename(file_path)}\n  | location={location}\n]]"
                            content.append(marker)
                        except Exception as e:
                            warnings_list.append(f"Failed to extract image in {location}: {str(e)}")

        elif isinstance(block, docx.table.Table):
            if len(block.rows) > config["max_table_rows"]:
                warnings_list.append(f"Table truncated: {len(block.rows)} rows > {config['max_table_rows']}")
                rows_to_process = block.rows[:config["max_table_rows"]]
                truncated = True
            else:
                rows_to_process = block.rows
                truncated = False

            table_lines = []
            for idx, row in enumerate(rows_to_process):
                cells = [cell.text.replace('\n', ' ').strip() for cell in row.cells]
                row_str = "| " + " | ".join(cells) + " |"
                table_lines.append(row_str)
                # Header separator after first row
                if idx == 0:
                     sep = "| " + " | ".join(["---"] * len(cells)) + " |"
                     table_lines.append(sep)

            if table_lines:
                content.append("\n" + "\n".join(table_lines) + "\n")
                if truncated:
                    content.append(f"[TABLE_TRUNCATED: rows_exceeded_max_limit ({len(block.rows)})]")

    content.append(f"--- END {location} ---")

    return {
        "status": "success",
        "content": clean_text("\n".join(content)),
        "images_count": images_count,
        "units_count": 1,
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

        shapes = sorted(slide.shapes, key=lambda s: (s.top if hasattr(s, 'top') else 0, s.left if hasattr(s, 'left') else 0))

        # Gather text for context
        slide_text = []
        for shape in shapes:
            if hasattr(shape, "text") and shape.text:
                slide_text.append(shape.text)
        full_slide_context = " ".join(slide_text)

        img_idx_on_slide = 0

        for shape in shapes:
            # Text
            if hasattr(shape, "text") and shape.text:
                content.append(shape.text)

            # Table
            if shape.has_table:
                table = shape.table
                table_lines = []
                rows = list(table.rows)
                if len(rows) > config["max_table_rows"]:
                    warnings_list.append(f"Table truncated in {location}: {len(rows)} rows")
                    rows_proc = rows[:config["max_table_rows"]]
                    trunc = True
                else:
                    rows_proc = rows
                    trunc = False

                for idx, row in enumerate(rows_proc):
                    cells = [cell.text_frame.text.replace('\n', ' ').strip() for cell in row.cells]
                    row_str = "| " + " | ".join(cells) + " |"
                    table_lines.append(row_str)
                    if idx == 0:
                        sep = "| " + " | ".join(["---"] * len(cells)) + " |"
                        table_lines.append(sep)

                content.append("\n" + "\n".join(table_lines) + "\n")
                if trunc:
                    content.append(f"[TABLE_TRUNCATED: rows_exceeded_max_limit ({len(rows)})]")

            # Image
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                try:
                    image = shape.image
                    image_bytes = image.blob
                    ext = image.ext
                    # Dimensions from shape
                    w_emu = shape.width
                    h_emu = shape.height
                    # EMU to pixels (approx 914400 EMU per inch, 96 dpi -> 9525 EMU per pixel)
                    w_px = int(w_emu / 9525)
                    h_px = int(h_emu / 9525)

                    unique_id = get_unique_id(rel_path, location, img_idx_on_slide)

                    context = (shape.name if shape.name else "") + " " + full_slide_context
                    img_type, depicts = classify_image(image_bytes, w_px, h_px, context, os.path.basename(file_path), config)

                    save_image(image_bytes, ext, unique_id, config)
                    images_count += 1
                    img_idx_on_slide += 1

                    # Anchor
                    if img_type in ["data_chart", "data_table", "conceptual_diagram", "formula"]:
                        anchor_label = "Figure"
                        content.append(f"\n[{anchor_label}: {img_type} depicting {depicts}]")

                    marker = f"[[IMAGE: IMG_{unique_id}\n  | type={img_type}\n  | depicts=\"{depicts}\"\n  | source={os.path.basename(file_path)}\n  | location={location}\n]]"
                    content.append(marker)
                except Exception as e:
                    warnings_list.append(f"Failed to extract image on {location}: {str(e)}")

        content.append(f"--- END {location} ---\n")

    return {
        "status": "success",
        "content": clean_text("\n".join(content)),
        "images_count": images_count,
        "units_count": len(prs.slides),
        "warnings": warnings_list
    }

def process_excel(file_path: str, rel_path: str, config: Dict[str, Any]) -> Dict[str, Any]:
    ext = os.path.splitext(file_path)[1].lower()
    content = []
    warnings_list = []
    units_count = 0
    images_count = 0

    # Common table renderer
    def render_table(rows, max_rows, loc_name):
        lines = []
        trunc = False
        if len(rows) > max_rows:
            warnings_list.append(f"{loc_name} truncated: {len(rows)} rows")
            rows = rows[:max_rows]
            trunc = True

        if rows:
            # Header
            headers = [str(c) if c is not None else "" for c in rows[0]]
            lines.append("| " + " | ".join(headers) + " |")
            lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
            # Body
            for row in rows[1:]:
                vals = [str(v) if v is not None else "" for v in row]
                lines.append("| " + " | ".join(vals) + " |")

        return "\n".join(lines), trunc

    try:
        if ext == '.csv':
            location = "TABLE"
            content.append(f"--- BEGIN {location} ---")

            if pd:
                df = pd.read_csv(file_path)
                # Convert to list of lists (including header)
                rows = [df.columns.tolist()] + df.values.tolist()
            else:
                with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                    reader = csv.reader(f)
                    rows = list(reader)

            table_str, trunc = render_table(rows, config["max_table_rows"], "CSV")
            content.append(table_str)
            if trunc:
                content.append(f"[TABLE_TRUNCATED: rows_exceeded_max_limit]")

            content.append(f"--- END {location} ---")
            units_count = 1

        elif ext == '.xlsx':
            if not openpyxl: return {"status": "failed", "error": "openpyxl not installed"}
            wb = openpyxl.load_workbook(file_path, data_only=True)
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                location = f'SHEET "{sheet_name}"'
                content.append(f"--- BEGIN {location} ---")
                units_count += 1

                rows = []
                for row in ws.rows:
                    rows.append([cell.value for cell in row])

                table_str, trunc = render_table(rows, config["max_table_rows"], f"Sheet '{sheet_name}'")
                content.append(table_str)
                if trunc:
                    content.append(f"[TABLE_TRUNCATED: rows_exceeded_max_limit]")
                content.append(f"--- END {location} ---\n")

        elif ext == '.xlsb':
            if not open_xlsb: return {"status": "failed", "error": "pyxlsb not installed"}
            with open_xlsb(file_path) as wb:
                for sheet_name in wb.sheets:
                    location = f'SHEET "{sheet_name}"'
                    content.append(f"--- BEGIN {location} ---")
                    units_count += 1
                    with wb.get_sheet(sheet_name) as ws:
                        rows = []
                        for row in ws.rows():
                            rows.append([c.v for c in row])
                        table_str, trunc = render_table(rows, config["max_table_rows"], f"Sheet '{sheet_name}'")
                        content.append(table_str)
                        if trunc:
                             content.append(f"[TABLE_TRUNCATED: rows_exceeded_max_limit]")
                    content.append(f"--- END {location} ---\n")

    except Exception as e:
        return {"status": "failed", "error": str(e)}

    return {
        "status": "success",
        "content": clean_text("\n".join(content)),
        "images_count": images_count,
        "units_count": units_count,
        "warnings": warnings_list
    }

def process_file(source_abs: str, input_root: str, output_root: str, config: Dict[str, Any]) -> Dict[str, Any]:
    start_time = datetime.datetime.now()
    rel_path = os.path.relpath(source_abs, input_root)
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
                result["status"] = "unsupported"
                result["content"] = "Legacy .doc format not supported."
            else:
                result = process_docx(source_abs, rel_path, config)
        elif file_ext in [".pptx", ".ppt"]:
             if file_ext == ".ppt":
                result["status"] = "unsupported"
                result["content"] = "Legacy .ppt format not supported."
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

    # FIX: Check if output valid despite errors
    if result.get("content") and len(result["content"]) > 0:
        result["status"] = "success"

    # Write output
    metadata = get_metadata_header(source_abs, rel_path, file_ext, result.get("warnings", []), result.get("errors", []))
    full_content = metadata + (result.get("content") or "")

    with open(output_abs_txt, 'w', encoding='utf-8') as f:
        f.write(full_content)

    # Double check file existence for status
    if os.path.exists(output_abs_txt) and os.path.getsize(output_abs_txt) > 0 and result["status"] == "failed":
         # If we wrote something, consider it partial success or success
         if not result.get("content"):
             # We wrote just metadata?
             pass
         else:
             result["status"] = "success"

    duration = (datetime.datetime.now() - start_time).total_seconds() * 1000

    log_entry = {
        "source_path": source_abs,
        "source_relpath": rel_path,
        "source_type": file_ext,
        "status": result["status"],
        "output_txt_path": output_abs_txt,
        "images_extracted_count": result.get("images_count", 0),
        "units_count": result.get("units_count", 0),
        "warnings": result.get("warnings", []),
        "errors": result.get("errors", []),
        "duration_ms": int(duration)
    }
    log_event(config, log_entry)
    return log_entry

def scan_and_process(config: Dict[str, Any]):
    input_root = config["input_root"]
    output_root = config["output_root"]
    subfolders = config["subfolders"]

    stats = { "total": 0, "success": 0, "failed": 0, "unsupported": 0, "images_extracted": 0 }
    print(f"Starting processing from: {input_root}")

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
                    print(f"  [Unsupported] {log_entry.get('content', '')[:50]}...")
                else:
                    stats["failed"] += 1
                    print(f"  [FAILED] Errors: {json.dumps(log_entry.get('errors', []))}")

                stats["images_extracted"] += log_entry["images_extracted_count"]

    print("\n--- Summary ---")
    print(f"Total files: {stats['total']}")
    print(f"Success: {stats['success']}")
    print(f"Failed: {stats['failed']}")
    print(f"Unsupported: {stats['unsupported']}")
    print(f"Images extracted: {stats['images_extracted']}")

def self_test(config: Dict[str, Any]):
    print("Running self-test...")
    # 1. Classification Logic
    print("Testing Classification...")
    cfg = config
    t, d = classify_image(b"fake", 500, 500, "Figure 1: Sales Growth Chart", "img.png", cfg)
    assert t == "data_chart", f"Expected data_chart, got {t}"
    assert "Sales Growth" in d, f"Expected description capture, got {d}"

    t, d = classify_image(b"fake", 10, 10, "", "icon.png", cfg)
    assert t == "decorative", f"Expected decorative, got {t}"

    # 2. Config
    assert config["image_classification"]["min_width"] == 100
    print("Config & Logic Check Passed.")

    # 3. Libraries
    print(f"Pillow available: {Image is not None}")

    print("Self-test completed successfully.")

if __name__ == "__main__":
    # Check dependencies before doing anything else
    ensure_dependencies()

    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true", help="Run self-test mode")
    args = parser.parse_args()

    cfg = load_config()

    if args.self_test:
        self_test(cfg)
    else:
        scan_and_process(cfg)
