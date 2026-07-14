/*!
 * SpiderX.AI embed widget — drop this on any website to give visitors a
 * floating "Call <agent>" bubble that opens a real voice conversation with
 * one of your phone-AI agents.
 *
 * Usage on a host page:
 *   <script src="https://app.spiderx.ai/static/embed.js" data-agent="my-agent-slug"></script>
 *
 * Optional data-* knobs (read off the same <script> tag):
 *   data-agent      — REQUIRED — slug of the agent (matches /api/agents/by-slug/<slug>)
 *   data-position   — bottom-right (default) | bottom-left
 *   data-color      — primary button color (default: brand violet gradient)
 *   data-label      — tooltip on the bubble (default: "Talk to <Agent>")
 *   data-mode       — "popover" (default, ~360×600 panel) | "fullscreen" | "drawer" (bottom sheet)
 *   data-accent     — response/bubble color inside the chat (e.g. #7c3aed)
 *   data-radius     — bubble corner radius in px (e.g. 18)
 *   data-size       — chat text scale: "sm" | "md" (default) | "lg"
 *
 * The widget is namespaced under `__sxAI_embed` to avoid colliding with any
 * other globals on the host page. Mic permission is requested by the iframe.
 */
(function () {
  if (typeof window === "undefined" || window.__sxAI_embed) return;
  window.__sxAI_embed = { v: 1 };

  // Find this very <script> tag so we can read its data-* attrs + its origin.
  // The origin tells us where the iframe should load from (same host that
  // served the script). currentScript is null inside async-loaded modules,
  // so we fall back to the last <script src*=embed.js> on the page.
  var scriptEl = document.currentScript;
  if (!scriptEl) {
    var all = document.getElementsByTagName("script");
    for (var i = all.length - 1; i >= 0; i--) {
      if ((all[i].src || "").indexOf("/static/embed.js") !== -1) { scriptEl = all[i]; break; }
    }
  }
  if (!scriptEl) {
    console.warn("[SpiderX.AI] embed.js: could not locate its own <script> tag");
    return;
  }

  var slug = scriptEl.getAttribute("data-agent");
  if (!slug) {
    console.warn("[SpiderX.AI] embed.js: missing data-agent='<slug>' on the script tag");
    return;
  }
  // data-channel: "voice" (default, mic orb) | "chat" (text surface). Chat is
  // the paid add-on; the iframe surface adapts via the ?channel=chat param.
  // This is fixed at the snippet — every OTHER knob can now come from the
  // dashboard (agent.chat_settings), fetched below, so operators change the
  // teaser / icon / colours / mode without re-pasting the embed code.
  var channel = (scriptEl.getAttribute("data-channel") || "voice").trim().toLowerCase();
  var defaultVerb = channel === "chat" ? "Chat with " : "Talk to ";
  // Explicit data-* overrides — null when the attribute is absent, so the
  // remote chat_settings value is used instead. A present attribute always wins.
  function _attr(n) { return scriptEl.hasAttribute(n) ? (scriptEl.getAttribute(n) || "").trim() : null; }
  var oPosition = _attr("data-position"), oLabel = _attr("data-label"), oMode = _attr("data-mode"),
      oColor = _attr("data-color"), oAccent = _attr("data-accent"), oRadius = _attr("data-radius"),
      oSize = _attr("data-size"), oIcon = _attr("data-icon"), oTeaser = _attr("data-teaser"),
      oTeaserDelay = _attr("data-teaser-delay");

  // Resolve our origin from the script's src so we know where to point the
  // iframe. Works regardless of how the host page is hosted.
  var ourOrigin;
  try {
    ourOrigin = new URL(scriptEl.src, location.href).origin;
  } catch (e) {
    ourOrigin = scriptEl.src.replace(/\/static\/embed\.js.*$/, "");
  }

  // Build the widget once we know the config. `cs` = agent.chat_settings from
  // the server (may be {}); explicit data-* attributes still win over it.
  var _booted = false;
  function boot(cs) {
    if (_booted) return; _booted = true;
    cs = (cs && typeof cs === "object") ? cs : {};
    var position   = oPosition || "bottom-right";
    var label      = oLabel || (cs.launcher_text || "").trim() || (defaultVerb + slug.replace(/-/g, " "));
    var mode       = oMode || cs.mode || "popover";
    var color      = oColor || "";
    var accent     = oAccent || (cs.accent_color || "").trim();
    var radius     = oRadius || (cs.bubble_radius != null ? String(cs.bubble_radius) : "");
    var size       = oSize || (cs.bubble_size || "").trim();
    var iconUrl    = oIcon || (cs.launcher_icon || cs.avatar_url || "").trim();
    var teaserMsg  = (oTeaser != null ? oTeaser : (cs.teaser || "")).trim();
    var teaserDelay = parseInt(oTeaserDelay != null ? oTeaserDelay : (cs.teaser_delay != null ? cs.teaser_delay : "8"), 10);
    if (isNaN(teaserDelay) || teaserDelay < 0) teaserDelay = 8;

  // ─── Styles — injected once, scoped under .sxai-fab / .sxai-frame ────────
  var styleEl = document.createElement("style");
  styleEl.textContent = [
    ".sxai-root{position:fixed;z-index:2147483646;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display','SF Pro Text','Inter','Helvetica Neue',sans-serif;}",
    ".sxai-root[data-pos='bottom-right']{right:18px;bottom:18px;}",
    ".sxai-root[data-pos='bottom-left']{left:18px;bottom:18px;}",
    // Floating action button
    ".sxai-fab{width:60px;height:60px;border:0;border-radius:50%;background:" + (color || "linear-gradient(135deg,#a855f7 0%,#ec4899 100%)") + ";box-shadow:0 8px 24px rgba(99,102,241,0.35),0 2px 6px rgba(0,0,0,0.18);cursor:pointer;display:flex;align-items:center;justify-content:center;color:#fff;transition:transform .15s ease,box-shadow .15s ease;}",
    ".sxai-fab:hover{transform:translateY(-2px) scale(1.04);box-shadow:0 12px 32px rgba(99,102,241,0.42),0 4px 10px rgba(0,0,0,0.22);}",
    ".sxai-fab:active{transform:scale(.96);}",
    ".sxai-fab svg{width:26px;height:26px;}",
    ".sxai-fab-icon{width:34px;height:34px;border-radius:50%;object-fit:cover;pointer-events:none;}",
    // Tooltip on hover
    ".sxai-tip{position:absolute;bottom:100%;right:0;margin-bottom:10px;padding:7px 12px;border-radius:8px;background:#0f1119;color:#fff;font-size:12.5px;white-space:nowrap;opacity:0;transform:translateY(4px);transition:opacity .15s ease,transform .15s ease;pointer-events:none;box-shadow:0 4px 14px rgba(0,0,0,0.22);}",
    ".sxai-root[data-pos='bottom-left'] .sxai-tip{left:0;right:auto;}",
    ".sxai-fab:hover + .sxai-tip{opacity:1;transform:translateY(0);}",
    // Popover panel containing the iframe
    ".sxai-panel{position:absolute;bottom:74px;right:0;width:min(380px,calc(100vw - 36px));height:min(600px,calc(100vh - 100px));border-radius:18px;overflow:hidden;background:#0f1119;box-shadow:0 24px 60px rgba(0,0,0,0.32),0 4px 14px rgba(0,0,0,0.18);transform-origin:bottom right;transform:scale(0.95) translateY(8px);opacity:0;pointer-events:none;transition:transform .22s cubic-bezier(.2,.9,.3,1),opacity .18s ease;}",
    ".sxai-root[data-pos='bottom-left'] .sxai-panel{left:0;right:auto;transform-origin:bottom left;}",
    ".sxai-panel.open{transform:scale(1) translateY(0);opacity:1;pointer-events:auto;}",
    // Fullscreen mode
    ".sxai-root[data-mode='fullscreen'] .sxai-panel{position:fixed;inset:24px;width:auto;height:auto;border-radius:20px;}",
    // Bottom-drawer mode — a bottom sheet that slides up, full-width on mobile,
    // centered with a max width on desktop. Rounded top corners only.
    ".sxai-root[data-mode='drawer'] .sxai-panel{position:fixed;left:0;right:0;bottom:0;margin:0 auto;width:min(100%,540px);height:min(72vh,660px);border-radius:20px 20px 0 0;transform-origin:bottom center;transform:translateY(100%);}",
    ".sxai-root[data-mode='drawer'] .sxai-panel.open{transform:translateY(0);}",
    "@media (max-width:560px){.sxai-root[data-mode='drawer'] .sxai-panel{height:82vh;width:100%;border-radius:18px 18px 0 0;}}",
    ".sxai-panel iframe{width:100%;height:100%;border:0;display:block;background:#0f1119;}",
    // Close button overlay
    ".sxai-close{position:absolute;top:10px;right:10px;width:28px;height:28px;border-radius:50%;border:0;background:rgba(255,255,255,0.10);color:#fff;cursor:pointer;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(8px);}",
    ".sxai-close:hover{background:rgba(255,255,255,0.18);}",
    ".sxai-close svg{width:14px;height:14px;}",
    // Proactive teaser bubble (Build 293)
    ".sxai-teaser{position:absolute;bottom:100%;right:0;margin-bottom:12px;max-width:260px;padding:12px 34px 12px 14px;border-radius:14px;background:#fff;color:#1a1c25;font-size:13.5px;line-height:1.4;box-shadow:0 10px 30px rgba(0,0,0,0.18);opacity:0;transform:translateY(8px) scale(.96);transition:opacity .25s ease,transform .25s ease;pointer-events:none;cursor:pointer;text-align:left;}",
    ".sxai-teaser.show{opacity:1;transform:translateY(0) scale(1);pointer-events:auto;}",
    ".sxai-root[data-pos='bottom-left'] .sxai-teaser{left:0;right:auto;}",
    ".sxai-teaser:after{content:'';position:absolute;bottom:-6px;right:24px;width:12px;height:12px;background:#fff;transform:rotate(45deg);box-shadow:3px 3px 6px rgba(0,0,0,0.06);}",
    ".sxai-root[data-pos='bottom-left'] .sxai-teaser:after{left:24px;right:auto;}",
    ".sxai-teaser-x{position:absolute;top:6px;right:7px;width:18px;height:18px;border:0;border-radius:50%;background:rgba(0,0,0,0.06);color:#6b7280;font-size:13px;line-height:1;cursor:pointer;display:flex;align-items:center;justify-content:center;}",
    ".sxai-teaser-x:hover{background:rgba(0,0,0,0.12);}",
  ].join("");
  document.head.appendChild(styleEl);

  // ─── Mount ──────────────────────────────────────────────────────────────
  var root = document.createElement("div");
  root.className = "sxai-root";
  root.setAttribute("data-pos", position);
  root.setAttribute("data-mode", mode);

  var fab = document.createElement("button");
  fab.className = "sxai-fab";
  fab.type = "button";
  fab.setAttribute("aria-label", label);
  var chatIconSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>';
  var voiceIconSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M2 13a14 14 0 0 1 20 0l-2.4 2.4a2 2 0 0 1-2.6.2l-2-1.5a2 2 0 0 1-.7-2.1l.6-2a10 10 0 0 0-5.8 0l.6 2a2 2 0 0 1-.7 2.1l-2 1.5a2 2 0 0 1-2.6-.2L2 13z"/></svg>';
  if (iconUrl) {
    var _img = document.createElement("img");
    _img.className = "sxai-fab-icon";
    _img.src = iconUrl;
    _img.alt = "";
    fab.appendChild(_img);
  } else {
    fab.innerHTML = channel === "chat" ? chatIconSvg : voiceIconSvg;
  }
  root.appendChild(fab);

  var tip = document.createElement("div");
  tip.className = "sxai-tip";
  tip.textContent = label;
  root.appendChild(tip);

  var panel = document.createElement("div");
  panel.className = "sxai-panel";
  // The iframe loads our minimal /embed/<slug> surface — same-origin to our
  // own app, so the WebSocket + mic + audio engine all work inside.
  var iframe = document.createElement("iframe");
  // Report the host-page domain so the chat can honour a per-agent domain
  // allowlist (best-effort abuse control).
  var q = [];
  if (channel === "chat") q.push("channel=chat");
  if (accent) q.push("accent=" + encodeURIComponent(accent));
  if (radius) q.push("radius=" + encodeURIComponent(radius));
  if (size)   q.push("size=" + encodeURIComponent(size));
  try { if (location.hostname) q.push("host=" + encodeURIComponent(location.hostname)); } catch (e) {}
  iframe.src = ourOrigin + "/embed/" + encodeURIComponent(slug) + (q.length ? "?" + q.join("&") : "");
  iframe.setAttribute("title", "SpiderX.AI — " + label);
  iframe.setAttribute("allow", "microphone; autoplay; clipboard-read; clipboard-write");
  iframe.setAttribute("loading", "lazy");
  panel.appendChild(iframe);

  var closeBtn = document.createElement("button");
  closeBtn.className = "sxai-close";
  closeBtn.type = "button";
  closeBtn.setAttribute("aria-label", "Close");
  closeBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>';
  panel.appendChild(closeBtn);

  root.appendChild(panel);
  document.body.appendChild(root);

  // ─── Behaviour ──────────────────────────────────────────────────────────
  var open = false;
  var setOpen = function (next) {
    open = next;
    panel.classList.toggle("open", open);
    fab.style.display = open ? "none" : "flex";
  };
  fab.addEventListener("click", function () { setOpen(true); hideTeaser(); });
  closeBtn.addEventListener("click", function () { setOpen(false); });

  // ─── Proactive teaser ─────────────────────────────────────────────────────
  var teaser = null;
  var hideTeaser = function () {
    if (teaser) teaser.classList.remove("show");
  };
  if (teaserMsg) {
    var seenKey = "sxai_teaser_" + slug;
    var alreadySeen = false;
    try { alreadySeen = sessionStorage.getItem(seenKey) === "1"; } catch (e) {}
    if (!alreadySeen) {
      teaser = document.createElement("div");
      teaser.className = "sxai-teaser";
      var teaserText = document.createElement("span");
      teaserText.textContent = teaserMsg;
      teaser.appendChild(teaserText);
      var teaserX = document.createElement("button");
      teaserX.className = "sxai-teaser-x";
      teaserX.type = "button";
      teaserX.setAttribute("aria-label", "Dismiss");
      teaserX.innerHTML = "&times;";
      teaser.appendChild(teaserX);
      root.appendChild(teaser);
      teaserX.addEventListener("click", function (e) {
        e.stopPropagation();
        hideTeaser();
        try { sessionStorage.setItem(seenKey, "1"); } catch (er) {}
      });
      teaser.addEventListener("click", function () {
        setOpen(true); hideTeaser();
        try { sessionStorage.setItem(seenKey, "1"); } catch (er) {}
      });
      setTimeout(function () {
        if (!open && teaser) {
          teaser.classList.add("show");
          try { sessionStorage.setItem(seenKey, "1"); } catch (er) {}
        }
      }, teaserDelay * 1000);
    }
  }
  // ESC dismisses the popover when focus is inside our iframe context (best
  // effort — cross-frame ESC can't be intercepted from the host page).
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && open) setOpen(false);
  });

  // Expose a tiny API for advanced users (open/close programmatically).
  window.__sxAI_embed.open = function () { setOpen(true); };
  window.__sxAI_embed.close = function () { setOpen(false); };
  window.__sxAI_embed.slug = slug;
  } // end boot()

  // Fetch the agent's live chat_settings so dashboard changes (teaser, launcher
  // icon, colours, mode, label…) take effect without re-pasting the snippet.
  // Falls back to data-* / defaults if the API is unreachable or slow (3s cap).
  var _fallback = setTimeout(function () { boot({}); }, 3000);
  try {
    fetch(ourOrigin + "/api/agents/by-slug/" + encodeURIComponent(slug), { credentials: "omit" })
      .then(function (r) { return r && r.ok ? r.json() : null; })
      .then(function (a) { clearTimeout(_fallback); boot(a && a.chat_settings ? a.chat_settings : {}); })
      .catch(function () { clearTimeout(_fallback); boot({}); });
  } catch (e) { clearTimeout(_fallback); boot({}); }
})();
