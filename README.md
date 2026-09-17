# Ark Recode Material

《Ark Re:Code》角色头像的自动同步仓库。抓取无人值守，新角色上线后会自动出现在 `avatars/` 里。

## 图从哪来

**不是** Wiki 上的那版。Wiki 的 `Icon_Head_S_H*.png` 虽然也是 128×128，但像素和游戏原图不一样（约四成像素有差异，最大色差上百），当素材用会糊。

这里走游戏自己发布的补丁 CDN：bootstrap 拿补丁域名 → 拉 Addressables content catalog → 用 UnityPy 从 AssetBundle 里解出原图。

## 怎么发现「新」

**不查 Wiki，直接读 catalog。**

catalog 是游戏客户端自己用来定位资源的索引，每一项的 address 就等于资源路径（`Assets/Game/Hero/H193/Img/Icon_Head_S_H193.png`）。新角色上线 = catalog 里多出对应条目。于是：

- 真相来源是游戏自己的 CDN，不需要维护任何映射表，也不依赖第三方 Wiki；
- 本仓库已有的 PNG 就是「已知」集合，只抓缺的（脚本只写不删）；
- 没有缺口时连 bundle 都不会下载。

**为什么不走 Wiki**：最初的设计是扫 miraheze 的 Events/Banners、取 Notes 列为 `Unit Debut` 的名单。实测 miraheze 挂在 Cloudflare 后面，对 GitHub Actions 的数据中心 IP **一律返回 403** —— 试过 7 种 UA 与端点组合（含 `api.php`、`rest.php`、带完整浏览器头），271KB 的挑战页全灭。catalog 这条路既更权威，也少一个依赖。

代价是范围变宽：这里收的是**全部角色头像**，而不只是 Wiki 上有 Unit Debut 记录的那些。

## 脚本来源（快照，会漂移）

`tools/` 与 `backend/config.py` 是从 [lucima-tools](https://github.com/lemon-o/lucima-tools) 同步过来的**快照**。

之所以用快照：lucima-tools 是私有仓库，本仓库的 Actions 默认 token 只能访问自己，跨仓库取脚本就得挂一个会过期的 PAT。抓图脚本改动不频繁，快照更省事。

**代价是漂移** —— 主仓库改了脚本，这里不会自动跟上。同步方式：

    cp ../lucima-tools/tools/fetch_assets.py tools/
    cp ../lucima-tools/backend/config.py backend/config.py

`backend/config.py` 是必需的：`fetch_assets.py` 的 `_config_constants()` 会用 ast 解析它取 `GAME_ROUTER` / `GAME_ORIGIN` / `GAME_REFERER` / `HTTP_TIMEOUT` 四个常量。

主仓库的 `tools/watch_new_units.py` **没有同步过来** —— 它依赖 miraheze，在 Actions 里必然 403，留在这儿是死代码。

## 目录

    avatars/     头像 PNG，文件名即角色 ID（H193.png）
    index.json   清单：ID -> 文件名 / 字节数 / sha256
    tools/       fetch_assets.py（抓图）、build_index.py（生成清单）
    backend/     仅 config.py，供抓图脚本读取端点常量

## 客户端怎么用

拉 `index.json`，跟本地已有的 ID 比对，缺的从 CDN 取：

    index   https://cdn.jsdelivr.net/gh/lemon-o/Ark-Recode-material@main/index.json
    avatar  https://cdn.jsdelivr.net/gh/lemon-o/Ark-Recode-material@main/avatars/<ID>.png

单张 20KB 上下。

**为什么走 jsDelivr 而不是 raw.githubusercontent.com**：后者在中国大陆的连通性不稳定，而这套东西的消费方包括手机端。jsDelivr 境内有节点，两个地址都保留、按顺序回退最稳。

## 手动触发 / 本地复现

Actions 页 -> Sync Avatars -> Run workflow，勾 `dry_run` 只扫描不抓取。

本地跑一遍（需要 Python 3.12 与 `pip install httpx UnityPy`）：

    python tools/fetch_assets.py --only avatars --target .
    python tools/build_index.py

首次运行会把 catalog 里的头像一次性补齐，之后每次只增量抓新增的。
