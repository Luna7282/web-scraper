# deduplicate.py
import json
from pathlib import Path

def deduplicate_jsonl(filepath: str):
    path = Path(filepath)
    if not path.exists():
        return

    seen_instructions = set()
    unique_rows = []

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            data = json.loads(line)
            inst = data.get("instruction", "").strip().lower()
            if inst not in seen_instructions:
                seen_instructions.add(inst)
                unique_rows.append(data)

    with open(path, "w", encoding="utf-8") as f:
        for row in unique_rows:
            f.write(json.dumps(row) + "\n")

    print(f"Cleaned {path.name}: {len(unique_rows)} unique pairs kept.")

deduplicate_jsonl("data/unified.jsonl")
deduplicate_jsonl("data/awesome.jsonl")