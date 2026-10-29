"""远程服务场景回归：本机才校验参考音频，远程交给服务端判断。

背景：用户反馈「不能连接别的服务器，参考音频存在校验是在本地路径」。
原来 validate_style 无条件 os.path.exists(refer_wav)，远程部署时 refer_wav
是服务器上的路径，本地必然不存在，于是插件在发请求前就把远程用户挡掉了。
"""

import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, r"C:/Users/luboc/AppData/Roaming/MaiBotOneKeyDesktop/0f4edb67bb15/python-env/Lib/site-packages")

# 目录名带 -，不是合法包名，注册一个假包让相对导入生效
_pkg = types.ModuleType("gptpkg")
_pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("gptpkg", _pkg)
gpt_sovits = importlib.import_module("gptpkg.gpt_sovits")

RESULTS = []


def make_client(server: str, refer_wav: str = "", prompt_text: str = "你好"):
    client = object.__new__(gpt_sovits.GPTSovitsClient)
    client._config = SimpleNamespace(
        server=server,
        styles=[
            SimpleNamespace(name="default", refer_wav=refer_wav, prompt_text=prompt_text)
        ],
    )
    return client


def check(label, got, expect):
    ok = got == expect
    RESULTS.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'} {label}: {got!r}")
    if not ok:
        print(f"        expect={expect!r}")


LOCAL_SERVERS = [
    "http://127.0.0.1:9880",
    "http://localhost:9880",
    "http://0.0.0.0:9880",
    "http://[::1]:9880",
    "127.0.0.1:9880",
]
REMOTE_SERVERS = [
    "http://192.168.1.10:9880",
    "https://tts.example.com",
    "http://10.0.0.5:9880",
    "http://my-nas:9880",
]


def test_is_local_server():
    print("--- _is_local_server ---")
    for s in LOCAL_SERVERS:
        check(f"本机 {s}", make_client(s)._is_local_server(), True)
    for s in REMOTE_SERVERS:
        check(f"远程 {s}", make_client(s)._is_local_server(), False)
    check("空地址按本机处理", make_client("")._is_local_server(), True)
    check("乱写按本机处理", make_client("不是个地址")._is_local_server(), True)


def test_validate_remote():
    print("--- validate_style 远程场景 ---")
    missing_path = "/root/GPT-SoVITS/refs/narrator.wav"

    # 远程 + 本地没有这个文件 → 必须放行（这正是用户被卡住的地方）
    remote = make_client("http://192.168.1.10:9880", refer_wav=missing_path)
    check("远程服务不校验本地文件", remote.validate_style("default"), None)

    # 本机 + 文件不存在 → 仍然要拦，保留原来的提示
    local = make_client("http://127.0.0.1:9880", refer_wav=missing_path)
    err = local.validate_style("default")
    check("本机服务仍然校验文件", err is not None, True)
    check("提示里带路径", missing_path in (err or ""), True)

    # 远程但 refer_wav 为空 → 还是要提示补配置
    empty = make_client("http://192.168.1.10:9880", refer_wav="")
    check("远程但没填路径仍报错", empty.validate_style("default") is not None, True)

    # 远程但没填 prompt_text → 同样要提示
    no_text = make_client("http://192.168.1.10:9880", refer_wav=missing_path, prompt_text="")
    check("远程但没填文本仍报错", no_text.validate_style("default") is not None, True)

    # 风格不存在
    unknown = make_client("http://192.168.1.10:9880", refer_wav=missing_path)
    check("风格不存在报错", unknown.validate_style("nope") is not None, True)

    # 本机 + 文件真实存在 → 放行
    real = ROOT / "README.md"
    ok_client = make_client("http://127.0.0.1:9880", refer_wav=str(real))
    check("本机文件存在时放行", ok_client.validate_style("default"), None)


def run(round_no: int) -> bool:
    print(f"===== 第 {round_no} 轮 =====")
    before = len(RESULTS)
    test_is_local_server()
    test_validate_remote()
    return all(RESULTS[before:])


if __name__ == "__main__":
    results = [run(i) for i in (1, 2, 3)]
    print(f"\n共 {len(RESULTS)} 个断言，失败 {RESULTS.count(False)} 个")
    print("三遍结果:", results)
    sys.exit(0 if all(results) else 1)
