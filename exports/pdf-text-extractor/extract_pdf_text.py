#!/home/dll/pdf-text-extractor/.venv/bin/python
from __future__ import annotations

import argparse
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

import fitz

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCRIPT_DIR / "output"

TOP_MARGIN_RATIO = 0.045
BOTTOM_MARGIN_RATIO = 0.05
MIN_BLOCK_TEXT_LEN = 3
COLUMN_GAP_MIN_RATIO = 0.08
COLUMN_MIN_BLOCKS = 2
MAX_RETRIES = 2
WIDE_BLOCK_RATIO = 0.72
TITLE_ZONE_RATIO = 0.32
FOOTER_ZONE_RATIO = 0.86
CAPTION_PREFIXES = (
    "fig.", "figure ", "table ", "extended data fig.", "supplementary fig.", "supplementary table"
)
NOISE_PATTERNS = [
    re.compile(r"^article$", re.I),
    re.compile(r"^open$", re.I),
    re.compile(r"^page\s+\d+(\s+of\s+\d+)?$", re.I),
    re.compile(r"^https?://\S+$", re.I),
    re.compile(r"^[\d\s\-–—|():;,\.]+$"),
    re.compile(r"^nature communications", re.I),
]


@dataclass
class FileResult:
    pdf_path: Path
    txt_path: Path
    status: str
    overwritten: bool = False
    attempts: int = 1
    reason: str = ""


@dataclass
class TextBlock:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def center_x(self) -> float:
        return (self.x0 + self.x1) / 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract text from one or more text-based PDF files into TXT files.")
    parser.add_argument("inputs", nargs="+", help="One or more PDF files and/or directories containing PDF files.")
    return parser.parse_args()


def collect_pdf_paths(inputs: Iterable[str]) -> List[Path]:
    pdfs: list[Path] = []
    seen: set[Path] = set()
    for raw in inputs:
        path = Path(raw).expanduser().resolve()
        if not path.exists():
            print(f"[WARN] Input not found: {path}", file=sys.stderr)
            continue
        if path.is_file():
            candidates = [path] if path.suffix.lower() == ".pdf" else []
            if not candidates:
                print(f"[WARN] Not a PDF file, skipped: {path}", file=sys.stderr)
        elif path.is_dir():
            candidates = sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")
        else:
            candidates = []
        for candidate in candidates:
            if candidate not in seen:
                seen.add(candidate)
                pdfs.append(candidate)
    return pdfs


def normalize_line(line: str) -> str:
    line = line.replace("\u00ad", "")
    line = re.sub(r"[ \t]+", " ", line)
    return line.strip()


def normalize_line_for_compare(line: str) -> str:
    return re.sub(r"\s+", " ", normalize_line(line).lower())


def looks_like_noise(line: str) -> bool:
    stripped = normalize_line(line)
    if not stripped:
        return True
    if len(stripped) <= 2 and not any(ch.isalpha() for ch in stripped):
        return True
    for pattern in NOISE_PATTERNS:
        if pattern.search(stripped):
            return True
    return False


def looks_like_metadata(line: str) -> bool:
    lower = normalize_line_for_compare(line)
    return any([
        ("@" in lower and ("email" in lower or ".ac." in lower or ".edu" in lower)),
        lower.startswith("these authors contributed equally"),
        lower.startswith("department of "),
        lower.startswith("institute of "),
        lower.startswith("wellcome centre"),
        lower.startswith("parasites and microbes programme"),
        lower.startswith("correspondence"),
    ])


def looks_like_caption(text: str) -> bool:
    lower = normalize_line_for_compare(text)
    return any(lower.startswith(prefix) for prefix in CAPTION_PREFIXES)


def should_start_new_paragraph(prev_line: str, next_line: str) -> bool:
    prev_line = prev_line.rstrip()
    next_line = next_line.lstrip()
    if not prev_line:
        return True
    if prev_line.endswith((".", "!", "?", ":")):
        return True
    if prev_line.endswith("-"):
        return False
    if next_line and next_line[0].isupper() and not prev_line.endswith(","):
        if len(next_line.split()) <= 14:
            return True
    return False


def join_paragraph_lines(lines: List[str]) -> str:
    out: list[str] = []
    for line in lines:
        if out and out[-1].endswith("-"):
            out[-1] = out[-1][:-1] + line.lstrip()
        else:
            out.append(line)
    return " ".join(out)


