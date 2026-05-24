import re

def parse_file(filepath):
    episodes = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            # Look for lines starting with index numbers: e.g., "1414      6077     audio  1401_MVS.mp3"
            # Pattern: index, msg_id, type, filename
            match = re.match(r'^\s*(\d+)\s+(\d+)\s+\w+\s+(.*?)\s*\[.*\]\s*$', line)
            if match:
                idx = int(match.group(1))
                msg_id = int(match.group(2))
                filename = match.group(3).strip()
                episodes.append((idx, msg_id, filename))
    return episodes

def extract_ep(filename):
    # Extract numbers from filename
    # Remove extension
    name = filename.rsplit('.', 1)[0]
    # Try common patterns: MVS__123, 123_MVS, Ep 123, 123.mp3
    # Look for digits
    digits = re.findall(r'\d+', name)
    if digits:
        # If there are multiple numbers, choose the one that makes sense
        # e.g., "103 (1).mp3" -> digits are ['103', '1'] -> choose '103'
        # e.g., "1401_MVS" -> digits is ['1401']
        # e.g., "MVS__1" -> digits is ['1']
        # If the filename starts with 'MVS__' or 'MVS_', return the number after it
        mvs_match = re.search(r'MVS__?(\d+)', name)
        if mvs_match:
            return int(mvs_match.group(1))
        # Check if first token is a number
        first_num = digits[0]
        if len(digits) > 1 and digits[1] == '1' and f"({digits[1]})" in name:
            # "103 (1)"
            return int(first_num)
        return int(first_num)
    return None

def main():
    episodes = parse_file("catbox_log_downloaded.txt")
    print(f"Total entries parsed: {len(episodes)}")
    
    # Extract episode numbers
    extracted = []
    failed = []
    for idx, msg_id, fname in episodes:
        ep = extract_ep(fname)
        if ep is not None:
            extracted.append((ep, msg_id, fname))
        else:
            failed.append((idx, msg_id, fname))
            
    print(f"Successfully extracted: {len(extracted)}")
    print(f"Failed to extract: {len(failed)}")
    for f in failed[:10]:
        print(f"  Failed: {f}")
        
    # Sort by extracted episode number
    sorted_ext = sorted(extracted, key=lambda x: x[0])
    
    # Find duplicate episode numbers
    seen = {}
    duplicates = []
    for ep, msg_id, fname in sorted_ext:
        if ep in seen:
            duplicates.append((ep, seen[ep], (msg_id, fname)))
        else:
            seen[ep] = (msg_id, fname)
            
    print(f"\nDuplicates found: {len(duplicates)}")
    for d in duplicates[:10]:
        print(f"  Ep {d[0]}: Msg {d[1][0]} ({d[1][1]}) and Msg {d[2][0]} ({d[2][1]})")
        
    # Find gaps in sequence from min to max
    if extracted:
        min_ep = min(x[0] for x in extracted)
        max_ep = max(x[0] for x in extracted)
        print(f"\nRange of episodes: {min_ep} to {max_ep}")
        
        all_eps = set(range(min_ep, max_ep + 1))
        found_eps = set(x[0] for x in extracted)
        missing = sorted(list(all_eps - found_eps))
        
        print(f"Total missing episode numbers: {len(missing)}")
        # Group missing into ranges
        if missing:
            ranges = []
            start = missing[0]
            prev = missing[0]
            for m in missing[1:]:
                if m == prev + 1:
                    prev = m
                else:
                    if start == prev:
                        ranges.append(f"{start}")
                    else:
                        ranges.append(f"{start}-{prev}")
                    start = m
                    prev = m
            if start == prev:
                ranges.append(f"{start}")
            else:
                ranges.append(f"{start}-{prev}")
            print(f"Missing Ranges: {', '.join(ranges)}")

if __name__ == "__main__":
    main()
