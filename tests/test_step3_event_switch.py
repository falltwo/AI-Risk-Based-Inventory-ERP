"""
tests/test_step3_event_switch.py
步驟 3 事件下拉：有多個正式事件時，使用者必須能從 A 切到 B。

原本卡片「查看分析」和 selectbox 都寫 active_risk_event_id，而 rerun 時又用它
強制覆寫 selectbox，於是選了 B 也會被拉回 A。跳轉改成一次性的 jump_to_risk_event_id。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _assigned_keys(tree):
    keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Attribute)
                        and target.value.attr == "session_state"
                        and isinstance(target.slice, ast.Constant)):
                    keys.add(target.slice.value)
    return keys


def test_card_jump_uses_one_shot_key_and_step3_consumes_it():
    supply_map = ast.parse((ROOT / "frontend/components/supply_map.py").read_text(encoding="utf-8"))
    dashboard_src = (ROOT / "frontend/components/risk_dashboard.py").read_text(encoding="utf-8")

    assert "jump_to_risk_event_id" in _assigned_keys(supply_map)
    assert "active_risk_event_id" not in _assigned_keys(supply_map)
    # 步驟 3 取出即清除，且不再拿 active_risk_event_id 去覆寫 selectbox
    assert 'st.session_state.pop("jump_to_risk_event_id"' in dashboard_src
    assert 'target_id = st.session_state["active_risk_event_id"]' not in dashboard_src
