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

  // "Reiniciar sistema": o painel também reinicia. Espera ele voltar (outro pid) e recarrega.
  async function panelPid() {
    try {
      const resp = await fetch("/healthz", { cache: "no-store" });
      return resp.ok ? (await resp.json()).pid : null;
    } catch (err) {
      return null; // painel fora do ar (reiniciando)
    }
  }

  async function waitForRestart(form, oldPid) {
    show(form, "Reiniciando… o painel volta em alguns segundos.", "muted");
    const deadline = Date.now() + 90000;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 1500));
      const pid = await panelPid();
      if (pid !== null && pid !== oldPid) {
        window.location.reload();
        return;
      }
    }
    show(form, "O painel não voltou em 90 s. Veja o terminal do `cortes run`.", "bad");
    busy = false;
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
    // Excluir corte: solta o vídeo do player antes (no Windows, arquivo aberto não apaga).
    const released = [];
    if ("releaseVideo" in form.dataset) {
      const box = form.closest("article") || form.parentElement;
      for (const video of box.querySelectorAll("video")) {
        released.push([video, video.getAttribute("src")]);
        video.pause();
        video.removeAttribute("src");
        video.load();
      }
      show(form, "Excluindo…", "muted");
      await new Promise((r) => setTimeout(r, 300));
    }
    const restoreVideos = () => released.forEach(([video, src]) => { if (src) video.setAttribute("src", src); });
    const oldPid = "restart" in form.dataset ? await panelPid() : null;
    try {
      const options = { method: "POST" };
      if (isJson) {
        options.headers = { "Content-Type": "application/json" };
        options.body = JSON.stringify(Object.fromEntries(new FormData(form).entries()));
      }
      const resp = await fetch(form.action, options);
      if (!resp.ok && resp.status !== 303) {
        restoreVideos();
        show(form, await errorText(resp), "bad");
        return;
      }
      if ("reload" in form.dataset) {
        window.location.reload();
        return;
      }
      if ("restart" in form.dataset) {
        busy = true; // não atualiza as seções enquanto o painel reinicia
        await waitForRestart(form, oldPid);
        return;
      }
      if (isJson) {
        let message = "Enviado. O Claude vai trabalhar nisso; acompanhe abaixo.";
        try {
          const body = await resp.json();
          if (body && body.message) message = String(body.message);
        } catch (err) { /* resposta sem JSON */ }
        show(form, message, "ok");
        if ("reset" in form.dataset) {
          form.querySelectorAll("textarea, input[type=text]").forEach((t) => { t.value = ""; });
        }
        // "Adicionar canal": mostra a mensagem, fecha o formulário e atualiza a tabela.
        const box = "close" in form.dataset ? form.closest("details") : null;
        if (box) {
          await new Promise((r) => setTimeout(r, 2500));
          box.open = false;
        }
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
      restoreVideos();
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
