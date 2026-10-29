"""插件 Pydantic 配置模型。"""

from typing import Any, List, Literal

from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator

from .utils import MODEL_VERSION_CHOICES, VERSION_CANONICAL

# 底模版本可选值。直接引用 utils 里的唯一定义，用 Literal 展开是为了让
# WebUI 把该字段渲染成下拉框而不是文本框。
# 用 `*` 解包需要 Python 3.11+，宿主本身要求 >=3.12，无需额外兼容。
ModelVersion = Literal[*MODEL_VERSION_CHOICES]


def _normalize_model_version(value: Any) -> Any:
    """把版本名归一化为标准写法，无法识别时原样返回交由 Literal 报错。"""

    if not isinstance(value, str):
        return value
    return VERSION_CANONICAL.get(value.strip().lower(), value)


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra={
            "label": "启用插件",
            "hint": (
                "关闭后不再响应 /tts 命令、tts_voice 工具、概率语音与语音对讲，"
                "两条出站钩子也会直接放行原消息。"
            ),
        },
    )
    config_version: str = Field(
        default="1.1.0",
        description="配置版本",
        json_schema_extra={
            "label": "配置版本",
            "hint": (
                "宿主据此判断配置结构版本，升级时自动补齐新增字段、迁移旧值。"
                "**不可删除或留空**——缺失时宿主会拒绝加载插件，只保留配置管理。"
                "正常使用无需修改。"
            ),
        },
    )


class GeneralConfig(PluginConfigBase):
    """通用设置。"""

    __ui_label__ = "通用"
    __ui_icon__ = "settings"
    __ui_order__ = 1

    min_text_length: int = Field(
        default=2,
        description="文本最小长度，过短（如单字）合成效果差，低于此值拒绝合成",
        json_schema_extra={
            "label": "文本最小长度",
            "hint": (
                "低于该字数直接拒绝合成。单字缺少上下文，GPT-SoVITS 容易读成杂音或吞字，"
                "建议保持 2 以上。"
            ),
            "placeholder": "2",
        },
    )
    max_text_length: int = Field(
        default=200,
        description="文本最大长度，超过有超时风险，拒绝合成",
        json_schema_extra={
            "label": "文本最大长度",
            "hint": (
                "超过该字数拒绝合成。文本越长合成耗时越久，容易触发 timeout。"
                "另外 QQ 语音本身有长度限制，过长会被平台截断。建议 100~300。"
            ),
            "placeholder": "200",
        },
    )
    use_base64_audio: bool = Field(
        default=False,
        description=(
            "true=base64 走 IPC；false=文件路径 voiceurl。"
            "base64 模式会把整段音频编码后塞进消息体，"
            "该内容会作为文本进入 MaiBot 上下文，单条可达数十万 token，"
            "极易造成费用暴涨，除非明确了解后果否则请保持 false"
        ),
        json_schema_extra={
            "label": "base64 发送音频",
            "hint": (
                "⚠️ 强烈建议保持关闭。开启后音频会以 base64 编码塞进消息体，"
                "MaiBot 会把它当普通文本读入上下文并按 token 计费——"
                "一条十几秒的语音约 30 万字符，单次请求可消耗数十万 token，"
                "且历史裁剪按条数计算不会移除它，会在每轮请求中反复重发，导致费用暴涨。"
                "关闭时走文件路径（voiceurl），只传路径，需要 adapter 支持该消息段。"
            ),
        },
    )
    audio_output_dir: str = Field(
        default="",
        description=(
            "音频输出目录（相对项目根目录或绝对路径）。"
            "留空则用 Runner 分配的插件临时目录，这是推荐做法"
        ),
        json_schema_extra={
            "label": "音频输出目录",
            "hint": (
                "文件模式下临时音频的落盘位置，支持相对项目根目录或绝对路径。"
                "**建议留空**：留空时用 Runner 分配的 runtime_dir，"
                "不用猜主程序装在哪，也不会污染插件仓库。"
                "音频发出后会延迟 180 秒自动删除。"
            ),
            "placeholder": "留空 = 使用插件临时目录",
        },
    )
    send_error_messages: bool = Field(
        default=True,
        description="合成失败时是否向用户发送错误提示",
        json_schema_extra={
            "label": "发送错误提示",
            "hint": (
                "开启后合成失败会在聊天里提示原因（文本过长、服务未启动等），方便排查。"
                "关闭则静默失败，只写日志，适合不想打扰群友的场景。"
            ),
        },
    )


