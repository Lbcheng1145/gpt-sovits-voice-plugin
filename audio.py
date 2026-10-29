"""音频发送：base64 / 文件路径双模式。"""

import asyncio
import base64
import logging
import os
import uuid
from typing import TYPE_CHECKING, Optional, Tuple

from .config import GeneralConfig

if TYPE_CHECKING:
    from maibot_sdk.context import PluginContext

logger = logging.getLogger("plugin.gpt_sovits_voice.audio")

# 文件路径模式发送时，临时音频的延迟清理时间（秒），给 adapter 上传留足时间
CLEANUP_DELAY = 180


class AudioSender:
    """把合成好的音频发送到会话：base64 直接走 IPC，或写文件后发 voiceurl。"""

    def __init__(self, config: GeneralConfig, project_root: str) -> None:
        self._config = config
        self._project_root = project_root
        # 持有延迟清理任务的强引用。asyncio 只保存任务的弱引用，
        # 不持有的话任务可能在 sleep 期间被 GC 回收，临时文件就永远留着了
        self._cleanup_tasks: set[asyncio.Task] = set()
        # 尚未清理的文件路径。任务被取消时靠它兜底删除，避免残留。
        self._pending_paths: set[str] = set()
        self._http_runner: Optional[Any] = None
        self._http_site: Optional[Any] = None
        self._output_dir_cached: Optional[str] = None

    async def start_http_server(self, ctx: "PluginContext") -> None:
        """若开启 enable_http_server，启动内置的 aiohttp.web 文件服务供远端客户端下载。"""
        if not getattr(self._config, "enable_http_server", False):
            return

        if self._http_runner is not None:
            return

        from aiohttp import web

        output_dir = os.path.abspath(self._resolve_output_dir(ctx))
        self._output_dir_cached = output_dir

        async def handle_voice(request: web.Request) -> web.Response:
            filename = request.match_info.get("filename", "")
            if not filename or ".." in filename or "/" in filename or "\\" in filename:
                raise web.HTTPForbidden(text="Invalid filename")
            if not filename.endswith(".wav"):
                raise web.HTTPForbidden(text="Invalid file type")

            filepath = os.path.join(output_dir, filename)
            if not os.path.isfile(filepath):
                raise web.HTTPNotFound(text="Voice file not found or expired")

            return web.FileResponse(filepath, headers={"Content-Type": "audio/wav"})

        app = web.Application()
        app.router.add_get("/voice/{filename}", handle_voice)

        host = (getattr(self._config, "http_host", "0.0.0.0") or "0.0.0.0").strip()
        port = int(getattr(self._config, "http_port", 9885) or 9885)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        self._http_runner = runner
        self._http_site = site
        logger.info("已启动语音 HTTP 回调服务: http://%s:%d/voice/", host, port)

    async def stop_http_server(self) -> None:
        """停止 HTTP 文件服务。"""
        if self._http_runner is not None:
            try:
                await self._http_runner.cleanup()
            except Exception as exc:
                logger.debug("关闭语音 HTTP 服务失败: %s", exc)
            self._http_runner = None
            self._http_site = None

    def get_public_url(self, audio_path: str) -> str:
        """根据音频文件本地路径生成供外部客户端拉取的 HTTP 链接。"""
        filename = os.path.basename(audio_path)
        base = (getattr(self._config, "http_public_base", "") or "").strip().rstrip("/")
        if not base:
            port = int(getattr(self._config, "http_port", 9885) or 9885)
            local_ip = self._detect_local_ip()
            base = f"http://{local_ip}:{port}"
        return f"{base}/voice/{filename}"

    @staticmethod
    def _detect_local_ip() -> str:
        """探测本机在局域网内可路由的 IPv4 地址。"""
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("192.168.1.1", 80))
                return s.getsockname()[0]
        except Exception:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.connect(("8.8.8.8", 80))
                    return s.getsockname()[0]
            except Exception:
                return "127.0.0.1"

    def resolve_audio_target(self, audio_path: str) -> str:
        """按配置返回出站音频目标引用（HTTP URL 或本地路径）。"""
        if getattr(self._config, "enable_http_server", False):
            return self.get_public_url(audio_path)
        return audio_path

    async def save_temp_file(self, audio_data: bytes, ctx: "PluginContext") -> Optional[str]:
        """把音频写入临时文件并安排延迟清理，返回路径供出站消息引用。"""

        audio_path = await self._save_file(audio_data, ctx)
        if audio_path is None:
            return None
        self._pending_paths.add(audio_path)
        task = asyncio.create_task(self._cleanup_async(audio_path))
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)
        return audio_path

    async def send(
        self,
        ctx: "PluginContext",
        audio_data: bytes,
        style_name: str,
        stream_id: str,
        log_prefix: str,
    ) -> Tuple[bool, str]:
        """发送语音，返回 ``(success, message)``。"""

        if self._config.use_base64_audio:
            # 显式开启了 base64 模式。该模式会把整段音频编码进消息体，
            # 随后作为文本进入 MaiBot 上下文并按 token 计费，代价极高，
            # 因此这里额外打一条警告，便于用户对照费用排查。
            logger.warning(
                "%s 使用 base64 模式发送语音，音频会进入上下文并按 token 计费，"
                "建议改用文件模式（use_base64_audio = false）",
                log_prefix,
            )
            audio_b64 = base64.b64encode(audio_data).decode("utf-8")
            await ctx.send.custom("voice", audio_b64, stream_id)
            logger.info(
                "%s 语音已发送（base64，%s 字节，风格=%s）",
                log_prefix, len(audio_data), style_name,
            )
            return True, f"已发送语音（风格: {style_name}）"

        # 文件模式统一走 save_temp_file，保证待清理路径被登记，
        # 卸载或热重载时才能正确收尾。
        audio_path = await self.save_temp_file(audio_data, ctx)
        if audio_path is None:
            return False, "保存音频文件失败"

        audio_target = self.resolve_audio_target(audio_path)
        await ctx.send.custom("voiceurl", audio_target, stream_id)
        mode_desc = "HTTP" if getattr(self._config, "enable_http_server", False) else "文件"
        logger.info("%s 语音已发送（%s，%s，风格=%s）", log_prefix, mode_desc, audio_target, style_name)
        return True, f"已发送语音（风格: {style_name}）"

    async def sweep_orphans(self, ctx: "PluginContext", *, max_age_seconds: float = 900.0) -> int:
        """清理上次运行遗留的临时音频，返回删除数量。

        为什么需要这个：延迟清理任务挂在 ``self._cleanup_tasks`` 上，
        插件热重载时旧实例被整体丢弃，未到期的任务连同 ``_pending_paths``
        一起消失，音频文件就永久留在目录里。实测重载几次就会攒下几十个。

        只删本插件命名的文件（``tts_gpt_sovits_*.wav``），且只删超过
        ``max_age_seconds`` 的，避免误删正在发送的音频。
        """

        loop = asyncio.get_running_loop()
        output_dir = self._resolve_output_dir(ctx)
        return await loop.run_in_executor(
            None, self._sweep_sync, output_dir, max_age_seconds
        )

    @staticmethod
    def _sweep_sync(output_dir: str, max_age_seconds: float) -> int:
        """同步版本（在线程池执行）。"""

        import time

        if not output_dir or not os.path.isdir(output_dir):
            return 0

        now = time.time()
        removed = 0
        try:
            entries = os.listdir(output_dir)
        except OSError as exc:
            logger.debug("扫描临时音频目录失败 %s: %s", output_dir, exc)
            return 0

        for name in entries:
            if not (name.startswith("tts_gpt_sovits_") and name.endswith(".wav")):
                continue
            path = os.path.join(output_dir, name)
            try:
                if now - os.path.getmtime(path) < max_age_seconds:
                    continue
                os.remove(path)
                removed += 1
            except OSError as exc:
                logger.debug("清理遗留音频失败 %s: %s", path, exc)

        if removed:
            logger.info("已清理 %d 个上次运行遗留的临时音频（%s）", removed, output_dir)
        return removed

    def _resolve_output_dir(self, ctx: "PluginContext") -> str:
        """确定音频落盘目录。

        优先级：
        1. 配置里的 ``audio_output_dir``（支持绝对路径），未配置时留空
        2. Runner 分配的 ``ctx.paths.runtime_dir``——临时音频放这里最稳，
           不用猜主程序装在哪，也不会污染插件仓库
        3. 退化为项目根目录
        """

        configured = self._config.audio_output_dir.strip()
        if configured:
            return (
                configured
                if os.path.isabs(configured)
                else os.path.join(self._project_root, configured)
            )

        # ctx.paths 由 Runner 按插件 ID 分配，比手搓路径可靠
        paths = getattr(ctx, "paths", None)
        runtime_dir = getattr(paths, "runtime_dir", None)
        if runtime_dir:
            return str(runtime_dir)

        return self._project_root

    async def _save_file(self, audio_data: bytes, ctx: "PluginContext") -> Optional[str]:
        """在后台线程写入音频文件，返回文件路径。"""

        loop = asyncio.get_running_loop()
        output_dir = self._resolve_output_dir(ctx)
        return await loop.run_in_executor(None, self._write_sync, audio_data, output_dir)

    def _write_sync(self, audio_data: bytes, output_dir: str) -> Optional[str]:
        """同步写入音频文件（内部方法，在后台线程执行）。"""

        if output_dir:
            try:
                os.makedirs(output_dir, exist_ok=True)
            except OSError as exc:
                logger.error("创建输出目录失败 %s: %s", output_dir, exc)
                return None
        else:
            output_dir = self._project_root

        filename = f"tts_gpt_sovits_{uuid.uuid4().hex[:12]}.wav"
        path = os.path.join(output_dir, filename)
        try:
            with open(path, "wb") as f:
                f.write(audio_data)
            return path
        except OSError as exc:
            logger.error("写入音频文件失败 %s: %s", path, exc)
            return None

    async def _cleanup_async(self, path: str) -> None:
        """延迟删除临时音频文件。"""

        try:
            await asyncio.sleep(CLEANUP_DELAY)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._cleanup_sync, path)
        finally:
            self._pending_paths.discard(path)

    async def close(self) -> None:
        """取消待执行的清理任务并立即删除已落盘的临时文件。

        插件卸载或进程退出时，延迟清理任务会被取消，音频文件就会残留。
        这里主动收尾，避免临时目录持续堆积。

        删文件改为一次性提交线程池并发执行：官方要求 ``on_unload`` 里
        不要做耗时操作，逐个 ``await`` 在文件多时会把卸载拖成几百毫秒。
        """

        await self.stop_http_server()

        tasks = list(self._cleanup_tasks)
        self._cleanup_tasks.clear()
        for task in tasks:
            task.cancel()

        pending_paths = list(self._pending_paths)
        self._pending_paths.clear()
        if not pending_paths:
            return

        loop = asyncio.get_running_loop()
        await asyncio.gather(
            *(
                loop.run_in_executor(None, self._cleanup_sync, path)
                for path in pending_paths
            ),
            return_exceptions=True,
        )

    @staticmethod
    def _cleanup_sync(path: str) -> None:
        """同步删除文件（内部方法，在后台线程执行）。"""

        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError as exc:
            logger.debug("清理临时音频失败 %s: %s", path, exc)
