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

# --- Dependency Management ---

REQUIRED_PACKAGES = [
    "openai",
    "requests",
    "beautifulsoup4", # import bs4
    "pymupdf",        # import fitz
    "googlesearch-python", # import googlesearch
    "tenacity"
]

def install_packages():
    """Installs required packages using pip."""
    logger.info("Installing dependencies...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install"] + REQUIRED_PACKAGES)
        logger.info("Dependencies installed successfully.")
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to install dependencies: {e}")
        sys.exit(1)

def ensure_dependencies(config_path="config.json"):
    """Checks for dependencies and installs them if configured."""
    # Load config just for this check
    auto_install = True
    if os.path.exists(config_path):
        with open(config_path, 'r') as f:
            c = json.load(f)
            auto_install = c.get("auto_install_dependencies", True)

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
            install_packages()
            # Restart script
            logger.info("Restarting script to apply changes...")
            os.execv(sys.executable, ['python'] + sys.argv)
        else:
            logger.error("Missing dependencies. Set 'auto_install_dependencies': true in config or install manually.")
            sys.exit(1)

# Perform check BEFORE importing external modules
if __name__ == "__main__" or "pytest" not in sys.modules:
    # We run this at top level, unless testing?
    # Actually, for the script to be importable without side effects, we usually guard this.
    # But for a standalone agent script, running it here is fine.
    # However, to avoid 'self-test' flag issues, we should be careful.
    # If self-test is requesting NO install (unlikely), we still need imports.
    # We will just run it.
    ensure_dependencies()

# --- Imports (Safe now) ---
try:
    from openai import OpenAI
    from tenacity import retry, stop_after_attempt, wait_exponential
    import requests
    from bs4 import BeautifulSoup
    import fitz  # PyMuPDF
    from googlesearch import search as google_search
except ImportError:
    # Should not happen if ensure_dependencies worked
    logger.error("Failed to import dependencies after installation check.")
    sys.exit(1)


# --- Constants & Config ---