class GPTSovitsStyle(PluginConfigBase):
    """GPT-SoVITS 单个语音风格配置。"""

    name: str = Field(
        default="default",
        description="风格名称",
        json_schema_extra={
            "label": "风格名称",
            "hint": "调用时用来指定风格的标识，需与自动语气规则、/tts -v 的值对应。",
            "placeholder": "default",
        },
    )
    refer_wav: str = Field(
        default="",
        description="参考音频路径",
        json_schema_extra={
            "label": "参考音频",
            "hint": (
                "必填。用于克隆音色的参考音频，建议 3~10 秒、干净无背景音。"
                "末尾若是突然截断，合成时最后一个字也容易含糊，"
                "建议选一段自然收尾的录音。"
                "服务在本机时填本机路径，插件会检查文件是否存在；"
                "连远程服务时填**服务器上**的路径，插件不会在本地校验。"
            ),
            "placeholder": "C:/path/to/reference.wav",
        },
    )
    prompt_text: str = Field(
        default="",
        description="参考音频对应的文本",
        json_schema_extra={
            "label": "参考音频文本",
            "hint": (
                "必填。必须与参考音频里说的话逐字一致，标点可省略。"
                "填错会导致合成的音频音色漂移或整段静音。"
            ),
            "placeholder": "参考音频里说的那句话",
        },
    )
    prompt_language: str = Field(
        default="zh",
        description="参考音频语言",
        json_schema_extra={
            "label": "参考音频语言",
            "hint": "参考音频所用的语言代码：zh / ja / en / ko / yue，需与音频内容一致。",
            "placeholder": "zh",
        },
    )
    gpt_weights: str = Field(
        default="",
        description="GPT 模型权重路径（可选，动态切模型）",
        json_schema_extra={
            "label": "GPT 权重路径",
            "hint": (
                "可选。留空表示沿用 GPT-SoVITS 服务当前已加载的权重。"
                "填写后每次合成前会调用接口切换，多风格共用同一服务时使用。"
            ),
            "placeholder": "留空则用服务当前权重",
        },
    )
    sovits_weights: str = Field(
        default="",
        description="SoVITS 模型权重路径（可选）",
        json_schema_extra={
            "label": "SoVITS 权重路径",
            "hint": "可选。与 GPT 权重配套使用，留空则沿用服务当前已加载的权重。",
            "placeholder": "留空则用服务当前权重",
        },
    )
    languages: List[str] = Field(
        default_factory=list,
        description=(
            "该风格适用的语言列表（zh/ja/en/ko/yue），留空表示通用；"
            "非空时按检测到的语言优先匹配"
        ),
        json_schema_extra={
            "label": "适用语言",
            "hint": (
                "留空 = 所有语言都用这个风格。"
                "填写后按检测到的语言优先匹配，适合为不同语种配不同音色。"
            ),
            "placeholder": "如 zh, ja；留空表示通用",
        },
    )
    language: str = Field(
        default="",
        description=(
            "强制指定合成语言，跳过自动检测（如 zh/ja/en/ko/yue）。"
            "留空则自动检测。适用于纯汉字日文等无法自动区分的场景"
        ),
        json_schema_extra={
            "label": "强制合成语言",
            "hint": (
                "留空 = 自动检测。日文里有大量汉字，自动检测可能误判为中文，"
                "这种情况可在此固定为 ja。"
            ),
            "placeholder": "留空则自动检测",
        },
    )


class AutoStyleRule(PluginConfigBase):
    """自动语气匹配规则。"""

    style: str = Field(
        default="",
        description="命中后使用的风格名",
        json_schema_extra={
            "label": "目标风格",
            "hint": "规则命中时切换到该风格，值需与 [[gpt_sovits.styles]] 的 name 一致。",
            "placeholder": "如 兴奋",
        },
    )
    pattern: str = Field(
        default="",
        description="正则表达式",
        json_schema_extra={
            "label": "匹配正则",
            "hint": "对回复文本做正则匹配，命中即采用该风格。支持 Python re 语法。",
            "placeholder": r"如 [！!]{2,}",
        },
    )
    weight: int = Field(
        default=50,
        description="命中权重，多个规则同时命中时取最高",
        json_schema_extra={
            "label": "命中权重",
            "hint": "多条规则同时命中时，累加同一风格的权重后取最高者。默认 50。",
            "placeholder": "50",
        },
    )


