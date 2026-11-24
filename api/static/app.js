// RankForge web UI. No framework, no inline scripts (the pages' CSP only allows this file).
// It uses the same JSON API as any client; the session cookie says who the user is.
(function () {
  "use strict";

  const body = document.body;
  const USER = body.dataset.user || "";
  const PAGE = body.dataset.page || "";

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  // ---------- small enhancements ----------
  $$("[data-autosubmit]").forEach((s) => s.addEventListener("change", () => s.form.submit()));
  $$(".bar[data-w]").forEach((b) => { b.style.width = Math.max(2, Number(b.dataset.w) || 0) + "%"; });

  // ---------- API helpers ----------
  async function postJSON(url, payload) {
    const res = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-RankForge-UI": "1" },
      body: JSON.stringify(payload),
    });
    let data = {};
    try { data = await res.json(); } catch (e) { /* not JSON */ }
    if (!res.ok) throw new Error(data.detail || ("Request failed (" + res.status + ")"));
    return data;
  }

  async function pendingApprovals() {
    const res = await fetch("/approvals", { credentials: "same-origin" });
    if (!res.ok) return [];
    return (await res.json()).approvals || [];
  }

  // Stream a chat turn: calls onProgress(label, seconds) per finished step, resolves with the final body.
  async function streamChat(payload, onProgress) {
    const res = await fetch("/chat/stream", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok || !res.body) throw new Error("Request failed (" + res.status + ")");
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let done = null;
    for (;;) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        let event = "message";
        let data = "";
        block.split("\n").forEach((line) => {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) data += line.slice(5).trim();
        });
        if (!data) continue;                       // keepalive comment
        const parsed = JSON.parse(data);
        if (event === "progress") onProgress(parsed.label, parsed.seconds);
        if (event === "done") done = parsed;
      }
    }
    if (!done) throw new Error("The connection closed before the answer arrived.");
    return done;
  }

  // ---------- approve / reject buttons (dashboard, generator, workspace) ----------
  async function resolve(container, decision) {
    const token = container.dataset.token;
    const out = $(".result", container);
    $$("button", container).forEach((b) => { b.disabled = true; });
    try {
      const data = await postJSON("/resume", { user_id: USER, resume_token: token, decision: decision });
      if (out) {
        out.replaceChildren();
        const div = el("div", "reply");
        div.innerHTML = data.reply_html || "";     // server-rendered, HTML-escaped Markdown
        out.appendChild(div);
      }
      const actions = $("[data-approval-actions]", container) || $(".actions", container);
      if (actions) actions.hidden = true;
    } catch (err) {
      if (out) out.textContent = err.message;
      $$("button", container).forEach((b) => { b.disabled = false; });
    }
  }

  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-resume]");
    if (!btn) return;
    const container = btn.closest("[data-token]");
    if (container) resolve(container, btn.dataset.resume);
  });

  // ---------- generator ----------
  if (PAGE === "generator") {
    const form = $("#chat-form");
    const input = $("#message");
    const log = $("#log");
    const progress = $("#progress");
    const send = $("#send");
    const chat = $(".chat");
    let thread = sessionStorageGet("rf-thread") || newThread();

    function sessionStorageGet(key) { try { return sessionStorage.getItem(key); } catch (e) { return null; } }
    function newThread() {
      const id = "web-" + Math.random().toString(36).slice(2, 10);
      try { sessionStorage.setItem("rf-thread", id); } catch (e) { /* private mode */ }
      return id;
    }

    $("#new-thread").addEventListener("click", () => {
      thread = newThread();
      $$(".msg:not(.intro)", log).forEach((m) => m.remove());
    });

    $$("[data-suggest]").forEach((b) => b.addEventListener("click", () => {
      input.value = b.dataset.suggest;
      input.focus();
    }));

    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
    });

    function addMessage(role, text, html) {
      const m = el("div", "msg " + role);
      if (html !== undefined) m.innerHTML = html;   // server-rendered, HTML-escaped Markdown
      else m.textContent = text;
      log.appendChild(m);
      m.scrollIntoView({ block: "end", behavior: "smooth" });
      return m;
    }

    async function offerApproval(afterNode) {
      const mine = (await pendingApprovals()).filter((a) => a.thread_key === USER + ":" + thread);
      if (!mine.length) return;
      const box = el("div", "approval");
      box.dataset.token = mine[0].resume_token;
      const actions = el("div", "actions");
      const yes = el("button", "btn small", "Approve and publish");
      yes.dataset.resume = "approve";
      const no = el("button", "btn small ghost", "Reject");
      no.dataset.resume = "reject";
      actions.append(yes, no);
      box.append(actions, el("div", "result"));
      afterNode.appendChild(box);
    }

    async function run(message, action, target) {
      addMessage("user", message);
      send.disabled = true;
      progress.replaceChildren();
      progress.hidden = false;
      const working = el("li", "working", "Working…");
      progress.appendChild(working);
      try {
        const payload = { user_id: USER, thread_id: thread, message: message };
        if (action) { payload.action = action; payload.target_id = Number(target); }
        const done = await streamChat(payload, (label, secs) => {
          const li = el("li", "", label + " (" + secs.toFixed(1) + "s)");
          progress.insertBefore(li, working);
        });
        const node = addMessage("assistant", "", done.reply_html);
        await offerApproval(node);
      } catch (err) {
        addMessage("assistant error", err.message);
      } finally {
        progress.hidden = true;
        send.disabled = false;
        input.focus();
      }
    }

    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const message = input.value.trim();
      if (!message) return;
      input.value = "";
      run(message);
    });

    if (chat.dataset.autorunAction) {
      run(chat.dataset.autorunMessage, chat.dataset.autorunAction, chat.dataset.autorunTarget);
    }
  }

  // ---------- workspace: request publish for this exact draft ----------
  if (PAGE === "workspace") {
    const box = $("#publish-box");
    const btn = $("#request-publish");
    if (box && btn) {
      btn.addEventListener("click", async () => {
        const approval = $(".approval", box);
        const out = $(".result", approval);
        btn.disabled = true;
        out.textContent = "Preparing…";
        const thread = "workspace-" + box.dataset.draft;
        try {
          const done = await streamChat({ user_id: USER, thread_id: thread, message: "publish this draft",
                                          action: "publish", target_id: Number(box.dataset.draft) }, () => {});
          out.innerHTML = done.reply_html;          // server-rendered, HTML-escaped Markdown
          const mine = (await pendingApprovals()).filter((a) => a.thread_key === USER + ":" + thread);
          if (mine.length) {
            approval.dataset.token = mine[0].resume_token;
            $("[data-approval-actions]", approval).hidden = false;
            btn.hidden = true;
            $(".prompt", approval).textContent = "Waiting for your approval.";
          } else {
            btn.disabled = false;
          }
        } catch (err) {
          out.textContent = err.message;
          btn.disabled = false;
        }
      });
    }
  }
})();
