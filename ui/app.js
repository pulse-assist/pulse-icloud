// Страница «iCloud Drive»: аккаунты Apple (вход паролем и кодом) и подключения — папки @icloud/… (макет «iCloud Drive»).
// Всё — через мост Пульса: pulse.plugin(...) к серверу плагина, pulse.navigate(...) — в разделы Пульса.
// В песочнице нет alert/confirm — подтверждения прямо на странице. Пароль не хранится и в странице: поле очищается сразу.

const app = document.getElementById("app");
const dialog = document.getElementById("dialog");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const gb = (bytes) => {
  if (bytes == null) return "";
  const g = bytes / 1024 ** 3;
  return g >= 100 ? `${Math.round(g).toLocaleString("ru")} ГБ` : `${g.toFixed(1).replace(".", ",")} ГБ`;
};
const filesLink = (path) => `/files?path=${encodeURIComponent(path)}`;
const DELIVERY = {
  trusted_device: "Apple отправил код на ваши устройства. Введите его:",
  sms: "Apple отправил код по SMS. Введите его:",
};

let data = null;
let error = "";
let openMenu = null;      // имя подключения с открытым меню «⋯»
let renaming = null;      // имя подключения, которое переименовывают
let confirm = null;       // {kind: "logout"|"unmount", id}
let busy = "";            // что сейчас делается: запросы к Apple бывают долгими — показываем, кнопки выключены
let loginOpen = false;    // показана форма входа (при уже подключённых аккаунтах)
let loginError = "";
let loginAppleId = "";    // запомнить Apple ID между шагами; пароль — нет

async function load() {
  try {
    data = await pulse.plugin("/state");
    error = "";
  } catch (e) {
    if (!data) { app.innerHTML = `<div class="pl-error">Плагин не отвечает: ${esc(e.message)}</div>`; return; }
    error = e.message;
  }
  render();
}

async function act(fn, label) {
  busy = label || "Выполняю…";
  openMenu = null;
  render();
  try {
    await fn();
    error = "";
  } catch (e) {
    error = e.message;
  }
  openMenu = renaming = confirm = null;
  await load();
  busy = "";
  render();
}

function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}

// --- отрисовка ----------------------------------------------------------------------------------------------

function render() {
  const { accounts, mounts } = data;
  app.classList.toggle("busy", !!busy);
  app.innerHTML = `
    ${busy ? `<div class="working" role="status"><span class="spinner"></span>${esc(busy)}</div>` : ""}
    <div class="status">
      <span>${accounts.length ? `${accounts.length} ${plural(accounts.length, "аккаунт", "аккаунта", "аккаунтов")}` : "аккаунтов нет"}
        · ${mounts.length} ${plural(mounts.length, "подключение", "подключения", "подключений")}</span>
      <span class="grow"></span>
      <button class="btn" data-go="${esc(filesLink("@icloud"))}">Открыть @icloud в «Файлах»</button>
    </div>
    ${error ? `<div class="pl-error">${esc(error)}</div>` : ""}
    <div class="cols">
      <section class="box">
        <div class="box-head"><h2>Аккаунты Apple</h2><span class="hint">вход паролем и кодом, пароль не хранится</span></div>
        ${accounts.map(renderAccount).join("")}
        ${renderLogin()}
      </section>
      <section class="box">
        <div class="box-head"><h2>Подключения</h2><span class="hint">папки в «Файлах» → @icloud/…, их же видят агенты</span>
          <span class="grow"></span>
          <button class="btn btn-primary" data-add ${accounts.length ? "" : "disabled title=\"Сначала войдите в аккаунт\""}>+ Подключить папку</button></div>
        ${mounts.length ? `<table class="mounts"><thead><tr><th>Папка в «Файлах»</th><th>Откуда</th><th>Доступ</th><th></th></tr></thead>
          <tbody>${mounts.map(renderMount).join("")}</tbody></table>`
          : `<div class="pl-empty">Подключений нет. Войдите в аккаунт — весь iCloud Drive подключится сам.</div>`}
        <div class="box-foot hint">Удаление из «Файлов» переносит файл в «Недавно удалённые» iCloud — его можно вернуть 30 дней.
          Документы Pages, Numbers и Keynote видны как есть: читать их могут только программы Apple.
          Одинаковые имена в одной папке различаются суффиксом «(2)».</div>
      </section>
    </div>
    <p class="hint">Это неофициальный веб-доступ iCloud — то же, что делает icloud.com. Apple может его поменять, тогда плагину
      понадобится обновление.</p>`;
}

