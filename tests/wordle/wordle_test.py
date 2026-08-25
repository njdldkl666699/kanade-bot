import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
wordle_dir_path = ROOT / "config" / "wordle"

for file in wordle_dir_path.iterdir():
    d = json.load(file.open())
    len_counts = {}
    for k in d:
        l = len(k)
        len_counts[l] = len_counts.get(l, 0) + 1
    print(file.name)
    for l, count in sorted(len_counts.items()):
        print(f"  {l}: {count}")

# min: 3
# max: 13