DEFAULT_CONFIG = {
  "internal_sources_dir": r"C:\Projects Python\Normalisation\Normalised Data\Internal Source files",
  "questions_dir": r"C:\Projects Python\Normalisation\Normalised Data\Questions",
  "images_dir": r"C:\Projects Python\Normalisation\Normalised Data\images",
  "output_dir": r"C:\Projects Python\Normalisation\Output\Agent 1",
  "openai_model": "gpt-4o",
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
        if not self.api_key:
            # Check if self-test mode, if so, we might not need key strictly if we don't call LLM
            # But the requirement says "API key from env var... If missing -> print instructions and exit"
            # Self test checks presence too.
            pass # Validation happens in run/self-test

        if self.api_key:
            self.client = OpenAI(api_key=self.api_key)
        else:
            self.client = None

        self.model = config.get("openai_model", "gpt-4o")
        self.timeout = config.get("request_timeout_sec", 120)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def call_llm(self, system_prompt: str, user_content: str, json_mode=False) -> str:
        """Calls OpenAI ChatCompletion with retries."""
        if not self.client:
             logger.error("OpenAI API Key missing.")
             sys.exit(1)

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
        for p in path.glob("*.txt"):
            try:
                files[p.name] = p.read_text(encoding='utf-8', errors='replace')
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

    def load_artifact(self, filename: str) -> Any:
        path = self.artifacts_dir / filename
        if not path.exists():
            return None
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)

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

    def chunk_text(self, text: str, max_chars: int) -> List[str]:
        return [text[i:i+max_chars] for i in range(0, len(text), max_chars)]

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
            "error": None
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
                text = soup.get_text(separator='\n')

                if len(text) > self.max_html_chars:
                    text = text[:self.max_html_chars] + "\n[TRUNCATED]"

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
        self.llm = LLMClient(self.config_manager.config)
        self.web = WebResearcher(self.config_manager)

    def run(self):
        if not self.llm.client:
            logger.error("API Key missing. Exiting.")
            print("Please set OPENAI_API_KEY env var.")
            sys.exit(1)

        logger.info("Starting Agent 1 Pipeline...")

        # STEP 1
        logger.info("STEP 1: Internal Ingestion & Memory Creation")
        internal_files = self.file_manager.read_internal_files()
        if not internal_files:
            logger.warning("No internal files found.")
            internal_index = {"provenance": []}
        else:
            internal_index = self.step_1_ingest(internal_files)

        # STEP 2
        logger.info("STEP 2: Synopsis Generation")
        synopsis = self.step_2_synopsis(internal_index)

        # STEP 3
        logger.info("STEP 3: Question Extraction")
        raw_questions = self.file_manager.read_question_files()
        if not raw_questions:
            logger.error("No question files found.")
            sys.exit(1)
        questions_struct = self.step_3_questions(raw_questions)

        # STEP 4
        logger.info("STEP 4: Internal Evidence Mapping")
        evidence_map = self.step_4_internal_mapping(internal_files, questions_struct, internal_index)

        # STEP 5
        logger.info("STEP 5: External Research Planning")
        search_plan = self.step_5_plan_search(questions_struct, evidence_map)

        # STEP 6
        logger.info("STEP 6: External Research Execution")
        external_sources_index = self.step_6_execute_search(search_plan)

        # STEP 7
        logger.info("STEP 7: External Evidence Mapping")
        evidence_map = self.step_7_external_mapping(external_sources_index, questions_struct, evidence_map)

        # STEP 8
        logger.info("STEP 8: Final Answer Synthesis")
        final_answers = self.step_8_answers(questions_struct, evidence_map, synopsis)

        # STEP 9
        logger.info("STEP 9: Adequacy Check")
        final_answers = self.step_9_check(final_answers, questions_struct)

        # STEP 10
        logger.info("STEP 10: Output Generation")
        self.step_10_output(synopsis, final_answers, evidence_map)

        logger.info("Agent 1 Pipeline Complete.")

    def step_1_ingest(self, files: Dict[str, str]) -> Dict:
        combined_index = {
            "definitions": [], "theories": [], "formulas": [],
            "examples": [], "image_markers": [], "provenance": []
        }
        prompt_tmpl = PROMPT_PERSONA + """
        Analyze the text from file '{filename}'.
        Extract: Definitions, Theories/Frameworks, Formulas, Examples, Image markers.
        Return JSON keys: definitions, theories, formulas, examples, image_markers.
        """
        for fname, content in files.items():
            combined_index["provenance"].append(fname)
            chunks = self.file_manager.chunk_text(content, self.config_manager.get("max_chars_per_chunk"))
            for chunk in chunks:
                response_str = self.llm.call_llm(
                    system_prompt="Indexing engine. JSON only.",
                    user_content=prompt_tmpl.format(filename=fname) + f"\n\nTEXT:\n{chunk}",
                    json_mode=True
                )
                try:
                    data = json.loads(response_str)
                    for key in combined_index:
                        if key in data and isinstance(data[key], list):
                             for item in data[key]:
                                if isinstance(item, dict): item['source'] = fname
                                elif isinstance(item, str): item = {'text': item, 'source': fname}
                                combined_index[key].append(item)
                except: pass
        self.file_manager.save_artifact("internal_index.json", combined_index)
        return combined_index

    def step_2_synopsis(self, index: Dict) -> str:
        index_str = json.dumps(index)
        if len(index_str) > 100000: index_str = index_str[:100000] + "... [TRUNCATED]"
        return self.llm.call_llm(
            system_prompt=PROMPT_PERSONA,
            user_content=f"INTERNAL INDEX:\n{index_str}\n\nWrite a comprehensive Synopsis."
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

    def step_4_internal_mapping(self, files: Dict[str, str], questions: List[Dict], index: Dict) -> Dict:
        evidence_map = {q['id']: {"internal_evidence": [], "external_evidence": [], "analysis": ""} for q in questions}
        all_chunks = []
        for fname, content in files.items():
            file_chunks = [content[i:i+4000] for i in range(0, len(content), 3000)]
            for i, c in enumerate(file_chunks):
                all_chunks.append({"source": fname, "chunk_id": i, "text": c})

        for q in questions:
            q_tokens = set((q['text'] + " " + " ".join(q.get('subparts', []))).lower().split())
            scored = []
            for chunk in all_chunks:
                score = sum(1 for t in q_tokens if t in chunk['text'].lower())
                scored.append((score, chunk))
            scored.sort(key=lambda x: x[0], reverse=True)
            top_chunks = [x[1] for x in scored[:5]]

            snippets = "\n".join([f"Source: {c['source']}\n{c['text']}" for c in top_chunks])
            response = self.llm.call_llm(
                system_prompt="Evidence Mapper. JSON only.",
                user_content=f"Q: {q['text']}\nSNIPPETS:\n{snippets}\n\nIdentify relevant info. Return JSON: {{'relevant_snippets': [...]}}",
                json_mode=True
            )
            try:
                evidence_map[q['id']]['internal_evidence'] = json.loads(response).get('relevant_snippets', [])
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
            text = ""
            for d in grouped[qid]:
                try:
                    with open(d['extracted_text_path']) as f: text += f"\nSource: {d['url']}\n{f.read()[:10000]}\n"
                except: pass

            response = self.llm.call_llm(
                system_prompt="Evidence Mapper. JSON only.",
                user_content=f"Q: {q['text']}\nTEXT:\n{text}\n\nExtract evidence. JSON: {{'external_evidence': [{{'url': '...', 'text_excerpt': '...', 'support': '...'}}]}}",
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
            response = self.llm.call_llm(
                system_prompt=PROMPT_PERSONA,
                user_content=f"SYNOPSIS: {synopsis}\nQ: {q['text']}\nINTERNAL: {json.dumps(ev.get('internal_evidence'))}\nEXTERNAL: {json.dumps(ev.get('external_evidence'))}\n\nWrite academic answer."
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
        trace = []
        for qid in sorted(answers.keys()):
            lines.extend([f"\n--- QUESTION {qid} ---", f"--- ANSWER {qid} ---", answers[qid]])
            ev = evidence_map.get(qid, {})
            for i in ev.get('internal_evidence', []):
                trace.append(f"Q{qid} | INTERNAL | {i.get('source')} | {str(i.get('text_excerpt'))[:50]}...")
            for i in ev.get('external_evidence', []):
                trace.append(f"Q{qid} | EXTERNAL | {i.get('url')} | {str(i.get('text_excerpt'))[:50]}...")

        self.file_manager.save_text("report.txt", "\n".join(lines))
        self.file_manager.save_text("citations_trace.txt", "\n".join(trace))

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

    logger.info("OK: Dependencies loaded")
    logger.info("Self-test complete.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        run_self_test()
    else:
        Orchestrator().run()
