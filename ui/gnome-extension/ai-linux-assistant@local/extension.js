// AI Linux Assistant overlay — GNOME Shell extension (GJS / ESM, GNOME 48–50).
//
// Single entry point: the top-bar icon starts/stops the engine, shows honest state, and controls it.
// The engine writes live state to $XDG_RUNTIME_DIR/ai-linux/state.json (~2s heartbeat ts); this reads
// it to know if the engine is running and to drive the orb. Control goes back via control.json.

import GObject from 'gi://GObject';
import St from 'gi://St';
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Pango from 'gi://Pango';
import cairo from 'gi://cairo';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

// Shared settings core (same module the prefs window uses — one source of truth).
import {
    RUNTIME_DIR, STATE_PATH, SETTINGS_PATH, SETTINGS_DEFAULTS, MENU_VOICES, WAKE_WORDS,
    readSettings, saveKey, writeControl, writeVoice, wakeControl,
} from './settingsLib.js';
// Vendored (MIT) window-control D-Bus service — dormant unless the window_control setting is on.
import {WindowControl} from './windowControl.js';

const DESKTOP_ID = 'ai-linux-assistant.desktop';

const DIR = RUNTIME_DIR;

const STATES = ['loading', 'idle', 'listening', 'thinking', 'speaking', 'muted', 'off'];
const ACTIVE_STATES = ['loading', 'thinking', 'speaking']; // force the overlay open during a turn
const STALE_MS = 6000;            // state.json older than this => engine not running (heartbeat is 2s)
const IDLE_HIDE_MS = 10000;       // fade out + hide after 10s with no conversation activity
const TRANSCRIPT_FRESH_MS = 1500; // a just-changed transcript still counts as activity this tick
const PIN_TIMEOUT_MS = 30000;
const LOG_MAX = 24;
const STARTING_TIMEOUT = 60000;   // stop the "starting" blink if the engine never comes up

// A flowing multi-colour "plasma" orb drawn with Cairo on an St.DrawingArea at ~30fps. Cairo (not a GPU
// shader) renders identically offline and live, so the look is predictable. Each state has its OWN palette,
// motion mode and speed so it reads at a glance: idle drifts slowly, listening ripples, thinking swirls
// fast, speaking pulses energetically. 2-3 colours per state; soft blobs orbit and blend inside the sphere.
const ORB_PARAMS = {
    loading:   {colors: [[1.00, 0.78, 0.25], [1.00, 0.50, 0.12], [1.00, 0.88, 0.45]], speed: 1.8,  amp: 0.07, blobs: 3, mode: 'pulse'},
    idle:      {colors: [[0.28, 0.40, 0.95], [0.45, 0.30, 0.88], [0.16, 0.62, 0.86]], speed: 0.45, amp: 0.04, blobs: 3, mode: 'drift'},
    listening: {colors: [[0.13, 0.85, 0.55], [0.16, 0.70, 0.92], [0.50, 0.92, 0.42]], speed: 1.4,  amp: 0.07, blobs: 3, mode: 'ripple'},
    thinking:  {colors: [[0.56, 0.30, 0.99], [0.88, 0.26, 0.86], [0.36, 0.46, 1.00]], speed: 2.7,  amp: 0.09, blobs: 4, mode: 'swirl'},
    speaking:  {colors: [[1.00, 0.18, 0.55], [1.00, 0.46, 0.22], [0.97, 0.16, 0.78]], speed: 3.6,  amp: 0.15, blobs: 4, mode: 'pulse'},
    muted:     {colors: [[0.46, 0.46, 0.54], [0.36, 0.36, 0.44]], speed: 0.0, amp: 0.0, blobs: 2, mode: 'static'},
    off:       {colors: [[0.40, 0.40, 0.48], [0.32, 0.32, 0.40]], speed: 0.0, amp: 0.0, blobs: 2, mode: 'static'},
};

