# Ark Recode Material

《Ark Re:Code》游戏素材的自动同步仓库。GitHub Actions 定时抓取，无人值守，游戏上新后新素材会自动出现在对应目录里（当前收角色头像、头像框、技能图标、功能图标、道具/装备/套装图标、赛季框、卡池横幅）。

<!-- SYNC-STATS:START -->
<!-- 本块由 sync-avatars 工作流自动更新，请勿手改 -->
- 素材总数：3717（头像 228 / 头像框 1051 / 技能图标 689 / 功能图标 34 / 道具图标 800 / 装备图标 609 / 套装图标 17 / 赛季框 149 / 卡池横幅 140）
- 最近扫描：2026-09-25T09:17:12+08:00（北京时间）
- 清单：`https://cdn.jsdelivr.net/gh/lemon-o/Ark-Recode-material@main/index.json`
<!-- SYNC-STATS:END -->

## 实现原理

整条链路只有两步：**从游戏自己的补丁 CDN 解出原图**，再**用 catalog 发现新资源**。

### 1. 图从哪来：补丁 CDN + UnityPy

**不是** Wiki 上的那版。Wiki 的 `Icon_Head_S_H*.png` 虽然也是 128×128，但像素和游戏原图不一样（约四成像素有差异，最大色差上百），当素材用会糊。

抓图走游戏客户端完全相同的 HTTP 路径：

1. `PUT` 游戏路由，`GameServerDBSettingHandler.QueryBulletinInfoResult`（免登录的 bootstrap 调用）→ 拿到补丁域 `PathDomain`、路径前缀 `PatchPosFix` 与目录名 `NewCatalogName`；
2. `GET {PathDomain}{PatchPosFix}/WebGL/{NewCatalogName}.json` —— Addressables content catalog（几十 MB，本地缓存，命中后不重复下载）；
3. 解码 catalog 里的 `m_KeyDataString` / `m_BucketDataString` / `m_EntryDataString` 三个 base64 块，得到 `address -> AssetBundle URL` 映射；
4. 按需下载 bundle，用 UnityPy 从中解出 `Texture2D` / `Sprite` 写成 PNG（新版头像被 Crunch 压成 1024x1024 图集，小头像要按 Sprite 裁剪，脚本会在两者之间自动回退）。

### 2. 怎么发现「新」：读 catalog，不查 Wiki

Addressables 的 **address 就是资源路径**，而路径由和数据表同一套 ID 拼成
（`Assets/Game/Hero/H193/Img/Icon_Head_S_H193.png`）。新角色上线 = catalog 里多出对应条目，于是：

- 真相来源是游戏自己的 CDN，**不需要维护任何映射表**，也不依赖第三方 Wiki；
- 仓库里已有的 PNG 就是「已知」集合，只抓缺的（脚本**只写不删**）；
- 没有缺口时连 bundle 都不会下载，一次 run 零 CDN 请求。

**为什么不走 Wiki**：最初的设计是扫 miraheze 的 Events/Banners、取 Notes 列为 `Unit Debut` 的名单。实测 miraheze 挂在 Cloudflare 后面，对 GitHub Actions 的数据中心 IP **一律返回 403** —— 试过 7 种 UA 与端点组合（含 `api.php`、`rest.php`、带完整浏览器头），271KB 的挑战页全灭。catalog 这条路既更权威，也少一个依赖。

代价是范围变宽：这里收的是**全部角色头像**，而不只是 Wiki 上有 Unit Debut 记录的那些。

### 3. 资产 ID 不等于角色 ID：`AVATAR_ID_REMAP`

极少数头像的资产路径 ID 和角色表 ID 对不上：游戏 catalog 里 `Assets/Game/Hero/H801/...` 装的其实是角色 **H804** 的图，而 `H804` 自己那条 asset 的图反而匹配不上任何已登记角色（实测逐像素比对确认）。

`tools/sync_assets.py` 里的 `AVATAR_ID_REMAP`（资产 ID -> 角色 ID）负责翻译，重映射条目**优先于**同名的原始条目——否则 `H804 -> H804.png` 会把已经落位的蜜娜顶掉。

```
remapped: H801 -> H804.png; skipped, output already taken: H804 -> H804.png
```

这张表必须可读：**读不到不是报错，而是静默跳过重映射**——图会按资产 ID 原样输出，仓库看起来一切正常，实际发出去一张废图和一张错图（2026-09-17 实际翻过车）。所以 workflow 里有一步「校验头像 ID 重映射表可读」，表缺失或为空直接红。

