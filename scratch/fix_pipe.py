import os
import glob
import re

plugins_dir = "c:/Users/User/Downloads/AryaBotNew/TryAryaBot/plugins"
files = glob.glob(f"{plugins_dir}/*.py")

replacements_made = 0

for file in files:
    with open(file, "r", encoding="utf-8") as f:
        content = f.read()

    original_content = content

    # Replace in tuple/list checks
    content = re.sub(
        r'"CONNECTION"',
        r'"CONNECTION", "BROKEN PIPE", "ERRNO 32"',
        content
    )
    
    # Also replace `"TIMEOUT" in err or "CONNECTION" in err` patterns
    content = re.sub(
        r'("CONNECTION" in [a-zA-Z0-9_]+(\.upper\(\))?)',
        r'\1 or "BROKEN PIPE" in \g<1> or "ERRNO 32" in \g<1>',
        content
    )
    # The above regex is a bit flawed since \g<1> includes the `"CONNECTION" in ...` part.
    # Let's fix that regex:
    # Match: "CONNECTION" in err
    # Replace: "CONNECTION" in err or "BROKEN PIPE" in err or "ERRNO 32" in err
    
    if content != original_content:
        # let's be careful and use a simpler replace strategy
        pass

for file in files:
    with open(file, "r", encoding="utf-8") as f:
        content = f.read()
        
    orig = content
    
    # 1. Tuples and Lists of strings
    content = content.replace('"CONNECTION",', '"CONNECTION", "BROKEN PIPE", "ERRNO 32",')
    
    # 2. String checks like `or "CONNECTION" in err`
    content = content.replace('or "CONNECTION" in err', 'or "CONNECTION" in err or "BROKEN PIPE" in err or "ERRNO 32" in err')
    content = content.replace('or "CONNECTION" in f_err', 'or "CONNECTION" in f_err or "BROKEN PIPE" in f_err or "ERRNO 32" in f_err')
    content = content.replace('or "CONNECTION" in str(dl_e).upper()', 'or "CONNECTION" in str(dl_e).upper() or "BROKEN PIPE" in str(dl_e).upper() or "ERRNO 32" in str(dl_e).upper()')
    content = content.replace('or "CONNECTION" in err_dl', 'or "CONNECTION" in err_dl or "BROKEN PIPE" in err_dl or "ERRNO 32" in err_dl')
    content = content.replace('or "CONNECTION" in str(e2).upper()', 'or "CONNECTION" in str(e2).upper() or "BROKEN PIPE" in str(e2).upper() or "ERRNO 32" in str(e2).upper()')
    content = content.replace('or "CONNECTION" in eup_err', 'or "CONNECTION" in eup_err or "BROKEN PIPE" in eup_err or "ERRNO 32" in eup_err')

    if content != orig:
        with open(file, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"Updated {file}")
        replacements_made += 1

print(f"Replacements made in {replacements_made} files.")
