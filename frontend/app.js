/* ============================================================
   品牌内容合规管控系统 - 前端 SPA 应用
   侧边栏导航 + 多视图切换 + 角色权限控制
   ============================================================ */

const App = {
  // ── 状态 ──
  me: null,
  regions: {},
  feedData: { items: [], page: 1, total: 0 },
  subPage: 1,
  revPage: 1,
  selectedFiles: [],
  t7ShotFiles: [],
  t7ProofFiles: [],
  editingRuleId: null,

  /* =========================================================
     API 助手
     ========================================================= */
  async _api(path, opts = {}) {
    const r = await fetch(path, { credentials: "same-origin", ...opts });
    // 尝试 JSON，失败则返回文本
    let data = {};
    try { data = await r.json(); } catch { data = { _text: await r.text().catch(() => "") }; }
    return { ok: r.ok, status: r.status, data };
  },

  /* =========================================================
     交互工具：数字滚动 + 骨架屏
     ========================================================= */
  // 数字从 0 滚动到目标值（easeOutCubic）
  animateNumber(el, target, opts = {}) {
    if (!el) return;
    const { duration = 700, decimals = 0 } = opts;
    const t = Number(target) || 0;
    const start = performance.now();
    const step = (now) => {
      const p = Math.min((now - start) / duration, 1);
      const eased = 1 - Math.pow(1 - p, 3);
      const v = t * eased;
      el.textContent = decimals ? v.toFixed(decimals) : Math.round(v).toLocaleString("zh-CN");
      if (p < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  },
  // 遍历容器内带 data-target 的数字元素并触发滚动
  animateNumbers(container) {
    if (!container) return;
    container.querySelectorAll("[data-target]").forEach(el => {
      const t = Number(el.dataset.target) || 0;
      const dec = Number(el.dataset.decimals) || 0;
      this.animateNumber(el, t, { decimals: dec });
    });
  },
  // 条形图增长动画：遍历容器内带 data-width 的元素，从 0 过渡到目标宽度
  animateBars(container) {
    if (!container) return;
    requestAnimationFrame(() => requestAnimationFrame(() => {
      container.querySelectorAll("[data-width]").forEach(el => { el.style.width = (el.dataset.width || 0) + "%"; });
    }));
  },
  // 骨架屏：N 行文本
  skeletonLines(n = 4) {
    return `<div class="sk-box">` + Array.from({ length: n }, () =>
      `<div class="sk" style="width:${35 + Math.floor(Math.random() * 55)}%"></div>`).join("") + `</div>`;
  },
  // 骨架屏：卡片网格
  skeletonCards(n = 6) {
    return `<div class="sk-grid">` + Array.from({ length: n }, () =>
      `<div class="sk-card"><div class="sk" style="width:45%;height:20px"></div><div class="sk" style="width:82%"></div><div class="sk" style="width:60%"></div></div>`).join("") + `</div>`;
  },
  // 全国战况 KPI 卡片（带图标 + 主题色）
  kpiCard(iconPath, label, target, accent = "#2563eb", soft = "#eff6ff") {
    return `<div class="rp-kpi" style="--accent:${accent};--accent-soft:${soft}">
      <span class="rp-kpi-icon"><svg class="ic" viewBox="0 0 24 24">${iconPath}</svg></span>
      <div class="rp-kpi-main">
        <div class="rp-kpi-num" data-target="${target}">0</div>
        <div class="rp-kpi-lbl">${label}</div>
      </div>
    </div>`;
  },

  /* =========================================================
     初始化 & 认证
     ========================================================= */
  async init() {
    // 页面关闭时清理授权轮询
    window.addEventListener("beforeunload", () => {
      if (this._authTimer) { clearTimeout(this._authTimer); }
    });

    // 判断当前访问的是哪个页面
    const pathname = window.location.pathname;
    this._isUserPage = (pathname === "/user" || pathname === "/" || pathname === "/index.html");
    this._isManagerPage = (pathname === "/manager");

    // 顶部栏仅在管理后台显示；公开上传页（/user）隐藏
    const mgrTopbar = document.getElementById("mgrTopbar");
    if (mgrTopbar) mgrTopbar.style.display = this._isManagerPage ? "" : "none";

    // 加载区域数据
    const { data: reg } = await this._api("/api/regions");
    if (reg) this.regions = reg;

    // 加载双百战役赛道（填充上传表单下拉）
    this._loadTracks();

    // 检查登录状态
    const { ok, data } = await this._api("/api/auth/me");
    if (!ok || !data || !data.id) {
      // 未登录
      this.me = null;
      if (this._isUserPage) {
        // /user：免登录上传页，隐藏侧边栏
        this._renderGuest();
      } else {
        // /manager：未登录 → 重定向到登录页
        window.location.href = "/login.html";
        return;
      }
      this._bindNav();
      this._populateBigRegions();
      this._prepRegions();
      document.addEventListener("click", this._clickOutside);
      return;
    }
    this.me = data;
    // 登录用户：显示侧边栏+通知铃
    document.getElementById("sidebar").style.display = "flex";
    document.getElementById("notifBell").style.display = "";
    this._renderSidebar();
    this._bindNav();

    if (this._isUserPage) {
      // /user：登录用户也展示上传页
      this.switchView("upload");
      this.loadRecentSubs();
    } else if (data.role === "uploader") {
      // /manager：uploader 仅看内容上传页
      this.switchView("upload");
      this.loadRecentSubs();
    } else {
      // /manager：从驾驶舱返回时恢复到上次所在视图；否则默认全国战况
      let lastView = null;
      try { lastView = sessionStorage.getItem("jk_last_view"); } catch (e) {}
      const isAdmin = data.role === "admin";
      const allowed = isAdmin
        ? ["regionperf", "subdash", "dashboard", "channels", "rules", "modelcfg", "review", "admin", "usage_log"]
        : ["regionperf", "subdash", "dashboard", "channels", "rules", "modelcfg", "review"];
      this.switchView(lastView && allowed.includes(lastView) ? lastView : "regionperf");
    }
    this.loadNotifs();
    this._prepRegions();
    document.addEventListener("click", this._clickOutside);
  },

  async _loadTracks() {
    try {
      const { data } = await this._api("/api/campaign/tracks");
      const tracks = (data && data.tracks) || [];
      const sel = document.getElementById("upTrack");
      if (sel) {
        sel.innerHTML = tracks.map(t => `<option value="${t.name}">${t.name}</option>`).join('') +
          '<option value="">不参与双百战役</option>';
        // 默认参加：默认选中第一条赛道
        if (tracks.length) sel.value = tracks[0].name;
      }
    } catch (e) {}
  },

  _renderGuest() {
    // 隐藏侧边栏和通知铃，显示顶条
    document.getElementById("sidebar").style.display = "none";
    document.getElementById("notifBell").style.display = "none";
    document.getElementById("guestBar").style.display = "";
    try { this.switchView("upload"); } catch(e) { console.error("[renderGuest] switchView error:", e); }
    this.loadRecentSubs();
  },

  _clickOutside(e) {
    // 关闭通知面板
    const panel = document.getElementById("notifPanel");
    const bell = document.getElementById("notifBell");
    if (panel && bell && panel.style.display !== "none" && !panel.contains(e.target) && !bell.contains(e.target)) {
      panel.style.display = "none";
    }
  },

  // 预加载区域数据到上传页下拉
  _prepRegions() {
    // populate region dropdowns if needed
    const subs = document.getElementById("subFilterSub");
    if (subs && subs.options.length <= 1) {
      for (const big of Object.keys(this.regions || {})) {
        for (const sub of (this.regions[big] || [])) {
          const o = document.createElement("option"); o.value = sub; o.textContent = sub;
          subs.appendChild(o);
        }
      }
    }
  },

  _renderSidebar() {
    const u = this.me;
    const roleLabel = u.role === "admin" ? "超级管理员" : u.role === "manager" ? "在地营销经理" : "区域经理";
    const initial = (u.display_name || u.username || "?").trim().charAt(0);
    document.getElementById("sideUser").innerHTML = `
      <span class="side-avatar">${this.esc(initial)}</span>
      <span class="side-user-meta">
        <span class="name">${this.esc(u.display_name || u.username)}</span>
        <span class="role">${roleLabel}</span>
      </span>`;

    // 按角色过滤导航项（主菜单 + 底部菜单）
    document.querySelectorAll("#sideNav > a, #sideNav > .sep, #sideBottom > a, #sideBottom > .sep").forEach(el => {
      const role = el.dataset.role || "";
      if (role === "all") { el.style.display = ""; return; }
      const roles = role.split(",");
      el.style.display = roles.includes(u.role) ? "" : "none";
    });

    // uploader 只有一个菜单项（内容上传），自动设为 active
    if (u.role === "uploader") {
      document.querySelectorAll("#sideNav > a").forEach(a => a.classList.remove("active"));
      const uploadLink = document.querySelector('#sideNav > a[data-view="upload"]');
      if (uploadLink) uploadLink.classList.add("active");
    }
  },

  _bindNav() {
    document.querySelectorAll(".side-nav a, .side-bottom a[data-view]").forEach(a => {
      a.addEventListener("click", () => this.switchView(a.dataset.view));
    });
  },

  logout() {
    this._api("/api/auth/logout", { method: "POST" }).then(() => {
      location.href = "/user";
    });
  },

  changePassword() {
    document.getElementById("pwdOld").value = "";
    document.getElementById("pwdNew").value = "";
    document.getElementById("pwdConfirm").value = "";
    document.getElementById("pwdMsg").style.display = "none";
    document.getElementById("pwdModal").classList.remove("hidden");
  },

  closePwdModal() {
    document.getElementById("pwdModal").classList.add("hidden");
  },

  async doChangePassword() {
    const oldPwd = document.getElementById("pwdOld").value;
    const newPwd = document.getElementById("pwdNew").value;
    const confirm = document.getElementById("pwdConfirm").value;
    const msg = document.getElementById("pwdMsg");

    msg.style.display = "none";
    if (!oldPwd) { msg.textContent = "请输入旧密码"; msg.style.display = "block"; return; }
    if (!newPwd || newPwd.length < 4) { msg.textContent = "新密码至少4位"; msg.style.display = "block"; return; }
    if (newPwd !== confirm) { msg.textContent = "两次输入的新密码不一致"; msg.style.display = "block"; return; }

    try {
      const { ok, data } = await this._api("/api/auth/change-password", {
        method: "PUT",
        body: JSON.stringify({ old_password: oldPwd, new_password: newPwd }),
        headers: { "Content-Type": "application/json" },
      });
      if (ok) {
        msg.style.color = "var(--success)";
        msg.textContent = "密码修改成功！";
        msg.style.display = "block";
        setTimeout(() => this.closePwdModal(), 1200);
      } else {
        msg.textContent = (data && data.detail) || "修改失败，请检查旧密码";
        msg.style.display = "block";
      }
    } catch (e) {
      msg.textContent = "请求失败：" + e.message;
      msg.style.display = "block";
    }
  },

  /* =========================================================
     视图切换
     ========================================================= */
  switchView(name) {
    document.querySelectorAll(".side-nav a, .side-bottom a[data-view]").forEach(a => a.classList.toggle("active", a.dataset.view === name));
    document.querySelectorAll(".pane").forEach(p => p.classList.toggle("active", p.id === "pane-" + name));

    const titles = {
      dashboard: "看板总览", subdash: "提交审核", upload: "内容审核",
      regionperf: "全国战况", channels: "视频号管理", rules: "合规规则", modelcfg: "模型配置",
      review: "审核管理", admin: "用户管理", usage_log: "使用日志",
    };
    const vt = document.getElementById("viewTitle");
    if (vt) vt.textContent = titles[name] || "南孚在地营销";

    const loaders = {
      dashboard: () => this.loadDashboard(),
      subdash: () => this.loadSubDashboard(),
      regionperf: () => this.loadRegionOverview(),
      channels: () => this.loadChannels(),
      upload: () => {
        this._setupDragDrop();
        // 未登录用户：显示姓名+区域输入框，每次都能自由选择
        const gf = document.getElementById("guestFields");
        if (gf) gf.style.display = this.me ? "none" : "";
        this._populateSubBig();
        // 提交审核表格仅在登录用户时加载（guest 页面没有 subTbody 等元素）
        if (this.me) this.loadSubmissions();
        // 始终加载近期提交
        this.loadRecentSubs();
      },
      rules: () => this.loadRules(),
      modelcfg: () => this.loadModelCfg(),
      review: () => { this._populateRevBig(); this.loadReview(); },
      admin: () => this.loadAdminUsers(),
      usage_log: () => this.loadUsageLog(),
    };
    if (loaders[name]) { try { loaders[name](); } catch(e) { console.error("[switchView] loader error:", name, e); } }
  },

  /* =========================================================
     看板总览
     ========================================================= */
  async loadDashboard() {
    const isAdmin = this.me && this.me.role === "admin";
    const isMgr   = isAdmin || (this.me && this.me.role === "manager");

    // ── 按角色显示/隐藏卡片 ──
    document.querySelectorAll(".is-super").forEach(el => el.style.display = isAdmin ? "" : "none");
    document.querySelectorAll(".is-mgr").forEach(el => el.style.display = isMgr ? "" : "none");

    // ── 并行拉取所有数据 ──
    const promises = [this._api("/api/dashboard/stats")];
    if (isAdmin) promises.push(this._api("/api/dashboard/overview"));
    const [statsRes, ovRes] = await Promise.all(promises);
    const data = statsRes.data;
    if (!data) return;

    // 统计卡片 — row1（采集系统）
    const labels = ["监控视频号", "采集内容", "违规内容", "合规内容", "待人工复核"];
    const colors = ["var(--primary)", "var(--text)", "var(--red)", "var(--green)", "var(--orange)"];
    const icons = [
      '<svg class="ic" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8"/><path d="M12 16v4"/></svg>',
      '<svg class="ic" viewBox="0 0 24 24"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/></svg>',
      '<svg class="ic" viewBox="0 0 24 24"><path d="M12 3 5 6v5c0 4.5 3 7.5 7 9 4-1.5 7-4.5 7-9V6z"/></svg>',
      '<svg class="ic" viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5"/></svg>',
      '<svg class="ic" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>',
    ];
    const vals = [data.channel_count || 0,
            data.content_count || 0,
            data.violation_count || 0,
            data.compliant_count || 0,
            data.manual_review_count || 0];
    document.getElementById("statGrid").innerHTML = labels.map((l, i) =>
      `<div class="stat-card"><span class="stat-ic" style="color:${colors[i]}">${icons[i]}</span><span class="stat-meta"><span class="num" style="color:${colors[i]}">${vals[i]}</span><span class="lbl">${l}</span></span></div>`
    ).join("");

    // 统计卡片 — row2（提交侧）
    if (data.submission_total != null) {
      const sl = ["提交总数", "已通过", "已驳回", "待复核"];
      const sv = [data.submission_total, data.submission_approved, data.submission_rejected, data.submission_manual_review];
      const sc = ["var(--primary)", "var(--green)", "var(--red)", "var(--orange)"];
      const sic = [
        '<svg class="ic" viewBox="0 0 24 24"><path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5 4h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z"/></svg>',
        '<svg class="ic" viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5"/></svg>',
        '<svg class="ic" viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg>',
        '<svg class="ic" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>',
      ];
      document.getElementById("statGridSub").innerHTML = sl.map((l, i) =>
        `<div class="stat-card"><span class="stat-ic" style="color:${sc[i]}">${sic[i]}</span><span class="stat-meta"><span class="num" style="color:${sc[i]}" data-target="${sv[i] || 0}">0</span><span class="lbl">${l}</span></span></div>`
      ).join("");
      this.animateNumbers(document.getElementById("statGridSub"));

      // 区域提交统计表（仅 admin）
      if (isAdmin) {
        const regions = data.by_region || [];
        document.getElementById("regionTbody").innerHTML = regions.map(r =>
          `<tr><td>${this.esc(r.region)}</td><td style="font-weight:600">${r.count}</td></tr>`
        ).join("") || '<tr><td colspan="2" style="color:var(--muted);text-align:center">暂无数据</td></tr>';
      }
    }

    // 风险分布
    const rd = data.risk_distribution || {};
    const rc = { red: "#f43f5e", yellow: "#f59e0b", green: "#10b981", none: "#c9ae7e",
                 forbidden: "#f43f5e", high: "#f43f5e", medium: "#f59e0b", low: "#10b981" };
    const rl = { red: "红灯", yellow: "黄灯", green: "绿灯", none: "未标",
                 forbidden: "严重", high: "高风险", medium: "中风险", low: "低风险" };
    const total = Object.values(rd).reduce((a, b) => (a || 0) + (b || 0), 0) || 1;
    document.getElementById("riskBars").innerHTML = Object.entries(rl).map(([k, lbl]) => {
      const cnt = rd[k] || 0;
      const pct = (cnt / total * 100).toFixed(0);
      return `<div class="risk-row"><div class="name">${lbl}</div><div class="bar"><span style="width:${pct}%;background:${rc[k] || '#c9ae7e'}"></span></div><div class="cnt" style="color:${rc[k] || '#c9ae7e'}">${cnt}</div></div>`;
    }).join("");

    // 视频号概览（仅 super，数据已在上面并行拉取）
    if (isAdmin && ovRes && ovRes.data) {
      const ov = ovRes.data;
      const ovTbody = document.getElementById("overviewTbody");
      if (ovTbody) {
        ovTbody.innerHTML = ov.map(c => `
          <tr><td>${this.esc(c.name)}</td><td>${c.active ? "✅" : "⏸️"}</td>
            <td>${c.content_count}</td><td style="color:var(--red)">${c.violation_count}</td>
            <td>${c.compliant_rate != null ? c.compliant_rate + "%" : "-"}</td></tr>`).join("");
      }
    }

    // 采集内容明细（始终加载，所有角色可见）
    {
      // 大区选择器（二级联动）
      const selBig = document.getElementById("fdRegionBig");
      if (selBig && selBig.options.length <= 1) {
        selBig.innerHTML = '<option value="">全部大区</option>';
        for (const big of Object.keys(this.regions || {})) {
          const o = document.createElement("option"); o.value = big; o.textContent = big; selBig.appendChild(o);
        }
      }
      // manager 自动过滤到所属区域
      if (!isAdmin && this.me && this.me.region && selBig) {
        const parts = (this.me.region || "").split("/");
        selBig.value = parts[0] || "";
        this.onFdRegionBig();
        const selSub = document.getElementById("fdRegionSub");
        if (selSub) selSub.value = parts[1] || "";
      }
    }
    this.renderFeed(1, "dashboard");
  },

  /* =========================================================
     提交审核看板（所有角色可见）
     ========================================================= */
  async loadSubDashboard() {
    // 初始化大区筛选下拉
    const db = document.getElementById("subdashRegionBig");
    if (db && db.options.length <= 1) {
      for (const big of Object.keys(this.regions || {})) {
        const o = document.createElement("option"); o.value = big; o.textContent = big; db.appendChild(o);
      }
    }
    // 初始化赛道筛选下拉
    const dt = document.getElementById("subdashTrack");
    if (dt && dt.options.length <= 1) {
      try {
        const { data: tr } = await this._api("/api/campaign/tracks");
        (tr?.tracks || []).forEach(t => { const o = document.createElement("option"); o.value = t.name; o.textContent = t.name; dt.appendChild(o); });
      } catch (e) {}
    }
    // 并行拉取统计 + 列表
    const [statsRes] = await Promise.all([
      this._api("/api/dashboard/stats"),
      this._loadSubdashTable(1),
    ]);
    const data = statsRes.data;
    if (!data) return;

    const role = this.me?.role;

    // 上下文提示
    const ctx = document.getElementById("subdashContext");
    if (ctx) {
      if (role === "uploader") {
        ctx.style.display = "";
        ctx.textContent = `你提交的内容（共 ${data.submission_total || 0} 条）`;
      } else if (role === "manager" && this.me?.region) {
        ctx.style.display = "";
        ctx.textContent = `你的管辖区域: ${this.me.region}（仅显示本区域提交）`;
      } else if (role === "admin") {
        ctx.style.display = "";
        ctx.textContent = `全国提交数据（共 ${data.submission_total || 0} 条）`;
      }
    }

    // 统计卡片
    const labels = ["总提交", "待审核", "分析中", "已通过", "已驳回", "待复核"];
    const colors = [
      "var(--primary)", "var(--muted)", "var(--primary)",
      "var(--green)", "var(--red)", "var(--orange)",
    ];
    const vals = [
      data.submission_total || 0,
      data.pending_count || 0,
      data.analyzing_count || 0,
      data.submission_approved || 0,
      data.submission_rejected || 0,
      data.submission_manual_review || 0,
    ];

    document.getElementById("subdashStatGrid").innerHTML = labels.map((l, i) =>
      `<div class="stat-card"><div class="num" style="color:${colors[i]}">${vals[i]}</div><div class="label">${l}</div></div>`
    ).join("");

    // 风险分布条（数据来源：内容上传提交）
    const rd = data.submission_risk_distribution || data.risk_distribution || {};
    const totalRisk = (rd.red || 0) + (rd.yellow || 0) + (rd.green || 0) || 1;
    const rc = { red: "var(--red)", yellow: "var(--orange)", green: "var(--green)" };
    const rl = { red: "违规", yellow: "疑似", green: "合规" };
    document.getElementById("subdashRiskBars").innerHTML = ["red", "yellow", "green"].map(k => {
      const cnt = rd[k] || 0;
      const pct = (cnt / totalRisk * 100).toFixed(0);
      return `<div class="risk-row"><div class="name">${rl[k]}</div><div class="bar"><span data-width="${pct}" style="width:0;background:${rc[k]}"></span></div><div class="cnt" style="color:${rc[k]}">${cnt}</div></div>`;
    }).join("");
    this.animateBars(document.getElementById("subdashRiskBars"));
  },

  async _loadSubdashTable(page = 1) {
    const status = document.getElementById("subdashFilterStatus")?.value || "";
    const campaign = document.getElementById("subdashCampaign")?.value || "";
    const track = document.getElementById("subdashTrack")?.value || "";
    const big = document.getElementById("subdashRegionBig")?.value || "";
    const sub = document.getElementById("subdashRegionSub")?.value || "";
    const region = big && sub ? `${big} / ${sub}` : (big || "");
    const params = new URLSearchParams({ page, page_size: 10 });
    if (status) params.set("status", status);
    if (campaign) params.set("campaign", campaign);
    if (track) params.set("track", track);
    if (region) params.set("region", region);

    const { data } = await this._api("/api/submissions?" + params);
    if (!data || !data.items) return;

    const cn = { pending: "待审核", analyzing: "分析中", approved: "已通过", rejected: "已驳回", manual_review: "待复核" };
    const bc = { approved: "badge-green", rejected: "badge-red", manual_review: "badge-yellow", pending: "badge-muted", analyzing: "badge-muted" };

    document.getElementById("subdashTbody").innerHTML = data.items.map(s => {
      const media = s.media || [];
      const hasVideo = media.some(m => m.is_video);
      const hasImage = media.some(m => !m.is_video);
      const mediaIcon = hasVideo ? "🎬" : (hasImage ? "🖼️" : "📄");
      return `
      <tr onclick="App.showDetail(${s.id})" style="cursor:pointer">
        <td>${mediaIcon} ${this.esc(s.title || s.caption || "(无标题)")}</td>
        <td>${this.esc(s.display_name || s.username || "访客")}</td>
        <td>${this.esc(s.region || "")}</td>
        <td style="font-size:12px;color:var(--muted)">${(s.created_at || "").substring(0, 16)}</td>
        <td><span class="badge ${bc[s.status] || 'badge-muted'}">${cn[s.status] || s.status}</span></td>
        <td><button class="btn sm danger" onclick="event.stopPropagation();App.deleteSub(${s.id})" title="删除">删除</button></td>
      </tr>`;
    }).join("") || '<tr><td colspan="6" style="color:var(--muted);text-align:center;padding:20px">暂无提交</td></tr>';

    const tp = Math.ceil(data.total / 10) || 1;
    let ph = "";
    if (page > 1) ph += `<button onclick="App._loadSubdashTable(${page - 1})">上一页</button>`;
    ph += `<span>${page}/${tp} 页，共 ${data.total} 条</span>`;
    if (page < tp) ph += `<button onclick="App._loadSubdashTable(${page + 1})">下一页</button>`;
    document.getElementById("subdashPagination").innerHTML = ph;

    const el = document.getElementById("subdashCount");
    if (el) el.textContent = `共 ${data.total} 条`;
  },

  onSubdashRegionBig() {
    const big = document.getElementById("subdashRegionBig")?.value || "";
    const sub = document.getElementById("subdashRegionSub");
    if (!sub) return;
    sub.innerHTML = '<option value="">全部区域</option>';
    if (big && this.regions[big]) {
      this.regions[big].forEach(s => { const o = document.createElement("option"); o.value = s; o.textContent = s; sub.appendChild(o); });
    }
    this._loadSubdashTable(1);
  },

  /* =========================================================
     区域业绩（三级下钻：大区 → 细分区域 → 作品/线索）
     ========================================================= */
  async loadRegionOverview() {
    const bc = document.getElementById("rpBreadcrumb");
    if (bc) bc.textContent = "全国战况 · 区域业绩";
    const body = document.getElementById("rpBody");
    if (!body) return;
    body.innerHTML = this.skeletonCards(6);
    const { data } = await this._api("/api/campaign/region/overview");
    if (!body) return;
    const zones = data || [];
    if (!zones.length) {
      body.innerHTML = '<div class="empty">暂无区域数据</div>';
      return;
    }
    const meritMap = {};
    zones.forEach(z => { meritMap[z.region] = z.total_merit || 0; });
    const maxMerit = Math.max(...zones.map(z => z.total_merit || 0), 1);

    const sum = k => zones.reduce((s, z) => s + (z[k] || 0), 0);

    const ALL_ZONES = ["华东区", "华北区", "华南区", "东北区", "西一区", "西二区"];
    const rankColors = ["#9f1239", "#1e3a8a", "#b45309", "#475569", "#64748b", "#94a3b8"];
    const zoneRank = {};
    zones.forEach((z, i) => { zoneRank[z.region] = i; });
    const legendHtml = ALL_ZONES.map(z => {
      const idx = zoneRank[z];
      const color = idx != null ? rankColors[idx] : "#e8edf4";
      const val = idx != null ? zones[idx].total_merit : "0";
      return `<div class="lg-item" onclick="App.drillSubregions('${z}')"><i style="background:${color}"></i><span>${z}</span><em>${val}</em></div>`;
    }).join("");

    body.innerHTML = `
      <div class="rp-kpis rise-in">
        ${this.kpiCard('<path d="M12 3 5 6v5c0 4.5 3 7.5 7 9 4-1.5 7-4.5 7-9V6z"/>', "总战功", sum("total_merit"), "#2563eb", "#eff6ff")}
        ${this.kpiCard('<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/>', "总作品", sum("work_count"), "#0ea5e9", "#f0f9ff")}
        ${this.kpiCard('<path d="M12 3l2.7 5.6 6.1.8-4.5 4.2 1.1 6.1L12 16.8 6.6 19.7l1.1-6.1L3.2 9.4l6.1-.8z"/>', "S·A 级作品", sum("s_a_count"), "#f59e0b", "#fffbeb")}
        ${this.kpiCard('<path d="M17 21v-2a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v2"/><circle cx="10" cy="7" r="4"/>', "有效创作者", sum("valid_creator_count"), "#16a34a", "#f0fdf4")}
        ${this.kpiCard('<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/><path d="M11 8v6M8 11h6"/>', "有效线索", sum("lead_count"), "#8b5cf6", "#f5f3ff")}
        ${this.kpiCard('<path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0z"/><circle cx="12" cy="10" r="3"/>', "参与大区", zones.length, "#0d9488", "#f0fdfa")}
      </div>
      <div class="warzone-wrap">
        <div class="warzone-map">
          <div class="map-title"><span class="dot"></span><b>全国战况地图</b><span>· 点击大区下钻</span></div>
          <div id="chinaMap" style="width:100%;height:100%"></div>
          <div class="map-legend">${legendHtml}</div>
          <div class="map-zoom">
            <button type="button" onclick="App.zoomMap(1.5)" title="放大">＋</button>
            <button type="button" onclick="App.zoomMap(0.67)" title="缩小">－</button>
            <button type="button" onclick="App.resetMap()" title="重置">↺</button>
          </div>
        </div>
        <div class="warzone-rank">
          <div class="wz-head">
            <div>
              <h3>大区战功榜</h3>
              <p>按 100 分制综合排名</p>
            </div>
            <span class="wz-badge">${zones.length} 大区</span>
          </div>
          <div class="wz-list">
            ${zones.map((z, i) => {
              const cls = i === 0 ? "gold" : i === 1 ? "silver" : i === 2 ? "bronze" : "";
              return `
              <div class="wz-row ${cls}" onclick="App.drillSubregions('${this.esc(z.region)}')">
                <span class="wz-idx ${cls}">${i + 1}</span>
                <div class="wz-main">
                  <div class="wz-name"><span>${this.esc(z.region)}</span><em>${z.total}分</em></div>
                  <div class="wz-bar"><i data-width="${Math.round((z.total_merit || 0) / maxMerit * 100)}" style="width:0"></i></div>
                  <div class="wz-sub">作品 ${z.work_count} · S·A ${z.s_a_count} · 创作者 ${z.valid_creator_count} · 线索 ${z.lead_count}</div>
                </div>
                <div class="wz-val"><b>${z.total_merit}</b><small>战功</small></div>
              </div>`;
            }).join("")}
          </div>
          <div class="wz-tip">点击大区下钻查看细分区域业绩</div>
        </div>
      </div>`;

    this._renderChinaMap(zones, meritMap);
    this.animateNumbers(body);
    this.animateBars(body);
  },

  async _renderChinaMap(zones, meritMap) {
    const el = document.getElementById("chinaMap");
    if (!el) return;
    if (typeof echarts === "undefined") {
      el.innerHTML = '<div style="padding:40px;text-align:center;color:var(--muted)">地图组件加载失败，请刷新重试</div>';
      return;
    }
    const ZONE_BY_KEYWORD = [
      ["华东区", ["上海", "江苏", "浙江", "安徽"]],
      ["华北区", ["北京", "天津", "河北", "山西", "山东", "河南"]],
      ["华南区", ["广东", "海南", "湖北", "湖南", "江西", "福建"]],
      ["东北区", ["黑龙江", "吉林", "辽宁", "内蒙古"]],
      ["西一区", ["重庆", "四川", "陕西", "甘肃", "青海", "宁夏", "西藏", "新疆"]],
      ["西二区", ["贵州", "广西", "云南"]],
    ];
    const zoneOf = name => {
      for (const [z, kws] of ZONE_BY_KEYWORD) {
        if (kws.some(k => name.includes(k))) return z;
      }
      return "";
    };
    const rankColors = ["#9f1239", "#1e3a8a", "#b45309", "#475569", "#64748b", "#94a3b8"];
    const zoneColor = {};
    zones.forEach((z, i) => { zoneColor[z.region] = rankColors[i] || "#94a3b8"; });

    let geo;
    try {
      const res = await fetch("/china.json");
      geo = await res.json();
    } catch (e) {
      el.innerHTML = '<div style="padding:40px;text-align:center;color:var(--muted)">地图数据加载失败</div>';
      return;
    }

    if (this._chinaChart) { this._chinaChart.dispose(); this._chinaChart = null; }
    echarts.registerMap("china", geo);
    const chart = echarts.init(el);
    this._chinaChart = chart;

    const option = {
      tooltip: {
        trigger: "item",
        formatter: p => {
          const z = zoneOf(p.name);
          const v = meritMap[z];
          return z
            ? `${p.name}<br/>${z}：战功 ${v != null ? v : "暂无数据"}`
            : `${p.name}<br/>未分区`;
        },
      },
      series: [{
        type: "map",
        map: "china",
        roam: true,
        scaleLimit: { min: 0.8, max: 6 },
        zoom: 1.2,
        label: { show: true, fontSize: 11, color: "#4b5563" },
        itemStyle: { borderColor: "#ffffff", borderWidth: 1 },
        emphasis: {
          label: { show: true, color: "#111", fontWeight: "bold" },
          itemStyle: { areaColor: "#fecaca" },
        },
        data: geo.features.map(f => {
          const pname = f.properties.name || "";
          const z = zoneOf(pname);
          const hasData = z && meritMap[z] != null;
          return {
            name: pname,
            value: meritMap[z] || 0,
            itemStyle: hasData
              ? { areaColor: zoneColor[z] }
              : (z ? { areaColor: "#e8edf4" } : { areaColor: "#f1f5f9" }),
          };
        }),
      }],
    };
    this._chinaBaseOption = option;
    chart.setOption(option);

    chart.on("click", p => {
      const z = zoneOf(p.name);
      if (z && meritMap[z] != null) this.drillSubregions(z);
    });
    window.addEventListener("resize", () => chart.resize());
  },

  zoomMap(factor) {
    const chart = this._chinaChart;
    if (!chart) return;
    const cur = (chart.getOption().series && chart.getOption().series[0] && chart.getOption().series[0].zoom) || 1;
    const next = Math.min(6, Math.max(0.8, cur * factor));
    chart.setOption({ series: [{ zoom: next }] });
  },

  resetMap() {
    const chart = this._chinaChart;
    if (!chart) return;
    if (this._chinaBaseOption) chart.setOption(this._chinaBaseOption, true);
  },

  async drillSubregions(big) {
    const bc = document.getElementById("rpBreadcrumb");
    if (bc) bc.innerHTML = `<a href="javascript:void(0)" onclick="App.loadRegionOverview()">全部大区</a><span class="sep">›</span>${this.esc(big)}`;
    const body = document.getElementById("rpBody");
    if (body) body.innerHTML = this.skeletonLines(6);
    const { data } = await this._api("/api/campaign/region/subregions?big=" + encodeURIComponent(big));
    if (!body) return;
    if (!data || !data.length) {
      body.innerHTML = '<div class="empty">该大区暂无细分区域数据</div>';
      return;
    }
    const sum = k => data.reduce((s, x) => s + (x[k] || 0), 0);
    body.innerHTML = `
      <div class="rp-kpis rise-in">
        ${this.kpiCard('<path d="M12 3 5 6v5c0 4.5 3 7.5 7 9 4-1.5 7-4.5 7-9V6z"/>', "总战功", sum("total_merit"), "#2563eb", "#eff6ff")}
        ${this.kpiCard('<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/>', "总作品", sum("work_count"), "#0ea5e9", "#f0f9ff")}
        ${this.kpiCard('<path d="M12 3l2.7 5.6 6.1.8-4.5 4.2 1.1 6.1L12 16.8 6.6 19.7l1.1-6.1L3.2 9.4l6.1-.8z"/>', "S·A 级作品", sum("s_a_count"), "#f59e0b", "#fffbeb")}
        ${this.kpiCard('<path d="M17 21v-2a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v2"/><circle cx="10" cy="7" r="4"/>', "有效创作者", sum("valid_creator_count"), "#16a34a", "#f0fdf4")}
        ${this.kpiCard('<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/><path d="M11 8v6M8 11h6"/>', "有效线索", sum("lead_count"), "#8b5cf6", "#f5f3ff")}
        ${this.kpiCard('<path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0z"/><circle cx="12" cy="10" r="3"/>', "细分区域", data.length, "#0d9488", "#f0fdfa")}
      </div>
      <div class="table-scroll rise-in"><table class="data-table">
      <thead><tr><th>细分区域</th><th>作品</th><th>S·A</th><th>创作者</th><th>战功</th><th>有效线索</th></tr></thead>
      <tbody>` + data.map(s => `
        <tr onclick="App.drillRegionDetail('${this.esc(s.region)}')" style="cursor:pointer">
          <td>${this.esc(s.sub_region)}</td>
          <td class="num">${s.work_count}</td>
          <td class="num">${s.s_a_count}</td>
          <td class="num">${s.valid_creator_count}</td>
          <td class="num" style="color:var(--primary);font-weight:600">${s.total_merit}</td>
          <td class="num">${s.lead_count}</td>
        </tr>`).join("") + `
      </tbody></table></div>`;
    this.animateNumbers(body);
  },

  async drillRegionDetail(fullRegion) {
    const bigName = (fullRegion || "").split("/")[0].trim();
    const bc = document.getElementById("rpBreadcrumb");
    if (bc) bc.innerHTML = `<a href="javascript:void(0)" onclick="App.loadRegionOverview()">全部大区</a><span class="sep">›</span><a href="javascript:void(0)" onclick="App.drillSubregions('${this.esc(bigName)}')">${this.esc(bigName)}</a><span class="sep">›</span>${this.esc(fullRegion)}`;
    const body = document.getElementById("rpBody");
    if (body) body.innerHTML = this.skeletonLines(6);
    const [worksRes, credsRes] = await Promise.all([
      this._api("/api/campaign/works?region=" + encodeURIComponent(fullRegion)),
      this._api("/api/campaign/credentials?region=" + encodeURIComponent(fullRegion)),
    ]);
    if (!body) return;
    const works = worksRes.data || [];
    const creds = credsRes.data || [];
    const tp = { screenshot: "后台数据截图", business: "有效线索回传" };
    const st = { pending_review: "待初审", pending_t7: "待T+7", qualified: "已计分", eliminated: "淘汰", disqualified: "已清零" };
    const qualifiedCount = works.filter(w => w.status === "qualified").length;
    const totalMerit = works.reduce((s, w) => s + (w.total_merit || 0), 0);
    const businessCount = creds.filter(c => c.type === "business").length;
    body.innerHTML = `
      <div class="rp-kpis rise-in col-4">
        ${this.kpiCard('<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/>', "作品总数", works.length, "#0ea5e9", "#f0f9ff")}
        ${this.kpiCard('<path d="M22 11.1V12a10 10 0 1 1-5.9-9.1"/><path d="m9 11 3 3L22 4"/>', "已计分作品", qualifiedCount, "#16a34a", "#f0fdf4")}
        ${this.kpiCard('<path d="M12 3 5 6v5c0 4.5 3 7.5 7 9 4-1.5 7-4.5 7-9V6z"/>', "总战功", totalMerit, "#2563eb", "#eff6ff")}
        ${this.kpiCard('<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/><path d="M11 8v6M8 11h6"/>', "有效线索", businessCount, "#8b5cf6", "#f5f3ff")}
      </div>
      <div class="rise-in">
      <div class="card"><h3>作品明细（${works.length}）</h3>
        <div class="table-scroll"><table class="data-table">
          <thead><tr><th>作品码</th><th>标题</th><th>创作者</th><th>赛道</th><th>状态</th><th>战功</th></tr></thead>
          <tbody>` + (works.length ? works.map(w => `
            <tr><td><code>${this.esc(w.code || "")}</code></td>
            <td>${this.esc((w.title || "").slice(0, 20))}</td>
            <td>${this.esc(w.creator_name || "")}</td>
            <td>${this.esc(w.track || "")}</td>
            <td>${st[w.status] || w.status || "-"}</td>
            <td class="num">${w.total_merit || 0}</td></tr>`).join("") : '<tr><td colspan="6" style="color:var(--muted);text-align:center">暂无作品</td></tr>') + `
          </tbody>
        </table></div>
      </div>
      <div class="card"><h3>有效线索明细（${creds.length}）</h3>
        <div class="table-scroll"><table class="data-table">
          <thead><tr><th>作品码</th><th>类型</th><th>凭证</th><th>时间</th></tr></thead>
          <tbody>` + (creds.length ? creds.map(c => `
            <tr><td><code>${this.esc(c.code || "")}</code></td>
            <td><span class="badge ${c.type === "business" ? "badge-green" : "badge-blue"}">${tp[c.type] || c.type}</span></td>
            <td>${c.url ? `<a href="${this.esc(c.url)}" target="_blank"><img src="${this.esc(c.url)}" style="width:36px;height:36px;object-fit:cover;border-radius:6px"></a>` : this.esc(c.file || "")}</td>
            <td style="font-size:12px;color:var(--muted)">${(c.created_at || "").slice(0, 16)}</td></tr>`).join("") : '<tr><td colspan="4" style="color:var(--muted);text-align:center">暂无有效线索</td></tr>') + `
          </tbody>
        </table></div>
      </div>
      </div>`;
    this.animateNumbers(body);
  },

  async renderFeed(page = 1, caller = "") {
    this.feedData.page = page;

    // 并行拉取：频道列表（首次）+ 内容列表
    const fdCh = document.getElementById("fdChannel");
    const needChannels = fdCh && fdCh.options.length <= 1;
    const feedParams = new URLSearchParams({ page, page_size: 20 });
    const channel = fdCh?.value || "";
    const status = document.getElementById("fdStatus")?.value || "";
    const big = document.getElementById("fdRegionBig")?.value || "";
    const sub = document.getElementById("fdRegionSub")?.value || "";
    const region = sub || big;
    const sortVal = document.getElementById("fdSort")?.value || "";
    const search = (document.getElementById("fdSearch")?.value || "").trim();
    if (channel) feedParams.set("channel_id", channel);
    if (status) feedParams.set("risk_level", status);
    if (region) feedParams.set("region", region);
    if (sortVal) {
      const parts = sortVal.split("_");
      feedParams.set("sort_by", parts[0]);
      feedParams.set("sort_order", parts[1] || "desc");
    }
    if (search) feedParams.set("search", search);

    const fetches = [this._api("/api/contents/all?" + feedParams)];
    if (needChannels) fetches.push(this._api("/api/channels"));
    const [feedRes, chRes] = await Promise.all(fetches);

    if (needChannels && chRes && chRes.data && fdCh) {
      fdCh.innerHTML = '<option value="">全部视频号</option>' +
        chRes.data.map(ch => `<option value="${ch.id}">${this.esc(ch.name)}</option>`).join("");
    }

    try {
      const { ok, data } = feedRes;
      if (!ok || !data || !data.items) {
        document.getElementById("contentFeed").innerHTML = '<p style="color:var(--muted);grid-column:1/-1;text-align:center;padding:40px">暂无采集内容，请先 <a href="#" onclick="App.switchView(\'channels\')">视频号管理</a> 中采集</p>';
        document.getElementById("feedCount").textContent = "共 0 条";
        return;
      }
      this.feedData.items = data.items;
      this.feedData.total = data.total;
      document.getElementById("feedCount").textContent = `共 ${data.total} 条`;

      const tagColor = { red: "#dc2626", yellow: "#f59e0b", green: "#16a34a", forbidden: "#dc2626", none: "#d1d5db" };
      const tagsLabel = { red: "违规", yellow: "疑似", green: "合规", forbidden: "违规", none: "" };
      const cards = data.items;
      document.getElementById("contentFeed").innerHTML = cards.length
        ? cards.map(c => {
          const thumb = c.thumb_url || "";
          const stats = c.stats || {};
          const ai = c.ai_review || {};
          const aiBrief = ai.interpretation ? ai.interpretation.substring(0, 60) + "…" : "";
          return `
          <div class="content-card" onclick="App.showContentDetail(${c.id})">
            <div class="media-cell">${thumb ? `<img src="${this.esc(thumb)}" loading="lazy" onerror="this.parentElement.innerHTML='<span style=\\'color:var(--muted);font-size:32px\\'>${c.is_video ? '🎬' : '📄'}</span>'" />` : `<span style="color:var(--muted);font-size:32px">${c.is_video ? '🎬' : '📄'}</span>`}</div>
            <div class="ctext">${this.esc(c.caption || c.title || "(无内容)")}</div>
            <div class="cmeta">
              <span style="display:inline-flex;align-items:center"><span style="width:8px;height:8px;border-radius:50%;background:${tagColor[c.risk_level]||'#d1d5db'};margin-right:5px;display:inline-block"></span>${tagsLabel[c.risk_level] || c.risk_level || "未标"}</span>
              <span>👁 ${stats.read || 0} ❤ ${stats.like || 0}</span>
            </div>
            ${aiBrief ? `<div style="font-size:11px;color:var(--muted);padding:4px 8px;line-height:1.4">🤖 ${this.esc(aiBrief)}</div>` : ""}
          </div>`;
        }).join("")
        : '<p style="color:var(--muted);grid-column:1/-1;text-align:center;padding:40px">该区域暂无采集内容</p>';

      const tp = Math.ceil(data.total / 20) || 1;
      let ph = "";
      if (page > 1) ph += `<button onclick="App.renderFeed(${page - 1})">上一页</button>`;
      ph += `<span>${page}/${tp} 页</span>`;
      if (page < tp) ph += `<button onclick="App.renderFeed(${page + 1})">下一页</button>`;
      document.getElementById("feedPagination").innerHTML = ph;
    } catch (e) {
      console.error("renderFeed error:", e);
      document.getElementById("contentFeed").innerHTML = `<p style="color:var(--red);grid-column:1/-1;text-align:center;padding:40px">加载失败: ${this.esc(e.message)}</p>`;
    }
  },

  async showContentDetail(id) {
    const { data } = await this._api(`/api/contents/${id}`);
    if (!data) return;
    const d = data;
    const ai = d.ai_review || {};
    const rules = d.matched_rules || [];
    const stats = d.stats || {};
    const media = d.media || [];

    // 渲染媒体（图片 + 视频，视频用代理接口保证能播放）
    let mediaHTML = "";
    if (media.length) {
      mediaHTML = `<div style="display:flex;flex-wrap:wrap;gap:8px;margin:12px 0">`;
      media.forEach((m, i) => {
        if (m.is_video) {
          // 优先本地文件，否则用代理从微信 CDN 流式播放
          const vSrc = m.video_local || `/api/video_proxy/${d.id}`;
          mediaHTML += `<video controls preload="metadata" src="${this.esc(vSrc)}" style="max-width:100%;max-height:360px;border-radius:8px" onerror="this.style.display='none'"></video>`;
        } else if (m.thumb) {
          mediaHTML += `<img src="${this.esc(m.thumb)}" style="max-width:200px;max-height:200px;border-radius:8px;object-fit:cover;cursor:pointer" onclick="window.open('${this.esc(m.orig || m.thumb_remote || m.thumb)}')" onerror="this.style.display='none'" />`;
        }
      });
      mediaHTML += `</div>`;
    }

    document.getElementById("modalTitle").textContent = d.title || d.caption || "内容详情";
    document.getElementById("modalBody").innerHTML = `
      <p style="color:var(--muted)">视频号: ${this.esc(d.channel_name || "")} · 发布时间: ${d.publish_time || "未知"} · 风险: <span style="color:${d.risk_level==='red'?'var(--red)':d.risk_level==='yellow'?'var(--orange)':'var(--green)'}">${d.risk_level || "未标"}</span></p>
      ${mediaHTML}
      <p style="margin:8px 0"><b>文案:</b> ${this.esc(d.caption || d.title || "-")}</p>
      ${d.ocr_text ? `<p><b>画面文字:</b> ${this.esc(d.ocr_text)}</p>` : ""}
      ${d.transcript ? `<p><b>语音转写:</b> ${this.esc(d.transcript)}</p>` : ""}
      <p style="margin:8px 0;font-size:13px;color:var(--muted)">
        👁 观看 ${stats.read || 0} · ❤ 点赞 ${stats.like || 0} · 💬 评论 ${stats.comment || 0} · 🔄 转发 ${stats.forward || 0} · ⭐ 收藏 ${stats.fav || 0}
      </p>
      ${ai.interpretation ? `<div class="result-box" style="background:var(--bg);border:1px solid var(--border);padding:10px;border-radius:8px;margin:8px 0"><b>🤖 AI 解读:</b><br><span style="font-size:13px">${this.esc(ai.interpretation)}</span></div>` : ""}
      ${ai.conclusion ? `<div class="result-box pass"><b>AI 结论:</b> ${this.esc(ai.conclusion)}</div>` : ""}
      ${ai.error ? `<p style="color:var(--red)">AI 异常: ${this.esc(ai.error)}</p>` : ""}
      ${!ai.used && !ai.error ? `<p style="color:var(--muted);font-size:12px">⏳ AI 分析尚未完成或模型未启用，可到 <a href="#" onclick="App.closeModal();App.switchView('modelcfg')">模型配置</a> 检查</p>` : ""}
      ${rules.length ? `<h4 style="margin-top:12px">命中规则:</h4>${rules.map(r => `<div style="padding:4px 0;border-bottom:1px solid var(--border);font-size:13px">[${r.severity}] ${this.esc(r.rule_name)}: ${this.esc(r.matched_text || "")}</div>`).join("")}` : ""}`;
    document.getElementById("modal").classList.remove("hidden");
  },

  async collectAll() {
    const { data } = await this._api("/api/collect-all", { method: "POST" });
    alert(data && data.message ? data.message : "采集请求已提交");
    setTimeout(() => this.loadDashboard(), 3000);
  },

  /* =========================================================
     视频号管理
     ========================================================= */
  async loadChannels() {
    // 初始化大区选择器
    const da = document.getElementById("chRegionDa");
    if (da.options.length <= 1) {
      for (const big of Object.keys(this.regions)) {
        const o = document.createElement("option"); o.value = big; o.textContent = big; da.appendChild(o);
      }
    }

    const { data } = await this._api("/api/channels");
    if (!data) return;
    document.getElementById("channelGrid").innerHTML = data.map(ch => {
      const authLabel = ch.authorized
        ? `<span style="color:var(--green);font-size:12px">已授权 ${ch.auth_days_left != null ? "(" + ch.auth_days_left + "天)" : ""}</span>`
        : `<span style="color:var(--red);font-size:12px">未授权</span>`;
      const btnLabel = ch.authorized ? "重新授权" : "授权";
      return `
      <div class="channel-card">
        <div class="cname">${this.esc(ch.name)}</div>
        <div class="cmeta">${ch.region || ""} ${ch.owner ? "· " + this.esc(ch.owner) : ""} · ${authLabel}</div>
        <div class="cactions">
          <button class="btn btn-sm btn-outline" onclick="App.startAuth(${ch.id})">${btnLabel}</button>
          <button class="btn btn-sm btn-outline" onclick="App.refreshChannel(${ch.id})">采集</button>
          <button class="btn btn-sm" style="color:var(--red)" onclick="if(confirm('删除？'))App.delChannel(${ch.id})">删除</button>
        </div>
      </div>`;
    }).join("") || '<p style="color:var(--muted)">暂无视频号，添加上方开始监控</p>';

    // 同步更新内容明细区域的视频号筛选下拉框
    const fdCh = document.getElementById("fdChannel");
    if (fdCh) {
      const curVal = fdCh.value;
      fdCh.innerHTML = '<option value="">全部视频号</option>' +
        data.map(ch => `<option value="${ch.id}">${this.esc(ch.name)}</option>`).join("");
      fdCh.value = curVal; // 保持之前的选择
    }
  },

  onChRegionDa() {
    const big = document.getElementById("chRegionDa").value;
    const sub = document.getElementById("chRegionSub");
    sub.innerHTML = '<option value="">细分区域</option>';
    if (big && this.regions[big]) {
      this.regions[big].forEach(s => { const o = document.createElement("option"); o.value = s; o.textContent = s; sub.appendChild(o); });
    }
  },

  onFdRegionBig() {
    const big = document.getElementById("fdRegionBig")?.value || "";
    const sub = document.getElementById("fdRegionSub");
    if (!sub) return;
    sub.innerHTML = '<option value="">全部区域</option>';
    if (big && this.regions[big]) {
      this.regions[big].forEach(s => { const o = document.createElement("option"); o.value = s; o.textContent = s; sub.appendChild(o); });
    }
    this.renderFeed(1, "regionBig");
  },

  async addChannel() {
    const name = document.getElementById("chName").value.trim();
    if (!name) return alert("请输入名称");
    const big = document.getElementById("chRegionDa").value;
    const sub = document.getElementById("chRegionSub").value;
    const region = big && sub ? `${big}/${sub}` : (big || sub || "");
    const { ok, data } = await this._api("/api/channels", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, region, owner: document.getElementById("chOwner").value.trim() }),
    });
    if (ok) { document.getElementById("chName").value = ""; this.loadChannels(); }
    else alert(data.detail || "添加失败");
  },

  async refreshChannel(id) {
    const btn = event.target;
    btn.disabled = true; btn.textContent = "采集中…";
    try {
      const { ok, status, data } = await this._api(`/api/channels/${id}/collect`, { method: "POST" });
      if (ok) {
        alert(`采集完成，共获取 ${data.collected} 条内容`);
        this.loadChannels();
        // 自动切到看板总览，让用户看到刚采集的内容
        this.switchView("dashboard");
        await this.loadDashboard();
      } else {
        alert(data.detail || `采集失败 (HTTP ${status})`);
      }
    } catch (e) {
      alert("采集请求失败: " + e.message);
    }
    btn.disabled = false; btn.textContent = "采集";
  },
  async delChannel(id) { await this._api(`/api/channels/${id}`, { method: "DELETE" }); this.loadChannels(); },

  // 扫码授权
  _authTimer: null,
  _authChannelId: null,

  async startAuth(id) {
    // 先取消旧会话再发起新的
    if (this._authChannelId) {
      await this._api(`/api/channels/${this._authChannelId}/auth/cancel`, { method: "POST" });
    }
    if (this._authTimer) { clearTimeout(this._authTimer); this._authTimer = null; }

    this._authChannelId = id;
    document.getElementById("authModal").classList.remove("hidden");
    document.getElementById("authQrImg").style.display = "none";
    document.getElementById("authQrWrap").style.display = "flex";
    document.getElementById("authMsg").textContent = "正在启动授权…";

    await this._api(`/api/channels/${id}/auth/start`, { method: "POST" });
    this._pollAuth();
  },

  async _pollAuth() {
    if (!this._authChannelId) return;
    const id = this._authChannelId;
    const { data } = await this._api(`/api/channels/${id}/auth/status`);
    if (!data) return;

    const wrap = document.getElementById("authQrWrap");
    const img = document.getElementById("authQrImg");
    const msg = document.getElementById("authMsg");
    const selDiv = document.getElementById("authChannelSelect");

    if (data.qrcode) {
      wrap.style.display = "none";
      img.style.display = "block";
      img.src = data.qrcode;
    }

    const statusMsgs = {
      idle: "未发起授权",
      starting: "正在启动浏览器，请稍候...",
      pending: "请使用微信扫码授权",
      scanned: "已扫码，请在弹出的浏览器窗口中完成登录",
      selecting: "请选择要管理的视频号",
      success: "授权成功！",
      timeout: "授权超时，请重新发起",
      failed: "授权失败，请重试",
    };
    msg.textContent = data.message || statusMsgs[data.status] || data.status;

    // 多视频号选择
    if (data.status === "selecting" && data.channel_options && data.channel_options.length > 0) {
      selDiv.style.display = "block";
      const initials = name => (name || "").charAt(0);
      selDiv.innerHTML = '<div class="auth-channel-list">' +
        data.channel_options.map(name =>
          `<div class="auth-channel-item" onclick="App.selectAuthChannel(${id}, '${this.esc(name)}')">
            <div class="ch-avatar">${initials(name)}</div>
            <div><div class="ch-name">${this.esc(name)}</div><div class="ch-hint">点击选择此视频号</div></div>
          </div>`
        ).join("") + '</div>';
    } else {
      selDiv.style.display = "none";
    }

    if (data.status === "success") {
      setTimeout(() => { this._doCleanAuth(); this.loadChannels(); }, 1500);
      return;
    }
    if (data.status === "timeout" || data.status === "failed") {
      return;
    }
    // pending / scanned / selecting: 继续轮询
    this._authTimer = setTimeout(() => this._pollAuth(), 2000);
  },

  selectAuthChannel(channelId, channelName) {
    this._api(`/api/channels/${channelId}/auth/select`, {
      method: "POST",
      body: JSON.stringify({ channel_name: channelName }),
      headers: { "Content-Type": "application/json" }
    });
    document.getElementById("authChannelSelect").style.display = "none";
    document.getElementById("authMsg").textContent = "已选择「" + channelName + "」，正在完成登录…";
  },

  _doCleanAuth() {
    this._authChannelId = null;
    if (this._authTimer) { clearTimeout(this._authTimer); this._authTimer = null; }
    document.getElementById("authModal").classList.add("hidden");
  },

  cancelAuth() {
    if (this._authChannelId) {
      this._api(`/api/channels/${this._authChannelId}/auth/cancel`, { method: "POST" }).catch(() => {});
    }
    this._doCleanAuth();
  },

  /* =========================================================
     内容上传
     ========================================================= */
  _setupDragDrop() {
    const zone = document.getElementById("dropZone");
    if (zone._bound) return;
    zone._bound = true;
    zone.addEventListener("click", () => document.getElementById("fileInput").click());
    zone.addEventListener("dragover", e => { e.preventDefault(); zone.classList.add("drag"); });
    zone.addEventListener("dragleave", () => zone.classList.remove("drag"));
    zone.addEventListener("drop", e => { e.preventDefault(); zone.classList.remove("drag"); this._onFiles(e.dataTransfer.files); });
    document.getElementById("fileInput").addEventListener("change", e => this._onFiles(e.target.files));
  },

  _onFiles(fl) {
    for (const f of fl) {
      if (!this.selectedFiles.find(sf => sf.name === f.name && sf.size === f.size)) this.selectedFiles.push(f);
    }
    this._renderFileList();
  },

  _renderFileList() {
    document.getElementById("fileList").innerHTML = this.selectedFiles.map((f, i) =>
      `<span style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:4px 8px;font-size:12px;display:flex;align-items:center;gap:6px">
        ${this.esc(f.name)} (${(f.size / 1024 / 1024).toFixed(1)}MB)
        <span onclick="App.selectedFiles.splice(${i},1);App._renderFileList()" style="cursor:pointer;color:var(--red)">×</span>
      </span>`).join("");
  },

  switchUploadTab(tab) {
    const isUpload = tab === "upload";
    document.getElementById("uploadCard").style.display = isUpload ? "" : "none";
    document.getElementById("t7Card").style.display = isUpload ? "none" : "";
    document.getElementById("tabUploadBtn").classList.toggle("on", isUpload);
    document.getElementById("tabT7Btn").classList.toggle("on", !isUpload);
  },

  goT7() {
    const code = (document.getElementById("t7Code")?.value || "").trim();
    if (!code) return alert("请先粘贴一稿一码");
    this.openT7Form(code);
  },

  openT7Form(code) {
    document.getElementById("t7FormCode").textContent = code;
    document.getElementById("t7Form").style.display = "";
    document.getElementById("t7Msg").textContent = "";
    this.t7ShotFiles = [];
    this.t7ProofFiles = [];
    const sf = document.getElementById("t7ShotFile"); if (sf) sf.value = "";
    const pf = document.getElementById("t7ProofFile"); if (pf) pf.value = "";
    this._renderT7Thumbs("screenshot");
    this._renderT7Thumbs("business");
    document.getElementById("t7Form").scrollIntoView({ behavior: "smooth", block: "center" });
  },

  onT7CredFiles(input, type) {
    const list = type === "screenshot" ? this.t7ShotFiles : this.t7ProofFiles;
    const files = Array.from(input.files || []);
    files.forEach(f => this._uploadT7Cred(f, type, list));
    input.value = "";
  },

  async _normalizeImage(file) {
    // 统一转成 JPEG 并压缩：解决 iPhone HEIC 无法显示、扩展名与内容不符的问题
    if (file.type === "image/gif") return file; // 动图保留
    try {
      const url = URL.createObjectURL(file);
      const img = await new Promise((resolve, reject) => {
        const im = new Image();
        im.onload = () => resolve(im);
        im.onerror = () => reject(new Error("无法解码图片"));
        im.src = url;
      });
      const maxDim = 1600;
      let w = img.naturalWidth, h = img.naturalHeight;
      const scale = Math.min(1, maxDim / Math.max(w, h));
      w = Math.max(1, Math.round(w * scale));
      h = Math.max(1, Math.round(h * scale));
      const canvas = document.createElement("canvas");
      canvas.width = w; canvas.height = h;
      const ctx = canvas.getContext("2d");
      ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, w, h);
      ctx.drawImage(img, 0, 0, w, h);
      URL.revokeObjectURL(url);
      const blob = await new Promise(res => canvas.toBlob(res, "image/jpeg", 0.85));
      if (!blob) throw new Error("转码失败");
      const base = (file.name || "image").replace(/\.[^.]+$/, "") || "image";
      return new File([blob], base + ".jpg", { type: "image/jpeg" });
    } catch (e) {
      return file; // 解码失败则回退原文件
    }
  },

  async _uploadT7Cred(file, type, list) {
    file = await this._normalizeImage(file);
    const fd = new FormData();
    fd.append("type", type);
    fd.append("file", file);
    try {
      const { ok, data } = await this._api("/api/campaign/credentials/upload", { method: "POST", body: fd });
      if (ok && data && data.files) data.files.forEach(item => list.push(item));
    } catch (e) {
      alert("图片上传失败：" + e.message);
    }
    this._renderT7Thumbs(type);
  },

  _renderT7Thumbs(type) {
    const list = type === "screenshot" ? this.t7ShotFiles : this.t7ProofFiles;
    const id = type === "screenshot" ? "t7ShotList" : "t7ProofList";
    const box = document.getElementById(id);
    if (box) box.innerHTML = list.map((x, i) =>
      `<span class="thumb-item"><img src="${this.esc(x.url || x.cos_key)}" alt=""><button type="button" class="rm" onclick="App.removeT7Thumb('${type}',${i})">×</button></span>`
    ).join("");
  },

  removeT7Thumb(type, i) {
    const list = type === "screenshot" ? this.t7ShotFiles : this.t7ProofFiles;
    list.splice(i, 1);
    this._renderT7Thumbs(type);
  },

  async doSubmitT7() {
    const code = document.getElementById("t7FormCode").textContent.trim();
    const name = (document.getElementById("t7Name")?.value || "").trim();
    if (!code) return alert("缺少一稿一码");
    if (!name) return alert("请先填写姓名");
    const body = {
      code: code,
      name: name,
      plays: parseInt(document.getElementById("t7Plays").value) || 0,
      retention_3s: parseFloat(document.getElementById("t7R3").value) || 0,
      completion_rate: parseFloat(document.getElementById("t7Comp").value) || 0,
      deep_interaction_rate: parseFloat(document.getElementById("t7Deep").value) || 0,
      like_rate: parseFloat(document.getElementById("t7Like").value) || 0,
      original: document.getElementById("t7Original").checked,
      screenshot: this.t7ShotFiles.map(x => ({ file: x.cos_key, url: x.url })),
      business_proof: this.t7ProofFiles.map(x => ({ file: x.cos_key, url: x.url })),
    };
    const msg = document.getElementById("t7Msg");
    msg.textContent = "提交中…";
    msg.style.color = "#4b5563";
    const { ok, data } = await this._api("/api/campaign/submit-by-code", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!ok) { msg.textContent = "回传失败：" + (data.detail || "服务器错误"); msg.style.color = "#dc2626"; return; }
    if (data.qualified) {
      msg.innerHTML = `已计分：总分 <b style="color:#2563eb">${data.score.total_score}</b> · 等级 <b>${data.score.grade}</b> · 战功 <b style="color:#2563eb">${data.score.total_merit}</b>`;
      msg.style.color = "#16a34a";
    } else {
      msg.textContent = "未达参评门槛：" + (data.reject_reasons || []).join("；");
      msg.style.color = "#d97706";
    }
  },

  async queryPending() {
    const name = (document.getElementById("t7Name")?.value || document.getElementById("upName")?.value || "").trim();
    if (!name) return alert("请先填写您的姓名");
    const big = (document.getElementById("upBigRegion")?.value || "").trim();
    const sub = (document.getElementById("upSubRegion")?.value || "").trim();
    const region = big && sub ? `${big} / ${sub}` : "";
    const box = document.getElementById("pendingList");
    if (box) box.innerHTML = '<div style="font-size:12px;color:#1d4ed8">查询中…</div>';
    const { ok, data } = await this._api(`/api/campaign/pending-by-name?name=${encodeURIComponent(name)}&region=${encodeURIComponent(region)}`);
    if (!box) return;
    if (!ok || !Array.isArray(data)) { box.innerHTML = '<div style="font-size:12px;color:#dc2626">查询失败</div>'; return; }
    if (!data.length) { box.innerHTML = '<div style="font-size:12px;color:#1d4ed8">没有待回传的作品</div>'; return; }
    box.innerHTML = data.map(w => `
      <div style="display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:1px dashed #dbeafe">
        <code style="font-size:12px;background:#fff;padding:2px 6px;border-radius:5px;color:#1d4ed8">${this.esc(w.code)}</code>
        <span style="flex:1;font-size:12px;color:#555;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${this.esc(w.title||'')}${w.track?' · '+this.esc(w.track):''}</span>
        <button class="btn sm" style="height:30px;padding:0 12px;font-size:12px;border-radius:8px" onclick="App.openT7Form('${this.esc(w.code)}')">回传</button>
      </div>`).join("");
  },

  async doUpload() {
    const caption = document.getElementById("upCaption").value.trim();
    if (!this.selectedFiles.length && !caption) return alert("请上传文件或填写文案");

    // 未登录用户：必须填写姓名和区域
    const isGuest = !this.me;
    const upName = document.getElementById("upName");
    const nameVal = (upName?.value || "").trim();
    const bigVal = (document.getElementById("upBigRegion")?.value || "").trim();
    const subVal = (document.getElementById("upSubRegion")?.value || "").trim();
    const regionVal = bigVal && subVal ? `${bigVal} / ${subVal}` : "";
    if (isGuest && !nameVal) return alert("请输入您的姓名");
    if (isGuest && !regionVal) return alert("请选择所属区域");

    // 检查文件大小（单个最大 500MB）
    const MAX = 500 * 1024 * 1024;
    for (const f of this.selectedFiles) {
      if (f.size > MAX) return alert(`文件「${f.name}」超过 500MB 限制，请压缩后重试`);
    }

    const btn = document.getElementById("btnUpload");
    btn.disabled = true; btn.textContent = "上传中...";
    const msg = document.getElementById("uploadMsg");
    msg.textContent = "";

    const form = new FormData();
    form.append("title", document.getElementById("upTitle").value.trim());
    form.append("caption", caption);
    const trackVal = (document.getElementById("upTrack")?.value || "").trim();
    if (trackVal) form.append("track", trackVal);
    if (isGuest) {
      form.append("display_name", nameVal);
      form.append("region", regionVal);
    }
    const totalSize = this.selectedFiles.reduce((s, f) => s + f.size, 0);
    let uploadedSize = 0;
    for (const f of this.selectedFiles) form.append("files", f);

    try {
      // 使用 XMLHttpRequest 支持上传进度
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/submissions/upload");
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          const pct = Math.round((e.loaded / e.total) * 100);
          btn.textContent = `上传中 ${pct}%`;
          if (pct < 100) {
            msg.textContent = `正在上传... ${pct}%（共 ${(totalSize/1024/1024).toFixed(0)}MB）`;
          } else {
            msg.textContent = "上传完成，正在存储到云端...";
          }
          msg.style.color = "var(--primary)";
        }
      };
      const result = await new Promise((resolve, reject) => {
        xhr.onload = () => {
          try { resolve({ ok: xhr.status === 200, data: JSON.parse(xhr.responseText) }); }
          catch { resolve({ ok: false, data: { _text: xhr.responseText } }); }
        };
        xhr.onerror = () => reject(new Error("网络错误"));
        xhr.send(form);
      });
      btn.disabled = false; btn.textContent = "提交审核";

      const { ok, data } = result;
      if (ok && data.ok) {
        if (data.campaign_code) {
          msg.innerHTML = `提交成功！<b style="color:var(--brand)">一稿一码：${this.esc(data.campaign_code)}</b><br><span style="font-size:12px">请保存此码，7 天后凭码回传 T+7 数据</span><br>正在 AI 审核中，请稍候...`;
        } else {
          msg.textContent = `提交成功！正在AI审核中，请稍候...`;
        }
        msg.style.color = "var(--primary)";
        this.selectedFiles = [];
        this._renderFileList();
        document.getElementById("upTitle").value = "";
        document.getElementById("upCaption").value = "";
        this.loadSubmissions();
        this._pollResult(data.submission_id);
      } else {
        msg.textContent = "上传失败: " + (data.detail || data._text || "服务器错误");
        msg.style.color = "var(--red)";
      }
    } catch (e) {
      btn.disabled = false; btn.textContent = "提交审核";
      msg.textContent = "网络错误，请检查连接后重试: " + e.message;
      msg.style.color = "var(--red)";
    }
  },

  async _pollResult(id, n = 0) {
    if (n > 90) {
      document.getElementById("uploadMsg").textContent = "审核超时，请刷新页面查看结果";
      document.getElementById("queueStatus").style.display = "none";
      return;
    }
    await new Promise(r => setTimeout(r, 2000));

    // 同步拉取队列状态
    this._showQueueStatus();

    const { data } = await this._api(`/api/submissions/${id}`);
    if (!data) { this._pollResult(id, n + 1); return; }
    const s = data.status;
    if (s !== "pending" && s !== "analyzing") {
      document.getElementById("queueStatus").style.display = "none";
      this.showResult(data);
      this.loadSubmissions();
      return;
    }
    // 更新进度提示
    const msg = document.getElementById("uploadMsg");
    if (msg && s === "analyzing") msg.textContent = `AI 正在分析中...（已等待${(n+1)*2}秒，请耐心等候）`;
    else if (msg && s === "pending") msg.textContent = `等待队列处理中...`;
    this._pollResult(id, n + 1);
  },

  async _showQueueStatus() {
    try {
      const { data } = await this._api("/api/queue/status");
      if (!data) return;
      const el = document.getElementById("queueStatus");
      const txt = document.getElementById("qsText");
      const fill = document.getElementById("qsFill");
      if (!el || !txt) return;

      const { queue_size, processing_count, total_waiting, estimated_wait_seconds, max_concurrent } = data;
      if (total_waiting === 0 && processing_count === 0) {
        el.style.display = "none";
        return;
      }

      el.style.display = "";
      let statusText = "";
      if (processing_count > 0) statusText += `正在处理 ${processing_count} 条`;
      if (queue_size > 0) statusText += `，${queue_size} 条排队中`;
      if (estimated_wait_seconds > 0) {
        statusText += `，预计等待 ${estimated_wait_seconds > 60 ? Math.round(estimated_wait_seconds/60)+'分钟' : estimated_wait_seconds+'秒'}`;
      }
      txt.textContent = statusText || "队列就绪";

      // 进度条：根据队列中任务比例
      const total = Math.max(total_waiting, 1);
      const done = processing_count / Math.max(total + max_concurrent, 1);
      fill.style.width = Math.min(done * 100, 100) + "%";
    } catch (e) { /* ignore */ }
  },

  showResult(sub) {
    const card = document.getElementById("resultCard");
    const content = document.getElementById("resultContent");
    if (!card || !content) return;

    const status = sub.status;
    const risk = sub.risk_level;
    const ai = sub.ai_result ? (typeof sub.ai_result === "string" ? JSON.parse(sub.ai_result) : sub.ai_result) : {};
    const rules = sub.matched_rules || [];

    const riskLabel = { low: "低风险", medium: "中风险", high: "高风险", forbidden: "严重违规", none: "无风险" };
    const riskColor = { low: "#f59e0b", medium: "#f97316", high: "#ef4444", forbidden: "#dc2626", none: "#10b981" };

    // ── 步骤1：AI 内容解读 ──
    const aiInterpretation = (ai.interpretation || "").trim();
    const aiConclusion = (ai.conclusion || "").trim();
    const aiError = (ai.error || "").trim();

    // ── 步骤2：规则命中情况 ──
    let rulesHTML = "";
    if (rules.length > 0) {
      const sevMap = { forbidden: "严重", high: "高", medium: "中", low: "低" };
      const sevColor = { forbidden: "#dc2626", high: "#ef4444", medium: "#f97316", low: "#f59e0b" };
      rulesHTML = rules.map(r => `
        <div style="display:flex;align-items:center;gap:10px;padding:8px 12px;background:var(--surface-alt);border:1px solid var(--border);border-radius:8px;margin-bottom:6px">
          <span style="flex-shrink:0;display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;color:#fff;background:${sevColor[r.severity] || '#999'}">${sevMap[r.severity] || r.severity}</span>
          <span style="font-weight:600;font-size:13px">${this.esc(r.rule_name || "未知规则")}</span>
          ${r.matched_text ? `<span style="font-size:12px;color:var(--muted);margin-left:auto">匹配: 「${this.esc(r.matched_text.substring(0, 40))}」</span>` : ""}
        </div>`).join("");
    } else {
      rulesHTML = `<div style="padding:8px 12px;background:var(--green-bg);border:1px solid rgba(62,207,142,.3);border-radius:8px;font-size:13px;color:var(--green)">未命中任何违规规则</div>`;
    }

    // ── 步骤3：最终判定 ──
    let verdictIcon, verdictColor, verdictTitle, verdictBg, actionHint;

    // 提取AI违规点（从matched_rules中汇总）
    const violationPoints = rules.filter(r => r.severity && r.severity !== "low")
      .map(r => `<li style="margin-bottom:4px"><b>${this.esc(r.rule_name||"")}</b>${r.matched_text ? "：" + this.esc(r.matched_text) : ""}</li>`)
      .join("");

    if (status === "approved") {
      verdictIcon = "✅"; verdictColor = "#10b981"; verdictTitle = "审核通过 · 内容合规";
      verdictBg = "rgba(16,185,129,.06)";
      actionHint = "该结果为AI审核结果，请发布严格遵循红线，以免违规";
    } else if (status === "manual_review") {
      verdictIcon = "⚠️"; verdictColor = "#f59e0b"; verdictTitle = "疑似风险 · 已提交人工复核";
      verdictBg = "rgba(245,158,11,.06)";
      const region = sub.region || "所属区域";
      actionHint = `该内容已移交对应的在地营销经理二次审核，请谨慎发布`;
      if (violationPoints) {
        actionHint += `<div style="text-align:left;margin-top:10px;font-size:12px;line-height:1.6;color:#92400e">
          <div style="font-weight:600;margin-bottom:4px">AI判定违规点：</div>
          <ul style="margin:0;padding-left:16px">${violationPoints}</ul>
        </div>`;
      }
    } else {
      verdictIcon = "❌"; verdictColor = "#dc2626"; verdictTitle = "审核不通过 · 内容违规";
      verdictBg = "rgba(220,38,38,.06)";
      actionHint = `该内容违规，请不要发布，如有疑问可以咨询所属在地营销经理`;
      if (violationPoints) {
        actionHint += `<div style="text-align:left;margin-top:10px;font-size:12px;line-height:1.6;color:#991b1b">
          <div style="font-weight:600;margin-bottom:4px">AI判定违规点：</div>
          <ul style="margin:0;padding-left:16px">${violationPoints}</ul>
        </div>`;
      }
    }

    content.innerHTML = `
      <div style="padding:0">
        <!-- 步骤1: AI 内容解读 -->
        <div style="margin-bottom:16px">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
            <span style="display:inline-flex;align-items:center;justify-content:center;width:24px;height:24px;border-radius:50%;background:var(--brand);color:#fff;font-size:12px;font-weight:700">1</span>
            <span style="font-weight:700;font-size:14px;color:var(--text)">AI 内容解读</span>
          </div>
          <div style="background:var(--surface-alt);border:1px solid var(--border);border-radius:10px;padding:14px 16px;font-size:13px;line-height:1.8;color:var(--text-secondary)">
            ${aiError ? `<div style="color:var(--red);margin-bottom:8px">⚠️ AI 分析失败：${this.esc(aiError)}</div>` : ""}
            ${aiInterpretation ? this.esc(aiInterpretation) : (aiError ? '<span style="color:var(--muted)">AI 分析未成功，请联系管理员检查模型配置</span>' : '<span style="color:var(--muted)">AI 正在分析中，请稍候刷新页面查看完整报告。</span>')}
            ${aiConclusion ? `<div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--border)"><span style="font-weight:600;color:var(--text)">AI 初步结论：</span>${this.esc(aiConclusion)}</div>` : ""}
          </div>
        </div>

        <!-- 步骤2: 规则命中情况 -->
        <div style="margin-bottom:16px">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
            <span style="display:inline-flex;align-items:center;justify-content:center;width:24px;height:24px;border-radius:50%;background:var(--electric);color:#fff;font-size:12px;font-weight:700">2</span>
            <span style="font-weight:700;font-size:14px;color:var(--text)">规则匹配检查</span>
            <span style="font-size:11px;color:var(--muted)">（命中 ${rules.length} 条规则）</span>
          </div>
          ${rulesHTML}
        </div>

        <!-- 步骤3: 最终判定 -->
        <div style="background:${verdictBg};border:2px solid ${verdictColor}20;border-radius:12px;padding:16px;text-align:center">
          <div style="font-size:36px;margin-bottom:8px">${verdictIcon}</div>
          <div style="font-size:18px;font-weight:700;color:${verdictColor};margin-bottom:6px">${verdictTitle}</div>
          <div style="font-size:13px;color:var(--text-secondary);line-height:1.6">${actionHint}</div>
        </div>
      </div>`;
    card.style.display = "";
    card.scrollIntoView({ behavior: "smooth" });
    document.getElementById("uploadMsg").textContent = "";
    // 刷新近期提交列表
    this.loadRecentSubs();
  },

  async loadRecentSubs() {
    const el = document.getElementById("recentList");
    if (!el) return;
    try {
      const { ok, data } = await this._api("/api/submissions?page=1&page_size=20");
      if (!ok || !data || !data.items) {
        el.innerHTML = '<div class="recent-empty">暂无提交记录，上传内容后将在此显示</div>';
        return;
      }
      const items = data.items || [];
      if (!items.length) {
        el.innerHTML = '<div class="recent-empty">暂无提交记录，上传内容后将在此显示</div>';
        return;
      }
      
      const statusMap = {
        pending: ["⏳", "排队中", "pending"],
        analyzing: ["🤖", "AI分析中", "analyzing"],
        approved: ["✅", "已通过", "approved"],
        manual_review: ["⚠️", "待复核", "review"],
        rejected: ["❌", "已驳回", "rejected"],
      };
      
      el.innerHTML = '<div class="recent-list">' + items.slice(0, 15).map(s => {
        const [ico, label, cls] = statusMap[s.status] || ["📌", s.status, ""];
        const ai = typeof s.ai_result === "string" ? (() => { try { return JSON.parse(s.ai_result); } catch(e) { return {}; } })() : (s.ai_result || {});
        const interp = (ai.interpretation || "").substring(0, 120);
        const time = (s.created_at || "").substring(5, 16);
        const title = (s.title || s.caption || "未命名").substring(0, 40);
        return `<div class="recent-item" data-id="${s.id}">
          <div class="recent-header" onclick="App.toggleSubDetail(${s.id}, this)">
            <div class="recent-icon status-${cls}">${ico}</div>
            <div class="recent-content">
              <div class="recent-title-row">
                <span class="recent-title">${title}</span>
                <span class="recent-status status-${cls}">${label}</span>
              </div>
              ${interp ? `<div class="recent-interp">${interp}</div>` : ""}
              <div class="recent-meta">${time} · ${s.region || ""}</div>
            </div>
          </div>
          <div class="recent-detail" id="subDetail-${s.id}"></div>
        </div>`;
      }).join("") + '</div>';
    } catch (e) {
      el.innerHTML = '<div class="recent-empty">加载失败</div>';
    }
  },

  async toggleSubDetail(subId, row) {
    const detailDiv = document.getElementById("subDetail-" + subId);
    if (!detailDiv) return;
    // 切换显示
    const isShow = detailDiv.classList.contains("show");
    if (isShow) {
      detailDiv.classList.remove("show");
      return;
    }
    detailDiv.classList.add("show");
    // 加载详情
    if (!detailDiv.dataset.loaded) {
      try {
        const { data: sub } = await this._api("/api/submissions/" + subId);
        if (!sub) { detailDiv.textContent = "加载失败"; return; }
        const ai = typeof sub.ai_result === "string" ? JSON.parse(sub.ai_result) : (sub.ai_result || {});
        const rules = sub.matched_rules || [];
        const statusMap = { approved: "✅ 已通过", rejected: "❌ 已驳回", manual_review: "⚠️ 待复核", pending: "⏳ 排队中", analyzing: "🤖 分析中" };
        const st = statusMap[sub.status] || sub.status;
        detailDiv.innerHTML = `
          <b>状态：</b>${st}<br>
          ${sub.region ? `<b>区域：</b>${sub.region}<br>` : ""}
          ${sub.caption ? `<b>文案：</b>${sub.caption.substring(0, 150)}<br>` : ""}
          <b>AI 解读：</b>${(ai.interpretation || "暂无").substring(0, 300)}<br>
          ${rules.length ? `<b>命中规则：</b>${rules.map(r => r.rule_name || r.matched_text || "未知").join("、")}<br>` : ""}
          ${ai.conclusion ? `<b>结论：</b>${ai.conclusion}` : ""}`;
        detailDiv.dataset.loaded = "1";
      } catch (e) { detailDiv.textContent = "加载详情失败"; }
    }
  },

  // ── 两级区域选择（公开上传页）──
  _populateBigRegions() {
    const sel = document.getElementById("upBigRegion");
    if (!sel) return;
    sel.innerHTML = '<option value="">请选择大区</option>';
    for (const big of Object.keys(this.regions || {})) {
      const o = document.createElement("option");
      o.value = big; o.textContent = big;
      sel.appendChild(o);
    }
  },

  onGuestBigChange() {
    const big = document.getElementById("upBigRegion").value;
    const sub = document.getElementById("upSubRegion");
    if (!sub) return;
    sub.innerHTML = '<option value="">请选择区域</option>';
    if (big && this.regions[big]) {
      for (const s of this.regions[big]) {
        const o = document.createElement("option");
        o.value = s; o.textContent = s;
        sub.appendChild(o);
      }
    }
  },

  _populateSubBig() {
    const sel = document.getElementById("subFilterBig");
    if (!sel || sel.options.length > 1) return;

    // 区域经理：只看到自己的，完全隐藏区域筛选
    if (this.me && this.me.role === "uploader") {
      sel.style.display = "none";
      const sub = document.getElementById("subFilterSub");
      if (sub) sub.style.display = "none";
      // 也隐藏 label 前面的 toolbar 区域
      const subCtx = document.getElementById("subContext");
      if (subCtx) {
        subCtx.style.display = "";
        subCtx.textContent = `你提交的内容`;
      }
      return;
    }

    sel.innerHTML = '<option value="">全部大区</option>';
    for (const big of Object.keys(this.regions)) {
      const o = document.createElement("option"); o.value = big; o.textContent = big; sel.appendChild(o);
    }

    // 在地营销经理：锁定区域筛选，不可改
    if (this.me && this.me.role === "manager" && this.me.region) {
      const mgrBig = this.me.region.split("/")[0];
      sel.value = mgrBig;
      sel.disabled = true;
      const sub = document.getElementById("subFilterSub");
      if (sub) {
        const mgrSub = this.me.region.includes("/") ? this.me.region.split("/")[1] : "";
        sub.innerHTML = '<option value="">全部区域</option>';
        if (this.regions[mgrBig]) {
          this.regions[mgrBig].forEach(s => { const o = document.createElement("option"); o.value = s; o.textContent = s; sub.appendChild(o); });
        }
        if (mgrSub) sub.value = mgrSub;
        sub.disabled = true;
      }
      const subCtx = document.getElementById("subContext");
      if (subCtx) {
        subCtx.style.display = "";
        subCtx.textContent = `你的管辖区域: ${this.me.region}（仅显示本区域提交，筛选已锁定）`;
      }
    }
  },

  onSubBig() {
    const big = document.getElementById("subFilterBig").value;
    const sub = document.getElementById("subFilterSub");
    sub.innerHTML = '<option value="">全部区域</option>';
    if (!big || !this.regions[big]) { this.loadSubmissions(); return; }
    this.regions[big].forEach(s => { const o = document.createElement("option"); o.value = s; o.textContent = s; sub.appendChild(o); });
    this.loadSubmissions();
  },

  async loadSubmissions(page = 1) {
    this.subPage = page;
    const status = document.getElementById("subFilterStatus").value;
    const big = document.getElementById("subFilterBig").value;
    const sub = document.getElementById("subFilterSub").value;
    const region = sub || big;  // 优先细分区域，其次大区
    const search = (document.getElementById("subFilterSearch")?.value || "").trim();
    const params = new URLSearchParams({ page, page_size: 20 });
    if (status) params.set("status", status);
    if (region) params.set("region", region);
    if (search) params.set("search", search);
    const { data } = await this._api("/api/submissions?" + params);
    if (!data || !data.items) return;

    const cn = { pending: "待审核", analyzing: "分析中", approved: "已通过", rejected: "已驳回", manual_review: "待复核" };
    const bc = { approved: "badge-green", rejected: "badge-red", manual_review: "badge-yellow", pending: "badge-muted", analyzing: "badge-muted" };

    document.getElementById("subTbody").innerHTML = data.items.map(s => {
      // 媒体预览图标
      const media = s.media || [];
      const hasVideo = media.some(m => m.is_video);
      const hasImage = media.some(m => !m.is_video);
      const mediaIcon = hasVideo ? "🎬" : (hasImage ? "🖼️" : "📄");
      // AI 结果摘要
      const ai = typeof s.ai_result === "string" ? (() => { try { return JSON.parse(s.ai_result); } catch(e) { return {}; } })() : (s.ai_result || {});
      const aiSummary = (ai.interpretation || "").substring(0, 60);
      return `
      <tr onclick="App.showDetail(${s.id})" style="cursor:pointer">
        <td>${mediaIcon} ${this.esc(s.title || s.caption || "(无标题)")}</td>
        <td>${this.esc(s.display_name || s.username || "访客")}</td>
        <td>${this.esc(s.region || "")}</td>
        <td style="font-size:12px;color:var(--muted)">${(s.created_at || "").substring(0, 16)}</td>
        <td><span class="badge ${bc[s.status] || 'badge-muted'}">${cn[s.status] || s.status}</span></td>
        <td><button class="btn sm danger" onclick="event.stopPropagation();App.deleteSub(${s.id})" title="删除">删除</button></td>
      </tr>`;
    }).join("") || '<tr><td colspan="6" style="color:var(--muted);text-align:center;padding:20px">暂无提交</td></tr>';

    // 根据角色显示区域上下文
    const subCtx = document.getElementById("subContext");
    if (subCtx) {
      const selBig = document.getElementById("subFilterBig");
      const selSub = document.getElementById("subFilterSub");
      const activeRegion = (selSub?.value || selBig?.value || "");
      if (activeRegion) {
        subCtx.style.display = "";
        subCtx.textContent = `当前筛选: ${activeRegion}`;
      } else if (this.me && this.me.role === "manager" && this.me.region) {
        subCtx.style.display = "";
        subCtx.textContent = `你的管辖区域: ${this.me.region}（仅显示本区域提交，筛选已锁定）`;
      } else if (this.me && this.me.role === "uploader") {
        subCtx.style.display = "";
        subCtx.textContent = `你提交的内容`;
      } else if (this.me && this.me.role === "admin") {
        subCtx.style.display = "";
        subCtx.textContent = `全国数据（共 ${data.total} 条）`;
      } else {
        subCtx.style.display = "none";
      }
    }

    const tp = Math.ceil(data.total / 20);
    document.getElementById("subPageInfo").textContent = `第 ${page}/${tp} 页，共 ${data.total} 条`;
    const subCnt = document.getElementById("subCount");
    if (subCnt) subCnt.textContent = `共 ${data.total} 条`;
    let ph = "";
    if (page > 1) ph += `<button onclick="App.loadSubmissions(${page - 1})">上一页</button>`;
    if (page < tp) ph += `<button onclick="App.loadSubmissions(${page + 1})">下一页</button>`;
    document.getElementById("subPagination").innerHTML = ph;
  },

  async showDetail(id) {
    const { data } = await this._api(`/api/submissions/${id}`);
    if (!data) return;
    const card = document.getElementById("detailCard");
    card.classList.remove("hidden");

    const cn = { pending: "待审核", analyzing: "分析中", approved: "已通过 ✅", rejected: "已驳回 ❌", manual_review: "待复核 ⚠️" };
    const rn = { none: "无风险", low: "低", medium: "中", high: "高", forbidden: "严重违规" };
    const ai = data.ai_result || {};
    const rules = data.matched_rules || [];
    const isMgr = this.me && (this.me.role === "admin" || this.me.role === "manager");

    let mediaHTML = (data.media || []).map(m => {
      const src = m.cos_url || m.local_url || (m.path ? ("/" + String(m.path).replace(/^\/+/, "")) : "");
      if (m.is_video) {
        return `<div style="display:inline-block;position:relative;margin:4px">
          <video controls preload="metadata" src="${this.esc(src)}" style="max-width:100%;max-height:300px;border-radius:8px;display:block"></video>
          <span style="position:absolute;top:4px;left:4px;background:rgba(0,0,0,.6);color:#fff;font-size:10px;padding:2px 6px;border-radius:4px">视频</span>
        </div>`;
      } else {
        return `<img src="${this.esc(src)}" style="max-width:100%;max-height:300px;border-radius:8px;margin:4px" />`;
      }
    }).join("");

    document.getElementById("detailContent").innerHTML = `
      <div class="result-box ${data.status === 'approved' ? 'pass' : data.status === 'rejected' ? 'fail' : data.status === 'manual_review' ? 'review' : ''}">
        <span class="badge">${cn[data.status]}</span>
        <span style="margin-left:8px;font-size:13px;color:var(--muted)">风险: ${rn[data.risk_level] || data.risk_level}</span>
      </div>
      <p><b>标题:</b> ${this.esc(data.title || "(无)")}</p>
      <p><b>文案:</b> ${this.esc(data.caption || "(无)")}</p>
      ${mediaHTML ? `<div style="margin:12px 0">${mediaHTML}</div>` : ""}
      ${ai.interpretation ? `<div class="card" style="margin:8px 0;padding:10px"><b>AI 解读:</b><br>${this.esc(ai.interpretation)}</div>` : ""}
      ${ai.conclusion ? `<div class="card" style="margin:8px 0;padding:10px"><b>AI 结论:</b><br>${this.esc(ai.conclusion)}</div>` : ""}
      ${ai.error ? `<p style="color:var(--red)">AI 异常: ${this.esc(ai.error)}</p>` : ""}
      ${rules.length ? `<div><b>命中规则:</b>${rules.map(r => `<div style="padding:4px 0;border-bottom:1px solid var(--border);font-size:13px">[${r.severity}] ${this.esc(r.rule_name)}: ${this.esc(r.matched_text || "")}</div>`).join("")}</div>` : ""}
      ${data.review_comment ? `<p><b>审核备注:</b> ${this.esc(data.review_comment)}</p>` : ""}
      ${data.status === "manual_review" && isMgr ? `
        <div style="margin-top:14px;display:flex;gap:8px">
          <button class="btn btn-primary" onclick="App.review(${data.id},'approve')">通过</button>
          <button class="btn btn-danger" onclick="App.review(${data.id},'reject')">驳回</button>
        </div>` : ""}`;
  },

  async review(id, action) {
    const comment = prompt("审核备注（可选）:");
    const { ok, data } = await this._api(`/api/submissions/${id}/review`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, comment: comment || null }),
    });
    if (ok) { this.loadSubmissions(); document.getElementById("detailCard").classList.add("hidden"); }
    else alert((data && data.detail) || "操作失败");
  },

  /* =========================================================
     合规规则
     ========================================================= */
  async loadRules() {
    const { data } = await this._api("/api/rules");
    this._rules = data || [];
    this.renderRules();
  },

  renderRules() {
    let list = this._rules || [];
    const q = (document.getElementById("fSearch")?.value || "").toLowerCase();
    const sev = document.getElementById("fSeverity")?.value || "";
    const en = document.getElementById("fEnabled")?.value || "";
    if (q) list = list.filter(r => (r.name || "").toLowerCase().includes(q) || (r.pattern || "").toLowerCase().includes(q));
    if (sev) list = list.filter(r => r.severity === sev);
    if (en) list = list.filter(r => String(r.enabled) === en);

    const sevTag = { forbidden: "🔴 严重", high: "🔴 风险", medium: "🟡 一般", low: "🟢 建议" };
    document.getElementById("rulesTbody").innerHTML = list.map(r => `
      <tr>
        <td>${this.esc(r.name)}</td><td>${this.esc(r.category || "")}</td><td>${r.rule_type || "keyword"}</td>
        <td><code>${this.esc(r.pattern)}</code></td><td>${sevTag[r.severity] || r.severity}</td>
        <td>${r.enabled ? "✅" : "⏸️"}</td>
        <td>
          <button class="btn btn-sm" onclick="App.editRule(${r.id})">编辑</button>
          <button class="btn btn-sm" onclick="App.toggleRule(${r.id})">${r.enabled ? "停用" : "启用"}</button>
          <button class="btn btn-sm" style="color:var(--red)" onclick="if(confirm('删除？'))App.delRule(${r.id})">删除</button>
        </td>
      </tr>`).join("");
  },

  async addRule() {
    const body = {
      name: document.getElementById("rName").value.trim(),
      category: document.getElementById("rCategory").value.trim(),
      rule_type: document.getElementById("rType").value,
      pattern: document.getElementById("rPattern").value.trim(),
      severity: document.getElementById("rSeverity").value,
    };
    if (!body.name || !body.pattern) return alert("名称和匹配内容必填");
    const { ok } = await this._api("/api/rules", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (ok) { document.getElementById("rName").value = ""; document.getElementById("rPattern").value = ""; this.loadRules(); }
  },

  editRule(rule) {
    const r = (this._rules || []).find(x => x.id === rule);
    if (!r) return;
    this.editingRuleId = r.id;
    document.getElementById("erName").value = r.name || "";
    document.getElementById("erCategory").value = r.category || "";
    document.getElementById("erType").value = r.rule_type || "keyword";
    document.getElementById("erPattern").value = r.pattern || "";
    document.getElementById("erSeverity").value = r.severity || "medium";
    document.getElementById("erDesc").value = r.description || "";
    document.getElementById("editRuleModal").classList.remove("hidden");
  },

  async saveRule() {
    const body = {
      name: document.getElementById("erName").value.trim(),
      category: document.getElementById("erCategory").value.trim(),
      rule_type: document.getElementById("erType").value,
      pattern: document.getElementById("erPattern").value.trim(),
      severity: document.getElementById("erSeverity").value,
      description: document.getElementById("erDesc").value.trim(),
    };
    const { ok } = await this._api(`/api/rules/${this.editingRuleId}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (ok) { this.closeEditRule(); this.loadRules(); }
  },

  closeEditRule() { document.getElementById("editRuleModal").classList.add("hidden"); this.editingRuleId = null; },

  async toggleRule(id) {
    const r = (this._rules || []).find(x => x.id === id);
    if (!r) return;
    await this._api(`/api/rules/${id}`, {
      method: "PUT", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({...r, enabled: !r.enabled}),
    });
    this.loadRules();
  },
  async delRule(id) { await this._api(`/api/rules/${id}`, { method: "DELETE" }); this.loadRules(); },

  async exportContents() {
    const channel = document.getElementById("fdChannel")?.value || "";
    const status = document.getElementById("fdStatus")?.value || "";
    const region = document.getElementById("fdRegionSub")?.value || document.getElementById("fdRegionBig")?.value || "";
    const params = new URLSearchParams();
    if (channel) params.set("channel_id", channel);
    if (status) params.set("risk_level", status);
    if (region) params.set("region", region);

    // 直接浏览器下载 CSV
    const a = document.createElement("a");
    a.href = "/api/contents/export?" + params.toString();
    a.download = "contents_export.csv";
    a.click();
  },

  async reanalyzeAll() {
    const btn = event.target;
    btn.disabled = true; btn.textContent = "分析中…";
    const { data: re } = await this._api("/api/reanalyze", { method: "POST" });
    btn.disabled = false; btn.textContent = "🔄 重新分析";
    alert(re && re.reanalyzed ? `已重新分析 ${re.reanalyzed} 条内容` : "已完成");
    this.renderFeed(1, "reanalyze");
  },

  /* =========================================================
     模型配置
     ========================================================= */
  async loadModelCfg() {
    const { data } = await this._api("/api/model-config");
    if (!data) return;
    document.getElementById("aiEnabled").checked = data.enabled;
    document.getElementById("aiBaseUrl").value = data.base_url || "";
    document.getElementById("aiModel").value = data.model || "";
  },

  async saveModel() {
    const { ok } = await this._api("/api/model-config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: document.getElementById("aiEnabled").checked,
        base_url: document.getElementById("aiBaseUrl").value.trim(),
        model: document.getElementById("aiModel").value.trim(),
        api_key: document.getElementById("aiApiKey").value || null,
      }),
    });
    if (ok) { document.getElementById("aiTestResult").innerHTML = '<span style="color:var(--green)">保存成功</span>'; }
  },

  async testModel() {
    const el = document.getElementById("aiTestResult");
    el.innerHTML = '<div class="spinner"></div> 测试中...';
    const { ok, data } = await this._api("/api/model-config/test", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        base_url: document.getElementById("aiBaseUrl").value.trim(),
        model: document.getElementById("aiModel").value.trim(),
        api_key: document.getElementById("aiApiKey").value || null,
      }),
    });
    el.innerHTML = data && data.ok
      ? '<span style="color:var(--green)">连接成功</span>'
      : `<span style="color:var(--red)">连接失败: ${(data && data.error) || "未知"}</span>`;
  },

  /* =========================================================
     审核管理 (manager/admin)
     ========================================================= */
  async loadReview(page = 1) {
    this.revPage = page;
    const status = document.getElementById("revFilterStatus").value;
    const risk = document.getElementById("revFilterRisk")?.value || "";
    const big = document.getElementById("revFilterBig").value;
    const sub = document.getElementById("revFilterSub").value;
    const search = (document.getElementById("revFilterSearch")?.value || "").trim();
    const region = sub || big;
    const params = new URLSearchParams({ page, page_size: 20 });
    if (status) params.set("status", status);
    if (region) params.set("region", region);
    if (risk) params.set("risk_level", risk);
    if (search) params.set("search", search);
    const { data } = await this._api("/api/review/contents?" + params);
    if (!data || !data.items) return;

    const riskLabel = { red: "违规", yellow: "疑似", green: "合规", forbidden: "违规", none: "未标" };
    const riskCls = { red: "badge-red", yellow: "badge-yellow", green: "badge-green", forbidden: "badge-red", none: "badge-muted" };
    const msLabel = { violation: "已标违规", compliant: "已标合规" };

    document.getElementById("reviewList").innerHTML = data.items.map(s => `
      <div class="review-row" style="cursor:default">
        <div class="info">
          <div class="title">${this.esc(s.caption || "(无描述)")}</div>
          <div class="sub">📺 ${this.esc(s.channel_name || "")} · ${this.esc(s.region || "")} · ${(s.publish_time || s.created_at || "").substring(0, 16)}</div>
        </div>
        <span class="badge ${riskCls[s.risk_level] || 'badge-muted'}">${riskLabel[s.risk_level] || s.risk_level || "未标"}</span>
        ${s.manual_status ? `<span class="badge badge-muted">${msLabel[s.manual_status] || s.manual_status}</span>` : ""}
        <button class="btn sm" onclick="App.showRevDetail(${s.id})" style="margin-right:4px">明细</button>
        ${!s.manual_status ? `<button class="btn sm primary" onclick="App.reviewApprove(${s.id})">通过</button><button class="btn sm danger" onclick="App.reviewReject(${s.id})">驳回</button>` : ""}
      </div>`).join("") || '<p style="color:var(--muted);text-align:center;padding:20px">暂无待审核内容</p>';

    // 审核列表分页
    const tp = Math.ceil(data.total / 20) || 1;
    let ph = "";
    if (page > 1) ph += `<button onclick="App.loadReview(${page - 1})">上一页</button>`;
    ph += `<span>${page}/${tp} 页，共 ${data.total} 条</span>`;
    if (page < tp) ph += `<button onclick="App.loadReview(${page + 1})">下一页</button>`;
    document.getElementById("revPagination").innerHTML = ph;

    // 计数标签
    const revCnt = document.getElementById("revCount");
    if (revCnt) revCnt.textContent = `共 ${data.total} 条`;

    // 待审核提醒栏
    const pendingCount = data.pending_count;
    const alertEl = document.getElementById("revAlert");
    if (alertEl) {
      if (pendingCount > 0) {
        alertEl.style.display = "";
        document.getElementById("revPendingCount").textContent = pendingCount;
      } else {
        alertEl.style.display = "none";
      }
    }

    // 更新区域上下文提示
    const ctx = document.getElementById("revContext");
    if (ctx) {
      if (region) {
        ctx.style.display = "";
        ctx.textContent = `当前筛选: ${region}`;
      } else if (this.me && this.me.role === "manager" && this.me.region) {
        ctx.style.display = "";
        ctx.textContent = `你的管辖区域: ${this.me.region}（仅显示本区域提交）`;
      } else if (this.me && this.me.role === "admin") {
        ctx.style.display = "";
        ctx.textContent = `全国数据（共 ${data.total} 条）`;
      } else {
        ctx.style.display = "none";
      }
    }
  },

  _populateRevBig() {
    const sel = document.getElementById("revFilterBig");
    if (!sel || sel.options.length > 1) return;
    sel.innerHTML = '<option value="">全部大区</option>';
    for (const big of Object.keys(this.regions)) {
      const o = document.createElement("option"); o.value = big; o.textContent = big; sel.appendChild(o);
    }
    // manager 预设为自己的区域
    if (this.me && this.me.role === "manager" && this.me.region) {
      const mgrBig = this.me.region.split("/")[0];
      if (sel.querySelector(`option[value="${mgrBig}"]`)) {
        sel.value = mgrBig;
      }
    }
  },

  onRevBig() {
    const big = document.getElementById("revFilterBig").value;
    const sub = document.getElementById("revFilterSub");
    sub.innerHTML = '<option value="">全部区域</option>';
    if (big && this.regions[big]) {
      this.regions[big].forEach(s => { const o = document.createElement("option"); o.value = s; o.textContent = s; sub.appendChild(o); });
    }
    this.loadReview();
  },

  async reviewAction(id, action) {
    if (action === "reject") {
      const comment = prompt("驳回原因（必填）:");
      if (!comment) return;
      const { ok } = await this._api(`/api/submissions/${id}/review`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, comment }),
      });
      if (ok) this.loadReview();
    } else {
      const { ok } = await this._api(`/api/submissions/${id}/review`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, comment: null }),
      });
      if (ok) this.loadReview();
    }
  },

  async deleteSub(id) {
    if (!confirm("确定删除这条提交？上传文件也会被清除。")) return;
    const { ok } = await this._api(`/api/submissions/${id}`, { method: "DELETE" });
    if (ok) {
      // 刷新当前活跃的列表
      const active = document.querySelector(".pane.active");
      if (active) {
        const view = active.id.replace("pane-", "");
        if (view === "subdash") this._loadSubdashTable(1);
        else if (view === "upload") this.loadSubmissions(1);
      }
    }
  },

  // 审核管理专用：对采集内容做人工复核（通过/驳回）
  async reviewApprove(id) {
    const { ok } = await this._api(`/api/contents/${id}/override-status`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ manual_status: "compliant" }),
    });
    if (ok) this.loadReview();
  },
  async reviewReject(id) {
    const comment = prompt("驳回原因（必填）:");
    if (!comment) return;
    const { ok } = await this._api(`/api/contents/${id}/override-status`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ manual_status: "violation" }),
    });
    if (ok) this.loadReview();
  },

  async showRevDetail(id) {
    const { ok, data } = await this._api(`/api/contents/${id}`);
    if (!ok || !data) return;
    const body = document.getElementById("revDetailBody");
    if (!body) return;

    const ai = data.ai_review || {};
    const rules = data.matched_rules || [];
    const riskLabel = { red: "违规", yellow: "疑似", green: "合规", forbidden: "违规", medium: "疑似", none: "无风险" };
    const riskColor = { red: "#dc2626", yellow: "#f59e0b", green: "#10b981", forbidden: "#dc2626", medium: "#f97316", none: "#999" };
    const msLabel = { violation: "已标违规", compliant: "已标合规" };

    const sevMap = { forbidden: "严重", high: "高", medium: "中", low: "低" };
    const sevColor = { forbidden: "#dc2626", high: "#ef4444", medium: "#f97316", low: "#f59e0b" };

    const rulesHTML = rules.length > 0
      ? rules.map(r => `
        <div style="padding:8px 12px;background:var(--surface-alt);border:1px solid var(--border);border-radius:6px;margin-bottom:4px">
          <span style="display:inline-block;padding:1px 6px;border-radius:3px;font-size:10px;font-weight:700;color:#fff;background:${sevColor[r.severity]||'#999'}">${sevMap[r.severity]||r.severity}</span>
          <b style="font-size:13px;margin-left:6px">${this.esc(r.rule_name||"")}</b>
          ${r.matched_text ? `<div style="font-size:11px;color:var(--text-secondary);margin-top:2px">${this.esc(r.matched_text)}</div>` : ""}
          ${r.field ? `<span style="font-size:10px;color:var(--muted)">位置：${this.esc(r.field)}</span>` : ""}
        </div>`).join("")
      : '<div style="font-size:13px;color:var(--green);background:var(--green-bg);border-radius:6px;padding:8px 12px">未命中违规规则</div>';

    body.innerHTML = `
      <div style="margin-bottom:12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <span class="badge" style="background:${riskColor[data.risk_level]||'#999'};color:#fff">${riskLabel[data.risk_level]||data.risk_level||"未标"}</span>
        ${data.manual_status ? `<span class="badge badge-muted">${msLabel[data.manual_status]||data.manual_status}</span>` : ""}
        <span style="font-size:12px;color:var(--muted)">📺 ${this.esc(data.channel_name||"")} · ${this.esc(data.region||"")}</span>
        <span style="font-size:12px;color:var(--muted)">${(data.publish_time||data.created_at||"").substring(0,16)}</span>
      </div>
      <div style="font-size:14px;font-weight:600;margin-bottom:8px;color:var(--text)">${this.esc(data.caption||"(无描述)")}</div>
      ${ai.interpretation ? `
        <div style="margin-bottom:8px">
          <div style="font-weight:700;font-size:12px;color:var(--electric);margin-bottom:4px">AI 内容解读</div>
          <div style="background:var(--surface-alt);border-radius:8px;padding:10px 14px;font-size:12px;line-height:1.7;color:var(--text-secondary)">${this.esc(ai.interpretation)}</div>
        </div>` : ""}
      ${ai.conclusion ? `
        <div style="margin-bottom:8px">
          <div style="font-weight:700;font-size:12px;color:var(--amber);margin-bottom:4px">AI 结论</div>
          <div style="background:var(--amber-bg);border-radius:8px;padding:10px 14px;font-size:12px;line-height:1.7;color:var(--amber)">${this.esc(ai.conclusion)}</div>
        </div>` : ""}
      ${ai.error ? `<div style="color:#dc2626;font-size:12px;margin-bottom:8px">⚠️ ${this.esc(ai.error)}</div>` : ""}
      <div style="margin-bottom:8px">
        <div style="font-weight:700;font-size:12px;color:var(--text);margin-bottom:4px">命中规则（${rules.length}条）</div>
        ${rulesHTML}
      </div>
    `;
    document.getElementById("revDetailModal").classList.remove("hidden");
  },

  closeRevDetail() {
    document.getElementById("revDetailModal").classList.add("hidden");
  },

  /* =========================================================
     消息通知中心
     ========================================================= */
  _notifs: [],  // [{id, text, time, read}]

  toggleNotif() {
    const panel = document.getElementById("notifPanel");
    if (panel) {
      panel.style.display = panel.style.display === "none" ? "" : "none";
      if (panel.style.display !== "none") this.loadNotifs();
    }
  },

  async loadNotifs() {
    const cat = this.notifCategories = this.notifCategories || {};
    const items = [];
    try {
      // 并行拉取统计数据 + 推送通知
      const [statsRes, notifRes] = await Promise.all([
        this._api("/api/dashboard/stats"),
        this._api("/api/notifications"),
      ]);
      const stats = statsRes.data;
      if (stats) {
        const role = this.me?.role;
        if (role === "admin") {
          if (stats.violation_count) items.push({ id: "vc", text: `监控到 ${stats.violation_count} 条违规内容待处理`, time: "", icon: "🚨" });
          const reviewCount = stats.manual_review_count || 0;
          if (reviewCount) items.push({ id: "mr", text: `${reviewCount} 条内容需要人工复核`, time: "", icon: "⚠️" });
        } else if (role === "manager") {
          if (stats.violation_count) items.push({ id: "vc", text: `你的区域有 ${stats.violation_count} 条违规内容`, time: "", icon: "🚨" });
          const mr = stats.manual_review_count || 0;
          if (mr) items.push({ id: "mr", text: `${mr} 条内容需要你复核`, time: "", icon: "⚠️" });
        }
        const pending = stats.pending_count || 0;
        if (pending) items.push({ id: "pen", text: `${pending} 条提交正在排队等待分析`, time: "", icon: "⏳" });
        const analyzing = stats.analyzing_count || 0;
        if (analyzing) items.push({ id: "anz", text: `${analyzing} 条提交正在AI分析中`, time: "", icon: "🤖" });
      }

      const dbNotifs = notifRes.data;
      if (dbNotifs && dbNotifs.length) {
        dbNotifs.forEach(n => {
          items.push({ id: "db"+n.id, text: n.text, time: (n.created_at||"").substring(5,16), icon: n.icon || "📌" });
        });
      }

      // 去重计数
      items.forEach(it => { cat[it.id] = it; });
      this._notifs = Object.values(cat).filter(it => it.text);
    } catch (e) { /* ignore */ }

    this._renderNotifs();
  },

  _renderNotifs() {
    const list = document.getElementById("notifList");
    const empty = document.getElementById("notifEmpty");
    const badge = document.getElementById("notifBadge");

    if (!list) return;

    if (!this._notifs.length) {
      list.innerHTML = "";
      if (empty) empty.style.display = "";
      if (badge) badge.style.display = "none";
      return;
    }

    if (empty) empty.style.display = "none";
    list.innerHTML = this._notifs.slice(0, 15).map(n => `
      <div class="ni">
        <span>${n.icon || "📌"}</span>
        <span style="flex:1">${n.text}</span>
        ${n.time ? `<span style="color:var(--muted);font-size:10px">${n.time}</span>` : ""}
      </div>`).join("");

    if (badge) {
      badge.textContent = this._notifs.length;
      badge.style.display = "";
    }
  },

  async clearNotif() {
    this._notifs = [];
    this.notifCategories = {};
    this._renderNotifs();
    try { await this._api("/api/notifications/read-all", { method: "PUT" }); } catch (e) {}
  },

  /* =========================================================
     用户管理 (admin)
     ========================================================= */
  async loadAdminUsers() {
    const { data } = await this._api("/api/users");
    if (!data) return;
    const roleCN = { admin: "超级管理员", manager: "在地营销经理", uploader: "区域经理" };
    document.getElementById("adminUsersTbody").innerHTML = data.map(u => `
      <tr>
        <td>${u.id}</td><td>${this.esc(u.username)}</td><td>${this.esc(u.display_name || "")}</td>
        <td>${roleCN[u.role] || u.role}</td><td>${this.esc(u.region || "")}</td>
        <td style="font-size:12px;color:var(--muted)">${(u.created_at || "").substring(0, 16)}</td>
        <td><button class="btn btn-sm" style="color:var(--red)" onclick="if(confirm('删除用户 ${this.esc(u.username)}？'))App.delUser(${u.id})">删除</button></td>
      </tr>`).join("");
  },

  showAddUser() {
    const card = document.getElementById("addUserCard");
    card.classList.toggle("hidden");
    if (!card.classList.contains("hidden")) {
      const big = document.getElementById("nuBig");
      if (big.options.length <= 1) {
        for (const k of Object.keys(this.regions)) { const o = document.createElement("option"); o.value = k; o.textContent = k; big.appendChild(o); }
      }
      App.onNuRole(); // 初始化区域选择显示
    }
  },

  onNuRole() {
    const role = document.getElementById("nuRole").value;
    const bigRow = document.getElementById("nuBigRow");
    const subRow = document.getElementById("nuSubRow");
    bigRow.classList.add("hidden");
    subRow.classList.add("hidden");
    if (role === "manager") bigRow.classList.remove("hidden");        // 在地营销经理 → 只有大区
    if (role === "uploader") { bigRow.classList.remove("hidden"); subRow.classList.remove("hidden"); } // 区域经理 → 大区 + 区域
  },

  onNuBig() {
    const big = document.getElementById("nuBig").value;
    const sub = document.getElementById("nuSub");
    sub.innerHTML = '<option value="">选择区域</option>';
    if (big && this.regions[big]) {
      this.regions[big].forEach(s => { const o = document.createElement("option"); o.value = big + "/" + s; o.textContent = s; sub.appendChild(o); });
    }
  },

  async addUser() {
    const role = document.getElementById("nuRole").value;
    let region = "";
    if (role === "manager") region = document.getElementById("nuBig").value;
    if (role === "uploader") region = document.getElementById("nuSub").value;
    const body = {
      username: document.getElementById("nuName").value.trim(),
      password: document.getElementById("nuPwd").value,
      display_name: document.getElementById("nuDisplay").value.trim(),
      role,
      region,
    };
    if (!body.username || !body.password) return alert("用户名和密码必填");
    if ((role === "manager" && !region) || (role === "uploader" && !region)) return alert("请选择区域");
    const { ok, data } = await this._api("/api/users", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (ok) { document.getElementById("addUserCard").classList.add("hidden"); this.loadAdminUsers(); }
    else alert(data.detail || "创建失败");
  },

  async delUser(id) { await this._api(`/api/users/${id}`, { method: "DELETE" }); this.loadAdminUsers(); },

  /* ── 使用日志 ── */
  _usageLogPage: 1,
  _usageLogTab: 'upload',

  async loadUsageLog(page = 1, tab) {
    if (tab) this._usageLogTab = tab;
    tab = this._usageLogTab;
    this._usageLogPage = page;

    // Tab 样式切换
    const tU = document.getElementById("logTabUpload");
    const tR = document.getElementById("logTabReview");
    const wU = document.getElementById("logUploadWrap");
    const wR = document.getElementById("logReviewWrap");
    if (tab === 'upload') {
      tU.style.cssText = "padding:8px 20px;border:none;background:none;cursor:pointer;font-size:14px;font-weight:600;color:var(--primary);border-bottom:2px solid var(--primary)";
      tR.style.cssText = "padding:8px 20px;border:none;background:none;cursor:pointer;font-size:14px;color:var(--muted);border-bottom:2px solid transparent";
      wU.style.display = ''; wR.style.display = 'none';
    } else {
      tR.style.cssText = "padding:8px 20px;border:none;background:none;cursor:pointer;font-size:14px;font-weight:600;color:var(--primary);border-bottom:2px solid var(--primary)";
      tU.style.cssText = "padding:8px 20px;border:none;background:none;cursor:pointer;font-size:14px;color:var(--muted);border-bottom:2px solid transparent";
      wU.style.display = 'none'; wR.style.display = '';
    }

    const { data } = await this._api(`/api/usage-log?page=${page}&page_size=30&action_type=${tab}`);
    if (!data) return;

    const items = data.items || [];
    const detail = (s) => this.esc((s || '').substring(0, 80));

    if (tab === 'upload') {
      const tbody = document.getElementById("usageLogTbody");
      tbody.innerHTML = items.length
        ? items.map(r => `
          <tr>
            <td>${r.id}</td>
            <td><b>${this.esc(r.display_name || '-')}</b></td>
            <td>${this.esc(r.region || '-')}</td>
            <td style="font-size:12px;color:var(--muted)">${this.esc(r.ip || '-')}</td>
            <td>${r.submission_id || '-'}</td>
            <td style="font-size:12px;max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${this.esc(r.detail || '')}">${detail(r.detail)}</td>
            <td style="font-size:12px;color:var(--muted)">${(r.created_at || '').substring(0, 16)}</td>
          </tr>`).join("")
        : '<tr><td colspan="7" style="color:var(--muted);text-align:center;padding:20px">暂无上传记录</td></tr>';
    } else {
      const tbody = document.getElementById("reviewLogTbody");
      tbody.innerHTML = items.length
        ? items.map(r => {
            // 解析 detail 字段：审核人 / 标题 / 备注
            const d = r.detail || '';
            const reviewer = (d.match(/审核人:\s*([^;]+)/) || [,''])[1];
            const title = (d.match(/标题:\s*([^;]+)/) || [,''])[1];
            const comment = (d.match(/备注:\s*(.+)/) || [,''])[1];
            const isApprove = r.action === '复核通过';
            const badge = isApprove
              ? '<span class="badge" style="background:var(--green-bg);color:var(--green)">复核通过</span>'
              : '<span class="badge" style="background:var(--red-bg);color:var(--danger)">复核驳回</span>';
            return `<tr>
              <td>${r.id}</td>
              <td>${badge}</td>
              <td><b>${this.esc(reviewer || r.display_name || '-')}</b></td>
              <td>${r.submission_id || '-'}</td>
              <td style="font-size:12px;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${this.esc(title)}">${this.esc(title || '-')}</td>
              <td>${this.esc(r.region || '-')}</td>
              <td style="font-size:12px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${this.esc(comment)}">${this.esc(comment || '-')}</td>
              <td style="font-size:12px;color:var(--muted)">${this.esc(r.ip || '-')}</td>
              <td style="font-size:12px;color:var(--muted)">${(r.created_at || '').substring(0, 16)}</td>
            </tr>`;
          }).join("")
        : '<tr><td colspan="9" style="color:var(--muted);text-align:center;padding:20px">暂无审核记录</td></tr>';
    }

    const tp = Math.ceil(data.total / 30) || 1;
    document.getElementById("usageLogCount").textContent = `共 ${data.total} 条`;
    let ph = "";
    if (page > 1) ph += `<button onclick="App.loadUsageLog(${page - 1})">上一页</button>`;
    ph += `<span style="margin:0 8px">${page}/${tp} 页</span>`;
    if (page < tp) ph += `<button onclick="App.loadUsageLog(${page + 1})">下一页</button>`;
    document.getElementById("usageLogPage").innerHTML = ph;
  },

  /* =========================================================
     弹窗
     ========================================================= */
  closeModal() { document.getElementById("modal").classList.add("hidden"); },

  /* =========================================================
     工具
     ========================================================= */
  esc(s) { return (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); },
};

// 启动
document.addEventListener("DOMContentLoaded", () => App.init());