def merge_lines(lines: List[str]) -> str:
    paragraphs: list[str] = []
    current: list[str] = []
    for raw in lines:
        line = normalize_line(raw)
        if not line:
            if current:
                paragraphs.append(join_paragraph_lines(current))
                current = []
            continue
        if current and should_start_new_paragraph(current[-1], line):
            paragraphs.append(join_paragraph_lines(current))
            current = [line]
        else:
            current.append(line)
    if current:
        paragraphs.append(join_paragraph_lines(current))
    return "\n\n".join(p for p in paragraphs if p.strip())


def collect_repeated_lines(doc: fitz.Document) -> set[str]:
    counts: Counter[str] = Counter()
    total_pages = len(doc)
    if total_pages < 2:
        return set()
    for page in doc:
        local_seen: set[str] = set()
        for block in page.get_text("blocks"):
            _, y0, _, y1, text, *_ = block
            if y0 > page.rect.height * 0.12 and y1 < page.rect.height * 0.88:
                continue
            for line in text.splitlines():
                normalized = normalize_line_for_compare(line)
                if len(normalized) < 4 or looks_like_noise(normalized):
                    continue
                if normalized in local_seen:
                    continue
                local_seen.add(normalized)
                counts[normalized] += 1
    threshold = max(2, int(total_pages * 0.4))
    return {line for line, count in counts.items() if count >= threshold}


def page_blocks(page: fitz.Page, repeated_lines: set[str], is_first_page: bool) -> list[TextBlock]:
    blocks: list[TextBlock] = []
    page_rect = page.rect
    top_cut = page_rect.height * TOP_MARGIN_RATIO
    bottom_cut = page_rect.height * (1 - BOTTOM_MARGIN_RATIO)
    for raw in page.get_text("blocks"):
        x0, y0, x1, y1, text, *_ = raw
        text = text.strip()
        if y1 <= top_cut or y0 >= bottom_cut:
            continue
        if len(text) < MIN_BLOCK_TEXT_LEN:
            continue
        normalized_compare = normalize_line_for_compare(text)
        if normalized_compare in repeated_lines:
            continue
        if looks_like_noise(text) or looks_like_caption(text):
            continue
        if is_first_page and y0 >= page_rect.height * FOOTER_ZONE_RATIO:
            continue
        if is_first_page and y0 >= page_rect.height * 0.62 and any(looks_like_metadata(line) for line in text.splitlines()):
            continue
        blocks.append(TextBlock(x0=x0, y0=y0, x1=x1, y1=y1, text=text))
    return blocks


def detect_column_split(blocks: list[TextBlock], page_width: float, page_height: float) -> float | None:
    body_blocks = [b for b in blocks if b.y0 > page_height * 0.05 and b.width < page_width * WIDE_BLOCK_RATIO]
    if len(body_blocks) < COLUMN_MIN_BLOCKS * 2:
        return None
    centers = sorted(b.center_x for b in body_blocks)
    gaps = [(right - left, left, right) for left, right in zip(centers, centers[1:])]
    if not gaps:
        return None
    max_gap, left, right = max(gaps, key=lambda item: item[0])
    if max_gap < page_width * COLUMN_GAP_MIN_RATIO:
        return None
    split = (left + right) / 2
    left_blocks = [b for b in body_blocks if b.center_x < split]
    right_blocks = [b for b in body_blocks if b.center_x >= split]
    if len(left_blocks) < COLUMN_MIN_BLOCKS or len(right_blocks) < COLUMN_MIN_BLOCKS:
        return None
    return split


def order_blocks(blocks: list[TextBlock], page_width: float, page_height: float) -> list[TextBlock]:
    split = detect_column_split(blocks, page_width, page_height)
    if split is None:
        return sorted(blocks, key=lambda b: (round(b.y0, 1), round(b.x0, 1)))

    title_zone = page_height * TITLE_ZONE_RATIO
    top_spanning = [b for b in blocks if b.width > page_width * WIDE_BLOCK_RATIO and b.y0 <= title_zone]
    remaining = [b for b in blocks if b not in top_spanning]

    left = [b for b in remaining if b.center_x < split and b.width < page_width * WIDE_BLOCK_RATIO]
    right = [b for b in remaining if b.center_x >= split and b.width < page_width * WIDE_BLOCK_RATIO]
    tail_spanning = [b for b in remaining if b not in left and b not in right]

    ordered: list[TextBlock] = []
    ordered.extend(sorted(top_spanning, key=lambda b: (round(b.y0, 1), round(b.x0, 1))))
    ordered.extend(sorted(left, key=lambda b: (round(b.y0, 1), round(b.x0, 1))))
    ordered.extend(sorted(right, key=lambda b: (round(b.y0, 1), round(b.x0, 1))))
    ordered.extend(sorted(tail_spanning, key=lambda b: (round(b.y0, 1), round(b.x0, 1))))
    return ordered


