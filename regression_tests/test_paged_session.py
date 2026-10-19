import asyncio, json
from pathlib import Path
import pytest
from session_probe import verify_case
ROOT=Path(__file__).resolve().parent.parent

@pytest.mark.parametrize('segmentation',['split','whole'])
def test_paged_session(segmentation):
    expected=json.loads((ROOT/'regression_tests/data/public.json').read_text())
    asyncio.run(verify_case(ROOT/'captures/paged-session',expected,segmentation))
