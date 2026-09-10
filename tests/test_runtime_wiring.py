"""
أن تكون الوحدة قابلة للاستيراد لا يعني أن دوالّها قابلة للنداء.

سبب وجود هذا الملف: بعد حذف الاستراتيجية القديمة بقي `process_commands`
يستورد `risk_manager` **داخل جسم الدالة**. `import main` نجح، والاختبارات
نجحت، والبوت أقلع وأرسل "يعمل" — ثم رمى ImportError في أول سطر من كل دورة
فلم يفحص أي أداة طوال 23 ساعة. كل المؤشرات الخارجية كانت خضراء.

الدرس: الاستيراد المؤجَّل داخل دالة لا يُكتشف إلا بندائها. لذلك تُنادى هنا
كل دالة يمرّ بها مسار التشغيل، لا تُستورد فقط.
"""

import ast
import importlib
import os
import sys
from pathlib import Path

import pytest

BOT = Path(__file__).resolve().parent.parent / "bot"


def _load(*names):
    before = set(sys.modules)
    sys.path.insert(0, str(BOT))
    try:
        return [importlib.import_module(n) for n in names]
    finally:
        sys.path.remove(str(BOT))
        for name in set(sys.modules) - before:
            del sys.modules[name]


def test_every_bot_module_imports():
    mods = sorted(p.stem for p in BOT.glob("*.py") if p.stem != "__init__")
    assert mods, "لم يُعثر على أي وحدة في bot/"
    _load(*mods)


def test_no_module_imports_something_that_does_not_exist():
    """
    يمسح كل تعليمة import في bot/ — بما فيها الموجودة داخل الدوال — ويتأكد
    أن كل اسم محلّي له ملف فعلي. هذا هو الفحص الذي كان سيمنع العطل.
    """
    local = {p.stem for p in BOT.glob("*.py")}
    missing = []
    for path in sorted(BOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            for n in names:
                # اسم يشبه وحدة محلية (ليس حزمة مثبّتة) لكن ملفه غير موجود
                if n not in local and (BOT / f"{n}.py").exists() is False:
                    try:
                        importlib.import_module(n)
                    except ImportError:
                        missing.append(f"{path.name}:{node.lineno} → {n}")
    assert not missing, "استيراد لوحدة غير موجودة:\n" + "\n".join(missing)


def test_process_commands_is_callable_with_no_updates(monkeypatch):
    """
    النداء الفعلي — وهو ما يكشف الاستيراد المؤجَّل. مع صفر تحديثات يجب أن
    تُرجع الحالة كما هي دون أي شبكة.
    """
    (tg,) = _load("telegram_bot")
    monkeypatch.setattr(tg, "get_updates", lambda offset=0: [])
    state = {"mode": "alert_only", "paused": False, "offset": 0}
    assert tg.process_commands(state) == state


@pytest.mark.parametrize("cmd,expect_paused", [("/pause", True), ("/resume", False)])
def test_pause_and_resume_commands(monkeypatch, cmd, expect_paused):
    (tg,) = _load("telegram_bot")
    sent = []
    monkeypatch.setattr(tg, "get_updates",
                        lambda offset=0: [{"update_id": 1,
                                           "message": {"text": cmd}}])
    monkeypatch.setattr(tg, "send_message", lambda text: sent.append(text))
    state = tg.process_commands({"mode": "alert_only", "paused": not expect_paused,
                                 "offset": 0})
    assert state["paused"] is expect_paused
    assert state["offset"] == 2          # تقدّم حتى لا يُعاد الأمر نفسه
    assert sent


def test_status_command_uses_the_callback(monkeypatch):
    (tg,) = _load("telegram_bot")
    sent = []
    monkeypatch.setattr(tg, "get_updates",
                        lambda offset=0: [{"update_id": 7,
                                           "message": {"text": "/status"}}])
    monkeypatch.setattr(tg, "send_message", lambda text: sent.append(text))
    tg.process_commands({"mode": "alert_only", "paused": False, "offset": 0},
                        status_fn=lambda: ["• XAUUSD BUY دخول 3650.00"])
    assert sent and "XAUUSD" in sent[0]


def test_main_status_lines_reports_open_positions():
    main, position = _load("main", "position")
    assert main._status_lines() == ["لا صفقات قائمة"]
    main._positions["XAUUSD"] = position.open_position("XAUUSD", "BUY", 3650.0, 12.0)
    try:
        line = main._status_lines()[0]
        assert "XAUUSD" in line and "BUY" in line
    finally:
        main._positions.clear()
