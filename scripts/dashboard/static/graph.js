/* ka-sfskills · studio graph
 *
 * Fetches /api/graph (skill_map.json) and renders an interactive
 * force-directed graph with d3-force v7.
 *
 * Data shape (from build_skill_map.py):
 *   { agents: [{id, name, category, skills: [skill_id, ...]}, ...],
 *     skills: [{id, name, domain, description, citers: [agent_id, ...]}, ...],
 *     domains: [{name, skill_count}, ...] }
 *
 * Edges: one per (agent, skill) pair where agent.skills includes that skill.
 * To keep performance reasonable on 982 skills, we only render skills that
 * are actually cited by at least one agent. That's typically ~200-400 of
 * the 982 — enough to be useful, few enough to lay out cleanly.
 */
(() => {
  "use strict";

  const svg = d3.select("#graph-svg");
  const statusEl = document.getElementById("graph-status");
  const detailCard = document.getElementById("graph-detail-card");
  const detailEmpty = document.querySelector(".graph-detail-empty");
  const emptyEl = document.getElementById("graph-empty");
  const emptyMsg = document.getElementById("graph-empty-msg");
  const statsEl = document.getElementById("graph-stats");
  const toolbar = document.querySelector(".graph-toolbar");

  let zoom, container, simulation;
  let nodes = [], links = [], nodeById = new Map();
  let selectedId = null;
  let activeFilter = "all";

  function setStatus(text) { if (statusEl) statusEl.textContent = text; }

  // --------------------------------------------------------------------
  // Boot
  // --------------------------------------------------------------------

  async function load(refresh) {
    setStatus("Loading skill_map.json…");
    emptyEl.hidden = true;
    try {
      const url = refresh ? "/api/graph?refresh=1" : "/api/graph";
      const res = await fetch(url);
      const data = await res.json();
      if (data.error) throw new Error(data.error);
      render(data);
    } catch (err) {
      setStatus("");
      emptyEl.hidden = false;
      emptyMsg.textContent = err.message || "Unknown error.";
    }
  }

  // --------------------------------------------------------------------
  // Render
  // --------------------------------------------------------------------

  function render(data) {
    setStatus(
      `${data.agent_count || data.agents.length} agents · ` +
      `${data.skill_count || data.skills.length} skills · ` +
      `simulating…`
    );

    // Build node and edge lists.
    nodes = [];
    links = [];
    nodeById.clear();

    // Only include skills with at least one citer; the rest are noise.
    const citedSkills = new Set();
    for (const a of data.agents) {
      for (const s of (a.skills || [])) citedSkills.add(stripSkillsPrefix(s));
    }

    for (const a of data.agents) {
      const n = {
        kind: "agent",
        id: "A::" + a.id,
        agent_id: a.id,
        name: a.name || a.id,
        domain: a.category || "other",
        skill_count: (a.skills || []).length,
        category: a.category || "other",
      };
      nodes.push(n);
      nodeById.set(n.id, n);
    }

    for (const s of data.skills) {
      const sid = stripSkillsPrefix(s.id);
      if (!citedSkills.has(sid)) continue;
      const n = {
        kind: "skill",
        id: "S::" + sid,
        skill_id: sid,
        name: s.name || sid.split("/").pop(),
        domain: s.domain || "other",
        description: s.description || "",
        citer_count: (s.citers || []).length,
      };
      nodes.push(n);
      nodeById.set(n.id, n);
    }

    for (const a of data.agents) {
      const src = "A::" + a.id;
      for (const sRaw of (a.skills || [])) {
        const sid = stripSkillsPrefix(sRaw);
        const tgt = "S::" + sid;
        if (nodeById.has(src) && nodeById.has(tgt)) {
          links.push({ source: src, target: tgt });
        }
      }
    }

    // Render stats in detail panel.
    if (statsEl) {
      const counts = {};
      for (const a of data.agents) counts[a.category || "other"] = (counts[a.category || "other"] || 0) + 1;
      const sortedDomains = Object.keys(counts).sort();
      statsEl.innerHTML = `
        <div><strong>${data.agents.length}</strong> agents · <strong>${nodes.filter(n => n.kind === "skill").length}</strong> cited skills</div>
        <div style="margin-top: 6px;"><strong>${links.length}</strong> agent → skill edges</div>
        <div style="margin-top: 12px; color: var(--text-3); font-size: 10px; text-transform: uppercase; letter-spacing: .5px;">Categories</div>
        ${sortedDomains.map(d => `<div style="font-family: var(--font-mono); font-size: 11px;">${d}: ${counts[d]}</div>`).join("")}
      `;
    }

    // Build filter chips from agent categories.
    buildFilterChips(data.agents);

    // Set up SVG: wipe, set viewbox, add zoom + container group.
    svg.selectAll("*").remove();
    const w = svg.node().clientWidth || 900;
    const h = svg.node().clientHeight || 600;
    svg.attr("viewBox", `0 0 ${w} ${h}`);
    container = svg.append("g");

    zoom = d3.zoom()
      .scaleExtent([0.2, 4])
      .on("zoom", (event) => container.attr("transform", event.transform));
    svg.call(zoom);

    // Draw links first (behind nodes).
    const linkSel = container.append("g")
      .attr("class", "glinks")
      .selectAll("line")
      .data(links)
      .join("line")
      .attr("class", "glink");

    // Draw nodes.
    const nodeSel = container.append("g")
      .attr("class", "gnodes")
      .selectAll("g")
      .data(nodes)
      .join("g")
      .attr("class", n => `gnode ${n.kind} ${n.domain || ""}`)
      .call(
        d3.drag()
          .on("start", (event, d) => {
            if (!event.active) simulation.alphaTarget(0.3).restart();
            d.fx = d.x; d.fy = d.y;
          })
          .on("drag", (event, d) => { d.fx = event.x; d.fy = event.y; })
          .on("end", (event, d) => {
            if (!event.active) simulation.alphaTarget(0);
            d.fx = null; d.fy = null;
          })
      )
      .on("click", (event, d) => { event.stopPropagation(); selectNode(d); });

    nodeSel.append("circle")
      .attr("r", n => n.kind === "agent" ? Math.min(6 + Math.sqrt(n.skill_count || 0) * 1.4, 16) : 3);

    nodeSel.append("text")
      .attr("dy", n => n.kind === "agent" ? -12 : -7)
      .attr("text-anchor", "middle")
      .text(n => {
        if (n.kind === "agent") return n.agent_id;
        // For skills, show only the last segment (e.g. "trigger-framework")
        // so labels stay readable.
        return n.skill_id.split("/").slice(-1)[0];
      });

    // Hover tooltip via title element.
    nodeSel.append("title").text(n => n.kind === "agent" ? n.agent_id : n.skill_id);

    // Background click: clear selection.
    svg.on("click", () => {
      if (selectedId) {
        selectedId = null;
        applyHighlight();
        showDetailEmpty();
      }
    });

    // Force simulation.
    simulation = d3.forceSimulation(nodes)
      .force("link", d3.forceLink(links).id(d => d.id).distance(50).strength(0.5))
      .force("charge", d3.forceManyBody().strength(n => n.kind === "agent" ? -240 : -60))
      .force("center", d3.forceCenter(w / 2, h / 2))
      .force("collide", d3.forceCollide().radius(n => n.kind === "agent" ? 18 : 6).strength(0.7))
      .alpha(1)
      .alphaDecay(0.025)
      .on("tick", () => {
        linkSel
          .attr("x1", d => d.source.x).attr("y1", d => d.source.y)
          .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
        nodeSel.attr("transform", d => `translate(${d.x},${d.y})`);
      })
      .on("end", () => setStatus("Ready · click any node"));

    // Apply default filter (all).
    applyFilter("all");
  }

  // --------------------------------------------------------------------
  // Selection + highlight
  // --------------------------------------------------------------------

  function selectNode(d) {
    selectedId = d.id;
    applyHighlight();
    showDetailCard(d);
  }

  function applyHighlight() {
    const neighborIds = new Set();
    if (selectedId) {
      neighborIds.add(selectedId);
      for (const l of links) {
        const s = typeof l.source === "object" ? l.source.id : l.source;
        const t = typeof l.target === "object" ? l.target.id : l.target;
        if (s === selectedId) neighborIds.add(t);
        if (t === selectedId) neighborIds.add(s);
      }
    }
    d3.selectAll(".gnode").classed("dimmed", n => selectedId && !neighborIds.has(n.id));
    d3.selectAll(".gnode").classed("selected", n => n.id === selectedId);
    d3.selectAll(".gnode").classed("highlighted", n => selectedId && neighborIds.has(n.id));
    d3.selectAll(".glink").classed("highlighted", l => {
      const s = typeof l.source === "object" ? l.source.id : l.source;
      const t = typeof l.target === "object" ? l.target.id : l.target;
      return selectedId && (s === selectedId || t === selectedId);
    });
    d3.selectAll(".glink").classed("dimmed", l => {
      if (!selectedId) return false;
      const s = typeof l.source === "object" ? l.source.id : l.source;
      const t = typeof l.target === "object" ? l.target.id : l.target;
      return s !== selectedId && t !== selectedId;
    });
  }

  function showDetailCard(d) {
    detailEmpty.hidden = true;
    detailCard.hidden = false;
    if (d.kind === "agent") {
      const neighbors = links
        .filter(l => (typeof l.source === "object" ? l.source.id : l.source) === d.id)
        .map(l => typeof l.target === "object" ? l.target : nodeById.get(l.target))
        .filter(Boolean);
      detailCard.innerHTML = `
        <div class="badge agent">Agent · ${d.domain}</div>
        <h2>${escapeHtml(d.agent_id)}</h2>
        <div class="meta">${d.skill_count} suggested skill${d.skill_count === 1 ? "" : "s"} · ${neighbors.length} edges in graph</div>
        <h3>Suggested skills (first 30)</h3>
        ${neighbors.slice(0, 30).map(n => `<a class="link-row" href="/skills/${n.skill_id.replaceAll("/", "__")}">${escapeHtml(n.skill_id)}</a>`).join("")}
        ${neighbors.length > 30 ? `<div style="font-size: 11px; color: var(--text-3); padding: 6px 10px;">+ ${neighbors.length - 30} more</div>` : ""}
        <div class="actions">
          <a href="/agents/${encodeURIComponent(d.agent_id)}">Open AGENT.md</a>
        </div>
      `;
    } else {
      const neighbors = links
        .filter(l => (typeof l.target === "object" ? l.target.id : l.target) === d.id)
        .map(l => typeof l.source === "object" ? l.source : nodeById.get(l.source))
        .filter(Boolean);
      detailCard.innerHTML = `
        <div class="badge skill">Skill · ${d.domain}</div>
        <h2>${escapeHtml(d.skill_id)}</h2>
        <div class="meta">cited by ${neighbors.length} agent${neighbors.length === 1 ? "" : "s"}</div>
        ${d.description ? `<p class="desc">${escapeHtml(d.description)}</p>` : ""}
        <h3>Cited by</h3>
        ${neighbors.map(n => `<a class="link-row" href="/agents/${encodeURIComponent(n.agent_id)}">${escapeHtml(n.agent_id)}</a>`).join("")}
        <div class="actions">
          <a href="/skills/${d.skill_id.replaceAll("/", "__")}">Open SKILL.md</a>
        </div>
      `;
    }
  }

  function showDetailEmpty() {
    detailEmpty.hidden = false;
    detailCard.hidden = true;
  }

  // --------------------------------------------------------------------
  // Filter chips by domain
  // --------------------------------------------------------------------

  function buildFilterChips(agents) {
    if (!toolbar) return;
    const cats = new Set(agents.map(a => a.category || "other"));
    // Wipe existing dynamic chips, keep the "all" one.
    toolbar.querySelectorAll(".chip.domain").forEach(c => c.remove());
    for (const cat of Array.from(cats).sort()) {
      const btn = document.createElement("button");
      btn.className = "chip domain";
      btn.dataset.filter = cat;
      btn.textContent = cat;
      btn.type = "button";
      btn.onclick = () => applyFilter(cat);
      toolbar.appendChild(btn);
    }
    // Wire the "all" button.
    const allBtn = toolbar.querySelector('.chip[data-filter="all"]');
    if (allBtn) allBtn.onclick = () => applyFilter("all");
  }

  function applyFilter(cat) {
    activeFilter = cat;
    toolbar.querySelectorAll(".chip").forEach(c =>
      c.classList.toggle("active", c.dataset.filter === cat)
    );
    d3.selectAll(".gnode").classed("filtered-out", n => {
      if (cat === "all") return false;
      // For an agent, filter by its own category. For a skill, show it
      // only if any of its agent-citers match the selected category.
      if (n.kind === "agent") return n.domain !== cat;
      const matched = links.some(l => {
        const s = typeof l.source === "object" ? l.source : nodeById.get(l.source);
        const t = typeof l.target === "object" ? l.target : nodeById.get(l.target);
        return t && t.id === n.id && s && s.domain === cat;
      });
      return !matched;
    });
    d3.selectAll(".gnode.filtered-out").style("display", "none");
    d3.selectAll(".gnode:not(.filtered-out)").style("display", null);
    d3.selectAll(".glink").style("display", l => {
      if (cat === "all") return null;
      const s = typeof l.source === "object" ? l.source : nodeById.get(l.source);
      return s && s.domain === cat ? null : "none";
    });
  }

  // --------------------------------------------------------------------
  // Zoom controls
  // --------------------------------------------------------------------

  function bindZoomButtons() {
    document.getElementById("graph-zoom-in").onclick = () =>
      svg.transition().duration(200).call(zoom.scaleBy, 1.4);
    document.getElementById("graph-zoom-out").onclick = () =>
      svg.transition().duration(200).call(zoom.scaleBy, 0.7);
    document.getElementById("graph-zoom-fit").onclick = () =>
      svg.transition().duration(300).call(zoom.transform, d3.zoomIdentity);
  }

  // --------------------------------------------------------------------
  // Utilities
  // --------------------------------------------------------------------

  function stripSkillsPrefix(id) {
    return String(id || "").replace(/^skills\//, "");
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // --------------------------------------------------------------------
  // Boot
  // --------------------------------------------------------------------

  // Boot is idempotent. We re-run on both initial load and on spa:ready
  // (the latter fires after SPA navigation swaps #page-content). If the
  // user has navigated away from /graph the host elements will be gone
  // and boot() simply returns.
  function boot() {
    const refreshBtn = document.getElementById("graph-refresh-btn");
    const svg = document.getElementById("graph-svg");
    if (!refreshBtn || !svg) return; // not on the graph page
    refreshBtn.onclick = () => load(true);
    bindZoomButtons();
    load(false);
  }
  document.addEventListener("DOMContentLoaded", boot);
  document.addEventListener("spa:ready", boot);
})();
