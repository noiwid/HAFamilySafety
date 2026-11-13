/*
 * Microsoft Family Safety - dashboard card (custom:familysafety-card).
 *
 * One card per child: header (today's time on Windows, lock state), the locks and the web
 * settings as tiles, the pending request, then tabs (today's apps, all apps, blocked apps,
 * websites, weekly limits). Every line opens a sheet whose draft lives in the sheet itself:
 * no helper and no script is needed, Save calls the integration's services directly.
 *
 * Plain custom element, no build step and no dependency.
 */
const DOMAIN = 'microsoft_family_safety';
const DAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'];
const DOW = { sunday: 0, monday: 1, tuesday: 2, wednesday: 3, thursday: 4, friday: 5, saturday: 6 };

const STR = {
	en: {
		today: 'Today', apps: 'Apps', blocked_tab: 'blocked', sites: 'Websites', week: 'Week', installed: 'installed', limited: 'limited', per_day: ' / day',
		apps_label: 'Applications', sites_label: 'Websites (7 days)', week_label: 'Weekly limits', cur_limited: 'Currently limited to {t} per day',
		lock: 'Lock', windows: 'Windows', xbox: 'Xbox', web: 'Web', limits: 'Limits',
		no_usage: 'No app used today', no_app: 'No app', no_site: 'No website', blocked: 'blocked', allowed: 'allowed', app_blocked: 'blocked',
		visits: 'visits in 7 days', lists: 'Your lists', add_site: 'Add a website',
		request: 'Request', more_time: 'More time on {p}', requested: 'requested at {t}',
		app: 'Application', cur_blocked: 'Currently blocked', cur_allowed: 'Currently allowed', used_today: '{t} today',
		none: 'No limit', none_sub: 'Only the overall screen time applies',
		limit: 'Limited', limit_sub: 'Per day, on Windows', block: 'Blocked', block_sub: 'Can no longer be opened',
		site: 'Website', site_new: 'Type the domain, then pick a rule', allow_site: 'Always allowed', allow_site_sub: 'Even when the web filter would block it',
		block_site: 'Always blocked', block_site_sub: 'Edge will refuse to open it', remove_site: 'Remove from the lists', remove_site_sub: 'The web filter decides again',
		web_title: 'Web and content', filter: 'Web filter', filter_sub: 'Blocks inappropriate sites in Edge',
		age: 'Age rating', age_sub: 'Apps, games and media', any_age: 'Any age',
		buy: 'Ask to buy', buy_sub: 'Purchases need your approval', buy_unknown: 'Current setting not readable: tap to choose',
		lock_title: 'Lock', lock_account: 'Whole account', lock_account_sub: 'Every day set to zero, restored on unlock',
		lock_windows_sub: 'Windows devices, for a set time', lock_xbox_sub: 'The console, for a set time',
		duration: 'For', duration_sub: 'Windows and Xbox locks only', unlock: 'Unlock',
		extra: 'Extra time', extra_sub: 'Added to today', approve: 'Approve', deny: 'Deny',
		day: 'Day', daily: 'Daily limit', daily_sub: 'Screen time allowed that day', from: 'Allowed from', until: 'Allowed until', step30: 'In 30 min steps',
		save: 'Save', saving: 'Saving…', close: 'Close', missing: 'Entity not found: ', h: 'h', d: 'day', ds: 'days',
		st_locked: 'Locked', st_extra_time: 'Extra time', st_no_limits: 'No limits', st_outside_allowed_hours: 'Outside allowed hours',
		st_allowance_used: 'Time used up', st_within_limits: 'Allowed', st_unknown: '', left: '{t} left', downtime_at: 'downtime {t}', downtime_now: 'downtime until {t}',
		paused: 'paused', pause: 'Paused', pause_sub: 'Blocked for now, its limit comes back on resume', cur_paused: 'Currently paused',
		resume: 'Resume', resume_sub: 'Unblocked, with the limit it had before', keep_paused: 'Keep paused', keep_paused_sub: 'Stays blocked',
		app_hours: 'Allowed between', app_hours_sub: 'Its time can only be used in this window', app_from: 'From', app_until: 'Until',
		lists_loading: 'Reading your lists…', lists_error: 'Lists not readable: ', several: 'One domain, or several separated by spaces',
		settings: 'Settings', limits_sw: 'Screen time limits', limits_sw_sub: 'Off: no daily limit, the schedule comes back when on',
		xbox_limits_sw: 'Xbox limits', xbox_limits_sw_sub: 'The console follows its own schedule', app_limits_sw: 'App limits',
		app_limits_sw_sub: 'Off: no per-app limit is enforced', web_row: 'Web and content', web_row_sub: 'Filter, age rating, ask to buy',
		conn: 'Connection', conn_ok: 'Connected to Microsoft', conn_degraded: 'Partly connected: {r}', conn_reauth: 'Sign-in needed',
		conn_terms: 'Microsoft asks to accept updated terms on account.microsoft.com', conn_proof: 'Microsoft asks to confirm your identity: sign in again',
		reauth: 'Sign in again', reauth_sub: 'Opens the Microsoft sign-in in Home Assistant', reauth_done: 'Check the notification to finish signing in',
		configure: 'Configure the integration', configure_sub: 'Users allowed, platforms, Xbox link',
		bonus: 'Extra time on the Xbox', bonus_sub: 'Added now, ends by itself', add_bonus: 'Add extra time', cancel_bonus: 'Cancel extra time',
		cancel_bonus_sub: 'Ends the extra time running now',
		dnames: { monday: 'Monday', tuesday: 'Tuesday', wednesday: 'Wednesday', thursday: 'Thursday', friday: 'Friday', saturday: 'Saturday', sunday: 'Sunday' },
	},
	fr: {
		today: "Aujourd'hui", apps: 'Applis', blocked_tab: 'bloquées', sites: 'Sites', week: 'Semaine', installed: 'installées', limited: 'limitées', per_day: ' / jour',
		apps_label: 'Applications', sites_label: 'Sites web (7 jours)', week_label: 'Limites de la semaine', cur_limited: 'Actuellement limitée à {t} par jour',
		lock: 'Verrou', windows: 'Windows', xbox: 'Xbox', web: 'Web', limits: 'Limites',
		no_usage: "Aucune appli utilisée aujourd'hui", no_app: 'Aucune appli', no_site: 'Aucun site', blocked: 'bloqué', allowed: 'autorisé', app_blocked: 'bloquée',
		visits: 'visites en 7 jours', lists: 'Vos listes', add_site: 'Ajouter un site',
		request: 'Demande', more_time: 'Plus de temps sur {p}', requested: 'demandé à {t}',
		app: 'Application', cur_blocked: 'Actuellement bloquée', cur_allowed: 'Actuellement autorisée', used_today: "{t} aujourd'hui",
		none: 'Sans limite', none_sub: "Seul le temps d'écran global s'applique",
		limit: 'Limitée', limit_sub: 'Par jour, sur Windows', block: 'Bloquée', block_sub: 'Ne peut plus être ouverte',
		site: 'Site web', site_new: 'Saisissez le domaine, puis choisissez une règle', allow_site: 'Toujours autorisé', allow_site_sub: 'Même si le filtre web le bloquerait',
		block_site: 'Toujours bloqué', block_site_sub: "Edge refusera de l'ouvrir", remove_site: 'Retirer des listes', remove_site_sub: 'Le filtre web décide à nouveau',
		web_title: 'Web et contenus', filter: 'Filtre web', filter_sub: 'Bloque les sites inappropriés dans Edge',
		age: 'Âge autorisé', age_sub: 'Applis, jeux et médias', any_age: 'Tout âge',
		buy: "Demander avant d'acheter", buy_sub: 'Les achats demandent votre accord', buy_unknown: 'Réglage actuel illisible : touchez pour choisir',
		lock_title: 'Verrou', lock_account: 'Compte entier', lock_account_sub: 'Chaque jour à zéro, rétabli au déverrouillage',
		lock_windows_sub: 'Appareils Windows, pour une durée', lock_xbox_sub: 'La console, pour une durée',
		duration: 'Pendant', duration_sub: 'Verrous Windows et Xbox seulement', unlock: 'Déverrouiller',
		extra: 'Temps en plus', extra_sub: "Ajouté à aujourd'hui", approve: 'Accepter', deny: 'Refuser',
		day: 'Jour', daily: 'Limite du jour', daily_sub: "Temps d'écran autorisé ce jour-là", from: 'Autorisé à partir de', until: "Autorisé jusqu'à", step30: 'Par pas de 30 min',
		save: 'Enregistrer', saving: 'Enregistrement…', close: 'Fermer', missing: 'Entité introuvable : ', h: 'h', d: 'jour', ds: 'jours',
		st_locked: 'Verrouillé', st_extra_time: 'Temps en plus', st_no_limits: 'Sans limite', st_outside_allowed_hours: 'Hors plage autorisée',
		st_allowance_used: 'Temps écoulé', st_within_limits: 'Autorisé', st_unknown: '', left: 'reste {t}', downtime_at: 'coupure à {t}', downtime_now: "coupé jusqu'à {t}",
		paused: 'en pause', pause: 'En pause', pause_sub: 'Bloquée pour le moment, sa limite revient à la reprise', cur_paused: 'Actuellement en pause',
		resume: 'Reprendre', resume_sub: "Débloquée, avec la limite qu'elle avait", keep_paused: 'Laisser en pause', keep_paused_sub: 'Reste bloquée',
		app_hours: 'Autorisée entre', app_hours_sub: 'Son temps ne se prend que dans cette plage', app_from: 'De', app_until: 'À',
		lists_loading: 'Lecture de vos listes…', lists_error: 'Listes illisibles : ', several: 'Un domaine, ou plusieurs séparés par des espaces',
		settings: 'Réglages', limits_sw: "Limites de temps d'écran", limits_sw_sub: 'Coupées : plus de limite du jour, le planning revient à la réactivation',
		xbox_limits_sw: 'Limites Xbox', xbox_limits_sw_sub: 'La console suit son propre planning', app_limits_sw: 'Limites des applis',
		app_limits_sw_sub: "Coupées : aucune limite d'appli ne s'applique", web_row: 'Web et contenus', web_row_sub: "Filtre, âge, demander avant d'acheter",
		conn: 'Connexion', conn_ok: 'Connecté à Microsoft', conn_degraded: 'Connexion partielle : {r}', conn_reauth: 'Reconnexion nécessaire',
		conn_terms: 'Microsoft demande d’accepter ses nouvelles conditions sur account.microsoft.com', conn_proof: 'Microsoft demande de confirmer votre identité : reconnectez-vous',
		reauth: 'Se reconnecter', reauth_sub: 'Ouvre la connexion Microsoft dans Home Assistant', reauth_done: 'Ouvrez la notification pour terminer la connexion',
		configure: "Configurer l'intégration", configure_sub: 'Utilisateurs autorisés, plateformes, liaison Xbox',
		bonus: 'Temps en plus sur la Xbox', bonus_sub: 'Ajouté maintenant, se termine seul', add_bonus: 'Ajouter du temps', cancel_bonus: 'Annuler le temps en plus',
		cancel_bonus_sub: 'Arrête le temps en plus en cours',
		dnames: { monday: 'Lundi', tuesday: 'Mardi', wednesday: 'Mercredi', thursday: 'Jeudi', friday: 'Vendredi', saturday: 'Samedi', sunday: 'Dimanche' },
	},
};
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const hm = (m) => { m = Math.round(m); const h = Math.floor(m / 60); return h ? `${h} h${m % 60 ? ' ' + String(m % 60).padStart(2, '0') : ''}` : `${m} min`; };
const hhmm = (min) => `${String(Math.floor(min / 60) % 24).padStart(2, '0')}h${String(min % 60).padStart(2, '0')}`;
const toMin = (s, dflt) => { const m = /^(\d{1,2}):(\d{2})/.exec(String(s || '')); return m ? Number(m[1]) * 60 + Number(m[2]) : dflt; };
const domain = (url) => String(url || '').replace(/^https?:\/\//i, '').replace(/\/.*$/, '');
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

/* role -> [domain, end of the entity id]: more specific endings first ("_lock" last) */
const ROLES = [
	['allowed_now', 'binary_sensor', '_windows_allowed_now'], ['downtime', 'binary_sensor', '_windows_downtime_active'],
	['time_remaining', 'sensor', '_windows_time_remaining'], ['websites', 'sensor', '_websites_visited'], ['web_filter', 'sensor', '_web_filter'],
	['pending_requests', 'sensor', '_pending_requests'], ['applications', 'sensor', '_applications'], ['screen_time', 'sensor', '_screen_time'],
	['windows_lock', 'switch', '_windows_lock'], ['xbox_lock', 'switch', '_xbox_lock'], ['xbox_limits', 'switch', '_xbox_screen_time_limits'],
	['limits', 'switch', '_screen_time_limits'], ['app_limits', 'switch', '_app_limits'], ['lock', 'switch', '_lock'],
];

/** The child's entities from its Family Safety device (the "… (Family Safety)" device of the child). */
function resolveChild(hass, device) {
	const ids = Object.values(hass.entities || {}).filter((e) => e.device_id === device && e.platform === DOMAIN).map((e) => e.entity_id);
	const used = new Set(), child = {};
	for (const [role, domainName, end] of ROLES) {
		const id = ids.find((x) => !used.has(x) && x.startsWith(domainName + '.') && x.endsWith(end) && !(role === 'screen_time' && /_(xbox|windows)_screen_time$/.test(x)));
		// an Xbox switch of an entry without Xbox stays unavailable: no tile for it
		if (id && !(hass.states[id] && hass.states[id].state === 'unavailable' && role.startsWith('xbox'))) { child[role] = id; used.add(id); }
	}
	const monday = ids.find((x) => x.startsWith('number.') && x.endsWith('_monday_limit') && !x.includes('_xbox_'));
	const name = (hass.devices && hass.devices[device] && (hass.devices[device].name_by_user || hass.devices[device].name)) || '';
	return { child, schedule: monday ? monday.slice(7, -'_monday_limit'.length) : undefined, title: name.replace(/\s*\(Family Safety\)\s*$/, '') };
}

class FamilySafetyCard extends HTMLElement {
	setConfig(config) {
		if (!config || !((config.child && config.child.screen_time) || config.device)) {
			throw new Error('familysafety-card: choose the child\'s Family Safety device ("device"), or give "child" entities');
		}
		this._raw = config;
		this._config = config.child ? config : null;
		this._tab = 'today';
		this._seen = '';
		if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
	}

	/** The default config of the card picker: the first child with a screen time sensor. */
	static getConfigElement() { return document.createElement('familysafety-card-editor'); }

	static getStubConfig(hass) {
		const e = Object.values(hass.entities || {}).find((x) => x.platform === DOMAIN && x.device_id && x.entity_id.startsWith('sensor.') && /_screen_time$/.test(x.entity_id)
			&& !/_(xbox|windows)_screen_time$/.test(x.entity_id) && Object.values(hass.entities).some((y) => y.device_id === x.device_id && y.entity_id.endsWith('_applications')));
		return { device: e ? e.device_id : '' };
	}

	getCardSize() { return this._compact() ? 5 : 9; }
	getGridOptions() { return { columns: 12, min_columns: 6, rows: 'auto' }; }

	/** The card picker preview is small: header, tiles and today's top apps only. */
	_compact() { return this._inPicker(); }

	connectedCallback() {
		// where the card sits is only known once it is in the page: render again for the picker
		if (this._hass && this._picker === undefined) { this._seen = ''; this.hass = this._hass; }
	}

	/** true inside the "add card" picker, whose preview boxes are small (not in a dashboard being edited) */
	_inPicker() {
		if (this._picker === undefined && this.isConnected) {
			let n = this;
			for (let i = 0; i < 40 && n; i++) {
				if (n.tagName === 'HUI-CARD-PICKER') { this._picker = true; break; }
				n = n.parentNode || (n.host || null);
			}
			if (this._picker === undefined) this._picker = false;
		}
		return !!this._picker;
	}

	set hass(hass) {
		this._hass = hass;
		if (!this._config) {
			const found = resolveChild(hass, this._raw.device);
			if (!found.child.screen_time) {
				this.shadowRoot.innerHTML = `<style>${FamilySafetyCard.css}</style><ha-card><div class="warn" style="padding:16px">${esc(this._t('missing'))}${esc(this._raw.device || 'device')}</div></ha-card>`;
				return;
			}
			this._config = { ...found, ...this._raw, child: { ...found.child, ...(this._raw.entities || {}) }, title: this._raw.title || found.title };
		}
		if (this._dialog) this._dialog.hass = hass;
		// Called on every state change of the whole instance: only the states read here may re-render.
		const seen = this._watched().map((id) => { const s = hass.states[id]; return s ? s.last_updated : 'x'; }).join('|') + hass.language;
		if (seen === this._seen) return;
		this._seen = seen;
		this._render();
	}

	_watched() {
		const c = this._config, ids = Object.values(c.child).filter((v) => typeof v === 'string' && v.includes('.'));
		if (c.schedule) DAYS.forEach((d) => ids.push(...this._dayIds(d, false), ...(c.child.xbox_lock ? [this._dayIds(d, true)[0]] : [])));
		return ids;
	}

	/** number/time entities of a weekday, from the "schedule" prefix (e.g. firstname_lastname_family_safety_firstname). */
	_dayIds(day, xbox) {
		const p = this._config.schedule;
		return xbox ? [`number.${p}_xbox_${day}_limit`] : [`number.${p}_${day}_limit`, `time.${p}_${day}_start`, `time.${p}_${day}_end`];
	}

	_t(key) { return (STR[((this._raw && this._raw.language) || this._hass.language || 'en').slice(0, 2)] || STR.en)[key] ?? STR.en[key]; }
	_state(id) { return id ? this._hass.states[id] : undefined; }
	_num(id) { const s = this._state(id); return s ? parseFloat(s.state) : NaN; }
	_attr(id, key, dflt) { const s = this._state(id); return (s && s.attributes[key]) ?? dflt; }

	_render() {
		const c = this._config, ch = c.child, t = (k) => this._t(k), compact = this._compact();
		const apps = this._attr(ch.applications, 'applications', []);
		const used = this._num(ch.screen_time);
		const today = DAYS[(new Date().getDay() + 6) % 7];
		const limit = c.schedule ? this._num(this._dayIds(today, false)[0]) : NaN;
		const pct = limit > 0 ? Math.min(100, (used / limit) * 100) : 0;
		const acc = this._state(ch.lock), accLocked = acc && acc.state === 'on';
		const reqs = this._attr(ch.pending_requests, 'requests', []);
		const paused = new Set(this._attr(ch.applications, 'paused_apps', []).filter((p) => p.paused === 'ours').map((p) => p.app_id));
		// live status: Allowed Now (reason), Time Remaining, Downtime (current or next)
		const now = this._state(ch.allowed_now), rem = this._num(ch.time_remaining), down = this._state(ch.downtime);
		const clock = (iso) => { const d = iso ? new Date(iso) : null; return d && !isNaN(d) ? d.toLocaleTimeString(this._hass.language, { hour: '2-digit', minute: '2-digit' }) : ''; };
		const status = now ? [
			`<b style="color:var(${now.state === 'on' ? '--success-color' : '--error-color'})">${esc(t('st_' + (now.attributes.reason || 'unknown')) || (now.state === 'on' ? t('st_within_limits') : ''))}</b>`,
			!isNaN(rem) && now.state === 'on' && now.attributes.reason !== 'no_limits' ? esc(t('left').replace('{t}', hm(rem))) : '',
			down && down.state === 'on' ? esc(t('downtime_now').replace('{t}', clock(down.attributes.downtime_end)))
				: down && down.attributes.downtime_start ? esc(t('downtime_at').replace('{t}', clock(down.attributes.downtime_start))) : '',
		].filter(Boolean).join(' · ') : '';
		const tile = (key, icon, label, on, col) => `<button class="tile${on ? ' on' : ''}" data-tile="${key}" style="--on:var(${col})"><ha-icon icon="${icon}"></ha-icon><span>${esc(label)}</span></button>`;
		const tiles = [
			ch.lock && tile('Account', accLocked ? 'mdi:lock' : 'mdi:lock-open-variant', t('lock'), accLocked, '--error-color'),
			ch.windows_lock && tile('Windows', 'mdi:microsoft-windows', t('windows'), this._state(ch.windows_lock)?.state === 'on', '--error-color'),
			ch.xbox_lock && tile('Xbox', 'mdi:microsoft-xbox', t('xbox'), this._state(ch.xbox_lock)?.state === 'on', '--error-color'),
			ch.web_filter && tile('web', 'mdi:web-check', t('web'), this._state(ch.web_filter)?.state === 'enabled', '--success-color'),
		].filter(Boolean).join('');
		const req = reqs[0];
		const reqLine = req ? (() => {
			const label = req.type === 'DeviceScreenTime' ? t('more_time').replace('{p}', req.platform || '?') : (req.appName || req.title || req.type || t('request'));
			let when = '';
			if (req.requestedTime) {
				let iso = String(req.requestedTime).replace(/(\.\d{3})\d+/, '$1');
				if (!/(Z|[+-]\d\d:?\d\d)$/.test(iso)) iso += 'Z';
				const d = new Date(iso);
				if (!isNaN(d)) when = ' · ' + t('requested').replace('{t}', d.toLocaleTimeString(this._hass.language, { hour: '2-digit', minute: '2-digit' }));
			}
			return `<button class="req" data-req="0"><ha-icon icon="mdi:hand-back-right"></ha-icon><span>${esc(label)}${esc(when)}${reqs.length > 1 ? ' · +' + (reqs.length - 1) : ''}</span><ha-icon icon="mdi:chevron-right"></ha-icon></button>`;
		})() : '';

		// apps as on Family Link: a rule per app (block, pause, limit, none), four tabs
		const rule = (a) => (paused.has(a.app_id) ? 'pause' : a.blocked ? 'block' : a.limit_minutes > 0 && a.limit_enabled !== false ? 'limit' : 'none');
		const mark = { block: 'mdi:block-helper', pause: 'mdi:pause-circle-outline', limit: 'mdi:timer-sand' };
		const appRow = (a, right, bar) => `
			<div class="row" data-app="${esc(a.app_id)}">
				<div class="name"><span>${esc(a.app_name)}</span>${mark[rule(a)] ? `<ha-icon icon="${mark[rule(a)]}"></ha-icon>` : ''}</div>
				<div class="right">${right}</div>
				${bar === undefined ? '' : `<div class="track"><div style="width:${bar}%"></div></div>`}
			</div>`;
		let list;
		if (this._tab === 'today') {
			const u = apps.filter((a) => a.usage_minutes > 0).sort((a, b) => b.usage_minutes - a.usage_minutes).slice(0, compact ? 3 : 7);
			const max = Math.max(1, ...u.map((a) => a.usage_minutes));
			list = u.length ? u.map((a) => appRow(a, hm(a.usage_minutes), (a.usage_minutes / max) * 100)).join('') : `<div class="empty">${t('no_usage')}</div>`;
		} else {
			const pick = { installed: () => true, limited: (a) => rule(a) === 'limit', blocked: (a) => rule(a) === 'block' || rule(a) === 'pause' }[this._tab] || (() => true);
			const rows = apps.filter(pick).sort((a, b) => String(a.app_name).localeCompare(String(b.app_name)));
			list = !rows.length ? `<div class="empty">${t('no_app')}</div>`
				: this._tab === 'limited' ? rows.map((a) => appRow(a, hm(a.limit_minutes) + t('per_day'))).join('')
				: `<div class="cols">${rows.map((a) => appRow(a, '')).join('')}</div>`;
		}
		let sitesPart = '';
		if (!compact && c.show_websites !== false && (ch.websites || ch.web_filter)) {
			const sites = this._attr(ch.websites, 'sites', []);
			const ex = this._lists();
			const siteRow = (d, right, bad) => `<div class="row" data-site="${esc(d)}"><div class="name"><span>${esc(d)}</span>${bad === undefined ? '' : `<em class="${bad ? 'bad' : 'ok'}">${bad ? t('blocked') : t('allowed')}</em>`}</div><div class="right">${right}</div></div>`;
			// get_sites failing falls back on the web filter sensor's copy; the reason only shows when that copy is empty too
			const lists = !this._sites && !ex.length ? `<div class="empty">${t('lists_loading')}</div>`
				: ex.map((e) => siteRow(e.domain, '', !e.allowed)).join('')
					+ (this._sites && this._sites.error && !ex.length ? `<div class="empty small">${esc(t('lists_error') + this._sites.error)}</div>` : '');
			sitesPart = `<div class="label">${t('sites_label')}<button class="add" data-add-site aria-label="${esc(t('add_site'))}" data-tip="${esc(t('add_site'))}"><ha-icon icon="mdi:plus"></ha-icon></button></div>
				<div class="list"><div class="sub">${esc(this._state(ch.websites)?.state ?? 0)} ${t('visits')}</div>`
				+ (sites.length ? sites.slice(0, 8).map((s) => siteRow(s.domain, esc(s.visits), s.blocked ? true : undefined)).join('') : `<div class="empty">${t('no_site')}</div>`)
				+ `<div class="sub">${t('lists')}</div>${lists}</div>`;
			if (!this._sites || Date.now() - this._sites.at > 60000) this._loadSites();
		}
		let weekPart = '';
		if (!compact && c.show_week !== false && c.schedule) {
			// same look as Family Link: 3h / 1h30, and the allowed window as 7h-20h
			const short = (m) => { m = Math.round(m); const h = Math.floor(m / 60), r = m % 60; return h ? `${h}h${r ? String(r).padStart(2, '0') : ''}` : `${r}min`; };
			const hour = (m) => `${Math.floor(m / 60) % 24}h${m % 60 ? String(m % 60).padStart(2, '0') : ''}`;
			const dayTile = (d, xbox) => {
				const [n, s, e] = this._dayIds(d, xbox);
				const v = this._num(n);
				const win = !xbox && this._state(s) && this._state(e) ? `${hour(toMin(this._state(s).state, 0))}-${hour(toMin(this._state(e).state, 0))}` : '';
				return `<button class="day${d === today ? ' on' : ''}" data-day="${d}" data-xbox="${xbox ? 1 : 0}"><small>${esc(t('dnames')[d].slice(0, 3))}</small><b>${isNaN(v) ? '–' : short(v)}</b><i>${win}</i></button>`;
			};
			weekPart = `<div class="label">${t('week_label')}</div>`
				+ (ch.xbox_lock ? `<div class="wsub">${t('windows')}</div>` : '') + `<div class="week">${DAYS.map((d) => dayTile(d, false)).join('')}</div>`
				+ (ch.xbox_lock ? `<div class="wsub">${t('xbox')}</div><div class="week">${DAYS.map((d) => dayTile(d, true)).join('')}</div>` : '');
		}
		const tab = (key, ico, label, word) => `<button class="tab${this._tab === key ? ' on' : ''}" data-tab="${key}" data-tip="${esc((label + (word ? ' ' + word : '')).trim())}"><ha-icon icon="${ico}"></ha-icon><span>${esc(label)}${word ? `<span class="w"> ${esc(word)}</span>` : ''}</span></button>`;
		const missing = this._watched().filter((id) => !this._hass.states[id]);
		this.shadowRoot.innerHTML = `
			<style>${FamilySafetyCard.css}</style>
			<ha-card>
				<div class="head">
					<ha-icon icon="${esc(c.icon || 'mdi:laptop')}" style="color:var(${accLocked ? '--error-color' : '--success-color'})"></ha-icon>
					<div class="tt"><span class="title">${esc(c.title || '')}</span>${status ? `<span class="status">${status}</span>` : ''}</div>
					<span class="used">${isNaN(used) ? '' : hm(used)}${limit > 0 ? ' / ' + hm(limit) : ''}</span>
					<button class="gear" data-settings aria-label="${esc(t('settings'))}" data-tip="${esc(t('settings'))}"><ha-icon icon="mdi:cog-outline"></ha-icon></button>
				</div>
				<div class="track big"><div style="width:${pct}%"></div></div>
				${missing.length ? `<div class="warn">${t('missing')}${esc(missing.join(', '))}</div>` : ''}
				${tiles && c.show_tiles !== false ? `<div class="tiles">${tiles}</div>` : ''}
				${compact || c.show_request === false ? '' : reqLine}
				${c.show_apps === false ? '' : `<div class="label">${t('apps_label')}</div>
				<div class="tabs">
					${tab('today', 'mdi:clock-outline', '', t('today'))}
					${tab('installed', 'mdi:apps', String(apps.length), t('installed'))}
					${tab('limited', 'mdi:timer-lock-outline', String(apps.filter((a) => rule(a) === 'limit').length), t('limited'))}
					${tab('blocked', 'mdi:cancel', String(apps.filter((a) => rule(a) === 'block' || rule(a) === 'pause').length), t('blocked_tab'))}
				</div>
				<div class="list">${list}</div>`}
				${sitesPart}
				${weekPart}
			</ha-card>`;
		const $ = (sel, fn) => this.shadowRoot.querySelectorAll(sel).forEach((el) => el.addEventListener('click', (ev) => fn(el, ev)));
		$('[data-tab]', (b) => { this._tab = b.dataset.tab; this._render(); });
		$('[data-app]', (r) => { const a = apps.find((x) => x.app_id === r.dataset.app); if (a) this._open(appSheet(this, a, paused.has(a.app_id))); });
		$('[data-settings]', () => this._open(settingsSheet(this)));
		$('[data-site]', (r) => this._open(siteSheet(this, r.dataset.site)));
		$('[data-add-site]', (b, ev) => { ev.stopPropagation(); this._open(siteSheet(this, '')); });
		$('[data-tile]', (b) => this._open(b.dataset.tile === 'web' ? webSheet(this) : lockSheet(this, b.dataset.tile)));
		$('[data-req]', () => { if (req) this._open(requestSheet(this, req)); });
		$('[data-day]', (b) => this._open(daySheet(this, b.dataset.day, b.dataset.xbox === '1')));
	}

	/** The allowed and blocked lists: get_sites when read, else the web filter sensor's exceptions. */
	_lists() {
		if (this._sites && !this._sites.error) {
			return [...this._sites.allowed.map((u) => ({ domain: domain(u), allowed: true })), ...this._sites.blocked.map((u) => ({ domain: domain(u), allowed: false }))];
		}
		return this._attr(this._config.child.web_filter, 'exceptions', []).map((e) => ({ domain: domain(e.website), allowed: !!e.allowed }));
	}

	/** get_sites answers with Microsoft's own lists (the sensor's copy can be empty). */
	async _loadSites() {
		if (this._loadingSites) return;
		this._loadingSites = true;
		try {
			const res = await this._hass.callWS({ type: 'call_service', domain: DOMAIN, service: 'get_sites',
				service_data: { entity_id: this._config.child.screen_time }, return_response: true });
			const r = (res && res.response) || {};
			this._sites = { at: Date.now(), blocked: r.blocked || [], allowed: r.allowed || [] };
		} catch (err) {
			this._sites = { at: Date.now(), error: (err && err.message) || String(err) };
		}
		this._loadingSites = false;
		this._render();
	}

	/** Call a per-child service on the child's entity; the integration resolves the account. */
	_call(service, data) {
		return this._hass.callService(DOMAIN, service, { entity_id: this._config.child.screen_time, ...data });
	}

	_open(sheet) {
		if (this._dialog) this._dialog.remove();
		const dialog = document.createElement('familysafety-dialog');
		dialog.hass = this._hass;
		dialog.open({ ...sheet, t: (k) => this._t(k), done: () => { this._dialog = null; } });
		document.body.appendChild(dialog);
		this._dialog = dialog;
	}
}

FamilySafetyCard.css = `
	:host { display: block; container-type: inline-size; }
	ha-card { overflow: hidden; }
	button { font: inherit; color: inherit; background: none; border: 0; cursor: pointer; padding: 0; }
	.head { display: flex; align-items: center; gap: 10px; padding: 14px 16px 10px; }
	.tt { display: flex; flex-direction: column; min-width: 0; line-height: 1.3; }
	.head ha-icon { --mdc-icon-size: 28px; }
	.title { font-weight: 700; font-size: 15px; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.used { margin-left: auto; font-weight: 700; font-size: 14px; white-space: nowrap; flex: none; }
	.gear { width: 32px; height: 32px; margin: -6px -6px -6px 0; border-radius: 16px; display: inline-flex; align-items: center; justify-content: center; opacity: 0.6; }
	.gear ha-icon { --mdc-icon-size: 20px; }
	.status { font-size: 13px; color: var(--secondary-text-color); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	em.warn2 { color: var(--warning-color, #ff9800); }
	.track { height: 6px; border-radius: 3px; background: var(--divider-color); grid-column: 1 / 3; }
	.track > div { height: 100%; border-radius: 3px; background: var(--primary-color); }
	.track.big { height: 8px; margin: 0 16px 12px; }
	.track.big > div { background: var(--accent-color); }
	.warn { margin: 0 16px 8px; font-size: 12px; color: var(--error-color); }
	.tiles { display: flex; border-top: 1px solid var(--divider-color); border-bottom: 1px solid var(--divider-color); }
	.tile { flex: 1; display: flex; flex-direction: column; align-items: center; gap: 4px; padding: 10px 4px; font-size: 13px; font-weight: 500;
		border-right: 1px solid var(--divider-color); }
	.tile:last-child { border-right: 0; }
	.tile ha-icon { --mdc-icon-size: 26px; opacity: 0.5; }
	.tile.on ha-icon { color: var(--on); opacity: 1; }
	.req { display: flex; align-items: center; gap: 10px; width: 100%; box-sizing: border-box; padding: 10px 16px; font-size: 14px; font-weight: 500; text-align: left;
		border-bottom: 1px solid var(--divider-color); background: rgba(255, 152, 0, 0.08); }
	.req span { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.req ha-icon:first-child { color: var(--accent-color); }
	.label { display: flex; align-items: center; gap: 8px; padding: 14px 16px 6px; font-size: 11px; font-weight: 600; letter-spacing: 1.2px; text-transform: uppercase; opacity: 0.55; }
	.label::before { content: ''; width: 3px; height: 12px; border-radius: 2px; background: var(--accent-color); }
	.label .add { margin-left: auto; opacity: 1; }
	.tabs { display: flex; border-top: 1px solid var(--divider-color); border-bottom: 1px solid var(--divider-color); }
	.tab { flex: 1; min-width: 0; display: inline-flex; align-items: center; justify-content: center; gap: 5px; padding: 12px 2px; font-size: 13px;
		font-weight: 600; opacity: 0.6; border-bottom: 3px solid transparent; white-space: nowrap; }
	.tab span { overflow: hidden; text-overflow: ellipsis; }
	.tab ha-icon { --mdc-icon-size: 18px; flex: none; }
	.tab.on { opacity: 1; border-bottom-color: var(--accent-color); }
	.tab.on ha-icon { color: var(--accent-color); }
	.list { padding: 6px 16px 14px; }
	.cols { columns: 2; column-gap: 12px; }
	.row { display: grid; grid-template-columns: 1fr auto; gap: 4px 10px; padding: 6px 0; cursor: pointer; break-inside: avoid; font-size: 14px; line-height: 1.45; }
	.name { display: flex; align-items: center; gap: 6px; min-width: 0; }
	.name ha-icon { --mdc-icon-size: 13px; opacity: 0.7; flex: none; }
	.cols .row { padding: 4px 0; font-size: 13px; }
	.name span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	em { font-style: normal; font-size: 11px; flex: none; }
	em.bad { color: var(--error-color); }
	em.ok { color: var(--success-color); }
	.right { opacity: 0.7; white-space: nowrap; }
	.sub { display: flex; align-items: center; justify-content: space-between; margin: 8px 0 4px; font-size: 12px; opacity: 0.6; }
	.add { width: 32px; height: 32px; border-radius: 16px; display: inline-flex; align-items: center; justify-content: center;
		background: rgba(var(--rgb-primary-text-color, 255, 255, 255), 0.1); opacity: 1; }
	.add ha-icon { --mdc-icon-size: 20px; }
	/* weekly limits: same grid as familylink-card */
	.week { display: flex; padding: 0 8px 10px; }
	.wsub { padding: 0 16px 2px; font-size: 11px; opacity: 0.55; }
	.day { flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: center; padding: 6px 0; border-radius: 8px; }
	.day small { font-size: 11px; opacity: 0.7; }
	.day b { font-size: 14px; }
	.day i { font-style: normal; font-size: 9px; opacity: 0.5; white-space: nowrap; min-height: 1em; }
	.day.on { background: rgba(255, 225, 150, 0.11); }
	.day.on small { color: var(--accent-color); opacity: 1; font-weight: 700; }
	.empty { padding: 6px 0; font-size: 14px; opacity: 0.6; text-align: center; }
	.empty.small { font-size: 11px; text-align: left; }
	[data-tip] { position: relative; }
	[data-tip]:hover::after { content: attr(data-tip); position: absolute; bottom: calc(100% + 8px); right: 0; z-index: 2; white-space: nowrap; pointer-events: none;
		padding: 4px 8px; border-radius: 4px; font-size: 12px; font-weight: 400; letter-spacing: 0; text-transform: none; color: #fff; background: rgba(40, 40, 40, 0.92); }
	.tab[data-tip]:hover::after { right: auto; left: 50%; transform: translateX(-50%); }
	/* narrow card (half width, phone, card picker preview): icons and counts only, tighter rows */
	@container (max-width: 420px) {
		.tab .w { display: none; } .tab { gap: 4px; }
		.head { gap: 8px; } .title { font-size: 14px; } .used { font-size: 13px; }
		.tile { font-size: 12px; padding: 8px 2px; } .day b { font-size: 13px; } .day i { font-size: 8px; }
	}
`;

/* ------------------------------------------------------------------ sheets
 * A sheet = { icon, title, name, sub, draft, rows: (draft, t) => [...], buttons: [...] }.
 * Row kinds: choice (key, value), stepper (key, step, min, max, fmt), toggle (key), text (key).
 * A button runs async (draft) => void; it throws to keep the sheet open with the message.
 */
function appSheet(card, a, isPaused) {
	const t = (k) => card._t(k);
	const used = a.usage_minutes ? ' · ' + t('used_today').replace('{t}', hm(a.usage_minutes)) : '';
	const head = { icon: 'mdi:application-cog', title: t('app'), chip: 'mdi:application-outline', name: a.app_name };
	const save = (run) => [{ label: t('save'), icon: 'mdi:content-save-outline', run }];
	const pauseOpt = { kind: 'choice', key: 'mode', value: 'pause', icon: 'mdi:pause-circle-outline', title: t('pause'), sub: t('pause_sub') };
	const xbox = String(a.app_id).startsWith('x:') && !String(a.app_id).startsWith('x:windowsplt');
	if (isPaused) {
		// a paused app is resumed (pause_app saved its limit) rather than unblocked, which would wipe the limit
		return { ...head, sub: t('cur_paused') + used, draft: { mode: 'resume' },
			rows: () => [
				{ kind: 'choice', key: 'mode', value: 'resume', icon: 'mdi:play-circle-outline', title: t('resume'), sub: t('resume_sub') },
				{ kind: 'choice', key: 'mode', value: 'keep', icon: 'mdi:pause-circle-outline', title: t('keep_paused'), sub: t('keep_paused_sub') },
			],
			buttons: save(async (d) => { if (d.mode === 'resume') await card._call('resume_app', { apps: [a.app_id] }); }) };
	}
	// limit_minutes / limit_start / limit_end: the app's own limit, when the integration exposes it
	const lim = a.limit_minutes > 0 ? a.limit_minutes : 0;
	return {
		...head, sub: (a.blocked ? t('cur_blocked') : lim ? t('cur_limited').replace('{t}', hm(lim)) : t('cur_allowed')) + used,
		draft: { mode: a.blocked ? 'block' : lim ? 'limit' : 'none', minutes: lim || 60,
			from: toMin(a.limit_start, 7 * 60), until: a.limit_end === '00:00' ? 1440 : toMin(a.limit_end, 22 * 60) },
		rows: (d) => [
			{ kind: 'choice', key: 'mode', value: 'none', icon: 'mdi:check-circle-outline', title: t('none'), sub: t('none_sub') },
			{ kind: 'choice', key: 'mode', value: 'limit', icon: 'mdi:timer-outline', title: t('limit'), sub: t('limit_sub'),
				stepper: { key: 'minutes', step: 15, min: 15, max: 1440, fmt: hm } },
			...(d.mode === 'limit' ? [
				{ kind: 'stepper', key: 'from', step: 30, min: 0, max: 1410, fmt: hhmm, icon: 'mdi:clock-start', title: t('app_from'), sub: t('app_hours_sub') },
				{ kind: 'stepper', key: 'until', step: 30, min: 30, max: 1440, fmt: hhmm, icon: 'mdi:clock-end', title: t('app_until'), sub: '' },
			] : []),
			...(a.blocked ? [] : [pauseOpt]),
			{ kind: 'choice', key: 'mode', value: 'block', icon: 'mdi:cancel', title: t('block'), sub: t('block_sub') },
		],
		buttons: save(async (d) => {
			const app = { app_id: a.app_id };
			if (d.mode === 'pause') { await card._call('pause_app', { apps: [a.app_id] }); return; }
			if (d.mode === 'block') { if (!a.blocked) await card._call('block_app', app); return; }
			// a blocked app is unblocked first: Microsoft takes no limit on it
			if (a.blocked) { await card._call('unblock_app', app); await wait(1000); }
			const named = { ...app, app_name: a.app_name, platform: xbox ? 'xbox' : 'windows' };
			const time = (m) => `${String(Math.floor(m / 60) % 24).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}:00`;
			if (d.mode === 'limit') {
				await card._call('set_app_time_limit', { ...named, hours: Math.floor(d.minutes / 60), minutes: d.minutes % 60,
					start_time: time(d.from), end_time: d.until >= 1440 ? '23:59:59' : time(d.until) });
			} else if (!a.blocked) await card._call('remove_app_time_limit', named);
		}),
	};
}

function siteSheet(card, site) {
	const t = (k) => card._t(k), ch = card._config.child;
	const ex = card._lists().find((e) => e.domain === site);
	const visit = card._attr(ch.websites, 'sites', []).find((s) => s.domain === site);
	const blocked = ex ? !ex.allowed : !!(visit && visit.blocked);
	return {
		icon: 'mdi:web', title: site ? t('site') : t('add_site'), chip: 'mdi:web', name: site || t('add_site'),
		sub: site ? (visit ? `${visit.visits} ${t('visits')}` : '') + (ex ? (visit ? ' · ' : '') + (ex.allowed ? t('allowed') : t('blocked')) : '') : t('several'),
		draft: { site, mode: site ? (blocked ? 'block' : 'allow') : 'block' },
		rows: () => [
			...(site ? [] : [{ kind: 'text', key: 'site', placeholder: 'example.com' }]),
			{ kind: 'choice', key: 'mode', value: 'allow', icon: 'mdi:check-circle-outline', title: t('allow_site'), sub: t('allow_site_sub') },
			{ kind: 'choice', key: 'mode', value: 'block', icon: 'mdi:cancel', title: t('block_site'), sub: t('block_site_sub') },
			...(site ? [{ kind: 'choice', key: 'mode', value: 'remove', icon: 'mdi:playlist-remove', title: t('remove_site'), sub: t('remove_site_sub') }] : []),
		],
		buttons: [{ label: t('save'), icon: 'mdi:content-save-outline', run: async (d) => {
			// one site, or several separated by spaces, commas or new lines: the services take a list
			const sites = String(d.site || '').split(/[\s,;]+/).map(domain).filter(Boolean);
			const bad = sites.filter((w) => !/^[^\s/]+\.[^\s/]+$/.test(w));
			if (!sites.length || bad.length) throw new Error((bad.join(', ') || '…') + ' → example.com');
			try {
				await card._call({ allow: 'allow_website', block: 'block_website', remove: 'remove_website' }[d.mode], { website: sites.length === 1 ? sites[0] : sites });
			} finally {
				card._sites = null; card._loadSites();   // some sites may be written even when the call fails
			}
		} }],
	};
}

function webSheet(card) {
	const t = (k) => card._t(k), w = card._state(card._config.child.web_filter);
	const on = !!w && w.state === 'enabled';
	const age = Math.min(21, Math.max(3, parseInt(w && w.attributes.max_age_rating, 10) || 21));
	return {
		icon: 'mdi:shield-check-outline', title: t('web_title'), chip: 'mdi:shield-check-outline', name: t('web_title'), sub: '',
		// ask to buy has no readable state: null until the parent picks a value, and only then sent
		draft: { filter: on, age, buy: null },
		rows: (d) => [
			{ kind: 'toggle', key: 'filter', icon: 'mdi:web-check', title: t('filter'), sub: t('filter_sub') },
			{ kind: 'stepper', key: 'age', step: 1, min: 3, max: 21, fmt: (v) => (v >= 21 ? t('any_age') : v + '+'), icon: 'mdi:star-circle-outline', title: t('age'), sub: t('age_sub') },
			{ kind: 'toggle', key: 'buy', icon: 'mdi:cart-outline', title: t('buy'), sub: d.buy === null ? t('buy_unknown') : t('buy_sub') },
		],
		buttons: [{ label: t('save'), icon: 'mdi:content-save-outline', run: async (d) => {
			if (d.filter !== on) await card._call('toggle_web_filter', { enabled: d.filter });
			if (d.age !== age) await card._call('set_age_rating', { age: d.age });
			if (d.buy !== null) await card._call('set_acquisition_policy', { require_approval: d.buy });
		} }],
	};
}

function lockSheet(card, target) {
	const t = (k) => card._t(k), ch = card._config.child;
	const choices = [
		ch.lock && { kind: 'choice', key: 'target', value: 'Account', icon: 'mdi:account-lock', title: t('lock_account'), sub: t('lock_account_sub') },
		ch.windows_lock && { kind: 'choice', key: 'target', value: 'Windows', icon: 'mdi:microsoft-windows', title: t('windows'), sub: t('lock_windows_sub') },
		ch.xbox_lock && { kind: 'choice', key: 'target', value: 'Xbox', icon: 'mdi:microsoft-xbox', title: t('xbox'), sub: t('lock_xbox_sub') },
	].filter(Boolean);
	const hrs = (v) => (v % 24 ? `${v} ${t('h')}` : `${v / 24} ${v === 24 ? t('d') : t('ds')}`);
	const go = (lock) => async (d) => {
		if (d.target === 'Account') await card._call(lock ? 'lock_account' : 'unlock_account', {});
		else if (lock) await card._call('lock_platform', { platform: d.target, duration_hours: d.hours });
		else await card._call('unlock_platform', { platform: d.target });
	};
	return {
		icon: 'mdi:lock-clock', title: t('lock_title'), chip: 'mdi:lock-clock', name: t('lock_title'), sub: card._config.title || '',
		draft: { target, hours: 24, bonus: 30 },
		rows: (d) => [
			...choices,
			{ kind: 'stepper', key: 'hours', step: 1, min: 1, max: 168, fmt: hrs, icon: 'mdi:timer-sand', title: t('duration'), sub: t('duration_sub') },
			// extra time exists on the Xbox only (Xbox Family Settings service)
			...(d.target === 'Xbox' ? [
				{ kind: 'stepper', key: 'bonus', step: 15, min: 15, max: 240, fmt: hm, icon: 'mdi:timer-plus-outline', title: t('bonus'), sub: t('bonus_sub') },
				{ kind: 'action', icon: 'mdi:plus-circle-outline', title: t('add_bonus'), sub: '', close: true,
					run: (dd) => card._call('add_time_bonus', { platform: 'Xbox', minutes: dd.bonus }) },
				{ kind: 'action', icon: 'mdi:close-circle-outline', title: t('cancel_bonus'), sub: t('cancel_bonus_sub'), close: true,
					run: () => card._call('cancel_time_bonus', { platform: 'Xbox' }) },
			] : []),
		],
		buttons: [
			{ label: t('lock'), icon: 'mdi:lock', color: 'var(--error-color, #f44336)', run: go(true) },
			{ label: t('unlock'), icon: 'mdi:lock-open-variant', color: 'var(--success-color, #4caf50)', run: go(false) },
		],
	};
}

function settingsSheet(card) {
	const t = (k) => card._t(k), ch = card._config.child;
	const sw = (id) => { const s = card._state(id); return s && s.state !== 'unavailable' ? s.state === 'on' : null; };
	const toggles = [
		['limits', 'mdi:clock-check-outline', t('limits_sw'), t('limits_sw_sub')],
		['xbox_limits', 'mdi:microsoft-xbox', t('xbox_limits_sw'), t('xbox_limits_sw_sub')],
		['app_limits', 'mdi:apps', t('app_limits_sw'), t('app_limits_sw_sub')],
	].filter(([key]) => sw(ch[key]) !== null);
	const draft = Object.fromEntries(toggles.map(([key]) => [key, sw(ch[key])]));
	// the connection sensor says what, if anything, the parent has to do
	const conn = card._state(card._config.connection || 'sensor.microsoft_family_safety_connection');
	const a = (conn && conn.attributes) || {};
	const connText = !conn ? '' : a.account_interrupt === 'terms_of_use' ? t('conn_terms') : a.account_interrupt === 'proof_confirm' ? t('conn_proof')
		: a.reauth_required || a.reauth_requested ? t('conn_reauth') : conn.state === 'connected' ? t('conn_ok') : t('conn_degraded').replace('{r}', conn.state);
	return {
		icon: 'mdi:cog-outline', title: t('settings'), chip: 'mdi:account-child-circle', name: card._config.title || t('settings'), sub: '',
		draft,
		rows: () => [
			...toggles.map(([key, icon, title, sub]) => ({ kind: 'toggle', key, icon, title, sub })),
			...(ch.web_filter ? [{ kind: 'action', nav: true, icon: 'mdi:shield-check-outline', title: t('web_row'), sub: t('web_row_sub'), run: () => card._open(webSheet(card)) }] : []),
			{ kind: 'info', icon: conn && conn.state === 'connected' && !a.reauth_required ? 'mdi:lan-connect' : 'mdi:lan-disconnect', title: t('conn'), sub: connText },
			{ kind: 'action', icon: 'mdi:login', title: t('reauth'), sub: t('reauth_sub'), done: t('reauth_done'),
				run: () => card._hass.callService(DOMAIN, 'request_reauth', {}) },
			{ kind: 'action', icon: 'mdi:open-in-app', title: t('configure'), sub: t('configure_sub'), close: true,
				run: async () => { history.pushState(null, '', '/config/integrations/integration/' + DOMAIN); window.dispatchEvent(new CustomEvent('location-changed')); } },
		],
		buttons: [{ label: t('save'), icon: 'mdi:content-save-outline', run: async (d) => {
			for (const [key] of toggles) {
				if (d[key] !== draft[key]) await card._hass.callService('switch', d[key] ? 'turn_on' : 'turn_off', { entity_id: ch[key] });
			}
		} }],
	};
}

function requestSheet(card, r) {
	const t = (k) => card._t(k);
	// approve/deny take the request id: the integration looks the request up in its own roster
	const call = (service, data) => card._hass.callService(DOMAIN, service, { request_id: String(r.id), ...data });
	return {
		icon: 'mdi:hand-back-right', title: t('request'), chip: 'mdi:hand-back-right',
		name: r.type === 'DeviceScreenTime' ? t('more_time').replace('{p}', r.platform || '?') : (r.appName || r.title || t('request')),
		sub: r.message ? `“${r.message}”` : '',
		draft: { minutes: 60 },
		rows: () => [{ kind: 'stepper', key: 'minutes', step: 15, min: 15, max: 480, fmt: hm, icon: 'mdi:timer-plus-outline', title: t('extra'), sub: t('extra_sub') }],
		buttons: [
			{ label: t('approve'), icon: 'mdi:check-circle', color: 'var(--success-color, #4caf50)', run: (d) => call('approve_request', { extension_minutes: d.minutes }) },
			{ label: t('deny'), icon: 'mdi:close-circle', color: 'var(--error-color, #f44336)', run: () => call('deny_request', {}) },
		],
	};
}

function daySheet(card, day, xbox) {
	const t = (k) => card._t(k);
	const [n, s, e] = card._dayIds(day, xbox);
	const limit = Math.max(0, Math.round(card._num(n)) || 0);
	const from = xbox ? 7 * 60 : toMin(card._state(s)?.state, 7 * 60), until = xbox ? 21 * 60 : toMin(card._state(e)?.state, 21 * 60);
	const r30 = (m) => Math.round(m / 30) * 30;
	return {
		icon: xbox ? 'mdi:microsoft-xbox' : 'mdi:laptop', title: (xbox ? t('xbox') : t('windows')) + ' · ' + t('day'), chip: xbox ? 'mdi:microsoft-xbox' : 'mdi:laptop',
		name: t('dnames')[day], sub: `${t('daily')}: ${hm(limit)}`,
		draft: { limit, from: r30(from), until: r30(until) },
		rows: () => [
			{ kind: 'stepper', key: 'limit', step: 15, min: 0, max: 1440, fmt: hm, icon: 'mdi:timer-sand', title: t('daily'), sub: t('daily_sub') },
			{ kind: 'stepper', key: 'from', step: 30, min: 0, max: 1410, fmt: hhmm, icon: 'mdi:clock-start', title: t('from'), sub: t('step30') },
			{ kind: 'stepper', key: 'until', step: 30, min: 30, max: 1440, fmt: hhmm, icon: 'mdi:clock-end', title: t('until'), sub: t('step30') },
		],
		buttons: [{ label: t('save'), icon: 'mdi:content-save-outline', run: async (d) => {
			const platform = xbox ? 'Xbox' : 'Windows', day_of_week = DOW[day];
			if (d.limit !== limit) await card._call('set_screentime_limit', { platform, day_of_week, hours: Math.floor(Math.min(d.limit, 1440) / 60), minutes: d.limit % 60 });
			if (d.from !== r30(from) || d.until !== r30(until)) {
				await card._call('set_screentime_intervals', { platform, day_of_week,
					start_hour: Math.floor(d.from / 60), start_minute: d.from % 60, end_hour: Math.floor(d.until / 60), end_minute: d.until % 60 });
			}
		} }],
	};
}

/** The sheet. Appended to <body>, so it carries its own (always dark) colours. */
class FamilySafetyDialog extends HTMLElement {
	open(sheet) {
		this._s = sheet;
		this._d = { ...sheet.draft };
		this._busy = -1; this._error = '';
		this.attachShadow({ mode: 'open' });
		// keys typed in the sheet must not reach Home Assistant's shortcuts (c, e, m, a...)
		this.addEventListener('keydown', (ev) => { ev.stopPropagation(); if (ev.key === 'Escape' && this._busy < 0) this._close(); });
		this._render();
	}

	connectedCallback() {
		// the text field can only take the focus once the sheet is in the page
		setTimeout(() => { const f = this.shadowRoot && this.shadowRoot.querySelector('[data-text]'); if (f) f.focus(); }, 0);
	}

	_close() { this.remove(); this._s.done(); }

	async _run(i) {
		this._busy = i; this._error = ''; this._render();
		try {
			await this._s.buttons[i].run(this._d);
			this._close();
		} catch (err) {
			this._busy = -1;
			this._error = (err && err.message) || String(err);
			this._render();
		}
	}

	/** An action row runs at once: closes on success, or shows its "done" text; stays open on error. */
	async _act(r) {
		this._busy = 1000; this._error = ''; this._notice = ''; this._render();
		try {
			await r.run(this._d);
			if (r.close) { this._close(); return; }
			this._notice = r.done || '';
		} catch (err) {
			this._error = (err && err.message) || String(err);
		}
		this._busy = -1;
		this._render();
	}

	_render() {
		const s = this._s, d = this._d, t = s.t, busy = this._busy >= 0;
		const chip = (icon) => `<div class="chip"><ha-icon icon="${icon}"></ha-icon></div>`;
		const txt = (r) => `<div class="txt"><div>${esc(r.title)}</div>${r.sub ? `<small>${esc(r.sub)}</small>` : ''}</div>`;
		const stepper = (st) => `<div class="step"><button data-step="${st.key}" data-by="${-st.step}">&minus;</button><span>${esc(st.fmt(d[st.key]))}</span><button data-step="${st.key}" data-by="${st.step}">+</button></div>`;
		const row = (r, i) => {
			if (r.kind === 'text') {
				return `<input class="field" data-text="${r.key}" value="${esc(d[r.key])}" placeholder="${esc(r.placeholder || '')}" autocomplete="off" spellcheck="false" ${busy ? 'disabled' : ''}>`;
			}
			if (r.kind === 'choice') {
				return `<div class="opt${d[r.key] === r.value ? ' on' : ''}" data-row="${i}">${chip(r.icon)}${txt(r)}${r.stepper ? stepper(r.stepper) : ''}</div>`;
			}
			if (r.kind === 'toggle') {
				return `<div class="opt${d[r.key] ? ' on' : ''}" data-row="${i}">${chip(r.icon)}${txt(r)}<div class="sw${d[r.key] === null ? ' unk' : d[r.key] ? ' on' : ''}"><i></i></div></div>`;
			}
			if (r.kind === 'info') return `<div class="opt flat">${chip(r.icon)}${txt(r)}</div>`;
			if (r.kind === 'action') return `<div class="opt act" data-row="${i}">${chip(r.icon)}${txt(r)}<ha-icon class="go" icon="mdi:chevron-right"></ha-icon></div>`;
			return `<div class="opt flat">${chip(r.icon)}${txt(r)}${stepper(r)}</div>`;
		};
		const rows = s.rows(d, t);
		const focus = this.shadowRoot.activeElement && this.shadowRoot.activeElement.dataset ? this.shadowRoot.activeElement.dataset.text : null;
		this.shadowRoot.innerHTML = `
			<style>${FamilySafetyDialog.css}</style>
			<div class="veil"></div>
			<div class="sheet" role="dialog" aria-modal="true" aria-label="${esc(s.title)}">
				<div class="bar"><ha-icon icon="${s.icon}"></ha-icon><span>${esc(s.title)}</span><button class="x" aria-label="${esc(t('close'))}"><ha-icon icon="mdi:close"></ha-icon></button></div>
				<div class="head"><div class="chip sq"><ha-icon icon="${s.chip}"></ha-icon></div><div class="txt"><div class="big">${esc(s.name)}</div>${s.sub ? `<small>${esc(s.sub)}</small>` : ''}</div></div>
				${rows.map(row).join('')}
				${this._error ? `<div class="err" role="alert">${esc(this._error)}</div>` : ''}
				${this._notice ? `<div class="note" role="status">${esc(this._notice)}</div>` : ''}
				<div class="btns">${s.buttons.map((b, i) => `<button class="save" data-btn="${i}" ${busy ? 'disabled' : ''}><ha-icon icon="${this._busy === i ? 'mdi:loading' : b.icon}" style="${b.color && this._busy !== i ? 'color:' + b.color : ''}"></ha-icon>${esc(this._busy === i ? t('saving') : b.label)}</button>`).join('')}</div>
			</div>`;
		const root = this.shadowRoot, $ = (sel, fn) => root.querySelectorAll(sel).forEach((el) => el.addEventListener('click', (ev) => fn(el, ev)));
		$('.veil', () => busy || this._close());
		$('.x', () => busy || this._close());
		$('[data-btn]', (b) => busy || this._run(Number(b.dataset.btn)));
		$('[data-row]', (o) => {
			if (busy) return;
			const r = rows[Number(o.dataset.row)];
			if (r.kind === 'action') { if (r.nav) r.run(d); else this._act(r); return; }
			if (r.kind === 'choice') d[r.key] = r.value; else d[r.key] = !d[r.key];
			this._render();
		});
		$('[data-step]', (b, ev) => {
			ev.stopPropagation();
			if (busy) return;
			const r = rows.find((x) => (x.stepper ? x.stepper.key : x.key) === b.dataset.step);
			const st = r.stepper || r;
			d[st.key] = Math.min(st.max, Math.max(st.min, d[st.key] + Number(b.dataset.by)));
			if (r.stepper) d[r.key] = r.value;
			this._render();
		});
		root.querySelectorAll('[data-text]').forEach((el) => {
			el.addEventListener('input', () => { d[el.dataset.text] = el.value; });
			el.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') { ev.preventDefault(); if (!busy) this._run(0); } });
		});
		// a re-render (choice tapped, error shown) keeps the caret in the field being typed in
		const f = focus && root.querySelector(`[data-text="${focus}"]`);
		if (f) { f.focus(); f.setSelectionRange(f.value.length, f.value.length); }
	}
}

FamilySafetyDialog.css = `
	:host { position: fixed; inset: 0; z-index: 1000; display: flex; align-items: center; justify-content: center; font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif); }
	.veil { position: absolute; inset: 0; background: rgba(0, 0, 0, 0.55); }
	.sheet { position: relative; width: min(440px, calc(100vw - 24px)); max-height: calc(100vh - 24px); overflow: auto; box-sizing: border-box; padding: 16px;
		border-radius: 28px; background: #1c1c1e; color: #ebebf0; }
	.bar { display: flex; align-items: center; gap: 10px; font-weight: 700; font-size: 14px; margin-bottom: 14px; }
	.bar span { flex: 1; }
	button { font: inherit; color: inherit; border: 0; cursor: pointer; background: none; }
	.x { width: 44px; height: 44px; border-radius: 22px; background: rgba(255, 255, 255, 0.08); display: inline-flex; align-items: center; justify-content: center; }
	.head, .opt { display: flex; align-items: center; gap: 14px; }
	.head { margin-bottom: 14px; }
	.opt { padding: 12px 14px; margin-bottom: 8px; border-radius: 18px; background: rgba(255, 255, 255, 0.06); border: 2px solid transparent; cursor: pointer; min-height: 48px; }
	.opt.flat { cursor: default; }
	.opt.on { border-color: #ff9800; }
	.chip { flex: none; width: 48px; height: 48px; border-radius: 24px; display: flex; align-items: center; justify-content: center; background: rgba(255, 225, 150, 0.11); }
	.chip.sq { width: 52px; height: 52px; border-radius: 14px; }
	.opt.on .chip { background: #ff9800; color: #1c1c1e; }
	.txt { flex: 1; min-width: 0; font-size: 17px; font-weight: 700; line-height: 1.2; }
	.txt .big { font-size: 20px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	small { display: block; margin-top: 3px; font-size: 13px; font-weight: 400; color: #a5a5ac; }
	.step { flex: none; display: flex; align-items: center; height: 44px; border-radius: 22px; background: rgba(255, 255, 255, 0.1); }
	.step button { width: 44px; height: 44px; font-size: 20px; }
	.step span { min-width: 58px; text-align: center; font-weight: 700; font-size: 15px; white-space: nowrap; }
	.sw { flex: none; width: 52px; height: 30px; border-radius: 15px; position: relative; background: rgba(255, 255, 255, 0.2); }
	.sw i { position: absolute; top: 3px; left: 3px; width: 24px; height: 24px; border-radius: 12px; background: #fff; transition: left 0.15s; }
	.sw.on { background: #ff9800; }
	.sw.on i { left: 25px; }
	.sw.unk i { left: 14px; opacity: 0.6; }
	.field { width: 100%; box-sizing: border-box; height: 52px; margin-bottom: 10px; padding: 0 16px; border-radius: 16px; font: inherit; font-size: 16px;
		color: #ebebf0; background: rgba(255, 255, 255, 0.06); border: 2px solid rgba(255, 255, 255, 0.15); outline: none; }
	.field:focus { border-color: #ff9800; }
	.err { margin: 4px 2px 10px; font-size: 13px; color: #ff6b6b; }
	.note { margin: 4px 2px 10px; font-size: 13px; color: #8fd18f; }
	.opt.act .go { flex: none; opacity: 0.5; }
	.btns { display: flex; gap: 8px; margin-top: 6px; }
	.save { flex: 1; height: 52px; border-radius: 26px; background: rgba(255, 255, 255, 0.1); font-weight: 600; font-size: 16px;
		display: flex; align-items: center; justify-content: center; gap: 8px; }
	.save[disabled] { opacity: 0.6; cursor: default; }
	.save[disabled] ha-icon[icon="mdi:loading"] { animation: spin 1s linear infinite; }
	@keyframes spin { to { transform: rotate(360deg); } }
`;

const SECTIONS = ['show_tiles', 'show_request', 'show_apps', 'show_websites', 'show_week'];

/** Visual editor: the child (its Family Safety device), a title, the sections shown, the language. */
class FamilySafetyCardEditor extends HTMLElement {
	setConfig(config) { this._config = config; this._render(); }
	set hass(hass) { this._hass = hass; this._render(); }

	_render() {
		if (!this._hass || !this._config) return;
		const fr = (this._hass.language || '').startsWith('fr');
		if (!this._form) {
			this._form = document.createElement('ha-form');
			this._form.computeLabel = (s) => ({
				device: fr ? 'Enfant (son appareil Family Safety)' : 'Child (its Family Safety device)', title: fr ? 'Titre' : 'Title',
				language: fr ? 'Langue' : 'Language',
				show_tiles: fr ? 'Tuiles (verrous, web)' : 'Tiles (locks, web)', show_request: fr ? 'Demande en attente' : 'Pending request',
				show_apps: fr ? 'Applications' : 'Applications', show_websites: fr ? 'Sites web' : 'Websites', show_week: fr ? 'Limites de la semaine' : 'Weekly limits',
			}[s.name]);
			this._form.addEventListener('value-changed', (e) => {
				const config = { ...e.detail.value };
				delete config.compact;  // replaced by the show_* switches
				Object.keys(config).forEach((k) => (config[k] === '' || config[k] === undefined || (SECTIONS.includes(k) && config[k] === true)) && delete config[k]);
				this._config = config;
				this.dispatchEvent(new CustomEvent('config-changed', { detail: { config }, bubbles: true, composed: true }));
			});
			this.appendChild(this._form);
		}
		this._form.hass = this._hass;
		this._form.data = { ...Object.fromEntries(SECTIONS.map((k) => [k, true])), ...this._config };
		this._form.schema = [
			// the child's own device carries the locks and limits (switches); a PC device only carries its time
			{ name: 'device', required: true, selector: { device: { filter: { integration: DOMAIN }, entity: { domain: 'switch' } } } },
			{ name: 'title', selector: { text: {} } },
			// sections shown unless set to false (absent = shown)
			...SECTIONS.map((name) => ({ name, selector: { boolean: {} } })),
			{ name: 'language', selector: { select: { mode: 'dropdown', options: [{ value: 'en', label: 'English' }, { value: 'fr', label: 'Français' }] } } },
		];
	}
}

// The integration will load this file as a frontend module; during development it is also a
// dashboard resource, so the elements must not be defined twice.
if (!customElements.get('familysafety-card')) {
	customElements.define('familysafety-card-editor', FamilySafetyCardEditor);
	customElements.define('familysafety-dialog', FamilySafetyDialog);
	customElements.define('familysafety-card', FamilySafetyCard);
	window.customCards = window.customCards || [];
	window.customCards.push({ type: 'familysafety-card', name: 'Microsoft Family Safety', preview: true, description: 'A child: screen time, locks, apps, websites and weekly limits.' });
}
