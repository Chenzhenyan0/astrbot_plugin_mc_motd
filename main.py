"""AstrBot Minecraft MOTD 查询插件

通过 /motd 指令查询 Minecraft 服务器（Java 版 / 基岩版）的 MOTD 状态，
调用 motd.frostlynx.work 的即时状态 API 获取 JSON 数据，
再使用 Pillow 在本地渲染为信息卡片图片发送给用户。
"""

import base64
import io
import os
import re
import tempfile
from datetime import datetime
from urllib.parse import quote

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_API_BASE = "https://motd.frostlynx.work/v1"
DEFAULT_PORTS = {"java": 25565, "bedrock": 19132}
EDITION_LABELS = {"java": "Java 版", "bedrock": "基岩版"}

# 匹配 Minecraft 颜色/样式代码（§a、&l 等）
COLOR_CODE_RE = re.compile(r"[§&][0-9a-fk-orx]", re.IGNORECASE)

# 常见中文字体候选路径（Linux / macOS / Windows）
FONT_CANDIDATES_REGULAR = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
FONT_CANDIDATES_BOLD = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


class MotdApiError(Exception):
    """MOTD 查询失败，message 为面向用户的友好提示。"""


# ---------------------------------------------------------------------------
# 网络查询
# ---------------------------------------------------------------------------


async def fetch_status(
    session: aiohttp.ClientSession,
    api_base: str,
    edition: str,
    address: str,
    port: int,
) -> dict:
    """调用 MOTD API 即时状态接口，返回响应 JSON。

    失败时抛出 MotdApiError（携带友好提示）。
    """
    edition = (edition or "").lower()
    if edition not in DEFAULT_PORTS:
        raise MotdApiError("未知的服务器版本，仅支持 java / bedrock。")
    address = (address or "").strip()
    if not address:
        raise MotdApiError("服务器地址不能为空。")
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise MotdApiError("端口必须是 1-65535 之间的整数。")
    if not (1 <= port <= 65535):
        raise MotdApiError("端口必须是 1-65535 之间的整数。")

    url = f"{api_base.rstrip('/')}/status/{edition}/{quote(address, safe='')}"
    try:
        async with session.get(url, params={"port": port}) as resp:
            if resp.status == 200:
                return await resp.json()
            msg = ""
            try:
                err = await resp.json()
                msg = err.get("message") or err.get("error") or ""
            except Exception:  # noqa: BLE001,S110 - 错误体解析失败不影响主流程
                pass
            if resp.status == 400:
                raise MotdApiError(f"查询参数有误：{msg or '请检查地址与端口格式'}")
            if resp.status == 429:
                raise MotdApiError("查询过于频繁，API 限流中，请稍后再试。")
            if resp.status == 504:
                raise MotdApiError("无法连接到目标服务器（地址无法解析或查询超时）。")
            raise MotdApiError(
                f"API 返回异常（HTTP {resp.status}）：{msg or '未知错误'}"
            )
    except TimeoutError:
        raise MotdApiError("查询超时，请稍后重试。")
    except aiohttp.ClientError as e:
        raise MotdApiError(f"网络请求失败：{e}")


# ---------------------------------------------------------------------------
# 文本与字体工具
# ---------------------------------------------------------------------------


def strip_color_codes(text: str) -> str:
    """移除 Minecraft 颜色/样式代码。"""
    return COLOR_CODE_RE.sub("", text or "")


def _load_font(
    size: int, bold: bool = False, custom_path: str = ""
) -> ImageFont.FreeTypeFont:
    """按优先级加载字体：用户自定义 > 系统候选 > PIL 默认位图字体。"""
    candidates = []
    if custom_path:
        candidates.append(custom_path)
    candidates.extend(FONT_CANDIDATES_BOLD if bold else FONT_CANDIDATES_REGULAR)
    for path in candidates:
        if path and os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:  # noqa: BLE001,S112 - 字体损坏则尝试下一个候选
                continue
    return ImageFont.load_default()


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list:
    """按像素宽度对文本换行（兼容中英文，按字符断行）。"""
    lines = []
    for raw_line in (text or "").split("\n"):
        line = ""
        for ch in raw_line:
            if draw.textlength(line + ch, font=font) <= max_width:
                line += ch
            else:
                lines.append(line)
                line = ch
        lines.append(line)
    return lines or [""]


# ---------------------------------------------------------------------------
# 图片渲染
# ---------------------------------------------------------------------------

THEMES = {
    "dark": {
        "bg": (24, 26, 32),
        "card": (34, 37, 46),
        "fg": (235, 238, 245),
        "sub": (150, 155, 168),
        "divider": (58, 62, 74),
        "accent": (96, 165, 250),
    },
    "light": {
        "bg": (236, 239, 244),
        "card": (255, 255, 255),
        "fg": (32, 33, 36),
        "sub": (110, 115, 128),
        "divider": (220, 224, 230),
        "accent": (59, 130, 246),
    },
}


