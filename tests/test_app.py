import re
from pathlib import Path

def test_page_config():
    app_code = Path('AllInOneDataApp/app/app.py').read_text(encoding='utf-8')
    assert "st.set_page_config" in app_code, "st.set_page_config is not set in app"

def test_title_present():
    app_code = Path('AllInOneDataApp/app/app.py').read_text(encoding='utf-8')
    pattern = r"st.title\(\s*\"All-in-One Data App\""
    assert re.search(pattern, app_code), "App title 'All-in-One Data App' is missing or not set correctly"
