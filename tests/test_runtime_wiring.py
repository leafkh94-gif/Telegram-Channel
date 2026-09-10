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
    monkeypatch.setattr(tg, "send_message",
                        lambda text, chat_id=None: sent.append(text))
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
    monkeypatch.setattr(tg, "send_message",
                        lambda text, chat_id=None: sent.append(text))
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


def test_get_updates_reports_a_blocked_receive_channel(monkeypatch, caplog):
    """
    409 من تيليغرام يعني webhook يحجب السحب. الصمت عنه يجعل "لا أوامر" و
    "الاستقبال معطّل" متطابقين — وهو الالتباس نفسه الذي أخفى توقّف الفحص.
    """
    (tg,) = _load("telegram_bot")

    class R:
        status_code = 409
    monkeypatch.setattr(tg.requests, "get", lambda *a, **k: R())
    with caplog.at_level("ERROR"):
        assert tg.get_updates() == []
    assert any("409" in r.message or "webhook" in r.message for r in caplog.records)


def test_get_updates_logs_network_failure_instead_of_swallowing(monkeypatch, caplog):
    (tg,) = _load("telegram_bot")

    def boom(*a, **k):
        raise ConnectionError("no route")
    monkeypatch.setattr(tg.requests, "get", boom)
    with caplog.at_level("WARNING"):
        assert tg.get_updates() == []
    assert any("ConnectionError" in r.message for r in caplog.records)


def test_check_receive_deletes_a_stale_webhook(monkeypatch):
    """webhook باقٍ من كود محذوف يحجب الأوامر ولا يخدم أحداً — يُحذف."""
    (tg,) = _load("telegram_bot")
    posted = []

    class G:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"result": {"url": "https://old.example/hook"}}

    class P:
        status_code = 200
        def raise_for_status(self): pass

    monkeypatch.setattr(tg.requests, "get", lambda *a, **k: G())
    monkeypatch.setattr(tg.requests, "post",
                        lambda *a, **k: (posted.append(a), P())[1])
    msg = tg.check_receive()
    assert msg and "webhook" in msg
    assert posted, "لم يُطلب حذف الـwebhook"


def test_check_receive_is_quiet_when_nothing_is_wrong(monkeypatch):
    (tg,) = _load("telegram_bot")

    class G:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"result": {"url": ""}}

    monkeypatch.setattr(tg.requests, "get", lambda *a, **k: G())
    assert tg.check_receive() is None


def _update(kind, text, chat_id=-100123, uid=1):
    return {"update_id": uid, kind: {"text": text, "chat": {"id": chat_id}}}


@pytest.mark.parametrize("kind", ["message", "channel_post",
                                  "edited_message", "edited_channel_post"])
def test_commands_are_read_from_every_update_kind(monkeypatch, kind):
    """
    أمر مكتوب في قناة يصل كـchannel_post لا message. قراءة "message" وحدها
    كانت تُسقطه بصمت — والإشارات هنا تُنشر في قناة، فبدا البوت أصمّ تماماً.
    """
    (tg,) = _load("telegram_bot")
    sent = []
    monkeypatch.setattr(tg, "get_updates", lambda offset=0: [_update(kind, "/status")])
    monkeypatch.setattr(tg, "send_message",
                        lambda text, chat_id=None: sent.append((text, chat_id)))
    tg.process_commands({"mode": "alert_only", "paused": False, "offset": 0})
    assert sent, f"أُهمل الأمر الوارد كـ{kind}"


def test_reply_goes_back_to_where_the_command_was_written(monkeypatch):
    (tg,) = _load("telegram_bot")
    sent = []
    monkeypatch.setattr(tg, "get_updates",
                        lambda offset=0: [_update("message", "/status", chat_id=555)])
    monkeypatch.setattr(tg, "send_message",
                        lambda text, chat_id=None: sent.append((text, chat_id)))
    tg.process_commands({"mode": "alert_only", "paused": False, "offset": 0})
    assert sent[0][1] == 555, "الردّ لم يعد إلى محادثة الأمر"


def test_command_with_bot_suffix_is_understood(monkeypatch):
    """في المجموعات والقنوات يكتب تيليغرام الأمر بصيغة /status@BotName."""
    (tg,) = _load("telegram_bot")
    sent = []
    monkeypatch.setattr(tg, "get_updates",
                        lambda offset=0: [_update("message", "/status@MyTradeBot")])
    monkeypatch.setattr(tg, "send_message",
                        lambda text, chat_id=None: sent.append(text))
    tg.process_commands({"mode": "alert_only", "paused": False, "offset": 0})
    assert sent


def test_update_without_text_is_skipped(monkeypatch):
    """صورة أو انضمام عضو يصل بلا نص — يجب ألا يُسقط الحلقة."""
    (tg,) = _load("telegram_bot")
    monkeypatch.setattr(tg, "get_updates",
                        lambda offset=0: [{"update_id": 3,
                                           "message": {"chat": {"id": 1}}}])
    st = tg.process_commands({"mode": "alert_only", "paused": False, "offset": 0})
    assert st["offset"] == 4
