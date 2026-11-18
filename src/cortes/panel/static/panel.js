// Painel: atualiza as seções sozinho e envia os botões/formulários sem recarregar a página.
(() => {
  const interval = Number(document.body.dataset.refresh) || 0;
  const pageReload = Number(document.body.dataset.refreshPage) || 0;
  let busy = false;

  // Não mexe numa seção enquanto você assiste um corte, está com um item aberto ou digitando.
  function inUse(scope) {
    if (scope.querySelector("details[open]")) return true;
    if ([...scope.querySelectorAll("video")].some((v) => !v.paused)) return true;
    if (scope.contains(document.activeElement) && document.activeElement !== document.body) return true;
    return [...scope.querySelectorAll("textarea")].some((t) => t.value.trim() !== "");
  }

  function setClock(text) {
    const clock = document.querySelector("[data-partial-clock]");
    if (clock) clock.textContent = text;
  }

  async function refresh() {
    if (busy || document.hidden) return;
    const sections = document.querySelectorAll("[data-partial]");
    if (!sections.length) return;
    busy = true;
    try {
      for (const section of sections) {
        if (inUse(section)) continue;
        const resp = await fetch(`/partials/${section.dataset.partial}`, { cache: "no-store" });
        if (resp.ok) section.innerHTML = await resp.text();
      }
      setClock(`atualizado às ${new Date().toLocaleTimeString("pt-BR")}`);
    } catch (err) {
      setClock("sem conexão com o painel, tentando de novo…");
    } finally {
      busy = false;
    }
  }

  async function errorText(resp) {
    try {
      const body = await resp.json();
      if (body && body.detail) return String(body.detail);
    } catch (err) { /* resposta sem JSON */ }
    return `erro ${resp.status}`;
  }

  function show(form, text, tone) {
    const out = form.querySelector("[data-result]");
    if (!out) return;
    out.textContent = text;
    out.className = `small tone-text-${tone}`;
  }

  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    const isJson = "json" in form.dataset;
    if (!isJson && !("async" in form.dataset)) return;
    event.preventDefault();
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) return;
    const button = form.querySelector("button[type=submit], button:not([type])");
    if (button) button.disabled = true;
    try {
      const options = { method: "POST" };
      if (isJson) {
        options.headers = { "Content-Type": "application/json" };
        options.body = JSON.stringify(Object.fromEntries(new FormData(form).entries()));
      }
      const resp = await fetch(form.action, options);
      if (!resp.ok && resp.status !== 303) {
        show(form, await errorText(resp), "bad");
        return;
      }
      if (isJson) {
        show(form, "Enviado. O Claude vai trabalhar nisso; acompanhe abaixo.", "ok");
        if ("reset" in form.dataset) form.querySelectorAll("textarea").forEach((t) => { t.value = ""; });
      } else {
        show(form, "Feito.", "ok");
      }
      if (document.querySelector("[data-partial]")) {
        busy = false;
        document.activeElement && document.activeElement.blur();
        await refresh();
      } else if (isJson) {
        setTimeout(() => window.location.reload(), 1200);
      }
    } catch (err) {
      show(form, "sem conexão com o painel", "bad");
    } finally {
      if (button) button.disabled = false;
    }
  });

  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  if (interval) setInterval(refresh, interval);
  // Página de pastas: recarrega sozinha enquanto houver pedido ao Claude em andamento.
  if (pageReload) {
    setInterval(() => { if (!document.hidden && !inUse(document.body)) window.location.reload(); }, pageReload);
  }
})();
