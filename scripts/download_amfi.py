import os
import io
import requests
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "amfi")

# The URLs could not be verified automatically as AMFI returns 404 for guessing
# patterns and blocks scraper user agents. 
# They must be populated manually after verifying on amfiindia.com.
AMFI_URLS = {
    "2019-H2": "https://www.amfiindia.com/Themes/Theme1/downloads/Avg.%20Market%20Capitalization%20of%20listed%20companies%20during%20Jul-Dec%202019.xlsx",
    "2020-H1": "https://www.amfiindia.com/Themes/Theme1/downloads/Average%20Market%20Capitalization%20of%20Listed%20Companies%20during%20Jan%20-%20Jun%202020_Final.xlsx",
    "2020-H2": "https://www.amfiindia.com/Themes/Theme1/downloads/Average%20Market%20Capitalization%20of%20Listed%20Companies%20during%20Jul%20-%20Dec%202020_Final.xlsx",
    "2021-H1": "https://www.amfiindia.com/Themes/Theme1/downloads/Average%20Market%20Capitalization%20of%20List%20Companies%20during%20Jan-June%202021.xlsx",
    "2021-H2": "https://www.amfiindia.com/Themes/Theme1/downloads/Average%20Market%20Capitalisation%20of%20Listed%20Companies%20during%20Jul%20-%20Dec%202021.xlsx",
    "2022-H1": "https://www.amfiindia.com/Themes/Theme1/downloads/AverageMarketCapitalizationoflistedcompaniesduringthesixmonthsended30June2022.xlsx",
    "2022-H2": "https://www.amfiindia.com/Themes/Theme1/downloads/AverageMarketCapitalizationoflistedcompaniesduringthesixmonthsended31Dec2022.xlsx",
    "2023-H1": "https://www.amfiindia.com/Themes/Theme1/downloads/AverageMarketCapitalizationoflistedcompaniesduringthesixmonthsended30Jun2023.xlsx",
    "2023-H2": "https://www.amfiindia.com/Themes/Theme1/downloads/AverageMarketCapitalizationoflistedcompaniesduringthesixmonthsended31Dec2023.xlsx",
    "2024-H1": "https://www.amfiindia.com/uploads/Average_Market_Capitalization_30_Jun2024_2a1ab4c1d8.xlsx",
    "2024-H2": "https://www.amfiindia.com/Themes/Theme1/downloads/AverageMarketCapitalizationoflistedcompaniesduringthesixmonthsended31Dec2024.xlsx",
    "2025-H1": "https://www.amfiindia.com/Themes/Theme1/downloads/AverageMarketCapitalization30Jun2025.xlsx",
    "2025-H2": "https://www.amfiindia.com/Themes/Theme1/downloads/AverageMarketCapitalization31Dec2025.xlsx",
    "2026-H1": "https://portal.amfiindia.com/spages/AverageMarketCapitalization30Jun2026.xlsx",
}

def is_valid_amfi_excel(content: bytes) -> tuple[bool, str]:
    """Check if the downloaded content is a valid AMFI classification Excel file."""
    try:
        df = pd.read_excel(io.BytesIO(content))
    except Exception as e:
        return False, f"Failed to parse Excel file: {str(e)}"
    
    # AMFI files sometimes have headers on the 2nd or 3rd row, so we scan the first few rows 
    # to see if the required columns exist.
    required_cols = ["symbol", "isin"]
    
    # Check if any row contains string values that match our required headers
    # Pandas will often put the actual headers in row 0 or 1 if it didn't guess right.
    found_cols = set()
    
    # Check actual DataFrame columns first
    for col in df.columns:
        col_lower = str(col).lower()
        if "symbol" in col_lower:
            found_cols.add("symbol")
        if "isin" in col_lower:
            found_cols.add("isin")
    
    # Check first 5 rows just in case headers are embedded
    for _, row in df.head(5).iterrows():
        for val in row.values:
            val_lower = str(val).lower()
            if "symbol" in val_lower:
                found_cols.add("symbol")
            if "isin" in val_lower:
                found_cols.add("isin")
                
    if not all(col in found_cols for col in required_cols):
        return False, f"Missing required columns (Symbol/ISIN). Found columns: {list(df.columns)}"
        
    return True, ""


def check_existing_file(filepath: str) -> tuple[bool, str]:
    """Check if an existing file is already a valid AMFI Excel file."""
    if not os.path.exists(filepath):
        return False, "File does not exist"
        
    with open(filepath, "rb") as f:
        content = f.read()
    
    is_valid, _ = is_valid_amfi_excel(content)
    if is_valid:
        return True, "Valid file already exists"
    return False, "Existing file is invalid"


def download_amfi_files():
    print(f"Target directory: {DATA_DIR}")
    os.makedirs(DATA_DIR, exist_ok=True)
    
    results = {
        "success": [],
        "failed": [],
        "skipped": []
    }
    
    for period, url in AMFI_URLS.items():
        filepath = os.path.join(DATA_DIR, f"{period}.xlsx")
        
        # 1. Check existing
        is_valid, msg = check_existing_file(filepath)
        if is_valid:
            results["skipped"].append((period, msg))
            print(f"[{period}] SKIPPED: {msg}")
            continue
            
        # 2. Check URL
        if not url:
            msg = "No verifiable official URL provided"
            results["failed"].append((period, msg))
            print(f"[{period}] FAILED: {msg}")
            continue
            
        print(f"[{period}] Downloading from {url} ...")
        try:
            # Implement reasonable timeouts and headers
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, application/vnd.ms-excel, */*"
            }
            response = requests.get(url, headers=headers, timeout=(10, 30))
            
            # 3. Check HTTP status
            if response.status_code != 200:
                msg = f"HTTP {response.status_code}"
                results["failed"].append((period, msg))
                print(f"[{period}] FAILED: {msg}")
                continue
                
            # 4. Verify Excel content & columns
            is_valid, err_msg = is_valid_amfi_excel(response.content)
            if not is_valid:
                results["failed"].append((period, err_msg))
                print(f"[{period}] FAILED: {err_msg}")
                continue
                
            # 5. Save file
            with open(filepath, "wb") as f:
                f.write(response.content)
                
            results["success"].append(period)
            print(f"[{period}] SUCCESS: Downloaded and verified.")
            
        except requests.exceptions.RequestException as e:
            msg = f"Network error: {str(e)}"
            results["failed"].append((period, msg))
            print(f"[{period}] FAILED: {msg}")
            
    print("\n" + "="*40)
    print("DOWNLOAD SUMMARY")
    print("="*40)
    print(f"Total Requested: {len(AMFI_URLS)}")
    print(f"Successful:      {len(results['success'])}")
    print(f"Failed:          {len(results['failed'])}")
    print(f"Skipped (Valid): {len(results['skipped'])}")
    print("\nSUCCESS:")
    for p in results["success"]:
        print(f"  {p}")
    print("\nFAILED:")
    for p, msg in results["failed"]:
        print(f"  {p} — {msg}")
    print("\nSKIPPED:")
    for p, msg in results["skipped"]:
        print(f"  {p} — {msg}")

if __name__ == "__main__":
    download_amfi_files()