function renderAccount(a) {
  const asking = confirm?.kind === "logout" && confirm.id === a.uid;
  const st = a.storage;
  const share = st?.total ? Math.min(100, Math.round((st.used / st.total) * 100)) : 0;
  return `<div class="account">
    <span class="avatar">${esc((a.name || a.apple_id || "?").slice(0, 1).toUpperCase())}</span>
    <div class="account-body">
      <div class="account-name">${esc(a.name || a.apple_id)} <span>· ${esc(a.apple_id)}</span>
        ${a.expired ? `<span class="badge badge-warn">вход истёк</span>` : ""}</div>
      ${a.expired ? `<div class="hint">Подключения аккаунта не открываются — войдите снова.</div>`
        : st?.total ? `<div class="space"><div class="space-bar"><div style="width:${share}%"></div></div>
          <span>${gb(st.used)} из ${gb(st.total)}</span></div>`
        : a.error ? `<div class="hint">${esc(a.error)}</div>` : `<div class="hint ok">вход действует</div>`}
    </div>
    ${asking
      ? `<span class="inline-form"><span class="hint">Выйти и убрать подключения?</span>
          <button class="btn btn-sm btn-danger" data-logout-yes="${esc(a.uid)}">Выйти</button>
          <button class="btn btn-sm" data-cancel>Нет</button></span>`
      : a.expired
        ? `<button class="btn btn-sm btn-primary" data-relogin="${esc(a.apple_id)}">Войти снова</button>`
        : `<button class="btn btn-sm" data-logout="${esc(a.uid)}">Выйти</button>`}
  </div>`;
}

function renderLogin() {
  const login = data.login;
  if (login?.status === "code") {
    return `<div class="login">
      <h3>Вход в ${data.accounts.length ? "ещё один " : ""}аккаунт Apple</h3>
      <div class="code-row"><span class="step-n done">✓</span><div>Apple ID и пароль приняты: <b>${esc(login.apple_id)}</b></div></div>
      <div class="code-row"><span class="step-n">2</span>
        <form id="code-form" style="flex-grow:1" autocomplete="off">
          <div style="margin-bottom:6px">${esc(DELIVERY[login.delivery] || "Введите код подтверждения Apple:")}</div>
          <div class="inline-form"><input id="code" class="code-input" inputmode="numeric" maxlength="7" placeholder="123456"
              aria-label="Код из 6 цифр" autocomplete="one-time-code">
            <button class="btn btn-primary" type="submit">Войти</button></div>
          ${loginError ? `<div class="error">${esc(loginError)}</div>` : ""}
          <div class="links"><button type="button" class="btn-link" data-sms>Прислать код по SMS</button>
            <button type="button" class="btn-link" data-login-cancel>Отменить вход</button></div>
          <div class="hint">Код действует несколько минут. Если включена «Расширенная защита данных», после кода
            iPhone может попросить разрешить доступ к iCloud из веба — разрешите.</div>
        </form></div>
    </div>`;
  }
  const done = login?.status === "done" ? `<div class="hint ok">Вход выполнен: ${esc(login.apple_id)}.</div>` : "";
  const failed = ["error", "expired"].includes(login?.status) && !loginError ? `<div class="error">${esc(login.error)}</div>` : "";
  if (!loginOpen && data.accounts.length) {
    return `<div class="login-start">${failed}${done}<button class="btn" data-login>+ Войти в аккаунт Apple</button></div>`;
  }
  return `<div class="login">
    <h3>Вход в ${data.accounts.length ? "ещё один " : ""}аккаунт Apple</h3>
    ${failed}
    <form id="login-form" class="login-form">
      <label class="field">Apple ID<input id="apple-id" value="${esc(loginAppleId)}" placeholder="name@icloud.com" autocomplete="username"></label>
      <label class="field">Пароль Apple ID<input id="password" type="password" autocomplete="current-password"></label>
      ${loginError ? `<div class="error">${esc(loginError)}</div>` : ""}
      <div class="inline-form"><button class="btn btn-primary" type="submit">Продолжить</button>
        ${data.accounts.length ? `<button type="button" class="btn" data-login-close>Отмена</button>` : ""}</div>
    </form>
    <div class="hint">Пароль уходит только в Apple и не сохраняется: после входа плагин хранит сессию на сервере Пульса,
      её нужно продлевать примерно раз в два месяца. Пароль для приложений не подойдёт — нужен обычный пароль Apple ID.</div>
  </div>`;
}

