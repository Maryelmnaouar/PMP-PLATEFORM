// ASSETPILOT AM V6 — interactions
(function () {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

  // Menu mobile
  const burger = $('.burger');
  const side = $('.side');
  const backdrop = document.createElement('div');
  backdrop.className = 'side-backdrop';
  document.body.appendChild(backdrop);
  const setMenu = open => { if (side) side.classList.toggle('open', open); backdrop.classList.toggle('show', open); };
  if (burger) burger.addEventListener('click', () => setMenu(!side.classList.contains('open')));
  backdrop.addEventListener('click', () => setMenu(false));

  // Application installable (écran d'accueil tablette)
  if ('serviceWorker' in navigator && (location.protocol === 'https:' || location.hostname === 'localhost' || location.hostname === '127.0.0.1')) {
    window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js').catch(() => {}));
  }

  // Lignes de tableau cliquables
  $$('tr[data-href]').forEach(tr => tr.addEventListener('click', e => {
    if (e.target.closest('a,button,form,select,input')) return;
    window.location = tr.dataset.href;
  }));

  // Modales
  window.openModal = id => $('#' + id).classList.add('show');
  window.closeModal = id => $('#' + id).classList.remove('show');
  $$('.modal-bg').forEach(m => m.addEventListener('click', e => { if (e.target === m) m.classList.remove('show'); }));

  // Rejet avec commentaire obligatoire
  $$('form[data-reject]').forEach(f => f.addEventListener('submit', e => {
    const c = prompt('Motif du rejet (obligatoire) :');
    if (!c) { e.preventDefault(); return; }
    f.querySelector('input[name=comment]').value = c;
  }));

  // Formulaire utilisateur : droits par défaut / personnalisés
  const roleSel = $('#role');
  const modeSel = $('#perm_mode');
  if (roleSel && modeSel && window.ROLE_DEFAULTS) {
    const apply = () => {
      const custom = modeSel.value === 'custom';
      const isAdmin = roleSel.value === 'Administrateur';
      $$('.perm-grid input[data-key]').forEach(cb => {
        if (!custom || isAdmin) cb.checked = isAdmin || (window.ROLE_DEFAULTS[roleSel.value] || []).includes(cb.dataset.key);
        cb.disabled = !custom || isAdmin;
      });
    };
    const chefField = $('#chef-field'), supSel = $('#supervisor_id');
    const CHEF_OF = {'Opérateur': "Chef d'équipe Production", 'Technicien': "Chef d'équipe Maintenance"};
    const toggleChef = () => {
      if (!chefField) return;
      const want = CHEF_OF[roleSel.value];
      chefField.style.display = want ? '' : 'none';
      if (supSel) supSel.querySelectorAll('option[data-role]').forEach(o => { o.hidden = o.dataset.role !== want; if (o.hidden && o.selected) supSel.value = ''; });
    };
    roleSel.addEventListener('change', toggleChef); toggleChef();
    roleSel.addEventListener('change', apply);
    modeSel.addEventListener('change', apply);
    apply();
  }

  // Tableau de suivi des anomalies : glisser-déposer (souris + tactile)
  const board = $('.anom-grid');
  let suppressClick = false;
  if (board) {
    const STATUSES = JSON.parse(board.dataset.statuses);
    const isAdmin = board.dataset.admin === '1';
    let dragged = null;
    const clearZones = () => $$('.zone').forEach(z => z.classList.remove('over', 'forbid'));
    const allowed = (zone) => {
      if (!dragged || !zone) return false;
      if (zone.dataset.type !== dragged.dataset.type) return false;
      if (dragged.dataset.sap === '1' && !dragged.dataset.notif && zone.dataset.status !== 'Nouveau') return false;
      const from = STATUSES.indexOf(dragged.dataset.status), to = STATUSES.indexOf(zone.dataset.status);
      if (from === to) return false;
      if (zone.dataset.status === 'Contre-mesure' && board.dataset.cm !== '1') return false;
      return isAdmin || to === from + 1;
    };
    const markZone = z => {
      clearZones();
      if (!z) return;
      if (allowed(z)) z.classList.add('over');
      else if (dragged && z.dataset.type === dragged.dataset.type) z.classList.add('forbid');
    };
    const doDrop = (card, z) => {
      const id = card.dataset.id, to = z.dataset.status;
      if (card.dataset.sap === '1' && !card.dataset.notif && to !== 'Nouveau') {
        alert(card.dataset.code + ' doit d\'abord être validé par le chef d\'équipe production, avec le N° de l\'avis SAP.');
        return;
      }
      if (to === 'En cours' || to === 'Traité') {
        const mid = to === 'En cours' ? 'modal-encours' : 'modal-traite';
        const m = $('#' + mid), f = m.querySelector('form');
        f.action = '/tags/' + id + '/move';
        m.querySelector('.tagref').textContent = card.dataset.code;
        const isSap = card.dataset.sap === '1';
        m.querySelectorAll('.only-sap').forEach(el => el.style.display = isSap ? '' : 'none');
        const ot = f.querySelector('[name=ot_number]');
        if (ot) { ot.value = card.dataset.ot || ''; ot.required = isSap && to === 'Traité'; }
        const tb = f.querySelector('[name=treated_by]');
        if (tb) tb.value = card.dataset.treated || '';
        openModal(mid);
        return;
      }
      const fd = new FormData(); fd.append('to', to);
      fetch('/tags/' + id + '/move', { method: 'POST', body: fd, headers: { 'X-Requested-With': 'fetch' } })
        .then(r => r.json())
        .then(j => {
          if (j.redirect) { window.location = j.redirect; return; }
          if (!j.ok) { alert(j.message); return; }
          window.location.reload();
        })
        .catch(() => alert('Erreur de communication avec le serveur.'));
    };

    // Souris (glisser-déposer HTML5)
    $$('.tagcard[draggable=true]').forEach(c => {
      c.addEventListener('dragstart', e => {
        dragged = c; c.classList.add('dragging');
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', c.dataset.id);
      });
      c.addEventListener('dragend', () => { c.classList.remove('dragging'); dragged = null; clearZones(); });
    });
    $$('.zone').forEach(z => {
      z.addEventListener('dragover', e => { if (allowed(z)) e.preventDefault(); markZone(z); });
      z.addEventListener('dragleave', () => z.classList.remove('over', 'forbid'));
      z.addEventListener('drop', e => {
        e.preventDefault(); clearZones();
        if (allowed(z)) doDrop(dragged, z);
      });
    });

    // Tactile : appui long (0,35 s) sur une étiquette, puis glisser avec le doigt
    let pressTimer = null, start = null, ghost = null, offX = 0, offY = 0, lastZone = null, scrollV = 0, raf = null;
    const zoneAt = (x, y) => {
      if (ghost) ghost.style.visibility = 'hidden';
      const el = document.elementFromPoint(x, y);
      if (ghost) ghost.style.visibility = '';
      return el ? el.closest('.zone') : null;
    };
    const autoScroll = () => {
      if (!ghost) { raf = null; return; }
      if (scrollV) window.scrollBy(0, scrollV);
      raf = requestAnimationFrame(autoScroll);
    };
    const endTouchDrag = (x, y, cancel) => {
      clearTimeout(pressTimer); pressTimer = null;
      if (!ghost) return;
      const card = dragged;
      ghost.remove(); ghost = null; scrollV = 0;
      card.classList.remove('dragging');
      document.body.classList.remove('touch-dragging');
      const z = cancel ? null : zoneAt(x, y);
      const ok = z && allowed(z);
      clearZones();
      if (ok) doDrop(card, z);
      dragged = null;
      suppressClick = true; setTimeout(() => { suppressClick = false; }, 400);
    };
    $$('.tagcard[draggable=true]').forEach(c => {
      c.addEventListener('touchstart', e => {
        if (e.touches.length !== 1 || c.classList.contains('under')) return;
        const t = e.touches[0];
        start = { x: t.clientX, y: t.clientY };
        pressTimer = setTimeout(() => {
          pressTimer = null;
          dragged = c;
          const r = c.getBoundingClientRect();
          offX = start.x - r.left; offY = start.y - r.top;
          ghost = c.cloneNode(true);
          ghost.classList.add('touch-ghost');
          ghost.style.width = r.width + 'px';
          ghost.style.left = r.left + 'px'; ghost.style.top = r.top + 'px';
          document.body.appendChild(ghost);
          c.classList.add('dragging');
          document.body.classList.add('touch-dragging');
          if (navigator.vibrate) navigator.vibrate(25);
          if (!raf) raf = requestAnimationFrame(autoScroll);
        }, 350);
      }, { passive: true });
      c.addEventListener('touchmove', e => {
        const t = e.touches[0];
        if (!ghost) {
          if (pressTimer && start && Math.hypot(t.clientX - start.x, t.clientY - start.y) > 10) { clearTimeout(pressTimer); pressTimer = null; }
          return;
        }
        e.preventDefault();
        ghost.style.left = (t.clientX - offX) + 'px';
        ghost.style.top = (t.clientY - offY) + 'px';
        const z = zoneAt(t.clientX, t.clientY);
        if (z !== lastZone) { markZone(z); lastZone = z; }
        const edge = 70;
        scrollV = t.clientY < edge ? -14 : (t.clientY > window.innerHeight - edge ? 14 : 0);
      }, { passive: false });
      c.addEventListener('touchend', e => {
        const t = e.changedTouches[0];
        if (ghost) { e.preventDefault(); endTouchDrag(t.clientX, t.clientY, false); }
        else { clearTimeout(pressTimer); pressTimer = null; }
      });
      c.addEventListener('touchcancel', () => endTouchDrag(0, 0, true));
      c.addEventListener('contextmenu', e => { if (window.matchMedia('(pointer: coarse)').matches) e.preventDefault(); });
    });
  }

  // Piles d'étiquettes : chaque niveau est replié en pile, un clic la déplie
  if (board) {
    const store = {
      get(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } },
      set(k, v) { try { v ? sessionStorage.setItem(k, v) : sessionStorage.removeItem(k); } catch (e) {} }
    };
    const zones = $$('.zone', board);
    const key = z => 'ap-open:' + z.dataset.type + ':' + z.dataset.status;
    const layout = z => {
      const cards = $$('.tagcard', z);
      const open = z.classList.contains('open') || cards.length < 2;
      z.classList.toggle('stacked', !open);
      cards.forEach((c, i) => {
        c.style.zIndex = String(cards.length - i);
        c.classList.toggle('under', !open && i > 0);
        if (open || i === 0) { c.style.marginTop = '0px'; c.style.transform = ''; c.style.opacity = ''; return; }
        const depth = Math.min(i, 3);
        const peek = i <= 3 ? 9 : 0;
        c.style.marginTop = (peek - c.offsetHeight - 7) + 'px';
        c.style.transform = 'scale(' + (1 - 0.045 * depth) + ')';
        c.style.opacity = i <= 3 ? '1' : '0';
      });
      const t = z._toggle;
      if (t) {
        t.classList.toggle('open', open);
        t.querySelector('.lbl-t').textContent = open ? 'Replier la pile' : 'Voir les ' + cards.length + ' TAGs';
      }
    };
    zones.forEach(z => {
      const cards = $$('.tagcard', z);
      if (cards.length < 2) return;
      const count = document.createElement('span');
      count.className = 'stack-count';
      count.textContent = '+' + (cards.length - 1);
      z.appendChild(count);
      const t = document.createElement('button');
      t.type = 'button'; t.className = 'stack-toggle';
      t.innerHTML = '<span class="lbl-t"></span><span class="chev">▾</span>';
      z.after(t); z._toggle = t;
      if (store.get(key(z))) z.classList.add('open');
      const toggle = () => { z.classList.toggle('open'); store.set(key(z), z.classList.contains('open') ? '1' : ''); layout(z); };
      t.addEventListener('click', toggle);
      z.addEventListener('click', e => {
        if (suppressClick || !z.classList.contains('stacked') || e.target.closest('a')) return;
        toggle();
      });
    });
    board.classList.add('board-static');
    zones.forEach(layout);
    requestAnimationFrame(() => requestAnimationFrame(() => board.classList.remove('board-static')));
    let rt; window.addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(() => zones.forEach(layout), 150); });
    const all = $('#stack-all');
    if (all) all.addEventListener('click', () => {
      const openAll = all.dataset.state !== 'open';
      zones.forEach(z => { if ($$('.tagcard', z).length > 1) { z.classList.toggle('open', openAll); store.set(key(z), openAll ? '1' : ''); layout(z); } });
      all.dataset.state = openAll ? 'open' : '';
      all.textContent = openAll ? 'Tout replier' : 'Tout déplier';
    });
  }

  // Animations d'entrée en cascade
  const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (!reduce) {
    const groups = ['.page > .filters', '.page > .card', '.page > .grid > *', '.page > .split > *', '.page > .split > div > .card',
      '.page > .form-a6 > *', '.page > form > .form-a6 > *', '.anom-grid > div', '.sgab-grid > div', '.syn > .eq',
      '.typesel label', '.check', '.timeline > div', '.tabs a', '.lvl-legend', '.anom-title, .anom-sub'];
    let n = 0;
    groups.forEach(sel => $$(sel).forEach(el => {
      if (el.classList.contains('anim')) return;
      el.style.setProperty('--d', Math.min(n++ * 45, 700) + 'ms');
      el.classList.add('anim');
      el.addEventListener('animationend', ev => { if (ev.target === el) { el.classList.remove('anim'); } }, { once: true });
    }));
    $$('table').forEach(tb => $$('tr', tb).slice(1, 25).forEach((tr, i) => {
      tr.style.setProperty('--d', (150 + i * 30) + 'ms'); tr.classList.add('anim-fade');
    }));
    $$('.trend .wk').forEach((w, i) => $$('.bars i', w).forEach(b => b.style.setProperty('--d', (200 + i * 70) + 'ms')));
    // Compteurs animés
    const countUp = el => {
      const m = el.textContent.trim().match(/^(\d+)(\s*%|\s*SOP)?$/);
      if (!m) return;
      const target = parseInt(m[1], 10), suffix = m[2] || '';
      if (target === 0) return;
      const t0 = performance.now(), dur = 900 + Math.min(target, 200) * 2;
      const step = now => {
        const p = Math.min((now - t0) / dur, 1), e = 1 - Math.pow(1 - p, 3);
        el.textContent = Math.round(target * e) + suffix;
        if (p < 1) requestAnimationFrame(step);
      };
      el.textContent = '0' + suffix;
      requestAnimationFrame(step);
    };
    $$('.kpi b, .blk-b .big').forEach(countUp);
    $$('.donut > div').forEach(d => { const f = d.firstChild; if (f && f.nodeType === 3) { const sp = document.createElement('span'); sp.textContent = f.textContent.trim(); d.replaceChild(sp, f); countUp(sp); } });
  }
  // Ombre de l'en-tête au défilement
  const head = $('header.top');
  if (head) { const sh = () => head.classList.toggle('scrolled', window.scrollY > 4); window.addEventListener('scroll', sh, { passive: true }); sh(); }

  // Formulaire TAG : sélection du type depuis la couleur
  $$('.typesel input').forEach(r => r.addEventListener('change', () => {
    const p = $('#a6-preview'); if (p) { p.className = 'a6 ' + r.value; p.querySelector('.ttl').textContent = 'ÉTIQUETTE ' + ({AM:'BLEUE', RCM:'ROUGE', SAFETY:'JAUNE'})[r.value];
      const ol = $('#pv-otline'); if (ol) ol.style.display = r.value === 'AM' ? 'none' : ''; }
  }));
  // Aperçu étiquette en direct
  const live = (src, dst) => { const s = $(src), d = $(dst); if (s && d) s.addEventListener('input', () => d.textContent = (s.tagName === 'SELECT' ? s.options[s.selectedIndex].text : s.value) || '…'); };
  live('#equipment_id', '#pv-machine'); live('#organe', '#pv-organe'); live('#description', '#pv-desc');
  live('#anomaly_type', '#pv-atype'); live('#observed_at', '#pv-obs');
  const obs = $('#observed_by'); if (obs) obs.addEventListener('change', () => $('#pv-observer').textContent = obs.value ? obs.options[obs.selectedIndex].text.replace(/ \(.*\)$/, '') : '');
  const eqs = $('#equipment_id'); if (eqs) eqs.addEventListener('change', () => $('#pv-machine').textContent = eqs.options[eqs.selectedIndex].text);
  const ats = $('#anomaly_type'); if (ats) ats.addEventListener('change', () => $('#pv-atype').textContent = ats.value);

  // Paramètres : listes en cascade Usine → Ligne → SGA
  const plantSel = $('[data-cascade=plant]');
  const lineSel = $('[data-cascade=line]');
  const sgaSel = $('[data-cascade=sga]');
  if (plantSel && lineSel) plantSel.addEventListener('change', () => {
    fetch('/api/lines?plant_id=' + plantSel.value).then(r => r.json()).then(rows => {
      lineSel.innerHTML = '<option value="">— Ligne —</option>' + rows.map(r => `<option value="${r.id}">${r.name}</option>`).join('');
      if (sgaSel) sgaSel.innerHTML = '<option value="">— Aucune SGA —</option>';
    });
  });
  if (lineSel && sgaSel) lineSel.addEventListener('change', () => {
    fetch('/api/sgas?line_id=' + lineSel.value).then(r => r.json()).then(rows => {
      sgaSel.innerHTML = '<option value="">— Aucune SGA —</option>' + rows.map(r => `<option value="${r.id}">${r.name}</option>`).join('');
    });
  });
})();

// Copier dans le presse-papiers (préparation de l'avis Fiori)
document.querySelectorAll('[data-copy]').forEach(b => b.addEventListener('click', () => {
  const el = document.getElementById(b.dataset.copy);
  const txt = el ? (el.value !== undefined && el.tagName !== 'DIV' && el.tagName !== 'CODE' ? el.value : el.textContent) : '';
  const done = () => { const o = b.textContent; b.textContent = 'Copié ✓'; setTimeout(() => b.textContent = o, 1400); };
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(txt).then(done).catch(() => fallback());
  else fallback();
  function fallback() { const t = document.createElement('textarea'); t.value = txt; document.body.appendChild(t); t.select(); try { document.execCommand('copy'); done(); } catch (e) {} t.remove(); }
}));
