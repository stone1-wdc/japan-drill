import os
import re

INPUT_FILE = "full_book.txt"
OUTPUT_DIR = "raw"

# Chapter heading patterns — each alternation captures the number in its own group
HEADING_RE = re.compile(
    r"^.*?"                         # optional leading text
    r"(?:第\s*(\d+)\s*課"          # 第1課, 第 1 課
    r"|Chapter\s+(\d+)"             # Chapter 1, Chapter  1
    r"|第\s*(\d+)\s*章"            # 第1章, 第 1 章
    r"|第\s*(\d+)\s*週)"           # 第1週, 第 1 週
    r".*$",                         # rest of line (e.g. title text)
    re.IGNORECASE,
)

# Stuff to strip from every line
STRIP_RE = re.compile(r"\[図\d+\]")      # illustration markers like [図1]
PAGE_RE = re.compile(r"^\s*\d+\s*$")     # standalone page numbers


def _extract_chapter_num(line: str) -> int | None:
    m = HEADING_RE.match(line.strip())
    if not m:
        return None
    # First non-None group is the chapter number
    for g in m.groups():
        if g is not None:
            return int(g)
    return None


def main():
    if not os.path.exists(INPUT_FILE):
        print(f"Error: {INPUT_FILE} not found")
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()

    chapters: list[tuple[int, list[str]]] = []
    current_num: int | None = None
    current_lines: list[str] = []

    for line in lines:
        clean = STRIP_RE.sub("", line)
        if PAGE_RE.match(clean):
            continue

        ch_num = _extract_chapter_num(line)
        if ch_num is not None:
            if current_num is not None:
                chapters.append((current_num, current_lines))
            current_num = ch_num
            current_lines = []
            continue

        if current_num is not None:
            current_lines.append(clean)

    if current_num is not None:
        chapters.append((current_num, current_lines))

    for ch_num, ch_lines in chapters:
        filename = f"chapter_{ch_num:02d}.txt"
        path = os.path.join(OUTPUT_DIR, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(ch_lines)
        print(f"  -> {filename}  ({len(ch_lines)} lines)")

    print(f"\nDone. {len(chapters)} chapters written to {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