function renderMount(m) {
  const where = m.folder_path ? `iCloud Drive/${esc(m.folder_path)}` : "весь iCloud Drive";
  const who = m.account ? ` · ${esc(m.account.apple_id)}${m.account.expired ? ` <span class="badge badge-warn">вход истёк</span>` : ""}`
    : ` <span class="badge badge-warn">аккаунт отключён</span>`;
  const name = renaming === m.name
    ? `<form class="inline-form" data-rename-form="${esc(m.name)}"><input name="to" value="${esc(m.name)}" aria-label="Новое имя">
        <button class="btn btn-sm btn-primary">Сохранить</button><button type="button" class="btn btn-sm" data-cancel>Отмена</button></form>`
    : `<b>${esc(m.name)}</b><div class="sub mono">${esc(m.files_path)}</div>`;
  const asking = confirm?.kind === "unmount" && confirm.id === m.name;
  const actions = asking
    ? `<span class="inline-form"><span class="hint">Отключить?</span>
        <button class="btn btn-sm btn-danger" data-unmount-yes="${esc(m.name)}">Отключить</button><button class="btn btn-sm" data-cancel>Нет</button></span>`
    : `<button class="btn-link" data-go="${esc(filesLink(m.files_path))}">Открыть</button>
       <span class="menu"><button class="btn btn-sm" data-menu="${esc(m.name)}" aria-label="Действия: ${esc(m.name)}">⋯</button>
       ${openMenu === m.name ? `<div class="menu-list">
          <button data-rename="${esc(m.name)}">Переименовать</button>
          <button class="btn-danger" data-unmount="${esc(m.name)}">Отключить</button></div>` : ""}</span>`;
  return `<tr><td class="name">${name}</td><td>${where}${who}</td><td>чтение и запись</td><td class="actions">${actions}</td></tr>`;
}

// --- действия ------------------------------------------------------------------------------------------------

app.addEventListener("click", async (e) => {
  const t = e.target.closest("button, a");
  if (!t) { if (openMenu) { openMenu = null; render(); } return; }
  const d = t.dataset;
  if (d.go) pulse.navigate(d.go);
  else if (busy) return;
  else if (d.login !== undefined) { loginOpen = true; loginError = ""; render(); app.querySelector("#apple-id")?.focus(); }
  else if (d.loginClose !== undefined) { loginOpen = false; loginError = ""; render(); }
  else if (d.relogin) { loginOpen = true; loginAppleId = d.relogin; loginError = ""; render(); app.querySelector("#password")?.focus(); }
  else if (d.loginCancel !== undefined) { loginError = ""; await act(() => pulse.plugin("/login", { method: "DELETE" }), "Отменяю вход…"); }
  else if (d.sms !== undefined) {
    loginError = "";
    await act(async () => {
      try { await pulse.plugin("/login/sms", { method: "POST" }); } catch (err) { loginError = err.message; }
    }, "Прошу Apple прислать SMS…");
  }
  else if (d.logout) { confirm = { kind: "logout", id: d.logout }; render(); }
  else if (d.logoutYes) await act(() => pulse.plugin(`/accounts/${encodeURIComponent(d.logoutYes)}`, { method: "DELETE" }), "Выхожу из аккаунта…");
  else if (d.unmount) { confirm = { kind: "unmount", id: d.unmount }; openMenu = null; render(); }
  else if (d.unmountYes) await act(() => pulse.plugin(`/mounts/${encodeURIComponent(d.unmountYes)}`, { method: "DELETE" }), "Отключаю…");
  else if (d.menu) { openMenu = openMenu === d.menu ? null : d.menu; render(); }
  else if (d.rename) { renaming = d.rename; openMenu = null; render(); app.querySelector("[data-rename-form] input")?.select(); }
  else if (d.cancel !== undefined) { confirm = renaming = null; render(); }
  else if (d.add !== undefined) openDialog();
});

async function step(label, fn) {
  busy = label;
  render();
  try {
    await fn();
    loginError = "";
  } catch (err) {
    loginError = err.message;
  }
  busy = "";
  await load();
}

app.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (e.target.id === "login-form") {
    const appleId = document.getElementById("apple-id").value.trim();
    const field = document.getElementById("password");
    const password = field.value;
    field.value = "";                                   // пароль не остаётся ни в поле, ни в состоянии страницы
    if (!appleId || !password) { loginError = "Нужны Apple ID и пароль"; render(); return; }
    loginAppleId = appleId;
    await step("Вхожу в Apple…", async () => {
      const state = await pulse.plugin("/login", { method: "POST", body: { apple_id: appleId, password } });
      if (state.status === "done") loginOpen = false;
    });
    return;
  }
  if (e.target.id === "code-form") {
    const code = document.getElementById("code").value.trim();
    if (!code) return;
    await step("Проверяю код и открываю iCloud Drive…", async () => {
      await pulse.plugin("/login/code", { method: "POST", body: { code } });
      loginOpen = false;
    });
    return;
  }
  const form = e.target.closest("[data-rename-form]");
  if (!form) return;
  const to = form.elements.to.value.trim();
  await act(() => pulse.plugin(`/mounts/${encodeURIComponent(form.dataset.renameForm)}`, { method: "PATCH", body: { name: to } }),
            "Переименовываю…");
});

