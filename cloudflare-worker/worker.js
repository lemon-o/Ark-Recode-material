/**
 * Cloudflare Worker: Discord 官方公告监听 -> GitHub Actions 自动联动网关
 *
 * 核心机制：
 * 1. 定时（Cron Trigger，如每 5 分钟）轻量查询 Discord 目标频道（#综合）；
 * 2. 深度提取最新消息（兼容原生文本、转发快照 message_snapshots 及富文本 Embeds）；
 * 3. 识别出维护或招募公告后，校验是否为最新未处理公告；
 * 4. 自动调用 GitHub API (repository_dispatch) 唤醒云端 Runner，将公告文本秒级注入构建流程；
 * 5. 支持直接在浏览器访问 Worker URL（如 /check）进行手动检测与状态排查。
 */

// 实例内存缓存，防止短时间内高频重复请求
let memoryLastProcessedId = null;

export default {
  // 1. 定时任务触发（Cron Trigger）
  async scheduled(event, env, ctx) {
    if (ctx && ctx.waitUntil) {
      ctx.waitUntil(checkAndDispatch(env, ctx, "https://internal.worker"));
    } else {
      await checkAndDispatch(env, ctx, "https://internal.worker");
    }
  },

  // 2. HTTP 路由（供手动测试或查看当前监听状态）
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    try {
      if (url.pathname === "/" || url.pathname === "/check") {
        const result = await checkAndDispatch(env, ctx, url.origin);
        return new Response(JSON.stringify(result, null, 2), {
          status: 200,
          headers: {
            "Content-Type": "application/json; charset=utf-8",
            "Access-Control-Allow-Origin": "*",
          },
        });
      }

      return new Response(
        JSON.stringify({
          status: "online",
          service: "Ark-Recode Discord Announcement Bridge",
          usage: "访问 /check 进行即时检测与触发",
        }, null, 2),
        {
          status: 200,
          headers: { "Content-Type": "application/json; charset=utf-8" },
        }
      );
    } catch (err) {
      return new Response(
        JSON.stringify({
          status: "exception",
          error: err.message,
          stack: err.stack,
        }, null, 2),
        {
          status: 500,
          headers: { "Content-Type": "application/json; charset=utf-8" },
        }
      );
    }
  },
};

/**
 * 提取 Discord 消息正文（兼容原生文本、转发快照 message_snapshots 及 Embeds）
 */
function extractMessageContent(msg) {
  const parts = [];
  if (msg.content && msg.content.trim()) {
    parts.push(msg.content.trim());
  }

  // 兼容 Discord 原生消息转发快照 (Forwarded Message)
  if (Array.isArray(msg.message_snapshots)) {
    for (const snap of msg.message_snapshots) {
      const sMsg = snap.message || {};
      if (sMsg.content && sMsg.content.trim()) {
        parts.push(sMsg.content.trim());
      }
      if (Array.isArray(sMsg.embeds)) {
        for (const emb of sMsg.embeds) {
          if (emb.title) parts.push(emb.title.trim());
          if (emb.description) parts.push(emb.description.trim());
        }
      }
    }
  }

  // 兼容原生 Embeds
  if (Array.isArray(msg.embeds)) {
    for (const emb of msg.embeds) {
      if (emb.title) parts.push(emb.title.trim());
      if (emb.description) parts.push(emb.description.trim());
    }
  }

  return parts.join("\n\n");
}

/**
 * 核心业务：读取 Discord、判重并调度 GitHub Actions
 */
