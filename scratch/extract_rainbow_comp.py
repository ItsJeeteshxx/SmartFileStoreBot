import re
import html

filepath = r"C:\Users\User\.gemini\antigravity\brain\2fcc5c9d-21f7-4f60-ba94-b2efb46fed34\.system_generated\steps\1043\content.md"

with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()

# Let's search around index 790000 - 798000
# We want to find the code block that starts with import or contains RainbowButtonProps
# Let's search for "interface RainbowButtonProps" or "export interface RainbowButtonProps"
match = re.search(r'RainbowButtonProps', content)
if match:
    idx = match.start()
    print(f"Found RainbowButtonProps at index {idx}")
    
    # Look back for standard imports or "use client"
    start_idx = content.rfind('import', 0, idx)
    if start_idx == -1:
        start_idx = idx - 500
        
    # Find the end of this code block (```)
    end_idx = content.find('```', idx)
    if end_idx != -1:
        code = content[start_idx:end_idx]
        code = html.unescape(code)
        
        # Clean up tags and Unicode/HTML escapes
        code = re.sub(r'<[^>]*>', '', code)
        code = code.replace('\\n', '\n').replace('\\"', '"').replace('\\t', '\t')
        
        with open('scratch/rainbow_button_component.tsx', 'w', encoding='utf-8') as out_f:
            out_f.write(code)
        print("Successfully wrote component to scratch/rainbow_button_component.tsx")
    else:
        print("Could not find end of code block")
else:
    print("RainbowButtonProps not found")
