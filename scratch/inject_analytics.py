import os

HTML_FILES = [
    r"c:\Users\User\Downloads\AryaBotNew\TryAryaBot\pocket-arya-store-new\admin.html",
    r"c:\Users\User\Downloads\AryaBotNew\TryAryaBot\pocket-arya-store-new\app.html",
    r"c:\Users\User\Downloads\AryaBotNew\TryAryaBot\pocket-arya-store-new\index.html",
    r"c:\Users\User\Downloads\AryaBotNew\TryAryaBot\pocket-arya-store-new\public\contact.html",
    r"c:\Users\User\Downloads\AryaBotNew\TryAryaBot\pocket-arya-store-new\public\landing.html",
    r"c:\Users\User\Downloads\AryaBotNew\TryAryaBot\pocket-arya-store-new\public\privacy.html",
    r"c:\Users\User\Downloads\AryaBotNew\TryAryaBot\pocket-arya-store-new\public\refund.html",
    r"c:\Users\User\Downloads\AryaBotNew\TryAryaBot\pocket-arya-store-new\public\shipping.html",
    r"c:\Users\User\Downloads\AryaBotNew\TryAryaBot\pocket-arya-store-new\public\terms.html"
]

tag = '<!-- Cloudflare Web Analytics --><script defer src=\'https://static.cloudflareinsights.com/beacon.min.js\' data-cf-beacon=\'{"token": "63c57238d9f245f09add26db117ca7cc"}\'></script><!-- End Cloudflare Web Analytics -->'

for file_path in HTML_FILES:
    if not os.path.exists(file_path):
        print(f"File not found: {file_path}")
        continue
        
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
        
    if "static.cloudflareinsights.com/beacon.min.js" in content:
        print(f"Already injected: {file_path}")
        continue
        
    if "</head>" in content:
        new_content = content.replace("</head>", f"  {tag}\n</head>")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        print(f"Successfully injected into: {file_path}")
    elif "</HEAD>" in content:
        new_content = content.replace("</HEAD>", f"  {tag}\n</HEAD>")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        print(f"Successfully injected into: {file_path}")
    else:
        print(f"Could not find head tag in: {file_path}")