async function checkAndDispatch(env, ctx, origin) {
  try {
    const botToken = env.DISCORD_BOT_TOKEN;
    const channelId = env.DISCORD_CHANNEL_ID || "1573590290424401932";
    const githubRepo = env.GITHUB_REPO || "lemon-o/Ark-Recode-material";
    const githubToken = env.GITHUB_PAT || env.GITHUB_TOKEN;

    if (!botToken) {
      return {
        status: "error",
        message: "缺少环境变量 DISCORD_BOT_TOKEN（请在 Settings -> Variables 中添加）",
      };
    }

    if (!githubToken) {
      return {
        status: "error",
        message: "缺少环境变量 GITHUB_PAT（请在 Settings -> Variables 中添加）",
      };
    }

    // 1. 查询 Discord 频道最新消息
    const discordUrl = `https://discord.com/api/v10/channels/${channelId}/messages?limit=5`;
    const discordResp = await fetch(discordUrl, {
      headers: {
        Authorization: `Bot ${botToken}`,
        "User-Agent": "DiscordBot (https://github.com/lemon-o/Ark-Recode-material, 1.0.0)",
      },
    });

    if (!discordResp.ok) {
      const errText = await discordResp.text();
      return {
        status: "error",
        message: `Discord API 请求失败: HTTP ${discordResp.status} - ${errText}`,
      };
    }

    const messages = await discordResp.json();
    if (!Array.isArray(messages) || messages.length === 0) {
      return { status: "ok", message: "频道暂无消息" };
    }

    // 2. 筛选包含维护、更新或招募特征的公告
    let targetMsg = null;
    let targetContent = "";
    const keywords = ["維護", "维护", "更新", "招募", "機率UP", "概率UP"];

    for (const m of messages) {
      const text = extractMessageContent(m);
      if (keywords.some((k) => text.includes(k))) {
        targetMsg = m;
        targetContent = text;
        break;
      }
    }

    if (!targetMsg) {
      return { status: "ok", message: "最新消息中未检测到维护或招募公告" };
    }

    // 3. 防重复触发检查
    let isProcessed = false;

    // 内存检查
    if (memoryLastProcessedId && memoryLastProcessedId === targetMsg.id) {
      isProcessed = true;
    }

    // KV 检查（若绑定了 ANNOUNCEMENT_KV）
    if (!isProcessed && env.ANNOUNCEMENT_KV) {
      try {
        const lastId = await env.ANNOUNCEMENT_KV.get("last_processed_msg_id");
        if (lastId === targetMsg.id) {
          isProcessed = true;
        }
      } catch (e) {
        // 忽略 KV 读取异常
      }
    }

    // Edge Cache 检查
    if (!isProcessed && origin) {
      try {
        const cache = caches.default;
        const cacheKey = new Request(`${origin}/__cache__/msg_${targetMsg.id}`);
        const hit = await cache.match(cacheKey);
        if (hit) {
          isProcessed = true;
        }
      } catch (e) {
        // 忽略 Cache API 异常
      }
    }

    if (isProcessed) {
      return {
        status: "ok",
        message: `该公告 (ID: ${targetMsg.id}) 之前已触发过同步，跳过重复调度`,
        content_preview: targetContent.slice(0, 80) + "...",
      };
    }

    // 4. 调用 GitHub API 触发 repository_dispatch
    const dispatchUrl = `https://api.github.com/repos/${githubRepo}/dispatches`;
    const dispatchResp = await fetch(dispatchUrl, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${githubToken}`,
        Accept: "application/vnd.github.v3+json",
        "User-Agent": "ArkRecodeSync/1.0",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        event_type: "discord_announcement",
        client_payload: {
          message_id: targetMsg.id,
          timestamp: targetMsg.timestamp,
          content: targetContent,
        },
      }),
    });

    if (!dispatchResp.ok) {
      const errText = await dispatchResp.text();
      return {
        status: "error",
        message: `GitHub repository_dispatch 触发失败: HTTP ${dispatchResp.status} - ${errText}`,
      };
    }

    // 5. 标记该消息为已处理
    memoryLastProcessedId = targetMsg.id;

    if (env.ANNOUNCEMENT_KV) {
      try {
        await env.ANNOUNCEMENT_KV.put("last_processed_msg_id", targetMsg.id);
      } catch (e) {
        // ignore
      }
    }

    if (origin) {
      try {
        const cache = caches.default;
        const cacheKey = new Request(`${origin}/__cache__/msg_${targetMsg.id}`);
        const cacheResp = new Response("processed", {
          headers: { "Cache-Control": "public, max-age=604800" },
        });
        if (ctx && ctx.waitUntil) {
          ctx.waitUntil(cache.put(cacheKey, cacheResp));
        } else {
          await cache.put(cacheKey, cacheResp);
        }
      } catch (e) {
        // ignore
      }
    }

    return {
      status: "dispatched",
      message: `[OK] 成功唤醒 GitHub Actions! 公告 ID: ${targetMsg.id}`,
      content_preview: targetContent.slice(0, 100),
    };
  } catch (err) {
    return {
      status: "exception",
      error: err.message,
      stack: err.stack,
    };
  }
}
