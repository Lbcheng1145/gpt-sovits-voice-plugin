"""分群开关回归：四条出语音的路径都受 ``[sessions]`` 约束。

背景：``/tts`` 命令、LLM 工具 ``tts_voice``、概率语音、语音对讲此前都没有
会话级控制，在人多或不适合语音的群里容易误发。这里覆盖总开关、白名单、
黑名单、以及「会话 → 群号」映射四种判定。

工具调用路径宿主只给 ``stream_id`` 不给群号，所以配置里填群号时依赖
``_learn_session_group`` 从入站消息学到的映射，这一条单独覆盖。
"""

import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 目录名带 -，不是合法包名，注册一个假包让相对导入生效
_pkg = types.ModuleType("gptpkg")
_pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("gptpkg", _pkg)
plugin = importlib.import_module("gptpkg.plugin")
config = importlib.import_module("gptpkg.config")

RESULTS = []
GROUP = "1015610655"


def check(label, got, expect):
    ok = got == expect
    RESULTS.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'} {label}: {got!r}")
    if not ok:
        print(f"        expect={expect!r}")


def make_plugin(sessions, mapping=None):
    """只带判定所需状态的轻量替身。

    ``config`` 在插件上是只读 property，不能直接赋值，所以不实例化插件，
    改用 SimpleNamespace 冒充 self 去调未绑定方法。
    """

    return SimpleNamespace(
        config=SimpleNamespace(sessions=sessions),
        _session_group=dict(mapping or {}),
    )


def allowed(fake, stream_id, group_id=""):
    """调插件里的判定逻辑（等同一个会话能不能出语音）。"""

    return plugin.GPTSovitsVoicePlugin._is_session_allowed(fake, stream_id, group_id)


def learn(fake, message):
    """调插件里的映射学习逻辑。"""

    return plugin.GPTSovitsVoicePlugin._learn_session_group(fake, message)


def test_disabled():
    print("--- 总开关关闭（保持原行为）---")
    p = make_plugin(config.SessionsConfig(enabled=False))
    check("任何会话都放行", allowed(p, "s1", GROUP), True)
    check("只给 stream_id 也放行", allowed(p, "s1"), True)


def test_whitelist():
    print("--- 白名单 ---")
    p = make_plugin(config.SessionsConfig(enabled=True, mode="whitelist", allow=[GROUP]))
    check("命中群号", allowed(p, "s1", GROUP), True)
    check("未命中群号", allowed(p, "s2", "999999999"), False)


def test_learned_mapping():
    print("--- 工具路径（宿主只给 stream_id）---")
    p = make_plugin(
        config.SessionsConfig(enabled=True, mode="whitelist", allow=[GROUP]),
        {"sess_known": GROUP},
    )
    check("靠映射命中", allowed(p, "sess_known"), True)
    check("靠映射未命中", allowed(p, "sess_other"), False)


def test_empty_whitelist():
    print("--- 白名单为空 ---")
    p = make_plugin(config.SessionsConfig(enabled=True, mode="whitelist", allow=[]))
    check("一律禁止", allowed(p, "s1", GROUP), False)


def test_blacklist():
    print("--- 黑名单 ---")
    p = make_plugin(config.SessionsConfig(enabled=True, mode="blacklist", deny=[GROUP]))
    check("命中则禁", allowed(p, "s1", GROUP), False)
    check("未命中放行", allowed(p, "s2", "888888888"), True)


def test_deny_wins():
    print("--- deny 优先于 allow ---")
    p = make_plugin(
        config.SessionsConfig(enabled=True, mode="whitelist", allow=[GROUP], deny=[GROUP])
    )
    check("同时出现在两边按禁止", allowed(p, "s1", GROUP), False)


def test_stream_id_listed():
    print("--- stream_id 也能直接进名单 ---")
    p = make_plugin(config.SessionsConfig(enabled=True, mode="whitelist", allow=["s_xx"]))
    check("按会话 ID 放行", allowed(p, "s_xx"), True)


def test_learn_mapping():
    print("--- 映射学习 ---")
    p = make_plugin(config.SessionsConfig())
    learn(p, 
        {"session_id": "sess_a", "message_info": {"group_info": {"group_id": GROUP}}}
    )
    check("学到映射", p._session_group.get("sess_a"), GROUP)
    # 私聊的 group_info 是 None，不应该往表里写空值
    learn(p, {"session_id": "sess_b", "message_info": {"group_info": None}})
    check("私聊不入表", "sess_b" in p._session_group, False)
    # 群号缺失时同样不写
    learn(p, 
        {"session_id": "sess_c", "message_info": {"group_info": {"group_id": ""}}}
    )
    check("空群号不入表", "sess_c" in p._session_group, False)


def run(round_no: int) -> bool:
    print(f"===== 第 {round_no} 轮 =====")
    before = len(RESULTS)
    test_disabled()
    test_whitelist()
    test_learned_mapping()
    test_empty_whitelist()
    test_blacklist()
    test_deny_wins()
    test_stream_id_listed()
    test_learn_mapping()
    return all(RESULTS[before:])


if __name__ == "__main__":
    results = [run(i) for i in (1, 2, 3)]
    print(f"\n共 {len(RESULTS)} 个断言，失败 {RESULTS.count(False)} 个")
    print("三遍结果:", results)
    sys.exit(0 if all(results) else 1)
