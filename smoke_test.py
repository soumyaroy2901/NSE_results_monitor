import logging
import sys
import os
from nse_results_telegram_bot import NSEClient, NSEAccessError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

def main():
    print("--------------------------------------------------")
    print("SMOKE TEST: Checking connection to NSE servers...")
    print("--------------------------------------------------")
    
    proxy = os.getenv("NSE_RESULT_PROXY_URL")
    if proxy:
        print(f"Using proxy: {proxy}")
    else:
        print("No proxy configured (NSE_RESULT_PROXY_URL is empty).")

    nse = NSEClient(proxy_url=proxy)
    try:
        print("\nStep 1: Warming up session (fetching home page & cookies)...")
        nse.warm_up()
        print("✅ Warm-up successful.")

        print("\nStep 2: Fetching corporate announcements API...")
        url = "https://www.nseindia.com/api/corporate-announcements?index=equities"
        referer = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
        data = nse.get_json(url, referer)
        
        print("✅ API fetch successful.")
        print(f"   Received {len(data)} announcements records in the response.")
        
        print("\n🎉 SMOKE TEST PASSED: Render/Current IP is NOT blocked by NSE!")
        return 0

    except NSEAccessError as e:
        print("\n❌ SMOKE TEST FAILED: NSE Access Error (Likely IP Blocked)")
        print(f"   Details: {e}")
        print("\nIf you are seeing this on Render, their IP range is blocked by NSE.")
        print("You will need to set a residential/datacenter proxy in the NSE_RESULT_PROXY_URL environment variable.")
        return 1
    except Exception as e:
        print("\n❌ SMOKE TEST FAILED: Unexpected Error")
        print(f"   Details: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
