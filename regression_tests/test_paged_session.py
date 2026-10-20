import asyncio, json
from pathlib import Path
import pytest
from session_probe import verify_case
ROOT=Path(__file__).resolve().parent.parent

@pytest.mark.parametrize('segmentation',['split','whole'])
@pytest.mark.parametrize('capture,golden',[('paged-session','public'),('shared-session','shared'),('sparse-session','sparse'),('frontier-session','frontier'),('striped-session','striped'),*[(f'recovery-public-{i}',f'recovery-public-{i}') for i in range(4)],*[(f'joint-public-{i}',f'joint-public-{i}') for i in range(4)],*[(f'continuity-public-{m}',f'continuity-public-{m}') for m in ['success','atomic-replace']]])
def test_paged_session(segmentation,capture,golden):
    expected=json.loads((ROOT/f'regression_tests/data/{golden}.json').read_text())
    asyncio.run(verify_case(ROOT/'captures'/capture,expected,segmentation))
