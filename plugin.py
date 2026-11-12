"""GPT-SoVITS 语音合成插件（MaiBot SDK 2.x）

基于本地 GPT-SoVITS 服务合成语音，支持多种语音风格：
- LLM 通过 ``tts_voice`` 工具自主调用；
- 用户通过 ``/tts`` 命令手动触发；
- 风格在 ``config.toml`` 的 ``[[gpt_sovits.styles]]`` 中配置，每个风格独立
  拥有参考音频、提示文本、模型权重等全部参数；
- 未指定风格时按文本内容自动选择语气，并按检测到的语言切换对应模型；
- 可配置概率语音，按比例把普通文字回复自动转成语音。

版本号以 ``_manifest.json`` 为准。
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import asyncio
import logging
import os
import time

import aiohttp

from maibot_sdk import API, Command, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.components import ErrorPolicy, HookMode, HookOrder
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from .audio import AudioSender
from .config import PluginConfig
from .gpt_sovits import GPTSovitsClient
from .intercom import IntercomWindowManager
from .random_voice import RandomVoiceDecider
from .style_router import DEFAULT_RULES, route_style
from .utils import clean_text, ensure_sentence_end, is_number_only

logger = logging.getLogger("plugin.gpt_sovits_voice")


def _strip_voice_wrapper(content: str) -> str:
    """去掉宿主加的 ``[语音: xxx]`` 包装，只留正文。"""

    text = str(content or "").strip()
    if text.startswith("[语音"):
        if ":" in text:
            text = text.split(":", 1)[1]
        else:
            return ""
    if text.endswith("]"):
        text = text[:-1]
    return text.strip()


class GPTSovitsVoicePlugin(MaiBotPlugin):
    """GPT-SoVITS 语音合成插件。

    ``tts_voice`` 工具由 LLM 自主决定是否调用；
    ``/tts`` 命令由用户手动触发，均支持 ``-v <风格>`` 指定语音风格。
    """

    config_model = PluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._session: Optional[aiohttp.ClientSession] = None
        self._client: Optional[GPTSovitsClient] = None
        self._audio: Optional[AudioSender] = None
        self._decider = RandomVoiceDecider()
        self._missing_styles_warned = False
        # 语音对讲的会话窗口（v1.6.0）
        self._intercom = IntercomWindowManager()
        # stream_id → (文本, 音频路径, 时刻)，用于短时间内复用同一段合成结果
        self._intercom_audio: Dict[str, Tuple[str, str, float]] = {}

    # ─── 生命周期 ────────────────────────────────────────────────────────

    def _enabled(self) -> bool:
        """插件总开关。

        ``config.plugin.enabled`` 是插件自己的字段，宿主不会读它——
        宿主那层的 enabled 属于组件注册状态，是另一回事。所以必须在这里
        判断，否则 WebUI 里关掉「启用插件」后插件照常工作，开关形同虚设。
        """

        section = getattr(self.config, "plugin", None)
        if section is None:
            return True
        return bool(getattr(section, "enabled", True))

    async def on_load(self) -> None:
        """插件加载：创建会话与客户端，并打印风格概况。"""

        await self._build_runtime()
        self.ctx.logger.info(
            "GPT-SoVITS 语音插件已加载，服务=%s，风格=%s",
            self.config.gpt_sovits.server,
            list(self._client.styles().keys()) if self._client else [],
        )
        self._check_config_ready()

        # 清理上次运行遗留的临时音频。延迟清理任务挂在旧实例上，
        # 热重载会连同 `_pending_paths` 一起丢弃，文件就永久留下了。
        if self._audio is not None:
            try:
                await self._audio.sweep_orphans(self.ctx)
            except Exception as exc:  # noqa: BLE001 —— 清理失败不影响加载
                self.ctx.logger.debug("清理遗留音频失败: %s", exc)

    def _check_config_ready(self) -> None:
        """首次加载时检查配置是否可用，配置缺失时给出明确引导。"""

        if self._client is None:
            return

        broken = []
        for name in self._client.styles():
            err = self._client.validate_style(name)
            if err:
                broken.append((name, err))

        if not broken:
            return

        self.ctx.logger.warning(
            "配置尚未完成：%d/%d 个风格缺少必要参数，合成会失败",
            len(broken), len(self._client.styles()),
        )
        for name, err in broken:
            self.ctx.logger.warning("  风格 %s：%s", name, err)
        self.ctx.logger.warning(
            "请编辑插件目录下的 config.toml 补齐 refer_wav 与 prompt_text；"
            "可参考同目录的 config.example.toml。"
            "另外请确认识别 GPT-SoVITS 服务已在 %s 启动",
            self.config.gpt_sovits.server,
        )

    async def on_unload(self) -> None:
        """插件卸载：关闭 HTTP 会话并清理未到期的临时音频。"""

        if self._audio is not None:
            try:
                await self._audio.close()
            except Exception as exc:
                self.ctx.logger.warning("清理临时音频失败: %s", exc)
        await self._close_session()

        # 语音窗口只存在内存里，重载后不会恢复。这里显式清空并记一笔，
        # 避免用户看到「刚才还在语音对话，重载后突然变文字」而困惑。
        active_ids = self._intercom.active_streams()
        if active_ids:
            self.ctx.logger.info(
                "卸载时清空 %d 个语音对讲窗口（该状态不持久化，重载后需重新发语音进入）",
                len(active_ids),
            )
            for sid in active_ids:
                self._intercom.close(sid)

        self.ctx.logger.info("GPT-SoVITS 语音插件已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """配置热更新回调。

        重建客户端与会话，否则旧的超时、服务地址、风格列表不会生效。
        """

        del config_data
        self.ctx.logger.info(
            "GPT-SoVITS 语音插件配置已更新: scope=%s version=%s", scope, version,
        )
        await self._build_runtime()
        self.ctx.logger.info(
            "配置已重新加载：服务=%s，风格=%s",
            self.config.gpt_sovits.server,
            list(self._client.styles().keys()) if self._client else [],
        )

    async def _close_session(self) -> None:
        """关闭当前 HTTP 会话（若存在）。"""

        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _build_runtime(self) -> None:
        """按当前配置重建会话、客户端与发送器。"""

        await self._close_session()

        # 热重载会替换 AudioSender，旧实例的待清理文件必须在这里收尾，
        # 否则延迟删除任务随对象一起被丢弃，音频文件就永远留在磁盘上。
        if self._audio is not None:
            try:
                await self._audio.close()
            except Exception as exc:
                self.ctx.logger.warning("重建运行时清理临时音频失败: %s", exc)

        timeout = max(self.config.gpt_sovits.timeout, 10)
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
        )
        self._client = GPTSovitsClient(self._session, self.config.gpt_sovits)
        self._audio = AudioSender(self.config.general, self._project_root())
        # 配置可能改了权重路径，清空缓存以强制重新切换
        self._client.reset_model_state()
        # 概率语音的随机结果也要清空，否则新旧概率会混在一起
        self._decider.reset()
        self._missing_styles_warned = False

    # ─── 组件注册：按配置把真实风格名注入 style 参数的枚举 ───────────────

    def _available_style_names(self) -> List[str]:
        """读当前配置里的风格名（default 排最前）。

        组件注册发生在配置注入之后，正常情况下这里能读到真实风格；
        若配置尚未注入，返回空列表，让上层跳过注入。
        """

        try:
            configured = list(self.config.gpt_sovits.styles or [])
        except Exception:  # 配置未注入 / 字段缺失时不能让注册整体失败
            return []

        names: List[str] = []
        for style in configured:
            name = str(getattr(style, "name", "") or "").strip()
            if name and name not in names:
                names.append(name)

        if "default" in names:
            names.remove("default")
            names.insert(0, "default")
        return names

    def get_components(self) -> List[Dict[str, Any]]:
        """在 SDK 产出的组件声明上补齐「真实风格名」（expose_style_enum 开启时）。

        宿主会丢弃 @Tool 的 detailed_description，工具默认 deferred，
        Planner 只看得到一句很短的 description —— style 参数里如果不带
        真实风格名，模型只能按 description 里的示例猜，猜不中会被
        resolve_style 回退 default，部署方配置的风格等于没生效。
        """

        components = super().get_components()
        if not getattr(self.config.gpt_sovits, "expose_style_enum", True):
            return components

        style_names = self._available_style_names()
        if not style_names:
            logger.info("GPT-SoVITS 风格列表暂不可读，跳过 style enum 注入")
            return components

        enum_hint = "、".join(style_names)
        for component in components:
            if str(component.get("name", "")) != "tts_voice":
                continue
            metadata = component.get("metadata")
            if not isinstance(metadata, dict):
                continue

            parameters = metadata.get("parameters")
            if isinstance(parameters, list):
                for parameter in parameters:
                    if not isinstance(parameter, dict):
                        continue
                    if str(parameter.get("name", "")) != "style":
                        continue
                    parameter["enum_values"] = list(style_names)
                    parameter["description"] = (
                        "语音风格，可选。请从 enum 里挑一个最贴合当前情绪的；"
                        "不传则按文本内容自动选语气"
                    )

            base_desc = str(
                metadata.get("description") or metadata.get("brief_description") or ""
            ).strip()
            metadata["description"] = (
                f"{base_desc}（可选语音风格：{enum_hint}；不传用 default）"
            ).strip()

        logger.info("GPT-SoVITS 已把 %d 个风格名注入 tts_voice.style enum", len(style_names))
        return components

    # ─── 工具：LLM 自主调用 ──────────────────────────────────────────────

    @Tool(
        "tts_voice",
        description="用语音回复，适合简短问候与活泼场景；用户明确要求语音时必须使用。",
        parameters=[
            ToolParameterInfo(
                name="text",
                param_type=ToolParamType.STRING,
                description="要转换为语音的文本（2~100字）",
                required=True,
            ),
            ToolParameterInfo(
                name="style",
                param_type=ToolParamType.STRING,
                description=(
                    "语音风格，可选。不传则按文本内容自动选语气，"
                    "常用：兴奋、吐槽、默认"
                ),
                required=False,
            ),
        ],
    )
    async def handle_tts(
        self,
        stream_id: str = "",
        text: str = "",
        style: str = "",
        **kwargs: Any,
    ) -> Tuple[bool, str]:
        """处理 LLM 调用的 ``tts_voice`` 工具。"""

        del kwargs
        if not self._enabled():
            return False, "插件已禁用"
        if not stream_id:
            return False, "缺少 stream_id，无法发送"
        return await self._handle(stream_id, text, style, "tool")

    # ─── 命令：用户手动触发 ─────────────────────────────────────────────

    @Command(
        "tts",
        description="将文本转换为语音（GPT-SoVITS）",
        pattern=r"^/(?:tts|voice|gptsovits)\s+(?P<text>.+?)(?:\s+-v\s+(?P<style>\S+))?$",
    )
    async def handle_tts_command(
        self,
        stream_id: str = "",
        text: str = "",
        matched_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Tuple[bool, str, bool]:
        """处理 ``/tts`` 系列命令。

        Returns:
            ``(success, message, intercept)`` 三元组。

            第三个值在官方文档里写作 weight（权重），但宿主实际实现
            （``component_query._build_command_executor``）是把它当
            ``intercept`` 用的：``intercept = bool(result[2])``。
            这里按宿主实行为准——命令由插件独占处理，一律返回 True。
        """

        del kwargs
        if not self._enabled():
            return False, "插件已禁用", True
        groups = matched_groups or {}
        user_text = (groups.get("text") or "").strip()
        user_style = (groups.get("style") or "").strip()

        if not stream_id:
            return False, "缺少 stream_id", True

        if user_text.lower() == "help":
            await self._send_help(stream_id)
            return True, "显示帮助信息", True

        if not user_text:
            await self._send_error(stream_id, "请输入要转换为语音的文本内容")
            return False, "缺少文本内容", True

        success, message = await self._handle(stream_id, user_text, user_style, "command")
        return success, message, True

    # ─── 公共处理链路 ────────────────────────────────────────────────────

    async def _handle(
        self,
        stream_id: str,
        text: str,
        style: str,
        source_desc: str,
    ) -> Tuple[bool, str]:
        """公共合成发送链路：Tool 与 Command 共用。"""

        log_prefix = f"[gpt_sovits_voice][{source_desc}][{stream_id}]"
        try:
            if self._client is None or self._audio is None:
                return False, "插件尚未完成加载"

            # 语音服务可能没开（GSV 由用户手动拉起 / 远程部署常态不在线）：
            # 先短超时探测，失败直接降级，别让请求挂满整个 timeout 再把
            # 技术错误抛给模型或用户。
            if getattr(self.config.gpt_sovits, "availability_probe", True):
                try:
                    available = await self._client.ensure_available(log_prefix)
                except Exception:  # 探测自身出错不拦合成，交给真实请求判定
                    available = True
                if not available:
                    return await self._degrade(stream_id, source_desc, log_prefix)

            # 先清洗不可见字符（零宽符等会导致 GPT-SoVITS 侧 GBK 编码失败）
            original = (text or "").strip()
            raw_text = clean_text(original)
            if not raw_text:
                if original:
                    self.ctx.logger.warning(
                        "%s 文本仅含不可见字符，拒绝合成", log_prefix,
                    )
                return False, "文本为空"
            if len(original) != len(raw_text):
                self.ctx.logger.info(
                    "%s 已清洗 %d 个不可见字符",
                    log_prefix, len(original) - len(raw_text),
                )

            # 补齐句末标点：没有标点时最后一段缺少终止信号，
            # 合成出来的最后一个字会被压缩或吞掉。
            # 保留补齐前的文本用于长度校验与错误提示。
            cleaned_text = raw_text
            raw_text = ensure_sentence_end(raw_text)

            min_length = self.config.general.min_text_length
            max_length = self.config.general.max_text_length
            # 用补齐标点前的长度校验：补上的句号是插件自己加的，
            # 不该让刚好等于上限的文本被判为过长，报错字数也更贴近用户输入。
            text_length = len(cleaned_text)

            if not min_length <= text_length <= max_length:
                if text_length < min_length:
                    message = f"文本过短（{text_length}字），无法合成语音"
                    result = "文本过短"
                else:
                    message = f"文本过长（{text_length}字），超过语音长度限制（{max_length}）"
                    result = "文本过长"
                self.ctx.logger.warning("%s %s", log_prefix, message)
                await self._send_error(stream_id, message)
                return False, result

            style_name = self._resolve_style_chain(raw_text, style, log_prefix)
            if style_name is None:
                await self._send_error(stream_id, "语音合成失败：配置不完整")
                return False, "配置不完整"

            # 不分句，整段直接合成（分句交给合成服务智能处理）
            audio_data = await self._client.synthesize(raw_text, style_name, log_prefix)
            if audio_data is None:
                await self._send_error(stream_id, "语音合成失败")
                return False, "合成失败"

            success, message = await self._audio.send(
                self.ctx, audio_data, style_name, stream_id, log_prefix,
            )
            if not success:
                await self._send_error(stream_id, f"语音合成失败: {message}")
            return success, message
        except asyncio.TimeoutError:
            timeout_sec = self.config.gpt_sovits.timeout
            self.ctx.logger.error("%s TTS 超时（%ss）", log_prefix, timeout_sec)
            await self._send_error(stream_id, f"语音合成超时（{timeout_sec}s）")
            return False, "timeout"
        except aiohttp.ClientConnectionError as exc:
            # 连接类失败 = 服务没开/网络不通，走降级而不是把原始异常抛出去
            self.ctx.logger.error("%s 语音服务连接失败: %s", log_prefix, exc)
            return await self._degrade(stream_id, source_desc, log_prefix)
        except aiohttp.ClientError as exc:
            self.ctx.logger.error("%s TTS 网络错误: %s", log_prefix, exc)
            await self._send_error(stream_id, f"语音合成网络错误: {type(exc).__name__}")
            return False, str(exc)
        except (KeyError, AttributeError, ValueError) as exc:
            self.ctx.logger.error(
                "%s TTS 配置或参数错误: %s", log_prefix, exc, exc_info=True,
            )
            await self._send_error(stream_id, f"语音合成参数错误: {exc}")
            return False, str(exc)
        except OSError as exc:
            self.ctx.logger.error("%s TTS 文件系统错误: %s", log_prefix, exc)
            await self._send_error(stream_id, f"语音合成文件错误: {exc}")
            return False, str(exc)
        except Exception as exc:  # 兜底，避免异常冒泡中断会话
            self.ctx.logger.error(
                "%s TTS 未预期错误: %s", log_prefix, exc, exc_info=True,
            )
            await self._send_error(stream_id, "语音合成发生内部错误")
            return False, str(exc)

    async def _degrade(
        self,
        stream_id: str,
        source_desc: str,
        log_prefix: str,
    ) -> Tuple[bool, str]:
        """语音服务不可用时的统一降级出口。

        工具路径把 ``unavailable_hint``（给 LLM 的指令）作为失败消息返回，
        Planner 看得到并能改用文字回复；命令路径按 ``silent_on_unavailable``
        决定要不要给用户发一句 ``unavailable_notice``。
        """

        del log_prefix
        general = self.config.general
        if source_desc == "tool":
            return False, general.unavailable_hint
        if not general.silent_on_unavailable and (general.unavailable_notice or "").strip():
            await self.ctx.send.text(general.unavailable_notice, stream_id)
        return False, "语音服务不可用"

    # ─── 概率语音：拦截出站文字并转成语音 ──────────────────────────────

    #: 出站消息里属于「元数据」而非正文的消息段类型。
    #:
    #: ``reply`` 是引用信息，``at`` 是 @ 的对象——QQ 里 @ 的是谁并不属于
    #: 朗读内容，两者都不影响正文。带图片、语音、文件等真实媒体段时
    #: 仍然整体放弃改写。
    _NON_BODY_SEGMENTS = frozenset({"reply", "at", "face"})

    @staticmethod
    def _extract_plain_text(message: Dict[str, Any]) -> Optional[str]:
        """从出站消息里取出纯文本内容。

        跳过 ``reply`` / ``at`` / ``face`` 这些元数据段后，若剩下的全是
        text，就返回拼好的正文；出现图片、语音、文件等真实媒体则返回
        ``None``——那些消息不该被改写成语音。

        为什么 ``at`` 要跳过：群里回复常带 @，它只是指明对谁说话。
        早先把 ``at`` 当作「非文本」直接放弃，导致群聊里的概率语音与
        语音对讲**完全不生效**，而群聊带 @ 是常态。
        """

        raw_message = message.get("raw_message")
        if not isinstance(raw_message, list) or not raw_message:
            return None

        parts: List[str] = []
        for item in raw_message:
            if not isinstance(item, dict):
                return None
            seg_type = str(item.get("type", "")).strip().lower()
            if seg_type in GPTSovitsVoicePlugin._NON_BODY_SEGMENTS:
                continue
            if seg_type != "text":
                return None
            parts.append(str(item.get("data") or ""))

        text = "".join(parts).strip()
        return text or None

    @staticmethod
    def _collect_at_names(message: Dict[str, Any]) -> List[str]:
        """收集消息里 ``at`` 段指向的昵称。

        ``at`` 段的 ``data`` 结构在不同路径下不一致：
        适配器发的是 ``{"qq": id}``，宿主序列化的是
        ``{"target_user_id":..., "target_user_nickname":..., ...}``。
        这里只取能拿到的昵称/卡片名，拿不到就返回空列表。
        """

        names: List[str] = []
        for item in message.get("raw_message") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("type", "")).strip().lower() != "at":
                continue
            data = item.get("data")
            if not isinstance(data, dict):
                continue
            for key in ("target_user_cardname", "target_user_nickname"):
                value = str(data.get(key) or "").strip()
                if value and value not in names:
                    names.append(value)
        return names

    @classmethod
    def _spoken_text(cls, message: Dict[str, Any]) -> Optional[str]:
        """取出真正要朗读的正文。

        在 :meth:`_extract_plain_text` 的基础上，额外剔除正文里与 ``at``
        段**完全对应**的 ``@昵称``。

        为什么只按精确昵称删，而不是按 ``@`` 字符猜：实测出站正文里
        ``@`` 出现率只有 0.14%，且几乎都是说明性文字（例如
        「先引用原话再@对方接内容」），按字符删会把语义直接删没。
        真正的 @ 提及是独立消息段，正文里通常不含 ``@`` 字符；
        只有少数路径会把 ``@昵称`` 写进正文，这时用 at 段的昵称精确匹配
        才安全。
        """

        text = cls._extract_plain_text(message)
        if text is None:
            return None

        for name in cls._collect_at_names(message):
            mention = f"@{name}"
            if mention in text:
                text = text.replace(mention, " ")
        return text.strip() or None

    @staticmethod
    def _build_voice_message(message: Dict[str, Any], audio_path: str) -> Dict[str, Any]:
        """把出站消息改写成一条语音消息。

        使用 voiceurl 段携带文件路径：voice 段需要 binary_data_base64，
        把整段音频编码进消息体后会被当作文本读入上下文并按 token 计费，
        单条可达数十万 token。voiceurl 只传路径，适配器自行读取文件。

        改写时保留 ``at`` 段：群里 @ 某人回复是有意义的信息，全部丢掉
        会让语音回复失去指向对象。``reply`` 段则必须去掉——QQ 不支持
        「引用 + 语音」组合，带上会返回成功回执但不实际投递。

        两条出站路径（概率语音、语音对讲）共用此方法。
        """

        # 只保留 at 段，其余正文段都被语音替换
        kept = [
            dict(item)
            for item in (message.get("raw_message") or [])
            if isinstance(item, dict)
            and str(item.get("type", "")).strip().lower() == "at"
        ]

        updated = dict(message)
        updated["raw_message"] = kept + [{"type": "voiceurl", "data": audio_path}]
        return updated

    # ─── 语音对讲：入站标记（v1.6.0）─────────────────────────────────────

    @HookHandler(
        "chat.receive.after_process",
        name="gpt_sovits_intercom_mark",
        description="发现语音消息时打开该会话的语音回复窗口",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        # 只做判定与标记，不合成，必须很快返回
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def intercom_inbound_hook(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """收到语音消息时标记会话。

        刻意 **不** abort 消息链：让入站消息正常注册进 chat_manager 并入库，
        这样对方的发言会进入长期记忆，Planner 也能用完整上下文生成回复。
        语音转换在出站钩子里做。

        返回 ``None`` 表示不干预消息内容。
        """

        config = getattr(self.config, "intercom", None)
        if config is None or not config.enabled or not self._enabled():
            return None

        message = kwargs.get("message")
        if not isinstance(message, dict):
            return None

        transcript, is_voice = self._extract_voice_transcript(message)
        if not is_voice:
            return None

        # 仅私聊：消息带 group_info 就判为群聊。
        # 不用「group_id 非空」——群号缺失时会被误判成私聊。
        if config.private_only and self._is_group_message(message):
            self.ctx.logger.debug("群聊消息，仅私聊模式下不开启对讲")
            return None

        stream_id = str(message.get("session_id") or "").strip()
        if not stream_id:
            self.ctx.logger.warning("消息缺少 session_id，无法标记语音会话")
            return None

        if not transcript:
            self.ctx.logger.info("语音转写为空或失败，本次不开启对讲")
            return None

        decision = self._intercom.evaluate(
            stream_id=stream_id,
            text=transcript,
            min_text_length=config.min_text_length,
            hold_seconds=config.hold_seconds,
            require_wake_word=config.require_wake_word,
            wake_words=list(config.wake_words or []),
        )
        if not decision.respond:
            self.ctx.logger.debug("不开启对讲（%s）：%s", decision.reason, transcript[:30])
            return None

        self.ctx.logger.info("语音对讲开启[%s]：%s", stream_id, transcript[:60])
        return None

    @staticmethod
    def _extract_voice_transcript(message: Dict[str, Any]) -> Tuple[str, bool]:
        """从消息段里取语音转写文本，返回 ``(文本, 是否含语音)``。

        宿主开启 ``voice.enable_asr`` 后会把语音转成 ``[语音: xxx]``。
        """

        raw = message.get("raw_message")
        if not isinstance(raw, list) or not raw:
            return "", False

        parts: List[str] = []
        has_voice = False
        for item in raw:
            if not isinstance(item, dict):
                return "", False
            seg_type = str(item.get("type") or "").strip().lower()
            data = item.get("data")

            if seg_type == "reply":
                continue
            if seg_type == "voice":
                has_voice = True
                content = str(data or "").strip()
                if "转录失败" in content:
                    return "", True
                parts.append(_strip_voice_wrapper(content))
                continue
            if seg_type == "text":
                parts.append(str(data or "").strip())
                continue
            # 图片/表情/文件等不参与对讲判定
            return "", False

        if not has_voice:
            return "", False
        return " ".join(p for p in parts if p).strip(), True

    @staticmethod
    def _is_group_message(message: Dict[str, Any]) -> bool:
        """是不是群聊消息。

        宿主序列化时私聊的 ``group_info`` 为 ``None``，群聊是字典。
        只看结构是否存在，不依赖 group_id 的值。
        """

        info = message.get("message_info")
        if not isinstance(info, dict):
            return False
        return isinstance(info.get("group_info"), dict)

    @HookHandler(
        "send_service.before_send",
        name="gpt_sovits_random_voice",
        description="按配置概率把机器人的纯文本回复替换成语音",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        # 与 gpt_sovits.timeout 的默认值同档。
        # 这是阻塞钩子，超时越长出站消息被堵得越久，
        # 所以不跟随用户把 timeout 调大，宁可在极端情况下放弃转语音。
        timeout_ms=60000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def random_voice_hook(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """出站拦截：把纯文本回复改成语音。

        两条触发路径，优先级从高到低：

        1. **语音对讲**（``intercom.enabled``）：该会话在语音窗口内 → 一定转。
        2. **概率语音**（``random_voice.enabled``）：掷骰子决定 → 按概率转。

        返回 ``{"action": "continue", "modified_kwargs": {...}}`` 表示改写消息；
        返回 ``None`` 表示不干预，消息原样发出。

        任何一步判断不通过都直接放行文字，绝不因为插件故障吞掉用户消息。
        """

        message = kwargs.get("message")
        if not isinstance(message, dict):
            return None
        if not self._enabled():
            return None

        # 已经是语音消息的不要再套一层。
        # 同时认 ``voice``（base64/其他插件发出）与 ``voiceurl``（本插件
        # 改写后的形态）。当前 ``_extract_plain_text`` 遇到这两种都会返回
        # None，所以即便这里漏判也不会重复合成；但显式判掉更直观，
        # 也避免以后放宽文本提取规则时出现「语音套语音」。
        raw_message = message.get("raw_message")
        if isinstance(raw_message, list) and any(
            isinstance(item, dict)
            and str(item.get("type") or "").strip().lower() in {"voice", "voiceurl"}
            for item in raw_message
        ):
            return None

        text = self._spoken_text(message)
        if text is None:
            return None

        stream_id = str(kwargs.get("stream_id") or message.get("session_id") or "").strip()
        if not stream_id:
            return None

        # ── 路径 1：语音对讲优先 ──────────────────────────────────────
        intercom = getattr(self.config, "intercom", None)
        if intercom is not None and intercom.enabled and self._intercom.is_voice_active(stream_id):
            return await self._convert_for_intercom(kwargs, message, text, stream_id, intercom)

        # ── 路径 2：概率语音 ─────────────────────────────────────────
        config = getattr(self.config, "random_voice", None)
        if config is None or not config.enabled:
            return None

        suitable, reason = self._decider.text_is_suitable(
            text,
            config.min_text_length,
            config.max_text_length,
            config.skip_patterns or None,
        )
        if not suitable:
            self.ctx.logger.debug("概率语音跳过（%s）：%s", reason, text[:30])
            return None

        should_convert, from_cache = self._decider.should_convert(
            stream_id, config.probability, config.decision_ttl,
        )
        if not should_convert:
            return None

        log_prefix = f"[gpt_sovits_voice][random][{stream_id}]"

        audio_path, size = await self._synthesize_and_save(
            text, config.style, log_prefix, tag="概率语音",
        )
        if audio_path is None:
            return None

        self.ctx.logger.info(
            "%s 已将文字回复转为语音（%d 字节，%s），文本前 30 字：%s",
            log_prefix, size, "缓存命中" if from_cache else "新掷骰", text[:30],
        )
        return self._wrap_voice_reply(kwargs, message, audio_path)

    async def _synthesize_and_save(
        self,
        text: str,
        style: str,
        log_prefix: str,
        *,
        tag: str,
    ) -> Tuple[Optional[str], int]:
        """合成音频并落盘，返回 ``(路径, 字节数)``。

        失败时返回 ``(None, 0)``，调用方据此回退发送原始文字。
        概率语音与语音对讲两条出站路径共用这段流程——它们的差别只在
        「要不要转」的判定，一旦决定要转，后续步骤完全相同。

        统一走文件路径而非 base64：base64 会把整段音频编进消息体，
        随后作为文本进入上下文并按 token 计费，单条可达数十万 token。
        """

        try:
            audio_data = await self._synthesize_reply(text, style, log_prefix)
        except Exception as exc:  # noqa: BLE001 —— 无论如何都不能吞掉消息
            self.ctx.logger.error("%s %s合成异常，回退文字：%s", log_prefix, tag, exc)
            return None, 0

        if audio_data is None:
            self.ctx.logger.info("%s %s合成失败，回退文字", log_prefix, tag)
            return None, 0

        if self._audio is None:
            self.ctx.logger.warning("%s 音频发送器未就绪，回退文字", log_prefix)
            return None, 0

        audio_path = await self._audio.save_temp_file(audio_data, self.ctx)
        if audio_path is None:
            self.ctx.logger.warning("%s 音频落盘失败，回退文字", log_prefix)
            return None, 0

        return audio_path, len(audio_data)

    async def _convert_for_intercom(
        self,
        kwargs: Dict[str, Any],
        message: Dict[str, Any],
        text: str,
        stream_id: str,
        intercom: Any,
    ) -> Optional[Dict[str, Any]]:
        """语音对讲路径的转换：无条件转语音（受字数与可读性约束）。"""

        limit = max(5, int(intercom.max_reply_chars or 60))
        content = text
        if len(content) > limit:
            if str(intercom.oversize_strategy) == "text":
                self.ctx.logger.debug(
                    "对讲回复超长（%d > %d），保持文字", len(content), limit
                )
                return None
            content = content[:limit].rstrip()

        # 含链接/代码的回复念出来很别扭，保持文字
        for pattern in ("http://", "https://", "```", "![", "|--"):
            if pattern in content:
                self.ctx.logger.debug("对讲回复含不宜朗读内容（%s），保持文字", pattern)
                return None

        log_prefix = f"[gpt_sovits_voice][intercom][{stream_id}]"

        # 同一条文本在短时间内重复出现时不重复合成。
        # 一条回复常被拆成多段发送，实测同一会话两次相邻发送只隔几秒；
        # 每段都调一次 GPT-SoVITS 会让合成耗时与显存占用翻倍。
        cached_path = self._recall_intercom_audio(stream_id, content)
        if cached_path is not None:
            self.ctx.logger.debug("%s 复用刚合成的音频：%s", log_prefix, content[:30])
            return self._wrap_voice_reply(kwargs, message, cached_path)

        audio_path, size = await self._synthesize_and_save(
            content, "", log_prefix, tag="对讲",
        )
        if audio_path is None:
            return None

        self._remember_intercom_audio(stream_id, content, audio_path)
        self.ctx.logger.info(
            "%s 对讲回复转语音（%d 字节）：%s", log_prefix, size, content[:40]
        )
        return self._wrap_voice_reply(kwargs, message, audio_path)

    @staticmethod
    def _wrap_voice_reply(
        kwargs: Dict[str, Any],
        message: Dict[str, Any],
        audio_path: str,
    ) -> Dict[str, Any]:
        """把改写后的 kwargs 组装成钩子返回值（两条出站路径共用）。"""

        updated_kwargs = dict(kwargs)
        updated_kwargs["message"] = GPTSovitsVoicePlugin._build_voice_message(
            message, audio_path
        )
        # QQ 不支持「引用 + 语音」组合
        if kwargs.get("set_reply"):
            updated_kwargs["set_reply"] = False
            updated_kwargs["reply_message_id"] = ""
        return {"action": "continue", "modified_kwargs": updated_kwargs}

    def _remember_intercom_audio(self, stream_id: str, text: str, path: str) -> None:
        """记下刚合成的音频，供短时间内的重复文本复用。"""

        now = time.monotonic()
        self._intercom_audio[stream_id] = (text, path, now)
        # 顺手清掉过期项，避免字典无限增长
        ttl = 15.0
        for sid in [k for k, v in self._intercom_audio.items() if now - v[2] > ttl]:
            self._intercom_audio.pop(sid, None)

    def _recall_intercom_audio(self, stream_id: str, text: str) -> Optional[str]:
        """取回 15 秒内同一会话、同一文本的音频路径。"""

        item = self._intercom_audio.get(stream_id)
        if not item:
            return None
        cached_text, path, at = item
        if cached_text != text or time.monotonic() - at > 15.0:
            return None
        if not os.path.exists(path):
            # 已被清理任务删掉，不能复用
            self._intercom_audio.pop(stream_id, None)
            return None
        return path

    async def _synthesize_reply(
        self,
        text: str,
        style: str,
        log_prefix: str,
    ) -> Optional[bytes]:
        """出站路径的合成：复用与 Tool/Command 相同的文本清洗与选风格逻辑。"""

        if self._client is None:
            return None

        raw_text = ensure_sentence_end(clean_text(text))
        if not raw_text:
            return None

        style_name = self._resolve_style_chain(raw_text, style, log_prefix)
        if style_name is None:
            return None

        return await self._client.synthesize(raw_text, style_name, log_prefix)

    def _resolve_style_chain(
        self,
        raw_text: str,
        style: str,
        log_prefix: str,
    ) -> Optional[str]:
        """按「纯数字 → 自动语气 → 语言路由」的顺序确定最终风格。

        这段逻辑原本内嵌在 ``_handle`` 里，概率语音也要用，
        提出来避免两处实现走偏。
        """

        if self._client is None:
            return None

        style_name = self._client.resolve_style(style, log_prefix)

        number_style_used = False
        if not (style or "").strip():
            auto = getattr(self.config.gpt_sovits, "auto_style", None)
            if auto is not None and auto.number_style:
                if is_number_only(
                    raw_text,
                    allow_chinese=auto.number_style_include_chinese,
                ):
                    available = list(self._client.styles().keys())
                    if auto.number_style in available:
                        style_name = auto.number_style
                        number_style_used = True
                        self.ctx.logger.info(
                            "%s 检测到纯数字，使用风格: %s", log_prefix, style_name,
                        )

        if not (style or "").strip() and not number_style_used:
            auto = getattr(self.config.gpt_sovits, "auto_style", None)
            if auto is not None and auto.enabled:
                available = list(self._client.styles().keys())
                picked, reason = route_style(
                    raw_text,
                    available,
                    [r.model_dump() for r in auto.rules],
                    auto.fallback or self._client.default_style_name(),
                )
                if picked in available:
                    style_name = picked
                    self.ctx.logger.info(
                        "%s 自动语气: %s（%s）", log_prefix, picked, reason,
                    )
                    self._warn_missing_styles(available)

        lang = self._client.effective_language(style_name, raw_text)
        lang_style = self._client.resolve_by_language(lang, style_name, log_prefix)
        if lang_style != style_name:
            self.ctx.logger.info(
                "%s 语言路由: %s -> %s（语言=%s）",
                log_prefix, style_name, lang_style, lang,
            )
            style_name = lang_style

        config_error = self._client.validate_style(style_name)
        if config_error:
            self.ctx.logger.error("%s %s", log_prefix, config_error)
            return None

        return style_name

    # ─── 辅助 ────────────────────────────────────────────────────────────

    def _warn_missing_styles(self, available: List[str]) -> None:
        """首次发现内置规则引用了未配置的风格时，输出一次提示。"""

        if self._missing_styles_warned:
            return
        self._missing_styles_warned = True

        # 只需关注用户已配置了部分语气、但内置规则还引用其它语气的情况
        referenced = {name for name, _w, _p in DEFAULT_RULES}
        missing = sorted(referenced - set(available))
        if missing:
            self.ctx.logger.info(
                "内置语气规则引用了未配置的风格 %s，对应规则不会生效"
                "（如需启用请在 config.toml 的 [[gpt_sovits.styles]] 中补充）",
                missing,
            )

    async def _send_error(self, stream_id: str, message: str) -> None:
        """根据配置决定是否向用户发送错误提示。"""

        if self.config.general.send_error_messages:
            await self.ctx.send.text(message, stream_id)

    async def _send_help(self, stream_id: str) -> None:
        """发送帮助信息。

        ``_client`` 是 Optional（插件未完成加载时为空），这里必须自己兜底：
        帮助命令在 ``_handle`` 之前就走到了，不能假定运行时已就绪。
        """

        if self._client is None:
            await self.ctx.send.text("插件尚未完成加载，请稍后再试", stream_id)
            return

        styles = self._client.styles()
        style_desc = "、".join(styles.keys()) if styles else "（未配置）"
        fallback = self._client.default_style_name() or "default"
        auto = getattr(self.config.gpt_sovits, "auto_style", None)
        auto_desc = "开启" if (auto is not None and auto.enabled) else "关闭"
        help_text = (
            "【GPT-SoVITS 语音插件帮助】\n\n"
            "📝 基本语法：\n"
            "/tts <文本> [-v <风格>]\n\n"
            "🎯 示例：\n"
            "/tts 你好世界\n"
            "/tts 今天真开心 -v 兴奋\n"
            "/gptsovits 用本地语音合成\n\n"
            f"🎤 可用风格：{style_desc}\n"
            f"🧠 自动语气：{auto_desc}"
            f"（不指定风格时按文本内容自动选择，未命中则用 {fallback}）\n"
            f"🌏 多语言：检测到日语等语言时自动切换到对应语言模型（若已配置）"
        )
        await self.ctx.send.text(help_text, stream_id)

    # ─── 对外 API（v1.6.0）──────────────────────────────────────────────

    @API(
        "is_voice_active",
        description="查询某个会话当前是否处于语音对讲窗口内",
        version="1",
        public=True,
    )
    async def api_is_voice_active(self, stream_id: str = "") -> Dict[str, Any]:
        """供其他插件调用：判断该会话现在会不会用语音回复。"""

        sid = str(stream_id or "").strip()
        if not sid:
            return {"success": False, "active": False, "reason": "缺少 stream_id"}
        active = self._intercom.is_voice_active(sid)
        return {
            "success": True,
            "active": active,
            "window_count": len(self._intercom.active_streams()),
        }

    @API(
        "set_voice_active",
        description="强制开启或关闭某个会话的语音对讲窗口",
        version="1",
        public=True,
    )
    async def api_set_voice_active(
        self,
        stream_id: str = "",
        active: bool = True,
        seconds: int = 0,
    ) -> Dict[str, Any]:
        """供其他插件调用：手动控制语音窗口，绕过唤醒词与字数检查。"""

        sid = str(stream_id or "").strip()
        if not sid:
            return {"success": False, "reason": "缺少 stream_id"}

        if not active:
            self._intercom.close(sid)
            return {"success": True, "active": False}

        hold = int(seconds) if seconds and seconds > 0 else int(
            getattr(getattr(self.config, "intercom", None), "hold_seconds", 180) or 180
        )
        self._intercom.force_open(sid, hold_seconds=hold)
        return {"success": True, "active": True, "expires_in": hold}

    @API(
        "list_styles",
        description="列出当前可用的声音风格名",
        version="1",
        public=True,
    )
    async def api_list_styles(self) -> Dict[str, Any]:
        """供其他插件调用：查到有哪些音色可选。"""

        names: List[str] = []
        if self._client is not None:
            names = list(self._client.styles().keys())
        return {
            "success": bool(names),
            "styles": names,
            "default": self._client.default_style_name() if self._client else "",
        }

    def _project_root(self) -> str:
        """推断 MaiBot 项目根目录（plugins/<本插件>/plugin.py 向上三级）。"""

        return str(Path(__file__).resolve().parents[2])


def create_plugin() -> GPTSovitsVoicePlugin:
    """创建 GPT-SoVITS 语音插件实例（SDK 入口）。"""

    return GPTSovitsVoicePlugin()