class AutoStyleConfig(PluginConfigBase):
    """自动语气选择配置。"""

    __ui_label__ = "自动语气"
    __ui_icon__ = "wand"
    __ui_order__ = 3

    enabled: bool = Field(
        default=True,
        description="开启后，调用方未指定 style 时按文本内容自动选择风格",
        json_schema_extra={
            "label": "启用自动语气",
            "hint": (
                "开启后按文本内容（感叹号、语气词等）自动挑风格，更有表现力。"
                "关闭则始终使用下方的兜底风格。"
            ),
        },
    )
    fallback: str = Field(
        default="default",
        description="所有规则都未命中时使用的风格",
        json_schema_extra={
            "label": "兜底风格",
            "hint": "没有规则命中时使用的风格名，需与已配置的 styles 名称一致。",
            "placeholder": "default",
        },
    )
    rules: List[AutoStyleRule] = Field(
        default_factory=list,
        description="自定义匹配规则（留空则使用插件内置规则）",
        json_schema_extra={
            "label": "自定义匹配规则",
            "hint": (
                "留空 = 使用插件内置规则（兴奋/吐槽/撒娇等）。"
                "填写后内置规则仍会生效，自定义规则优先参与评分。"
            ),
        },
    )
    number_style: str = Field(
        default="",
        description=(
            "纯数字文本使用的风格（如价格、时间、电话号码）。"
            "留空则按正常流程处理。播报数字与说话的韵律差异较大，"
            "指定专用风格往往效果更好"
        ),
        json_schema_extra={
            "label": "纯数字专用风格",
            "hint": (
                "文本全是数字时（如「2333」「123」）改用该风格，值需与 styles 的 name 一致。"
                "留空则按常规语气规则处理。"
            ),
            "placeholder": "留空则不启用，如 数字",
        },
    )
    number_style_include_chinese: bool = Field(
        default=True,
        description="判断纯数字时，是否把中文数字（一二三、十百千等）也算作数字",
        json_schema_extra={
            "label": "中文数字也算数字",
            "hint": "开启后「一二三」也会被判定为纯数字文本，从而走纯数字专用风格。",
        },
    )


