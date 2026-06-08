import re
import html

filepath = r"C:\Users\User\.gemini\antigravity\brain\2fcc5c9d-21f7-4f60-ba94-b2efb46fed34\.system_generated\steps\855\content.md"

with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()

# We can search for code blocks in html: they usually are in <pre><code>...</code></pre>
# Or inside data-line attributes or simply raw text.
# Let's extract all code fragments and search for ones mentioning "motion" or "framer-motion" or "Dock"

# Let's clean up HTML tags to get pure text first.
# A regex to strip tags:
def strip_tags(text):
    return re.sub(r'<[^>]*>', '', text)

# Let's look for sections that look like React code for Dock
# Find all <pre ...> ... </pre> blocks
blocks = re.findall(r'<pre[^>]*>(.*?)</pre>', content, re.DOTALL)

clean_blocks = []
for b in blocks:
    clean_b = html.unescape(strip_tags(b))
    if "Dock" in clean_b and ("framer-motion" in clean_b or "motion" in clean_b or "useMotionValue" in clean_b):
        clean_blocks.append(clean_b)

print(f"Found {len(clean_blocks)} relevant blocks.")
for idx, cb in enumerate(clean_blocks):
    print(f"\n--- BLOCK {idx} ---")
    # print first 100 lines
    lines = cb.split('\n')
    print('\n'.join(lines[:120]))
    if len(lines) > 120:
        print("... (truncated)")
