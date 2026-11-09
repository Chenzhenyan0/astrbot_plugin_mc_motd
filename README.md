# astrbot_plugin_mc_motd

AstrBot Minecraft MOTD 查询插件：通过指令查询 Minecraft 服务器（Java 版 / 基岩版）的 MOTD 状态，并将结果**渲染为图片卡片**发送到聊天中。

## 功能

- `/motd` —— 无参数时查询 WebUI 配置中的**默认服务器**
- `/地址`（别名 `/address`）—— 直接输出当前配置的**默认服务器地址**（不发起查询，纯文本）
- `/motd java <地址> [端口]` —— 查询 Java 版服务器（默认端口 25565）
- `/motd bedrock <地址> [端口]` —— 查询基岩版服务器（默认端口 19132）
- `/motd <地址> [java|bedrock] [端口]` —— 统一写法，版本可通过参数指定（缺省取默认配置）
- `/motd <地址> <端口>` —— 仅指定端口，版本取默认配置

`/地址` 输出示例：

```
默认服务器：play.example.com:25565（Java 版）
发送 /motd 即可查看实时状态。
```

未配置或配置无效（地址为空、端口越界、版本非 `java`/`bedrock`）时，`/地址` 会提示前往 WebUI 配置。

图片卡片中展示：服务器地址与端口、在线状态、版本（含协议号）、在线/最大玩家数（含在线玩家名单）、延迟、游戏模式（基岩版）、清理颜色代码后的 MOTD 文本、服务器 favicon（如有）。

## 实现方案

- 状态数据来源：[motd.frostlynx.work](https://motd.frostlynx.work/api.html) 的即时状态接口 `GET /v1/status/java|bedrock/{address}`（JSON）。
- 图片输出：该 API 没有直接生成"即时 MOTD 卡片"的接口，`/v1/subscriptions/chart.png` 依赖需管理员验证码的订阅机制，插件无法自动化。因此本插件采用**本地渲染方案**：使用 Pillow 将 API 返回的 JSON 绘制成信息卡片 PNG，再通过 `event.image_result()` 发送，不依赖任何订阅流程。

## 配置（WebUI 插件配置页）

| 配置项 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `default_server_address` | string | （空） | 默认服务器地址，留空表示未配置 |
| `default_server_port` | int | 0 | 默认端口，0 时按版本取 25565 / 19132 |
| `default_server_edition` | string | java | 默认版本：`java` / `bedrock` |
| `api_base_url` | string | `https://motd.frostlynx.work/v1` | MOTD API 基础地址 |
| `request_timeout` | int | 15 | 请求超时（秒） |
| `card_theme` | string | dark | 卡片主题：`dark` / `light` |
| `card_width` | int | 880 | 卡片宽度（像素，600-1600） |
| `font_path` | string | （空） | 自定义中文字体路径；MOTD 中文显示为方块时填写 |

## 依赖

见 `requirements.txt`：`aiohttp`、`pillow`。

## 注意

- 即时状态接口默认限流 10 次/分钟，频繁查询会收到友好提示。
- 地址无效、端口越界（1-65535）、默认版本非 `java`/`bedrock` 时均视为配置无效并给出引导提示。
- 插件生成的临时图片保存在 AstrBot `data` 目录下的插件数据目录中，不会写入插件自身目录。