const Orb = GObject.registerClass(
class Orb extends St.DrawingArea {
    _init() {
        super._init({style_class: 'ai-orb', width: 112, height: 112, reactive: false});
        this._state = 'idle';
        this._t = 0;
        this._timer = 0;
        this.connect('repaint', this._draw.bind(this));
        this.connect('notify::mapped', () => this._sync());   // pause when hidden (perf)
        this.connect('destroy', () => this._stop());
    }

    setState(s) {
        this._state = ORB_PARAMS[s] ? s : 'idle';
        this._sync();
        this.queue_repaint();
    }

    _sync() {
        const p = ORB_PARAMS[this._state];
        if (this.mapped && p.speed > 0) this._start();
        else { this._stop(); this.queue_repaint(); }   // static states (muted/off) still draw one frame
    }

    _start() {
        if (this._timer) return;
        this._timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 33, () => { // ~30 fps
            this._t += 0.033;
            this.queue_repaint();
            return GLib.SOURCE_CONTINUE;
        });
    }

    _stop() {
        if (this._timer) { GLib.source_remove(this._timer); this._timer = 0; }
    }

    _draw(area) {
        const cr = area.get_context();
        const [w, h] = area.get_surface_size();
        const p = ORB_PARAMS[this._state];
        const pal = p.colors, c0 = pal[0], t = this._t;
        const cx = w / 2, cy = h / 2, R = Math.min(cx, cy);
        const mn = (x) => Math.min(1, x);

        cr.setOperator(cairo.Operator.CLEAR); cr.paint();
        cr.setOperator(cairo.Operator.OVER);

        // Overall size motion: 'pulse' (speaking/loading) beats with two frequencies for an organic
        // throb; everything else just breathes gently. Idle barely moves; speaking heaves.
        const pulse = (p.mode === 'pulse')
            ? 1 + p.amp * (0.6 * Math.sin(t * p.speed * 1.6) + 0.4 * Math.sin(t * p.speed * 2.7 + 1))
            : 1 + p.amp * Math.sin(t * p.speed);
        const Rs = R * 0.62 * pulse;

        try {
            // soft outer halo (primary colour)
            let gl = new cairo.RadialGradient(cx, cy, Rs * 0.40, cx, cy, R * 0.99);
            gl.addColorStopRGBA(0, c0[0], c0[1], c0[2], 0.32);
            gl.addColorStopRGBA(0.5, c0[0], c0[1], c0[2], 0.09);
            gl.addColorStopRGBA(1, c0[0], c0[1], c0[2], 0.0);
            cr.setSource(gl); cr.arc(cx, cy, R * 0.99, 0, 2 * Math.PI); cr.fill();

            // translucent glass body (dark core -> colour -> soft edge)
            let bd = new cairo.RadialGradient(cx, cy, 0, cx, cy, Rs);
            bd.addColorStopRGBA(0.00, c0[0] * 0.35, c0[1] * 0.35, c0[2] * 0.5, 0.55);
            bd.addColorStopRGBA(0.70, c0[0] * 0.85, c0[1] * 0.85, c0[2] * 0.95, 0.45);
            bd.addColorStopRGBA(1.00, c0[0], c0[1], c0[2], 0.0);
            cr.setSource(bd); cr.arc(cx, cy, Rs, 0, 2 * Math.PI); cr.fill();

            // ---- flowing fluid: soft colour blobs orbiting + blending inside the sphere ----
            cr.save();
            cr.arc(cx, cy, Rs * 0.98, 0, 2 * Math.PI); cr.clip();
            const K = p.blobs;
            for (let k = 0; k < K; k++) {
                const col = pal[k % pal.length];
                const dir = (k % 2) ? -1 : 1;                       // alternate spin -> they cross + mix
                const sp = p.speed * (0.55 + 0.22 * k);
                const ang = t * sp * dir + k * 2.2;
                const orbit = Rs * (0.30 + 0.20 * Math.sin(t * p.speed * 0.7 + k * 1.7));  // breathing radius
                const bx = cx + Math.cos(ang) * orbit;
                const by = cy + Math.sin(ang * 1.25 + k) * orbit * 0.92;                   // elliptical -> organic
                const br = Rs * (0.55 + 0.18 * Math.sin(t * p.speed + k * 2.0));
                let bl = new cairo.RadialGradient(bx, by, 0, bx, by, br);
                bl.addColorStopRGBA(0, mn(col[0] * 1.25), mn(col[1] * 1.25), mn(col[2] * 1.25), 0.55);
                bl.addColorStopRGBA(0.6, col[0], col[1], col[2], 0.20);
                bl.addColorStopRGBA(1, col[0], col[1], col[2], 0.0);
                cr.setSource(bl); cr.arc(cx, cy, Rs, 0, 2 * Math.PI); cr.fill();
            }
            cr.restore();

            // per-mode signature motion
            if (p.mode === 'ripple') {            // listening: concentric rings expanding outward
                for (let i = 0; i < 2; i++) {
                    const ph = ((t * p.speed * 0.5) + i * 0.5) % 1;
                    const rr = Rs * 0.55 + ph * (R * 0.96 - Rs * 0.55);
                    cr.setLineWidth(R * 0.022);
                    cr.setSourceRGBA(pal[1][0], pal[1][1], pal[1][2], 0.40 * (1 - ph));
                    cr.arc(cx, cy, rr, 0, 2 * Math.PI); cr.stroke();
                }
            } else if (p.mode === 'swirl') {      // thinking: a bright arc sweeping round
                const a0 = t * p.speed * 1.4;
                cr.setLineWidth(R * 0.05);
                cr.setSourceRGBA(mn(pal[1][0] * 1.2), mn(pal[1][1] * 1.2), mn(pal[1][2] * 1.3), 0.5);
                cr.arc(cx, cy, Rs * 0.84, a0, a0 + Math.PI * 0.7); cr.stroke();
            }

            // crisp bright rim
            cr.setLineWidth(R * 0.022);
            cr.setSourceRGBA(mn(c0[0] * 1.5), mn(c0[1] * 1.5), mn(c0[2] * 1.7), 0.6);
            cr.arc(cx, cy, Rs * 0.98, 0, 2 * Math.PI); cr.stroke();

            // glassy specular highlight (upper-left) — static, sells the 3D sphere
            const hx = cx - Rs * 0.34, hy = cy - Rs * 0.36;
            let hl = new cairo.RadialGradient(hx, hy, 0, hx, hy, Rs * 0.55);
            hl.addColorStopRGBA(0, 1, 1, 1, 0.55);
            hl.addColorStopRGBA(1, 1, 1, 1, 0.0);
            cr.setSource(hl); cr.arc(hx, hy, Rs * 0.55, 0, 2 * Math.PI); cr.fill();
        } catch (e) {
            cr.setSourceRGBA(c0[0], c0[1], c0[2], 0.9);  // fallback: never invisible
            cr.arc(cx, cy, Rs, 0, 2 * Math.PI); cr.fill();
        }

        cr.$dispose();
    }
});