def render_status_card(
    data: dict,
    edition: str,
    address: str,
    port: int,
    theme: str = "dark",
    width: int = 880,
    font_path: str = "",
) -> Image.Image:
    """将 API 返回的 JSON 渲染为状态卡片图片，返回 PIL Image。"""
    colors = THEMES.get((theme or "dark").lower(), THEMES["dark"])
    width = max(600, min(int(width or 880), 1600))

    online = str(data.get("status", "")).lower() == "online"
    status_color = (74, 222, 128) if online else (248, 113, 113)
    status_text = "在线" if online else "离线"

    font_title = _load_font(34, bold=True, custom_path=font_path)
    font_badge = _load_font(20, custom_path=font_path)
    font_label = _load_font(22, custom_path=font_path)
    font_value = _load_font(24, custom_path=font_path)
    font_motd = _load_font(24, custom_path=font_path)
    font_footer = _load_font(18, custom_path=font_path)

    pad = 32
    content_width = width - pad * 2

    # --- 收集展示字段 -------------------------------------------------------
    rows = []  # (label, value)
    version = data.get("version") or {}
    ver_txt = version.get("name") or ""
    if version.get("protocol") is not None:
        ver_txt += (
            f"（协议 {version['protocol']}）"
            if ver_txt
            else f"协议 {version['protocol']}"
        )
    if ver_txt:
        rows.append(("版本", ver_txt))

    players = data.get("players") or {}
    if players.get("online") is not None or players.get("max") is not None:
        p_txt = f"{players.get('online', 0)} / {players.get('max', 0)}"
        sample = players.get("sample") or []
        names = [p.get("name", "") for p in sample if p.get("name")]
        if names:
            shown = names[:12]
            p_txt += (
                "　在线玩家: " + ", ".join(shown) + (" ..." if len(names) > 12 else "")
            )
        rows.append(("玩家", p_txt))

    if data.get("latency") is not None:
        rows.append(("延迟", f"{data['latency']} ms"))

    if data.get("gamemode"):
        rows.append(("游戏模式", str(data["gamemode"])))

    motd_raw = (
        (data.get("motd") or {}).get("clean")
        or (data.get("motd") or {}).get("raw")
        or ""
    )
    motd_text = strip_color_codes(motd_raw).strip()

    # --- 先用临时画布计算排版高度 ------------------------------------------
    tmp = ImageDraw.Draw(Image.new("RGB", (width, 100)))
    row_h = 42
    header_h = 100
    motd_lines = []
    if motd_text:
        for ln in _wrap_text(tmp, motd_text, font_motd, content_width - 40):
            motd_lines.append(ln)
            if len(motd_lines) >= 6:  # 最多 6 行，防止超长 MOTD 撑爆图片
                motd_lines[-1] = motd_lines[-1].rstrip() + " ..."
                break
    motd_block_h = (len(motd_lines) * 34 + 52) if motd_lines else 0
    footer_h = 56
    height = (
        pad + header_h + 16 + 1 + 16 + len(rows) * row_h + motd_block_h + footer_h + pad
    )

    # --- 正式绘制 -----------------------------------------------------------
    img = Image.new("RGB", (width, height), colors["bg"])
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [8, 8, width - 8, height - 8], radius=18, fill=colors["card"]
    )

    x = pad
    y = pad

    # favicon
    favicon_img = None
    favicon_b64 = data.get("favicon") or ""
    if favicon_b64:
        try:
            if "," in favicon_b64:
                favicon_b64 = favicon_b64.split(",", 1)[1]
            favicon_img = Image.open(io.BytesIO(base64.b64decode(favicon_b64))).convert(
                "RGBA"
            )
            favicon_img = favicon_img.resize((80, 80), Image.NEAREST)
        except Exception:  # noqa: BLE001 - favicon 解析失败时降级为默认图标
            favicon_img = None
    if favicon_img:
        img.paste(favicon_img, (x, y + 4), favicon_img)
        text_x = x + 96
    else:
        # 无 favicon 时绘制一个默认方块图标
        draw.rounded_rectangle(
            [x, y + 4, x + 80, y + 84], radius=10, fill=colors["accent"]
        )
        draw.text(
            (x + 40, y + 44), "MC", font=font_title, fill=(255, 255, 255), anchor="mm"
        )
        text_x = x + 96

    # 服务器地址 + 端口
    draw.text((text_x, y + 8), f"{address}:{port}", font=font_title, fill=colors["fg"])
    # 版本徽标
    badge_text = EDITION_LABELS.get(edition, edition)
    bw = draw.textlength(badge_text, font=font_badge) + 24
    draw.rounded_rectangle(
        [text_x, y + 56, text_x + bw, y + 84], radius=14, fill=colors["accent"]
    )
    draw.text((text_x + 12, y + 60), badge_text, font=font_badge, fill=(255, 255, 255))

    # 在线状态（右上角）
    st_w = draw.textlength(status_text, font=font_value) + 30
    st_x = width - pad - st_w
    draw.ellipse([st_x, y + 16, st_x + 14, y + 30], fill=status_color)
    draw.text((st_x + 22, y + 10), status_text, font=font_value, fill=status_color)

    y += header_h + 16
    draw.line([pad, y, width - pad, y], fill=colors["divider"], width=1)
    y += 16

    # 字段行
    for label, value in rows:
        draw.text((x, y), label, font=font_label, fill=colors["sub"])
        draw.text((x + 120, y - 2), str(value), font=font_value, fill=colors["fg"])
        y += row_h

    # MOTD 区块
    if motd_lines:
        y += 8
        draw.text((x, y), "MOTD", font=font_label, fill=colors["sub"])
        y += 34
        for ln in motd_lines:
            draw.text((x + 16, y), ln, font=font_motd, fill=colors["fg"])
            y += 34
        y += 10

    # 页脚
    draw.line([pad, y, width - pad, y], fill=colors["divider"], width=1)
    footer = f"数据来源 motd.frostlynx.work · 查询时间 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    draw.text((x, y + 14), footer, font=font_footer, fill=colors["sub"])

    return img


