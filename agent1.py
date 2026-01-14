import os
import sys
import json
import time
import logging
import argparse
import subprocess
import glob
import re
import math
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("Agent1")

# --- Constants & Configuration ---

DEFAULT_CONFIG = {
  "internal_sources_dir": r"C:\Projects Python\Normalisation\Normalised Data\Internal Source files",
  "questions_dir": r"C:\Projects Python\Normalisation\Normalised Data\Questions",
  "images_dir": r"C:\Projects Python\Normalisation\Normalised Data\images",
  "output_dir": r"C:\Projects Python\Normalisation\Output\Agent 1",
  "openai_model": "gpt-5.2",
  "max_chars_per_chunk": 120000,
  "external_research_mandatory": True,
  "max_web_sources_per_question": 6,
  "download_pdfs": True,
  "max_pdf_pages": 500,
  "max_html_chars": 250000,
  "request_timeout_sec": 120,
  "overwrite": True,
  "auto_install_dependencies": True
}

# --- Dependency Management ---

def ensure_dependencies(config_path="config.json", self_test=False):
    """Checks for dependencies and installs them if configured."""
    if self_test:
        logger.info("Self-test mode: Skipping dependency installation.")
        return

    # Load config to check auto_install policy
    auto_install = True
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                c = json.load(f)
                auto_install = c.get("auto_install_dependencies", True)
        except: pass

    missing = False
    try:
        import openai
        import requests
        import bs4
        import fitz
        import googlesearch
        import tenacity
    except ImportError:
        missing = True

    if missing:
        if auto_install:
            logger.info("Installing dependencies from requirements.txt...")
            req_path = "requirements.txt"
            if not os.path.exists(req_path):
                logger.error("requirements.txt not found. Cannot auto-install.")
                sys.exit(1)
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", req_path])
                logger.info("Dependencies installed successfully. Restarting script...")
                os.execv(sys.executable, ['python'] + sys.argv)
            except subprocess.CalledProcessError as e:
                logger.error(f"Failed to install dependencies: {e}")
                sys.exit(1)
        else:
            logger.error("Missing dependencies. Set 'auto_install_dependencies': true or install manually.")
            sys.exit(1)

# Perform check BEFORE importing external modules
# We need to parse args briefly to check for --self-test before main execution
if __name__ == "__main__":
    is_self_test = "--self-test" in sys.argv
    ensure_dependencies(self_test=is_self_test)

# --- Imports (Safe now) ---
try:
    from openai import OpenAI
    from tenacity import retry, stop_after_attempt, wait_exponential
    import requests
    from bs4 import BeautifulSoup, Comment
    import fitz  # PyMuPDF
    from googlesearch import search as google_search
except ImportError:
    # Handle missing dependencies for self-test mode to avoid crashing on definition
    if "--self-test" in sys.argv:
        # Define dummies so class definitions don't crash
        def retry(*args, **kwargs):
            return lambda f: f
        def stop_after_attempt(*args): return None
        def wait_exponential(*args, **kwargs): return None
        # Mock other modules if strictly needed, but Python is dynamic so generally fine unless used at top-level
        # Only decorators like @retry are evaluated at definition time.
    else:
        # Should have been handled by ensure_dependencies
        logger.error("Dependencies missing and not in self-test mode.")
        sys.exit(1)

# --- Helper Classes ---

class ConfigManager:
    def __init__(self, config_path="config.json"):
        self.config_path = config_path
        self.config = self.load_config()

    def load_config(self):
        if not os.path.exists(self.config_path):
            logger.error(f"Config file not found: {self.config_path}")
            sys.exit(1)
        with open(self.config_path, 'r') as f:
            return json.load(f)

    def get(self, key, default=None):
        return self.config.get(key, default)

    @property
    def internal_sources_dir(self): return Path(self.config['internal_sources_dir'])
    @property
    def questions_dir(self): return Path(self.config['questions_dir'])
    @property
    def images_dir(self): return Path(self.config['images_dir'])
    @property
    def output_dir(self): return Path(self.config['output_dir'])

