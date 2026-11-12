/*
 * Screen time cards, shared by the Google Family Link and Microsoft Family Safety integrations
 * (the same file ships with both; whichever loads first defines the elements).
 *
 *   type: custom:parental-screen-time-card        # today: total, per source, limit, locks, hourly chart
 *   type: custom:parental-screen-time-history-card  # the last days, one bar per day
 *   devices: [<device id>, ...]      # phones, tablets, PCs of Family Link or Family Safety ({ device, name } to rename one)
 *   entities: [sensor.switch_used_screen_time]      # any other time sensor in minutes
 *   sources:                         # optional, same with a name and a limit entity
 *     - { entity: sensor.console_time, name: Console, limit: number.console_limit }
 *   title / language (en, fr) / chart: false (summary card) / days: 14 (history card)
 *
 * A device is resolved by the websocket command "<integration>/card_entities" of the integration
 * that owns it. Contract (each integration answers the same way):
 *   { device: { daily_screen_time, daily_limit?, screen_time_remaining?, active_bonus?, lock?, next_restriction? },
 *     child: { daily_limit_1 .. daily_limit_7? },  locked_when?: 'on' | 'off' (default 'off') }
 * Charts read Home Assistant's long-term statistics of the time sensors and add them up:
 * no helper, no template sensor, no chart card needed.
 */