// --- окно «Подключить папку» --------------------------------------------------------------------------------------

const form = { account: "", trail: [], folders: null, name: "", error: "", busy: false };

function openDialog() {
  const first = data.accounts.find((a) => !a.expired) || data.accounts[0];
  Object.assign(form, { account: first?.uid || "", trail: [], folders: null, name: "", error: "", busy: false });
  browse([]);
}

function closeDialog() { dialog.innerHTML = ""; }

// trail: [{id, name}] — путь папок внутри iCloud Drive; пустой — весь Drive
async function browse(trail) {
  form.trail = trail;
  form.folders = null;
  form.name = "";
  renderDialog();
  const params = { account: form.account };
  if (trail.length) params.folder = trail[trail.length - 1].id;
  try {
    form.folders = (await pulse.plugin("/browse", { params })).folders;
  } catch (e) {
    form.folders = [];
    form.error = e.message;
  }
  renderDialog();
}

function renderDialog() {
  const title = form.trail.length ? form.trail[form.trail.length - 1].name : "iCloud Drive";
  const crumbs = [{ label: "iCloud Drive", n: 0 }, ...form.trail.map((f, i) => ({ label: f.name, n: i + 1 }))];
  dialog.innerHTML = `<div class="overlay" data-close-overlay><div class="modal" role="dialog" aria-label="Подключить папку">
    <div class="modal-head"><h2>Подключить папку</h2><button class="btn-link" data-close aria-label="Закрыть">✕</button></div>
    <div class="modal-body">
      <label class="field">Аккаунт<select id="f-account">${data.accounts.map((a) =>
        `<option value="${esc(a.uid)}" ${a.uid === form.account ? "selected" : ""} ${a.expired ? "disabled" : ""}>${esc(a.name || a.apple_id)} · ${esc(a.apple_id)}${a.expired ? " (вход истёк)" : ""}</option>`).join("")}</select></label>
      <div class="field">Папка
        <div class="browser">
          <div class="crumbs">${crumbs.map((c) => `<button data-crumb="${c.n}">${esc(c.label)}</button>`).join(" / ")}</div>
          <div class="folder-list">${form.folders === null ? `<div class="pl-empty">Загружаю…</div>`
            : form.folders.length ? form.folders.map((f) => `<button class="folder" data-folder="${esc(f.id)}" data-folder-name="${esc(f.name)}">${esc(f.name)}</button>`).join("")
            : `<div class="pl-empty">Вложенных папок нет</div>`}</div>
        </div>
        <span class="hint">Будет подключено: <b>${esc(crumbs.map((c) => c.label).join(" → "))}</b></span></div>
      <label class="field">Имя папки в «Файлах»<input id="f-name" value="${esc(form.name)}" placeholder="${esc(title)}" autocomplete="off">
        <span class="hint mono">@icloud/${esc(form.name || title)}</span></label>
      ${form.error ? `<div class="error">${esc(form.error)}</div>` : ""}
      ${form.busy ? `<div class="working"><span class="spinner"></span>Подключаю…</div>` : ""}
    </div>
    <div class="modal-foot"><button class="btn" data-close>Отмена</button>
      <button class="btn btn-primary" data-submit ${form.busy || form.folders === null ? "disabled" : ""}>Подключить</button></div>
  </div></div>`;
}

dialog.addEventListener("click", async (e) => {
  if (e.target.dataset.closeOverlay !== undefined) { closeDialog(); return; }
  const t = e.target.closest("button");
  if (!t) return;
  const d = t.dataset;
  form.error = "";
  if (d.close !== undefined) closeDialog();
  else if (d.crumb !== undefined) browse(form.trail.slice(0, Number(d.crumb)));
  else if (d.folder) browse([...form.trail, { id: d.folder, name: d.folderName }]);
  else if (d.submit !== undefined) {
    form.busy = true;
    renderDialog();
    const last = form.trail[form.trail.length - 1];
    const body = { account: form.account, folder_id: last ? last.id : null,
                   folder_path: form.trail.length ? form.trail.map((f) => f.name).join("/") : null, name: form.name || null };
    try {
      await pulse.plugin("/mounts", { method: "POST", body });
      closeDialog();
      await load();
    } catch (err) {
      form.busy = false;
      form.error = err.message;
      renderDialog();
    }
  }
});

dialog.addEventListener("input", (e) => {
  if (e.target.id === "f-name") {
    form.name = e.target.value;
    const hint = e.target.parentElement.querySelector(".hint");
    if (hint) hint.textContent = `@icloud/${form.name || e.target.placeholder}`;
  }
});

dialog.addEventListener("change", (e) => {
  if (e.target.id === "f-account") { form.account = e.target.value; browse([]); }
});

load();
