import os
import io
import pandas as pd
import pytest
from unittest.mock import patch, MagicMock

from scripts.download_amfi import is_valid_amfi_excel, check_existing_file, download_amfi_files, AMFI_URLS, DATA_DIR

def create_mock_excel(valid=True):
    """Helper to generate in-memory Excel file bytes."""
    cols = ["Symbol", "ISIN", "Market Cap"] if valid else ["Col1", "Col2", "Col3"]
    df = pd.DataFrame({
        cols[0]: ["RELIANCE", "TCS"] if valid else ["Val1", "Val2"],
        cols[1]: ["INE123", "INE456"] if valid else ["Val3", "Val4"],
        cols[2]: ["Large", "Large"] if valid else ["Val5", "Val6"]
    })
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False)
    return output.getvalue()


def test_is_valid_amfi_excel_success():
    content = create_mock_excel(valid=True)
    is_valid, msg = is_valid_amfi_excel(content)
    assert is_valid is True
    assert msg == ""


def test_is_valid_amfi_excel_missing_columns():
    content = create_mock_excel(valid=False)
    is_valid, msg = is_valid_amfi_excel(content)
    assert is_valid is False
    assert "Missing required columns" in msg


def test_is_valid_amfi_excel_invalid_bytes():
    content = b"This is just some text, not an Excel file."
    is_valid, msg = is_valid_amfi_excel(content)
    assert is_valid is False
    assert "Failed to parse Excel file" in msg


def test_check_existing_file_not_exists(tmp_path):
    filepath = str(tmp_path / "nonexistent.xlsx")
    is_valid, msg = check_existing_file(filepath)
    assert is_valid is False
    assert msg == "File does not exist"


def test_check_existing_file_valid(tmp_path):
    filepath = str(tmp_path / "valid.xlsx")
    with open(filepath, "wb") as f:
        f.write(create_mock_excel(valid=True))
    
    is_valid, msg = check_existing_file(filepath)
    assert is_valid is True
    assert msg == "Valid file already exists"


@patch("scripts.download_amfi.requests.get")
@patch("scripts.download_amfi.DATA_DIR", new_callable=lambda: "/tmp/mock_amfi")
def test_download_amfi_files_http_failure(mock_data_dir, mock_get, capsys, tmp_path):
    # Setup mock URLs
    for k in AMFI_URLS:
        AMFI_URLS[k] = "http://mock.url"
    
    # Mock HTTP 404 response
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_get.return_value = mock_response
    
    with patch("scripts.download_amfi.DATA_DIR", str(tmp_path)):
        download_amfi_files()
        
    captured = capsys.readouterr()
    assert "FAILED: HTTP 404" in captured.out
    
    # Reset URLs
    for k in AMFI_URLS:
        AMFI_URLS[k] = None


@patch("scripts.download_amfi.requests.get")
@patch("scripts.download_amfi.check_existing_file")
def test_download_amfi_files_skip_existing(mock_check, mock_get, capsys, tmp_path):
    # Setup mock URLs
    AMFI_URLS["2019-H2"] = "http://mock.url"
    
    # Mock check_existing_file to return True for the first file
    mock_check.return_value = (True, "Valid file already exists")
    
    with patch("scripts.download_amfi.DATA_DIR", str(tmp_path)):
        download_amfi_files()
        
    captured = capsys.readouterr()
    assert "[2019-H2] SKIPPED: Valid file already exists" in captured.out
    
    # Reset URLs
    for k in AMFI_URLS:
        AMFI_URLS[k] = None


@patch("scripts.download_amfi.requests.get")
def test_download_amfi_files_success(mock_get, capsys, tmp_path):
    AMFI_URLS["2019-H2"] = "http://mock.url"
    
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = create_mock_excel(valid=True)
    mock_get.return_value = mock_response
    
    with patch("scripts.download_amfi.DATA_DIR", str(tmp_path)):
        download_amfi_files()
        
    captured = capsys.readouterr()
    assert "SUCCESS: Downloaded and verified" in captured.out
    
    # Verify file was saved
    assert os.path.exists(os.path.join(str(tmp_path), "2019-H2.xlsx"))
    
    # Reset URLs
    for k in AMFI_URLS:
        AMFI_URLS[k] = None
