// Window-control D-Bus service. VENDORED (MIT) from computer-use-linux.
//
//   Source:  https://github.com/avifenesh/computer-use-linux
//            gnome-shell-extension/computer-use-linux@avifenesh.dev/extension.js
//   License: MIT. Copyright (c) 2026 Avi Fenesh  (see LICENSE.computer-use-linux)
//
// Why it lives here: on Wayland, only code running inside gnome-shell can enumerate or focus
// windows. The computer-use-linux Rust binary (the GUI-automation backend) calls this session
// D-Bus service BY NAME (`dev.avifenesh.ComputerUseLinux.WindowControl`), so folding it into our
// extension (keeping the exact name/path) removes the separate third-party extension while the
// binary keeps working unchanged. It is DORMANT by default: the AI Linux extension only calls
// enable() when the `window_control` setting is on, so nothing registers on the bus otherwise.
//
// Kept close to upstream on purpose (tiny, stable 2-method surface). If a future binary adds
// methods, re-sync from the source above.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const SERVICE_NAME = 'dev.avifenesh.ComputerUseLinux.WindowControl';
const OBJECT_PATH = '/dev/avifenesh/ComputerUseLinux/WindowControl';
const BACKEND = 'gnome-shell-extension';

const WINDOW_CONTROL_XML = `
<node>
  <interface name="${SERVICE_NAME}">
    <method name="ListWindows">
      <arg name="json" type="s" direction="out"/>
    </method>
    <method name="ActivateWindow">
      <arg name="window_id" type="t" direction="in"/>
      <arg name="ok" type="b" direction="out"/>
      <arg name="message" type="s" direction="out"/>
    </method>
  </interface>
</node>
`;

const WindowControlDBus = GObject.registerClass(
class WindowControlDBus extends GObject.Object {
    constructor() {
        super();

        this._dbusObject = Gio.DBusExportedObject.wrapJSObject(
            WINDOW_CONTROL_XML, this);
        this._dbusObject.export(Gio.DBus.session, OBJECT_PATH);
        this._nameId = Gio.DBus.session.own_name(
            SERVICE_NAME,
            Gio.BusNameOwnerFlags.NONE,
            null,
            () => log(`AI Linux window control lost DBus name ${SERVICE_NAME}`));
    }

    destroy() {
        if (this._nameId) {
            Gio.DBus.session.unown_name(this._nameId);
            this._nameId = 0;
        }

        this._dbusObject?.unexport();
        this._dbusObject?.run_dispose();
        this._dbusObject = null;
    }

    ListWindowsAsync(_params, invocation) {
        this._returnJson(invocation, this._listWindows());
    }

    ActivateWindowAsync([windowId], invocation) {
        const requestedId = Number(windowId);
        const window = this._listMetaWindows().find(
            candidate => Number(candidate.get_id()) === requestedId);

        if (!window) {
            invocation.return_value(new GLib.Variant('(bs)', [
                false,
                `No window matched window_id ${requestedId}`,
            ]));
            return;
        }

        try {
            if (Main.overview.visible)
                Main.overview.hide();

            if (window.minimized && typeof window.unminimize === 'function')
                window.unminimize();

            Main.activateWindow(window, global.get_current_time());
            invocation.return_value(new GLib.Variant('(bs)', [
                true,
                `Activated window_id ${requestedId}`,
            ]));
        } catch (error) {
            invocation.return_value(new GLib.Variant('(bs)', [
                false,
                `Activation failed: ${error.message}`,
            ]));
        }
    }

    _returnJson(invocation, value) {
        invocation.return_value(new GLib.Variant('(s)', [
            JSON.stringify(value),
        ]));
    }

    _listWindows() {
        return this._listMetaWindows()
            .map(window => this._windowInfo(window))
            .filter(window => window !== null);
    }

    _listMetaWindows() {
        return global.get_window_actors()
            .map(actor => actor.meta_window)
            .filter(window => window && !window.is_override_redirect?.())
            .filter(window => window.get_window_type?.() !== Meta.WindowType.DESKTOP);
    }

    _windowInfo(window) {
        if (!window)
            return null;

        const app = Shell.WindowTracker.get_default().get_window_app(window);
        const rect = window.get_frame_rect();
        const workspace = window.get_workspace?.();

        return {
            window_id: Number(window.get_id()),
            title: window.get_title?.() ?? null,
            app_id: app?.get_id?.() ?? null,
            wm_class: window.get_wm_class?.() ?? null,
            pid: window.get_pid?.() ?? null,
            bounds: rect ? {
                x: rect.x,
                y: rect.y,
                width: rect.width,
                height: rect.height,
            } : null,
            workspace: workspace?.index?.() ?? null,
            focused: global.display.focus_window === window && !Main.overview.visible,
            hidden: window.minimized ?? false,
            client_type: clientTypeName(window.get_client_type?.()),
            backend: BACKEND,
        };
    }
});

function clientTypeName(value) {
    if (value === undefined || value === null)
        return null;
    if (value === Meta.WindowClientType.WAYLAND)
        return 'wayland';
    if (value === Meta.WindowClientType.X11)
        return 'x11';
    return 'unknown';
}

// Thin lifecycle wrapper the AI Linux extension drives: enable() owns the D-Bus name only when the
// user has turned window control on; disable() releases it. Idempotent.
export class WindowControl {
    enable() {
        // Never let a D-Bus export/own failure propagate into the extension's enable(): that would
        // leave GNOME with a half-enabled extension it won't disable(), leaking the overlay chrome.
        if (this._dbus) return;
        try {
            this._dbus = new WindowControlDBus();
        } catch (e) {
            logError(e, 'ai-linux: window control failed to start');
            this._dbus = null;
        }
    }

    disable() {
        this._dbus?.destroy();
        this._dbus = null;
    }

    get active() {
        return !!this._dbus;
    }
}
