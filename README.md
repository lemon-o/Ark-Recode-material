# Ark Recode Material

《Ark Re:Code》新团员首发头像的自动同步仓库。抓取全程无人值守，新角色上线后会自动出现在 `avatars/` 里。

## 图从哪来

**不是** Wiki 上的那版。Wiki 的 `Icon_Head_S_H*.png` 虽然也是 128×128，但像素和游戏原图不一样（约四成像素有差异，最大色差上百），当素材用会糊。

这里用的是游戏自己发布的补丁 CDN：走 WebGL 客户端同一条链路（bootstrap 拿补丁域名 → 拉 Addressables catalog → 用 UnityPy 从 AssetBundle 里解出原图）。

抓取脚本来自 [lucima-tools](https://github.com/lemon-o/lucima-tools) 的 `tools/`。运行时不复制进本仓库，而是整仓 clone 过来执行 —— 因为 `fetch_assets.py` 要用 ast 读那边的 `backend/config.py` 拿游戏地址，`watch_new_units.py` 又要靠自身位置定位它。整仓带上，这两个依赖自然满足，也就不用维护第二份副本来对齐。

## 怎么判断「新」

每次运行扫一遍 Wiki 的 [Events/Banners](https://arkrecodewiki.miraheze.org/wiki/Events/Banners) 及其全部转写子页，取 Notes 列为 `Unit Debut` 的团员；再拿本仓库 `avatars/` 里已有的 PNG 做差集，只抓缺的那几张。所以：

- 本仓库的 PNG 就是「已知」集合，不需要额外的状态文件；
- 没有缺口时脚本连 CDN 都不会碰，一次运行只访问 Wiki。

日期还没定的角色（Wiki 上标着 `?`）不会被记录，等它定档后自然会再被扫出来。

## 目录

    avatars/     头像 PNG，文件名即角色 ID（H193.png）
    index.json   清单：ID -> 文件名 / 字节数 / sha256
    tools/       本仓库自有的小工具（目前只有生成清单这一个）

## 客户端怎么用

拉 `index.json`，跟本地已有的 ID 比对，缺的从 CDN 取：

    index   https://cdn.jsdelivr.net/gh/lemon-o/Ark-Recode-material@main/index.json
    avatar  https://cdn.jsdelivr.net/gh/lemon-o/Ark-Recode-material@main/avatars/<ID>.png

单张 20KB 上下。

**为什么走 jsDelivr 而不是 raw.githubusercontent.com**：后者在中国大陆经常连不上，而这套东西的消费方包括手机端。jsDelivr 在境内有节点，同一个文件两个地址都可用，客户端按顺序回退即可。

## 手动触发 / 本地复现

Actions 页 -> Sync Avatars -> Run workflow，勾 `dry_run` 只扫描不抓取。

本地跑一遍（需要 Python 3.12 与 `pip install httpx UnityPy`）：

    git clone https://github.com/lemon-o/lucima-tools _lucima
    python _lucima/tools/watch_new_units.py --target . --include-existing
    python tools/build_index.py

首次运行会把 Wiki 上已发布的团员一次性补齐（`--include-existing` 加空仓库 = 全量 backfill），之后每次只增量抓新增的。
