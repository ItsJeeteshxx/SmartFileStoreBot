import re
import sys
import os

sys.path.append(os.path.abspath(os.path.dirname(__file__)))
from plugins.utils import extract_ep_label_robust

def parse_report(file_path):
    episodes = set()
    rows = []
    
    with open(file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
        
    current_row = None
    for line in lines:
        match = re.match(r'^\s*(\d+)\s+(\d+)\s+(audio|video|document|photo)\s+(.*?)\s+\[\d+(?:\.\d+)?(?:MB|KB|GB)\]', line)
        if match:
            if current_row:
                rows.append(current_row)
            idx, msg_id, mtype, filename = match.groups()
            current_row = {
                "idx": int(idx),
                "msg_id": int(msg_id),
                "type": mtype,
                "filename": filename,
                "title": None
            }
        elif line.strip().startswith("↳ Title:"):
            if current_row:
                current_row["title"] = line.split("Title:", 1)[1].strip()
    if current_row:
        rows.append(current_row)
        
    for r in rows:
        fullname = r["filename"]
        if r["title"]:
            fullname = f"{r['title']} @@@ {fullname}"
        
        ep_info = extract_ep_label_robust(fullname)
        if ep_info and ep_info.get("numbers"):
            ep_nums = ep_info["numbers"]
            for e in ep_nums:
                episodes.add(e)
        else:
            print(f"Failed to extract ep from: {fullname} (MsgID: {r['msg_id']})")
            
    return episodes, rows

if __name__ == "__main__":
    file_path = "catbox_log_new.txt"
    episodes, rows = parse_report(file_path)
    
    max_ep = 2296
    missing = []
    for i in range(1, max_ep + 1):
        if i not in episodes:
            missing.append(i)
            
    print("\n--- RESULTS ---")
    print(f"Total Unique Episodes Found: {len(episodes)}")
    print(f"Total Missing Episodes: {len(missing)}")
    if missing:
        print(f"Missing list (first 100): {missing[:100]}")
        # Let's print gaps/ranges
        gaps = []
        if len(missing) > 0:
            start = missing[0]
            prev = missing[0]
            for m in missing[1:]:
                if m == prev + 1:
                    prev = m
                else:
                    if start == prev:
                        gaps.append(f"{start}")
                    else:
                        gaps.append(f"{start}-{prev}")
                    start = m
                    prev = m
            if start == prev:
                gaps.append(f"{start}")
            else:
                gaps.append(f"{start}-{prev}")
        print(f"Missing Ranges: {', '.join(gaps)}")
    else:
        print("No missing episodes!")