const Overlay = GObject.registerClass(
class Overlay extends St.BoxLayout {
    _init() {
        super._init({
            orientation: Clutter.Orientation.VERTICAL,
            style_class: 'ai-overlay',
            reactive: true,
            track_hover: true,
        });

        // orb (clickable for click-to-talk)
        this._orb = new Orb();
        this._orbStack = new St.Widget({
            layout_manager: new Clutter.BinLayout(),
            style_class: 'ai-orbstack',
            x_align: Clutter.ActorAlign.END,
        });
        this._orbStack.add_child(this._orb);
        // orb body click -> _onOrbClick (click-to-talk; wired in enable()). Use the button-release-event
        // signal, not Clutter.ClickAction — that class was removed in the Mutter 48+ gesture refactor
        // (GNOME 50), where `new Clutter.ClickAction()` throws "is not a constructor".
        this._orbStack.reactive = true;
        this._onOrbClick = null;
        this._orbStack.connect('button-release-event', () => {
            if (this._onOrbClick) this._onOrbClick();
            return Clutter.EVENT_STOP;
        });
        this.add_child(this._orbStack);

        // transcript panel (translucent; no Shell blur — its square corners poked past the rounding)
        this._panel = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, style_class: 'ai-panel'});
        this._scroll = new St.ScrollView({style_class: 'ai-scroll', x_expand: true});
        this._scroll.set_policy(St.PolicyType.NEVER, St.PolicyType.EXTERNAL);  // scrollable but no visible scrollbar
        this._scroll.style = 'max-height: 240px;';
        this._logBox = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, style_class: 'ai-log', x_expand: true});
        this._scroll.set_child(this._logBox);
        this._panel.add_child(this._scroll);
        this._panel.visible = false;
        this.add_child(this._panel);

        this._state = '';
        this._anchor = 'top';
        this._scrollIdleId = 0;
    }

    // Reorder rows so the transcript stacks ABOVE the orb when anchored at the bottom of the screen
    // (chat grows upward), and orb-on-top when anchored at the top. Driven by _reposition().
    setAnchor(anchor) {
        if (anchor !== 'top' && anchor !== 'bottom') anchor = 'bottom';
        if (this._anchor === anchor) return;
        this._anchor = anchor;
        for (const c of [this._orbStack, this._panel]) this.remove_child(c);
        const order = anchor === 'bottom'
            ? [this._panel, this._orbStack]   // transcript on top, orb at the bottom
            : [this._orbStack, this._panel];  // orb on top (classic)
        for (const c of order) this.add_child(c);
    }

    update(data) {
        const state = STATES.includes(data.state) ? data.state : 'idle';
        this._state = state;
        this._orb.setState(state);
        return state;
    }

    renderLog(entries) {
        this._logBox.destroy_all_children();
        for (const e of entries) {
            const lbl = new St.Label({
                style_class: e.role === 'you' ? 'ai-you' : 'ai-ai',
                x_expand: true,
                text: (e.role === 'you' ? 'You: ' : 'AI: ') + e.text,
            });
            lbl.clutter_text.line_wrap = true;
            lbl.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
            lbl.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
            this._logBox.add_child(lbl);
        }
        this._panel.visible = entries.length > 0;
        this._scrollToBottom();
    }

    _scrollToBottom() {
        if (this._scrollIdleId) GLib.source_remove(this._scrollIdleId);
        this._scrollIdleId = GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
            this._scrollIdleId = 0;
            try {
                const adj = this._scroll?.get_vadjustment();
                if (adj) adj.set_value(Math.max(0, adj.get_upper() - adj.get_page_size()));
            } catch (e) {}
            return GLib.SOURCE_REMOVE;
        });
    }

    destroy() {
        if (this._scrollIdleId) { GLib.source_remove(this._scrollIdleId); this._scrollIdleId = 0; }
        super.destroy();
    }
});

