// Preferences window (Extension Manager -> Settings, or "All settings" in the top-bar menu).
// Runs in a SEPARATE gjs process from the shell extension, so it must not import extension.js —
// but it shares the SAME settings core (settingsLib.js): one storage contract, one set of options.
// The top-bar menu live-syncs to every change made here via its settings.json file monitor.
// Voice + listening also apply LIVE to a running engine via the bridge's runtime channels.

import Adw from 'gi://Adw';
import Gtk from 'gi://Gtk';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

import {
    SETTINGS_DEFAULTS, MODELS, MENU_VOICES, WAKE_WORDS, OVERLAY_POSITIONS,
    readSettings, saveKey, writeControl, writeVoice, wakeControl,
} from './settingsLib.js';

export default class AiLinuxPreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const s = readSettings();
        const page = new Adw.PreferencesPage({title: 'AI Linux', icon_name: 'preferences-system-symbolic'});
        window.add(page);
        window.set_default_size(560, 640);

        const combo = (group, title, subtitle, options, current, onPick) => {
            const row = new Adw.ComboRow({title, subtitle, model: Gtk.StringList.new(options.map(o => o.label))});
            const idx = options.findIndex(o => o.id === current);
            row.set_selected(idx >= 0 ? idx : 0);
            row.connect('notify::selected', () => onPick(options[row.get_selected()].id));
            group.add(row);
            return row;
        };
        const toggle = (group, title, subtitle, current, onFlip) => {
            const row = new Adw.SwitchRow({title, subtitle, active: !!current});
            row.connect('notify::active', () => onFlip(row.get_active()));
            group.add(row);
            return row;
        };

        const brain = new Adw.PreferencesGroup({title: 'Brain', description: 'Applies on the next Start.'});
        page.add(brain);
        combo(brain, 'Model', 'qwen3:4b is smarter; qwen3:1.7b is lighter and faster',
            MODELS, s.model ?? SETTINGS_DEFAULTS.model, id => saveKey('model', id));
        toggle(brain, 'Deep thinking', 'Needed for reliable actions; turning this off can break tool use',
            s.think ?? SETTINGS_DEFAULTS.think, on => saveKey('think', on));

        const speech = new Adw.PreferencesGroup({title: 'Voice & listening', description: 'Applies live while the assistant is running, and persists.'});
        page.add(speech);
        combo(speech, 'Voice', 'SuperTonic speaker for spoken replies',
            MENU_VOICES, s.voice ?? SETTINGS_DEFAULTS.voice,
            id => { saveKey('voice', id); writeVoice(id); });
        combo(speech, 'Listening', 'Wake word, always-on, or click-to-talk',
            WAKE_WORDS, s.wake_word ?? SETTINGS_DEFAULTS.wake_word,
            id => { saveKey('wake_word', id); writeControl(wakeControl(id)); });

        const behavior = new Adw.PreferencesGroup({title: 'Behavior', description: 'Applies on the next Start. Command-line flags always win.'});
        page.add(behavior);
        toggle(behavior, 'Allow actions', 'Let the assistant run shell and desktop actions (volume, apps, etc.)',
            s.actions ?? SETTINGS_DEFAULTS.actions, on => saveKey('actions', on));
        toggle(behavior, 'Barge-in', 'Talk over the assistant (PipeWire echo cancellation); off = half-duplex',
            s.barge_in ?? SETTINGS_DEFAULTS.barge_in, on => saveKey('barge_in', on));
        toggle(behavior, 'Window control (for GUI automation)',
            'Lets the assistant see and focus windows. Applies instantly. Full click/type automation also needs computer_use enabled in the config.',
            s.window_control ?? SETTINGS_DEFAULTS.window_control, on => saveKey('window_control', on));

        const overlay = new Adw.PreferencesGroup({title: 'Overlay', description: 'Where the on-screen orb + transcript appears.'});
        page.add(overlay);
        combo(overlay, 'Position', 'Corner of the screen',
            OVERLAY_POSITIONS, s.overlay_position ?? SETTINGS_DEFAULTS.overlay_position,
            id => saveKey('overlay_position', id));

        const logsGroup = new Adw.PreferencesGroup({title: 'Logs'});
        page.add(logsGroup);
        const logRow = new Adw.ActionRow({title: 'Open logs', subtitle: "The assistant's run log (run.log)"});
        const logBtn = new Gtk.Button({label: 'Open', valign: Gtk.Align.CENTER});
        logBtn.connect('clicked', () => {
            const p = GLib.build_filenamev([GLib.get_user_state_dir(), 'ai-linux', 'run.log']);
            if (GLib.file_test(p, GLib.FileTest.EXISTS)) {
                Gio.AppInfo.launch_default_for_uri('file://' + p, null);
            }
        });
        logRow.add_suffix(logBtn);
        logRow.activatable_widget = logBtn;
        logsGroup.add(logRow);

        const about = new Adw.PreferencesGroup({title: 'About'});
        page.add(about);
        about.add(new Adw.ActionRow({
            title: 'Version',
            subtitle: `${this.metadata['version-name'] ?? '?'}; must match ./ai-linux --version (run ./ai-linux doctor to check)`,
        }));
    }
}