class RandomVoiceConfig(PluginConfigBase):
    """概率语音配置：按概率把普通文字回复自动转成语音。"""

    __ui_label__ = "概率语音"
    __ui_icon__ = "dice"
    __ui_order__ = 4

    enabled: bool = Field(
        default=False,
        description=(
            "开启后，机器人的普通文字回复会按 probability 概率自动转成语音发送。"
            "关闭时行为不变，仅响应 LLM 工具调用与 /tts 命令"
        ),
        json_schema_extra={
            "label": "启用概率语音",
            "hint": (
                "开启后机器人会自己决定偶尔用语音回复，更像真人。"
                "关闭时只有 LLM 主动调用工具或用户发 /tts 才会合成。"
            ),
        },
    )
    probability: float = Field(
        default=0.3,
        description="自动转语音的概率（0~1），0.3 表示约三成回复会变成语音",
        json_schema_extra={
            "label": "转语音概率",
            "hint": (
                "0~1 之间。0.3 表示约三成回复转成语音。"
                "太高会显得吵闹且明显增加合成开销，建议 0.1~0.3。"
            ),
            "placeholder": "0.3",
        },
    )
    decision_ttl: float = Field(
        default=8.0,
        description=(
            "同一条回复在多少秒内复用同一个随机结果。"
            "避免一条长回复被拆成多段时语音、文字混发"
        ),
        json_schema_extra={
            "label": "决策缓存时长（秒）",
            "hint": (
                "一条回复常被拆成多段发送。该时长内沿用第一次的随机结果，"
                "保证整段回复要么全是语音、要么全是文字，不会中途切换。"
            ),
            "placeholder": "8",
        },
    )
    min_text_length: int = Field(
        default=2,
        description="短于此长度的回复不转语音（如「嗯」「好」）",
        json_schema_extra={
            "label": "最短字数",
            "hint": "短于该字数的回复不转语音。太短的话合成出来很突兀，建议 2 以上。",
            "placeholder": "2",
        },
    )
    max_text_length: int = Field(
        default=80,
        description="长于此长度的回复不转语音（长文用语音不好听，也容易超时）",
        json_schema_extra={
            "label": "最长字数",
            "hint": (
                "长于该字数的回复不转语音。语音念长文既费时又难听，"
                "而且容易触发超时。默认 80。"
            ),
            "placeholder": "80",
        },
    )
    skip_patterns: List[str] = Field(
        default_factory=list,
        description=(
            "命中这些正则的文本不转语音，如代码块、网址、表格。留空则使用内置规则"
        ),
        json_schema_extra={
            "label": "不转语音的规则（正则）",
            "hint": (
                "留空 = 使用内置规则（自动跳过代码块、网址、表格、列表、HTML、Markdown 链接）。"
                "注意：一旦填写，内置规则会被完全替换，不会追加。"
            ),
            "placeholder": r"例如 ``` 或 https?://\S+",
        },
    )
    style: str = Field(
        default="",
        description="自动转语音时使用的风格，留空则走自动语气选择",
        json_schema_extra={
            "label": "随机语音使用的风格",
            "hint": (
                "留空 = 按文本内容自动判断语气（推荐）。"
                "填写则固定使用该风格，值需与 [[gpt_sovits.styles]] 的 name 一致，如「默认」。"
            ),
            "placeholder": "留空则自动选择，如 默认",
        },
    )