const Indicator = GObject.registerClass(
class Indicator extends PanelMenu.Button {
    _init(cb) {
        super._init(0.5, 'AI Linux Assistant'); // 0.5 = center the popup menu under the icon
        this._cb = cb;
        this._samplesDir = cb.samplesDir ?? '';
        this._dotState = '';

        const box = new St.BoxLayout({style_class: 'ai-indicator-box'});
        this._mono = new St.Label({text: 'AI', style_class: 'ai-mono state-off', y_align: Clutter.ActorAlign.CENTER});
        this._mono.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;  // show "AI", never an ellipsized "…"
        box.add_child(this._mono);
        this.add_child(box);

        this._header = new PopupMenu.PopupMenuItem('AI Linux', {reactive: false});
        this.menu.addMenuItem(this._header);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        // START (blue) / STOP (red) rounded gradient button.
        this._startStopItem = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        this._startBtn = new St.Button({style_class: 'ai-startstop start', label: 'START', x_expand: true, can_focus: true});
        this._startBtn.connect('clicked', () => { this._cb.startStop(); this.menu.close(); });
        this._startStopItem.add_child(this._startBtn);
        this.menu.addMenuItem(this._startStopItem);

        // Voice picker (4 curated, each with a speaker preview).
        this._voiceSub = new PopupMenu.PopupSubMenuMenuItem('Voice');
        this._voiceItems = {};
        for (const v of MENU_VOICES) {
            const item = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
            const selectBtn = new St.Button({
                style_class: 'ai-voice-select', x_expand: true, can_focus: true,
                child: new St.Label({text: v.label, y_align: Clutter.ActorAlign.CENTER}),
            });
            selectBtn.connect('clicked', () => this._pickVoice(v.id));
            const previewBtn = new St.Button({
                style_class: 'ai-voice-preview', can_focus: true, accessible_name: 'Play sample',
                child: new St.Icon({icon_name: 'audio-volume-high-symbolic', icon_size: 16}),
            });
            previewBtn.connect('clicked', () => this._playSample(v.sample));
            item.add_child(selectBtn);
            item.add_child(previewBtn);
            this._voiceItems[v.id] = item;
            this._voiceSub.menu.addMenuItem(item);
        }
        this.menu.addMenuItem(this._voiceSub);

        // Listening: wake word (Computer / Jarvis / AI), always-on, or click-to-talk.
        this._wakeSub = new PopupMenu.PopupSubMenuMenuItem('Listening');
        this._wakeItems = {};
        for (const w of WAKE_WORDS) {
            const it = new PopupMenu.PopupMenuItem(w.label);
            it.connect('activate', () => this._pickWake(w.id));
            this._wakeItems[w.id] = it;
            this._wakeSub.menu.addMenuItem(it);
        }
        this.menu.addMenuItem(this._wakeSub);

        // Mute + Show overlay as SWITCHES so their on/off state is visible at a glance.
        this._muteItem = new PopupMenu.PopupSwitchMenuItem('Mute microphone', false);
        this._muteItem.connect('toggled', (_i, state) => {
            if (this._syncing) return;                       // ignore programmatic setToggleState
            writeControl({action: state ? 'mute' : 'unmute'});
        });
        this.menu.addMenuItem(this._muteItem);

        this._showItem = new PopupMenu.PopupSwitchMenuItem('Show overlay', false);
        this._showItem.connect('toggled', (_i, state) => {
            if (this._syncing) return;
            this._cb.setOverlay(state);
        });
        this.menu.addMenuItem(this._showItem);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        const prefs = new PopupMenu.PopupMenuItem('All settings');
        prefs.connect('activate', () => this._cb.openPrefs());
        this.menu.addMenuItem(prefs);

        this.refreshSettings();  // labels/ornaments from the shared store (also re-run by the file monitor)
    }

    refreshSettings() {
        // Re-read the shared store so a change made ANYWHERE (prefs window, this menu, even a
        // hand edit) is reflected here — the settings.json file monitor calls this on every write.
        this._settings = readSettings();
        const voice = this._settings.voice ?? SETTINGS_DEFAULTS.voice;
        this._voiceSub.label.text = 'Voice: ' + voice;
        for (const k in this._voiceItems)
            this._voiceItems[k].setOrnament(k === voice ? PopupMenu.Ornament.DOT : PopupMenu.Ornament.NONE);
        const wake = this._settings.wake_word ?? SETTINGS_DEFAULTS.wake_word;
        this._wakeSub.label.text = this._wakeLabel(wake);
        for (const k in this._wakeItems)
            this._wakeItems[k].setOrnament(k === wake ? PopupMenu.Ornament.DOT : PopupMenu.Ornament.NONE);
    }

    setState(state, running) {
        this._running = !!running;  // used by _pickVoice's live-vs-next-start notification
        const s = running ? (STATES.includes(state) ? state : 'idle') : 'off';
        this._startBtn.reactive = true;   // clear any "starting" lock
        this._mono.style_class = `ai-mono state-${s}`;
        this._header.label.text = running ? `AI Linux: ${s}` : 'AI Linux: not running';
        this._startBtn.label = running ? 'STOP' : 'START';
        this._startBtn.style_class = running ? 'ai-startstop stop' : 'ai-startstop start';
        this._syncSwitch(this._muteItem, running && s === 'muted');
        this._muteItem.setSensitive(running);
        this._showItem.setSensitive(running);

        if (s === this._dotState) return;
        this._dotState = s;
        this._mono.remove_all_transitions();
        this._mono.opacity = 255;
        const PULSE = {loading: 700, listening: 1300, thinking: 500, speaking: 380};
        if (PULSE[s]) {
            this._mono.ease({
                opacity: 90,
                duration: PULSE[s],
                mode: Clutter.AnimationMode.EASE_IN_OUT_SINE,
                autoReverse: true,
                repeatCount: -1,
            });
        }
    }

    // Set a switch's visual state WITHOUT it echoing back as a user 'toggled' (guarded by _syncing).
    _syncSwitch(item, state) {
        this._syncing = true;
        item.setToggleState(state);
        this._syncing = false;
    }

    // Blink the monogram amber while the engine boots, before it writes its first state.json.
    setStarting() {
        this._header.label.text = 'AI Linux: starting';
        this._startBtn.label = 'STARTING';
        this._startBtn.reactive = false;   // no second launch mid-boot
        if (this._dotState === 'starting') return;
        this._dotState = 'starting';
        this._mono.style_class = 'ai-mono state-loading';
        this._mono.remove_all_transitions();
        this._mono.opacity = 255;
        this._mono.ease({
            opacity: 90, duration: 600, mode: Clutter.AnimationMode.EASE_IN_OUT_SINE,
            autoReverse: true, repeatCount: -1,
        });
    }

    setOverlayShown(shown) {
        this._syncSwitch(this._showItem, !!shown);
    }

    _pickVoice(id) {
        saveKey('voice', id);   // persists for next Start (launcher -> GLADOS_VOICE)
        writeVoice(id);         // applies LIVE via the bridge's voice.json channel
        this.refreshSettings();
        Main.notify('AI Linux Assistant',
            this._running ? 'Voice switched to ' + id + '.' : 'Voice ' + id + ' applies on Start.');
    }

    _wakeLabel(id) {
        const w = WAKE_WORDS.find(x => x.id === id);
        return 'Listening: ' + (w ? w.label.replace(/\s*\(.*\)$/, '') : id);  // drop the parenthetical note
    }

    _pickWake(id) {
        saveKey('wake_word', id);        // persists for next Start
        writeControl(wakeControl(id));   // live to a running engine
        this.refreshSettings();
        const msg = id === 'always' ? 'Always listening.'
            : id === 'click' ? 'Click the orb to talk.' : 'Wake word set to ' + id + '.';
        Main.notify('AI Linux Assistant', msg);
    }

    _playSample(file) {
        try {
            const path = GLib.build_filenamev([this._samplesDir, file]);
            global.display.get_sound_player().play_from_file(Gio.File.new_for_path(path), 'AI voice sample', null);
        } catch (e) {
            try {
                Gio.Subprocess.new(['pw-play', GLib.build_filenamev([this._samplesDir, file])], Gio.SubprocessFlags.NONE);
            } catch (e2) {
                Main.notify('AI Linux Assistant', 'Could not play the voice sample.');
            }
        }
    }

    destroy() {
        this._mono?.remove_all_transitions();
        super.destroy();
    }
});

