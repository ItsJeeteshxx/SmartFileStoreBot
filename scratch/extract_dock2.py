import re
import html

filepath = r"C:\Users\User\.gemini\antigravity\brain\2fcc5c9d-21f7-4f60-ba94-b2efb46fed34\.system_generated\steps\855\content.md"

with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()

# Let's search for any occurrence of 'framer-motion' or 'motion.div'
matches = [m.start() for m in re.finditer(r'useMotionValue|motion\.div|DockProps', content)]
print(f"Found {len(matches)} matches.")

for idx, m_start in enumerate(matches[:10]):
    start = max(0, m_start - 300)
    end = min(len(content), m_start + 1500)
    snippet = content[start:end]
    # strip HTML tags for readability
    clean_snippet = re.sub(r'<[^>]*>', '', snippet)
    clean_snippet = html.unescape(clean_snippet)
    print(f"\n--- MATCH {idx} (near index {m_start}) ---")
    print(clean_snippet[:800])
