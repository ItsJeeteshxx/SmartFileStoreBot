import re
import os

files = [
    r"c:\Users\User\Downloads\AryaBotNew\TryAryaBot\pocket-arya-store-new\src\components\arya\views\AdminView.tsx",
    r"c:\Users\User\Downloads\AryaBotNew\TryAryaBot\pocket-arya-store-new\src\components\arya\admin\EnterpriseAnalyticsPanel.tsx",
    r"c:\Users\User\Downloads\AryaBotNew\TryAryaBot\mini_app_api.py"
]

# Standard emoji regex: matches miscellaneous symbols, pictographs, flags, etc.
emoji_pattern = re.compile(
    r'[\U0001f000-\U0001ffff]'  # Miscellaneous Symbols and Pictographs, Emoticons, Transport and Map Symbols, etc.
    r'|[\u2600-\u27bf]'          # Miscellaneous Symbols, Dingbats
    r'|[\u2300-\u23ff]'          # Miscellaneous Technical
    r'|[\u2b50]'                  # Star
    r'|[\U00002700-\U000027BF]'
)

output_path = r"c:\Users\User\Downloads\AryaBotNew\TryAryaBot\scratch\emojis_log.txt"

with open(output_path, 'w', encoding='utf-8') as out:
    for file_path in files:
        if not os.path.exists(file_path):
            out.write(f"\nSkipping (not found): {file_path}\n")
            continue
        out.write(f"\nScanning: {file_path}\n")
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        for idx, line in enumerate(lines):
            matches = emoji_pattern.findall(line)
            if matches:
                out.write(f"  Line {idx+1}: {matches} -> {line.strip()[:100]}\n")

print("Finished scanning. Log written to scratch/emojis_log.txt")
