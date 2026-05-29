import os
import glob

# The Google Analytics tag script block
GTAG_SCRIPT = """  <!-- Google tag (gtag.js) -->
  <script async src="https://www.googletagmanager.com/gtag/js?id=G-ZW33W2TXLN"></script>
  <script>
    window.dataLayer = window.dataLayer || [];
    function gtag(){dataLayer.push(arguments);}
    gtag('js', new Date());

    gtag('config', 'G-ZW33W2TXLN');
  </script>"""

base_dir = r"c:\Users\User\Downloads\AryaBotNew\TryAryaBot\pocket-arya-store-new"

# HTML files to process
html_files = [
    os.path.join(base_dir, "app.html"),
    os.path.join(base_dir, "admin.html"),
    os.path.join(base_dir, "index.html"),
]

# Find all HTML files in the public directory
public_dir = os.path.join(base_dir, "public")
if os.path.exists(public_dir):
    html_files.extend(glob.glob(os.path.join(public_dir, "*.html")))

print(f"Found {len(html_files)} HTML files to process.")

for filepath in html_files:
    if not os.path.exists(filepath):
        print(f"File not found: {filepath}")
        continue
        
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
        
    # Check if Google Tag is already present in this file to avoid duplicates
    if "G-ZW33W2TXLN" in content:
        print(f"Google Tag already exists in: {os.path.basename(filepath)}")
        continue
        
    # Find <head> tag and insert the Google Tag immediately after it
    if "<head>" in content:
        content = content.replace("<head>", f"<head>\n{GTAG_SCRIPT}", 1)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"Added Google Tag to: {os.path.basename(filepath)}")
    elif "<HEAD>" in content:
        content = content.replace("<HEAD>", f"<HEAD>\n{GTAG_SCRIPT}", 1)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"Added Google Tag to: {os.path.basename(filepath)} (uppercase HEAD)")
    else:
        print(f"ERROR: <head> tag not found in: {os.path.basename(filepath)}")

print("All HTML files processed successfully!")
