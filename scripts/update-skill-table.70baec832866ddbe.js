(() => {
  'use strict';
  const scriptURL = new URL(document.currentScript.src);
  const tableBase = new URL('../skill-transfer/', scriptURL);
  const revisionMeta = document.querySelector('meta[name="far-table-revision"]');
  const generationMeta = document.querySelector('meta[name="far-table-generation"]');
  const localGeneration = Number(generationMeta?.content || 0);
  let pending = false;
  let lastCheck = 0;

  async function checkForUpdate() {
    if (document.visibilityState === 'hidden' || pending || Date.now() - lastCheck < 10000) return;
    pending = true;
    lastCheck = Date.now();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    try {
      const manifestURL = new URL('revision.json', tableBase);
      manifestURL.searchParams.set('check', String(Date.now()));
      const response = await fetch(manifestURL, {
        cache: 'no-store', credentials: 'omit', signal: controller.signal
      });
      if (!response.ok) return;
      const release = await response.json();
      if (release.schema !== 'far.skill-table.release.v1' ||
          !/^[a-f0-9]{16}$/.test(release.revision) ||
          !Number.isSafeInteger(release.generation) ||
          release.document !== `table.${release.revision}.html`) return;
      const target = new URL(release.document, tableBase);
      const link = document.querySelector('#skill-library a.skill-link');
      if (revisionMeta) {
        if (release.generation <= localGeneration || release.revision === revisionMeta.content) return;
      } else if (!link || release.generation <= Number(link.dataset.tableGeneration || 0)) {
        return;
      }
      // Confirm the new document exists before replacing a usable page or link.
      const documentResponse = await fetch(target, {
        cache: 'no-store', credentials: 'omit', signal: controller.signal
      });
      if (documentResponse.status !== 200 || !documentResponse.headers.get('content-type')?.includes('text/html')) return;
      const documentText = await documentResponse.text();
      if (!documentText.includes(`<meta name="far-table-revision" content="${release.revision}">`) ||
          !documentText.includes(`<meta name="far-table-generation" content="${release.generation}">`)) return;
      if (revisionMeta) {
        // A stale edge response must never send a newer page backwards.
        target.hash = window.location.hash;
        window.location.replace(target.href);
      } else {
        link.href = target.href;
        link.dataset.tableGeneration = String(release.generation);
      }
    } catch {
      // The embedded table and download links remain usable when offline.
    } finally {
      clearTimeout(timeout);
      pending = false;
    }
  }

  checkForUpdate();
  window.addEventListener('pageshow', checkForUpdate);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') checkForUpdate();
  });
})();
