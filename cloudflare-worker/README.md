# Cloudflare Worker: Discord 官方公告秒级联动网关

本目录包含用于将 Discord 官方公告即时联动至 GitHub Actions 的 Cloudflare Worker 代码。

部署后，Worker 会在云端全天候运行（Cloudflare 免费额度每日 100,000 次请求，完全免费），每 5 分钟轻量查询一次你的 Discord `#综合` 频道。一旦发现官方发布了新维护或招募公告，**毫秒级唤醒 GitHub Actions**，摆脱固定星期几的限制。

---

## 快速部署指南（3 分钟完成）

### 步骤 1：创建 Worker
1. 登录 [Cloudflare Dashboard](https://dash.cloudflare.com/)；
2. 点击左侧导航栏的 **Workers & Pages** -> **Create application** -> **Create Worker**；
3. 为 Worker 起一个名字（如 `ark-recode-bridge`），点击 **Deploy** 完成初始创建；
4. 点击 **Edit code** 进入网页代码编辑器，清空原有内容，将本目录下的 `worker.js` 完整内容复制粘贴进去，点击右上角 **Deploy** 保存。

---

### 步骤 2：配置环境变量与密钥
进入该 Worker 详情页 -> 点击 **Settings** -> **Variables and Secrets**，点击 **Add** 配置以下 4 个变量：

| 变量名称 | 类型 | 说明 / 默认值 |
| :--- | :--- | :--- |
| `DISCORD_BOT_TOKEN` | **Secret (加密)** | 你的 Discord Bot Token（填入在 Discord Developer Portal 生成的 Token） |
| `GITHUB_PAT` | **Secret (加密)** | 你的 GitHub Personal Access Token（需要包含 `repo` 权限） |
| `DISCORD_CHANNEL_ID` | Text (明文) | `#综合` 频道 ID：`1573590290424401932` |
| `GITHUB_REPO` | Text (明文) | `lemon-o/Ark-Recode-material` |

> 如何生成 `GITHUB_PAT`：
> 1. 打开 GitHub -> 右上角头像 -> Settings -> Developer Settings -> Personal access tokens -> Tokens (classic)；
> 2. 点击 Generate new token (classic)，勾选 `repo` 与 `workflow` 权限，生成并复制该 Token。

---

### 步骤 3：配置定时触发器（Cron Trigger）
1. 在 Worker 详情页，点击 **Settings** -> **Triggers**；
2. 找到 **Cron Triggers**，点击 **Add Cron Trigger**；
3. 填入 Cron 表达式：`*/5 * * * *`（每 5 分钟执行一次检测），点击 **Save**。

---

### 步骤 4：测试运行
在浏览器中直接打开你的 Worker 域名加上 `/check`，例如：
`https://ark-recode-bridge.<你的子域>.workers.dev/check`

页面会立即输出 JSON 格式的执行结果：
- 若检测到新公告，会输出：`[OK] 成功唤醒 GitHub Actions! 公告 ID: ...`；
- 若近期无新公告或已处理过，会输出：`公告之前已触发过同步，跳过重复调度`。
