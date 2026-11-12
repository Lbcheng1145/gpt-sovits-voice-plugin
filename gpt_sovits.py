"""GPT-SoVITS 客户端：可用性探测、模型权重切换与语音合成。"""

import asyncio
import logging
import os
import time
import urllib.parse
from typing import Dict, Optional, Tuple

import aiohttp

from .config import GPTSovitsConfig, GPTSovitsStyle
from .utils import (
    detect_language,
    is_language_supported,
    is_text_readable_as,
    is_valid_audio,
    resolve_version,
    supported_languages,
    trim_leading_silence,
)

logger = logging.getLogger("plugin.gpt_sovits_voice.gpt_sovits")

# 音频数据最小有效大小（字节），小于该值视为合成失败
MIN_AUDIO_SIZE = 100
# 音频数据上限（字节）。正常合成的 wav 远小于该值；设置上限是为了防止
# 服务端异常返回超大响应时把内存撑爆（read() 默认会一次性读入全部内容）。
MAX_AUDIO_SIZE = 32 * 1024 * 1024


class GPTSovitsClient:
    """封装 GPT-SoVITS HTTP 接口：按风格切换模型并合成音频。

    每个风格拥有独立的参考音频、提示文本与模型权重；
    风格不存在或未指定时由 :meth:`resolve_style` 回退到兜底风格。
    """

    def __init__(self, session: aiohttp.ClientSession, config: GPTSovitsConfig) -> None:
        self._session = session
        self._config = config
        # 实例级状态：避免多实例/重载后共享过期状态
        self._current_gpt_weights: Optional[str] = None
        self._current_sovits_weights: Optional[str] = None
        # 保护「切换权重 + 合成」这一整段，防止并发请求串模型
        self._model_lock = asyncio.Lock()
        # 「服务可用」探测成功的时间戳（monotonic）；失败不缓存
        self._probe_ok_at: Optional[float] = None

    async def ensure_available(self, log_prefix: str) -> bool:
        """短超时探测 GPT-SoVITS 服务是否可达，失败返回 False。

        GSV 常态可能没开（手动拉起 / 远程部署）：与其让请求挂满整个 timeout，
        不如先用 probe_timeout 秒发一个探测请求 —— 只要拿到任何 HTTP 响应
        （含 4xx 参数错误）就说明服务活着；连不上/超时/5xx 判为不可用。
        只有「可用」才缓存（probe_cache_seconds 秒），「不可用」不缓存，
        服务一开就立即恢复。
        """

        now = time.monotonic()
        cache_seconds = max(getattr(self._config, "probe_cache_seconds", 0.0), 0.0)
        if self._probe_ok_at is not None and (now - self._probe_ok_at) < cache_seconds:
            return True

        probe_url = f"{self._config.server.rstrip('/')}/tts"
        probe_timeout = max(getattr(self._config, "probe_timeout", 2.0), 0.5)
        try:
            async with self._session.post(
                probe_url,
                json={"text": "", "text_lang": "zh"},
                timeout=aiohttp.ClientTimeout(total=probe_timeout),
            ) as response:
                if response.status >= 500:
                    logger.warning(
                        "%s 语音服务异常[%s]，视为不可用", log_prefix, response.status,
                    )
                    return False
                await response.read()
                self._probe_ok_at = time.monotonic()
                return True
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            logger.warning(
                "%s 语音服务不可达 %s（%s）",
                log_prefix, self._config.server, type(exc).__name__,
            )
            return False

    def styles(self) -> Dict[str, GPTSovitsStyle]:
        """将配置中的风格列表转换为 ``{name: style}`` 映射。"""

        return {style.name: style for style in self._config.styles if style.name}

    def default_style_name(self) -> str:
        """返回兜底风格名：优先名为 default/默认 的风格，否则取第一个。"""

        styles = self.styles()
        for name in ("default", "默认"):
            if name in styles:
                return name
        if styles:
            return next(iter(styles))
        return ""

    def resolve_style(self, style: str, log_prefix: str) -> str:
        """解析最终风格：精确命中配置则使用，否则回退到兜底风格。"""

        styles = self.styles()
        if style and style in styles:
            return style
        fallback = self.default_style_name()
        if style:
            logger.warning("%s 风格 '%s' 不存在，回退 %s", log_prefix, style, fallback)
        return fallback

    def _is_local_server(self) -> bool:
        """GPT-SoVITS 服务是否跑在本机。

        远程部署时 ``refer_wav`` 是**服务器上**的路径，本地当然没有这个文件，
        拿本地文件系统去校验只会把远程用户全挡在门外。
        """

        server = str(getattr(self._config, "server", "") or "").strip()
        try:
            host = (urllib.parse.urlparse(server).hostname or "").strip().lower()
        except Exception:
            # 解析不出来就按本机处理，保持原来那套严格校验
            return True
        if not host:
            return True
        return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0", "[::1]"}

    def validate_style(self, style_name: str) -> Optional[str]:
        """检查风格配置是否可用，返回错误说明（可用时返回 None）。

        首次安装时 SDK 会自动生成一份占位配置（``refer_wav`` 为空），
        直接调用合成只会从服务端拿到 400，用户无从得知要补什么。
        这里提前拦下并给出明确指引。
        """

        style = self.styles().get(style_name)
        if style is None:
            return (
                f"找不到风格 '{style_name}'，请检查 config.toml 的 "
                f"[[gpt_sovits.styles]] 配置"
            )

        missing = []
        if not (style.refer_wav or "").strip():
            missing.append("refer_wav（参考音频路径）")
        if not (style.prompt_text or "").strip():
            missing.append("prompt_text（参考音频对应的文本）")

        if missing:
            return (
                f"风格 '{style_name}' 尚未配置 {' 和 '.join(missing)}。"
                f"请在 config.toml 的 [[gpt_sovits.styles]] 中补齐后重试；"
                f"可参考同目录下的 config.example.toml"
            )

        # 只有服务跑在本机时，本地文件存在性校验才有意义。
        # 远程服务下 refer_wav 指向的是服务器上的路径，交给服务端自己判断；
        # 这里再拦就是「本地没有这个文件所以不让连远程」，正是用户反馈的问题。
        if self._is_local_server() and not os.path.exists(style.refer_wav):
            return (
                f"风格 '{style_name}' 的参考音频不存在：{style.refer_wav}，"
                f"请检查路径是否正确"
            )

        return None

    def resolve_by_language(
        self,
        lang: str,
        style_name: str,
        log_prefix: str,
    ) -> str:
        """按语言挑选风格。

        优先级：
        1. 当前风格本身声明支持该语言
        2. 同名语言变体 ``<风格>_<语言>`` / ``<风格>-<语言>``
        3. 该语言对应的默认风格变体
        4. 保持原风格（该语言无专用模型时用通用模型合成）
        """

        styles = self.styles()
        if not lang:
            return style_name

        # 1) 当前风格本身支持该语言
        cur = styles.get(style_name)
        if cur is not None and lang in (cur.languages or []):
            return style_name

        # 2) 同名语言变体
        for suffix in (f"_{lang}", f"-{lang}"):
            candidate = f"{style_name}{suffix}"
            if candidate in styles:
                return candidate

        # 3) 该语言的默认风格变体
        default_name = self.default_style_name()
        for suffix in (f"_{lang}", f"-{lang}"):
            candidate = f"{default_name}{suffix}"
            if candidate in styles:
                return candidate

        # 4) 保持原风格
        return style_name

    def effective_language(self, style_name: str, text: str) -> str:
        """返回该风格下实际使用的语言。

        风格显式配置 ``language`` 时直接返回，否则自动检测。
        语言路由与合成请求应共用此结果，避免两处判定不一致。
        """

        style = self.styles().get(style_name)
        if style is not None:
            forced = (getattr(style, "language", "") or "").strip()
            if forced:
                return forced
        return detect_language(text)

    def effective_version(self, style: GPTSovitsStyle) -> str:
        """返回该风格实际使用的底模版本。

        全局填了具体版本就用它；填 ``auto`` 时按该风格自己的权重路径推断。
        每个风格权重路径可能不同，所以推断要按风格逐个进行。
        """

        return resolve_version(
            getattr(self._config, "model_version", ""),
            sovits_weights=getattr(style, "sovits_weights", "") or "",
            gpt_weights=getattr(style, "gpt_weights", "") or "",
        )

    async def synthesize(self, text: str, style_name: str, log_prefix: str) -> Optional[bytes]:
        """调用 GPT-SoVITS ``/tts`` 接口合成音频，失败返回 ``None``。

        切换权重与发起合成在同一把锁内完成，避免并发请求互相覆盖模型。
        """

        styles = self.styles()
        style = styles.get(style_name) or styles.get(self.default_style_name())
        if style is None:
            logger.error("%s 找不到可用风格配置", log_prefix)
            return None

        # 风格显式指定语言时优先使用，否则自动检测
        text_lang = self.effective_language(style_name, text)
        forced = (getattr(style, "language", "") or "").strip()

        # 底模不支持该语言时直接拒绝，避免 API 返回 400 或产出空白音频
        version = self.effective_version(style)
        if not is_language_supported(text_lang, version):
            supported = sorted(supported_languages(version))
            # 拉丁字母等可用中文模型勉强读出，回退中文
            if is_text_readable_as(text, "zh"):
                logger.warning(
                    "%s 语言 %s 不被底模 %s 支持（支持: %s），改用 zh",
                    log_prefix, text_lang, version or "默认", supported,
                )
                text_lang = "zh"
            else:
                logger.warning(
                    "%s 语言 %s 不被底模 %s 支持（支持: %s），且文本用中文模型无法读出，跳过合成",
                    log_prefix, text_lang, version or "默认", supported,
                )
                return None

        logger.info(
            "%s 合成语言: %s（%s）",
            log_prefix, text_lang, "风格指定" if forced else "自动检测",
        )

        request_data = {
            "text": text,
            "text_lang": text_lang,
            "ref_audio_path": style.refer_wav,
            "prompt_text": style.prompt_text,
            "prompt_lang": style.prompt_language,
            "speed_factor": getattr(self._config, "speed_factor", 1.0),
            # 分段间隔：API 默认 0.3 秒会在分段之间插入纯静音，
            # 静音前后伴有衰减尾音和硬起音，听感像颤音，置 0 消除
            "fragment_interval": getattr(self._config, "fragment_interval", 0.0),
            # 切分参数：切块过碎会让块首起音来不及建立，表现为开头吞字。
            # 具体的默认值与说明见 config.GPTSovitsConfig。
            "text_split_method": getattr(self._config, "text_split_method", "cut1"),
            "min_chunk_length": getattr(self._config, "min_chunk_length", 16),
            "overlap_length": getattr(self._config, "overlap_length", 0),
            "top_k": getattr(self._config, "top_k", 15),
            "top_p": getattr(self._config, "top_p", 1.0),
            "temperature": getattr(self._config, "temperature", 1.0),
        }
        tts_url = f"{self._config.server.rstrip('/')}/tts"

        async with self._model_lock:
            # 风格配置了模型权重时，先切换模型（锁内，避免并发串模型）
            if style.gpt_weights or style.sovits_weights:
                switch_ok, switch_error = await self._switch_model(style)
                if not switch_ok:
                    logger.error("%s %s", log_prefix, switch_error)
                    return None

            logger.info(
                "%s GPT-SoVITS 请求: text='%s...', style=%s",
                log_prefix, text[:30], style_name,
            )

            async with self._session.post(tts_url, json=request_data) as response:
                if response.status != 200:
                    error_text = (await response.text())[:200]
                    logger.error(
                        "%s GPT-SoVITS API 失败[%s]: %s",
                        log_prefix, response.status, error_text,
                    )
                    return None
                # 流式读取并限制总量，避免异常响应把内存撑爆
                chunks: list[bytes] = []
                received = 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    received += len(chunk)
                    if received > MAX_AUDIO_SIZE:
                        logger.error(
                            "%s 音频响应超过上限（>%d 字节），已中断读取",
                            log_prefix, MAX_AUDIO_SIZE,
                        )
                        return None
                    chunks.append(chunk)
                audio_data = b"".join(chunks)

        # 验证音频数据：既查大小也查是否为静音/无效 wav
        if not audio_data or len(audio_data) < MIN_AUDIO_SIZE:
            logger.error(
                "%s 音频数据无效（%s 字节）", log_prefix, len(audio_data or b""),
            )
            return None

        # 音频校验含 wav 解析与 RMS 计算，放到线程池执行，
        # 避免在没有 audioop 的环境里阻塞事件循环。
        loop = asyncio.get_running_loop()
        valid, detail = await loop.run_in_executor(None, is_valid_audio, audio_data)
        if not valid:
            logger.error("%s 音频内容异常：%s", log_prefix, detail)
            return None

        # 重整开头：裁净弱起音后补一段固定静音，播放链路才不会把第一个字
        # 的声母一起截掉（听感上就是开头吞字）。
        #
        # 判据只能用「是不是同一个对象」，不能比较长度：该函数会补 500ms
        # 静音，输出必然**比输入长**，用 `len(trimmed) < len(audio_data)`
        # 判断的话永远不会成立，裁剪就白做了。
        trimmed = await loop.run_in_executor(None, trim_leading_silence, audio_data)
        if trimmed is not audio_data:
            logger.debug(
                "%s 已重整开头：%d -> %d 字节",
                log_prefix, len(audio_data), len(trimmed),
            )
            audio_data = trimmed

        return audio_data

    async def _switch_model(self, style: GPTSovitsStyle) -> Tuple[bool, str]:
        """按风格切换 GPT-SoVITS 模型权重（已加载的权重不重复切换）。"""

        server = self._config.server.rstrip("/")

        # 切换 GPT 权重
        if style.gpt_weights and style.gpt_weights != self._current_gpt_weights:
            gpt_url = (
                f"{server}/set_gpt_weights?"
                + urllib.parse.urlencode({"weights_path": style.gpt_weights})
            )
            async with self._session.get(gpt_url) as response:
                if response.status != 200:
                    return False, f"GPT模型切换失败: {await response.text()}"
            self._current_gpt_weights = style.gpt_weights
            logger.info("已切换 GPT 模型: %s", style.gpt_weights)

        # 切换 SoVITS 权重
        if style.sovits_weights and style.sovits_weights != self._current_sovits_weights:
            sovits_url = (
                f"{server}/set_sovits_weights?"
                + urllib.parse.urlencode({"weights_path": style.sovits_weights})
            )
            async with self._session.get(sovits_url) as response:
                if response.status != 200:
                    return False, f"SoVITS模型切换失败: {await response.text()}"
            self._current_sovits_weights = style.sovits_weights
            logger.info("已切换 SoVITS 模型: %s", style.sovits_weights)

        return True, ""

    def reset_model_state(self) -> None:
        """清空已加载模型记录，下次合成时强制重新切换。

        服务端重启或手动切换权重后调用，避免客户端状态与实际不一致。
        """

        self._current_gpt_weights = None
        self._current_sovits_weights = None
        logger.info("已重置模型状态缓存")