class GPTSovitsConfig(PluginConfigBase):
    """GPT-SoVITS 后端配置。"""

    __ui_label__ = "GPT-SoVITS"
    __ui_icon__ = "server"
    __ui_order__ = 2

    server: str = Field(
        default="http://127.0.0.1:9880",
        description="GPT-SoVITS 服务地址",
        json_schema_extra={
            "label": "服务地址",
            "hint": (
                "GPT-SoVITS 的 API 地址，通常是 api_v2.py 启动后监听的端口。"
                "本机默认 9880；换机器或改端口时同步修改这里。"
                "填错会提示网络错误，请先用浏览器打开 /docs 确认服务已启动。"
                "也可以填远程地址（如 http://192.168.1.10:9880），"
                "此时参考音频和模型权重要填服务器上的路径。"
            ),
            "placeholder": "http://127.0.0.1:9880",
        },
    )
    timeout: int = Field(
        default=60,
        description="请求超时（秒）",
        json_schema_extra={
            "label": "请求超时（秒）",
            "hint": (
                "单次合成请求的最长等待时间。文本越长、模型越大耗时越久，"
                "长文本或首次加载模型时可能需要放宽到 90~120。"
                "超时不会重试，只会返回失败提示。"
            ),
            "placeholder": "60",
        },
    )
    model_version: ModelVersion = Field(
        default="auto",
        description="底模版本，决定支持哪些语言",
        json_schema_extra={
            "label": "底模版本",
            "hint": (
                "决定插件允许合成哪些语言，选错会导致 400 报错或空白音频。"
                "auto = 按权重路径自动推断（推荐），推断不出时按 v2ProPlus 处理。"
                "v1 = 中英日；其余版本 = 中英日粤。"
                "韩语在所有版本上都不可用（上游缺 eunjeon 依赖会抛内部异常），"
                "俄语、法语、德语、泰语等所有版本都不支持。"
            ),
        },
    )
    speed_factor: float = Field(
        default=1.0,
        description="语速倍率，越大越快",
        json_schema_extra={
            "label": "语速倍率",
            "hint": (
                "1.0 = 原始语速，越大越快。\n"
                "实测这个参数不呈线性：0.9~1.0 之间几乎听不出差别，1.2 以上才明显加快。"
                "所以想靠调到 0.9 来放慢语速，效果很有限。\n"
                "如果觉得合成结果整体偏快，根源通常是参考音频本身语速快，"
                "换一段语速更慢的参考音频比调这个参数更有效。"
            ),
            "placeholder": "1.0",
        },
    )
    fragment_interval: float = Field(
        default=0.0,
        description="分段之间的静音间隔（秒）",
        json_schema_extra={
            "label": "分段间隔（秒）",
            "hint": (
                "GPT-SoVITS 会把长文本按标点切成多段分别合成，再用纯静音拼接。"
                "间隔越大，句间停顿越空洞，还会在停顿前后产生衰减尾音和硬起音，听感像「颤音」。\n"
                "注意：这个参数只在「文本真的被切成多段」时才生效。"
                "切分方式是 cut0 时整段不切分，设了也没用；"
                "用 cut1 时短句会被合并成一整块，同样不生效。"
                "想要明显停顿，用 cut5 + 0.05~0.1。"
            ),
            "placeholder": "0",
        },
    )
    text_split_method: str = Field(
        default="cut0",
        description="文本切分方式（cut0~cut5）",
        json_schema_extra={
            "label": "文本切分方式",
            "x-widget": "select",
            "options": ["cut0", "cut1", "cut2", "cut3", "cut4", "cut5"],
            "x-option-labels": {
                "cut0": "cut0 整段合成（停顿最自然，推荐）",
                "cut1": "cut1 四句一切（短句不切分，容易没停顿）",
                "cut2": "cut2 按标点切且块更长",
                "cut3": "cut3 按标点切",
                "cut4": "cut4 按标点切且块较短",
                "cut5": "cut5 每标点都切（停顿最明确）",
            },
            "hint": (
                "决定句与句之间有没有停顿，是最影响自然度的参数。\n"
                "cut0 = 整段一次合成，由模型自己决定在哪停顿，听感最自然；"
                "但长文本容易显存不足或超时，建议配合 80 字以内的文本。\n"
                "cut1 = 按句号切，但「每块尽量凑够 4 句」——短回复会被合并成一整块，"
                "实际并不切分，听起来几乎没有停顿。这是最常见的误区。\n"
                "cut5 = 每个标点都切，句间停顿最明确，适合想要抑扬顿挫的场景；"
                "需要配合「分段间隔」控制停顿长度，否则会显得拖沓。\n"
                "想让停顿更明显时，优先用 cut5 + 分段间隔 0.05~0.1，而不是调 cut1。"
            ),
        },
    )
    min_chunk_length: int = Field(
        default=16,
        description="切块的最小长度（字），过短的块会被合并",
        json_schema_extra={
            "label": "最小切块长度",
            "hint": (
                "切分后短于该长度的块会与相邻块合并。块太短时开头起音来不及建立，"
                "听感上就是第一个字含糊。16 是官方默认；开头吞字明显时可调到 24~32。"
            ),
            "placeholder": "16",
        },
    )
    overlap_length: int = Field(
        default=0,
        description="切块之间的重叠字数",
        json_schema_extra={
            "label": "切块重叠字数",
            "hint": (
                "相邻块在交界处重复的字数，用来减少接缝处的断裂感。"
                "官方默认 2，但重复会导致听感上有个字被念两遍。"
                "如果听到交界处有重复字，调为 0。"
            ),
            "placeholder": "0",
        },
    )
    top_k: int = Field(
        default=15,
        description="采样 top_k",
        json_schema_extra={
            "label": "top_k",
            "hint": "采样时只保留概率最高的前 k 个候选。数值越低越稳定但越平淡，默认 15。",
            "placeholder": "15",
        },
    )
    top_p: float = Field(
        default=1.0,
        description="采样 top_p",
        json_schema_extra={
            "label": "top_p",
            "hint": "核采样阈值，与 top_k 配合控制随机性。默认 1.0 表示不额外截断。",
            "placeholder": "1.0",
        },
    )
    temperature: float = Field(
        default=1.0,
        description="采样温度",
        json_schema_extra={
            "label": "温度",
            "hint": "越高越有变化但越容易跑调，越低越平稳但可能呆板。默认 1.0。",
            "placeholder": "1.0",
        },
    )
    styles: List[GPTSovitsStyle] = Field(
        default_factory=lambda: [GPTSovitsStyle()],
        description="语音风格配置列表（每个风格独立完整参数）",
    )
    auto_style: AutoStyleConfig = Field(
        default_factory=AutoStyleConfig,
        description="根据文本内容自动选择语气",
    )

    @field_validator("model_version", mode="before")
    @classmethod
    def _check_model_version(cls, value: Any) -> Any:
        return _normalize_model_version(value)