### 4. `index.json`：客户端判缺的依据

每次产出后由 `tools/build_index.py` 重建：

```json
{
  "updated": "2026-09-17T13:47:42+08:00",
  "count": 261,
  "avatars": {
    "H804": {"file": "H804.png", "bytes": 26114, "sha256": "9436de16..."}
  }
}
```

时间戳统一东八区（`+08:00`）。只由图片本身决定内容，集合没变就不产生 diff（不制造无意义提交）。`updated` 时间戳是新鲜度信号：缓存渠道可能滞后，客户端应当取 `updated` 最新的那份索引，**而不是条目最多的那份**——上游删掉一个 id 后，旧缓存反而条目更多。

### 5. 分发：jsDelivr + raw 双源

- `https://cdn.jsdelivr.net/gh/lemon-o/Ark-Recode-material@main/index.json`
- `https://cdn.jsdelivr.net/gh/lemon-o/Ark-Recode-material@main/avatars/<ID>.png`

单张 20KB 上下。jsDelivr 境内有节点、国内直连可用，但对**分支引用**有约 12 小时缓存；`raw.githubusercontent.com` 更实时却在国内不稳。两个地址都保留、按顺序回退最稳。客户端拿到文件后用 `index.json` 里的 `sha256` 校验，不符就换下一个源。

### 6. 18+ 防线：白名单，不是黑名单

catalog 里 7000+ 张 PNG 大半是 CG / 立绘 / 羁绊图（`*_Sex_LoveTalk.png`、`Assets/Game/CG/A*/` 羁绊 CG、`Hero/<ID>/<ID>/CG_*.png`），那些是 18+ 内容。脚本用**白名单**收图：只收文件名以 `Icon` 开头、且路径落在 `Hero/*/Img` 或 `Assets/Game/Icon/` 下的资产；卡池横幅单独走 `Assets/Game/Banner/BN_Summon_*` 白名单前缀，同一目录树下的**活动宣传图 `BN_Activity_*` 不收**（那是运营物料，不是卡池横幅）。CG 与羁绊图从来不叫这些名字，白名单结构上就碰不到它们。新增家族时必须维持这条纪律。

## 目录

    avatars/     角色头像（Icon_Head_S_<ID>，文件名即角色 ID，H193.png）
    heads/       三种尺寸头像框（Icon_Head_{L,M,S}_<ID>.png，B=大 不收）
    skills/      技能图标（Icon_Skill_<ID>_<nnn>.png）
    icons/       功能/星座图标（Assets/Game/Icon/{Function,Constellation}）
    items/       道具图标（Item.spriteatlas 里的 Sprite，按游戏图标键命名，如 45StarHeroTicket）
    equip/       装备图标（Equip.spriteatlas，名字去掉下划线以对齐应用约定，E001_1 -> E0011）
    equipset/    套装图标（EquipSet.spriteatlas，Attack / Critical / ...）
    uiconz/      赛季头像框（UI_iconz.spriteatlas，如 Frame_031）
    banners/     卡池横幅（Banner/BN_Summon_NNN 每池一个 bundle，512x256 简中图，
                 命名 BN_Summon_H804.png；卡池通用图如 MultiSummonMystic 同目录）
    index.json   清单：分区 -> 键 -> 文件名 / 字节数 / sha256
    tools/       fetch_assets.py（抓图）、build_index.py（生成清单）、update_readme.py（回写「当前状态」）、sync_assets.py（供读取 AVATAR_ID_REMAP，在此仓库从不执行）
    backend/     config.py：抓图脚本要用的四个端点常量（GAME_ROUTER / GAME_ORIGIN / GAME_REFERER / HTTP_TIMEOUT）

自包含：Actions 只 checkout 本仓库，不 clone 任何其它仓库、不需要任何 PAT。

## 手动触发 / 本地复现

Actions 页 -> Sync Avatars -> Run workflow，勾 `dry_run` 只扫描不抓取。

本地跑一遍（需要 Python 3.12 与 `pip install httpx UnityPy`）：

    python tools/fetch_assets.py --only avatars,heads,skills,icons,atlas,banner,data --target .
    python tools/build_index.py

首次运行会把 catalog 里的白名单素材一次性补齐（~2100 张），之后每次只增量抓新增的。