# ---------------------------------------------------------------------------
# 指令参数解析
# ---------------------------------------------------------------------------


def resolve_default_server(config: dict) -> tuple:
    """读取并校验默认服务器配置，返回 (edition, address, port)。

    地址为空、端口越界、版本非 java/bedrock 时视为未配置，抛出 ValueError。
    """
    address = str(config.get("default_server_address", "") or "").strip()
    edition_raw = str(config.get("default_server_edition", "java") or "java").lower()
    try:
        port_val = int(config.get("default_server_port", 0) or 0)
    except (TypeError, ValueError):
        port_val = -1

    invalid = (
        not address
        or edition_raw not in DEFAULT_PORTS
        or port_val < 0
        or port_val > 65535
    )
    if invalid:
        raise ValueError(
            "尚未配置默认服务器（或配置无效）。\n"
            "请使用 /motd java <地址> [端口] 或 /motd bedrock <地址> [端口] 手动查询，"
            "或联系管理员在 WebUI 插件配置中填写默认服务器信息。"
        )
    port = port_val if port_val > 0 else DEFAULT_PORTS[edition_raw]
    return edition_raw, address, port


def format_default_address(config: dict) -> str:
    """生成 /地址 指令的回复文本（直接输出配置好的默认服务器地址）。"""
    try:
        edition, address, port = resolve_default_server(config)
    except ValueError:
        return (
            "尚未配置默认服务器地址。\n"
            "请联系管理员在 WebUI 插件配置中填写 default_server_address，"
            "或使用 /motd java <地址> 手动查询。"
        )
    label = EDITION_LABELS.get(edition, edition)
    return f"默认服务器：{address}:{port}（{label}）\n发送 /motd 即可查看实时状态。"


def parse_motd_args(args: list, config: dict) -> tuple:
    """解析 /motd 指令参数，返回 (edition, address, port)。

    支持形式：
      /motd                          -> 使用配置中的默认服务器
      /motd java <地址> [端口]
      /motd bedrock <地址> [端口]
      /motd <地址> [java|bedrock] [端口]
      /motd <地址> [端口]            -> 版本取默认配置，无效则 java
    参数无效时抛出 ValueError（携带友好提示）。
    """

    def _default_edition() -> str:
        ed = str(config.get("default_server_edition", "java") or "java").lower()
        return ed if ed in DEFAULT_PORTS else "java"

    def _parse_port(raw, default: int) -> int:
        if raw is None or raw == "":
            return default
        try:
            p = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"端口「{raw}」不是有效数字，应为 1-65535 的整数。")
        if not (1 <= p <= 65535):
            raise ValueError(f"端口 {p} 超出范围，应为 1-65535。")
        return p

    if not args:
        # 无参数：读取默认服务器配置（与 /地址 指令共用同一套校验逻辑）
        return resolve_default_server(config)

    first = args[0].lower()
    if first in DEFAULT_PORTS:
        # /motd java|bedrock <地址> [端口]
        if len(args) < 2 or not args[1].strip():
            raise ValueError(f"请提供服务器地址，例如：/motd {first} play.example.com")
        edition = first
        address = args[1].strip()
        port = _parse_port(args[2] if len(args) > 2 else None, DEFAULT_PORTS[edition])
        return edition, address, port

    # /motd <地址> [java|bedrock] [端口] 或 /motd <地址> [端口]
    address = args[0].strip()
    edition = _default_edition()
    port_raw = None
    if len(args) > 1:
        second = args[1].lower()
        if second in DEFAULT_PORTS:
            edition = second
            port_raw = args[2] if len(args) > 2 else None
        else:
            port_raw = args[1]
    port = _parse_port(port_raw, DEFAULT_PORTS[edition])
    return edition, address, port


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------