class IntercomConfig(PluginConfigBase):
    """语音对讲：收到语音消息后，一段时间内的回复都用语音。

    与「概率语音」的区别：概率语音按骰子决定单条回复是否转语音；
    语音对讲按会话状态决定——对方发了语音，就进入一段语音对话，
    直到超时才恢复文字。
    """

    __ui_label__ = "语音对讲"
    __ui_icon__ = "phone-call"
    __ui_order__ = 5

    enabled: bool = Field(
        default=False,
        description=(
            "开启后，收到语音消息会打开一个语音窗口，窗口内的回复都用语音发出。"
            "适合「打电话」式的连续语音对话"
        ),
        json_schema_extra={
            "label": "启用语音对讲",
            "hint": (
                "与「概率语音」不同：概率语音每句话掷骰子决定；"
                "语音对讲是对方发了语音后，连续一段时间都用语音回，更像对话。"
                "两者可以同时开，语音对讲优先。"
            ),
        },
    )
    private_only: bool = Field(
        default=True,
        description="只在私聊生效，群里不触发",
        json_schema_extra={
            "label": "仅私聊",
            "hint": (
                "默认开启。群里发语音不会触发对讲，避免连续刷语音打扰别人。"
                "想让她在群里也用语音对话时再关掉。"
            ),
        },
    )
    require_wake_word: bool = Field(
        default=False,
        description="需要先收到唤醒词才进入对讲状态",
        json_schema_extra={
            "label": "需要唤醒词",
            "hint": (
                "开启后必须先用文字或语音说唤醒词才开始对讲。"
                "关闭时任意语音都直接触发。"
            ),
        },
    )
    wake_words: List[str] = Field(
        default_factory=lambda: ["语音对讲", "打电话", "对讲"],
        description="唤醒词列表，命中任意一个即进入对讲状态",
        json_schema_extra={
            "label": "唤醒词",
            "hint": "仅「需要唤醒词」开启时生效。命中后会重置该会话的超时计时。",
        },
    )
    min_text_length: int = Field(
        default=1,
        ge=1,
        le=50,
        description="转写文本短于该字数时不触发，避免噪声误判",
        json_schema_extra={
            "label": "最短文本",
            "hint": (
                "ASR 常把杂音识别成一两个字，低于该长度不触发语音回复，"
                "消息仍会正常进入对话。"
            ),
            "placeholder": "1",
        },
    )
    hold_seconds: int = Field(
        default=180,
        ge=10,
        le=3600,
        description="收到语音后多少秒内的回复都转成语音",
        json_schema_extra={
            "label": "语音窗口（秒）",
            "hint": (
                "对方发了语音后的这段时间里，她的回复都会用语音发出；"
                "超时后自动恢复文字。每条新语音都会续期。默认 3 分钟。"
            ),
            "placeholder": "180",
        },
    )
    max_reply_chars: int = Field(
        default=60,
        ge=5,
        le=500,
        description="对讲模式下合成前的最大字数",
        json_schema_extra={
            "label": "回复字数上限",
            "hint": (
                "语音太长会拖慢合成、也容易被平台截断。"
                "超过时按下面的策略处理。"
            ),
            "placeholder": "60",
        },
    )
    oversize_strategy: Literal["text", "truncate"] = Field(
        default="text",
        description="对讲模式下回复超长的处理方式",
        json_schema_extra={
            "label": "超长处理",
            "hint": (
                "text=超长就发文字（推荐，读起来完整）；"
                "truncate=强行截断后合成语音。"
            ),
        },
    )


class PluginConfig(PluginConfigBase):
    """插件总配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    gpt_sovits: GPTSovitsConfig = Field(default_factory=GPTSovitsConfig)
    random_voice: RandomVoiceConfig = Field(
        default_factory=RandomVoiceConfig,
        description="概率语音：按概率自动把文字回复转成语音",
    )
    intercom: IntercomConfig = Field(
        default_factory=IntercomConfig,
        description="语音对讲：收到语音后一段时间内的回复都用语音",
    )
