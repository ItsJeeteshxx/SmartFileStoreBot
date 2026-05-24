import os

src_dir = "pocket-arya-store-new/src"
out_lines = []

def is_emoji(char):
    cp = ord(char)
    # Check common emoji ranges
    return (
        (0x1F300 <= cp <= 0x1F9FF) or 
        (0x1FA70 <= cp <= 0x1FAFF) or
        (0x2600 <= cp <= 0x26FF) or
        (0x2700 <= cp <= 0x27BF) or
        (0x1F600 <= cp <= 0x1F64F) or
        (0x1F680 <= cp <= 0x1F6FF)
    )

for root, dirs, files in os.walk(src_dir):
    for f in files:
        if f.endswith((".ts", ".tsx", ".json", ".js", ".jsx", ".html")):
            path = os.path.join(root, f)
            try:
                with open(path, "r", encoding="utf-8") as file:
                    content = file.read()
                
                for i, char in enumerate(content):
                    if is_emoji(char):
                        start = max(0, i - 30)
                        end = min(len(content), i + 30)
                        ctx = content[start:end].replace('\n', ' ')
                        out_lines.append(f"File: {path}\n  Emoji: {char} (U+{ord(char):04X}) at index {i}\n  Context: {ctx}\n")
            except Exception as e:
                out_lines.append(f"Error reading {path}: {e}\n")

with open("emojis_found.txt", "w", encoding="utf-8") as out_f:
    out_f.writelines(out_lines)

print(f"Done. Found {len(out_lines)} emojis. Results written to emojis_found.txt")