class LLMClient:
    def __init__(self, config):
        self.api_key = os.environ.get("OPENAI_API_KEY")
        if not self.api_key and "--self-test" not in sys.argv:
            logger.error("OPENAI_API_KEY environment variable is missing.")
            print("\nPlease set OPENAI_API_KEY environment variable.\n")
            sys.exit(1)

        if self.api_key:
            self.client = OpenAI(api_key=self.api_key)
        else:
            self.client = None # For self-test

        self.model = config.get("openai_model", "gpt-5.2")
        self.timeout = config.get("request_timeout_sec", 120)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def call_llm(self, system_prompt: str, user_content: str, json_mode=False) -> str:
        """Calls OpenAI ChatCompletion with retries."""
        if not self.client:
             raise RuntimeError("OpenAI Client not initialized (missing API key?)")

        try:
            kwargs = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                "temperature": 0.2,
                "timeout": self.timeout
            }
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}

            response = self.client.chat.completions.create(**kwargs)
            return response.choices[0].message.content
        except Exception as e:
            logger.error(f"LLM Call failed: {e}")
            raise

class FileManager:
    def __init__(self, config: ConfigManager):
        self.config = config
        self.artifacts_dir = self.config.output_dir / "artifacts"
        self.external_dir = self.config.output_dir / "external_sources"
        self.run_log_path = self.config.output_dir / "run_log.jsonl"

        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.external_dir.mkdir(parents=True, exist_ok=True)

    def read_internal_files(self) -> Dict[str, str]:
        files = {}
        path = self.config.internal_sources_dir
        if not path.exists():
            return {}
        # Recursive glob
        for p in path.rglob("*.txt"):
            try:
                # Use relative path for provenance
                rel_path = p.relative_to(path).as_posix()
                files[rel_path] = p.read_text(encoding='utf-8', errors='replace')
            except Exception as e:
                logger.warning(f"Could not read {p}: {e}")
        return files

    def read_question_files(self) -> Dict[str, str]:
        files = {}
        path = self.config.questions_dir
        if not path.exists():
            return {}
        for p in path.glob("*.txt"):
             try:
                files[p.name] = p.read_text(encoding='utf-8', errors='replace')
             except Exception as e:
                logger.warning(f"Could not read {p}: {e}")
        return files

    def save_artifact(self, filename: str, data: Any):
        path = self.artifacts_dir / filename
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        logger.info(f"Saved artifact: {filename}")

    def log_run(self, step: str, details: Dict):
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "step": step,
            "details": details
        }
        with open(self.run_log_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry) + "\n")

    def save_text(self, filename: str, content: str):
        path = self.config.output_dir / filename
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)

    def parse_with_page_markers(self, filename: str, content: str) -> List[Dict]:
        """
        Splits content by '--- BEGIN PAGE n ---' markers.
        Returns list of dicts: {'text': ..., 'page': n, 'file': filename}
        If no markers, treats as Page 1.
        """
        chunks = []
        # Regex to find markers
        # Assumes format: --- BEGIN PAGE 1 ---
        # We split keeping the delimiter to parse it
        parts = re.split(r'(--- BEGIN PAGE \d+ ---)', content)

        current_page = "1"
        current_text = ""

        # If the file doesn't start with a marker, the first chunk is Page 1 (or 0, but let's say 1)
        if parts and not parts[0].startswith('--- BEGIN PAGE'):
            current_text = parts[0]
            # Check for images in this preamble
            images = re.findall(r'\[\[IMAGE:\s*(IMG_[a-fA-F0-9]+)\]\]', current_text)
            chunks.append({
                "file": filename,
                "page": current_page,
                "text": current_text.strip(),
                "images": images
            })
            parts = parts[1:]

        for i in range(0, len(parts), 2):
            if i+1 < len(parts):
                marker = parts[i]
                text = parts[i+1]

                # Extract page number
                m = re.search(r'PAGE (\d+)', marker)
                if m:
                    current_page = m.group(1)

                images = re.findall(r'\[\[IMAGE:\s*(IMG_[a-fA-F0-9]+)\]\]', text)
                chunks.append({
                    "file": filename,
                    "page": current_page,
                    "text": text.strip(),
                    "images": images
                })

        if not chunks and content.strip():
             # Fallback if regex failed but content exists
             images = re.findall(r'\[\[IMAGE:\s*(IMG_[a-fA-F0-9]+)\]\]', content)
             chunks.append({"file": filename, "page": "1", "text": content, "images": images})

        return chunks

