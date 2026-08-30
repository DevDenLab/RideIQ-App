"""
build_prompts.py -- flatten each diagram JSON into one ready-to-paste .txt prompt.

The NN-*.json files are the source of truth. This script derives prompts/*.prompt.txt
plus prompts/_MANIFEST.tsv from them, so a browser agent (see AGENT-PROMPT.md) never
has to parse JSON in the browser.

Edit the JSON, then re-run:   python build_prompts.py
Never hand-edit the .txt files -- they are generated.
"""
import json, glob, os

DIA = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(DIA, "prompts")
os.makedirs(OUT, exist_ok=True)

rows = []
for path in sorted(glob.glob(os.path.join(DIA, "[0-9][0-9]-*.json"))):
    base = os.path.basename(path)
    d = json.load(open(path, encoding="utf-8"))
    if "image_prompt" not in d:
        continue                                   # 00-index.json has none
    meta = d.get("meta", {})
    title = meta.get("title", base)
    ratio = meta.get("aspect_ratio", "16:9")

    text = []
    text.append(d["image_prompt"].strip())
    text.append("")
    text.append("Aspect ratio: %s. Style: %s" % (ratio, d.get("style", {}).get("look", "flat modern")))
    text.append("")
    text.append("Follow this structure EXACTLY. Every node label, arrow label and grouping "
                "below must appear in the image, spelled as written. Do not invent extra "
                "components and do not omit any:")
    text.append("")
    text.append(json.dumps(d["spec"], indent=2, ensure_ascii=False))
    text.append("")
    text.append("Requirements: all text horizontal and legible; no overlapping labels; "
                "no spelling changes to any label; leave whitespace rather than crowding.")

    name = base.replace(".json", ".prompt.txt")
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        f.write("\n".join(text) + "\n")
    rows.append((name, title, ratio, os.path.getsize(os.path.join(OUT, name))))

print("%-40s %-52s %-6s %s" % ("FILE", "TITLE", "RATIO", "BYTES"))
for n, t, r, s in rows:
    print("%-40s %-52s %-6s %d" % (n, t[:50], r, s))

# The browser agent reads this to know which aspect ratio each diagram wants --
# they are not all the same, and getting it wrong squashes the layout.
manifest = os.path.join(OUT, "_MANIFEST.tsv")
with open(manifest, "w", encoding="utf-8") as f:
    f.write("file\ttitle\taspect_ratio\tbytes\n")
    for n, t, r, s in rows:
        f.write("%s\t%s\t%s\t%d\n" % (n, t, r, s))

print("\n%d prompt files + _MANIFEST.tsv in %s" % (len(rows), OUT))
