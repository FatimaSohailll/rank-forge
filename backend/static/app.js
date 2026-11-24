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
    if (!res.ok) throw new Error("Could not check pending approvals. Please try again.");
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
        if (event === "progress") onProgress(parsed.label, parsed.seconds, parsed);
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
    const chat = $(".chat");
    let thread = sessionStorageGet("rf-thread") || newThread();
    let busy = false;
    let editingDraft = null;
    let activeCard = null;
    let pendingBox = null;
    const editingHint = $("#editing-hint");

    function setBusy(value) {
      busy = value;
      $$("button", chat).forEach((button) => { button.disabled = value; });
      $("#new-thread").disabled = value;
      input.disabled = value;
      form.setAttribute("aria-busy", String(value));
    }
    function clearEditing() {
      editingDraft = null;
      editingHint.hidden = true;
      input.placeholder = "What would you like to write about?";
    }
    function beginEditing(article) {
      editingDraft = article;
      editingHint.textContent = "Editing “" + article.title + "” · Tell us what you’d like to change.";
      editingHint.hidden = false;
      input.placeholder = "For example: shorten the introduction, or make the tone more friendly…";
      input.focus();
    }

    function sessionStorageGet(key) { try { return sessionStorage.getItem(key); } catch (e) { return null; } }
    function newThread() {
      const id = "web-" + Math.random().toString(36).slice(2, 10);
      try { sessionStorage.setItem("rf-thread", id); } catch (e) { /* private mode */ }
      return id;
    }

    $("#new-thread").addEventListener("click", () => {
      if (busy) return;
      thread = newThread();
      log.replaceChildren();
      clearEditing();
      activeCard = null;
      pendingBox = null;
      input.value = "";
      input.focus();
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

    function retireCard() {
      if (!activeCard) return;
      const actions = $(".article-actions", activeCard);
      if (actions) actions.hidden = true;
      activeCard.classList.add("previous-version");
    }

    function articleCard(node, article) {
      retireCard();
      clearEditing();
      const card = el("section", "article-ready");
      card.append(el("p", "eyebrow", "Your article is ready"), el("h2", "", article.title));
      card.append(el("p", "article-stats", article.words + " words · " + (article.seo_score == null ? "Not scored yet" : "SEO score " + article.seo_score + "/100") + " · Saved as a draft"));
      const actions = el("div", "article-actions");
      const publish = el("button", "btn", "Publish");
      publish.type = "button";
      publish.addEventListener("click", () => run("Review this article for publishing", "publish", article.draft_id));
      const edit = el("button", "btn ghost", "Continue editing");
      edit.type = "button";
      edit.addEventListener("click", () => beginEditing(article));
      const view = el("a", "text-link", "Open full workspace ↗");
      view.href = article.workspace_url;
      actions.append(publish, edit, view);
      card.append(actions, el("p", "muted small", "Only you decide when it goes live."));
      node.prepend(card);
      activeCard = card;
    }

    async function offerApproval(afterNode) {
      const mine = (await pendingApprovals()).filter((a) => a.thread_key === USER + ":" + thread);
      if (!mine.length) {
        if (pendingBox) { pendingBox.remove(); pendingBox = null; }
        return false;
      }
      if (pendingBox) pendingBox.remove();
      const approval = mine[0];
      const box = el("section", "approval publish-review");
      pendingBox = box;
      if (activeCard) $(".article-actions", activeCard).hidden = true;
      clearEditing();
      box.dataset.token = approval.resume_token;
      box.append(el("h2", "", "Ready to go live?"));
      box.append(el("p", "", "Review the article above before publishing it to your public RankForge blog."));
      if (approval.action.replaces_title) {
        box.append(el("p", "note", "This replaces your live article “" + approval.action.replaces_title + "” at the same address."));
      }
      const view = el("a", "text-link", "Review the exact article ↗");
      view.href = "/app/articles/" + approval.action.draft_id;
      view.target = "_blank";
      view.rel = "noopener";
      box.append(view);
      const actions = el("div", "actions");
      const yes = el("button", "btn", "Confirm and publish");
      const no = el("button", "btn ghost", "Back to editing");
      yes.type = no.type = "button";
      const out = el("div", "result");
      out.setAttribute("aria-live", "polite");
      async function decide(decision) {
        if (busy) return;
        setBusy(true);
        try {
          const data = await postJSON("/resume", { user_id: USER, resume_token: approval.resume_token, decision });
          // Refresh approval state before enabling edits; a failed request must not bypass the gate.
          const stillPending = (await pendingApprovals()).some((a) => a.resume_token === approval.resume_token);
          out.innerHTML = data.reply_html || "";
          if (stillPending) return;
          actions.hidden = true;
          pendingBox = null;
          if (decision === "reject") {
            const article = {draft_id: approval.action.draft_id, title: approval.action.title};
            if (activeCard) $(".article-actions", activeCard).hidden = false;
            beginEditing(article);
          } else {
            retireCard();
          }
        } catch (err) {
          out.textContent = err.message;
        } finally { setBusy(false); }
      }
      yes.addEventListener("click", () => decide("approve"));
      no.addEventListener("click", () => decide("reject"));
      actions.append(yes, no);
      box.append(actions, out);
      afterNode.appendChild(box);
      return true;
    }

    const stageMap = {
      keyword_scout: ["research", "Researching your topic", "Topic researched"],
      content_planner: ["plan", "Planning your article", "Article planned"],
      writer: ["write", "Writing your article", "Article written"],
      reviser: ["write", "Updating your article", "Changes applied"],
      fact_checker: ["checks", "Checking facts and links", "Facts and links reviewed"],
      link_builder: ["checks", "Checking facts and links", "Facts and links reviewed"],
      editor: ["polish", "Polishing your article", "Article polished"],
      publisher: ["publish", "Publishing your article", "Article published"],
      rank_tracker: ["performance", "Reviewing your article performance", "Performance reviewed"],
      click_tracker: ["performance", "Reviewing your article performance", "Performance reviewed"],
      optimizer: ["recommend", "Finding opportunities to improve", "Recommendations ready"],
      remember: ["remember", "Saving your preferences", "Preferences saved"]
    };

    async function run(message, action, target) {
      if (busy) return;
      if (pendingBox && action === "revise") {
        input.value = message;
        input.focus();
        return;
      }
      addMessage("user", message);
      setBusy(true);
      const task = el("section", "turn-progress");
      task.setAttribute("aria-label", "Article progress");
      const status = el("p", "progress-heading", "Getting started…");
      const steps = el("ol", "progress-steps");
      task.append(status, steps);
      log.appendChild(task);
      const nodes = new Map();
      const rows = new Map();
      let interrupted = false;
      try {
        const payload = { user_id: USER, thread_id: thread, message: message };
        if (action) { payload.action = action; payload.target_id = Number(target); }
        const done = await streamChat(payload, (label, secs, event) => {
          const stage = stageMap[event.node];
          if (!stage) return;
          nodes.set(event.node, event.status || "complete");
          const [key, workingLabel, completeLabel] = stage;
          let row = rows.get(key);
          if (!row) { row = el("li", "working", workingLabel); steps.append(row); rows.set(key, row); }
          const related = Object.keys(stageMap).filter((n) => stageMap[n][0] === key);
          const required = key === "checks" ? ["fact_checker", "link_builder"] :
            key === "performance" ? ["rank_tracker", "click_tracker"] : related.filter((n) => nodes.has(n));
          const failed = required.some((n) => nodes.has(n) && !["started", "complete"].includes(nodes.get(n)));
          const complete = required.every((n) => nodes.get(n) === "complete");
          row.className = failed ? "stopped" : complete ? "complete" : "working";
          row.textContent = failed ? workingLabel + " — needs attention" : complete ? completeLabel : workingLabel + "…";
          if (failed) interrupted = true;
          status.textContent = failed ? "This step needs attention" : workingLabel + "…";
          row.scrollIntoView({block: "nearest"});
        });
        if (done.halted || done.error) interrupted = true;
        steps.querySelectorAll(".working").forEach((row) => { row.className = "stopped"; row.textContent += " — not completed"; interrupted = true; });
        status.textContent = interrupted ? "Stopped — see the message below" : done.article ? "Ready for your review" : "Finished";
        if (!rows.size) task.remove();
        const node = addMessage("assistant", "", done.reply_html);
        if (done.article) {
          // Keep the article itself as a readable, collapsible preview.
          const preview = el("details", "article-preview");
          preview.open = true;
          preview.append(el("summary", "", "Read your article"));
          const content = el("div", "article-body");
          while (node.firstChild) content.appendChild(node.firstChild);
          preview.append(content);
          node.append(preview);
          articleCard(node, done.article);
        }
        await offerApproval(node);
      } catch (err) {
        status.textContent = "Connection interrupted — your saved drafts are safe";
        steps.querySelectorAll(".working").forEach((row) => { row.className = "stopped"; });
        addMessage("assistant error", err.message + " Check My articles before retrying.");
        if (action === "revise") input.value = message;
      } finally {
        setBusy(false);
        input.focus();
      }
    }

    form.addEventListener("submit", (e) => {
      e.preventDefault();
      if (busy) return;
      const message = input.value.trim();
      if (!message) return;
      input.value = "";
      run(message, editingDraft ? "revise" : undefined, editingDraft ? editingDraft.draft_id : undefined);
    });

    const savedDraft = $("#saved-draft");
    if (savedDraft) {
      $(".saved-draft-fallback", savedDraft).remove();
      articleCard(savedDraft, {
        draft_id: Number(savedDraft.dataset.draft), title: savedDraft.dataset.title,
        words: Number(savedDraft.dataset.words),
        seo_score: savedDraft.dataset.score === "" ? null : Number(savedDraft.dataset.score),
        workspace_url: savedDraft.dataset.workspace
      });
    }

    if (!chat.dataset.autorunAction) {
      offerApproval(log).catch(() => { /* existing drafts remain available in My articles */ });
    }

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
