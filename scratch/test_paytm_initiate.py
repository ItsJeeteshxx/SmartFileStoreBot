import sys
import httpx
import json
import uuid

try:
    import paytmchecksum
except ImportError:
    print("Error: paytmchecksum library not installed. Please run: pip install paytmchecksum pycryptodome")
    sys.exit(1)

# Default Test Credentials (Staging sandbox)
STAGING_MID = "TEST_tuslva67052853079507" # Example staging MID
STAGING_KEY = "YourStagingMerchantKeyHere" # Replace with actual key

def test_initiate(mid, merchant_key, order_id=None, amount=1.00, is_sandbox=True):
    if not order_id:
        order_id = f"TEST_{uuid.uuid4().hex[:12].upper()}"
        
    domain = "securegw-stage.paytm.in" if is_sandbox else "securegw.paytm.in"
    website = "WEBSTAGING" if is_sandbox else "DEFAULT"
    callback_url = "https://aryapremium.store/api/paytm-callback"
    
    body = {
        "requestType": "Payment",
        "mid": mid,
        "websiteName": website,
        "orderId": order_id,
        "callbackUrl": callback_url,
        "txnAmount": {
            "value": f"{amount:.2f}",
            "currency": "INR"
        },
        "userInfo": {
            "custId": "CUST_TEST_123"
        }
    }
    
    body_json = json.dumps(body)
    try:
        signature = paytmchecksum.generateSignature(body_json, merchant_key)
    except Exception as e:
        print(f"Error generating signature: {e}")
        return
        
    payload_data = {
        "body": body,
        "head": {
            "signature": signature
        }
    }
    
    url = f"https://{domain}/theia/api/v1/initiateTransaction?mid={mid}&orderId={order_id}"
    print(f"Sending request to: {url}")
    print(f"Payload body: {json.dumps(body, indent=2)}")
    print(f"Signature: {signature}")
    
    try:
        r = httpx.post(url, json=payload_data, headers={"Content-Type": "application/json"}, timeout=15.0)
        print(f"Status Code: {r.status_code}")
        print("Raw Response:")
        print(json.dumps(r.json(), indent=2))
    except Exception as e:
        print(f"Request failed: {e}")

if __name__ == "__main__":
    if len(sys.argv) > 2:
        mid = sys.argv[1]
        key = sys.argv[2]
        sandbox = "test" in mid.lower() or mid.startswith("TEST_")
        print(f"Testing with provided credentials (sandbox={sandbox})...")
        test_initiate(mid, key, is_sandbox=sandbox)
    else:
        print("Usage: python test_paytm_initiate.py <MID> <MERCHANT_KEY>")
        print("Example (running with default sandbox placeholder):")
        test_initiate("TEST_MID_PLACEHOLDER", "KEY_PLACEHOLDER", is_sandbox=True)
