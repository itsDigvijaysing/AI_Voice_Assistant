// Shared settings core — the SINGLE implementation used by BOTH the shell extension
// (extension.js) and the preferences window (prefs.js, a separate gjs process).
// Storage contract: ~/.config/ai-linux/settings.json holds ONLY user-touched keys; DEFAULTS are
// display-time fallbacks and are never written back (the launcher treats present keys as explicit
// user choices). Live-apply channels go through the engine bridge's runtime files.
// This module imports only GLib so it is safe in both processes.

import GLib from 'gi://GLib';

export const RUNTIME_DIR = GLib.build_filenamev([GLib.get_user_runtime_dir(), 'ai-linux']);
export const STATE_PATH = GLib.build_filenamev([RUNTIME_DIR, 'state.json']);
export const CONTROL_PATH = GLib.build_filenamev([RUNTIME_DIR, 'control.json']);
export const VOICE_PATH = GLib.build_filenamev([RUNTIME_DIR, 'voice.json']);
export const SETTINGS_DIR = GLib.build_filenamev([GLib.get_user_config_dir(), 'ai-linux']);
export const SETTINGS_PATH = GLib.build_filenamev([SETTINGS_DIR, 'settings.json']);

// think:true mirrors the config default (llm_think: true — reasoning is required for reliable
// tool-calling on qwen3).
export const SETTINGS_DEFAULTS = {
    model: 'qwen3:4b', think: true, wake_word: 'computer',
    voice: 'M1', actions: true, barge_in: true,
    window_control: false,  // GUI-automation window service; off = the D-Bus service never registers
    overlay_position: 'bottom-right',  // bottom-right | top-right
};

export const MODELS = [
    {id: 'qwen3:4b', label: 'Smart (qwen3:4b)'},
    {id: 'qwen3:1.7b', label: 'Fast (qwen3:1.7b)'},
];
// Curated: Computer (most STT-robust, the default), Jarvis (distinct, reliable), AI (short — least
// reliable for speech-to-text but on-brand), plus always-on and click-to-talk. Assistant/Hey Linux dropped.
export const WAKE_WORDS = [
    {id: 'computer', label: 'Computer (recommended)'}, {id: 'jarvis', label: 'Jarvis'},
    {id: 'ai', label: 'AI (short, less reliable)'},
    {id: 'always', label: 'Always listening (no wake word)'},
    {id: 'click', label: 'Click to talk (mic off until clicked)'},
];

// The voices offered in BOTH the top-bar menu and Preferences (each has a shipped preview clip).
export const MENU_VOICES = [
    {id: 'M1', label: 'Male 1 (default)', sample: 'M1.ogg'},
    {id: 'M4', label: 'Male 2',           sample: 'M4.ogg'},
    {id: 'F1', label: 'Female 1',         sample: 'F1.ogg'},
    {id: 'F3', label: 'Female 2',         sample: 'F3.ogg'},
];

export const OVERLAY_POSITIONS = [
    {id: 'bottom-right', label: 'Bottom-right'},
    {id: 'top-right',    label: 'Top-right'},
];

export function readSettings() {
    try {
        const [ok, c] = GLib.file_get_contents(SETTINGS_PATH);
        if (ok) return JSON.parse(new TextDecoder().decode(c));
    } catch (e) {}
    return {};
}

export function writeSettings(s) {
    try {
        GLib.mkdir_with_parents(SETTINGS_DIR, 0o755);
        GLib.file_set_contents(SETTINGS_PATH, JSON.stringify(s));
    } catch (e) {
        logError(e, 'ai-linux: failed to write settings.json');
    }
}

// Read-modify-write: two writer processes share the file, so never flush a cached copy wholesale.
export function saveKey(key, value) {
    const s = readSettings();
    s[key] = value;
    writeSettings(s);
    return s;
}

function writeRuntime(path, obj) {  // best-effort live apply; harmless when the engine is off
    try {
        GLib.mkdir_with_parents(RUNTIME_DIR, 0o700);
        GLib.file_set_contents(path, JSON.stringify(obj));
    } catch (e) {
        logError(e, 'ai-linux: failed to write ' + path);
    }
}

export function writeControl(obj) { writeRuntime(CONTROL_PATH, obj); }
export function writeVoice(id) { writeRuntime(VOICE_PATH, {voice: id}); }

// A wake-word pick maps to a listening-mode command for the running engine.
export function wakeControl(id) {
    if (id === 'always') return {mode: 'always'};
    if (id === 'click') return {mode: 'click'};
    return {mode: 'wake', wake_word: id};
}
