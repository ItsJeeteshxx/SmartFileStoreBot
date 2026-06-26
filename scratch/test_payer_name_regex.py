import re

def clean_extracted_name(name: str) -> str:
    # Remove extra spaces
    name = re.sub(r'\s+', ' ', name).strip()
    
    # Split into words and stop at any common non-name keywords
    stop_words = {
        "amount", "utr", "rrn", "txn", "txnid", "date", "ref", "rs", "inr", "upi", 
        "payment", "status", "type", "received", "credited", "transferred", "has", 
        "been", "via", "on", "in", "to", "your", "my", "account", "bank", "slice",
        "customer", "user", "card", "rupees", "id", "no", "reference", "credited",
        "debit", "credit", "wallet", "balance", "success", "failed", "pending"
    }
    
    words = name.split()
    valid_words = []
    for w in words:
        # Strip trailing punctuation from the word for checking
        w_clean = re.sub(r'[^a-zA-Z]', '', w).lower()
        if w_clean in stop_words:
            break
        valid_words.append(w)
        
    cleaned = " ".join(valid_words).strip()
    # Clean any trailing punctuation or special chars from the name
    cleaned = re.sub(r'[^a-zA-Z\s\.\-\&]', '', cleaned).strip()
    # Strip any trailing punctuation like dots or dashes from the end of the cleaned name
    cleaned = cleaned.rstrip('. - &').strip()
    return cleaned

def extract_payer_name_from_email(body: str) -> str:
    """Helper to extract sender name from slice email notifications."""
    if not body:
        return ""
    
    # Normalize spaces and strip HTML tags if present
    body_clean = re.sub(r'<[^>]+>', ' ', body)
    body_clean = re.sub(r'\s+', ' ', body_clean).strip()
    
    # We will search with multiple regex patterns. We order them from most specific to general.
    patterns = [
        # Explicit fields in tables or lists (e.g. "Payer: John Doe" or "Payer Name: John Doe")
        r'(?:payer|sender|remitter)(?:\s+name)?\s*[:\-]\s*([a-zA-Z\s\.\-\&]{3,40})',
        
        # Sentences like "received from John Doe via UPI" or "transferred by John Doe"
        # We allow an optional colon after from/by as well
        r'\b(?:from|by)\s*:?\s*([a-zA-Z\s\.\-\&]{3,40})'
    ]
    
    words_to_skip = {
        "your", "my", "slice", "account", "bank", "upi", "card", "rs", "rupees", "inr", 
        "customer", "user", "payment", "has", "been", "credited", "received", "transferred", 
        "by", "via", "on", "in", "to"
    }
    
    for pattern in patterns:
        for match in re.finditer(pattern, body_clean, re.IGNORECASE):
            name = match.group(1).strip()
            cleaned_name = clean_extracted_name(name)
            
            if len(cleaned_name) >= 3 and cleaned_name.lower() not in words_to_skip:
                return cleaned_name.title()
                
    return ""

def extract_amount_from_email(body: str) -> float | None:
    # Normalize body: replace newlines/tabs with space
    normalized = body.replace("\n", " ").replace("\r", " ")
    body_lower = normalized.lower()
    
    # Let's search using the same patterns as verify_amount_in_email
    patterns = [
        r'(?:received|credited|deposit|transfer|payment|added)\s+(?:value\s+)?(?:of\s+)?(?:rs\.?|₹|inr)?\s*([\d,]+(?:\.\d{1,2})?)',
        r'(?:rs\.?|₹|inr)?\s*([\d,]+(?:\.\d{1,2})?)\s*(?:received|credited|deposited|added|transfer)',
        r'(?:received|credited|deposit)\s+(?:rs\.?|₹|inr)?\s*([\d,]+(?:\.\d{1,2})?)'
    ]
    
    for pattern in patterns:
        for match in re.finditer(pattern, body_lower):
            val_str = match.group(1).replace(",", "")
            try:
                val = float(val_str)
                # Ignore values like 0 or very small or extremely large values that might be balances/dates
                if 1.0 <= val <= 100000.0:
                    return val
            except ValueError:
                continue
                
    # Fallback to general currency match
    fallback_patterns = [
        r'(?:rs\.?|₹|inr)\s*([\d,]+(?:\.\d{1,2})?)'
    ]
    for pattern in fallback_patterns:
        for match in re.finditer(pattern, body_lower):
            val_str = match.group(1).replace(",", "")
            try:
                val = float(val_str)
                if 1.0 <= val <= 100000.0:
                    return val
            except ValueError:
                continue
                
    return None

test_cases = [
    ("Rs. 149.00 has been credited to your slice account from JOHN DOE", "John Doe", 149.0),
    ("₹149 received from Smt. REKHA DEVI via UPI", "Smt. Rekha Devi", 149.0),
    ("You have received a payment of Rs.149 from John Doe", "John Doe", 149.0),
    ("Transfer of Rs.149 received from: JOHN DOE", "John Doe", 149.0),
    ("Payer: JOHN DOE", "John Doe", None),
    ("Payer Name: JOHN DOE", "John Doe", None),
    ("Sender Name: REKHA DEVI", "Rekha Devi", None),
    ("Remitter Name: REKHA DEVI", "Rekha Devi", None),
    ("credited to your account. Payer Name: JOHN DOE. UPI Ref ...", "John Doe", None),
    ("credited to account by JOHN DOE", "John Doe", None),
    ("Money received from JOHN DOE. UTR: 123456789012", "John Doe", None),
    ("from  JOHN DOE ", "John Doe", None),
    ("Rs. 149.00 credited to account from JOHN DOE has been credited", "John Doe", 149.0),
    ("credited to account from JOHN DOE (upi", "John Doe", None),
    ("Dear customer, Rs. 149 received from PRAMOD KUMAR SAHU via UPI.", "Pramod Kumar Sahu", 149.0),
    ("slice Hi Jeetesh, You have received ₹9 via UPI in your slice bank account xx9594. Avl. Bal. ₹110.01 Transaction date 25-Jun-26 From Jeetesh Meena RRN 617640765258 Best, Team slice Digital safety tips", "Jeetesh Meena", 9.0),
]

print("=== Running Payer Name & Amount Extraction Tests ===")
passed = 0
for idx, (body, expected_name, expected_amount) in enumerate(test_cases):
    actual_name = extract_payer_name_from_email(body)
    actual_amount = extract_amount_from_email(body)
    
    name_ok = (actual_name == expected_name)
    amount_ok = (expected_amount is None or actual_amount == expected_amount)
    
    status = "PASS" if (name_ok and amount_ok) else "FAIL"
    if name_ok and amount_ok:
        passed += 1
        
    print(f"Test #{idx+1}: {status}")
    print(f"  Input: {body}")
    print(f"  Expected Name: '{expected_name}' | Actual: '{actual_name}'")
    print(f"  Expected Amt: {expected_amount} | Actual: {actual_amount}")
    print("-" * 50)

print(f"Result: {passed}/{len(test_cases)} tests passed.")