class WebResearcher:
    def __init__(self, config: ConfigManager):
        self.config = config
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        self.max_html_chars = self.config.get("max_html_chars", 250000)
        self.max_pdf_pages = self.config.get("max_pdf_pages", 500)

    def search(self, query: str, num_results: int = 5) -> List[str]:
        try:
            results = list(google_search(query, num_results=num_results, lang="en"))
            return results
        except Exception as e:
            logger.error(f"Search failed for '{query}': {e}")
            return []

    def download_and_extract(self, url: str, output_dir: Path) -> Dict[str, Any]:
        result = {
            "url": url,
            "access_date": datetime.now(timezone.utc).isoformat(),
            "title": "",
            "local_path": "",
            "success": False,
            "error": None,
            "truncated": False
        }

        try:
            response = requests.get(url, headers=self.headers, timeout=30)
            response.raise_for_status()

            content_type = response.headers.get('Content-Type', '').lower()
            filename_base = re.sub(r'[^a-zA-Z0-9]', '_', url)[-50:]
            if not filename_base: filename_base = "source"

            if 'application/pdf' in content_type or url.lower().endswith('.pdf'):
                filename = f"{filename_base}.pdf"
                local_path = output_dir / filename
                with open(local_path, 'wb') as f:
                    f.write(response.content)

                text = self._extract_pdf_text(local_path)
                result["local_path"] = str(local_path)
                result["success"] = True
                result["extracted_text_path"] = str(output_dir / f"{filename_base}.txt")
                with open(result["extracted_text_path"], 'w', encoding='utf-8') as f:
                    f.write(text)

            else:
                filename = f"{filename_base}.html"
                local_path = output_dir / filename
                with open(local_path, 'w', encoding='utf-8', errors='replace') as f:
                    f.write(response.text)

                soup = BeautifulSoup(response.text, 'html.parser')
                result["title"] = soup.title.string if soup.title else "No Title"

                # Strip scripts and styles
                for element in soup(["script", "style", "nav", "footer", "header"]):
                    element.decompose()

                # Remove comments
                for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
                    comment.extract()

                text = soup.get_text(separator='\n')
                text = re.sub(r'\n\s*\n', '\n\n', text).strip() # clean whitespace

                if len(text) > self.max_html_chars:
                    text = text[:self.max_html_chars] + "\n\n[TRUNCATED]"
                    result["truncated"] = True

                result["local_path"] = str(local_path)
                result["success"] = True
                result["extracted_text_path"] = str(output_dir / f"{filename_base}.txt")
                with open(result["extracted_text_path"], 'w', encoding='utf-8') as f:
                    f.write(text)

        except Exception as e:
            result["error"] = str(e)
            logger.warning(f"Download failed for {url}: {e}")

        return result

    def _extract_pdf_text(self, pdf_path: Path) -> str:
        text = ""
        try:
            doc = fitz.open(pdf_path)
            for i, page in enumerate(doc):
                if i >= self.max_pdf_pages:
                    text += "\n[PDF TRUNCATED: Max pages reached]"
                    break
                text += f"\n--- PDF PAGE {i+1} ---\n"
                text += page.get_text()
            doc.close()
        except Exception as e:
            text += f"\n[PDF READ ERROR: {e}]"
        return text

# --- Prompts ---