export default class AiLinuxOverlayExtension extends Extension {
    enable() {
        this._pinned = false;
        this._activeUntil = 0;
        this._lastYou = '';
        this._lastReply = '';
        this._lastYouTs = 0;
        this._lastReplyTs = 0;
        this._lastTranscriptTs = 0;
        this._pinTimeoutId = 0;
        this._log = [];
        this._running = false;
        this._mode = '';             // last listening mode from state.json (always|wake|click)
        this._session = false;       // wake-word conversation window open (keeps the overlay up)
        this._shownTarget = false;   // desired overlay visibility (drives fade in/out)
        this._starting = false;      // engine launch in progress (blinks the top-bar icon)
        this._startedAt = 0;

        this._overlay = new Overlay();
        // In click-to-talk mode, tapping the orb starts one listen turn.
        this._overlay._onOrbClick = () => {
            if (this._running && this._mode === 'click') writeControl({action: 'activate'});
        };
        Main.layoutManager.addChrome(this._overlay);
        this._overlay.hide();
        this._reposition();
        this._monitorsId = Main.layoutManager.connect('monitors-changed', () => this._reposition());

        this._indicator = new Indicator({
            startStop: () => this._startStop(),
            setOverlay: (want) => this._setOverlayPinned(want),
            openPrefs: () => this.openPreferences(),
            samplesDir: GLib.build_filenamev([this.path, 'samples']),
        });
        Main.panel.addToStatusArea('ai-linux-assistant', this._indicator, 0, 'right');

        // Window-control D-Bus service (vendored, MIT): registered only when the setting is on.
        this._windowControl = new WindowControl();
        this._syncWindowControl();

        try {
            const dir = Gio.File.new_for_path(DIR);
            if (!dir.query_exists(null)) dir.make_directory_with_parents(null);
            this._dirMon = dir.monitor_directory(Gio.FileMonitorFlags.NONE, null);
            this._dirMonId = this._dirMon.connect('changed', (_m, f) => {
                if (f && f.get_basename() === 'state.json') this._readState();
            });
        } catch (e) {
            logError(e, 'ai-linux: dir monitor failed');
        }
        try {
            // Keep the panel in ALWAYS-SYNC with the shared store: any settings.json write —
            // from the prefs window, this menu, or by hand — refreshes the menu AND the
            // window-control service immediately (no relogin needed to start/stop it).
            this._settingsMon = Gio.File.new_for_path(SETTINGS_PATH).monitor_file(Gio.FileMonitorFlags.NONE, null);
            this._settingsMonId = this._settingsMon.connect('changed', () => {
                this._indicator?.refreshSettings();
                this._syncWindowControl();
                this._reposition();
            });
        } catch (e) {
            logError(e, 'ai-linux: settings monitor failed');
        }
        this._pollId = GLib.timeout_add(GLib.PRIORITY_DEFAULT_IDLE, 1000, () => {
            this._readState();
            return GLib.SOURCE_CONTINUE;
        });

        this._readState();
    }

