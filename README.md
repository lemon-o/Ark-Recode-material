# Ark Recode Material

《Ark Re:Code》游戏素材的自动同步仓库。GitHub Actions 定时抓取，无人值守，游戏上新后新素材会自动出现在对应目录里（当前收角色头像、头像框、技能图标、功能图标、道具/装备/套装图标、赛季框、卡池横幅、羁绊小图、背景图鉴、Spine骨骼立绘）。

<!-- SYNC-STATS:START -->
<!-- 本块由 sync-avatars 工作流自动更新，请勿手改 -->
- 素材总数：5198（头像 269 / 头像框 1217 / 技能图标 812 / 功能图标 34 / 道具图标 958 / 装备图标 650 / 套装图标 17 / 赛季框 150 / 卡池横幅 345 / 羁绊小图 270 / 背景图鉴 244 / Spine立绘 232）
- 最近扫描：2026-11-25T17:02:27+08:00（北京时间）
- 清单：`https://cdn.jsdelivr.net/gh/lemon-o/Ark-Recode-material@main/index.json`
<!-- SYNC-STATS:END -->

### 当前招募活动动态看板 (Active Summon Pools)

<!-- CURRENT-POOLS:START -->
<!-- 本看板联动官方维护公告与卡池配置自动生成，请勿手动修改 -->
> **当前招募活动窗口**：`11/25 14:00 - 17:00 (UTC+8)` | **更新类型**：常规停机维护
> **实时接口**：[`data/current-pools.json`](https://cdn.jsdelivr.net/gh/lemon-o/Ark-Recode-material@main/data/current-pools.json)

| 卡池横幅 | 招募类型 | 专属羁绊 | 开放时间 |
| :---: | :---: | :---: | :---: |
| [![Banner](https://cdn.jsdelivr.net/gh/lemon-o/Ark-Recode-material@main/banners/BN_Summon_H180.png)](https://cdn.jsdelivr.net/gh/lemon-o/Ark-Recode-material@main/banners/BN_Summon_H180.png) | **限定招募** | <img src="https://cdn.jsdelivr.net/gh/lemon-o/Ark-Recode-material@main/bonds/A0356.png" width="28" height="28" align="center" /> 「跃动的烈情」 | 2026/11/18 08:00 - 2026/12/02 08:00<br><sub>(持续 2周)</sub> |
| [![Banner](https://cdn.jsdelivr.net/gh/lemon-o/Ark-Recode-material@main/banners/BN_Summon_H016.png)](https://cdn.jsdelivr.net/gh/lemon-o/Ark-Recode-material@main/banners/BN_Summon_H016.png) | **常规招募** | <img src="https://cdn.jsdelivr.net/gh/lemon-o/Ark-Recode-material@main/bonds/A0012.png" width="28" height="28" align="center" /> 「拘束与放荡」 | 2026/11/18 08:00 - 2026/12/02 08:00<br><sub>(持续 2周)</sub> |
| [![Banner](https://cdn.jsdelivr.net/gh/lemon-o/Ark-Recode-material@main/banners/BN_Summon_H199.png)](https://cdn.jsdelivr.net/gh/lemon-o/Ark-Recode-material@main/banners/BN_Summon_H199.png) | **限定招募 (首发)** | <img src="https://cdn.jsdelivr.net/gh/lemon-o/Ark-Recode-material@main/bonds/A0455.png" width="28" height="28" align="center" /> 「孤独的再遇」 | 2026/11/25 17:00 - 2026/12/09 14:00<br><sub>(持续 2周)</sub> |
| [![Banner](https://cdn.jsdelivr.net/gh/lemon-o/Ark-Recode-material@main/banners/BN_Summon_MultiSummonMystic.png)](https://cdn.jsdelivr.net/gh/lemon-o/Ark-Recode-material@main/banners/BN_Summon_MultiSummonMystic.png) | **神秘招募 (自选)** | — (自选1名团员) | 2026/11/25 17:00 - 2026/12/16 14:00<br><sub>(持续 3周)</sub> |
<!-- CURRENT-POOLS:END -->

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

catalog 里 7000+ 张 PNG 大半是 CG / 羁绊图（*_Sex_LoveTalk.png、Assets/Game/CG/A*/IMG_* 羁绊原大图、Hero/<ID>/<ID>/CG_*.png），那些是 18+ 内容。脚本用**白名单**收图：只收文件名以 Icon 开头、且路径落在 Hero/Img 或 Icon/ 下的合规资产；羁绊小图仅收录 128x128 的方块头像图标（Assets/Game/CG/<ID>/Img/Icon_Head_S_* 与 Icon_Carnival###_S），原大图 CG（IMG_*）、长条横幅（Icon_Head_M_*）与剧情封面（*_LoveTalk）一律不收；卡池横幅单独走 Assets/Game/Banner/BN_Summon_* 白名单前缀，同一目录树下的**活动宣传图 BN_Activity_* 不收**（那是运营物料，不是卡池横幅）。新增家族时必须维持这条纪律。

### 7. Spine 骨骼立绘与合规安全准则

游戏客户端的完整角色立绘在官方 CDN 中并非现成的单张静态全画幅图片，而是基于 Spine 骨骼运行库在运行时动态拼装渲染生成。

- **原始散件三件套收录**：仓库仅且必须收录原始骨骼切片三件套（`.skel` 骨骼动画二进制数据、`.atlas` 图集切片坐标、`.png` 散装零件切片贴图）。绝不收录组装渲染后的完整裸图，利用 Atlas 散装碎图的天然解构特性彻底规避平台机器视觉审核风控；
- **统一 ID 命名契约**：所有角色立绘统一以角色 ID 归一化命名，目录格式为 `spine/<ID>/<ID>.skel|.atlas|.png`。对于游戏早期少数内部资产命名不一致的角色（如 `H001` 内部为 `CG_H001_a`），提取脚本会在导出时自动将 `.atlas` 首行的贴图文件名重写对齐为 `<ID>.png`，下游 Spine Web Player / Pixi-Spine 播放器直接按 ID 拼接 URL 即可加载，无需额外查表；
- **增量更新机制**：已存在完整三件套的角色在后续扫描中会自动跳过，每周维护仅针对新增角色执行拉取与解包。

### 8. 角色档案与正式上线状态判定：`data/heroes.json`

游戏补丁包与 Addressables catalog 中通常包含大量提前打包的「拆包预埋角色」（如未来节日限定、未开放活动的卫星角色）。为了让下游应用与页面能够精准区分「官方已正式上线角色」与「拆包未上线角色」，工作流集成了 `tools/build_heroes.py`，每次更新自动从官方 StaticData 与多语言文本表构建 `data/heroes.json`：

- **判定标准**：游戏内已开放上线的角色必定在好感度台词签名表（`RoleSignature.txt`）中登记互动内容；拆包预埋角色在签名表中无条目。以此作为区分正式上线（`is_released: true`）与未上线拆包（`is_released: false`）的黄金标准；
- **多语言与属性元数据**：提取简中（`name_chs`）、繁中（`name_cht`）、英文（`name_eng`）、日文（`name_jpn`）四语官方名称，以及星级（`star`）、属性（`element`）、职业（`job`）；
- **素材可用性标记**：包含 `has_spine`（Spine 骨骼立绘是否存在）、`has_avatar`（头像是否存在）、`has_gacha`（卡池是否有产出）、`has_collection`（是否纳入图鉴）等状态；
- **全自动同步**：每次 GitHub Actions 跑更新时全自动重构并推送，通过 jsDelivr CDN 全球分发：
  `https://cdn.jsdelivr.net/gh/lemon-o/Ark-Recode-material@main/data/heroes.json`

### 9. 维护公告与卡池情报：`data/announcement.json`

供下游 Wiki、网页前端、抽卡模拟器与 Discord 机器人即时获取官方最新维护状态、开服时间窗口与本期 UP 角色：

- **结构化时间窗口**：包含原始文本（`maintenance.window`）、标准 ISO 时间（`start_time`, `end_time`）及时间戳（秒），便于下游倒计时组件直接消费；
- **本期招募角色情报**：`heroes.pickup`（限定角色）、`heroes.mystic`（神秘招募角色）、`heroes.rerun`（复刻角色），每位团员附带唯一 ID、四语名称、星级属性与直链头像 URL；
- **更新类型识别**：`is_hot_reload` 区分不停机活动热更新与停机大维护；
- **原始全文备份**：`content` 包含官方发布的完整 Markdown 公告文本；
- **CDN 全球分发**：
  `https://cdn.jsdelivr.net/gh/lemon-o/Ark-Recode-material@main/data/announcement.json`

### 10. 活跃卡池数据与动态看板：`data/current-pools.json`

由 `tools/build_current_pools.py` 自动联动官方最新维护公告、游戏数据表与卡池配置生成，聚合当前正在开放中的各招募卡池完整数据：

- **核心角色与专属羁绊联动**：将 UP 角色（唯一 ID、多语言名称、星级、属性、职业、直链头像）与其专属羁绊（羁绊 ID、多语言名称、小图直链）精准配对；
- **横幅与保底机制**：包含卡池横幅高清直链（`BN_Summon_<HID>.png`）与保底上限（限定招募 120 抽、神秘招募 200 抽）；
- **时间与开放周期**：包含维护后生效时间戳与开放持续周期（如持续 2 周）；
- **README 动态看板**：卡池状态会自动渲染并嵌入在 README 顶部看板表格中；
- **CDN 全球分发**：
  `https://cdn.jsdelivr.net/gh/lemon-o/Ark-Recode-material@main/data/current-pools.json`

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
    bonds/       羁绊小图（128x128 方块头像，同时兼容 ID 命名 A0413.png 与官方名 Icon_Head_S_A0413.png）
    bg/          背景图鉴（BG.spriteatlas 里的切片，包含场景环境缩略图 *_Dialog 与图鉴道具小图标 Carnival###）
    videos/      招募/抽卡动画（videos/gacha/ 下收录裁切去除侧边栏与悬浮窗的出蓝/出紫/出金/出红视频）
    spine/       全角色 Spine 骨骼立绘三件套（spine/<ID>/<ID>.{skel,atlas,png}）
    data/        静态数据与元数据（announcement.json 官方维护与卡池公告、current-pools.json 活跃卡池情报、heroes.json 全角色档案、bonds.json 羁绊表、item-meta.json 道具表、summon-pools.json 卡池表）
    index.json   清单：分区 -> 键 -> 文件名 / 字节数 / sha256
    tools/       sync_smart.py（智能调度与定向抓取）、fetch_assets.py（抓小图标）、fetch_spine.py（抓 Spine 骨骼立绘）、build_heroes.py（构建角色档案）、build_current_pools.py（构建活跃卡池情报与看板）、build_index.py（生成清单）、update_readme.py（回写统计信息）、check_version.py（Catalog 版本探测与维护公告检查）、sync_assets.py（供读取 AVATAR_ID_REMAP）
    backend/     config.py：抓图脚本要用的四个端点常量（GAME_ROUTER / GAME_ORIGIN / GAME_REFERER / HTTP_TIMEOUT）

自包含：Actions 只 checkout 本仓库，不 clone 任何其它仓库、不需要任何 PAT。

## 手动触发 / 本地复现

- **GitHub 端执行（标准流程）**：
  进入仓库 Actions 页 -> 选择 Sync Assets -> Run workflow。勾选 `force` 可跳过版本对比强制全量扫描，勾选 `dry_run` 只扫描不写入。
  依据 `AGENTS.md` 规范，所有素材抓取与批量更新优先在 GitHub Actions 云端 Linux Runner 执行并落库，本地环境再通过 `git pull` 同步。

- **本地微量验证（仅用于脚本调试）**：
  需要 Python 3.12 与 `pip install httpx UnityPy`。严禁在本地进行全量抓取并向远端推送大体积素材提交，本地仅通过指定 ID 进行抽样验证：

    # 抽样提取单个角色 Spine 立绘以验证逻辑
    python tools/fetch_spine.py --ids H195 --cache "Asset Cache"
    # 提取常规小图标素材
    python tools/fetch_assets.py --only avatars,heads,skills,icons,atlas,banner,data,bonds --target .
    python tools/build_index.py