PROMPT_PERSONA = """You are Deep Research Agent 1, an expert academic researcher and MBA examiner.
Your domain covers management, business, economics, finance, and accounting.
You are methodical, comprehensive, and defensible.
You prioritise correctness, evidence, and rigorous reasoning over style.
"""

# --- Orchestrator ---

class Orchestrator:
    def __init__(self):
        self.config_manager = ConfigManager()
        self.file_manager = FileManager(self.config_manager)
        # LLM initialized later or checked
        if "--self-test" not in sys.argv:
             self.llm = LLMClient(self.config_manager.config)
             self.web = WebResearcher(self.config_manager)

    def run(self):
        logger.info("Starting Agent 1 Pipeline...")
        start_time = time.time()

        # STEP 1
        logger.info("STEP 1: Internal Ingestion & Memory Creation")
        internal_files = self.file_manager.read_internal_files()

        # Process into page-aware chunks
        all_chunks = []
        for fname, content in internal_files.items():
            chunks = self.file_manager.parse_with_page_markers(fname, content)
            all_chunks.extend(chunks)

        internal_index = self.step_1_ingest(all_chunks)
        self.file_manager.log_run("step_1", {"files": len(internal_files), "chunks": len(all_chunks)})

        # STEP 2
        logger.info("STEP 2: Synopsis Generation")
        synopsis = self.step_2_synopsis(internal_index)
        self.file_manager.log_run("step_2", {"synopsis_len": len(synopsis)})

        # STEP 3
        logger.info("STEP 3: Question Extraction")
        raw_questions = self.file_manager.read_question_files()
        if not raw_questions:
            logger.error("No question files found.")
            sys.exit(1)
        questions_struct = self.step_3_questions(raw_questions)
        self.file_manager.log_run("step_3", {"questions_count": len(questions_struct)})

        # STEP 4
        logger.info("STEP 4: Internal Evidence Mapping")
        evidence_map = self.step_4_internal_mapping(all_chunks, questions_struct, internal_index)
        self.file_manager.log_run("step_4", {"mapped_questions": len(evidence_map)})

        # STEP 5
        logger.info("STEP 5: External Research Planning")
        search_plan = self.step_5_plan_search(questions_struct, evidence_map)
        self.file_manager.log_run("step_5", {"plan_items": len(search_plan)})

        # STEP 6
        logger.info("STEP 6: External Research Execution")
        external_sources_index = self.step_6_execute_search(search_plan)
        self.file_manager.log_run("step_6", {"sources_downloaded": len(external_sources_index)})

        # STEP 7
        logger.info("STEP 7: External Evidence Mapping")
        evidence_map = self.step_7_external_mapping(external_sources_index, questions_struct, evidence_map)
        self.file_manager.log_run("step_7", {"updated_map": True})

        # STEP 8
        logger.info("STEP 8: Final Answer Synthesis")
        final_answers = self.step_8_answers(questions_struct, evidence_map, synopsis)
        self.file_manager.log_run("step_8", {"answers_generated": len(final_answers)})

        # STEP 9
        logger.info("STEP 9: Adequacy Check")
        final_answers = self.step_9_check(final_answers, questions_struct)
        self.file_manager.log_run("step_9", {"checked": True})

        # STEP 10
        logger.info("STEP 10: Output Generation")
        self.step_10_output(synopsis, final_answers, evidence_map)
        self.file_manager.log_run("step_10", {"duration": time.time() - start_time})

        logger.info("Agent 1 Pipeline Complete.")

    def step_1_ingest(self, chunks: List[Dict]) -> Dict:
        combined_index = {
            "definitions": [], "theories": [], "formulas": [],
            "examples": [], "image_markers": [], "provenance": []
        }

        # Track unique files for provenance list
        files_seen = set()

        prompt_tmpl = PROMPT_PERSONA + """
        Analyze the text from file '{filename}' (Page {page}).
        Extract: Definitions, Theories/Frameworks, Formulas, Examples.
        Also note the pre-extracted image markers: {images}.
        Return JSON keys: definitions, theories, formulas, examples.
        For images, return a list 'images_context' describing what the image likely depicts based on surrounding text.
        """

        for c in chunks:
            fname = c['file']
            page = c['page']
            text = c['text']
            imgs = c['images']
            files_seen.add(fname)

            # If text is very long, might need sub-chunking, but let's assume page-sized chunks are okay for index
            # If > max_chars, we truncate or split. Config says 120k chars per chunk for LLM, usually page is smaller.

            response_str = self.llm.call_llm(
                system_prompt="Indexing engine. JSON only.",
                user_content=prompt_tmpl.format(filename=fname, page=page, images=imgs) + f"\n\nTEXT:\n{text}",
                json_mode=True
            )
            try:
                data = json.loads(response_str)
                for key in ["definitions", "theories", "formulas", "examples"]:
                    if key in data and isinstance(data[key], list):
                        for item in data[key]:
                            if isinstance(item, str): item = {'text': item}
                            item['source'] = fname
                            item['page'] = page
                            combined_index[key].append(item)

                # Handle Images
                # We want to store image markers with context
                # The chunks already have the IDs. The LLM gives context.
                if imgs:
                    img_contexts = data.get("images_context", [])
                    # Map loosely by index or just dump
                    for i, img_id in enumerate(imgs):
                        ctx = img_contexts[i] if i < len(img_contexts) else "No context extracted"
                        combined_index["image_markers"].append({
                            "image_id": img_id,
                            "source": fname,
                            "page": page,
                            "context": ctx
                        })

            except Exception as e:
                logger.warning(f"Index error {fname} p{page}: {e}")

        combined_index["provenance"] = list(files_seen)
        self.file_manager.save_artifact("internal_index.json", combined_index)
        return combined_index

    def step_2_synopsis(self, index: Dict) -> str:
        # Simplified index for synopsis
        short_index = {k: len(v) for k,v in index.items() if k!="provenance"}
        short_index["topics"] = [x['text'][:50] for x in index['definitions'][:20]] # sample

        return self.llm.call_llm(
            system_prompt=PROMPT_PERSONA,
            user_content=f"INTERNAL INDEX STATS:\n{json.dumps(short_index)}\n\nWrite a comprehensive Synopsis of the material."
        )

    def step_3_questions(self, raw_files: Dict[str, str]) -> List[Dict]:
        questions_text = "\n".join([f"--- FILE: {k} ---\n{v}" for k, v in raw_files.items()])
        prompt = """Extract questions. Return JSON: {"questions": [{"id": "1", "text": "...", "subparts": [...]}]}."""
        response = self.llm.call_llm(
            system_prompt="Question extractor. JSON only.",
            user_content=f"{questions_text}\n\n{prompt}",
            json_mode=True
        )
        try:
            data = json.loads(response)
            qs = data.get("questions", [])
            self.file_manager.save_artifact("questions.json", qs)
            return qs
        except: return []

    def step_4_internal_mapping(self, all_chunks: List[Dict], questions: List[Dict], index: Dict) -> Dict:
        evidence_map = {q['id']: {"internal_evidence": [], "external_evidence": [], "image_evidence": [], "analysis": ""} for q in questions}

        # Prepare chunks for retrieval (simple keyword overlap)
        # We need to sub-chunk large pages if needed, but for now we search page-level

        for q in questions:
            q_text = (q['text'] + " " + " ".join(q.get('subparts', []))).lower()
            q_tokens = set(q_text.split())

            scored = []
            for c in all_chunks:
                # Simple score
                score = sum(1 for t in q_tokens if t in c['text'].lower())
                scored.append((score, c))

            # Top 5 pages
            scored.sort(key=lambda x: x[0], reverse=True)
            top_chunks = [x[1] for x in scored[:5]]

            snippets = ""
            for c in top_chunks:
                snippets += f"\n--- Source: {c['file']} (Page {c['page']}) ---\n"
                snippets += f"Image Markers: {c['images']}\n"
                snippets += f"{c['text']}\n"

            prompt = PROMPT_PERSONA + """
            Analyze the provided snippets against the Question.
            Identify relevant text evidence AND image evidence.

            Return JSON:
            {
              "relevant_text": [{"source": "filename", "page": "n", "text_excerpt": "...", "what_it_supports": "..."}],
              "relevant_images": [{"image_id": "IMG_...", "source": "filename", "page": "n", "reason": "..."}]
            }
            """

            response = self.llm.call_llm(
                system_prompt="Evidence Mapper. JSON only.",
                user_content=f"Q: {q['text']}\nSNIPPETS:\n{snippets}\n\n{prompt}",
                json_mode=True
            )
            try:
                data = json.loads(response)
                evidence_map[q['id']]['internal_evidence'] = data.get('relevant_text', [])
                evidence_map[q['id']]['image_evidence'] = data.get('relevant_images', [])
            except: pass

        self.file_manager.save_artifact("evidence_map.json", evidence_map)
        return evidence_map

    def step_5_plan_search(self, questions: List[Dict], evidence_map: Dict) -> List[Dict]:
        ctx = [{"id": q['id'], "text": q['text'], "has_internal": len(evidence_map[q['id']]['internal_evidence'])>0} for q in questions]
        response = self.llm.call_llm(
            system_prompt="Research Planner. JSON only.",
            user_content=f"STATUS:\n{json.dumps(ctx)}\n\nGenerate search queries. JSON: {{'search_plan': [{{'question_id': '...', 'queries': [...]}}]}}",
            json_mode=True
        )
        try:
            plan = json.loads(response).get("search_plan", [])
            self.file_manager.save_artifact("search_plan.json", plan)
            return plan
        except: return []

    def step_6_execute_search(self, plan: List[Dict]) -> List[Dict]:
        ext_index = []
        for item in plan:
            seen = set()
            for q in item['queries']:
                for url in self.web.search(q, 3):
                    if url in seen: continue
                    seen.add(url)
                    logger.info(f"Downloading {url}")
                    res = self.web.download_and_extract(url, self.file_manager.external_dir)
                    res['question_id_target'] = item['question_id']
                    ext_index.append(res)
        self.file_manager.save_artifact("external_sources_index.json", ext_index)
        return ext_index

    def step_7_external_mapping(self, external_index: List[Dict], questions: List[Dict], evidence_map: Dict) -> Dict:
        grouped = {}
        for d in external_index:
            if d['success']: grouped.setdefault(d.get('question_id_target'), []).append(d)

        for q in questions:
            qid = q['id']
            if qid not in grouped: continue
            text_context = ""
            for d in grouped[qid]:
                try:
                    with open(d['extracted_text_path'], encoding='utf-8') as f:
                        content = f.read()[:10000] # Limit context
                    text_context += f"\nSource: {d['url']}\nAccess Date: {d['access_date']}\n{content}\n"
                except: pass

            prompt = PROMPT_PERSONA + """
            Analyze EXTERNAL text. Extract evidence.
            Return JSON: {"external_evidence": [{"url": "...", "access_date": "...", "text_excerpt": "...", "what_it_supports": "..."}]}
            """

            response = self.llm.call_llm(
                system_prompt="Evidence Mapper. JSON only.",
                user_content=f"Q: {q['text']}\nTEXT:\n{text_context}\n\n{prompt}",
                json_mode=True
            )
            try:
                if qid in evidence_map:
                    evidence_map[qid]['external_evidence'] = json.loads(response).get('external_evidence', [])
            except: pass
        self.file_manager.save_artifact("evidence_map.json", evidence_map)
        return evidence_map

    def step_8_answers(self, questions: List[Dict], evidence_map: Dict, synopsis: str) -> Dict:
        answers = {}
        for q in questions:
            ev = evidence_map.get(q['id'], {})

            # Format evidence for prompt
            internal_str = json.dumps(ev.get('internal_evidence', []))
            external_str = json.dumps(ev.get('external_evidence', []))
            image_str = json.dumps(ev.get('image_evidence', []))

            response = self.llm.call_llm(
                system_prompt=PROMPT_PERSONA,
                user_content=f"""SYNOPSIS: {synopsis}
                Q: {q['text']}
                INTERNAL TEXT EVIDENCE: {internal_str}
                INTERNAL IMAGE EVIDENCE: {image_str}
                EXTERNAL EVIDENCE: {external_str}

                Write academic answer. Cite images as [IMAGE: IMG_xxx]. Cite text as [internal: file p.x] or [external: url].
                """
            )
            answers[q['id']] = response
        return answers

    def step_9_check(self, answers: Dict, questions: List[Dict]) -> Dict:
        for q in questions:
            qid = q['id']
            resp = self.llm.call_llm(
                system_prompt="Examiner.",
                user_content=f"Q: {q['text']}\nANS: {answers.get(qid)}\n\nCritique. If ok, return PASS. Else return improved answer."
            )
            if "PASS" not in resp[:10]: answers[qid] = resp
        return answers

    def step_10_output(self, synopsis: str, answers: Dict, evidence_map: Dict):
        lines = ["=== SYNOPSIS ===", synopsis, "\n=== QUESTIONS & ANSWERS ==="]
        trace_lines = ["source_type | source | location | what_it_supports"]

        for qid in sorted(answers.keys()):
            lines.extend([f"\n--- QUESTION {qid} ---", f"--- ANSWER {qid} ---", answers[qid]])
            ev = evidence_map.get(qid, {})

            # Internal Text
            for i in ev.get('internal_evidence', []):
                loc = f"Page {i.get('page', '?')}"
                supp = i.get('what_it_supports', 'Evidence')
                trace_lines.append(f"internal | {i.get('source')} | {loc} | {supp}")

            # Internal Images
            for i in ev.get('image_evidence', []):
                loc = f"Page {i.get('page', '?')}"
                supp = i.get('reason', 'Image Evidence')
                trace_lines.append(f"image | {i.get('image_id')} | {loc} | {supp}")

            # External
            for i in ev.get('external_evidence', []):
                loc = i.get('access_date', 'Unknown Date')
                supp = i.get('what_it_supports', 'Evidence')
                trace_lines.append(f"external | {i.get('url')} | {loc} | {supp}")

        self.file_manager.save_text("report.txt", "\n".join(lines))
        self.file_manager.save_text("citations_trace.txt", "\n".join(trace_lines))