    _startStop() {
        if (this._running) {
            writeControl({action: 'quit'});
        } else {
            const app = Gio.DesktopAppInfo.new(DESKTOP_ID);
            if (app) {
                try {
                    app.launch([], null);
                    this._starting = true;                                  // blink the top-bar icon while it boots
                    this._startedAt = GLib.get_monotonic_time() / 1000;
                    this._indicator?.setStarting();
                } catch (e) { Main.notify('AI Linux Assistant', 'Failed to start: ' + e); }
            } else {
                Main.notify('AI Linux Assistant', 'Launcher not found. Run "./ai-linux setup", or start it from a terminal with "./ai-linux".');
            }
        }
    }

    _setOverlayPinned(want) {
        if (!this._overlay) return;
        if (want) {
            this._pinned = true;
            this._armPinTimeout();
            this._fadeIn();
        } else {
            this._pinned = false;
            this._clearPinTimeout();
            this._fadeOut();
        }
    }

    _armPinTimeout() {
        this._clearPinTimeout();
        this._pinTimeoutId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, PIN_TIMEOUT_MS, () => {
            this._pinned = false;
            this._pinTimeoutId = 0;
            this._applyVisibility(this._overlay?._state || 'idle');
            return GLib.SOURCE_REMOVE;
        });
    }

    _clearPinTimeout() {
        if (this._pinTimeoutId) {
            GLib.source_remove(this._pinTimeoutId);
            this._pinTimeoutId = 0;
        }
    }

    _pushLog(role, text) {
        const last = this._log[this._log.length - 1];
        if (last && last.role === role && last.text === text) return;
        this._log.push({role, text});
        if (this._log.length > LOG_MAX) this._log.shift();
    }

    _readState() {
        try {
            let running = false;
            let data = null;
            const f = Gio.File.new_for_path(STATE_PATH);
            if (f.query_exists(null)) {
                const [ok, contents] = GLib.file_get_contents(STATE_PATH);
                if (ok) {
                    data = JSON.parse(new TextDecoder().decode(contents));
                    const nowMs = GLib.get_real_time() / 1000; // µs -> ms (wall clock, matches ts)
                    const fresh = data.ts && (nowMs - data.ts) < STALE_MS;
                    running = !!fresh && data.state !== 'off';
                }
            }
            this._running = running;

            if (!running) {
                if (this._log.length) { this._log = []; this._overlay?.renderLog(this._log); }
                this._lastYou = '';
                this._lastReply = '';
                this._lastYouTs = 0;
                this._lastReplyTs = 0;
                this._session = false;
                const bootMs = GLib.get_monotonic_time() / 1000;
                if (this._starting && (bootMs - this._startedAt) < STARTING_TIMEOUT) {
                    this._indicator?.setStarting();   // keep blinking until the engine reports in
                } else {
                    this._starting = false;
                    this._indicator?.setState('off', false);
                }
                this._applyVisibility('off');
                return;
            }

            this._starting = false;   // engine reported in — stop the "starting" blink
            this._mode = data.mode || '';
            this._session = !!data.session;
            const state = this._overlay.update(data);
            this._indicator?.setState(state, true);

            const you = (data.you || '').trim();
            const reply = (data.assistant || '').trim();
            // dedup on (text, event ts): the ts lets a REPEATED identical utterance/reply still bubble
            const youTs = data.you_ts ?? 0;
            const replyTs = data.reply_ts ?? 0;
            let changed = false;
            if (you && (you !== this._lastYou || youTs !== this._lastYouTs)) {
                this._lastYou = you; this._lastYouTs = youTs; this._pushLog('you', you); changed = true;
            }
            if (reply && (reply !== this._lastReply || replyTs !== this._lastReplyTs)) {
                this._lastReply = reply; this._lastReplyTs = replyTs; this._pushLog('ai', reply); changed = true;
            }
            if (changed) {
                this._overlay.renderLog(this._log);
                this._reposition();
                this._lastTranscriptTs = GLib.get_monotonic_time() / 1000;
            }
            this._applyVisibility(state);
        } catch (e) {
            // bad/mid-write tick; keep polling
        }
    }

    _applyVisibility(state) {
        if (!this._overlay) return;
        if (state === 'off') {
            if (!this._pinned) { this._activeUntil = 0; this._fadeOut(); }
            return;
        }
        const nowMs = GLib.get_monotonic_time() / 1000;
        // "someone is speaking" = a turn is in progress (thinking/speaking/loading) or the transcript
        // just changed; plain idle/listening (mic open, waiting) does NOT keep it open.
        const active = ACTIVE_STATES.includes(state) || (nowMs - this._lastTranscriptTs < TRANSCRIPT_FRESH_MS);
        if (active) this._activeUntil = nowMs + IDLE_HIDE_MS;   // keep open until 10s after the last activity
        // Show while: pinned, click-to-talk (orb must stay reachable), a wake conversation window is open,
        // or there was recent activity. In wake mode this means the overlay appears on the wake word and
        // goes away after the session's silence timeout.
        const visible = this._pinned || this._mode === 'click' || this._session || nowMs < this._activeUntil;
        if (visible) this._fadeIn(); else this._fadeOut();
    }

    // fade the overlay in (appears when speech starts) / out (after 10s of silence); idempotent per tick
    _fadeIn() {
        if (!this._overlay || this._shownTarget) return;
        this._shownTarget = true;
        this._indicator?.setOverlayShown(true);
        this._overlay.remove_all_transitions();
        if (!this._overlay.visible) {
            this._overlay.opacity = 0;
            this._overlay.show();
            this._reposition();
        }
        this._overlay.ease({opacity: 255, duration: 320, mode: Clutter.AnimationMode.EASE_OUT_QUAD});
    }

    _fadeOut() {
        if (!this._overlay || !this._shownTarget) return;
        this._shownTarget = false;
        this._indicator?.setOverlayShown(false);
        this._overlay.remove_all_transitions();
        if (!this._overlay.visible) return;
        this._overlay.ease({
            opacity: 0,
            duration: 300,
            mode: Clutter.AnimationMode.EASE_IN_QUAD,
            onComplete: () => { if (!this._shownTarget && this._overlay) this._overlay.hide(); },
        });
    }

    _reposition() {
        if (!this._overlay) return;
        const mon = Main.layoutManager.primaryMonitor;
        if (!mon) return;
        const pos = readSettings().overlay_position ?? SETTINGS_DEFAULTS.overlay_position;
        const [, natW] = this._overlay.get_preferred_width(-1);
        const [, natH] = this._overlay.get_preferred_height(natW || -1);
        const w = natW || 340, h = natH || 200, M = 16;
        let x, y, anchor;
        if (pos === 'top-right') {
            x = mon.x + mon.width - w - M; y = mon.y + 44; anchor = 'top';
        } else { // bottom-right (default)
            x = mon.x + mon.width - w - M; y = mon.y + mon.height - h - M; anchor = 'bottom';
        }
        if (x < mon.x + 8) x = mon.x + 8;
        if (y < mon.y + 8) y = mon.y + 8;
        this._overlay.setAnchor(anchor);
        this._overlay.set_position(Math.round(x), Math.round(y));
    }

    _syncWindowControl() {
        // Register the D-Bus service iff the user turned window control on; release it otherwise.
        const want = !!(readSettings().window_control ?? SETTINGS_DEFAULTS.window_control);
        if (!this._windowControl) return;
        if (want && !this._windowControl.active) this._windowControl.enable();
        else if (!want && this._windowControl.active) this._windowControl.disable();
    }

    disable() {
        if (this._windowControl) { this._windowControl.disable(); this._windowControl = null; }
        if (this._pollId) { GLib.source_remove(this._pollId); this._pollId = null; }
        this._clearPinTimeout();
        if (this._dirMon) {
            if (this._dirMonId) this._dirMon.disconnect(this._dirMonId);
            this._dirMon.cancel();
            this._dirMon = null;
        }
        if (this._settingsMon) {
            if (this._settingsMonId) this._settingsMon.disconnect(this._settingsMonId);
            this._settingsMon.cancel();
            this._settingsMon = null;
        }
        if (this._monitorsId) { Main.layoutManager.disconnect(this._monitorsId); this._monitorsId = null; }
        if (this._indicator) { this._indicator.destroy(); this._indicator = null; }
        if (this._overlay) {
            Main.layoutManager.removeChrome(this._overlay);
            this._overlay.destroy();
            this._overlay = null;
        }
        this._log = [];
    }
}