(() => {
const STR = {
	en: {
		today: 'Screen time today', history: 'Last {n} days', left: 'left', no_limit: ' · no limit', locked: 'Locked', unlocked: 'Unlocked',
		loading: 'Loading…', none: 'Choose at least one device or time sensor.', missing: 'Not found: ',
		f_devices: 'Devices (Family Link, Family Safety)', f_entities: 'Other time sensors (minutes)', f_title: 'Title', f_language: 'Language',
		f_chart: 'Hourly chart', f_days: 'Days',
	},
	fr: {
		today: "Temps d'écran aujourd'hui", history: '{n} derniers jours', left: 'reste', no_limit: ' · sans limite', locked: 'Verrouillé', unlocked: 'Libre',
		loading: 'Chargement…', none: "Choisissez au moins un appareil ou un capteur de temps.", missing: 'Introuvable : ',
		f_devices: 'Appareils (Family Link, Family Safety)', f_entities: 'Autres capteurs de temps (minutes)', f_title: 'Titre', f_language: 'Langue',
		f_chart: 'Graphique par heure', f_days: 'Jours',
	},
};
const PLATFORMS = ['familylink', 'microsoft_family_safety'];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const hm = (m) => { m = Math.round(m); const h = Math.floor(m / 60); return h ? `${h} h ${String(m % 60).padStart(2, '0')}` : `${m} min`; };
const todayNum = () => ((new Date().getDay() + 6) % 7) + 1;  // 1 = Monday
const sum = (list) => list.reduce((a, b) => a + b, 0);
const ms = (t) => (typeof t === 'number' ? t : Date.parse(t));
const lang = (card) => (STR[(card._config.language || card._hass.language || 'en').slice(0, 2)] ? (card._config.language || card._hass.language).slice(0, 2) : 'en');

/** Shared by both cards: config -> list of sources { name, used, limit, remaining, lock, lockedWhen, next } (entity ids). */
class Base extends HTMLElement {
	setConfig(config) {
		this._config = config || {};
		this._sources = null;
		this._seen = '';
		if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
	}

	set hass(hass) {
		this._hass = hass;
		if (!this._sources) return this._resolve();
		const seen = this._watched().map((id) => (hass.states[id] ? hass.states[id].last_updated : 'x')).join('|') + hass.language;
		if (seen === this._seen) return;
		this._seen = seen;
		this._update();
	}

	_t(key) { return STR[lang(this)][key]; }
	_watched() { return this._sources.flatMap((s) => [s.used, s.limit, s.remaining, s.lock, s.next, s.bonus].filter(Boolean)); }
	_num(id) { const s = id && this._hass.states[id]; return s ? parseFloat(s.state) : NaN; }
	_shell(body) { this.shadowRoot.innerHTML = `<style>${CSS}</style><ha-card>${body}</ha-card>`; }

	async _resolve() {
		if (this._resolving) return;
		const c = this._config, hass = this._hass;
		const devices = c.devices || [], entities = c.entities || [], extra = c.sources || [];
		if (!devices.length && !entities.length && !extra.length) return this._shell(`<div class="msg">${this._t('none')}</div>`);
		this._shell(`<div class="msg">${this._t('loading')}</div>`);
		this._resolving = true;
		const sources = [];
		for (const item of devices) {
			const device = typeof item === 'string' ? item : item.device;
			const reg = Object.values(hass.entities || {}).find((e) => e.device_id === device && PLATFORMS.includes(e.platform));
			const dev = hass.devices && hass.devices[device];
			let found = null;
			if (reg) found = await hass.callWS({ type: `${reg.platform}/card_entities`, entity_id: reg.entity_id }).catch(() => null);
			if (!found || !found.device || !found.device.daily_screen_time) { sources.push({ name: device, missing: true }); continue; }
			const d = found.device;
			sources.push({
				name: (typeof item === 'object' && item.name) || (dev && (dev.name_by_user || dev.name || '').trim()) || d.daily_screen_time, used: d.daily_screen_time,
				limit: d.daily_limit || (found.child || {})[`daily_limit_${todayNum()}`], remaining: d.screen_time_remaining,
				bonus: d.active_bonus, lock: d.lock, lockedWhen: found.locked_when || 'off', next: d.next_restriction,
				icon: /tab|pad/i.test((dev && (dev.model || dev.name)) || '') ? 'mdi:tablet' : reg.platform === 'familylink' ? 'mdi:cellphone' : 'mdi:monitor',
			});
		}
		for (const e of entities) sources.push({ name: (hass.states[e] && hass.states[e].attributes.friendly_name) || e, used: e });
		for (const s of extra) sources.push({ name: s.name || (hass.states[s.entity] && hass.states[s.entity].attributes.friendly_name) || s.entity, used: s.entity, limit: s.limit, remaining: s.remaining, bonus: s.bonus, next: s.next, lock: s.lock, lockedWhen: s.locked_when || 'off', icon: s.icon });
		this._sources = sources;
		this._resolving = false;
		this.hass = hass;
	}

	/** Long-term statistics of every source, added up per period: [{ start, minutes }]. */
	async _stats(period, start) {
		const ids = this._sources.filter((s) => s.used && !s.missing).map((s) => s.used);
		const res = await this._hass.callWS({ type: 'recorder/statistics_during_period', start_time: start.toISOString(), statistic_ids: ids, period, types: ['max', 'state'] });
		return { ids, rows: res };
	}

	static getConfigElement() { return document.createElement('parental-screen-time-editor'); }
	// sections view: full width by default, can be narrowed to half; the height follows the content
	getGridOptions() { return { columns: 12, min_columns: 6, rows: 'auto' }; }
}

/** Today: total, per source, limit, locks, and the hourly chart. */
class ScreenTimeCard extends Base {
	static getStubConfig(hass) {
		const dev = Object.values(hass.entities || {}).filter((e) => PLATFORMS.includes(e.platform) && e.device_id && e.entity_id.startsWith('button.')).map((e) => e.device_id);
		return { devices: [...new Set(dev)] };
	}

	getCardSize() { return 7; }

	_update() {
		const t = (k) => this._t(k), S = this._sources;
		const live = S.filter((s) => !s.missing);
		const used = live.map((s) => this._num(s.used)).filter((v) => !isNaN(v));
		const total = sum(used);
		const limits = live.map((s) => this._num(s.limit) + (this._num(s.bonus) || 0));
		const allLimited = live.length && limits.every((v) => v > 0);
		const limit = allLimited ? sum(limits) : NaN;
		const left = allLimited ? sum(live.map((s, i) => { const r = this._num(s.remaining); return isNaN(r) ? Math.max(0, limits[i] - (this._num(s.used) || 0)) : r; })) : NaN;
		const pct = limit > 0 ? Math.min(100, (total / limit) * 100) : 0;
		const col = pct >= 100 ? 'var(--error-color)' : pct >= 80 ? 'var(--warning-color)' : 'var(--accent-color)';
		const next = live.map((s) => s.next && this._hass.states[s.next]).find((st) => st && !['unknown', 'unavailable'].includes(st.state));
		const tiles = live.filter((s) => s.lock && this._hass.states[s.lock]).map((s) => {
			const locked = this._hass.states[s.lock].state === s.lockedWhen;
			const c = locked ? 'var(--error-color)' : 'var(--success-color)';
			const r = this._num(s.remaining);
			const icon = locked && s.icon === 'mdi:cellphone' ? 'mdi:cellphone-lock' : s.icon || 'mdi:devices';
			return `<div class="tile"><ha-icon icon="${icon}" style="color:${c}"></ha-icon><div class="tt"><b>${esc(s.name)}</b><small style="color:${c}">${locked ? t('locked') : t('unlocked')}</small></div>${isNaN(r) ? '' : `<div class="tr"><b>${hm(r)}</b><small>${t('left')}</small></div>`}</div>`;
		}).join('');
		const missing = S.filter((s) => s.missing).map((s) => s.name);
		this._shell(`
			<div class="head"><ha-icon icon="mdi:timer-outline"></ha-icon><span>${esc(this._config.title || t('today'))}</span><em>${isNaN(left) ? '' : `${t('left')} ${hm(left)}`.trim()}</em></div>
			<div class="body">
				<div class="big">${hm(total)}<span>${isNaN(limit) ? t('no_limit') : ' / ' + hm(limit)}</span></div>
				<div class="per">${live.map((s) => `${esc(s.name)}: ${hm(this._num(s.used) || 0)}`).join(' · ')}</div>
				<div class="bar"><div style="width:${pct}%;background:${col}"></div></div>
				${next ? `<div class="next">${esc(next.state)}</div>` : ''}
				${missing.length ? `<div class="msg err">${t('missing')}${esc(missing.join(', '))}</div>` : ''}
				${tiles ? `<div class="tiles">${tiles}</div>` : ''}
			</div>
			${this._config.chart === false ? '' : '<div class="chart" id="chart"></div>'}`);
		if (this._config.chart !== false) this._chart();
	}

	async _chart() {
		const now = new Date(), midnight = new Date(now); midnight.setHours(0, 0, 0, 0);
		// finished hours come from the statistics, fetched once an hour; the current hour from the live states
		const key = `${midnight.getTime()}|${now.getHours()}`;
		if (this._config.demo_data) {  // shortcut: fixed bars for screenshots of a demo without statistics
			this._bars = Array.from({ length: 24 }, (_, h) => (h <= now.getHours() ? this._config.demo_data.hours[h] || 0 : 0));
			return this._drawHours();
		}
		if (this._chartKey !== key) {
			this._chartKey = key;
			this._hourly = await this._stats('hour', new Date(midnight.getTime() - 3600000));
		}
		const { ids, rows } = this._hourly;
		// each sensor counts up through the day and drops back at its daily reset (not always at midnight):
		// the time used in an hour is its growth over that hour, or its whole value right after a reset
		const bars = new Array(24).fill(0);
		for (const id of ids) {
			const list = (rows[id] || []).map((r) => ({ t: ms(r.start), v: r.max ?? r.state })).filter((r) => r.v != null);
			const cur = this._num(id);
			if (!isNaN(cur)) list.push({ t: now.getTime(), v: cur });
			let prev = list.length && list[0].t < midnight.getTime() ? list[0].v : 0;
			const own = new Array(24).fill(0);
			for (const r of list) {
				if (r.t < midnight.getTime()) continue;
				own[new Date(r.t).getHours()] += Math.max(0, r.v >= prev ? r.v - prev : r.v);
				prev = r.v;
			}
			// a device reports its usage in bursts (Family Link syncs late): no device is used more than
			// 60 min in an hour, the excess belongs to the hours before
			for (let h = 23; h > 0; h--) if (own[h] > 60) { own[h - 1] += own[h] - 60; own[h] = 60; }
			own.forEach((v, h) => { bars[h] += Math.min(60, v); });
		}
		this._bars = bars;
		this._drawHours();
	}

	_drawHours() {
		const el = this.shadowRoot.getElementById('chart');
		if (el) el.innerHTML = bars(this._bars, (i) => (i % 2 ? '' : `${i}h`), Math.max(60, Math.ceil(Math.max(...this._bars) / 15) * 15), (v) => `${Math.round(v)} min`, 4);
	}
}

/** The last days, one bar per day: each day's highest value, added up over the sources. */
class HistoryCard extends Base {
	static getStubConfig(hass) { return ScreenTimeCard.getStubConfig(hass); }
	getCardSize() { return 4; }

	async _update() {
		const days = this._config.days || 14, t = (k) => this._t(k);
		const key = `${new Date().toDateString()}|${days}`;
		if (this._config.demo_data && this._key !== key) {  // shortcut: fixed values for screenshots of a demo without statistics
			this._key = key;
			const start = new Date(); start.setHours(0, 0, 0, 0);
			this._days = this._config.demo_data.days.slice(-days).map((v, i, all) => ({ t: start.getTime() - (all.length - 1 - i) * 86400000, v }));
		}
		if (this._key !== key) {
			this._key = key;
			const start = new Date(); start.setHours(0, 0, 0, 0); start.setDate(start.getDate() - (days - 1));
			const { ids, rows } = await this._stats('day', start);
			this._days = Array.from({ length: days }, (_, i) => { const d = new Date(start); d.setDate(d.getDate() + i); return { t: d.getTime(), v: 0 }; });
			for (const id of ids) for (const r of rows[id] || []) {
				const day = this._days.find((d) => d.t === new Date(ms(r.start)).setHours(0, 0, 0, 0));
				if (day) day.v += r.max ?? r.state ?? 0;
			}
		}
		// today from the live states, the statistics of the day are not complete yet
		this._days[this._days.length - 1].v = sum(this._sources.filter((s) => !s.missing).map((s) => this._num(s.used) || 0));
		const top = Math.max(...this._days.map((d) => d.v));
		const step = top > 480 ? 240 : 60;
		this._shell(`
			<div class="head"><ha-icon icon="mdi:chart-bar"></ha-icon><span>${esc(this._config.title || t('history').replace('{n}', days))}</span></div>
			<div class="chart">${bars(this._days.map((d) => d.v), (i) => (i % 2 ? '' : `${new Date(this._days[i].t).getDate()}/${new Date(this._days[i].t).getMonth() + 1}`),
				Math.max(step * 2, Math.ceil(top / step) * step), (v) => `${Math.round(v / 60)} h`, 4, true)}</div>`);
	}
}

/** Bars in plain HTML: values, x label per index, top of the scale, y label, number of grid steps. */
function bars(values, xLabel, top, yLabel, steps, shade) {
	const grid = Array.from({ length: steps + 1 }, (_, i) => top - (i * top) / steps);
	const max = Math.max(...values, 1);
	return `<div class="plot">
		<div class="y">${grid.map((v) => `<span>${yLabel(v)}</span>`).join('')}</div>
		<div class="area">
			${grid.map((_, i) => `<i style="top:${(i * 100) / steps}%"></i>`).join('')}
			<div class="cols">${values.map((v, i) => `<div class="col"><div style="height:${Math.min(100, (v / top) * 100)}%;${shade ? `opacity:${0.4 + 0.6 * (v / max)}` : ''}" title="${hm(v)}"></div></div>`).join('')}</div>
		</div>
		<div></div><div class="x">${values.map((_, i) => `<span>${xLabel(i)}</span>`).join('')}</div>
	</div>`;
}

const CSS = `
	ha-card { overflow: hidden; }
	.msg { padding: 16px; opacity: 0.7; } .msg.err { color: var(--error-color); opacity: 1; padding: 6px 0 0; }
	.head { display: flex; align-items: center; gap: 8px; height: 40px; padding: 0 16px; border-bottom: 1px solid var(--divider-color); }
	.head ha-icon { --mdc-icon-size: 20px; opacity: 0.35; }
	.head span { font-size: 15px; font-weight: 700; flex: 1; }
	.head em { font-style: normal; font-size: 13px; color: var(--secondary-text-color); white-space: nowrap; }
	.body { padding: 14px 16px 16px; border-bottom: 1px solid var(--divider-color); }
	.big { font-size: 44px; font-weight: 600; line-height: 1; letter-spacing: -1px; }
	.big span { font-size: 20px; font-weight: 400; letter-spacing: 0; color: var(--secondary-text-color); }
	.per { font-size: 13px; color: var(--secondary-text-color); margin-top: 4px; }
	.bar { height: 8px; border-radius: 4px; background: var(--divider-color); margin: 12px 0 8px; }
	.bar div { height: 100%; border-radius: 4px; }
	.next { font-size: 12px; opacity: 0.5; }
	.tiles { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
	.tile { flex: 1; min-width: 140px; display: flex; align-items: center; gap: 10px; padding: 10px 12px; border-radius: 12px;
		background: rgba(var(--rgb-primary-text-color, 255, 255, 255), 0.05); }
	.tile ha-icon { --mdc-icon-size: 28px; flex: none; }
	.tt, .tr { line-height: 1.25; } .tt { flex: 1; min-width: 0; } .tr { text-align: right; }
	.tile b { display: block; font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.tile small { display: block; font-size: 12px; } .tr small { font-size: 11px; color: var(--secondary-text-color); }
	.chart { padding: 16px 12px 10px 8px; }
	.plot { display: grid; grid-template-columns: auto 1fr; grid-template-rows: 150px auto; column-gap: 6px; }
	.y { display: flex; flex-direction: column; justify-content: space-between; font-size: 10px; font-weight: 600; text-align: right; margin: -6px 0; }
	.area { position: relative; }
	.area i { position: absolute; left: 0; right: 0; border-top: 1px dashed rgba(var(--rgb-primary-text-color, 255, 255, 255), 0.2); }
	.cols { position: absolute; inset: 0; display: flex; gap: 3px; align-items: flex-end; }
	.col { flex: 1; height: 100%; display: flex; align-items: flex-end; }
	.col div { width: 100%; border-radius: 4px 4px 0 0; background: var(--accent-color); min-height: 0; }
	.x { display: flex; gap: 3px; font-size: 10px; font-weight: 600; margin-top: 6px; }
	.x span { flex: 1; text-align: center; white-space: nowrap; overflow: visible; }
`;

class Editor extends HTMLElement {
	setConfig(config) { this._config = config; this._render(); }
	set hass(hass) { this._hass = hass; this._render(); }

	_render() {
		if (!this._hass || !this._config) return;
		const t = STR[(this._hass.language || 'en').slice(0, 2)] || STR.en;
		const history = this._config.type === 'custom:parental-screen-time-history-card';
		if (!this._form) {
			this._form = document.createElement('ha-form');
			this._form.computeLabel = (s) => t['f_' + s.name];
			this._form.addEventListener('value-changed', (e) => {
				const config = { ...e.detail.value };
				Object.keys(config).forEach((k) => ((config[k] === '' || config[k] === undefined || (Array.isArray(config[k]) && !config[k].length)) && delete config[k]));
				this._config = config;
				this.dispatchEvent(new CustomEvent('config-changed', { detail: { config }, bubbles: true, composed: true }));
			});
			this.appendChild(this._form);
		}
		this._form.hass = this._hass;
		this._form.data = this._config;
		this._form.schema = [
			{ name: 'devices', selector: { device: { multiple: true, filter: PLATFORMS.map((integration) => ({ integration })) } } },
			{ name: 'entities', selector: { entity: { multiple: true, filter: { domain: 'sensor', device_class: 'duration' } } } },
			{ name: 'title', selector: { text: {} } },
			history ? { name: 'days', selector: { number: { min: 3, max: 31, mode: 'box' } } } : { name: 'chart', selector: { boolean: {} } },
			{ name: 'language', selector: { select: { mode: 'dropdown', options: [{ value: 'en', label: 'English' }, { value: 'fr', label: 'Français' }] } } },
		];
	}
}

if (!customElements.get('parental-screen-time-card')) {
	customElements.define('parental-screen-time-editor', Editor);
	customElements.define('parental-screen-time-card', ScreenTimeCard);
	customElements.define('parental-screen-time-history-card', HistoryCard);
	window.customCards = window.customCards || [];
	window.customCards.push(
		{ type: 'parental-screen-time-card', name: 'Screen time today', description: 'Screen time of several devices added up, with limits, locks and an hourly chart (Family Link, Family Safety, any time sensor).', preview: true },
		{ type: 'parental-screen-time-history-card', name: 'Screen time history', description: 'Screen time of the last days, several devices added up.', preview: true },
	);
}
})();
