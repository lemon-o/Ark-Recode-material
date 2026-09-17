"""抓图脚本需要的最小配置面。

`tools/fetch_assets.py` 的 `_config_constants()` 会用 ast 解析本文件，取出
下面这几个游戏端点常量 —— 这样端点定义只有一处，不必写死在抓图脚本里。

这份是 lucima-tools 的 `backend/config.py` 的精简快照：完整那份还带有代理
模式解析、settings 迁移等逻辑，抓图用不到，也不该出现在这里。
"""

GAME_ROUTER = "https://game-arkre-labs.ecchi.xxx/Router/RouterHandler.ashx"
GAME_ORIGIN = "https://game-arkre-labs.ecchi.xxx"
GAME_REFERER = "https://game-arkre-labs.ecchi.xxx/WebGL/index.html"

HTTP_TIMEOUT = 30.0
