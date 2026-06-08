import re
import html

filepath = r"C:\Users\User\.gemini\antigravity\brain\2fcc5c9d-21f7-4f60-ba94-b2efb46fed34\.system_generated\steps\895\content.md"

with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()

# Let's search for any occurrence of theme-toggler, toggle, or similar keywords
matches = [m.start() for m in re.finditer(r'toggle|theme|button|svg', content, re.IGNORECASE)]
print(f"Found {len(matches)} matches.")

# Let's extract blocks that contain next-themes or button/svg elements with transitions
blocks = re.findall(r'<pre[^>]*>(.*?)</pre>', content, re.DOTALL)
print(f"Found {len(blocks)} pre blocks.")

clean_blocks = []
for b in blocks:
    clean_b = html.unescape(re.sub(r'<[^>]*>', '', b))
    if "theme" in clean_b.lower() and ("button" in clean_b.lower() or "lucide" in clean_b.lower() or "framer" in clean_b.lower()):
        clean_blocks.append(clean_b)

print(f"Found {len(clean_blocks)} relevant blocks.")
for idx, cb in enumerate(clean_blocks[:5]):
    print(f"\n--- BLOCK {idx} ---")
    lines = cb.split('\n')
    print('\n'.join(lines[:120]))
    if len(lines) > 120:
        print("... (truncated)")
