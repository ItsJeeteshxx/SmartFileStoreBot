def print_log_sections():
    with open("catbox_log_downloaded.txt", "r", encoding="utf-8") as f:
        lines = f.readlines()
        
    print("--- Section Around Ep 240-250 ---")
    for line in lines:
        if "MVS" in line:
            # Extract number
            parts = line.split()
            if len(parts) >= 5:
                fname = parts[4]
                if "24" in fname:
                    print(line.strip())
                    
    print("\n--- Section Around Ep 1420-1470 ---")
    for line in lines:
        if "MVS" in line:
            parts = line.split()
            if len(parts) >= 5:
                fname = parts[4]
                try:
                    # extract the number from file name e.g. "1430_MVS.mp3"
                    import re
                    num_match = re.search(r'\d+', fname)
                    if num_match:
                        val = int(num_match.group(0))
                        if 1420 <= val <= 1475:
                            print(line.strip())
                except:
                    pass

print_log_sections()