def filter_block_lines(block: TextBlock, repeated_lines: set[str], is_first_page: bool) -> list[str]:
    out: list[str] = []
    for line in block.text.splitlines():
        normalized = normalize_line(line)
        if not normalized:
            continue
        if normalize_line_for_compare(normalized) in repeated_lines:
            continue
        if looks_like_noise(normalized) or looks_like_metadata(normalized):
            continue
        if is_first_page and normalized.startswith(tuple(str(i) for i in range(1, 10))) and "Department of" in normalized:
            continue
        out.append(normalized)
    return out


def postprocess_final_text(text: str) -> str:
    text = re.sub(r"\bP\.\s*\n\nfalciparum\b", "P. falciparum", text)
    text = re.sub(r"\bN6\s*\n\n methyl", "N6-methyl", text)
    text = re.sub(r"\bApiAP2\s*\n\n TFs\b", "ApiAP2 TFs", text)
    text = re.sub(r"\bco\s*\n\ntranscribed\b", "co-transcribed", text)
    text = re.sub(r"\bspecies\s*\n\nspecific\b", "species-specific", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def extract_text_once(pdf_path: Path) -> str:
    with fitz.open(pdf_path) as doc:
        repeated_lines = collect_repeated_lines(doc)
        page_texts: list[str] = []
        for idx, page in enumerate(doc):
            is_first_page = idx == 0
            blocks = page_blocks(page, repeated_lines, is_first_page=is_first_page)
            ordered_blocks = order_blocks(blocks, page.rect.width, page.rect.height)
            lines: list[str] = []
            for block in ordered_blocks:
                block_lines = filter_block_lines(block, repeated_lines, is_first_page=is_first_page)
                if block_lines:
                    lines.extend(block_lines)
                    lines.append("")
            merged = merge_lines(lines)
            if merged.strip():
                page_texts.append(merged)
        final_text = "\n\n".join(page_texts).strip()
        if not final_text:
            raise ValueError("No extractable body text found.")
        return postprocess_final_text(final_text)


def process_pdf(pdf_path: Path) -> FileResult:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    txt_path = OUTPUT_DIR / f"{pdf_path.stem}.txt"
    overwritten = txt_path.exists()
    attempts = 0
    last_error = ""
    for attempt in range(1, MAX_RETRIES + 2):
        attempts = attempt
        try:
            text = extract_text_once(pdf_path)
            txt_path.write_text(text, encoding="utf-8")
            return FileResult(pdf_path=pdf_path, txt_path=txt_path, status="success", overwritten=overwritten, attempts=attempts)
        except Exception as exc:
            last_error = str(exc)
            if attempt <= MAX_RETRIES:
                time.sleep(0.3)
                continue
            return FileResult(pdf_path=pdf_path, txt_path=txt_path, status="failed", overwritten=overwritten, attempts=attempts, reason=last_error)
    return FileResult(pdf_path=pdf_path, txt_path=txt_path, status="failed", reason="Unknown error")


def print_summary(results: list[FileResult]) -> None:
    success = [r for r in results if r.status == "success"]
    failed = [r for r in results if r.status == "failed"]
    overwritten = [r for r in success if r.overwritten]
    print("\n=== PDF Text Extraction Summary ===")
    print(f"Output directory: {OUTPUT_DIR}")
    print(f"Processed: {len(results)} | Success: {len(success)} | Failed: {len(failed)}")
    if success:
        print("\n[Success]")
        for item in success:
            suffix = " [OVERWRITTEN]" if item.overwritten else ""
            print(f"- {item.pdf_path} -> {item.txt_path}{suffix}")
    if failed:
        print("\n[Failed]")
        for item in failed:
            print(f"- {item.pdf_path} (attempts={item.attempts}, reason={item.reason})")
    if overwritten:
        print("\n[Overwritten]")
        for item in overwritten:
            print(f"- {item.txt_path}")


def main() -> int:
    args = parse_args()
    pdf_paths = collect_pdf_paths(args.inputs)
    if not pdf_paths:
        print("No valid PDF files found.", file=sys.stderr)
        return 1
    results = [process_pdf(pdf_path) for pdf_path in pdf_paths]
    print_summary(results)
    return 0 if any(r.status == "success" for r in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
