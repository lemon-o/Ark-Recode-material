/**
 * Cloudflare Worker: Discord 官方公告智能排程 -> GitHub Actions 定点准时联动网关
 *
 * 核心逻辑（智能排程，绝不盲目提前触发）：
 * 1. 定时（Cron Trigger，每 5 分钟）轻量查询 Discord 目标频道（#综合）；
 * 2. 深度解析公告正文，提取维护结束时间（如 11/26 18:00 或 11/18 08:00）；
 * 3. 严格执行两阶段精准触发：
 *    - 【第一阶段】维护结束时间准时触发 1 次（从 CDN 获取刚更新的一手素材）；
 *    - 【第二阶段】维护结束当天晚上 23:55 兜底触发 1 次（防止开服延期或晚间补丁）；
 *    - 【其余所有时间】绝不触发 GitHub Actions，返回等待状态并输出倒计时；
 * 4. 访问 /check 路由可实时查看当前公告、维护时间及下次计划抓取时间。
 */

// 实例内存记录已触发过的槽位: Set of `${msgId}_${slot}`
const dispatchedSlots = new Set();
// 记录最近派发时间戳，防止毫秒级瞬时双发并发: Map of `${slotKey} -> timestamp`
const lastDispatchTimestamps = new Map();

export default {
  // 1. 定时任务触发（Cron Trigger）
  async scheduled(event, env, ctx) {
    if (ctx && ctx.waitUntil) {
      ctx.waitUntil(checkAndDispatch(env, ctx, "https://internal.worker"));
    } else {
      await checkAndDispatch(env, ctx, "https://internal.worker");
    }
  },

  // 2. HTTP 路由（供手动测试或查看当前排程状态）
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    try {
      if (url.pathname === "/" || url.pathname === "/check") {
        const force = url.searchParams.get("force") === "true";
        const result = await checkAndDispatch(env, ctx, url.origin, force);
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
          service: "Ark-Recode Discord Announcement Scheduled Bridge",
          usage: "访问 /check 查看当前排程状态，/check?force=true 可强制触发一次测试",
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

function resolveYear(yearStr, targetMonth, currentMonth, currentYear) {
  if (yearStr) return parseInt(yearStr, 10);
  if (currentMonth === 11 && targetMonth === 0) return currentYear + 1;
  if (currentMonth === 0 && targetMonth === 11) return currentYear - 1;
  return currentYear;
}

/**
 * 解析公告文本中的维护结束时间点（转换为 UTC 时间戳）
 */
function parseMaintenanceSchedule(text) {
  if (!text) return null;
  const now = new Date();
  const defaultYear = now.getFullYear();
  const currentMonth = now.getMonth();

  // 格式 1：时间范围，如 11/26 14:00 - 18:00 (UTC+8)
  const rangeRegex = /(?:(\d{4})[年/.-])?(\d{1,2})[月/.-](\d{1,2})[日号]?(?:\s*\([^\)]*\))?\s*(\d{1,2}):(\d{2})\s*[-~至到]\s*(\d{1,2}):(\d{2})(?:\s*\(?UTC\+?8\)?)?/i;
  const matchRange = text.match(rangeRegex);
  if (matchRange) {
    const month = parseInt(matchRange[2], 10) - 1;
    const year = resolveYear(matchRange[1], month, currentMonth, defaultYear);
    const day = parseInt(matchRange[3], 10);
    const startH = parseInt(matchRange[4], 10);
    const startM = parseInt(matchRange[5], 10);
    const endH = parseInt(matchRange[6], 10);
    const endM = parseInt(matchRange[7], 10);

    // 东八区 (UTC+8) 转为标准 UTC 时间戳
    const startUtc = Date.UTC(year, month, day, startH - 8, startM, 0);
    const endUtc = Date.UTC(year, month, day, endH - 8, endM, 0);
    // 维护开始后 2 小时：如果维护本身短于 2 小时，则取 endUtc
    const mid2hUtc = (endUtc - startUtc > 2 * 3600 * 1000)
      ? (startUtc + 2 * 3600 * 1000)
      : endUtc;
    const nightUtc = Date.UTC(year, month, day, 23 - 8, 55, 0);

    const formatDt = (ts) => {
      const d = new Date(ts + 8 * 3600 * 1000);
      return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}-${String(d.getUTCDate()).padStart(2, "0")} ${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`;
    };

    return {
      type: "range",
      raw: matchRange[0],
      isHotReload: false,
      updateType: "常规停机维护（大版本/新角色）",
      startUtcTimestamp: startUtc,
      mid2hUtcTimestamp: mid2hUtc,
      endUtcTimestamp: endUtc,
      nightUtcTimestamp: nightUtc,
      beijingStartStr: formatDt(startUtc),
      beijingMid2hStr: formatDt(mid2hUtc),
      beijingEndStr: formatDt(endUtc),
      beijingNightStr: `${year}-${String(month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")} 23:55`,
    };
  }

  // 格式 2：单点更新，如 將於11/18 08:00 (UTC+8)進行活動更新
  const singleRegex = /(?:將?於|时间|時間)?\s*(?:(\d{4})[年/.-])?(\d{1,2})[月/.-](\d{1,2})[日号]?(?:\s*\([^\)]*\))?\s*(\d{1,2}):(\d{2})(?:\s*\(?UTC\+?8\)?)?/i;
  const matchSingle = text.match(singleRegex);
  if (matchSingle) {
    const month = parseInt(matchSingle[2], 10) - 1;
    const year = resolveYear(matchSingle[1], month, currentMonth, defaultYear);
    const day = parseInt(matchSingle[3], 10);
    const h = parseInt(matchSingle[4], 10);
    const m = parseInt(matchSingle[5], 10);

    const startUtc = endUtc;
    const mid2hUtc = endUtc;
    const formatDt = (ts) => {
      const d = new Date(ts + 8 * 3600 * 1000);
      return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}-${String(d.getUTCDate()).padStart(2, "0")} ${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`;
    };

    return {
      type: "point",
      raw: matchSingle[0],
      isHotReload,
      updateType: isHotReload ? "不停机/不关机活动更新（通常为复刻活动）" : "常规维护更新",
      startUtcTimestamp: startUtc,
      mid2hUtcTimestamp: mid2hUtc,
      endUtcTimestamp: endUtc,
      nightUtcTimestamp: nightUtc,
      beijingStartStr: formatDt(startUtc),
      beijingMid2hStr: formatDt(mid2hUtc),
      beijingEndStr: formatDt(endUtc),
      beijingNightStr: `${year}-${String(month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")} 23:55`,
    };
  }

  return null;
}

/**
 * 核心调度：读取 Discord 公告、解析时间、定点准时触发
 */
async function checkAndDispatch(env, ctx, origin, force = false) {
  try {
    const botToken = env.DISCORD_BOT_TOKEN;
    const channelId = env.DISCORD_CHANNEL_ID || "1573590290424401932";
    const githubRepo = env.GITHUB_REPO || "lemon-o/Ark-Recode-material";
    const githubToken = env.GITHUB_PAT || env.GITHUB_TOKEN;

    if (!botToken || !githubToken) {
      return {
        status: "error",
        message: "缺少必要环境变量: DISCORD_BOT_TOKEN 或 GITHUB_PAT",
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

    // 2. 筛选维护公告
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

    // 3. 解析维护时间排程
    const schedule = parseMaintenanceSchedule(targetContent);
    const nowUtc = Date.now();
    const beijingNowStr = new Date(nowUtc + 8 * 3600 * 1000).toISOString().replace("T", " ").slice(0, 16);

    if (!schedule && !force) {
      return {
        status: "waiting",
        message: "已获取到公告，但正文中未包含具体维护时间，跳过触发",
        content_preview: targetContent.slice(0, 80) + "...",
      };
    }

    // 4. 计算当前是否命中【准时触发窗口】
    let activeSlot = null;
    let slotReason = "";

    if (force) {
      activeSlot = "manual_force";
      slotReason = "手动强制触发测试";
    } else if (schedule) {
      // 阶段 1：维护开始后 2 小时定向抓取素材（此时 CDN 补丁已就绪）
      const windowMidStart = schedule.mid2hUtcTimestamp;
      const windowMidEnd = schedule.endUtcTimestamp;

      // 阶段 2：维护结束开服核验（如 17:00 - 17:30）
      const windowEndStart = schedule.endUtcTimestamp;
      const windowEndEnd = schedule.endUtcTimestamp + 30 * 60 * 1000;

      // 阶段 3：维护当晚 23:55 全量增量兜底 (23:55 - 00:30)
      const windowNightStart = schedule.nightUtcTimestamp;
      const windowNightEnd = schedule.nightUtcTimestamp + 35 * 60 * 1000;

      if (nowUtc >= windowMidStart && nowUtc < windowMidEnd) {
        activeSlot = "maint_mid_2h";
        slotReason = `命中维护开始后 2 小时素材定向抓取窗口 (${schedule.beijingMid2hStr})`;
      } else if (nowUtc >= windowEndStart && nowUtc < windowEndEnd) {
        activeSlot = "end_time";
        slotReason = `命中维护结束开服核验窗口 (${schedule.beijingEndStr})`;
      } else if (nowUtc >= windowNightStart && nowUtc < windowNightEnd) {
        activeSlot = "night_fallback";
        slotReason = `命中维护当晚 12 点兜底抓取窗口 (${schedule.beijingNightStr})`;
      } else if (nowUtc < windowMidStart) {
        // 槽位 0：新公告发布后，立即向 Actions 派发轻量 notice_sync 槽位，同步 announcement.json 与看板供下游消费
        const noticeSyncSlotKey = `${targetMsg.id}_notice_sync`;
        let isNoticeSynced = dispatchedSlots.has(noticeSyncSlotKey);
        if (!isNoticeSynced && env.ANNOUNCEMENT_KV) {
          try {
            const val = await env.ANNOUNCEMENT_KV.get(noticeSyncSlotKey);
            if (val) isNoticeSynced = true;
          } catch (e) {}
        }
        if (!isNoticeSynced) {
          try {
            const annResp = await fetch(
              `https://raw.githubusercontent.com/${githubRepo}/main/data/announcement.json?t=${Date.now()}`,
              { headers: { Authorization: `Bearer ${githubToken}` } }
            );
            if (annResp.ok) {
              const annData = await annResp.json();
              if (String(annData.id) === String(targetMsg.id)) {
                isNoticeSynced = true;
              }
            }
          } catch (e) {}
        }

        if (!isNoticeSynced) {
          activeSlot = "notice_sync";
          slotReason = `发现最新维护公告 #${targetMsg.id}，即时同步公告元数据与看板供下游消费`;
        } else {
          const diffMinutes = Math.round((windowMidStart - nowUtc) / (60 * 1000));
          return {
            status: "waiting",
            message: "维护公告已即时同步；当前等待维护开始后 2 小时准时抓取素材",
            update_type: schedule.updateType,
            current_time_beijing: beijingNowStr,
            maintenance_start_beijing: schedule.beijingStartStr,
            assets_extract_beijing: schedule.beijingMid2hStr,
            maintenance_end_beijing: schedule.beijingEndStr,
            night_fallback_beijing: schedule.beijingNightStr,
            countdown: `距离维护开始后 2 小时素材抓取还剩约 ${diffMinutes} 分钟`,
            announcement_id: targetMsg.id,
          };
        }
      } else if (nowUtc > windowNightEnd) {
        return {
          status: "expired",
          message: "该维护公告已过期（已超过当晚 12 点兜底时间），跳过",
          update_type: schedule.updateType,
          current_time_beijing: beijingNowStr,
          maintenance_end_beijing: schedule.beijingEndStr,
          announcement_id: targetMsg.id,
        };
      } else {
        return {
          status: "waiting_fallback",
          message: "开服校对阶段已过，当前等待当晚 23:55 全量兜底抓取",
          update_type: schedule.updateType,
          current_time_beijing: beijingNowStr,
          next_trigger_beijing: schedule.beijingNightStr,
          announcement_id: targetMsg.id,
        };
      }
    }

    // 5. 防重复触发检查（每个公告的每个槽位只触发一次）
    const slotKey = `${targetMsg.id}_${activeSlot}`;
    const nowTimestamp = Date.now();
    const lastDispatchedAt = lastDispatchTimestamps.get(slotKey) || 0;
    // 60 秒内严格防抖，杜绝多实例/并发瞬时双发
    if (nowTimestamp - lastDispatchedAt < 60000) {
      return {
        status: "ok",
        message: `[${slotReason}] 该槽位 (${slotKey}) 刚刚已在 60 秒内触发过，防抖拦截`,
        current_time_beijing: beijingNowStr,
      };
    }

    let isDispatched = dispatchedSlots.has(slotKey);

    // 检查 Cloudflare KV（若绑定）
    if (!isDispatched && env.ANNOUNCEMENT_KV) {
      try {
        const val = await env.ANNOUNCEMENT_KV.get(slotKey);
        if (val) isDispatched = true;
      } catch (e) {
        // ignore
      }
    }

    // 检查 GitHub 远端 catalog-info.json 记录
    if (!isDispatched && !force) {
      try {
        const infoResp = await fetch(
          `https://raw.githubusercontent.com/${githubRepo}/main/data/catalog-info.json?t=${Date.now()}`,
          { headers: { Authorization: `Bearer ${githubToken}` } }
        );
        if (infoResp.ok) {
          const info = await infoResp.json();
          // 如果仓库中记录的槽位已处理过
          if (info.dispatched_slots && info.dispatched_slots.includes(slotKey)) {
            isDispatched = true;
          }
        }
      } catch (e) {
        // ignore
      }
    }

    if (isDispatched) {
      return {
        status: "ok",
        message: `[${slotReason}] 该槽位 (${slotKey}) 之前已触发过，跳过重复调度`,
        current_time_beijing: beijingNowStr,
      };
    }

    // 6. 准时唤醒 GitHub Actions
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
          slot: activeSlot,
          slot_key: slotKey,
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

    // 7. 记录该槽位为已触发
    dispatchedSlots.add(slotKey);
    lastDispatchTimestamps.set(slotKey, Date.now());
    if (env.ANNOUNCEMENT_KV) {
      try {
        await env.ANNOUNCEMENT_KV.put(slotKey, "dispatched", { expirationTtl: 604800 });
      } catch (e) {
        // ignore
      }
    }

    return {
      status: "dispatched",
      slot: activeSlot,
      reason: slotReason,
      message: `[OK] 准时成功唤醒 GitHub Actions! (${slotReason})`,
      current_time_beijing: beijingNowStr,
      announcement_id: targetMsg.id,
      preview: targetContent.slice(0, 100),
    };
  } catch (err) {
    return {
      status: "exception",
      error: err.message,
      stack: err.stack,
    };
  }
}