@register(
    "astrbot_plugin_mc_motd",
    "Chenzhenyan0",
    "查询 Minecraft 服务器 MOTD 状态并渲染为图片卡片",
    "v1.1.1",
    "https://github.com/Chenzhenyan0/astrbot_plugin_mc_motd",
)
class McMotdPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._session: aiohttp.ClientSession | None = None

    async def initialize(self):
        """插件加载时创建共享的 HTTP 会话。"""
        timeout = self._get_timeout()
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"User-Agent": "AstrBot-McMotd-Plugin/1.1.1"},
        )

    async def terminate(self):
        """插件卸载时关闭 HTTP 会话。"""
        if self._session and not self._session.closed:
            await self._session.close()

    # -- 内部工具 ------------------------------------------------------------

    def _get_timeout(self) -> int:
        try:
            t = int(self.config.get("request_timeout", 15) or 15)
            return t if t > 0 else 15
        except (TypeError, ValueError):
            return 15

    def _get_api_base(self) -> str:
        base = str(self.config.get("api_base_url", "") or "").strip()
        return base or DEFAULT_API_BASE

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._get_timeout()),
                headers={"User-Agent": "AstrBot-McMotd-Plugin/1.1.1"},
            )
        return self._session

    def _get_data_dir(self) -> str:
        """获取插件数据目录（优先 AstrBot data 目录，失败退回系统临时目录）。"""
        try:
            from astrbot.api.star import StarTools

            return StarTools.get_data_dir("astrbot_plugin_mc_motd")
        except Exception:  # noqa: BLE001 - 兼容不同 AstrBot 版本
            path = os.path.join(tempfile.gettempdir(), "astrbot_plugin_mc_motd")
            os.makedirs(path, exist_ok=True)
            return path

    # -- 指令 ----------------------------------------------------------------

    @filter.command("motd")
    async def motd(self, event: AstrMessageEvent):
        """查询 Minecraft 服务器 MOTD 状态。

        用法：
          /motd                          查询默认服务器
          /motd java <地址> [端口]
          /motd bedrock <地址> [端口]
          /motd <地址> [java|bedrock] [端口]
        """
        tokens = (event.message_str or "").strip().split()
        # 去掉指令本体（兼容 /motd、motd、/motd@bot 等写法）
        if tokens and tokens[0].lstrip("/").split("@")[0].lower() == "motd":
            args = tokens[1:]
        else:
            args = tokens

        # 1. 解析参数
        try:
            edition, address, port = parse_motd_args(args, self.config)
        except ValueError as e:
            yield event.plain_result(str(e))
            return

        # 2. 查询 API
        try:
            session = await self._ensure_session()
            data = await fetch_status(
                session, self._get_api_base(), edition, address, port
            )
        except MotdApiError as e:
            yield event.plain_result(f"❌ 查询失败：{e}")
            return
        except Exception as e:  # noqa: BLE001 - 兜底，避免插件因单次查询崩溃
            logger.error(
                f"[mc_motd] 查询 {edition}://{address}:{port} 出现未知错误: {e}"
            )
            yield event.plain_result("❌ 查询时出现未知错误，请稍后重试。")
            return

        # 3. 渲染图片
        try:
            img = render_status_card(
                data=data,
                edition=edition,
                address=address,
                port=port,
                theme=str(self.config.get("card_theme", "dark") or "dark"),
                width=int(self.config.get("card_width", 880) or 880),
                font_path=str(self.config.get("font_path", "") or ""),
            )
            out_path = os.path.join(
                self._get_data_dir(),
                f"motd_{edition}_{int(datetime.now().timestamp())}.png",
            )
            img.save(out_path, "PNG")
        except Exception as e:  # noqa: BLE001 - 兜底，渲染失败时回退文本提示
            logger.error(f"[mc_motd] 渲染状态卡片失败: {e}")
            yield event.plain_result("❌ 生成状态图片失败，请检查插件日志。")
            return

        yield event.image_result(out_path)

    @filter.command("地址")
    async def default_address(self, event: AstrMessageEvent):
        """输出当前配置的默认服务器地址（不发起查询）。"""
        yield event.plain_result(format_default_address(self.config))

    @filter.command("address")
    async def default_address_en(self, event: AstrMessageEvent):
        """输出当前配置的默认服务器地址（英文指令别名）。"""
        yield event.plain_result(format_default_address(self.config))