# --- Self Test ---

def run_self_test():
    logger.info("Running self-test...")
    if not os.path.exists("config.json"):
        logger.error("FAIL: config.json missing")
        sys.exit(1)
    with open("config.json") as f: conf = json.load(f)

    # Check paths
    for p in [conf['internal_sources_dir'], conf['questions_dir'], conf['images_dir']]:
        if not os.path.exists(p): logger.warning(f"WARNING: Path missing: {p}")
        else: logger.info(f"OK: Path exists: {p}")

    # Check output
    try:
        os.makedirs(conf['output_dir'], exist_ok=True)
        t = os.path.join(conf['output_dir'], "test.tmp")
        with open(t, 'w') as f: f.write("x")
        os.remove(t)
        logger.info("OK: Output writable")
    except: logger.error("FAIL: Output not writable")

    # Check API Key
    if os.environ.get("OPENAI_API_KEY"): logger.info("OK: API Key present")
    else: logger.error("FAIL: API Key missing")

    # Check dependencies (should be present if ensure_dependencies ran or env is good)
    try:
        import openai
        import requests
        import bs4
        import fitz
        import googlesearch
        import tenacity
        logger.info("OK: Dependencies importable")
    except ImportError as e:
        logger.error(f"FAIL: Missing dependencies: {e}")

    logger.info("Self-test complete.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        run_self_test()
    else:
        Orchestrator().run()
